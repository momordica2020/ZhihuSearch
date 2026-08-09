#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地离线测试：解析器单测 + fixture 端到端爬取（不访问真实网络）。"""

import os
import sys
import json
import gzip
import shutil
import tempfile
import subprocess
import unittest

CR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(CR)

QUESTION_HTML = """<!DOCTYPE html><html><head><title>问题标题A</title></head><body>
<script id="js-initialData" type="text/json">{"initialState":{"env":{"ctx":{"path":"/question/123456"}},"entities":{"questions":{"123456":{"type":"question","id":123456,"title":"问题标题A","url":"https://www.zhihu.com/question/123456","comment_count":5}},"answers":{"111":{"type":"answer","url":"https://www.zhihu.com/question/123456/answer/111","question":{"id":123456,"title":"问题标题A"},"author":{"name":"答主甲"},"excerpt":"这是回答摘要一","voteup_count":42,"comment_count":3}},"articles":{}}}}</script>
<a href="https://www.zhihu.com/question/654321">下一个问题</a>
<a href="https://www.zhihu.com/topic/999999">话题链接</a>
<a href="https://external.example.com/foo">站外链接</a>
</body></html>"""

QUESTION_HTML_B = """<!DOCTYPE html><html><head><title>问题标题B</title></head><body>
<script id="js-initialData" type="text/json">{"initialState":{"env":{"ctx":{"path":"/question/654321"}},"entities":{"questions":{"654321":{"type":"question","id":654321,"title":"问题标题B","url":"https://www.zhihu.com/question/654321"}},"answers":{"222":{"type":"answer","url":"https://www.zhihu.com/question/654321/answer/222","question":{"id":654321,"title":"问题标题B"},"author":{"name":"答主乙"},"excerpt":"这是回答摘要二","voteup_count":7,"comment_count":0}},"articles":{}}}}</script>
</body></html>"""

TOPIC_HTML = """<!DOCTYPE html><html><head><title>话题页</title></head><body>
<script id="js-initialData" type="text/json">{"initialState":{"env":{"ctx":{"path":"/topic/999999"}},"entities":{"questions":{},"answers":{},"articles":{},"topics":{},"feeds":{"f1":{"type":"feed","target":{"type":"question","id":777001,"title":"话题内问题一","url":"https://www.zhihu.com/question/777001","comment_count":2}},"f2":{"type":"feed","target":{"type":"question","id":777002,"title":"话题内问题二","url":"https://www.zhihu.com/question/777002"}}}}}}</script>
<a href="https://www.zhihu.com/question/654321">链接</a>
</body></html>"""

HOT_HTML = """<!DOCTYPE html><html><head><title>热榜</title></head><body>
<script id="js-initialData" type="text/json">{"initialState":{"env":{"ctx":{"path":"/hot"}},"entities":{"questions":{},"answers":{},"articles":{},"topics":{},"feeds":{"h1":{"type":"feed","target":{"type":"question","id":888001,"title":"热榜问题一","url":"https://www.zhihu.com/question/888001"}},"h2":{"type":"feed","target":{"type":"question","id":888002,"title":"热榜问题二","url":"https://www.zhihu.com/question/888002"}}}}}}</script>
<a href="https://www.zhihu.com/topic/19551275/questions?page=2">话题问题列表</a>
</body></html>"""

TOPIC_QUESTIONS_HTML = """<!DOCTYPE html><html><head><title>话题问题列表</title></head><body>
<script id="js-initialData" type="text/json">{"initialState":{"env":{"ctx":{"path":"/topic/19551275/questions"}},"entities":{"questions":{},"answers":{},"articles":{},"topics":{},"feeds":{"q1":{"type":"feed","target":{"type":"question","id":777101,"title":"列表内问题","url":"https://www.zhihu.com/question/777101"}}}}}}</script>
<a href="https://www.zhihu.com/topic/19551275/hot">话题热榜子页</a>
</body></html>"""

TOPIC_HOT_HTML = """<!DOCTYPE html><html><head><title>话题热榜</title></head><body>
<script id="js-initialData" type="text/json">{"initialState":{"env":{"ctx":{"path":"/topic/19551275/hot"}},"entities":{"questions":{},"answers":{},"articles":{},"topics":{},"feeds":{"h3":{"type":"feed","target":{"type":"question","id":777102,"title":"热榜子页问题","url":"https://www.zhihu.com/question/777102"}}}}}}</script>
</body></html>"""


