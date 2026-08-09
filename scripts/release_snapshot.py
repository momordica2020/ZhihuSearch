#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把数据快照归档到 GitHub Release（可选方案，配合历史压缩使用）

为什么：
  当仓库历史已经压缩过（或不想再重写历史）时，继续把每个快照都提交进 git
  仍会膨胀。GitHub Release 的附件不占用 git 历史空间，适合存放历史归档，
  仓库里只保留最新一份 data/。

用法：
  python scripts/release_snapshot.py                    # 以当天日期为 tag 上传
  python scripts/release_snapshot.py --tag data-20260809
  python scripts/release_snapshot.py --dry-run          # 只打包不上传

依赖：
  gh CLI 并已登录（gh auth status 检查），系统自带 tar。
"""

import os
import sys
import argparse
import subprocess
import tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def main():
    parser = argparse.ArgumentParser(description="归档数据快照到 GitHub Release")
    parser.add_argument("--tag", default="", help="Release tag（默认 data-YYYYMMDD）")
    parser.add_argument("--repo", default="", help="owner/repo（默认读 git remote）")
    parser.add_argument("--dry-run", action="store_true", help="只打包，不上传")
    args = parser.parse_args()

    if not os.path.isdir(DATA):
        print("未找到 data/ 目录")
        sys.exit(1)

    repo = args.repo
    if not repo:
        r = run(["git", "config", "--get", "remote.origin.url"], cwd=ROOT)
        url = r.stdout.strip()
        if not url:
            print("无法确定仓库，请用 --repo owner/repo")
            sys.exit(1)
        # 支持 https://github.com/a/b.git 与 git@github.com:a/b.git
        url = url.rstrip(".git").rstrip("/")
        if "github.com/" in url:
            repo = url.split("github.com/", 1)[1]
        else:
            repo = url.split(":", 1)[-1]

    tag = args.tag or ("data-" + datetime.now(timezone.utc).strftime("%Y%m%d"))
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "%s.tar.gz" % tag)
        r = run(["tar", "-czf", archive, "-C", ROOT, "data"])
        if r.returncode != 0:
            print("打包失败：%s" % r.stderr.strip())
            sys.exit(1)
        size = os.path.getsize(archive) / 1024.0
        print("已打包 data/ -> %s（%.1f KB）" % (os.path.basename(archive), size))
        if args.dry_run:
            print("预演模式，未上传。")
            return
        r = run(["gh", "release", "create", tag, archive,
                 "--repo", repo, "--title", "索引快照 %s" % tag,
                 "--notes", "知乎搜索索引历史归档（不占用 git 历史空间）"])
        if r.returncode != 0:
            print("上传失败：%s" % r.stderr.strip())
            print("提示：tag 已存在时可用 gh release upload %s %s --clobber" % (tag, archive))
            sys.exit(1)
        print("已上传：https://github.com/%s/releases/tag/%s" % (repo, tag))


if __name__ == "__main__":
    main()
