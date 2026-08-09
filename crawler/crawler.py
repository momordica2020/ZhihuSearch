#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知乎内容元数据爬虫（种子 + BFS 逐页发现模式）

设计目标：
  从种子页（话题 / 作者 / 问题 / 文章）出发，解析页面内的链接，
  按内容类型抽取元数据（标题、摘要、作者、点赞、评论、链接），构建自建检索库。
  仅保存元数据，不保存全文。

反爬应对策略：
  1. 使用 Playwright 真实 Chromium 内核，执行页面 JS，接近真人访问。
  2. 串行访问，每次之间随机限速；单页超时保护。
  3. 通过环境变量注入已登录 Cookie（ZHIHU_COOKIE），绕过匿名访问限制。
  4. 检测验证码 / 登录墙时优雅停止，不硬闯。

产物：
  data/index.json   前端搜索使用的元数据索引
  data/seen.json    已访问 URL 记录（断点续爬）
"""

import os
import re
import sys
import json
import time
import random
import logging
from urllib.parse import urlparse
from datetime import datetime, timezone
from collections import deque

from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEDS_FILE = os.path.join(BASE_DIR, "seeds.txt")
OUTPUT_FILE = os.path.join(BASE_DIR, "data", "index.json")
SEEN_FILE = os.path.join(BASE_DIR, "data", "seen.json")

MAX_VISITS = int(os.environ.get("ZHIHU_MAX_VISITS", "300"))
MAX_DEPTH = int(os.environ.get("ZHIHU_MAX_DEPTH", "3"))
DELAY_RANGE = (2.5, 5.0)
PAGE_TIMEOUT = 30000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("zhihu-crawler")

ZHI_HU_NETLOCS = {"www.zhihu.com", "zhuanlan.zhihu.com"}


# ---------------------------------------------------------------- URL 分类
def classify_url(url):
    """返回 content（可产出元数据）/ queue（可发现内容）/ skip。"""
    parsed = urlparse(url)
    if parsed.netloc not in ZHI_HU_NETLOCS:
        return "skip"
    path = parsed.path

    if path.startswith("/question/"):
        return "content"
    if path.startswith("/p/"):  # 专栏文章
        return "content"
    # 搜索页 /search 匿名返回登录墙且独特 query 无限多，跳过以免浪费访问
    if path.startswith("/search"):
        return "skip"

    if path.startswith("/topic/"):
        parts = path.split("/topic/", 1)[1].split("/")
        if len(parts) == 1:
            return "content"  # 基础话题页（匿名可访问，含子话题与少量问题链接）
        return "skip"  # /hot /questions /top-answers 等子页均需登录态，匿名不可用

    if path.startswith("/people/") or path.startswith("/column/"):
        return "queue"

    return "skip"


def is_followable(url):
    return classify_url(url) in ("content", "queue")


# ---------------------------------------------------------------- 元数据抽取
def _text(locator, default=""):
    try:
        return locator.first.inner_text(timeout=2500).strip()
    except Exception:
        return default


def _count_from(vote_text):
    m = re.search(r"[\d.,kK万]+", vote_text or "")
    if not m:
        return 0
    raw = m.group(0).replace(",", "").lower()
    if "万" in raw or "k" in raw:
        return int(float(raw.replace("万", "").replace("k", "")) * 1000)
    return int(float(raw))


def _abs_url(href):
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://www.zhihu.com" + href
    return href


def extract_question_entry(item):
    """从页面条目中抽取问题标题 + 链接（话题页 / 列表页通用）。"""
    link = None
    for a in item.locator('a[href*="/question/"]').all():
        href = a.get_attribute("href") or ""
        if href:
            link = _abs_url(href)
            break
    title = _text(item.locator(".ContentItem-title, .QuestionItem-title"))
    if not title and link:
        # 退而求其次取链接文本
        try:
            title = (item.locator('a[href*="/question/"]').first.inner_text(timeout=1500) or "").strip()
        except Exception:
            title = ""
    return title, link


def extract_answer(page, item, question_title="", uniq=""):
    """从回答条目抽取元数据。item 为 .ContentItem 定位器。"""
    title = question_title or _text(item.locator(".QuestionItem-title a, .ContentItem-title a"))
    excerpt = _text(item.locator(".RichText.CollapsedText, .RichContent-inner"))[:160]
    author = _text(item.locator(".AuthorInfo-name"))
    votes = _count_from(_text(item.locator(".VoteButton--up, .VoteButton")))
    comments = _count_from(_text(item.locator(".ContentItem-actions a:has-text('评论')")))
    # 回答与问题共用 page.url，需附加唯一锚点避免被去重误删
    anchor = item.get_attribute("id") or ""
    return {
        "kind": "回答",
        "title": title,
        "excerpt": excerpt,
        "author": author,
        "url": f"{page.url}#answer-{anchor or uniq}",
        "votes": votes,
        "comments": comments,
        "time": 0,
    }


def extract_article(page):
    """从专栏文章页抽取元数据。"""
    title = _text(page.locator("h1.Post-Title"))
    if not title:
        return None
    author = _text(page.locator(".AuthorInfo-name"))
    votes = _count_from(_text(page.locator(".VoteButton--up")))
    return {
        "kind": "文章",
        "title": title,
        "excerpt": _text(page.locator(".Post-RichTextContainer"))[:160],
        "author": author,
        "url": page.url,
        "votes": votes,
        "comments": 0,
        "time": 0,
    }


def extract_question(page):
    """从问题页抽取标题。"""
    title = _text(page.locator("h1.QuestionHeader-title"))
    if not title:
        return None
    return {
        "kind": "问题",
        "title": title,
        "excerpt": "",
        "author": "",
        "url": page.url,
        "votes": 0,
        "comments": 0,
        "time": 0,
    }


def collect_links(page):
    """收集当前页面所有值得跟进的链接。"""
    links = set()
    try:
        for href in page.evaluate(
            "Array.from(document.querySelectorAll('a')).map(a => a.href)"
        ):
            if href and is_followable(href):
                links.add(href)
    except Exception as exc:
        logger.debug("链接提取失败: %s", exc)
    return links


# ---------------------------------------------------------------- 风控检测
def blocked(page):
    """检测是否被知乎风控拦截。仅依赖知乎风控返回的特定 JSON 特征，避免误报。"""
    text = page.content()[:30000]
    return '"code":40362' in text or "限制本次访问" in text


# ---------------------------------------------------------------- 主流程
def main():
    if not os.path.exists(SEEDS_FILE):
        logger.error("未找到 %s，请先填写种子链接", SEEDS_FILE)
        sys.exit(1)

    seeds = []
    with open(SEEDS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            seeds.append(line)

    results = []
    seen_by_url = set()
    visited = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-logging", "--log-level=3", "--no-sandbox",
                "--disable-crash-reporter",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
            viewport={"width": 1366, "height": 900},
        )
        # 隐藏 webdriver 标记，降低被知乎识别为自动化浏览器的概率
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )

        cookie_str = os.environ.get("ZHIHU_COOKIE")
        if cookie_str:
            try:
                for part in cookie_str.split(";"):
                    if "=" not in part:
                        continue
                    name, value = part.strip().split("=", 1)
                    context.add_cookies([{
                        "name": name.strip(),
                        "value": value.strip(),
                        "domain": "zhihu.com",
                        "path": "/",
                    }])
                logger.info("已注入 Cookie")
            except Exception as exc:
                logger.warning("Cookie 注入失败: %s", exc)

        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        # 双队列：先处理内容页，避免被话题页淹没
        content_q = deque()
        queue_q = deque()
        for s in seeds:
            content_q.append((s, 0))

        visits = 0
        try:
            while visits < MAX_VISITS:
                # 优先取内容页
                if content_q:
                    url, depth = content_q.popleft()
                elif queue_q:
                    url, depth = queue_q.popleft()
                else:
                    break

                if url in visited:
                    continue
                visited.add(url)
                visits += 1

                kind = classify_url(url)
                if kind == "skip":
                    continue

                time.sleep(random.uniform(*DELAY_RANGE))
                logger.info("访问 %d/%d 深度=%d: %s", visits, MAX_VISITS, depth, url)

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                    page.wait_for_timeout(2500)
                except Exception as exc:
                    logger.warning("加载失败: %s", exc)
                    continue

                if blocked(page):
                    logger.warning("检测到风控/验证码，停止爬取。请调整限速或刷新 Cookie。")
                    break

                # 按页面类型产出元数据
                if kind == "content":
                    if "/topic/" in url:
                        try:
                            for item in page.locator(".ContentItem").all():
                                title, link = extract_question_entry(item)
                                if title and link and link not in seen_by_url:
                                    seen_by_url.add(link)
                                    results.append({
                                        "kind": "问题",
                                        "title": title,
                                        "excerpt": "",
                                        "author": "",
                                        "url": link,
                                        "votes": 0,
                                        "comments": 0,
                                        "time": 0,
                                    })
                        except Exception as exc:
                            logger.debug("话题页解析失败: %s", exc)
                    elif "/p/" in url:
                        meta = extract_article(page)
                        if meta and meta["url"] not in seen_by_url:
                            seen_by_url.add(meta["url"])
                            results.append(meta)
                    elif "/question/" in url:
                        q = extract_question(page)
                        qt = q["title"] if q else ""
                        if q and q["url"] not in seen_by_url:
                            seen_by_url.add(q["url"])
                            results.append(q)
                        try:
                            for i, item in enumerate(page.locator(".ContentItem").all()):
                                rec = extract_answer(page, item, qt, str(i))
                                if rec["title"] and rec["url"] not in seen_by_url:
                                    seen_by_url.add(rec["url"])
                                    results.append(rec)
                        except Exception as exc:
                            logger.debug("回答解析失败: %s", exc)

                # 收集新链接
                if depth < MAX_DEPTH:
                    for link in collect_links(page):
                        if link in visited:
                            continue
                        lk = classify_url(link)
                        if lk == "content":
                            content_q.append((link, depth + 1))
                        elif lk == "queue":
                            queue_q.append((link, depth + 1))

                if visits % 20 == 0:
                    save_progress(results, seen_by_url)
        finally:
            try:
                browser.close()
            except Exception:
                pass

    results.sort(key=lambda r: r["votes"], reverse=True)
    save_progress(results, seen_by_url)
    logger.info("爬取完成，共收录 %d 条元数据", len(results))


def save_progress(results, seen_by_url):
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    index = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(results),
        "items": results,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_by_url), f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