def setup_fixtures(tmp):
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "question_123456.html"), "w", encoding="utf-8") as f:
        f.write(QUESTION_HTML)
    with open(os.path.join(tmp, "question_654321.html"), "w", encoding="utf-8") as f:
        f.write(QUESTION_HTML_B)
    with open(os.path.join(tmp, "topic_999999.html"), "w", encoding="utf-8") as f:
        f.write(TOPIC_HTML)
    with open(os.path.join(tmp, "hot.html"), "w", encoding="utf-8") as f:
        f.write(HOT_HTML)
    with open(os.path.join(tmp, "topic_19551275_questions.html"), "w", encoding="utf-8") as f:
        f.write(TOPIC_QUESTIONS_HTML)
    with open(os.path.join(tmp, "topic_19551275_hot.html"), "w", encoding="utf-8") as f:
        f.write(TOPIC_HOT_HTML)


class ParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, CR)
        os.environ["ZHIHU_FIXTURE_DIR"] = "x"  # 仅让模块可导入，不真正请求
        from crawler import crawler as c
        cls.c = c

    def test_classify(self):
        c = self.c
        self.assertEqual(c.classify_url("https://www.zhihu.com/question/1"), "content")
        self.assertEqual(c.classify_url("https://www.zhihu.com/p/123"), "content")
        self.assertEqual(c.classify_url("https://www.zhihu.com/topic/42"), "content")
        self.assertEqual(c.classify_url("https://www.zhihu.com/topic/42/hot"), "skip")
        self.assertEqual(c.classify_url("https://www.zhihu.com/people/foo"), "queue")
        self.assertEqual(c.classify_url("https://www.zhihu.com/column/c1"), "queue")
        self.assertEqual(c.classify_url("https://www.zhihu.com/search?q=x"), "skip")
        self.assertEqual(c.classify_url("https://example.com/x"), "skip")

    def test_classify_with_cookie(self):
        c = self.c
        old = c.COOKIE_STR
        try:
            c.COOKIE_STR = "x=1"
            self.assertEqual(c.classify_url("https://www.zhihu.com/topic/42/hot"), "queue")
            self.assertEqual(c.classify_url("https://www.zhihu.com/topic/42/questions"), "queue")
            self.assertEqual(c.classify_url("https://www.zhihu.com/topic/42/top-answers"), "queue")
            self.assertEqual(c.classify_url("https://www.zhihu.com/topic/42/latest"), "queue")
            self.assertEqual(c.classify_url("https://www.zhihu.com/people/foo/answers"), "queue")
            self.assertEqual(c.classify_url("https://www.zhihu.com/column/c1/posts"), "queue")
            self.assertEqual(c.classify_url("https://www.zhihu.com/topic/42/foo"), "skip")
        finally:
            c.COOKIE_STR = old

    def test_canonical_url(self):
        c = self.c
        self.assertEqual(
            c.canonical_url("https://www.zhihu.com/question/123?noti_id=9#foo"),
            "https://www.zhihu.com/question/123",
        )
        self.assertEqual(
            c.canonical_url("https://www.zhihu.com/topic/42/questions?page=2&source=x"),
            "https://www.zhihu.com/topic/42/questions?page=2",
        )
        self.assertEqual(
            c.canonical_url("https://www.zhihu.com/topic/42/questions"),
            "https://www.zhihu.com/topic/42/questions",
        )
        self.assertEqual(
            c.canonical_url("https://www.zhihu.com/people/x/answers?page=3&tab=1"),
            "https://www.zhihu.com/people/x/answers?page=3",
        )
        self.assertEqual(
            c.canonical_url("https://zhuanlan.zhihu.com/p/123?source=1"),
            "https://zhuanlan.zhihu.com/p/123",
        )

    def test_parse_question(self):
        data = self.c.extract_initial_data(QUESTION_HTML)
        self.assertIsNotNone(data)
        items, links, path = self.c.parse_entities(data)
        self.assertEqual(path, "/question/123456")
        kinds = [it["kind"] for it in items]
        self.assertEqual(kinds, ["问题", "回答"])
        ans = items[1]
        self.assertEqual(ans["author"], "答主甲")
        self.assertEqual(ans["votes"], 42)
        self.assertEqual(ans["comments"], 3)
        # 回答实体中的 question 应贡献问题链接
        self.assertIn("https://www.zhihu.com/question/123456", links)

    def test_parse_topic_feeds(self):
        data = self.c.extract_initial_data(TOPIC_HTML)
        items, _links, path = self.c.parse_entities(data)
        self.assertEqual(path, "/topic/999999")
        self.assertEqual(len(items), 2)
        self.assertTrue(all(it["kind"] == "问题" for it in items))
        titles = {it["title"] for it in items}
        self.assertEqual(titles, {"话题内问题一", "话题内问题二"})

    def test_html_links(self):
        links = self.c.collect_html_links(QUESTION_HTML)
        self.assertIn("https://www.zhihu.com/question/654321", links)
        self.assertIn("https://www.zhihu.com/topic/999999", links)
        self.assertNotIn("https://external.example.com/foo", links)

    def test_blocked_detection(self):
        self.assertTrue(self.c.http_blocked(403, ""))
        self.assertTrue(self.c.http_blocked(200, '"code":40362'))
        # 正常 SSR JSON 里的 captcha 字段不应误报
        self.assertFalse(self.c.http_blocked(
            200, '{"captcha":{"captchaNeeded":true}}'))
        self.assertTrue(self.c.http_blocked(200, "<title>安全验证</title>"))


