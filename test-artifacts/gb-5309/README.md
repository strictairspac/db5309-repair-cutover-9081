# gb-5309 测试工件

真实 PostgreSQL 上的端到端验证，不使用模拟数据库。

## 环境准备

测试需要一个可创建 schema 的 PostgreSQL 15/16 库，以及装好 `backend/requirements.txt`
（另含测试用 `httpx`）的 Python 环境。

```sh
# 业务演练库（solve() 会在其中创建并清理 gb5309_* 独立 schema）
export GB5309_DATABASE_URL='postgresql+psycopg://app@/workflow?host=/tmp/pgrun'
# 工作流持久化测试库（会建表并清空业务表，勿指向演示库）
export GB5309_WORKFLOW_DSN='postgresql+psycopg://app@/gb5309_test?host=/tmp/pgrun'
```

未设置环境变量时，默认连接本机 `/tmp/pgrun` 上的 `workflow` 与 `gb5309_test`。
`AUTH_KEY` 未设置时测试模块会给出本地默认值。

## 运行

在仓库根目录执行（与 `BUSINESS.md` 保持一致）：

```sh
PYTHONPATH=backend python -m unittest discover -s test-artifacts/gb-5309/tests -v
```

## 覆盖的验收口径

| 测试文件 | 覆盖要求 |
| --- | --- |
| `test_evidence_trail.py` | 按版次排序的逐工单消费轨迹；重复/过期/冲突分别计数；重跑不新增事件且报告与校验码一致；独立 schema 清理；`SPEC.expect` 固定断言 |
| `test_stale_workflow.py` | 签署后源数据更新导致旧证据失效；指纹兜底拦截；失效证据不可签署/导出；非法输入不产生失效标记 |
| `test_acceptance_api.py` | FastAPI 全链路：建档案、执行、导出字节与校验码、重跑同码、`PUT` 更新后 409 隔离、角色与 422 校验 |
