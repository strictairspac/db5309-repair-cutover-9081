# 结构演进

## 初始结构

`src/models/tables.py` 定义初始关系结构。API 启动时显式创建演示库的表。

## 追加列升级（逐笔证据与失效标记）

`business_evidence` 增加以下可空列，旧卷升级时由 `upgrade_schema(engine)` 在启动阶段幂等补列（`schema.create_all` 之后执行，重复运行无副作用）：

| 列 | 类型 | 用途 |
| --- | --- | --- |
| `input_fingerprint` | varchar(64) | 源输入规范序列化后的 SHA-256，用于重跑一致性与签署/导出前的指纹核对 |
| `staled_at` | varchar(32) | 源数据更新后旧结论的失效时间；非空即失效 |
| `stale_reason` | varchar(200) | 失效原因 |

每次演练使用的独立 schema（`gb5309_*`，含 `repair_orders`、`change_events` 与只追加的 `order_journal` 消费轨迹表）仍在执行结束时整体 `DROP SCHEMA ... CASCADE` 清理，不需要单独迁移。

需要在已有部署引入更多结构变更时，继续在 `upgrade_schema` 中以信息架构判断列/表是否存在，保持幂等。
