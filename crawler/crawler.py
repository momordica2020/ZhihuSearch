#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知乎内容元数据爬虫（种子 + BFS 逐页发现模式）

设计目标：
  从种子页（话题 / 作者 / 问题 / 文章）出发，解析页面内的链接，
  按内容类型抽取元数据（标题、摘要、作者、点赞、评论、链接），构建自建检索库。
  仅保存元数据，不保存全文。

提速与风控策略：
  1. 默认走 HTTP 快速通道：curl_cffi 模拟 Chrome TLS 指纹直连页面 HTML，
     解析 SSR 内嵌的 js-initialData JSON（知乎服务端渲染产物），无需启动
     浏览器，单页耗时从 ~4s 降到 ~1s。
  2. 多 Worker 并发（默认 3），配合全局/单主机令牌桶限速、随机抖动与
     指数退避：命中 403/429/验证码时自动降速，连续触发则优雅停止，不硬闯。
  3. 自动降级链（ZHIHU_AUTO_DEGRADE=1）：快速通道被风控时，先切换到
     Playwright 真实浏览器网络栈（context.request，不渲染页面，仍然较快），
     再不行才退回完整渲染模式，保证爬取不中断。
  4. 可选强制渲染模式（ZHIHU_RENDER=1）；快速通道解析不到数据的页面可走
     渲染兜底（ZHIHU_RENDER_FALLBACK=1），保持对动态页面的兼容。
  5. 已登录 Cookie（ZHIHU_COOKIE）注入请求头，绕过匿名访问限制。

