#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动态种子池与随机探测逻辑的离线单测。"""

import os
import sys
import json
import tempfile
import unittest

CR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CR)

from explorer import SeedPool, kind_of, extract_id


class KindTest(unittest.TestCase):
    def test_kind_of(self):
        self.assertEqual(kind_of("https://www.zhihu.com/question/123"), "question")
        self.assertEqual(kind_of("https://www.zhihu.com/topic/42"), "topic")
        self.assertEqual(kind_of("https://www.zhihu.com/people/foo"), "people")
        self.assertEqual(kind_of("https://www.zhihu.com/column/c1"), "column")
        self.assertEqual(kind_of("https://www.zhihu.com/hot"), "hot")
        self.assertEqual(kind_of("https://example.com/x"), "other")
        self.assertEqual(
            extract_id("https://www.zhihu.com/question/123", "question"), 123)
        self.assertEqual(extract_id("https://www.zhihu.com/topic/42", "topic"), 42)
        self.assertIsNone(extract_id("https://www.zhihu.com/people/x", "people"))


class PoolTest(unittest.TestCase):
    def _pool(self, seeds, **kw):
        tmp = tempfile.TemporaryDirectory()
        self._tmps = getattr(self, "_tmps", [])
        self._tmps.append(tmp)
        path = os.path.join(tmp.name, "seed_pool.json")
        return SeedPool(path, seeds, rng=__import__("random").Random(7), **kw)

    def tearDown(self):
        for tmp in getattr(self, "_tmps", []):
            tmp.cleanup()
        self._tmps = []

    def test_bootstrap_and_select(self):
        seeds = [
            "https://www.zhihu.com/topic/1",
            "https://www.zhihu.com/topic/2",
            "https://www.zhihu.com/hot",
        ]
        pool = self._pool(seeds)
        self.assertEqual(len(pool.seeds), 3)
        chosen = pool.select(10)
        self.assertEqual(len(chosen), 3)  # 超出池大小就全选
        self.assertEqual(set(chosen), set(seeds))

    def test_observe_feedback_and_disable(self):
        pool = self._pool(["https://www.zhihu.com/topic/1"])
        url = "https://www.zhihu.com/topic/1"
        pool.observe(url, 5, ok=True)
        self.assertGreater(pool.seeds[url]["score"], 5.0)
        pool.observe(url, 0, ok=True)
        pool.observe(url, 0, ok=True)
        pool.observe(url, 0, ok=True)
        self.assertFalse(pool.seeds[url]["enabled"])
        self.assertEqual(pool.select(10), [])
        # 失败同样累计静默
        pool2 = self._pool(["https://www.zhihu.com/topic/2"])
        u2 = "https://www.zhihu.com/topic/2"
        pool2.observe(u2, 0, ok=False)
        pool2.observe(u2, 0, ok=False)
        pool2.observe(u2, 0, ok=False)
        self.assertFalse(pool2.seeds[u2]["enabled"])

    def test_discover_only_entry_kinds(self):
        pool = self._pool(["https://www.zhihu.com/hot"])
        pool.discover([
            "https://www.zhihu.com/topic/100",
            "https://www.zhihu.com/people/foo",
            "https://www.zhihu.com/column/c1",
            "https://www.zhihu.com/question/999",  # 不入池
        ])
        urls = set(pool.seeds)
        self.assertIn("https://www.zhihu.com/topic/100", urls)
        self.assertIn("https://www.zhihu.com/people/foo", urls)
        self.assertIn("https://www.zhihu.com/column/c1", urls)
        self.assertNotIn("https://www.zhihu.com/question/999", urls)

    def test_probes_near_anchors(self):
        import re
        pool = self._pool(["https://www.zhihu.com/topic/19551275"])
        pool.add_anchors([
            "https://www.zhihu.com/question/629590442",
            "https://www.zhihu.com/question/48510028",
        ])
        probes = pool.make_probes(200, walk=1000)
        self.assertEqual(len(probes), 200)
        pattern = re.compile(r"^https://www\.zhihu\.com/(question|topic)/\d+$")
        self.assertTrue(all(pattern.match(p) for p in probes), probes[:5])
        # 随机游走应大量落在已知问题锚点附近（而不是纯随机海捞）
        near_question = sum(
            1 for p in probes
            if "/question/" in p
            and abs(int(p.rsplit("/", 1)[1]) - 629590442) <= 1000
        )
        self.assertGreater(near_question, 20)

    def test_persistence_and_prune(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "seed_pool.json")
        p1 = SeedPool(path, ["https://www.zhihu.com/topic/1"], max_size=5,
                      rng=__import__("random").Random(1))
        p1.discover(["https://www.zhihu.com/topic/%d" % i for i in range(2, 20)])
        p1.prune()
        self.assertLessEqual(len(p1.seeds), 5)
        p1.save()

        p2 = SeedPool(path, [], max_size=5, rng=__import__("random").Random(2))
        self.assertGreaterEqual(len(p2.seeds), 1)
        self.assertLessEqual(len(p2.seeds), 5)
        tmp.cleanup()

    def test_cooldown_reenable(self):
        import time as _time
        pool = self._pool(["https://www.zhihu.com/topic/1"], cooldown_days=0)
        url = "https://www.zhihu.com/topic/1"
        pool.observe(url, 0, ok=True)
        pool.observe(url, 0, ok=True)
        pool.observe(url, 0, ok=True)
        self.assertFalse(pool.seeds[url]["enabled"])
        pool.seeds[url]["disabled_at"] = _time.time() - 1  # 模拟冷却到期
        pool._reenable_cooldown()
        self.assertTrue(pool.seeds[url]["enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
