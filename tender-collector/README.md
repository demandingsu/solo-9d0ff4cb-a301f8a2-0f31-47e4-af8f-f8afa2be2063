# 市政工程投标截止时间采集与回执核对服务

面向招投标专员的公告采集 API：提交市政工程公告站的**列表页地址**，服务自动遍历
所有分页与详情页，将**更正公告按其引用的原公告编号归并**到同一项目，并**按发布
时间**（而非页面排列顺序）确定当前生效的投标截止时间，标记是否已过期。

同时提供**回执核对 API**：输入已采集的原公告编号、标段号、手头文件 SHA-256 与
回执目录 URL，服务沿同站分页读取标书上传/撤回回执，按现行（更正后）投标截止时间
过滤后，依据撤回链定位平台当前受理的末份未撤回上传，回答手头文件是否仍是平台
受理版本；摘要冲突、撤回悬空或页面抓取失败时明确标记为无法判定。

## 核心规则

1. **分页遍历**：从提交的列表页出发，沿 `nav.pagination` 链接抓取全部列表页
   （去重、防环、限量），收集所有详情页链接后逐页抓取。
2. **更正归并**：更正公告通过"原公告编号"归并到对应原公告所在项目；引用悬空
   （原公告不存在/不是原公告）视为数据错误，本次采集整体失败。
3. **生效截止时间**：同一项目内按**发布时间**排序，取发布时间最新且声明了
   截止时间的公告；发布时间相同时按公告编号确定次序，结果与页面顺序完全无关。
   若最新更正公告未给出截止时间（例如只改联系人），沿用上一份带截止时间的公告。
4. **过期判定**：`is_expired = 截止时间 <= 当前时间`（截止时间按服务器本地时区
   的 naive 时间比较，与公告页 `YYYY-MM-DD HH:MM` 格式一致）。
5. **幂等采集**：公告以编号为唯一键 UPSERT，重复采集不产生重复记录。
6. **失败保留**：任一列表页/详情页读取失败（网络错误、非 2xx、字段缺失）或
   归并校验失败时，本次采集不写入任何公告数据，**完整保留上次成功采集结果**；
   失败原因记入 `collection_runs` 表并通过 HTTP 422 返回。

## 回执核对规则（`POST /api/receipt-check`）

1. **截止时间过滤**：只处理该原公告 + 该标段、受理时间不晚于**现行更正后投标
   截止时间**（含同一时刻）的回执；截止时间之后受理的上传/撤回一律忽略，并在
   响应的 `ignored_after_deadline` 中列出。
2. **撤回按引用生效**：撤回回执通过“撤回上传编号”作用于对应上传；引用的上传
   编号在该标段没有对应记录时，撤回链无法自洽，结论为**无法判定**。
3. **末份未撤回上传**：在从未被撤回的上传中取受理时间最新（同时间按回执编号
   排序）的一份，作为平台当前受理版本；若全部上传均已撤回，则**没有有效上传**。
4. **无法判定情形**：
   - 同一受理时间存在多份上传且 SHA-256 摘要不一致（无法确定平台实际受理哪份）；
   - 撤回引用的上传编号无对应记录；
   - 回执目录任一列表页/详情页抓取失败（HTTP 非 2xx、网络错误、字段缺失）。
   无法判定时 `status="undecidable"`、`file_matches=null`，具体原因见 `reasons`。
5. **核对结论**：`matched`（手头文件与末份上传摘要一致）、`mismatch`（不一致）、
   `no_valid_upload`（没有有效上传，明确说明原因）、`undecidable`（无法判定）。
   比对大小写不敏感；输入的 SHA-256 必须为 64 位十六进制，否则 422。
6. 判定与页面排列、抓取顺序完全无关（纯函数，按受理时间与编号确定性排序）。

## 目录结构

```
tender-collector/
├── app/                    # FastAPI 应用
│   ├── main.py             # 路由：/api/collect、/api/receipt-check、/api/projects、/api/collections
│   ├── service.py          # 采集编排 + 事务边界 + 过期判定
│   ├── scraper.py          # httpx + BeautifulSoup 公告抓取/解析
│   ├── merge.py            # 更正归并与生效截止时间判定（纯函数）
│   ├── receipt_service.py  # 回执核对编排：取现行截止时间 -> 抓回执 -> 判定
│   ├── receipt_scraper.py  # 回执目录分页抓取/解析
│   ├── receipt_merge.py    # 撤回生效、末份上传定位、无法判定规则（纯函数）
│   ├── database.py         # SQLite 连接、建表、UPSERT
│   ├── models.py           # Pydantic 模型
│   └── config.py           # 环境变量配置
├── demo_site/              # 生成的本地分页 HTML 示例公告站（静态文件）
│   └── receipts/           # 标书上传/撤回回执目录子站（分页 + 详情）
├── scripts/generate_demo_site.py  # 示例站与回执样例生成器（数据可复现）
├── tests/test_collection.py       # 公告采集端到端 + 归并单元测试
├── tests/test_receipt_check.py    # 回执核对端到端 + 判定/解析单元测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
# 1. 生成示例公告站静态文件（已随仓库提供，可重复生成）
python3 scripts/generate_demo_site.py

# 2. 启动：API 在 :8000，示例公告站在 :8001
docker compose up --build -d

# 3. 提交列表页地址触发采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site/list_1.html"}'

# 4. 查询项目当前截止时间
curl http://localhost:8000/api/projects

# 5. 回执核对（先采集公告，再核对手头标书；回执目录在示例站 receipts/ 下）
curl -X POST http://localhost:8000/api/receipt-check \
  -H 'Content-Type: application/json' \
  -d '{
    "original_code": "SZ-2026-001",
    "section_code": "G1",
    "file_sha256": "3f1b9c5e7a2d4f6081b9c5e7a2d4f6081b9c5e7a2d4f6081b9c5e7a2d4f6081b",
    "receipt_list_url": "http://demo-site/receipts/receipt_list_1.html"
  }'
```