class E2ETest(unittest.TestCase):
    def _run(self, env, data_dir):
        env = dict(os.environ, **env)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return subprocess.run(
            [sys.executable, os.path.join(CR, "crawler.py")],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )

    def _base_env(self, tmp, data_dir, shard_bytes):
        return {
            "ZHIHU_SEEDS": os.path.join(tmp, "seeds.txt"),
            "ZHIHU_FIXTURE_DIR": os.path.join(tmp, "pages"),
            "ZHIHU_DATA_DIR": data_dir,
            "ZHIHU_MAX_VISITS": "10",
            "ZHIHU_MAX_DEPTH": "3",
            "ZHIHU_WORKERS": "3",
            "ZHIHU_DELAY_MIN": "0",
            "ZHIHU_DELAY_MAX": "0",
            "ZHIHU_REQ_PER_MIN": "1000",
            "ZHIHU_HOST_REQ_PER_MIN": "1000",
            "ZHIHU_RENDER": "0",
            "ZHIHU_RENDER_FALLBACK": "0",
            "ZHIHU_SHARD_BYTES": str(shard_bytes),
            # 探索功能单独用单测覆盖，E2E 保持确定性
            "ZHIHU_PROBE": "0",
            "ZHIHU_SEEDS_PER_ROUND": "10",
        }

    def test_end_to_end_sharded(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup_fixtures(os.path.join(tmp, "pages"))
            with open(os.path.join(tmp, "seeds.txt"), "w", encoding="utf-8") as f:
                f.write("https://www.zhihu.com/question/123456\n")
                f.write("https://www.zhihu.com/topic/999999\n")
                f.write("https://www.zhihu.com/hot\n")

            data_dir = os.path.join(tmp, "data")
            env = self._base_env(tmp, data_dir, shard_bytes=500)
            env["ZHIHU_COOKIE"] = "dummy=1"  # 模拟登录态，允许话题列表子页
            r1 = self._run(env, data_dir)
            self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)

            with open(os.path.join(data_dir, "index.json"), encoding="utf-8") as f:
                manifest = json.load(f)
            self.assertEqual(manifest["count"], 10)
            self.assertIn("shards", manifest)
            parts_dir = os.path.join(data_dir, "parts")
            gz_names = sorted(n for n in os.listdir(parts_dir) if n.endswith(".json.gz"))
            self.assertGreaterEqual(len(gz_names), 2)
            total = 0
            for name in gz_names:
                with gzip.open(os.path.join(parts_dir, name), "rt", encoding="utf-8") as f:
                    total += len(json.load(f))
            self.assertEqual(total, 10)
            self.assertFalse(os.path.exists(os.path.join(data_dir, "index.json.gz")))
            with open(os.path.join(data_dir, "stats.json"), encoding="utf-8") as f:
                stats = json.load(f)
            self.assertEqual(stats["new_items"], 10)
            self.assertEqual(stats["visited"], 6)  # 3 种子 + 654321 + 话题列表页 + 热榜子页

            # 断点续爬：再次运行不应重复收录
            r2 = self._run(env, data_dir)
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
            with open(os.path.join(data_dir, "stats.json"), encoding="utf-8") as f:
                stats2 = json.load(f)
            self.assertEqual(stats2["total"], 10)
            self.assertEqual(stats2["new_items"], 0)

    def test_end_to_end_single(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup_fixtures(os.path.join(tmp, "pages"))
            with open(os.path.join(tmp, "seeds.txt"), "w", encoding="utf-8") as f:
                f.write("https://www.zhihu.com/question/123456\n")

            data_dir = os.path.join(tmp, "data")
            env = self._base_env(tmp, data_dir, shard_bytes=0)
            r = self._run(env, data_dir)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            with open(os.path.join(data_dir, "index.json"), encoding="utf-8") as f:
                idx = json.load(f)
            self.assertEqual(idx["count"], 6)  # 种子页 + 发现的 654321/话题
            self.assertIn("items", idx)
            self.assertTrue(os.path.exists(os.path.join(data_dir, "index.json.gz")))
            self.assertFalse(os.path.exists(os.path.join(data_dir, "parts")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
