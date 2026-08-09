#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动态种子池 + 随机 ID 游走探索。

目标：不再每轮从固定种子出发，而是让爬虫像人一样"漫游"知乎：
  1. 种子池持久化在 data/seed_pool.json（不入 git），每轮按产出分加权
     随机挑选若干种子，产出高的多选、连续空转/失败的自动静默；
  2. BFS 过程中发现的新话题/人物/专栏链接自动入池，实现种子动态增删；
  3. 随机探测：围绕已知有效的问题/话题 ID 做随机游走（ID ± 偏移），
     命中率远高于纯随机，用少量访问换取对知乎内容空间的持续探索。
"""

import os
import re
import json
import time
import random
from urllib.parse import urlparse

QUESTION_RE = re.compile(r"/question/(\d+)")
TOPIC_RE = re.compile(r"/topic/(\d+)")


def kind_of(url):
    path = urlparse(url or "").path.rstrip("/")
    if QUESTION_RE.search(path):
        return "question"
    if TOPIC_RE.search(path):
        return "topic"
    if path.startswith("/people/"):
        return "people"
    if path.startswith("/column/"):
        return "column"
    if path in ("/hot", "/explore"):
        return "hot"
    return "other"


def extract_id(url, kind):
    if kind == "question":
        m = QUESTION_RE.search(url or "")
    elif kind == "topic":
        m = TOPIC_RE.search(url or "")
    else:
        return None
    return int(m.group(1)) if m else None


class SeedPool:
    """持久化的动态种子池：加权随机选择、产出反馈、自动休眠与修剪。"""

    def __init__(self, path, bootstrap, max_size=300, max_empty=3,
                 cooldown_days=3, rng=None):
        self.path = path
        self.max_size = max_size
        self.max_empty = max_empty
        self.cooldown_days = cooldown_days
        self.rng = rng or random.Random()
        self.seeds = {}
        self._load()
        # 合并种子文件：新写入的种子总是生效（已有条目只补建不覆盖）
        for url in bootstrap:
            self._add(url, kind_of(url), score=5.0)
        self._save()

    # ------------------------------------------------------------ 基础读写
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for e in raw.get("seeds", []):
                if isinstance(e, dict) and e.get("url"):
                    self.seeds[e["url"]] = e
        except Exception:
            self.seeds = {}
        self._reenable_cooldown()

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"version": 1, "seeds": list(self.seeds.values())},
                f, ensure_ascii=False, separators=(",", ":"),
            )
        os.replace(tmp, self.path)

    def save(self):
        self._save()

    def _add(self, url, kind, score=5.0):
        url = (url or "").strip()
        if not url or url in self.seeds:
            return
        self.seeds[url] = {
            "url": url,
            "kind": kind or kind_of(url),
            "score": float(score),
            "hits": 0,
            "misses": 0,
            "empties": 0,
            "last_used": 0.0,
            "created": time.time(),
            "enabled": True,
        }

    def _reenable_cooldown(self):
        """被静默的种子冷却一段时间后自动复活，保持探索的动态性。"""
        now = time.time()
        for e in self.seeds.values():
            if not e.get("enabled"):
                since = e.get("disabled_at", e.get("created", now))
                if now - since >= self.cooldown_days * 86400:
                    e["enabled"] = True
                    e["misses"] = 0
                    e["empties"] = 0
                    e["score"] = max(e.get("score", 1.0), 2.0)

    # ------------------------------------------------------------ 选择与反馈
    def select(self, k):
        """按产出分加权不放回抽样，返回选中的 URL 列表。"""
        cand = [e for e in self.seeds.values() if e.get("enabled")]
        if not cand:
            return []
        pool = []
        for e in cand:
            w = max(float(e.get("score", 1.0)), 0.1)
            if e.get("kind") == "hot":
                w *= 3.0  # 热榜是最高价值发现入口，提高再选概率
            w *= self.rng.uniform(0.5, 1.5)  # 随机扰动，避免总是同一批
            pool.append((e, w))
        chosen = []
        for _ in range(min(k, len(pool))):
            total = sum(w for _, w in pool)
            if total <= 0:
                break
            r = self.rng.uniform(0, total)
            acc = 0.0
            for i, (e, w) in enumerate(pool):
                acc += w
                if r <= acc:
                    chosen.append(e["url"])
                    e["last_used"] = time.time()
                    pool.pop(i)
                    break
        return chosen

    def observe(self, url, new_items, ok):
        """记录一次访问结果：新增条目提升分数，空转/失败降低并最终静默。"""
        e = self.seeds.get(url)
        if not e:
            return
        if not ok:
            e["misses"] = e.get("misses", 0) + 1
            e["score"] = float(e.get("score", 1.0)) * 0.5
            if e["misses"] >= self.max_empty:
                e["enabled"] = False
                e["disabled_at"] = time.time()
            return
        e["misses"] = 0
        e["hits"] = e.get("hits", 0) + 1
        if new_items > 0:
            e["empties"] = 0
            e["score"] = min(100.0, float(e.get("score", 1.0)) * 0.8
                             + 8.0 * new_items + 2.0)
        else:
            e["empties"] = e.get("empties", 0) + 1
            e["score"] = float(e.get("score", 1.0)) * 0.7
            if e["empties"] >= self.max_empty:
                e["enabled"] = False
                e["disabled_at"] = time.time()

    def discover(self, urls):
        """把新发现的入口（话题/人物/专栏）加入种子池。"""
        for u in urls:
            k = kind_of(u)
            if k in ("topic", "people", "column", "hot"):
                self._add(u, k, score=3.0)

    def prune(self):
        """超出上限时：先删已静默的，再删分数最低且很久未用的。"""
        if len(self.seeds) <= self.max_size:
            return
        for u in [u for u, e in self.seeds.items() if not e.get("enabled")]:
            if len(self.seeds) <= self.max_size:
                break
            del self.seeds[u]
        for u, _ in sorted(
            self.seeds.items(),
            key=lambda kv: (float(kv[1].get("score", 0)), kv[1].get("last_used", 0)),
        ):
            if len(self.seeds) <= self.max_size:
                break
            del self.seeds[u]

    # ------------------------------------------------------------ 随机探测
    def anchors(self, kind):
        ids = []
        for e in self.seeds.values():
            if e.get("kind") == kind:
                i = extract_id(e.get("url", ""), kind)
                if i:
                    ids.append(i)
        return ids

    def add_anchors(self, urls):
        """把已知有效 URL 作为低分锚点入池，供随机游走参考（不做主种子）。"""
        for u in urls:
            k = kind_of(u)
            if k in ("question", "topic") and extract_id(u, k):
                self._add(u, k, score=1.0)

    def make_probes(self, count, walk=50000):
        """围绕已知有效 ID 随机游走生成探测 URL；锚点不足时退化为均匀随机。"""
        qids = self.anchors("question")
        tids = self.anchors("topic")
        probes = []
        for _ in range(count):
            r = self.rng.random()
            if qids and r < 0.7:
                base = self.rng.choice(qids)
                nid = max(1_000_000, base + self.rng.randint(-walk, walk))
                probes.append("https://www.zhihu.com/question/%d" % nid)
            elif tids and r < 0.95:
                base = self.rng.choice(tids)
                nid = max(10000, base + self.rng.randint(-walk, walk))
                probes.append("https://www.zhihu.com/topic/%d" % nid)
            else:
                probes.append(
                    "https://www.zhihu.com/question/%d"
                    % self.rng.randint(1_000_000, 900_000_000)
                )
        return probes
