"""事实确认、待确认队列和变化事件的存储辅助。"""

from __future__ import annotations

import json
from typing import Any

from .db import Database, new_id
from .timeutils import canonical_json, iso_now


def record_event(
    db: Database,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    *,
    actor: str | None = None,
    reason: str | None = None,
    before: Any = None,
    after: Any = None,
    run_id: str | None = None,
    related_id: str | None = None,
    commit: bool = False,
) -> str:
    event_id = new_id("E-")
    db.execute(
        """
        INSERT INTO change_events(
            id, occurred_at, aggregate_type, aggregate_id, event_type, actor,
            reason, before_json, after_json, run_id, related_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            iso_now(),
            aggregate_type,
            aggregate_id,
            event_type,
            actor,
            reason,
            None if before is None else canonical_json(before),
            None if after is None else canonical_json(after),
            run_id,
            related_id,
        ),
    )
    if commit:
        db.commit()
    return event_id


def _value_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple, dict)):
        return canonical_json(value)
    return str(value)


def upsert_collected_fact(
    db: Database,
    *,
    subject_type: str,
    subject_id: str,
    field: str,
    value: Any,
    source_id: str | None = None,
    raw_version_id: str | None = None,
    reported_at: str | None = None,
) -> str | None:
    """登记来源提供的事实；若值未变化则不制造待办。

    已有人工确认值仍然是当前有效事实；新来源值会形成一条 pending 事实，等待
    联络员/课程负责人确认。
    """
    if value is None or value == "" or value == []:
        return None
    value_json = canonical_json(value)
    latest = db.query_one(
        """
        SELECT * FROM fact_confirmations
        WHERE subject_type=? AND subject_id=? AND field=?
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (subject_type, subject_id, field),
    )
    if latest and latest["value_json"] == value_json and latest["status"] in {"pending", "confirmed"}:
        return str(latest["id"])

    # 同一字段旧的未确认值被新采集值取代；confirmed 保留为历史和有效依据，
    # 直到人工确认新值。
    db.execute(
        """
        UPDATE fact_confirmations SET status='superseded'
        WHERE subject_type=? AND subject_id=? AND field=? AND status='pending'
        """,
        (subject_type, subject_id, field),
    )
    fact_id = new_id("F-")
    db.execute(
        """
        INSERT INTO fact_confirmations(
            id, subject_type, subject_id, field, value_json, value_text, origin,
            status, source_id, raw_version_id, reported_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'collected', 'pending', ?, ?, ?, ?)
        """,
        (
            fact_id,
            subject_type,
            subject_id,
            field,
            value_json,
            _value_text(value),
            source_id,
            raw_version_id,
            reported_at,
            iso_now(),
        ),
    )
    return fact_id


def add_confirmed_fact(
    db: Database,
    subject_type: str,
    subject_id: str,
    field: str,
    value: Any,
    *,
    confirmer: str,
    origin: str,
    note: str | None = None,
    commit: bool = False,
) -> str:
    if origin not in {"liaison", "owner", "governance", "system"}:
        raise ValueError("无效确认来源")
    value_json = canonical_json(value)

    # 若确认的是某条待确认事实的相同值，把该事实转为 confirmed。
    pending = db.query_one(
        """
        SELECT * FROM fact_confirmations
        WHERE subject_type=? AND subject_id=? AND field=? AND status='pending'
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (subject_type, subject_id, field),
    )
    if pending and pending["value_json"] == value_json:
        fact_id = str(pending["id"])
        db.execute(
            """
            UPDATE fact_confirmations
            SET status='confirmed', confirmer=?, note=COALESCE(?, note), confirmed_at=?
            WHERE id=?
            """,
            (confirmer, note, iso_now(), fact_id),
        )
        db.execute(
            """
            UPDATE fact_confirmations SET status='superseded'
            WHERE subject_type=? AND subject_id=? AND field=?
              AND status='confirmed' AND id <> ?
            """,
            (subject_type, subject_id, field, fact_id),
        )
    else:
        db.execute(
            """
            UPDATE fact_confirmations SET status='superseded'
            WHERE subject_type=? AND subject_id=? AND field=? AND status IN ('pending','confirmed')
            """,
            (subject_type, subject_id, field),
        )
        fact_id = new_id("F-")
        db.execute(
            """
            INSERT INTO fact_confirmations(
                id, subject_type, subject_id, field, value_json, value_text, origin,
                status, confirmer, note, confirmed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?)
            """,
            (
                fact_id,
                subject_type,
                subject_id,
                field,
                value_json,
                _value_text(value),
                origin,
                confirmer,
                note,
                iso_now(),
                iso_now(),
            ),
        )
    if commit:
        db.commit()
    return fact_id


def current_confirmed_fact(db: Database, subject_type: str, subject_id: str, field: str) -> Any | None:
    row = db.query_one(
        """
        SELECT value_json FROM fact_confirmations
        WHERE subject_type=? AND subject_id=? AND field=? AND status='confirmed'
        ORDER BY confirmed_at DESC, created_at DESC, rowid DESC LIMIT 1
        """,
        (subject_type, subject_id, field),
    )
    return json.loads(row["value_json"]) if row and row["value_json"] is not None else None


def latest_pending_fact(db: Database, subject_type: str, subject_id: str, field: str) -> dict[str, Any] | None:
    row = db.query_one(
        """
        SELECT * FROM fact_confirmations
        WHERE subject_type=? AND subject_id=? AND field=? AND status='pending'
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (subject_type, subject_id, field),
    )
    if not row:
        return None
    result = dict(row)
    result["value"] = json.loads(result["value_json"]) if result.get("value_json") is not None else None
    return result


def effective_fact(
    db: Database,
    subject_type: str,
    subject_id: str,
    field: str,
    fallback: Any = None,
) -> dict[str, Any]:
    """返回有效值及其来源；优先人工确认，其次最新来源事实。"""
    confirmed = db.query_one(
        """
        SELECT * FROM fact_confirmations
        WHERE subject_type=? AND subject_id=? AND field=? AND status='confirmed'
        ORDER BY confirmed_at DESC, created_at DESC, rowid DESC LIMIT 1
        """,
        (subject_type, subject_id, field),
    )
    if confirmed:
        row = dict(confirmed)
        row["value"] = json.loads(row["value_json"]) if row.get("value_json") is not None else None
        row["effective_origin"] = "confirmed"
        return row
    pending = latest_pending_fact(db, subject_type, subject_id, field)
    if pending:
        pending["effective_origin"] = "collected_unconfirmed"
        return pending
    return {
        "value": fallback,
        "status": "missing",
        "origin": None,
        "effective_origin": "fallback",
    }


def pending_facts_for(db: Database, subject_type: str, subject_id: str) -> list[dict[str, Any]]:
    rows = db.query(
        """
        SELECT * FROM fact_confirmations
        WHERE subject_type=? AND subject_id=? AND status='pending'
        ORDER BY field, created_at DESC
        """,
        (subject_type, subject_id),
    )
    result = []
    for row in rows:
        item = dict(row)
        item["value"] = json.loads(item["value_json"]) if item.get("value_json") is not None else None
        result.append(item)
    return result