产物：
  data/index.json        前端加载清单（分片列表；数据量小时为全量索引）
  data/index.json.gz     未分片时的全量 gzip 索引（前端优先加载）
  data/parts/*.json.gz   超大规模时的分片 gzip 索引
  data/seen.json         已访问 URL 记录（断点续爬，已 gitignore）
  data/stats.json        本轮统计：新增量等（已 gitignore，供常驻脚本决策）
"""

import os
import re
import sys
import json
import time
import html
import random
import logging
import gzip
import shutil
import threading
from datetime import datetime, timezone
from collections import deque
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

try:
    from .explorer import SeedPool  # 以包方式导入（测试/模块复用）
except ImportError:  # pragma: no cover
    from explorer import SeedPool  # 直接运行 crawler/crawler.py

try:
    import requests
except ImportError:  # pragma: no cover - 仅提示
    requests = None

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover - 仅提示
    cffi_requests = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - 渲染模式为可选项
    sync_playwright = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("ZHIHU_DATA_DIR", os.path.join(BASE_DIR, "data"))
SEEDS_FILE = os.environ.get(
    "ZHIHU_SEEDS", os.path.join(BASE_DIR, "seeds.txt")
)
OUTPUT_FILE = os.path.join(DATA_DIR, "index.json")
GZ_FILE = OUTPUT_FILE + ".gz"
PARTS_DIR = os.path.join(DATA_DIR, "parts")
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
FULL_DUMP = os.path.join(DATA_DIR, "index.full.json")  # 本地调试用，不入库
SEED_POOL_FILE = os.environ.get(
    "ZHIHU_SEED_POOL", os.path.join(DATA_DIR, "seed_pool.json")
)

MAX_VISITS = int(os.environ.get("ZHIHU_MAX_VISITS", "300"))
MAX_DEPTH = int(os.environ.get("ZHIHU_MAX_DEPTH", "3"))
WORKERS = max(1, min(8, int(os.environ.get("ZHIHU_WORKERS", "3"))))
# 基础单次访问间隔（秒），实际间隔 = 随机抖动 + 令牌桶全局/单主机限速
DELAY_MIN = float(os.environ.get("ZHIHU_DELAY_MIN", "0.6"))
DELAY_MAX = float(os.environ.get("ZHIHU_DELAY_MAX", "1.8"))
BACKOFF_MAX = float(os.environ.get("ZHIHU_BACKOFF_MAX", "120"))
# 全局每分钟请求上限（风控安全线，可调低但不要无脑调高）
REQ_PER_MIN = float(os.environ.get("ZHIHU_REQ_PER_MIN", "60"))
# 单主机每分钟请求上限，模拟真人浏览节奏
HOST_REQ_PER_MIN = float(os.environ.get("ZHIHU_HOST_REQ_PER_MIN", "24"))
PAGE_TIMEOUT = 30000
HTTP_TIMEOUT = float(os.environ.get("ZHIHU_HTTP_TIMEOUT", "15"))
# 页面加载后额外停留（毫秒），仅渲染模式使用
SETTLE_MS = int(os.environ.get("ZHIHU_SETTLE_MS", "300"))
# 单分片原始 JSON 字节阈值；超过后自动切换为分片存储（0 表示永不分片）
SHARD_BYTES = int(os.environ.get("ZHIHU_SHARD_BYTES", "2500000"))
RENDER_MODE = os.environ.get("ZHIHU_RENDER", "0") == "1"
RENDER_FALLBACK = os.environ.get("ZHIHU_RENDER_FALLBACK", "1") == "1"
MAX_RENDER_FALLBACK = int(os.environ.get("ZHIHU_MAX_RENDER_FALLBACK", "10"))
# 快速通道被风控时自动降级到 Playwright 网络栈/渲染模式，保证爬取不中断
AUTO_DEGRADE = os.environ.get("ZHIHU_AUTO_DEGRADE", "1") == "1"
# 连续多少次硬性风控信号（403/验证码）后停止，避免浪费时间
HARD_BLOCK_LIMIT = int(os.environ.get("ZHIHU_HARD_BLOCK_LIMIT", "3"))

# ---- 动态探索配置 ----
SEEDS_PER_ROUND = int(os.environ.get("ZHIHU_SEEDS_PER_ROUND", "8"))
PROBE_ENABLED = os.environ.get("ZHIHU_PROBE", "1") == "1"
PROBE_RATIO = float(os.environ.get("ZHIHU_PROBE_RATIO", "0.3"))
PROBE_WALK = int(os.environ.get("ZHIHU_PROBE_WALK", "50000"))
POOL_MAX = int(os.environ.get("ZHIHU_POOL_MAX", "300"))
SEED_MAX_EMPTY = int(os.environ.get("ZHIHU_SEED_MAX_EMPTY", "3"))
SEED_COOLDOWN_DAYS = float(os.environ.get("ZHIHU_SEED_COOLDOWN_DAYS", "3"))
RANDOM_SEED = os.environ.get("ZHIHU_RANDOM_SEED", "")

COOKIE_STR = os.environ.get("ZHIHU_COOKIE", "")
FIXTURE_DIR = os.environ.get("ZHIHU_FIXTURE_DIR", "")  # 调试用本地 HTML 目录

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("zhihu-crawler")

ZHI_HU_NETLOCS = {"www.zhihu.com", "zhuanlan.zhihu.com"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Referer": "https://www.zhihu.com/",
    "Upgrade-Insecure-Requests": "1",
}

INITIAL_DATA_RE = re.compile(
    r'<script id="js-initialData" type="text/json">(.*?)</script>', re.S
)
LINK_RE = re.compile(
    r'href="(https?://(?:www\.zhihu\.com|zhuanlan\.zhihu\.com)/[^"#]+)"'
)
_PAGINATED_RE = re.compile(
    r"^/(?:topic/\d+|people/[^/]+|column/[^/]+)/"
    r"(?:hot|questions|top-answers|latest|answers|posts|archive)(?:/|$)"
)


# ---------------------------------------------------------------- URL 分类
def canonical_url(url):
    """去掉片段与追踪参数；仅保留话题/人物/专栏列表页的 page 分页参数。"""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    base = "https://%s%s" % (parsed.netloc, path)
    if not parsed.query:
        return base
    if _PAGINATED_RE.match(path):
        keep = "&".join(
            "%s=%s" % (k, v[0])
            for k, v in parse_qs(parsed.query).items()
            if k == "page"
        )
        return base + ("?" + keep if keep else "")
    return base


def classify_url(url):
    """返回 content（可产出元数据）/ queue（可发现内容）/ skip。"""
    parsed = urlparse(url)
    if parsed.netloc not in ZHI_HU_NETLOCS:
        return "skip"
    path = parsed.path.rstrip("/") or "/"

    if path.startswith("/question/") or path.startswith("/p/"):
        return "content"
    # 搜索页 /search 匿名返回登录墙且独特 query 无限多，跳过以免浪费访问
    if path.startswith("/search"):
        return "skip"
    if path.startswith("/topic/"):
        parts = [p for p in path.split("/topic/", 1)[1].split("/") if p]
        if len(parts) == 1:
            return "content"  # 基础话题页（匿名可访问，含子话题与少量问题链接）
        # 登录态下的话题列表子页是重要的扩展入口，能发现大量问题链接
        if COOKIE_STR and len(parts) == 2 and parts[1] in (
            "hot", "questions", "top-answers", "latest"
        ):
            return "queue"
        return "skip"  # /hot /questions /top-answers 等子页均需登录态，匿名不可用
    if path.startswith("/people/") or path.startswith("/column/"):
        # 登录态下允许进入个人/专栏的列表子页，用于发现更多内容
        if COOKIE_STR and path.count("/") >= 3:
            section = path.split("/", 2)[2].split("/")[0]
            if section in (
                "answers", "questions", "posts", "articles", "pins",
                "following", "followers", "archive", "likes", "videos",
            ):
                return "queue"
        return "queue"
    return "skip"


def is_followable(url):
    return classify_url(url) in ("content", "queue")


# ---------------------------------------------------------------- 风控检测
def http_blocked(status, text=""):
    """HTTP 层的风控信号检测。403/429 或页面内含验证码/限流特征即视为风控。"""
    if status in (403, 429, 406):
        return True
    if status != 200:
        return False
    body = (text or "")[:30000]
    # 仅匹配确定的风控特征；注意正常 SSR JSON 里含 "captcha":{...} 字段，
    # 因此不能只匹配 captcha 单词。
    if '"code":40362' in body or "限制本次访问" in body:
        return True
    if "<title>安全验证</title>" in body or "访问验证" in body:
        return True
    return False


def render_blocked(page):
    """渲染模式的风控检测，依赖知乎风控返回的特定特征，避免误报。"""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    try:
        text = page.content()[:30000]
    except Exception:
        return False  # 页面仍在跳转/加载，视为正常页面，交给解析逻辑处理
    return '"code":40362' in text or "限制本次访问" in text


# ---------------------------------------------------------------- 限速器
class TokenBucket:
    """线程安全令牌桶：控制全局与单主机请求速率。"""

    def __init__(self, rate_per_sec, capacity):
        self.rate = rate_per_sec
        self.capacity = capacity
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity, self.tokens + (now - self.updated) * self.rate
                )
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(min(max(wait, 0.01), 2.0))


class AdaptiveThrottle:
    """自适应限速：成功时缓慢提速，风控时指数退避。"""

    def __init__(self):
        self.base_min = DELAY_MIN
        self.base_max = DELAY_MAX
        self.delay_min = DELAY_MIN
        self.delay_max = DELAY_MAX
        self.clean_streak = 0
        self.blocked_until = 0.0
        self.lock = threading.Lock()

    def on_success(self):
        with self.lock:
            self.clean_streak += 1
            if self.clean_streak >= 20 and self.delay_min > self.base_min * 0.6:
                self.delay_min = max(self.base_min * 0.6, self.delay_min * 0.9)
                self.delay_max = max(self.base_max * 0.6, self.delay_max * 0.9)
                self.clean_streak = 0
                logger.info(
                    "运行平稳，限速微调至 %.1f~%.1f 秒/请求",
                    self.delay_min, self.delay_max,
                )

    def reset(self):
        """降级切换通道时重置限速，避免把上一通道的退避带入新通道。"""
        with self.lock:
            self.delay_min = self.base_min
            self.delay_max = self.base_max
            self.clean_streak = 0
            self.blocked_until = 0.0

    def on_block(self, hard=False):
        with self.lock:
            self.clean_streak = 0
            self.delay_min = min(BACKOFF_MAX, self.delay_min * 2)
            self.delay_max = min(BACKOFF_MAX, self.delay_max * 2)
            cooldown = random.uniform(self.delay_min, self.delay_max)
            if hard:
                cooldown = max(cooldown, 30.0)
            self.blocked_until = time.monotonic() + cooldown
            logger.warning(
                "检测到风控信号%s，限速上调至 %.1f~%.1f 秒/请求，冷却 %.0f 秒",
                "（硬性）" if hard else "", self.delay_min, self.delay_max, cooldown,
            )

    def wait_before(self):
        with self.lock:
            now = time.monotonic()
            if now < self.blocked_until:
                wait = self.blocked_until - now
            else:
                wait = random.uniform(self.delay_min, self.delay_max)
        time.sleep(wait)


throttle = AdaptiveThrottle()
global_bucket = TokenBucket(REQ_PER_MIN / 60.0, max(6, WORKERS * 3))
_host_buckets = {}
_host_lock = threading.Lock()


def host_bucket(host):
    with _host_lock:
        b = _host_buckets.get(host)
        if b is None:
            b = TokenBucket(HOST_REQ_PER_MIN / 60.0, 3)
            _host_buckets[host] = b
        return b


# ---------------------------------------------------------------- HTTP 快速通道
def fixture_path(url):
    """调试钩子：ZHIHU_FIXTURE_DIR 下按 URL 路径找本地 HTML 文件。"""
    path = urlparse(url).path.strip("/").replace("/", "_")
    return os.path.join(FIXTURE_DIR, path + ".html") if path else ""


def http_get(url):
    """请求页面 HTML，返回 (status, text)。风控时抛 BlockedError。"""
    if FIXTURE_DIR:
        fp = fixture_path(url)
        if fp and os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                return 200, f.read()
        return 404, ""

    headers = dict(HEADERS)
    if COOKIE_STR:
        headers["Cookie"] = COOKIE_STR
    # curl_cffi 模拟 Chrome 的 TLS/HTTP2 指纹，比纯 requests 更接近真人浏览器；
    # 没有 curl_cffi 时退回 requests（可能被风控，届时会自动降级到 Playwright）
    if cffi_requests is not None:
        resp = cffi_requests.get(
            url, headers=headers, timeout=HTTP_TIMEOUT,
            impersonate="chrome124", allow_redirects=True,
        )
        return resp.status_code, resp.text
    if requests is not None:
        resp = requests.get(
            url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True
        )
        return resp.status_code, resp.text
    raise RuntimeError(
        "HTTP 快速通道需要 curl_cffi 或 requests：pip install curl_cffi requests"
    )


# ---------------------------------------------------------------- SSR 数据解析
def extract_initial_data(html_text):
    m = INITIAL_DATA_RE.search(html_text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def strip_html(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()


def parse_entities(data):
    """从 js-initialData 抽取元数据条目与可跟进链接。"""
    st = (data or {}).get("initialState") or {}
    ctx = (st.get("env") or {}).get("ctx") or {}
    path = (ctx or {}).get("path") or ""
    ent = st.get("entities") or {}

    items = []
    links = set()

    def add_question(q):
        if not isinstance(q, dict) or not q.get("url"):
            return
        title = q.get("title") or ""
        if not title:
            return
        items.append({
            "kind": "问题",
            "title": title,
            "excerpt": "",
            "author": "",
            "url": q["url"],
            "votes": 0,
            "comments": q.get("comment_count") or 0,
            "time": q.get("created_time") or 0,
        })

    def add_answer(a):
        if not isinstance(a, dict) or not a.get("url"):
            return
        title = ""
        q = a.get("question")
        if isinstance(q, dict):
            title = q.get("title") or ""
            qid = q.get("id")
            if qid:
                links.add(canonical_url("https://www.zhihu.com/question/%s" % qid))
        author = ""
        au = a.get("author")
        if isinstance(au, dict):
            author = au.get("name") or ""
        if not (title or author):
            return
        excerpt = (a.get("excerpt") or strip_html(a.get("content")))[:160]
        items.append({
            "kind": "回答",
            "title": title,
            "excerpt": excerpt,
            "author": author,
            "url": a["url"],
            "votes": a.get("voteup_count") or 0,
            "comments": a.get("comment_count") or 0,
            "time": a.get("created_time") or 0,
        })

    def add_article(ar):
        if not isinstance(ar, dict) or not ar.get("url") or not ar.get("title"):
            return
        author = ""
        au = ar.get("author")
        if isinstance(au, dict):
            author = au.get("name") or ""
        items.append({
            "kind": "文章",
            "title": ar["title"],
            "excerpt": (ar.get("excerpt") or strip_html(ar.get("content")))[:160],
            "author": author,
            "url": ar["url"],
            "votes": ar.get("voteup_count") or 0,
            "comments": ar.get("comment_count") or 0,
            "time": ar.get("created_time") or 0,
        })

    for q in (ent.get("questions") or {}).values():
        add_question(q)
    for a in (ent.get("answers") or {}).values():
        add_answer(a)
    for ar in (ent.get("articles") or {}).values():
        add_article(ar)

    # 话题/列表页的问题通常挂在 feeds 里，target 指向实体 id
    for feed in (ent.get("feeds") or {}).values():
        if not isinstance(feed, dict):
            continue
        target = feed.get("target")
        if isinstance(target, dict):
            ttype = target.get("type")
            if ttype == "question":
                add_question(target)
            elif ttype == "answer":
                add_answer(target)
            elif ttype == "article":
                add_article(target)

    # 子话题链接继续跟进
    for t in (ent.get("topics") or {}).values():
        if isinstance(t, dict) and t.get("url"):
            links.add(canonical_url(t["url"]))

    return items, links, path


def collect_html_links(html_text):
    """直接从 HTML 收集值得跟进的链接，替代浏览器 DOM 遍历。"""
    links = set()
    for m in LINK_RE.finditer(html_text or ""):
        href = canonical_url(m.group(1))
        if is_followable(href):
            links.add(href)
    return links


def fetch_and_parse(url):
    """单个 Worker：限速 -> 请求 -> 解析。返回 (items, links, blocked)。"""
    host = urlparse(url).netloc
    throttle.wait_before()
    host_bucket(host).acquire()
    global_bucket.acquire()
    try:
        status, text = http_get(url)
    except Exception as exc:
        logger.warning("请求失败 %s: %s", url, exc)
        return None, None, None

    if status == 403:
        return None, None, "hard-block"
    if http_blocked_for(status, text):
        return None, None, "soft-block"
    if status != 200 or not text:
        return None, None, None

    data = extract_initial_data(text)
    items, links, _path = parse_entities(data) if data else ([], set(), "")
    # SSR JSON 与 HTML 链接是互补的信息源：JSON 拿不到时就靠 HTML 发现
    links |= collect_html_links(text)
    if not items and not links:
        return [], set(), None
    throttle.on_success()
    return items, links, None


def http_blocked_for(status, text):
    return http_blocked(status, text)


def make_rng():
    if RANDOM_SEED and RANDOM_SEED != "0":
        return random.Random(int(RANDOM_SEED))
    return random.Random()


# ---------------------------------------------------------------- 渲染模式（Playwright 兜底）
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
    anchor = item.get_attribute("id") or ""
    return {
        "kind": "回答",
        "title": title,
        "excerpt": excerpt,
        "author": author,
        "url": "%s#answer-%s" % (page.url, anchor or uniq),
        "votes": votes,
        "comments": comments,
        "time": 0,
    }


def extract_article(page):
    title = _text(page.locator("h1.Post-Title"))
    if not title:
        return None
    return {
        "kind": "文章",
        "title": title,
        "excerpt": _text(page.locator(".Post-RichTextContainer"))[:160],
        "author": _text(page.locator(".AuthorInfo-name")),
        "url": page.url,
        "votes": _count_from(_text(page.locator(".VoteButton--up"))),
        "comments": 0,
        "time": 0,
    }


def extract_question(page):
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


def render_collect_links(page):
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


def render_page(page, url):
    """渲染单个页面并抽取元数据/链接；返回 (items, links) 或 (None, 风控标记)。"""
    throttle.wait_before()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        if SETTLE_MS > 0:
            page.wait_for_timeout(SETTLE_MS)
    except Exception as exc:
        logger.warning("渲染加载失败 %s: %s", url, exc)
        return None, None

    try:
        if render_blocked(page):
            return None, "hard-block"
    except Exception:
        pass

    kind = classify_url(url)
    items = []
    try:
        if kind == "content":
            if "/topic/" in url:
                for item in page.locator(".ContentItem").all():
                    title, link = extract_question_entry(item)
                    if title and link:
                        items.append({
                            "kind": "问题", "title": title, "excerpt": "",
                            "author": "", "url": link, "votes": 0,
                            "comments": 0, "time": 0,
                        })
            elif "/p/" in url:
                meta = extract_article(page)
                if meta:
                    items.append(meta)
            elif "/question/" in url:
                q = extract_question(page)
                qt = q["title"] if q else ""
                if q:
                    items.append(q)
                for i, item in enumerate(page.locator(".ContentItem").all()):
                    rec = extract_answer(page, item, qt, str(i))
                    if rec["title"]:
                        items.append(rec)
    except Exception as exc:
        logger.debug("页面解析失败: %s", exc)

    links = render_collect_links(page)
    return items, links


def _inject_cookies(context):
    if not COOKIE_STR:
        return
    try:
        for part in COOKIE_STR.split(";"):
            if "=" not in part:
                continue
            name, value = part.strip().split("=", 1)
            context.add_cookies([{
                "name": name.strip(), "value": value.strip(),
                "domain": "zhihu.com", "path": "/",
            }])
        logger.info("已注入 Cookie（%d 个键值）", COOKIE_STR.count("="))
    except Exception as exc:
        logger.warning("Cookie 注入失败: %s", exc)


def run_render_pass(urls, results, seen_by_url):
    """对快速通道解析不到数据的少量页面做渲染兜底（单线程）。"""
    if sync_playwright is None:
        logger.warning("未安装 playwright，跳过渲染兜底")
        return
    logger.info("渲染兜底 %d 个页面…", len(urls))
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
            user_agent=HEADERS["User-Agent"],
            locale="zh-CN",
            viewport={"width": 1366, "height": 900},
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        _inject_cookies(context)
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)
        try:
            for url in urls:
                if url in seen_by_url:
                    continue
                items, links = render_page(page, url)
                if items is None and links == "hard-block":
                    throttle.on_block(hard=True)
                    logger.warning("渲染兜底命中风控，停止")
                    break
                for rec in items or []:
                    if rec.get("url") and rec["url"] not in seen_by_url:
                        seen_by_url.add(rec["url"])
                        results.append(rec)
        finally:
            try:
                browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------- 输出
def write_gzip(path, text):
    with open(path, "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as gz:
            gz.write(text.encode("utf-8"))


def save_progress(results, seen_by_url, stats=None):
    """紧凑 JSON + gzip；超大规模时自动分片，控制单文件与增量体积。"""
    os.makedirs(PARTS_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    count = len(results)
    stats = stats or {}

    # 本地调试用完整快照（不入 git）
    with open(FULL_DUMP, "w", encoding="utf-8") as f:
        json.dump(
            {"generated_at": stamp, "count": count, "items": results},
            f, ensure_ascii=False, separators=(",", ":"),
        )

    items_json = json.dumps(results, ensure_ascii=False, separators=(",", ":"))
    if SHARD_BYTES > 0 and len(items_json) > SHARD_BYTES:
        # 分片：按原始 JSON 字节精确切割，写入 data/parts/*.json.gz
        shards = []
        chunk, chunk_bytes = [], 0
        for item in results:
            item_json = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            if chunk and chunk_bytes + len(item_json) > SHARD_BYTES:
                shards.append(chunk)
                chunk, chunk_bytes = [], 0
            chunk.append(item)
            chunk_bytes += len(item_json)
        if chunk:
            shards.append(chunk)

        names = []
        for idx, chunk in enumerate(shards):
            fname = "index.%04d.json.gz" % idx
            write_gzip(os.path.join(PARTS_DIR, fname),
                       json.dumps(chunk, ensure_ascii=False, separators=(",", ":")))
            names.append("parts/" + fname)
        # 避免与旧的单文件快照并存，造成仓库里出现两份全量数据
        if os.path.exists(GZ_FILE):
            os.remove(GZ_FILE)
        manifest = {
            "generated_at": stamp,
            "count": count,
            "shards": names,
        }
        compact = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(compact)
        shard_count = len(names)
        logger.info("已保存 %d 条元数据（分片 %d 个）", count, shard_count)
    else:
        # 数据量小：单快照。清理历史分片，避免仓库膨胀
        if os.path.isdir(PARTS_DIR):
            shutil.rmtree(PARTS_DIR, ignore_errors=True)
        index = {
            "generated_at": stamp,
            "count": count,
            "items": results,
        }
        compact = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(compact)
        write_gzip(GZ_FILE, compact)
        shard_count = 1
        logger.info("已保存 %d 条元数据（单快照）", count)

    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_by_url), f, ensure_ascii=False)

    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": stamp,
            "total": count,
            "new_items": stats.get("new_items", 0),
            "visited": stats.get("visited", 0),
            "mode": stats.get("mode", "http"),
            "shards": shard_count,
        }, f, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- 主流程
def load_history(results, seen_by_url):
    """断点续爬：加载上次已收录的索引与 URL。返回加载的条目数。"""
    loaded = 0
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                seen_by_url.update(json.load(f))
        except Exception as exc:
            logger.warning("读取 seen.json 失败，从头开始: %s", exc)
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
            items = old.get("items") or []
            if not items:
                # 分片模式：从 parts 重新合并历史
                parts_dir = PARTS_DIR
                for name in sorted(os.listdir(parts_dir)):
                    if not name.endswith(".json.gz"):
                        continue
                    with gzip.open(os.path.join(parts_dir, name), "rt", encoding="utf-8") as f:
                        items.extend(json.load(f))
            for item in items:
                url = item.get("url", "")
                if url:
                    seen_by_url.add(url)
                    results.append(item)
            loaded = len(items)
            logger.info("已加载 %d 条历史索引、%d 条已访问 URL，断点续爬",
                        loaded, len(seen_by_url))
        except Exception as exc:
            logger.warning("读取历史索引失败: %s", exc)
    return loaded


def run_http_crawl(initial, results, seen_by_url, render_queue, explorer=None):
    """HTTP 快速通道 + 并发 Worker 主循环；explorer 用于动态种子反馈。"""
    visited = set()
    content_q = deque((s, 0) for s in initial)
    queue_q = deque()
    stats = {"visited": 0, "new_items": 0, "mode": "http"}
    hard_blocks = 0
    stop_event = threading.Event()

    def process_result(url, depth, items, links, blocked):
        nonlocal hard_blocks
        if blocked == "hard-block":
            hard_blocks += 1
            throttle.on_block(hard=True)
            if hard_blocks >= HARD_BLOCK_LIMIT:
                if AUTO_DEGRADE and sync_playwright is not None:
                    logger.warning(
                        "快速通道连续 %d 次硬性风控，自动切换到 Playwright 网络栈…",
                        hard_blocks,
                    )
                    stats["degrade"] = "playwright-http"
                else:
                    logger.warning(
                        "连续 %d 次硬性风控信号，停止爬取。请刷新 Cookie 或调低 ZHIHU_REQ_PER_MIN。",
                        hard_blocks,
                    )
                stop_event.set()
            return
        if blocked == "soft-block":
            throttle.on_block(hard=False)
            return
        if items is None:
            if explorer is not None:
                explorer.observe(url, 0, ok=False)
            return
        throttle.on_success()

        if not items and not links and classify_url(url) == "content":
            # SSR 解析为空：留给渲染兜底
            if explorer is not None:
                explorer.observe(url, 0, ok=True)  # 空转一次，累计后自动静默
            if RENDER_FALLBACK and url not in seen_by_url and len(render_queue) < MAX_RENDER_FALLBACK:
                render_queue.append(url)
            return

        new_count = 0
        for rec in items:
            u = rec.get("url", "")
            if u and u not in seen_by_url:
                seen_by_url.add(u)
                results.append(rec)
                new_count += 1
        if explorer is not None:
            explorer.observe(url, new_count, ok=True)
            explorer.discover(links)
        for link in links:
            if link in visited or link in seen_by_url:
                continue
            lk = classify_url(link)
            if lk == "content":
                content_q.append((link, depth + 1))
            elif lk == "queue":
                queue_q.append((link, depth + 1))

    def worker(url, depth):
        return url, depth, *fetch_and_parse(url)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        pending = {}
        while not stop_event.is_set() and (pending or content_q or queue_q):
            while len(pending) < WORKERS and stats["visited"] < MAX_VISITS:
                if content_q:
                    url, depth = content_q.popleft()
                elif queue_q:
                    url, depth = queue_q.popleft()
                else:
                    break
                if url in visited or url in seen_by_url:
                    continue
                visited.add(url)
                stats["visited"] += 1
                logger.info("访问 %d/%d 深度=%d: %s",
                            stats["visited"], MAX_VISITS, depth, url)
                fut = pool.submit(worker, url, depth)
                pending[fut] = (url, depth)

            if not pending:
                break
            done, _ = wait(list(pending), timeout=60, return_when=FIRST_COMPLETED)
            for fut in done:
                url, depth = pending.pop(fut)
                try:
                    url, depth, items, links, blocked = fut.result()
                except Exception as exc:
                    logger.warning("Worker 异常 %s: %s", url, exc)
                    continue
                process_result(url, depth, items, links, blocked)
            if stats["visited"] % 20 == 0:
                save_progress(results, seen_by_url,
                              {**stats, "new_items": len(results) - initial_count})

    stats["new_items"] = len(results) - initial_count
    return stats


def run_playwright_http_crawl(seeds, results, seen_by_url, render_queue):
    """Playwright 真实浏览器网络栈：不渲染页面，但 TLS/HTTP2/UA 与真浏览器一致。

    用于快速通道（curl_cffi/requests）被风控时的自动降级；仍比完整渲染快数倍。
    """
    if sync_playwright is None:
        logger.warning("未安装 playwright，无法降级到 Playwright 网络栈")
        return {"visited": 0, "new_items": 0, "mode": "playwright-http"}
    visited = set()
    content_q = deque((s, 0) for s in seeds)
    queue_q = deque()
    stats = {"visited": 0, "new_items": 0, "mode": "playwright-http"}
    hard_blocks = 0
    throttle.reset()

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
            user_agent=HEADERS["User-Agent"],
            locale="zh-CN",
            viewport={"width": 1366, "height": 900},
        )
        _inject_cookies(context)
        api = context.request
        try:
            while stats["visited"] < MAX_VISITS:
                if content_q:
                    url, depth = content_q.popleft()
                elif queue_q:
                    url, depth = queue_q.popleft()
                else:
                    break
                if url in visited:
                    continue
                visited.add(url)
                stats["visited"] += 1
                logger.info("访问 %d/%d 深度=%d: %s",
                            stats["visited"], MAX_VISITS, depth, url)

                throttle.wait_before()
                host_bucket(urlparse(url).netloc).acquire()
                global_bucket.acquire()
                try:
                    resp = api.get(url, timeout=int(HTTP_TIMEOUT * 1000))
                    status, text = resp.status, resp.text()
                except Exception as exc:
                    logger.warning("请求失败 %s: %s", url, exc)
                    continue

                if status == 403:
                    hard_blocks += 1
                    throttle.on_block(hard=True)
                    if hard_blocks >= HARD_BLOCK_LIMIT:
                        logger.warning(
                            "Playwright 网络栈连续 %d 次硬性风控，降级到渲染模式…",
                            hard_blocks,
                        )
                        stats["degrade"] = "render"
                        break
                    continue
                if http_blocked_for(status, text):
                    throttle.on_block(hard=False)
                    continue
                if status != 200 or not text:
                    continue

                data = extract_initial_data(text)
                items, links, _path = parse_entities(data) if data else ([], set(), "")
                links |= collect_html_links(text)
                throttle.on_success()

                if not items and not links and classify_url(url) == "content":
                    if (RENDER_FALLBACK and url not in seen_by_url
                            and len(render_queue) < MAX_RENDER_FALLBACK):
                        render_queue.append(url)
                    continue

                for rec in items:
                    u = rec.get("url", "")
                    if u and u not in seen_by_url:
                        seen_by_url.add(u)
                        results.append(rec)
                if depth < MAX_DEPTH:
                    for link in links:
                        if link in visited or link in seen_by_url:
                            continue
                        lk = classify_url(link)
                        if lk == "content":
                            content_q.append((link, depth + 1))
                        elif lk == "queue":
                            queue_q.append((link, depth + 1))
                if stats["visited"] % 20 == 0:
                    save_progress(results, seen_by_url,
                                  {**stats, "new_items": len(results) - initial_count})
        finally:
            try:
                browser.close()
            except Exception:
                pass

    stats["new_items"] = len(results) - initial_count
    return stats


def run_render_mode(seeds, results, seen_by_url):
    """强制渲染模式（兼容旧行为，但限速改为自适应）。"""
    if sync_playwright is None:
        logger.error("渲染模式需要 playwright：pip install playwright && playwright install chromium")
        return {"visited": 0, "new_items": 0, "mode": "render"}
    visited = set()
    content_q = deque((s, 0) for s in seeds)
    queue_q = deque()
    stats = {"visited": 0, "new_items": 0, "mode": "render"}
    hard_blocks = 0
    throttle.reset()

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
            user_agent=HEADERS["User-Agent"],
            locale="zh-CN",
            viewport={"width": 1366, "height": 900},
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        _inject_cookies(context)
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)
        try:
            while stats["visited"] < MAX_VISITS:
                if content_q:
                    url, depth = content_q.popleft()
                elif queue_q:
                    url, depth = queue_q.popleft()
                else:
                    break
                if url in visited:
                    continue
                visited.add(url)
                stats["visited"] += 1
                logger.info("访问 %d/%d 深度=%d: %s",
                            stats["visited"], MAX_VISITS, depth, url)
                items, links = render_page(page, url)
                if items is None and links == "hard-block":
                    hard_blocks += 1
                    throttle.on_block(hard=True)
                    if hard_blocks >= HARD_BLOCK_LIMIT:
                        logger.warning("连续 %d 次硬性风控信号，停止爬取。", hard_blocks)
                        break
                    continue
                if items is None:
                    continue
                throttle.on_success()
                for rec in items:
                    u = rec.get("url", "")
                    if u and u not in seen_by_url:
                        seen_by_url.add(u)
                        results.append(rec)
                if depth < MAX_DEPTH:
                    for link in links:
                        if link in visited or link in seen_by_url:
                            continue
                        lk = classify_url(link)
                        if lk == "content":
                            content_q.append((link, depth + 1))
                        elif lk == "queue":
                            queue_q.append((link, depth + 1))
                if stats["visited"] % 20 == 0:
                    save_progress(results, seen_by_url,
                                  {**stats, "new_items": len(results) - initial_count})
        finally:
            try:
                browser.close()
            except Exception:
                pass

    stats["new_items"] = len(results) - initial_count
    return stats


initial_count = 0


def merge_stats(a, b, mode):
    return {
        "visited": a.get("visited", 0) + b.get("visited", 0),
        "new_items": max(a.get("new_items", 0), b.get("new_items", 0)),
        "mode": mode,
        "degrade": b.get("degrade"),
    }


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
    global initial_count
    initial_count = load_history(results, seen_by_url)
    if COOKIE_STR:
        logger.info("已配置 ZHIHU_COOKIE（%d 个字符），以登录态访问", len(COOKIE_STR))
    else:
        logger.info("未配置 ZHIHU_COOKIE，以匿名状态访问（可能被登录墙拦截）")

    # 动态种子池：每轮按产出分随机挑选种子，随机 ID 游走探测新内容
    explorer = SeedPool(
        SEED_POOL_FILE, seeds,
        max_size=POOL_MAX, max_empty=SEED_MAX_EMPTY,
        cooldown_days=SEED_COOLDOWN_DAYS, rng=make_rng(),
    )
    explorer.add_anchors(seen_by_url)

    try:
        if RENDER_MODE:
            stats = run_render_mode(seeds, results, seen_by_url)
        else:
            selected = explorer.select(SEEDS_PER_ROUND)
            probes = []
            if PROBE_ENABLED:
                probes = explorer.make_probes(
                    max(0, int(MAX_VISITS * PROBE_RATIO)), PROBE_WALK
                )
            initial = (selected + probes) if selected else (seeds + probes)
            logger.info(
                "动态种子池 %d 个，本轮 %d 个种子 + %d 个随机探测",
                len(explorer.seeds), len(selected), len(probes),
            )
            render_queue = []
            stats = run_http_crawl(initial, results, seen_by_url,
                                   render_queue, explorer=explorer)
            # 自动降级链：快速通道 -> Playwright 网络栈 -> 完整渲染
            if stats.get("degrade") == "playwright-http":
                stats2 = run_playwright_http_crawl(initial, results, seen_by_url, render_queue)
                stats = merge_stats(stats, stats2, "http->playwright-http")
                if stats2.get("degrade") == "render":
                    stats3 = run_render_mode(seeds, results, seen_by_url)
                    stats = merge_stats(stats, stats3, "http->playwright-http->render")
            if render_queue:
                run_render_pass(render_queue, results, seen_by_url)
                stats["mode"] = "http+render"
    except KeyboardInterrupt:
        logger.warning("收到 Ctrl+C，保存进度后退出")
        stats = {"visited": 0, "new_items": 0, "mode": "interrupted"}
    finally:
        explorer.prune()
        explorer.save()
        logger.info("种子池更新完成：%d 个种子已持久化", len(explorer.seeds))

    results.sort(key=lambda r: r.get("votes", 0), reverse=True)
    save_progress(results, seen_by_url, stats)
    logger.info(
        "爬取完成：新增 %d 条，共收录 %d 条（访问 %d 页）",
        stats.get("new_items", 0), len(results), stats.get("visited", 0),
    )


if __name__ == "__main__":
    main()