> 上例 SHA-256 仅为占位；可复现的样例摘要由生成器常量
> `SAMPLE_FILE_HASHES` 给出（见下文“回执样例数据设计”）。

容器内 API 通过 compose 网络访问示例站（主机名 `demo-site`）；从宿主机浏览
示例站则打开 <http://localhost:8001/list_1.html>。SQLite 数据存于命名卷
`tender-data`（容器内 `/data/tenders.db`）。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除采集数据
```

## 本地开发运行（不使用 Docker）

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/generate_demo_site.py

# 终端 A：示例公告站
python3 -m http.server 8001 --directory demo_site

# 终端 B：API（数据库默认在 ./data/tenders.db）
uvicorn app.main:app --reload --port 8000

# 终端 C：测试
pytest -q

# 采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://127.0.0.1:8001/list_1.html"}'
```

交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>

## API 说明

### `POST /api/collect` — 提交列表页，触发采集

请求：

```json
{ "list_url": "http://demo-site/list_1.html" }
```

成功响应（200）：

```json
{
  "status": "success",
  "source_url": "http://demo-site/list_1.html",
  "collected": 10,
  "projects": [
    {
      "project_name": "城北污水处理厂扩建工程",
      "current_deadline": "2026-11-30 10:00",
      "deadline_source_code": "SZ-2026-002-G2",
      "deadline_source_url": "http://demo-site/SZ-2026-002-G2.html",
      "is_expired": false
    }
  ]
}
```

失败响应（422，任一页面不可读/数据无法归并）：

```json
{ "detail": "读取页面失败 http://demo-site/SZ-2026-002-G2.html: HTTP 404" }
```

### `GET /api/projects` — 查询全部项目的当前截止时间

返回字段：项目名称、当前生效截止时间、截止时间来源公告编号、来源链接、是否过期。

### `GET /api/collections?limit=20` — 采集运行历史

包含每次采集的成功/失败状态、采集条数与错误信息。

### `POST /api/receipt-check` — 回执核对：手头文件是否仍是平台受理版本

请求：

```json
{
  "original_code": "SZ-2026-001",
  "section_code": "G1",
  "file_sha256": "<64 位十六进制 SHA-256>",
  "receipt_list_url": "http://demo-site/receipts/receipt_list_1.html"
}
```

成功响应（200，`matched` 示例）：

```json
{
  "status": "matched",
  "file_matches": true,
  "message": "手头文件与平台受理的末份未撤回上传 UP-001-3 摘要一致，确认是当前受理版本。",
  "original_code": "SZ-2026-001",
  "section_code": "G1",
  "project_name": "滨河路市政改造工程",
  "current_deadline": "2026-11-25 14:30",
  "deadline_source_code": "SZ-2026-001-G1",
  "deadline_source_url": "http://demo-site/SZ-2026-001-G1.html",
  "receipt_list_url": "http://demo-site/receipts/receipt_list_1.html",
  "effective_upload": {
    "code": "UP-001-3",
    "kind": "upload",
    "accepted_at": "2026-11-22 16:40",
    "file_sha256": "…",
    "withdraws_code": null,
    "detail_url": "http://demo-site/receipts/UP-001-3.html"
  },
  "valid_receipts": [ { "code": "UP-001-1", "kind": "upload", "…": "…" } ],
  "source_urls": ["http://demo-site/receipts/UP-001-1.html", "…"],
  "ignored_after_deadline": [
    { "code": "UP-001-LATE", "kind": "upload", "…": "…" }
  ],
  "reasons": []
}
```

`status` 取值与语义：

| status | file_matches | 含义 |
| --- | --- | --- |
| `matched` | `true` | 手头文件与末份未撤回上传摘要一致 |
| `mismatch` | `false` | 末份未撤回上传存在，但摘要不一致 |
| `no_valid_upload` | `null` | 截止前没有未撤回的上传（含全部撤回/无记录），`message` 明确说明 |
| `undecidable` | `null` | 摘要冲突、撤回引用悬空或页面抓取失败，原因见 `reasons` |

