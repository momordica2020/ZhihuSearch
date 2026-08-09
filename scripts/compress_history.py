#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仓库历史压缩工具（超大规模场景使用）

问题：
  持续爬取并把 data/ 快照提交进 git 后，仓库体积会随历史无限增长，
  即使每个文件很小，GitHub 也会在仓库超过 1GB 后开始警告。

做法：
  1. 创建备份 tag（backup-compress-<时间戳>），失败可回退；
  2. 用 git-filter-repo 把 data/ 从全部历史中剔除；
  3. 重新提交当前最新 data/ 快照，历史里只保留一份最新数据；
  4. 可选 --push 强推远端（覆盖远端历史）。

用法：
  python scripts/compress_history.py                  # 预演，只打印将执行的操作
  python scripts/compress_history.py --force          # 本地执行压缩
  python scripts/compress_history.py --force --push   # 压缩并强推远端

依赖：
  git filter-repo（pip install git-filter-repo），未安装时仅提示并建议 git gc。

注意：
  重写历史是破坏性操作，会改变所有提交哈希，强制推送前务必确认没有协作者
  正在基于旧历史工作。备份 tag 保存在本地，push --tags 可上传。
"""

import os
import sys
import time
import shutil
import argparse
import tempfile
import subprocess
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def git(args, check=True):
    r = run(["git", *args])
    if check and r.returncode != 0:
        raise RuntimeError("git %s 失败: %s" % (" ".join(args), r.stderr.strip()))
    return r.stdout.strip()


def repo_size_human():
    r = run(["git", "count-objects", "-vH"])
    for line in r.stdout.splitlines():
        if line.startswith("size-pack:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def has_filter_repo():
    r = run(["git", "filter-repo", "--version"])
    return r.returncode == 0


def main():
    parser = argparse.ArgumentParser(description="压缩 data/ 历史，仅保留最新快照")
    parser.add_argument("--force", action="store_true",
                        help="实际执行历史重写（默认只预演）")
    parser.add_argument("--push", action="store_true",
                        help="重写后强推 origin 当前分支（危险，需确认）")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="允许工作区有未提交变更（变更会随新快照一并提交）")
    args = parser.parse_args()

    git(["rev-parse", "--git-dir"])  # 校验仓库
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"])
    remote_url = git(["config", "--get", "remote.origin.url"]) if run(
        ["git", "config", "--get", "remote.origin.url"]).returncode == 0 else ""

    status = git(["status", "--porcelain"])
    if status and not args.allow_dirty:
        print("工作区有未提交变更，先提交或加 --allow-dirty：\n%s" % status)
        sys.exit(1)

    if not has_filter_repo():
        print("未安装 git-filter-repo（pip install git-filter-repo）。")
        print("可先执行轻量整理：git gc --aggressive --prune=now && git pack-refs --all")
        sys.exit(1 if args.force else 0)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = "backup-compress-%s" % stamp
    print("当前分支: %s | 远端: %s | 仓库数据包体积: %s" % (
        branch, remote_url or "(无)", repo_size_human()))
    print("计划：备份 tag=%s -> 剔除 data/ 历史 -> 重新提交当前快照" % tag)
    if args.push:
        print("并将强推 origin/%s（会改写远端历史！）" % branch)
    if not args.force:
        print("\n这是预演。确认无误后加 --force 执行。")
        return

    print("\n[1/4] 创建备份 tag ...")
    git(["tag", tag])
    print("  tag %s 已创建（git push --tags 可上传备份）" % tag)

    print("[2/4] 备份 data/ 到临时目录 ...")
    tmp = tempfile.mkdtemp(prefix="zhihu-history-")
    data_src = os.path.join(ROOT, "data")
    data_dst = os.path.join(tmp, "data")
    if os.path.isdir(data_src):
        shutil.copytree(data_src, data_dst)

    print("[3/4] 剔除 data/ 历史 ...")
    # 先确保当前变更已提交，避免 filter-repo 的 reset --hard 丢工作
    if status:
        git(["add", "-A"])
        git(["commit", "-m", "历史压缩前的工作区变更 %s" % stamp])
    r = run(["git", "filter-repo", "--path", "data", "--invert-paths",
             "--force"])
    if r.returncode != 0:
        print("filter-repo 失败：%s" % r.stderr.strip())
        print("可回退：git reset --hard %s" % tag)
        sys.exit(1)
    if remote_url and not run(["git", "config", "--get", "remote.origin.url"]).stdout.strip():
        # filter-repo 默认移除 origin，按原地址恢复
        git(["remote", "add", "origin", remote_url])
        print("  已恢复远端 origin: %s" % remote_url)

    print("[4/4] 恢复最新 data/ 快照并提交 ...")
    if os.path.isdir(data_dst):
        shutil.rmtree(data_src, ignore_errors=True)
        shutil.move(data_dst, data_src)
    git(["add", "-A"])
    git(["commit", "-m", "重建索引快照（历史压缩） %s" % stamp])

    print("完成。仓库数据包体积: %s（压缩前见上方）" % repo_size_human())
    print("如需回退：git reset --hard %s（然后 git push --force origin %s）" % (tag, branch))
    if args.push:
        print("强推 origin/%s ..." % branch)
        git(["push", "--force", "origin", branch])
        print("已强推完成。")
    else:
        print("未推送。确认后执行：git push --force origin %s" % branch)


if __name__ == "__main__":
    main()
