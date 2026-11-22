"""针渡修衣·工时刻度换版：逐笔消费轨迹、重跑确定性与源数据失效测试。

环境变量 GB5309_DATABASE_URL 指向可创建 schema 的 PostgreSQL 测试库，默认使用
本机 /tmp/pgrun 的可移植实例。
"""
import copy
import hashlib
import os
import sys
import unittest

from sqlalchemy import create_engine

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
sys.path.insert(0, BACKEND)

from src.services.domain import SPEC, solve  # noqa: E402
from src.utils.encoding import canonical  # noqa: E402

DSN = os.getenv(
    "GB5309_DATABASE_URL",
    "postgresql+psycopg://app@/workflow?host=/tmp/pgrun",
)


def section(report, title):
    return next(part for part in report["sections"] if part["title"] == title)


def trail_for(report, ticket):
    return [row for row in section(report, "工单消费轨迹")["rows"] if row["工单号"] == ticket]


class MigrationEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(DSN)
        self.payload = copy.deepcopy(SPEC["sample"])

    def tearDown(self):
        self.engine.dispose()

    def test_sample_matches_fixed_expect(self):
        # 断言固定在 SPEC.expect：重复演练同一输入生成相同的业务证据。
        report = solve(self.payload, self.engine)
        for key, value in SPEC["expect"].items():
            self.assertEqual(report["metrics"][key], value, key)

    def test_per_ticket_trail_kept_in_revision_order(self):
        report = solve(self.payload, self.engine)
        trail = trail_for(report, "ZD-101")
        # 每条修衣记录保留按版次排序的消费轨迹：立案、过期、拦截、双写、重复。
        self.assertEqual([(r["版次"], r["环节"], r["判定"]) for r in trail], [
            (3, "立案", "影子待回填"),
            (3, "双写事件", "过期已消费"),
            (3, "分批回填", "快照拦截"),
            (4, "双写事件", "双写已消费"),
            (4, "双写事件", "重复已消费"),
        ])
        self.assertEqual([r["版次"] for r in trail], sorted(r["版次"] for r in trail))
        # 事件序号可回溯到原始流水，拦截条目保留被拒的 5 刻度与批次。
        self.assertEqual([r["事件序号"] for r in trail if r["事件序号"]], [2, 1, 3])
        blocked = next(r for r in trail if r["判定"] == "快照拦截")
        self.assertEqual((blocked["影子刻度"], blocked["回填批次"]), (5, 1))
        # 不可转换的旧版次同样留痕，且新版次由双写触发器补上 2 刻度。
        self.assertEqual([(r["版次"], r["判定"], r["影子刻度"])
                          for r in trail_for(report, "ZD-104")],
                         [(1, "不可转换", None), (2, "双写已消费", 2)])
        # 游标之后补录的 ZD-100 由插入触发器直接写 1 刻度并可被验收查到。
        z100 = trail_for(report, "ZD-100")
        self.assertEqual(z100[0]["判定"], "双写已消费")
        self.assertEqual(z100[0]["影子刻度"], 1)

    def test_duplicate_stale_conflict_counted_separately(self):
        report = solve(self.payload, self.engine)
        metrics = report["metrics"]
        self.assertEqual(metrics["重复已消费事件"], 1)
        self.assertEqual(metrics["过期已消费事件"], 1)
        self.assertEqual(metrics["同版冲突事件"], 0)
        consumed = {r["判定"] for r in section(report, "双写事件消费")["rows"]}
        self.assertEqual(consumed, {"双写已消费", "过期已消费", "重复已消费"})

        # 同版异值必须独立计为冲突，未到执行批次的事件保持待消费。
        conflict_payload = copy.deepcopy(self.payload)
        conflict_payload["orders"].append(
            {"ticket": "ZD-201", "legacy_hours": "1.00", "revision": 1})
        conflict_payload["events"] = [
            {"ticket": "ZD-201", "legacy_hours": "2.00", "revision": 1, "after_batch": 1},
            {"ticket": "ZD-202", "legacy_hours": "3.00", "revision": 1, "after_batch": 9},
        ]
        blocked = solve(conflict_payload, self.engine)
        self.assertEqual(blocked["metrics"]["同版冲突事件"], 1)
        self.assertEqual(blocked["metrics"]["未消费事件"], 2)
        self.assertEqual(blocked["metrics"]["切换门禁"], "暂缓切换")
        conflict = next(r for r in section(blocked, "双写事件消费")["rows"]
                        if r["判定"] == "同版冲突")
        self.assertEqual((conflict["工单号"], conflict["提交版次"], conflict["消费时工单版次"]),
                         ("ZD-201", 1, 1))
        pending = next(r for r in section(blocked, "双写事件消费")["rows"]
                       if r["判定"] == "待消费")
        self.assertEqual(pending["工单号"], "ZD-202")

    def test_rerun_adds_no_events_and_keeps_digest_stable(self):
        first = solve(self.payload, self.engine)
        second = solve(self.payload, self.engine)
        # 同输入重跑产生结构完全相同的业务证据（每次演练使用独立随机 schema）。
        self.assertEqual(canonical(first), canonical(second))
        digest = hashlib.sha256(canonical(first)).hexdigest()
        self.assertEqual(hashlib.sha256(canonical(second)).hexdigest(), digest)
        # 重跑后不新增事件：两条事件消费明细完全一致，待消费数仍为 0。
        self.assertEqual(section(first, "双写事件消费")["rows"],
                         section(second, "双写事件消费")["rows"])
        self.assertEqual(first["metrics"]["未消费事件"], 0)
        self.assertEqual(first["input_fingerprint"], second["input_fingerprint"])
        self.assertEqual(hashlib.sha256(canonical(self.payload)).hexdigest(),
                         first["input_fingerprint"])

    def test_conflict_trail_and_metrics_are_deterministic(self):
        payload = copy.deepcopy(self.payload)
        payload["events"].append(
            {"ticket": "ZD-103", "legacy_hours": "9.00", "revision": 4, "after_batch": 1})
        reports = [solve(payload, self.engine) for _ in range(2)]
        self.assertEqual(canonical(reports[0]), canonical(reports[1]))
        for report in reports:
            self.assertEqual(report["metrics"]["同版冲突事件"], 1)
            conflict_trail = [r for r in trail_for(report, "ZD-103") if r["判定"] == "同版冲突"]
            self.assertEqual(len(conflict_trail), 1)
            self.assertEqual(conflict_trail[0]["版次"], 4)

    def test_failed_rehearsal_leaves_no_residual_schema(self):
        bad_payload = copy.deepcopy(self.payload)
        bad_payload["batch_size"] = 0  # 校验失败，演练 schema 不应建立
        with self.assertRaises(ValueError):
            solve(bad_payload, self.engine)
        with self.engine.connect() as conn:
            left = conn.exec_driver_sql(
                "SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'gb5309_%%'").scalar_one()
        self.assertEqual(left, 0)
        # 正常演练结束后独立 schema 同样被清理。
        solve(self.payload, self.engine)
        with self.engine.connect() as conn:
            left = conn.exec_driver_sql(
                "SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'gb5309_%%'").scalar_one()
        self.assertEqual(left, 0)


if __name__ == "__main__":
    unittest.main()
