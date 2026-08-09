# 知乎内容搜索索引

自建检索库，用于搜索知乎话题、作者、问题与文章元数据。仅保存元数据，不存储全文。

## 目录结构

- `crawler/crawler.py` 知乎元数据爬虫：HTTP 快速通道 + 并发 + 自适应限速
- `crawler/test_crawler.py` 离线测试（fixture 驱动，不访问网络）
- `seeds.txt` 爬取起点链接，按话题或作者或问题填写
- `data/index.json` 前端加载清单（数据量小时为全量索引，量大时为分片清单）
- `data/index.json.gz` 单快照 gzip 索引（前端优先加载）
- `data/parts/` 超大规模时的分片 gzip 索引
- `data/seen.json` 已访问 URL 记录，用于断点续爬（不入库）
- `data/stats.json` 本轮统计：新增量等（不入库，供常驻脚本决策）
- `index.html`、`app.js`、`style.css`、`vendor/` 静态搜索页
- `run_local.py` 本地常驻爬取循环
- `scripts/compress_history.py` 仓库历史压缩（超大规模仓库膨胀时使用）
- `scripts/release_snapshot.py` 把数据快照归档到 GitHub Release（可选）
- `.github/workflows/crawl.yml` GitHub Actions 定时更新

## 爬虫加速与风控

默认走 **HTTP 快速通道**：用 curl_cffi 模拟 Chrome 的 TLS/HTTP2 指纹直接
请求页面 HTML，解析知乎 SSR 内嵌的 `js-initialData` JSON，不需要启动浏览器，
单页耗时从约 4 秒降到约 1 秒。配合并发 Worker（默认 3）与两层令牌桶限速
（全局 + 单主机），在风控阈值内最大化吞吐：

- 命中 403/429/验证码时指数退避，连续硬性拦截默认 3 次后优雅停止；
- 运行平稳时自动小幅提速，触发风控时自动大幅降速；
- 若纯 HTTP 客户端被知乎风控（TLS 指纹识别），会自动降级到
  Playwright 真实浏览器网络栈（`context.request`，不渲染页面，仍然较快），
  再不行才退回完整渲染模式，保证爬取不中断（`ZHIHU_AUTO_DEGRADE=1`）；
- 快速通道解析不到数据的页面可自动交给 Playwright 渲染兜底；
- 强制渲染模式（`ZHIHU_RENDER=1`）保留旧版行为。

登录态（`ZHIHU_COOKIE`）下，爬虫还会自动跟进话题子页
（`/topic/{id}/hot`、`/questions`、`/top-answers`、`/latest`）和个人/专栏
列表页（`/people/{token}/answers` 等），这些页面是大量新链接的来源；
`seeds.txt` 默认加入了热榜 `/hot`。链接会规范化去重（去除追踪参数、保留
`page` 分页参数），避免同一页面被重复排队。

### 动态探索（不再从固定种子出发）

爬虫维护一个持久化的**动态种子池**（`data/seed_pool.json`，不入 git）：

- 每轮按"产出分"加权随机挑选若干种子：产出越多越常被选，连续空转/失败的
  种子自动静默，冷却几天后自动复活；BFS 中发现的新话题/人物/专栏自动入池，
  池子超限时按分数修剪——实现种子的动态增删；
- 同时按 `ZHIHU_PROBE_RATIO` 的比例做**随机 ID 游走探测**：围绕已知有效的
  问题/话题 ID ± 随机偏移生成新 URL 尝试访问（命中率远高于纯随机），
  用少量访问持续探索知乎内容空间；
- `seeds.txt` 只在种子池首次创建时作为初始种子，之后每轮以池子为准；
  向 `seeds.txt` 新增的种子仍会被合并进池子。

