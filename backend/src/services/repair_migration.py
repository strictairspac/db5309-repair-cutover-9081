"""PostgreSQL transactions for one isolated repair-time migration rehearsal."""

import json
from uuid import uuid4

from sqlalchemy import text


class RepairMigration:
    def __init__(self, engine):
        self.engine = engine
        self.schema = f"gb5309_{uuid4().hex}"
        self.orders = f"{self.schema}.repair_orders"
        self.events = f"{self.schema}.change_events"
        self.to_ticks = f"{self.schema}.to_ticks"

    def install(self, orders, events):
        with self.engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA {self.schema}"))
            conn.execute(text(f"""
                CREATE TABLE {self.orders} (
                    ticket text COLLATE "C" PRIMARY KEY,
                    legacy_hours numeric NOT NULL CHECK (legacy_hours BETWEEN 0 AND 168),
                    revision integer NOT NULL CHECK (revision > 0)
                )
            """))
            if orders:
                conn.execute(text(f"""
                    INSERT INTO {self.orders} (ticket, legacy_hours, revision)
                    VALUES (:ticket, CAST(:legacy_hours AS numeric), :revision)
                """), orders)
            conn.execute(text(f"""
                ALTER TABLE {self.orders}
                    ADD COLUMN shadow_ticks integer CHECK (shadow_ticks BETWEEN 0 AND 672),
                    ADD COLUMN shadow_revision integer CHECK (shadow_revision > 0),
                    ADD CHECK ((shadow_ticks IS NULL) = (shadow_revision IS NULL))
            """))
            # An exact quarter-hour conversion. Casting an inexact numeric to integer would round it.
            conn.execute(text(f"""
                CREATE FUNCTION {self.to_ticks}(hours numeric) RETURNS integer
                LANGUAGE sql IMMUTABLE STRICT AS $$
                    SELECT CASE WHEN mod(hours * 4, 1) = 0 THEN (hours * 4)::integer END
                $$
            """))
            conn.execute(text(f"""
                CREATE FUNCTION {self.schema}.dual_write() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    NEW.shadow_ticks := {self.to_ticks}(NEW.legacy_hours);
                    NEW.shadow_revision := CASE WHEN NEW.shadow_ticks IS NOT NULL THEN NEW.revision END;
                    RETURN NEW;
                END;
                $$
            """))
            conn.execute(text(f"""
                CREATE TRIGGER dual_write
                BEFORE INSERT OR UPDATE OF legacy_hours, revision ON {self.orders}
                FOR EACH ROW EXECUTE FUNCTION {self.schema}.dual_write()
            """))
            conn.execute(text(f"""
                CREATE TABLE {self.events} (
                    seq integer PRIMARY KEY,
                    after_batch integer NOT NULL CHECK (after_batch >= 0),
                    ticket text NOT NULL,
                    legacy_hours numeric NOT NULL CHECK (legacy_hours BETWEEN 0 AND 168),
                    revision integer NOT NULL CHECK (revision > 0),
                    status text NOT NULL DEFAULT '待消费',
                    observed_revision integer
                )
            """))
            conn.execute(text(f"""
                CREATE INDEX change_events_pending ON {self.events} (after_batch, seq)
                WHERE status = '待消费'
            """))
            if events:
                conn.execute(text(f"""
                    INSERT INTO {self.events} (seq, after_batch, ticket, legacy_hours, revision)
                    VALUES (:seq, :after_batch, :ticket, CAST(:legacy_hours AS numeric), :revision)
                """), [{**event, "seq": seq} for seq, event in enumerate(events, 1)])

    def replay(self, batch_no):
        # The legacy update, the trigger's shadow write, and event acknowledgement commit together.
        with self.engine.begin() as conn:
            events = conn.execute(text(f"""
                SELECT seq, ticket, legacy_hours, revision
                FROM {self.events}
                WHERE after_batch = :batch AND status = '待消费'
                ORDER BY seq FOR UPDATE
            """), {"batch": batch_no}).mappings().all()
            for event in events:
                revision = conn.execute(text(f"""
                    INSERT INTO {self.orders} AS target (ticket, legacy_hours, revision)
                    VALUES (:ticket, :legacy_hours, :revision)
                    ON CONFLICT (ticket) DO UPDATE
                    SET legacy_hours = EXCLUDED.legacy_hours, revision = EXCLUDED.revision
                    WHERE target.revision < EXCLUDED.revision
                    RETURNING revision
                """), event).scalar_one_or_none()
                status = "双写已消费"
                if revision is None:
                    revision, same_hours = conn.execute(text(f"""
                        SELECT revision, legacy_hours = :legacy_hours FROM {self.orders}
                        WHERE ticket = :ticket
                    """), event).one()
                    if revision > event["revision"]:
                        status = "过期已消费"
                    else:
                        status = "重复已消费" if same_hours else "同版冲突"
                conn.execute(text(f"""
                    UPDATE {self.events} SET status = :status, observed_revision = :revision
                    WHERE seq = :seq
                """), {"status": status, "revision": revision, "seq": event["seq"]})

    def backfill(self, cursor, batch_size, batch_no, replay_due):
        with self.engine.connect().execution_options(isolation_level="READ COMMITTED") as conn, conn.begin():
            snapshot = conn.execute(text(f"""
                SELECT ticket, revision, {self.to_ticks}(legacy_hours) AS ticks,
                       shadow_revision IS NULL OR shadow_revision < revision AS needs_backfill
                FROM {self.orders}
                WHERE ticket > :cursor ORDER BY ticket LIMIT :batch_size
            """), {"cursor": cursor, "batch_size": batch_size}).mappings().all()
            if not snapshot:
                return None
            # No row locks are held by the snapshot. Replay commits on a separate connection.
            if replay_due:
                self.replay(batch_no)
            candidates = [dict(row) for row in snapshot if row["ticks"] is not None and row["needs_backfill"]]
            written = self.write_snapshot(conn, candidates)
            races = self.race_evidence(conn, batch_no, [row for row in candidates if row["ticket"] not in written])
            return {
                "批次": batch_no,
                "起始工单": snapshot[0]["ticket"],
                "最后工单": snapshot[-1]["ticket"],
                "扫描工单": len(snapshot),
                "回填写入": len(written),
                "竞态拦截": len(races),
                "快照不可转换": sum(row["ticks"] is None for row in snapshot),
                "快照已同步": sum(row["ticks"] is not None and not row["needs_backfill"] for row in snapshot),
            }, races

    def write_snapshot(self, conn, candidates):
        if not candidates:
            return set()
        return set(conn.execute(text(f"""
            UPDATE {self.orders} AS target
            SET shadow_ticks = snapshot.ticks, shadow_revision = snapshot.revision
            FROM jsonb_to_recordset(CAST(:snapshot AS jsonb)) AS snapshot(ticket text, revision integer, ticks integer)
            WHERE target.ticket = snapshot.ticket AND target.revision = snapshot.revision
              AND (target.shadow_revision IS NULL OR target.shadow_revision < snapshot.revision)
            RETURNING target.ticket
        """), {"snapshot": json.dumps(candidates)}).scalars())

    def race_evidence(self, conn, batch_no, skipped):
        if not skipped:
            return []
        current = conn.execute(text(f"""
            SELECT ticket, revision, shadow_ticks, shadow_revision FROM {self.orders}
            WHERE ticket = ANY(CAST(:tickets AS text[]))
        """), {"tickets": [row["ticket"] for row in skipped]}).mappings()
        by_ticket = {row["ticket"]: row for row in current}
        return [{
            "批次": batch_no,
            "工单号": row["ticket"],
            "快照版次": row["revision"],
            "快照刻度": row["ticks"],
            "更新行数": 0,
            "当前版次": by_ticket[row["ticket"]]["revision"],
            "影子版次": by_ticket[row["ticket"]]["shadow_revision"],
            "保留刻度": by_ticket[row["ticket"]]["shadow_ticks"],
        } for row in skipped]

    def read_gate(self):
        # Check the source, shadow and event queue against a single committed snapshot.
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn, conn.begin():
            orders = conn.execute(text(f"""
                SELECT ticket, legacy_hours::text AS legacy_hours, revision, shadow_ticks, shadow_revision,
                    CASE
                        WHEN mod(legacy_hours, 0.25) <> 0 THEN '不可转换'
                        WHEN shadow_ticks IS NULL THEN '待回填'
                        WHEN shadow_revision IS DISTINCT FROM revision THEN '版次不一致'
                        WHEN shadow_ticks * 0.25 <> legacy_hours THEN '刻度不一致'
                        ELSE '一致'
                    END AS status
                FROM {self.orders} ORDER BY ticket
            """)).mappings().all()
            events = conn.execute(text(f"""
                SELECT seq, after_batch, ticket, legacy_hours::text AS legacy_hours,
                       revision, status, observed_revision
                FROM {self.events} ORDER BY seq
            """)).mappings().all()
            return orders, events

    def close(self):
        with self.engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {self.schema} CASCADE"))
