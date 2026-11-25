"""SQLite 持久化与结构初始化。

数据库既是采集快照库，也是推荐/预留/确认工作库。所有写操作尽量由
:class:`parkbridge.service.ParkService` 在事务中完成，避免容量检查和写入之间
出现半完成状态。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .timeutils import iso_now

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('job', 'course')),
    name TEXT NOT NULL,
    base_url TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running', 'success', 'failed')),
    complete INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    records_seen INTEGER NOT NULL DEFAULT 0,
    next_cursor TEXT,
    next_url TEXT,
    seen_ids_json TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    resumed_from TEXT
);

CREATE TABLE IF NOT EXISTS raw_documents (
    id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES fetch_runs(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    url TEXT NOT NULL,
    page_key TEXT,
    status_code INTEGER,
    content_type TEXT,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_docs_source ON raw_documents(source_id, fetched_at);

CREATE TABLE IF NOT EXISTS record_versions (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('job', 'course')),
    subject_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL DEFAULT '{}',
    source_updated_at TEXT,
    fetched_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id, version_no),
    UNIQUE(source_id, external_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_record_versions_subject ON record_versions(subject_type, subject_id, version_no);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source_id TEXT REFERENCES sources(id),
    external_id TEXT,
    source_url TEXT,
    company TEXT,
    title TEXT,
    location TEXT,
    source_state TEXT,
    source_updated_at TEXT,
    collected_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    current_version_id TEXT REFERENCES record_versions(id),
    content_hash TEXT,
    fingerprint TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'closed', 'withdrawn', 'source_deleted', 'duplicate')),
    duplicate_of TEXT REFERENCES jobs(id),
    last_successful_run_id TEXT,
    deleted_at TEXT,
    UNIQUE(source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS courses (
    id TEXT PRIMARY KEY,
    source_id TEXT REFERENCES sources(id),
    external_id TEXT,
    source_url TEXT,
    provider TEXT,
    title TEXT,
    equipment_json TEXT NOT NULL DEFAULT '[]',
    skills_json TEXT NOT NULL DEFAULT '[]',
    prerequisites_json TEXT NOT NULL DEFAULT '[]',
    seats INTEGER,
    start_date TEXT,
    source_state TEXT,
    source_updated_at TEXT,
    collected_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    current_version_id TEXT REFERENCES record_versions(id),
    content_hash TEXT,
    fingerprint TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'closed', 'source_deleted', 'duplicate', 'cancelled')),
    duplicate_of TEXT REFERENCES courses(id),
    last_successful_run_id TEXT,
    deleted_at TEXT,
    UNIQUE(source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_courses_provider ON courses(provider);
CREATE INDEX IF NOT EXISTS idx_courses_start ON courses(start_date);
CREATE INDEX IF NOT EXISTS idx_courses_status ON courses(status);

CREATE TABLE IF NOT EXISTS job_skill_tags (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    term_id INTEGER,
    PRIMARY KEY(job_id, position)
);

CREATE TABLE IF NOT EXISTS course_tag_sets (
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    tag_kind TEXT NOT NULL CHECK(tag_kind IN ('skill', 'equipment', 'prerequisite')),
    position INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    term_id INTEGER,
    PRIMARY KEY(course_id, tag_kind, position)
);

CREATE TABLE IF NOT EXISTS terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical TEXT NOT NULL UNIQUE,
    normalized TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'skill' CHECK(kind IN ('skill', 'equipment', 'any')),
    created_by TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized TEXT NOT NULL UNIQUE,
    created_by TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aliases_term ON aliases(term_id);

CREATE TABLE IF NOT EXISTS coverage_rules (
    id TEXT PRIMARY KEY,
    requirement_term_id INTEGER NOT NULL REFERENCES terms(id),
    covering_term_id INTEGER NOT NULL REFERENCES terms(id),
    coverage TEXT NOT NULL CHECK(coverage IN ('full', 'partial')),
    rule_kind TEXT NOT NULL DEFAULT 'coverage',
    confirmer TEXT,
    note TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(requirement_term_id, covering_term_id, coverage, rule_kind)
);
CREATE INDEX IF NOT EXISTS idx_rules_requirement ON coverage_rules(requirement_term_id, active);

CREATE TABLE IF NOT EXISTS fact_confirmations (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('job', 'course')),
    subject_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value_json TEXT,
    value_text TEXT,
    origin TEXT NOT NULL CHECK(origin IN ('collected', 'liaison', 'owner', 'governance', 'system')),
    status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'superseded', 'rejected')),
    confirmer TEXT,
    note TEXT,
    source_id TEXT,
    raw_version_id TEXT,
    reported_at TEXT,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    replaces_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_subject_field ON fact_confirmations(subject_type, subject_id, field, status);

CREATE TABLE IF NOT EXISTS change_events (
    id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT,
    reason TEXT,
    before_json TEXT,
    after_json TEXT,
    run_id TEXT,
    related_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_aggregate ON change_events(aggregate_type, aggregate_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_changes_type ON change_events(event_type, occurred_at);

CREATE TABLE IF NOT EXISTS referrals (
    referral TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    course_id TEXT NOT NULL REFERENCES courses(id),
    person TEXT,
    state TEXT NOT NULL CHECK(state IN (
        'proposed', 'held', 'confirmed', 'needs_review', 'mismatch',
        'released', 'cancelled', 'rejected'
    )),
    previous_state TEXT,
    review_status TEXT NOT NULL DEFAULT 'ok' CHECK(review_status IN ('ok', 'required', 'resolved')),
    review_reasons_json TEXT NOT NULL DEFAULT '[]',
    auto_assessment_json TEXT NOT NULL DEFAULT '{}',
    contact_company INTEGER NOT NULL DEFAULT 0,
    contact_learner INTEGER NOT NULL DEFAULT 0,
    seats INTEGER NOT NULL DEFAULT 1 CHECK(seats >= 0),
    seat_held INTEGER NOT NULL DEFAULT 0 CHECK(seat_held IN (0, 1)),
    reason_json TEXT NOT NULL DEFAULT '{}',
    basis_snapshot_json TEXT,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated TEXT
);
CREATE INDEX IF NOT EXISTS idx_referrals_job ON referrals(job_id);
CREATE INDEX IF NOT EXISTS idx_referrals_course ON referrals(course_id);
CREATE INDEX IF NOT EXISTS idx_referrals_person ON referrals(person);
CREATE INDEX IF NOT EXISTS idx_referrals_state ON referrals(state);

CREATE TABLE IF NOT EXISTS confirmed_learner_providers (
    person TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    referral_id TEXT NOT NULL REFERENCES referrals(referral),
    confirmed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS advisor_notes (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    note TEXT NOT NULL,
    author TEXT,
    created_at TEXT NOT NULL
);
"""