相关环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ZHIHU_SEEDS_PER_ROUND` | 8 | 每轮从种子池挑选的种子数 |
| `ZHIHU_PROBE` | 1 | 是否启用随机 ID 探测 |
| `ZHIHU_PROBE_RATIO` | 0.3 | 探测访问占单轮访问上限的比例 |
| `ZHIHU_PROBE_WALK` | 50000 | 随机游走偏移半径 |
| `ZHIHU_POOL_MAX` | 300 | 种子池上限，超限自动修剪 |
| `ZHIHU_SEED_MAX_EMPTY` | 3 | 连续空转/失败多少次后静默该种子 |
| `ZHIHU_SEED_COOLDOWN_DAYS` | 3 | 静默种子多少天后自动复活 |
| `ZHIHU_RANDOM_SEED` | 空 | 固定随机种子（调试用，0/空=真随机） |

### 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ZHIHU_MAX_VISITS` | 300 | 单次访问上限 |
| `ZHIHU_MAX_DEPTH` | 3 | 最大跟进深度 |
| `ZHIHU_WORKERS` | 3 | HTTP 模式并发数（1–8） |
| `ZHIHU_DELAY_MIN` / `ZHIHU_DELAY_MAX` | 0.6 / 1.8 | 基础请求间隔（秒），自适应会调整 |
| `ZHIHU_REQ_PER_MIN` | 60 | 全局每分钟请求上限（风控安全线） |
| `ZHIHU_HOST_REQ_PER_MIN` | 24 | 单主机每分钟请求上限 |
| `ZHIHU_BACKOFF_MAX` | 120 | 风控退避上限（秒） |
| `ZHIHU_HARD_BLOCK_LIMIT` | 3 | 连续硬性拦截几次后停止 |
| `ZHIHU_SETTLE_MS` | 300 | 渲染模式页面加载后停留（毫秒） |
| `ZHIHU_SHARD_BYTES` | 2500000 | 单分片原始 JSON 字节阈值，超过自动分片；0 永不分片 |
| `ZHIHU_RENDER` | 0 | 1=强制 Playwright 渲染模式 |
| `ZHIHU_RENDER_FALLBACK` | 1 | 快速通道解析不到数据的页面用渲染兜底 |
| `ZHIHU_AUTO_DEGRADE` | 1 | 快速通道被风控时自动降级（curl_cffi → Playwright 网络栈 → 渲染） |
| `ZHIHU_COOKIE` | 空 | 已登录的知乎 Cookie，提升可见内容覆盖率 |
| `ZHIHU_FIXTURE_DIR` | 空 | 调试用本地 HTML 目录（离线测试） |

## 本地常驻运行（24 小时持续更新）

保持电脑开机，运行：

```bash
python run_local.py
```

可选参数：

- `--visits 60` 单轮访问上限，默认 60
- `--interval 600` 每轮之间休息秒数，默认 600
- `--rounds 10` 只跑 10 轮后退出，用于验证
- `--min-new 20` 新增条目达到该值才提交推送，默认 5
- `--push-interval 3600` 两次推送最小间隔秒数，默认 1800

每轮爬取完成后，仅当新增量达标且距上次推送超过间隔时，才会提交并推送，
避免高频小提交撑大仓库和 GitHub Actions 用量。

## 单次手动爬取

```bash
python crawler/crawler.py
```

依赖：`pip install curl_cffi requests`；降级/渲染/兜底模式另需
`pip install playwright && playwright install chromium`。

## Git 上传瘦身与增量控制

仓库只提交必要产物，全部中间/断点文件通过 `.gitignore` 排除：

- 只提交 `data/index.json`（清单或全量）、`data/index.json.gz` 与
  `data/parts/` 分片；`seen.json`、`stats.json`、`index.full.json`
  均为可再生文件，不入库；
- 数据超过 `ZHIHU_SHARD_BYTES` 后自动分片：每个分片独立压缩、独立提交，
  git 增量只携带变化的分片，也绕开了 GitHub 单文件 100MB 上限；
- 常驻脚本默认"新增 ≥5 条且间隔 ≥30 分钟"才提交推送，控制提交频率；
- 前端按清单并行加载分片后合并索引，浏览器无需一次性下载全量数据。

### 超大规模仓库压缩

持续提交多年后，git 历史体积会增长。`scripts/compress_history.py` 可把
`data/` 从全部历史中剔除，只保留最新一份快照：

```bash
python scripts/compress_history.py                  # 预演
python scripts/compress_history.py --force          # 本地压缩（自动建备份 tag）
python scripts/compress_history.py --force --push   # 压缩并强推远端
```

需要 `pip install git-filter-repo`。重写历史会改变提交哈希，强推前请确认
没有协作者基于旧历史工作。

如果不想重写历史，可用 `scripts/release_snapshot.py` 把历史快照归档到
GitHub Release（附件不占 git 历史空间），仓库只保留最新一份数据。

## 测试

```bash
python -m unittest crawler.test_crawler -v
```

测试完全离线：解析器单测 + 本地 HTML fixture 端到端爬取（含分片、断点续爬、
风控识别）。

## 部署

GitHub Actions 每 6 小时自动运行一次并在有变更时提交部署。也可在 Actions
页面手动触发。