说明：

- 回执页面抓取失败**不返回 422**，而是 200 + `undecidable`（属于核对结论）；
  仅请求本身不合法（原公告未采集、SHA-256 格式错误等）才返回 422。
- `valid_receipts` 为截止前受理、且撤回引用可解析的全部回执；`source_urls`
  为其来源链接（含末份上传详情页），便于人工复核。
- `ignored_after_deadline` 列出截止时间之后受理、按规则忽略的记录。

## 示例站数据设计

`demo_site/` 含 5 个项目、10 条公告、3 个列表页，列表按发布时间**倒序**排列
（更正公告排在原公告之前），专门用于验证归并不受页面顺序影响：

| 项目 | 场景 | 生效截止时间来源 |
| --- | --- | --- |
| 滨河路市政改造工程 | 一次更正延期 | 更正公告 SZ-2026-001-G1 |
| 城北污水处理厂扩建工程 | 两次更正，取最新一次 | 第二次更正 SZ-2026-002-G2 |
| 东风桥加固维修工程 | 无更正，已过期 | 原公告 SZ-2026-003 |
| 高新区雨污分流管网工程 | 更正不改截止时间 | 原公告 SZ-2026-004 |
| 湖滨公园配套市政道路工程 | 原公告在末页、更正在首页（跨页乱序） | 更正公告 SZ-2026-005-G1 |

## 回执样例数据设计

`demo_site/receipts/` 含 23 份回执（上传/撤回）与 4 个分页，目录按受理时间
**倒序**分页（最新记录在第一页），跨页乱序，判定不依赖页面顺序。场景如下：

| 原公告/标段 | 截止时间 | 场景 | 核对结论 |
| --- | --- | --- | --- |
| SZ-2026-001 / G1 | 2026-11-25 14:30 | 两次上传均撤回后再上传；另有截止后的上传 | 末份 UP-001-3，截止后记录忽略 |
| SZ-2026-002 / G1 | 2026-11-30 10:00 | 同一受理时间两份上传摘要不一致 | `undecidable` |
| SZ-2026-002 / G2 | 2026-11-30 10:00 | 首份上传撤回、再上传；G9 标段为无关噪声 | 末份 UP-002G2-2，G9 不参与 |
| SZ-2026-003 / G1 | 2026-10-30 17:00 | 两份上传均被撤回 | `no_valid_upload` |
| SZ-2026-004 / G1 | 2026-11-28 09:30 | 撤回引用了不存在的上传编号 | `undecidable` |
| SZ-2026-005 / G1 | 2026-11-20 15:00 | 截止后才受理的撤回不生效 | 末份 UP-005-2 仍有效 |

样例中用于“手头文件”的确定性摘要由生成器常量 `SAMPLE_FILE_HASHES` 给出，
测试直接引用；所有摘要均为对种子字符串取真实 SHA-256，可离线复现：

```bash
python3 -c "import importlib.util,sys; \
spec=importlib.util.spec_from_file_location('g','scripts/generate_demo_site.py'); \
m=importlib.util.module_from_spec(spec); sys.modules['g']=m; spec.loader.exec_module(m); \
print(m.SAMPLE_FILE_HASHES)"
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TENDER_DB_PATH` | 本地 `./data/tenders.db`；容器内 `/data/tenders.db` | SQLite 文件路径 |
| `TENDER_HTTP_TIMEOUT` | `15` | 单个页面请求超时（秒） |
| `TENDER_MAX_LIST_PAGES` | `200` | 单次采集最多遍历的列表页数（防环） |
| `TENDER_USER_AGENT` | `TenderCollector/1.0 ...` | 请求 UA |

## 解析约定（接入真实站点时）

解析器依赖以下稳定选择器，适配其他站点时调整 `app/scraper.py`、
`app/receipt_scraper.py` 即可：

- 公告列表页：`ul.announcement-list a.detail-link` 为详情链接，
  `nav.pagination a` 为分页链接；
- 公告详情页：`article.announcement-detail` 容器，支持
  `data-code` / `data-kind` / `data-published-at` / `data-corrects` 属性，
  以及 `table.detail-table` 内 `th[data-field="字段名"] + td` 表格字段
  （公告编号、公告类型、项目名称、发布时间、投标截止时间、原公告编号）。
- 回执目录页：`ul.receipt-list a.receipt-link` 为回执详情链接，
  `nav.pagination a` 为分页链接；
- 回执详情页：`article.receipt-detail` 容器，支持
  `data-code` / `data-kind` / `data-original-code` / `data-section-code` /
  `data-accepted-at` / `data-withdraws` 属性，以及
  `table.detail-table` 内字段：回执编号、回执类型、原公告编号、标段号、
  受理时间、文件摘要SHA-256（上传）、撤回上传编号（撤回）。