def new_id(prefix: str = "") -> str:
    value = uuid.uuid4().hex
    return f"{prefix}{value}" if prefix else value


class Database:
    """SQLite 连接封装。

    测试可使用 ``Database.memory()`` 或 ``Database(":memory:")``；CLI/生产可
    传入文件路径。连接默认开启外键和行字典。
    """

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.init_schema()

    @classmethod
    def memory(cls) -> "Database":
        return cls(":memory:")

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        # 对早期开发中的文件库做最小增量迁移。
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(fetch_runs)")}
        if "next_url" not in columns:
            self.conn.execute("ALTER TABLE fetch_runs ADD COLUMN next_url TEXT")
        if "seen_ids_json" not in columns:
            self.conn.execute("ALTER TABLE fetch_runs ADD COLUMN seen_ids_json TEXT NOT NULL DEFAULT '[]'")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.close()

    def execute(self, sql: str, params: tuple | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple | dict[str, Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def insert(self, table: str, values: dict[str, Any]) -> str:
        if "id" not in values:
            values = {**values, "id": new_id()}
        columns = ", ".join(values)
        placeholders = ", ".join(f":{key}" for key in values)
        self.conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            {key: self._param(value) for key, value in values.items()},
        )
        return str(values["id"])

    def update(self, table: str, values: dict[str, Any], where: str, params: tuple | dict[str, Any]) -> None:
        assignments = ", ".join(f"{key} = :set_{key}" for key in values)
        bound = {f"set_{key}": self._param(value) for key, value in values.items()}
        if isinstance(params, dict):
            bound.update(params)
        else:
            # tuple where parameters are not compatible with named binding; callers
            # in this project mostly use dict. Keep a simple fallback.
            self.conn.execute(
                f"UPDATE {table} SET {assignments} WHERE {where}",
                tuple(self._param(v) for v in values.values()) + tuple(params),
            )
            return
        self.conn.execute(f"UPDATE {table} SET {assignments} WHERE {where}", bound)

    @staticmethod
    def _param(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        if isinstance(value, bool):
            return 1 if value else 0
        return value

    @classmethod
    def _param_dict(cls, values: dict[str, Any]) -> dict[str, Any]:
        return {key: cls._param(value) for key, value in values.items()}

    def now(self) -> str:
        return iso_now()
