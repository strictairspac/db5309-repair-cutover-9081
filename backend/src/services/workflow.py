import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from src.config.settings import cache, engine
from src.constants.workflow import Status
from src.models.tables import audit, cases, evidence, runs
from src.services import domain
from src.utils.encoding import canonical
from src.utils.time import utc_time


def log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action, reference=str(reference), created_at=utc_time()))


def recent(table):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table).order_by(table.c.id.desc()).limit(100)).mappings()]


def found(conn, table, identity):
    row = conn.execute(select(table).where(table.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该记录')
    return dict(row)


def validate_payload(payload):
    try:
        if len(canonical(payload)) > 512 * 1024:
            raise ValueError('业务输入超过 512 KiB')
        if callable(getattr(domain, 'validate', None)):
            domain.validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None


def payload_fingerprint(payload):
    return hashlib.sha256(canonical(payload)).hexdigest()


def mark_stale(conn, case_id, reason):
    # Every conclusion derived from this case's older input loses validity together.
    conn.execute(evidence.update().where(
        evidence.c.run_id.in_(select(runs.c.id).where(runs.c.case_id == case_id)),
        evidence.c.staled_at.is_(None)
    ).values(staled_at=utc_time(), stale_reason=reason))


def make_case(title, payload, actor):
    title = title.strip()
    if not title:
        raise HTTPException(422, '档案名称不能为空')
    validate_payload(payload)
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(slug=uuid.uuid4().hex, title=title, payload=payload,
                               author=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'create_case', case_id)
        return found(conn, cases, case_id)


def update_case(case_id, payload, actor):
    validate_payload(payload)
    with engine.begin() as conn:
        found(conn, cases, case_id)
        conn.execute(cases.update().where(cases.c.id == case_id).values(payload=payload))
        mark_stale(conn, case_id, '源数据更新，旧结论失效')
        log(conn, actor, 'update_case', case_id)
        return found(conn, cases, case_id)


def enqueue(case_id, actor):
    with engine.begin() as conn:
        found(conn, cases, case_id)
        run_id = conn.execute(runs.insert().values(case_id=case_id, status=Status.QUEUED,
                              owner=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'enqueue', run_id)
        result = found(conn, runs, run_id)
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        # The durable database queue is polled even if Redis is temporarily unavailable.
        pass
    return result


def claim():
    ready = or_(runs.c.status == Status.QUEUED,
                and_(runs.c.status == Status.RUNNING, runs.c.started_at < utc_time(-600)))
    with engine.begin() as conn:
        run = conn.execute(select(runs.c.id, runs.c.case_id).where(ready).order_by(runs.c.id)
                           .limit(1).with_for_update(skip_locked=True)).first()
        if not run:
            return None
        token = str(uuid.uuid4())
        conn.execute(runs.update().where(runs.c.id == run.id).values(
            started_at=utc_time(), status=Status.RUNNING, attempt_token=token))
        return run.id, token, found(conn, cases, run.case_id)['payload']


def finish(run_id, token, result=None, error=None):
    digest = hashlib.sha256(canonical(result)).hexdigest() if result is not None else None
    status = Status.FAILED if error else Status.COMPLETE
    with engine.begin() as conn:
        count = conn.execute(runs.update().where(runs.c.id == run_id,
                        runs.c.attempt_token == token, runs.c.status == Status.RUNNING)
                        .values(status=status, finished_at=utc_time(), error=error)).rowcount
        if not count:
            return
        if result is not None:
            conn.execute(evidence.insert().values(
                run_id=run_id, report=result, digest=digest,
                input_fingerprint=result.get('input_fingerprint')))
        log(conn, 'worker', status.value, run_id)


def review(identity, note, actor):
    note = note.strip()
    if not note:
        raise HTTPException(422, '复核意见不能为空')
    with engine.connect() as conn:
        row = found(conn, evidence, identity)
        if row['staled_at'] is not None:
            raise HTTPException(409, '源数据已更新，该证据已失效，请基于新源数据重新演练后再签署')
        case = found(conn, cases, _case_of_run(conn, identity))
        mismatch = payload_fingerprint(case['payload']) != row['input_fingerprint']
    if mismatch:
        # Invalidate in its own transaction so the flag survives the rejected signing request.
        with engine.begin() as conn:
            mark_stale(conn, case['id'], '签署时校验输入指纹不一致，旧结论失效')
        raise HTTPException(409, '当前源数据与该证据的输入指纹不一致，旧结论已失效')
    with engine.begin() as conn:
        count = conn.execute(evidence.update().where(evidence.c.id == identity, evidence.c.reviewed_at.is_(None))
                            .values(review_note=note, reviewer=actor, reviewed_at=utc_time())).rowcount
        if count != 1:
            raise HTTPException(409, '该证据已经复核')
        log(conn, actor, 'review', identity)
        return found(conn, evidence, identity)


def _case_of_run(conn, identity):
    return conn.execute(select(runs.c.case_id).select_from(runs).join(
        evidence, evidence.c.run_id == runs.c.id).where(evidence.c.id == identity)).scalar_one()


def assert_current(identity):
    """Export guard: an acceptance sheet must never carry superseded evidence."""
    with engine.connect() as conn:
        row = found(conn, evidence, identity)
        if row['staled_at'] is not None:
            raise HTTPException(409, '该证据已随源数据更新失效，不能导出为验收单')
        case_id = _case_of_run(conn, identity)
        case = found(conn, cases, case_id)
        if payload_fingerprint(case['payload']) != row['input_fingerprint']:
            raise HTTPException(409, '当前源数据与该证据的输入指纹不一致，导出已拒绝')
        return row
