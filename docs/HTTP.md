# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

## 营业日签发

- `GET /api/settlements?run_id=...`（也兼容 `/api/days`、`/api/daily`、`/api/business-days`）：返回各营业日的总额、`signed_amount` 已签、`pending_amount` 暂挂、`held_amount` 跨日暂挂。不传 `run_id` 时可用 `?profile=clean|dirty` 现算，或取最近批次。
- `POST /api/settlements/sign`：按营业日幂等签发。请求体支持：
  - `{"run_id": "...", "business_date": "YYYY-MM-DD"}`：签发该日可签的单日骑行，以及所有触及该日且门控已开的跨日骑行（跨日各段一起入账）；
  - `{"run_id": "...", "dates": ["...", "..."]}`：同时提交多个营业日，跨日单位自动去重；
  - `{"run_id": "...", "all": true}`：签发当前所有可入账单位；
  - 也可用 `trip_id` 整趟签发（门控未开会返回 `409 CROSS_DAY_LOCKED`），或直接给 `results` / `path` / `csv` / `rows` 补齐数据。
- 路径风格别名：`POST /api/days/{date}/sign`、`POST /api/business-days/{date}/sign`、`POST /api/runs/{id}/days/{date}/sign` 等。
- 重复或并发提交不会重复计入已签车费；响应含 `inserted` / `duplicated` 与最新 `days` / `totals`。
- `POST /api/runs` 额外接受 `path`、`csv`（内联 CSV 文本）或 `rows`（原始行对象数组），便于“补齐后重跑”。
