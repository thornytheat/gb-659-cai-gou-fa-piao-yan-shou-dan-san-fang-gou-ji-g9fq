# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。

## 版本化勾稽批次（M6）

- version_id：`<scope>-v<内容摘要前 12 位>`。scope 为 `order`（采购订单）、`acceptance`（验收与退货）、`tax`（发票税额）之一。
- version_no：同一 scope 内单调递增的版本序号。版本一经写入不可修改；内容完全相同则复用原 version_id。
- dataset_heads：每个 scope 当前生效的版本。仅在引用该版本的批次提交时于同一事务内切换。
- batch_id：`batch-<规则版本 + 订单/验收/税额版本号的摘要前 12 位>`。同一组版本重复开批复用既有批次。
- order_version / acceptance_version / tax_version：批次开立时绑定的三类事实版本，之后不再重算。
- checkpoint：已处理并提交的计划项序号。批次写入中断后从 checkpoint + 1 续跑。
- accepted / quarantined / carried：批次内已通过、隔离、沿用（原登记已通过）的条目数。
- posted：本批次实际写入不可变台账的行数；重试时由主键 `(doc_type, doc_no, line_no)` 去重，不会重复入账。
- invoice_registrations：发票登记凭据。登记时冻结三方口径（三个版本号、业务键、证据 JSON 及其摘要），此后只读。
- EVIDENCE_DRIFT：按登记时版本重算的证据与登记凭据摘要不一致。只新增隔离条目，不改写已通过记录。
- QUANTITY_CONFLICT / AMOUNT_CONFLICT / TAX_DIFFERENCE：新发票相对最新版本的三方数量或金额冲突，进入隔离，不覆盖历史通过记录。
- UNINVOICED / OPEN_QUANTITY：已验收未开票金额与订单未到量，作为差异导出。
- order_revisions：订单修订审计。status 为 `pending`、`committed` 或 `rejected`；`rejected` 保留旧版本与旧批次。
- 导出：`/api/export/differences.csv?batch_id=...` 只导出所选批次；不传 batch_id 时导出当前批次。
