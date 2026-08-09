#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知乎搜索索引 - 本地常驻爬取循环

持续运行：每轮执行一次爬虫，并在产生新数据后自动提交推送，
实现"24h 不断更新"知乎内容索引的效果。

用法：
    python run_local.py                 # 以默认配置常驻运行
    python run_local.py --rounds 10     # 只跑 10 轮后退出（用于验证）
    python run_local.py --visits 60     # 单轮访问上限覆盖

说明：
    需要保持电脑开机运行；Ctrl+C 可正常结束当前轮并保存进度。
    首次请先设置 ZHIHU_COOKIE 环境变量（可选，提升可见内容覆盖率）。
"""

import os
import sys
import time
import argparse
import logging
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CRAWLER = os.path.join(BASE_DIR, "crawler", "crawler.py")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("local-runner")

# 每轮之间的休息时间（秒），给风控留缓冲；可通过 --interval 覆盖
DEFAULT_ROUND_INTERVAL = 600


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


def has_changes():
    """检查 data/ 目录是否有未提交变更。"""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "data/"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return bool(out)
    except Exception as exc:
        logger.warning("检查 git 状态失败: %s", exc)
        return False


def sync_remote():
    """推送前先拉取远程变更并变基，避免远程领先导致推送被拒。"""
    try:
        subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                       cwd=BASE_DIR, check=True)
        return True
    except subprocess.CalledProcessError as exc:
        logger.warning("拉取远程失败: %s", exc)
        return False


def commit_and_push():
    """提交并推送本次爬取结果。"""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        subprocess.run(["git", "add", "data/"], cwd=BASE_DIR, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"更新知乎搜索索引 {stamp}"],
            cwd=BASE_DIR,
            check=True,
        )
        if not sync_remote():
            return False
        subprocess.run(["git", "push"], cwd=BASE_DIR, check=True)
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
    args = parser.parse_args()

    round_no = 0
    while args.rounds == 0 or round_no < args.rounds:
        round_no += 1
        logger.info("===== 开始第 %d 轮爬取 =====", round_no)
        ok = run_crawler(args.visits)
        if not ok:
            logger.warning("本轮爬虫异常退出")

        if has_changes():
            commit_and_push()
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