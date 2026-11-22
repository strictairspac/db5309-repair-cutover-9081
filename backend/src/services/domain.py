"""针渡修衣工时换版的输入契约及切换验收单。"""

from collections import Counter
from decimal import Decimal
import re

from .repair_migration import RepairMigration


SPEC = {
    "name": "针渡修衣·工时刻度换版",
    "summary": "在门店继续改工时的同时回填整数刻度，核验新版读数和未消费事件后签发切换结论。",
    "customer": "针渡修衣连锁结算组与门店调度组",
    "entity_labels": ["修衣工单", "分批回填", "切换验收"],
    "field_labels": {
        "batch_size": "每批工单数", "max_batches": "本次最多执行批次",
        "orders": "存量修衣工单", "events": "双写变更",
        "ticket": "工单号", "legacy_hours": "旧工时（小时，十进制字符串）",
        "revision": "版次", "after_batch": "交错批次（0 为回填前）",
    },
    "guidance": "每 15 分钟换 1 刻度。交错批次为 0 时先消费变更，其他值在对应批次取数后、写回前消费。",
    "sample": {
        "batch_size": 2,
        "max_batches": 3,
        "orders": [
            {"ticket": "ZD-101", "legacy_hours": "1.25", "revision": 3},
            {"ticket": "ZD-102", "legacy_hours": "0.75", "revision": 1},
            {"ticket": "ZD-103", "legacy_hours": "2.00", "revision": 4},
            {"ticket": "ZD-104", "legacy_hours": "0.10", "revision": 1},
            {"ticket": "ZD-105", "legacy_hours": "0.00", "revision": 1},
        ],
        "events": [
            {"ticket": "ZD-101", "legacy_hours": "1.75", "revision": 4, "after_batch": 1},
            {"ticket": "ZD-101", "legacy_hours": "1.25", "revision": 3, "after_batch": 1},
            {"ticket": "ZD-101", "legacy_hours": "1.75", "revision": 4, "after_batch": 1},
            {"ticket": "ZD-104", "legacy_hours": "0.50", "revision": 2, "after_batch": 2},
            {"ticket": "ZD-100", "legacy_hours": "0.25", "revision": 1, "after_batch": 2},
        ],
    },
    "expect": {
        "执行批次": 3, "一致工单": 6, "回填写入": 3, "竞态拦截": 1,
        "未消费事件": 0, "切换门禁": "允许切换",
    },
}


def _require_integer(value, label, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{label}须为 {lower} 至 {upper} 的整数")


def validate(payload):
    if not isinstance(payload, dict) or payload.keys() != SPEC["sample"].keys():
        raise ValueError("请提供每批工单数、批次上限、存量工单和双写变更")
    _require_integer(payload["batch_size"], "每批工单数", 1, 1000)
    _require_integer(payload["max_batches"], "批次上限", 0, 5000)
    seen = set()
    for key, label in (("orders", "存量工单"), ("events", "双写变更")):
        rows = payload[key]
        if not isinstance(rows, list) or len(rows) > 5000:
            raise ValueError(f"{label}须为最多 5000 项的数组")
        fields = {"ticket", "legacy_hours", "revision"}
        if key == "events":
            fields.add("after_batch")
        for number, row in enumerate(rows, 1):
            location = f"{label}第 {number} 行"
            if not isinstance(row, dict) or row.keys() != fields:
                raise ValueError(f"{location}字段缺失或未知")
            ticket, hours = row["ticket"], row["legacy_hours"]
            if not isinstance(ticket, str) or re.fullmatch(r"[A-Z][A-Z0-9-]{0,31}", ticket) is None:
                raise ValueError(f"{location}工单号须以大写字母开头，仅含大写字母、数字或连字符，最多 32 位")
            if (not isinstance(hours, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,6})?", hours) is None
                    or Decimal(hours) > 168):
                raise ValueError(f"{location}旧工时须为 0 至 168 的十进制字符串，最多 6 位小数")
            _require_integer(row["revision"], f"{location}版次", 1, 2147483647)
            if key == "events":
                _require_integer(row["after_batch"], f"{location}交错批次", 0, 5000)
            else:
                if ticket in seen:
                    raise ValueError(f"重复存量工单 {ticket}")
                seen.add(ticket)


def _report(orders, events, batches, races):
    order_counts = Counter(row["status"] for row in orders)
    event_counts = Counter(row["status"] for row in events)
    checks = [
        ("存在可验收工单", len(orders), bool(orders)),
        ("不可转换工单", order_counts["不可转换"], order_counts["不可转换"] == 0),
        ("待回填工单", order_counts["待回填"], order_counts["待回填"] == 0),
        ("影子版次不一致", order_counts["版次不一致"], order_counts["版次不一致"] == 0),
        ("影子刻度不一致", order_counts["刻度不一致"], order_counts["刻度不一致"] == 0),
        ("待重放事件", event_counts["待消费"], event_counts["待消费"] == 0),
        ("同版冲突事件", event_counts["同版冲突"], event_counts["同版冲突"] == 0),
    ]
    blockers = [label for label, _, passed in checks if not passed]
    verdict = "暂缓切换" if blockers else "允许切换"
    summary = f"暂缓切换，需处理{'、'.join(blockers)}。" if blockers else (
        f"允许切换，{len(orders)} 张工单的旧工时、影子刻度与版次一致，变更队列已清空。"
    )
    return {
        "summary": summary,
        "metrics": {
            "切换门禁": verdict, "执行批次": len(batches), "一致工单": order_counts["一致"],
            "回填写入": sum(batch["回填写入"] for batch in batches), "竞态拦截": len(races),
            "待回填工单": order_counts["待回填"], "不可转换工单": order_counts["不可转换"],
            "影子不一致": order_counts["版次不一致"] + order_counts["刻度不一致"],
            "未消费事件": event_counts["待消费"] + event_counts["同版冲突"],
        },
        "sections": [
            {"title": "切换门禁", "rows": [
                {"核对项": label, "工单或事件数": count, "判定": "通过" if passed else "阻断"}
                for label, count, passed in checks
            ]},
            {"title": "工单读数", "rows": [
                {"工单号": row["ticket"], "旧工时（小时）": row["legacy_hours"], "当前版次": row["revision"],
                 "影子刻度": row["shadow_ticks"], "影子版次": row["shadow_revision"], "判定": row["status"]}
                for row in orders
            ]},
            {"title": "分批回填", "rows": batches},
            {"title": "过期快照写入凭据", "rows": races},
            {"title": "双写事件消费", "rows": [
                {"事件序号": row["seq"], "交错批次": row["after_batch"], "工单号": row["ticket"],
                 "提交工时（小时）": row["legacy_hours"], "提交版次": row["revision"],
                 "消费时工单版次": row["observed_revision"], "判定": row["status"]}
                for row in events
            ]},
        ],
    }


def solve(payload, engine):
    validate(payload)
    if engine.dialect.name != "postgresql":
        raise ValueError("工时迁移演练需要 PostgreSQL")
    migration = RepairMigration(engine)
    migration.install(payload["orders"], payload["events"])
    try:
        replay_phases = {row["after_batch"] for row in payload["events"]}
        if 0 in replay_phases:
            migration.replay(0)
        cursor, batches, races = "", [], []
        for number in range(1, payload["max_batches"] + 1):
            batch = migration.backfill(cursor, payload["batch_size"], number, number in replay_phases)
            if batch is None:
                break
            evidence, skipped = batch
            batches.append(evidence)
            races.extend(skipped)
            cursor = evidence["最后工单"]
        orders, events = migration.read_gate()
        return _report(orders, events, batches, races)
    finally:
        migration.close()
