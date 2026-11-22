"""通过 FastAPI TestClient 的端到端验收：导出隔离、签署失效与重跑校验码。"""
import copy
import hashlib
import os
import sys
import unittest

os.environ.setdefault("AUTH_KEY", "test-key")
os.environ.setdefault("DB_HOST", "/tmp/pgrun")
os.environ.setdefault("DB_NAME", "gb5309_test")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6390/0")
os.environ["SEED_DEMO"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
sys.path.insert(0, BACKEND)

from src.main import app  # noqa: E402
from src.models.tables import cases, evidence, runs, users  # noqa: E402
from src.services import workflow  # noqa: E402
from src.services.domain import SPEC  # noqa: E402
from src.services.security import hash_password  # noqa: E402
from src.utils.encoding import canonical  # noqa: E402


class AcceptanceApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.__enter__()
        # Start from a clean slate so repeated discover runs do not collide on usernames.
        from src.models.tables import audit
        with workflow.engine.begin() as conn:
            for table in (audit, evidence, runs, cases, users):
                conn.execute(table.delete())
        with workflow.engine.begin() as conn:
            for role in ("operator", "reviewer", "viewer"):
                conn.execute(users.insert().values(
                    username=role, role=role,
                    password_hash=hash_password("Demo-Walkthrough-2026")))
        cls.tokens = {}
        for role in cls.tokens_targets:
            response = cls.client.post("/login", json={
                "username": role, "password": "Demo-Walkthrough-2026"})
            assert response.status_code == 200, response.text
            cls.tokens[role] = response.json()["token"]

    tokens_targets = ("operator", "reviewer", "viewer")

    @classmethod
    def tearDownClass(cls):
        with workflow.engine.begin() as conn:
            for table in (evidence, runs, cases, users):
                conn.execute(table.delete())
        cls.client.__exit__(None, None, None)

    def auth(self, role):
        return {"Authorization": f"Bearer {self.tokens[role]}"}

    def process_next_run(self):
        job = workflow.claim()
        self.assertIsNotNone(job)
        run_id, token, payload = job
        from src.services.domain import solve
        workflow.finish(run_id, token, result=solve(payload, workflow.engine))
        return run_id

    def test_full_acceptance_lifecycle(self):
        payload = copy.deepcopy(SPEC["sample"])
        created = self.client.post("/cases", json={"title": "端到端验收档案", "payload": payload},
                                   headers=self.auth("operator"))
        self.assertEqual(created.status_code, 201, created.text)
        case_id = created.json()["id"]

        run_response = self.client.post(f"/cases/{case_id}/runs", headers=self.auth("operator"))
        self.assertEqual(run_response.status_code, 202)
        run_id = self.process_next_run()

        listing = self.client.get("/evidence", headers=self.auth("reviewer")).json()
        evidence_id = next(row["id"] for row in listing if row["run_id"] == run_id)

        # 导出的验收单与库内证据逐字节一致，校验码即报告的规范序列化 SHA-256。
        download = self.client.get(f"/evidence/{evidence_id}/download", headers=self.auth("viewer"))
        self.assertEqual(download.status_code, 200)
        self.assertIn(f'filename="evidence-{evidence_id}.json"',
                      download.headers["content-disposition"])
        import json
        exported = json.loads(download.content)
        row = next(row for row in listing if row["id"] == evidence_id)
        self.assertEqual(download.content, canonical(row))
        self.assertEqual(row["digest"], hashlib.sha256(canonical(row["report"])).hexdigest())
        # 逐笔轨迹随验收单一起导出。
        titles = [section["title"] for section in exported["report"]["sections"]]
        self.assertIn("工单消费轨迹", titles)

        # 复核签署。
        review = self.client.post(f"/evidence/{evidence_id}/review",
                                  json={"note": "同意切换"}, headers=self.auth("reviewer"))
        self.assertEqual(review.status_code, 200, review.text)
        self.assertEqual(review.json()["reviewer"], "reviewer")

        # 同输入重跑：新证据校验码与首份完全一致，且事件明细不新增。
        self.client.post(f"/cases/{case_id}/runs", headers=self.auth("operator"))
        second_run = self.process_next_run()
        listing = self.client.get("/evidence", headers=self.auth("reviewer")).json()
        second = next(row for row in listing if row["run_id"] == second_run)
        first = next(row for row in listing if row["id"] == evidence_id)
        self.assertEqual(second["digest"], first["digest"])
        self.assertEqual(second["report"]["input_fingerprint"], first["report"]["input_fingerprint"])

        # 源数据更新：旧证据整体失效。
        changed = copy.deepcopy(payload)
        changed["orders"][0]["legacy_hours"] = "1.50"
        amended = self.client.put(f"/cases/{case_id}", json={"payload": changed},
                                  headers=self.auth("operator"))
        self.assertEqual(amended.status_code, 200, amended.text)
        listing = self.client.get("/evidence", headers=self.auth("reviewer")).json()
        for row in listing:
            if row["run_id"] in (run_id, second_run):
                self.assertIsNotNone(row["staled_at"])

        # 失效证据既不能补签，也不能混入导出的验收单。
        self.assertEqual(self.client.post(f"/evidence/{evidence_id}/review",
                                          json={"note": "补签"}, headers=self.auth("reviewer")).status_code, 409)
        blocked = self.client.get(f"/evidence/{evidence_id}/download", headers=self.auth("viewer"))
        self.assertEqual(blocked.status_code, 409)

        # 新源数据重跑后，新证据可签可导，导出内容不含任何失效行。
        self.client.post(f"/cases/{case_id}/runs", headers=self.auth("operator"))
        third_run = self.process_next_run()
        listing = self.client.get("/evidence", headers=self.auth("reviewer")).json()
        fresh = next(row for row in listing if row["run_id"] == third_run)
        self.assertIsNone(fresh["staled_at"])
        fresh_download = self.client.get(f"/evidence/{fresh['id']}/download",
                                         headers=self.auth("viewer"))
        self.assertEqual(fresh_download.status_code, 200)
        fresh_json = json.loads(fresh_download.content)
        self.assertIsNone(fresh_json["staled_at"])
        self.assertEqual(fresh_json["report"]["input_fingerprint"],
                         hashlib.sha256(canonical(changed)).hexdigest())

    def test_roles_and_input_guards(self):
        payload = copy.deepcopy(SPEC["sample"])
        # 观察员与复核员不能建档案；更新接口校验失败返回明细且不产生失效标记。
        self.assertEqual(self.client.post("/cases", json={"title": "x", "payload": payload},
                                          headers=self.auth("viewer")).status_code, 403)
        created = self.client.post("/cases", json={"title": "权限档案", "payload": payload},
                                   headers=self.auth("operator")).json()
        self.assertEqual(self.client.put(f"/cases/{created['id']}",
                                         json={"payload": {**payload, "batch_size": 0}},
                                         headers=self.auth("reviewer")).status_code, 403)
        bad = self.client.put(f"/cases/{created['id']}",
                              json={"payload": {**payload, "batch_size": 0}},
                              headers=self.auth("operator"))
        self.assertEqual(bad.status_code, 422)
        self.assertIn("每批工单数", bad.json()["detail"])
        case = self.client.get("/cases", headers=self.auth("operator")).json()
        self.assertEqual(next(row for row in case if row["id"] == created["id"])["payload"],
                         payload)


if __name__ == "__main__":
    unittest.main()
