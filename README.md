# 采购三方勾稽报告

面向真实数据复核的中型数据工程工具，包含六个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18218。测试：docker compose exec -T app pytest -q

## 模块

- M1 单据采信：页面 /documents，接口 /api/documents/import
- M2 主数据映射：页面 /master，接口 /api/master/resolve
- M3 数量勾稽：页面 /quantity，接口 /api/quantity/match
- M4 税额核验：页面 /tax，接口 /api/tax/check
- M5 多对多匹配：页面 /matching，接口 /api/matches/solve
- M6 差异批次：页面 /batches，接口 /api/reconcile

### 版本化勾稽（M6）

- `POST /api/versions/{order|acceptance|tax}`：登记不可变数据版本，返回 `version_id`；版本号只有在引用它的批次提交时才成为当前版本。
- `POST /api/batches`：按版本开批，可用 `order_version` / `acceptance_version` / `tax_version` 指定，缺省用当前版本；传 `process: false` 只建批不处理。
- `POST /api/batches/{id}/process`（`?max_items=`）与 `POST /api/batches/{id}/retry`：从断点续跑，已入账条目不会重复记账。
- `GET /api/batches`、`GET /api/batches/{id}`：列出当前与历史批次，并给出相邻已提交批次的差异。
- `GET /api/batches/compare?base=&target=`：显式比较两个批次。
- `POST /api/orders/{order_no}/revisions`：原子修订订单。失败时保留旧版本与旧批次，并记录拒绝原因。
- `GET /api/invoices/{invoice_no}/registration`：按登记时的三方口径重新核验已登记发票。
- `GET /api/export/differences.csv?batch_id=`：只导出所选版本；不传时导出当前批次。

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

每个勾稽批次在开立时绑定订单、验收和税额三个不可变版本。已登记发票的凭据按登记时的三方口径复核，不会被后续重算改写；新发票按最新版本勾稽，数量或金额冲突进入隔离。订单修订在批次处理完成后原子切换版本与当前批次，失败则保留旧版。批次以 checkpoint 断点续跑，台账主键保证重试不重复入账。

