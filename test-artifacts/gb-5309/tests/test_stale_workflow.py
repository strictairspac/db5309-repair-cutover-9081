"""复核签署后源数据更新、旧结论失效与验收单导出隔离测试。

环境变量 GB5309_WORKFLOW_DSN 指向专用测试库（会被建表/写入，勿用生产库），
默认使用本机可移植实例的 gb5309_test 库。
"""
import copy
import os
import sys
import unittest

os.environ.setdefault("AUTH_KEY", "gb5309-local-test-key")

from fastapi import HTTPException
from sqlalchemy import create_engine

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
sys.path.insert(0, BACKEND)

from src.models.tables import cases, evidence, runs, schema  # noqa: E402
from src.services import workflow  # noqa: E402
from src.services.domain import SPEC, solve  # noqa: E402
from src.utils.encoding import canonical  # noqa: E402

DSN = os.getenv(
    "GB5309_WORKFLOW_DSN",
    "postgresql+psycopg://app@/gb5309_test?host=/tmp/pgrun",
)


class StaleEvidenceWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DSN)
        schema.create_all(cls.engine)
        cls._original_engine = workflow.engine
        workflow.engine = cls.engine

    @classmethod
    def tearDownClass(cls):
        workflow.engine = cls._original_engine
        with cls.engine.begin() as conn:
            for table in (evidence, runs, cases):
                conn.execute(table.delete())
        cls.engine.dispose()

    def _run_once(self, payload):
        row = workflow.enqueue(self.case_id, "operator")
        run_id = row["id"]
        claimed_id, token, claimed_payload = workflow.claim()
        self.assertEqual(claimed_id, run_id)
        report = solve(claimed_payload, self.engine)
        workflow.finish(run_id, token, result=report)
        evidence_id = workflow.recent(evidence)[0]["id"]
        return run_id, evidence_id, report

    def setUp(self):
        self.payload = copy.deepcopy(SPEC["sample"])
        self.case_id = workflow.make_case("失效演练档案", self.payload, "operator")["id"]

    def test_signed_evidence_invalidated_when_source_changes(self):
        _, evidence_id, report = self._run_once(self.payload)
        # 复核签署先通过：输入指纹与当前源数据一致。
        signed = workflow.review(evidence_id, "核对无误，同意切换", "reviewer")
        self.assertEqual(signed["reviewer"], "reviewer")
        self.assertIsNone(signed["staled_at"])

        # 源数据更新：同一批演练的旧结论必须整体失效。
        changed = copy.deepcopy(self.payload)
        changed["orders"][0]["legacy_hours"] = "1.50"
        workflow.update_case(self.case_id, changed, "operator")
        with self.engine.connect() as conn:
            stale = workflow.found(conn, evidence, evidence_id)
        self.assertIsNotNone(stale["staled_at"])
        self.assertTrue(stale["stale_reason"])

        # 失效证据不能再签署，也不能导出验收单。
        with self.assertRaises(HTTPException) as review_err:
            workflow.review(evidence_id, "试图补签", "reviewer")
        self.assertEqual(review_err.exception.status_code, 409)
        with self.assertRaises(HTTPException) as export_err:
            workflow.assert_current(evidence_id)
        self.assertEqual(export_err.exception.status_code, 409)

        # 新源数据重跑得到新证据；导出的验收单只含新证据，不混入旧条目。
        _, evidence_id_two, report_two = self._run_once(changed)
        exported = workflow.assert_current(evidence_id_two)
        self.assertNotEqual(exported["id"], evidence_id)
        self.assertIsNone(exported["staled_at"])
        self.assertEqual(exported["report"]["input_fingerprint"], report_two["input_fingerprint"])
        self.assertNotEqual(report["input_fingerprint"], report_two["input_fingerprint"])
        # 新输入重跑依旧确定性：结果与校验码保持一致。
        rerun = solve(changed, self.engine)
        self.assertEqual(canonical(rerun), canonical(report_two))
        self.assertEqual(rerun["input_fingerprint"], report_two["input_fingerprint"])

    def test_fingerprint_mismatch_blocks_review_and_export(self):
        _, evidence_id, _ = self._run_once(self.payload)
        changed = copy.deepcopy(self.payload)
        changed["batch_size"] = 3
        # 绕过 update_case 的显式失效标记，模拟外部直接改源数据，验证签署时的指纹兜底。
        with self.engine.begin() as conn:
            conn.execute(cases.update().where(cases.c.id == self.case_id).values(payload=changed))
        with self.assertRaises(HTTPException) as ctx:
            workflow.review(evidence_id, "签署", "reviewer")
        self.assertEqual(ctx.exception.status_code, 409)
        with self.engine.connect() as conn:
            row = workflow.found(conn, evidence, evidence_id)
        self.assertIsNotNone(row["staled_at"])
        with self.assertRaises(HTTPException):
            workflow.assert_current(evidence_id)
    def test_invalid_payload_rejected_before_stale_marking(self):
        bad = copy.deepcopy(self.payload)
        bad["batch_size"] = 99999
        with self.assertRaises(HTTPException) as ctx:
            workflow.update_case(self.case_id, bad, "operator")
        self.assertEqual(ctx.exception.status_code, 422)
        # 校验失败不得改动源数据，已有证据保持有效。
        _, evidence_id, _ = self._run_once(self.payload)
        row = workflow.assert_current(evidence_id)
        self.assertIsNone(row["staled_at"])


if __name__ == "__main__":
    unittest.main()
