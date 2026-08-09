# 知乎内容搜索索引

自建检索库，用于搜索知乎话题、作者、问题与文章元数据。仅保存元数据，不存储全文。

## 目录结构

- `crawler/crawler.py` 知乎元数据爬虫，种子加 BFS 逐页发现模式
- `seeds.txt` 爬取起点链接，按话题或作者或问题填写
- `data/index.json` 前端搜索使用的索引产物
- `data/seen.json` 已访问 URL 记录，用于断点续爬
- `index.html`、`app.js`、`style.css`、`vendor/` 静态搜索页
- `run_local.py` 本地常驻爬取循环脚本
- `.github/workflows/crawl.yml` GitHub Actions 定时更新

## 本地常驻运行（24 小时持续更新）

保持电脑开机，运行：

```bash
python run_local.py
```

可选参数：

- `--visits 60` 单轮访问上限，默认 60
- `--interval 600` 每轮之间休息秒数，默认 600
- `--rounds 10` 只跑 10 轮后退出，用于验证

每轮爬取完成后会自动提交并推送到 GitHub，触发 Pages 重新部署。

## 单次手动爬取

```bash
python crawler/crawler.py
```

可用环境变量控制行为：

- `ZHIHU_MAX_VISITS` 单次访问上限，默认 300
- `ZHIHU_MAX_DEPTH` 最大跟进深度，默认 3
- `ZHIHU_DELAY_MIN`、`ZHIHU_DELAY_MAX` 单次访问间隔秒数，默认 1.5 到 3.0
- `ZHIHU_SETTLE_MS` 页面加载后停留毫秒数，默认 1500
- `ZHIHU_COOKIE` 已登录的知乎 Cookie，可选，提升可见内容覆盖率

## 部署

GitHub Actions 每 6 小时自动运行一次并在有变更时提交部署。也可在 Actions 页面手动触发。