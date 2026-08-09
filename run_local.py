#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知乎搜索索引 - 本地常驻爬取循环

持续运行：每轮执行一次爬虫，并在有足够新增数据时提交推送，
实现"24h 不断更新"知乎内容索引的效果，同时避免高频小提交撑爆仓库。

用法：
    python run_local.py                 # 以默认配置常驻运行
    python run_local.py --rounds 10     # 只跑 10 轮后退出（用于验证）
    python run_local.py --visits 60     # 单轮访问上限覆盖
    python run_local.py --min-new 20    # 新增达到 20 条才提交
    python run_local.py --push-interval 3600  # 至少间隔 1 小时才推送

说明：
    需要保持电脑开机运行；Ctrl+C 可正常结束当前轮并保存进度。
    首次请先设置 ZHIHU_COOKIE 环境变量（可选，提升可见内容覆盖率）。
    数据文件默认只提交 data/index.json、index.json.gz 与 data/parts/ 分片；
    seen.json / stats.json / index.full.json 为可再生的断点与调试文件，不入库。
"""

import os
import sys
import json
import time
import argparse
import logging
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CRAWLER = os.path.join(BASE_DIR, "crawler", "crawler.py")
STATS_FILE = os.path.join(BASE_DIR, "data", "stats.json")
PUSH_STATE = os.path.join(BASE_DIR, "data", ".push_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("local-runner")

DEFAULT_ROUND_INTERVAL = 0
# 需要 git 跟踪的产物（分片目录整个跟踪，gitignore 已排除中间文件）
TRACKED = ["data/index.json", "data/index.json.gz", "data/parts"]


def run_crawler(visits):
    """执行一轮爬虫，返回是否成功。"""
    env = dict(os.environ)
    env["ZHIHU_MAX_VISITS"] = str(visits)
    proc = subprocess.run(
        [sys.executable, CRAWLER],
        cwd=BASE_DIR,
        env=env,
    )
    return proc.returncode == 0


def read_stats():
    """读取爬虫写出的本轮统计：新增条目数等。"""
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def last_push_time():
    try:
        with open(PUSH_STATE, "r", encoding="utf-8") as f:
            return float(json.load(f).get("last_push", 0))
    except Exception:
        return 0.0


def mark_pushed():
    os.makedirs(os.path.dirname(PUSH_STATE), exist_ok=True)
    with open(PUSH_STATE, "w", encoding="utf-8") as f:
        json.dump({"last_push": time.time()}, f)


def has_changes():
    """检查待跟踪的 data 产物是否有未提交变更。"""
    paths = [p for p in TRACKED if os.path.exists(os.path.join(BASE_DIR, p))]
    if not paths:
        return False
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.strip()
        return bool(out)
    except Exception as exc:
        logger.warning("检查 git 状态失败: %s", exc)
        return False


def sync_remote():
    """推送前先拉取远程变更并变基，避免远程领先导致推送被拒。"""
    try:
        subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                       cwd=BASE_DIR, check=True, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
        return True
    except subprocess.CalledProcessError as exc:
        logger.warning("拉取远程失败: %s", exc)
        return False


def commit_and_push():
    """提交并推送本次爬取结果；成功后才更新推送时间戳。"""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        # -A 确保分片删除（例如从分片模式回退到单快照）也会被记录
        subprocess.run(["git", "add", "-A", "--", "data"], cwd=BASE_DIR, check=True)
        subprocess.run(
            ["git", "commit", "-m", "更新知乎搜索索引 %s" % stamp],
            cwd=BASE_DIR, check=True,
        )
        if not sync_remote():
            return False
        subprocess.run(["git", "push"], cwd=BASE_DIR, check=True)
        mark_pushed()
        logger.info("已提交并推送索引更新")
        return True
    except subprocess.CalledProcessError as exc:
        logger.warning("提交或推送失败: %s", exc)
        return False


def main():
    parser = argparse.ArgumentParser(description="本地常驻爬取循环")
    parser.add_argument("--rounds", type=int, default=0, help="运行轮数，0 表示无限运行")
    parser.add_argument("--visits", type=int, default=60, help="单轮访问上限")
    parser.add_argument("--interval", type=int, default=DEFAULT_ROUND_INTERVAL,
                        help="每轮之间的休息秒数")
    parser.add_argument("--min-new", type=int, default=5,
                        help="新增条目达到该值才提交推送，避免高频小提交撑大仓库")
    parser.add_argument("--push-interval", type=int, default=1800,
                        help="两次推送之间的最小间隔秒数；未到间隔则继续本地累积")
    args = parser.parse_args()

    round_no = 0
    while args.rounds == 0 or round_no < args.rounds:
        round_no += 1
        logger.info("===== 开始第 %d 轮爬取 =====", round_no)
        try:
            ok = run_crawler(args.visits)
        except KeyboardInterrupt:
            logger.info("收到中断，结束常驻循环")
            break
        if not ok:
            logger.warning("本轮爬虫异常退出")

        stats = read_stats()
        new_items = stats.get("new_items", 0)
        total = stats.get("total", 0)
        logger.info("本轮新增 %d 条，累计 %d 条", new_items, total)

        now = time.time()
        last_push = last_push_time()
        interval_ok = (last_push == 0) or (now - last_push >= args.push_interval)
        if has_changes() and new_items >= args.min_new and interval_ok:
            commit_and_push()
        elif has_changes():
            logger.info(
                "本轮新增 %d 条 < %d 或未到推送间隔，继续本地累积（已提交/已推送状态保持）",
                new_items, args.min_new,
            )
        else:
            logger.info("本轮无新增数据，跳过提交")

        if args.rounds != 0 and round_no >= args.rounds:
            break

        logger.info("休息 %d 秒后进入下一轮", args.interval)
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("收到中断，退出循环")
            break

    logger.info("运行结束，共完成 %d 轮", round_no)


if __name__ == "__main__":
    main()
