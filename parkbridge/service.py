"""园区联动核心服务：确认、治理匹配、推荐、席位、变更复核和查询。"""

from __future__ import annotations

import json
from typing import Any, Iterable

from .collectors import AccessPolicy, Collector, HttpFetcher, SourceAdapter
from .db import Database, new_id
from .errors import (
    CapacityExceededError,
    DuplicateLearnerConfirmationError,
    NotFoundError,
    ReviewRequiredError,
    ValidationError,
)
from .facts import (
    add_confirmed_fact,
    effective_fact,
    pending_facts_for,
    record_event,
)
from .ontology import SkillGovernance
from .timeutils import coerce_date, iso_now, normalize_term, period_bounds, split_values


ACTIVE_REFERRAL_STATES = {"proposed", "held", "needs_review", "mismatch"}
OCCUPYING_STATES = {"held", "confirmed", "needs_review"}
TERMINAL_STATES = {"released", "cancelled", "rejected"}


class ParkService:
    def __init__(self, db: Database | str | None = None, *, fetcher: HttpFetcher | None = None):
        if isinstance(db, Database):
            self.db = db
        elif db is None:
            self.db = Database.memory()
        else:
            self.db = Database(db)
        self.governance = SkillGovernance(self.db)
        self.collector = Collector(self.db, fetcher=fetcher, hooks=self)

    @classmethod
    def open(cls, path: str) -> "ParkService":
        return cls(Database(path))

    @classmethod
    def in_memory(cls) -> "ParkService":
        return cls(Database.memory())

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "ParkService":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 通用读取
    # ------------------------------------------------------------------

    @staticmethod
    def _loads(value: Any, default: Any) -> Any:
        if value in (None, ""):
            return default
        if isinstance(value, (list, dict)):
            return value
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

    def _row(self, table: str, entity_id: str, id_column: str = "id") -> dict[str, Any]:
        row = self.db.query_one(f"SELECT * FROM {table} WHERE {id_column}=?", (entity_id,))
        if not row:
            raise NotFoundError(f"{table} 记录不存在: {entity_id}")
        result = dict(row)
        # 显式解析常用 JSON 字段。
        for key in ("equipment", "skills", "prerequisites"):
            json_key = f"{key}_json"
            if json_key in result:
                result[key] = self._loads(result.get(json_key), [])
        if "reason_json" in result:
            result["reason"] = self._loads(result.get("reason_json"), {})
        if "basis_snapshot_json" in result:
            result["basis_snapshot"] = self._loads(result.get("basis_snapshot_json"), None)
        if "review_reasons_json" in result:
            result["review_reasons"] = self._loads(result.get("review_reasons_json"), [])
        if "auto_assessment_json" in result:
            result["auto_assessment"] = self._loads(result.get("auto_assessment_json"), {})
        return result

    def _provenance(self, subject_type: str, row: dict[str, Any]) -> dict[str, Any]:
        version = None
        if row.get("current_version_id"):
            version_row = self.db.query_one(
                "SELECT * FROM record_versions WHERE id=?", (row["current_version_id"],)
            )
            if version_row:
                version = dict(version_row)
                version["raw"] = self._loads(version.pop("raw_json", None), None)
                version["normalized"] = self._loads(version.pop("normalized_json", None), {})
        source = None
        if row.get("source_id"):
            source_row = self.db.query_one("SELECT * FROM sources WHERE id=?", (row["source_id"],))
            source = dict(source_row) if source_row else None
        return {
            "source": source,
            "source_id": row.get("source_id"),
            "source_url": row.get("source_url"),
            "source_link": row.get("source_url"),
            "link": row.get("source_url"),
            "url": row.get("source_url"),
            "source_updated_at": row.get("source_updated_at"),
            "updated_at": row.get("updated_at"),
            "collected_at": row.get("collected_at"),
            "current_version_id": row.get("current_version_id"),
            "raw_version_id": row.get("current_version_id"),
            "raw_version": version,
        }

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self._row("jobs", job_id)
        effective = self.job_effective_values(job_id)
        job["effective"] = effective
        job["source_location"] = job.get("location")
        # 顶层呈现联络员确认后的有效工作地点；来源新值仍在 pending_facts/provenance。
        if effective.get("location") is not None:
            job["location"] = effective["location"]
        job["pending_facts"] = pending_facts_for(self.db, "job", job_id)
        job["provenance"] = self._provenance("job", job)
        # 常用扁平别名，方便脚本和表格导出。
        job["source_link"] = job["source_url"]
        job["raw_version_id"] = job["current_version_id"]
        if job["provenance"]["raw_version"]:
            job["raw_version_no"] = job["provenance"]["raw_version"]["version_no"]
            job["raw_payload"] = job["provenance"]["raw_version"]["raw"]
        return job

    def get_course(self, course_id: str) -> dict[str, Any]:
        course = self._row("courses", course_id)
        course["source_start_date"] = course.get("start_date")
        course["source_seats"] = course.get("seats")
        effective = self.course_effective_values(course_id)
        course["effective"] = effective
        # 顶层字段呈现当前有效事实；来源最新但尚未确认的值仍保留在
        # source_start_date/source_seats、pending_facts 和原始版本中。
        course["start_date"] = effective.get("start_date")
        if effective.get("capacity") is not None:
            course["seats"] = effective.get("capacity")
        course["equipment"] = effective.get("equipment") or []
        course["skills"] = effective.get("covered_skills") or []
        course["prerequisites"] = effective.get("prerequisites") or []
        course["pending_facts"] = pending_facts_for(self.db, "course", course_id)
        occupancy = self.course_occupancy(course_id)
        course.update(occupancy)
        course["provenance"] = self._provenance("course", course)
        course["source_link"] = course["source_url"]
        course["raw_version_id"] = course["current_version_id"]
        if course["provenance"]["raw_version"]:
            course["raw_version_no"] = course["provenance"]["raw_version"]["version_no"]
            course["raw_payload"] = course["provenance"]["raw_version"]["raw"]
        return course

    def list_jobs(self, *, include_inactive: bool = False, company: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id FROM jobs WHERE 1=1"
        params: list[Any] = []
        if not include_inactive:
            sql += " AND status NOT IN ('source_deleted','duplicate')"
        if company:
            sql += " AND company = ?"
            params.append(company)
        sql += " ORDER BY id"
        return [self.get_job(row["id"]) for row in self.db.query(sql, tuple(params))]

    def list_courses(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id FROM courses ORDER BY id")
        result = []
        for row in rows:
            course = self.get_course(row["id"])
            if include_inactive or course["status"] not in {"source_deleted", "duplicate", "cancelled"}:
                result.append(course)
        return result

    def job_effective_values(self, job_id: str) -> dict[str, Any]:
        job = self._row("jobs", job_id)
        skills_fact = effective_fact(self.db, "job", job_id, "required_skills", self._job_tag_skills(job_id))
        hiring_fact = effective_fact(self.db, "job", job_id, "hiring_status", job.get("source_state"))
        location_fact = effective_fact(self.db, "job", job_id, "location", job.get("location"))
        return {
            "required_skills": skills_fact.get("value") or [],
            "skills_origin": skills_fact.get("effective_origin"),
            "hiring_status": hiring_fact.get("value"),
            "hiring_origin": hiring_fact.get("effective_origin"),
            "location": location_fact.get("value"),
            "location_origin": location_fact.get("effective_origin"),
        }

    def _job_tag_skills(self, job_id: str) -> list[str]:
        rows = self.db.query(
            "SELECT raw_text FROM job_skill_tags WHERE job_id=? ORDER BY position",
            (job_id,),
        )
        return [row["raw_text"] for row in rows]

    def course_effective_values(self, course_id: str) -> dict[str, Any]:
        course = self._row("courses", course_id)
        equipment = effective_fact(self.db, "course", course_id, "equipment", course.get("equipment") or [])
        skills = effective_fact(self.db, "course", course_id, "covered_skills", course.get("skills") or [])
        prerequisites = effective_fact(self.db, "course", course_id, "prerequisites", course.get("prerequisites") or [])
        start = effective_fact(self.db, "course", course_id, "start_date", course.get("start_date"))
        capacity = effective_fact(self.db, "course", course_id, "capacity", course.get("seats"))
        return {
            "equipment": equipment.get("value") or [],
            "covered_skills": skills.get("value") or [],
            "prerequisites": prerequisites.get("value") or [],
            "start_date": start.get("value"),
            "capacity": capacity.get("value"),
            "start_origin": start.get("effective_origin"),
            "capacity_origin": capacity.get("effective_origin"),
        }

    # ------------------------------------------------------------------
    # 手工/样例记录写入，也便于测试
    # ------------------------------------------------------------------

    def add_source(self, source_id: str, kind: str, name: str | None = None, **config: Any) -> None:
        self.collector.register_source(source_id, kind, name=name, config=config or AccessPolicy())

    def add_job(
        self,
        job_id: str | None = None,
        *,
        company: str | None = None,
        title: str | None = None,
        skills: Iterable[str] | str | None = None,
        location: str | None = None,
        state: str | None = "open",
        source: str = "manual",
        source_url: str | None = None,
        updated: str | None = None,
        external_id: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        self.collector.register_source(source, "job", name=source, config=AccessPolicy(respect_robots_txt=False))
        jid = job_id or new_id("J-")
        ext = external_id or jid
        url = source_url or f"https://sources.example.test/{source}/jobs/{ext}"
        raw = {
            "id": jid,
            "external_id": ext,
            "company": company,
            "title": title,
            "skills": skills if isinstance(skills, str) else list(skills or []),
            "location": location,
            "state": state,
            "updated": updated,
            "url": url,
            **extra,
        }
        adapter = SourceAdapter(source, "job", url)
        normalized = adapter.normalize_item(raw)
        normalized["id"] = jid
        change = self.collector.upsert_record(adapter, normalized, raw, url, None, "manual-write")
        return self.get_job(change["subject_id"])

    # 常见命名别名。
    create_job = add_job
    upsert_job = add_job

    def add_course(
        self,
        course_id: str | None = None,
        *,
        provider: str | None = None,
        title: str | None = None,
        equipment: Iterable[str] | str | None = None,
        skills: Iterable[str] | str | None = None,
        prerequisites: Iterable[str] | str | str | None = None,
        seats: int | None = None,
        start: str | None = None,
        state: str | None = "open",
        source: str = "manual",
        source_url: str | None = None,
        updated: str | None = None,
        external_id: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        self.collector.register_source(source, "course", name=source, config=AccessPolicy(respect_robots_txt=False))
        cid = course_id or new_id("C-")
        ext = external_id or cid
        url = source_url or f"https://sources.example.test/{source}/courses/{ext}"
        raw = {
            "id": cid,
            "external_id": ext,
            "provider": provider,
            "title": title,
            "equipment": equipment if isinstance(equipment, str) else list(equipment or []),
            "skills": skills if isinstance(skills, str) else list(skills or []),
            "prerequisites": prerequisites if isinstance(prerequisites, str) else list(prerequisites or []),
            "seats": seats,
            "start": start,
            "state": state,
            "updated": updated,
            "url": url,
            **extra,
        }
        adapter = SourceAdapter(source, "course", url)
        normalized = adapter.normalize_item(raw)
        normalized["id"] = cid
        change = self.collector.upsert_record(adapter, normalized, raw, url, None, "manual-write")
        return self.get_course(change["subject_id"])

    create_course = add_course
    upsert_course = add_course

    # ------------------------------------------------------------------
    # 人工确认
    # ------------------------------------------------------------------

    def confirm_job(
        self,
        job_id: str,
        *,
        confirmer: str = "company-liaison",
        is_hiring: bool | None = None,
        hiring_status: str | None = None,
        location: str | None = None,
        required_skills: Iterable[str] | str | None = None,
        skills: Iterable[str] | str | None = None,
        note: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        job = self._row("jobs", job_id)
        old = self._job_basis(job_id)
        actor_name = actor or confirmer

        if is_hiring is False or str(hiring_status or "").lower() in {"closed", "stopped", "false", "withdrawn", "停招"}:
            status_value = "closed" if hiring_status == "closed" else "withdrawn"
            add_confirmed_fact(
                self.db,
                subject_type="job",
                subject_id=job_id,
                field="hiring_status",
                value=status_value,
                confirmer=confirmer,
                origin="liaison",
                note=note,
            )
            self.db.execute(
                "UPDATE jobs SET status=?, source_state='closed', updated_at=? WHERE id=?",
                ("withdrawn", iso_now(), job_id),
            )
            record_event(
                self.db, "job", job_id, "job_withdrawn", actor=actor_name,
                reason=note or "企业联络员确认岗位不再招聘", before={"status": job["status"]},
                after={"status": "withdrawn"},
            )
        elif is_hiring is True or str(hiring_status or "").lower() in {"open", "active", "true", "hiring", "在招"}:
            add_confirmed_fact(
                self.db, "job", job_id, "hiring_status", "open",
                confirmer=confirmer, origin="liaison", note=note,
            )
            self.db.execute(
                "UPDATE jobs SET status='active', source_state='open', deleted_at=NULL, updated_at=? WHERE id=?",
                (iso_now(), job_id),
            )

        if location is not None:
            add_confirmed_fact(
                self.db, "job", job_id, "location", location,
                confirmer=confirmer, origin="liaison", note=note,
            )
            self.db.execute("UPDATE jobs SET location=?, updated_at=? WHERE id=?", (location, iso_now(), job_id))

        skill_value = required_skills if required_skills is not None else skills
        skills_changed = False
        if skill_value is not None:
            skill_list = split_values(skill_value) if isinstance(skill_value, str) else list(skill_value)
            skill_list = [str(item).strip() for item in skill_list if str(item).strip()]
            add_confirmed_fact(
                self.db, "job", job_id, "required_skills", skill_list,
                confirmer=confirmer, origin="liaison", note=note,
            )
            self.db.execute("DELETE FROM job_skill_tags WHERE job_id=?", (job_id,))
            for pos, raw in enumerate(skill_list):
                term = self.governance.resolve(raw)
                self.db.execute(
                    "INSERT INTO job_skill_tags(job_id, position, raw_text, term_id) VALUES (?, ?, ?, ?)",
                    (job_id, pos, raw, term["id"] if term else None),
                )
            new_skills = {normalize_term(s) for s in skill_list}
            old_skills = {normalize_term(s) for s in old.get("required_skills", [])}
            skills_changed = new_skills != old_skills

        record_event(self.db, "job", job_id, "job_confirmed", actor=actor_name, reason=note)
        self.db.commit()

        if is_hiring is False or hiring_status:
            refreshed = self._job_basis(job_id)
            if refreshed.get("hiring_status") in {"withdrawn", "closed"} or is_hiring is False:
                self._propagate_job_change(
                    job_id, "job_withdrawn", old, refreshed, actor=actor_name,
                    company_already_contacted=True,
                )
        if skills_changed:
            new = self._job_basis(job_id)
            record_event(
                self.db, "job", job_id, "job_skills_changed", actor=actor_name,
                reason="企业联络员确认的必须技能发生变化",
                before={"required_skills": old.get("required_skills")},
                after={"required_skills": new.get("required_skills")},
            )
            self._propagate_job_change(job_id, "job_skills_changed", old, new, actor=actor_name)
        self.db.commit()
        return self.get_job(job_id)

    def confirm_course(
        self,
        course_id: str,
        *,
        confirmer: str = "course-owner",
        equipment: Iterable[str] | str | None = None,
        covered_skills: Iterable[str] | str | None = None,
        skills: Iterable[str] | str | None = None,
        prerequisites: Iterable[str] | str | None = None,
        start_date: str | None = None,
        start: str | None = None,
        seats: int | None = None,
        capacity: int | None = None,
        note: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        self._row("courses", course_id)
        old = self._course_basis(course_id)
        actor_name = actor or confirmer
        new_start = coerce_date(start_date if start_date is not None else start)
        start_changed = new_start is not None and normalize_term(new_start) != normalize_term(old.get("start_date"))

        def as_list(value: Any) -> list[str] | None:
            if value is None:
                return None
            return split_values(value) if isinstance(value, str) else [str(v).strip() for v in value if str(v).strip()]

        equipment_list = as_list(equipment)
        skills_list = as_list(covered_skills if covered_skills is not None else skills)
        prereq_list = as_list(prerequisites)
        capacity_value = capacity if capacity is not None else seats

        if equipment_list is not None:
            add_confirmed_fact(self.db, subject_type="course", subject_id=course_id, field="equipment", value=equipment_list, confirmer=confirmer, origin="owner", note=note)
            self.db.execute("UPDATE courses SET equipment_json=?, updated_at=? WHERE id=?", (json.dumps(equipment_list, ensure_ascii=False), iso_now(), course_id))
        if skills_list is not None:
            add_confirmed_fact(self.db, subject_type="course", subject_id=course_id, field="covered_skills", value=skills_list, confirmer=confirmer, origin="owner", note=note)
            self.db.execute("UPDATE courses SET skills_json=?, updated_at=? WHERE id=?", (json.dumps(skills_list, ensure_ascii=False), iso_now(), course_id))
        if prereq_list is not None:
            add_confirmed_fact(self.db, subject_type="course", subject_id=course_id, field="prerequisites", value=prereq_list, confirmer=confirmer, origin="owner", note=note)
            self.db.execute("UPDATE courses SET prerequisites_json=?, updated_at=? WHERE id=?", (json.dumps(prereq_list, ensure_ascii=False), iso_now(), course_id))
        if new_start is not None:
            add_confirmed_fact(self.db, subject_type="course", subject_id=course_id, field="start_date", value=new_start, confirmer=confirmer, origin="owner", note=note)
            self.db.execute("UPDATE courses SET start_date=?, updated_at=? WHERE id=?", (new_start, iso_now(), course_id))
        if capacity_value is not None:
            add_confirmed_fact(self.db, subject_type="course", subject_id=course_id, field="capacity", value=int(capacity_value), confirmer=confirmer, origin="owner", note=note)
            self.db.execute("UPDATE courses SET seats=?, updated_at=? WHERE id=?", (int(capacity_value), iso_now(), course_id))

        self._replace_course_tags(course_id, equipment_list, skills_list, prereq_list)
        record_event(self.db, "course", course_id, "course_confirmed", actor=actor_name, reason=note)
        self.db.commit()

        new_basis = self._course_basis(course_id)
        coverage_changed = any(
            [
                equipment_list is not None and {normalize_term(x) for x in equipment_list} != {normalize_term(x) for x in old.get("equipment", [])},
                skills_list is not None and {normalize_term(x) for x in skills_list} != {normalize_term(x) for x in old.get("covered_skills", [])},
                prereq_list is not None and {normalize_term(x) for x in prereq_list} != {normalize_term(x) for x in old.get("prerequisites", [])},
            ]
        )
        if start_changed:
            record_event(
                self.db, "course", course_id, "course_rescheduled", actor=actor_name,
                reason="课程负责人确认开班时间变化", before={"start_date": old.get("start_date")},
                after={"start_date": new_start},
            )
            self._propagate_course_change(course_id, "course_rescheduled", old, new_basis, actor=actor_name)
        if coverage_changed:
            record_event(
                self.db, "course", course_id, "course_coverage_changed", actor=actor_name,
                reason="课程负责人确认设备或技能覆盖变化", before=old, after=new_basis,
            )
            self._propagate_course_change(course_id, "course_coverage_changed", old, new_basis, actor=actor_name)
        self.db.commit()
        return self.get_course(course_id)

    def _replace_course_tags(self, course_id, equipment, skills, prerequisites) -> None:
        existing = {
            row["tag_kind"]: []
            for row in self.db.query("SELECT DISTINCT tag_kind FROM course_tag_sets WHERE course_id=?", (course_id,))
        }
        # 简单起见，仅当对应列表被确认时重建该类标签。
        for kind, values in (("equipment", equipment), ("skill", skills), ("prerequisite", prerequisites)):
            if values is None:
                continue
            self.db.execute("DELETE FROM course_tag_sets WHERE course_id=? AND tag_kind=?", (course_id, kind))
            for pos, raw in enumerate(values):
                term = self.governance.resolve(raw)
                self.db.execute(
                    "INSERT INTO course_tag_sets(course_id, tag_kind, position, raw_text, term_id) VALUES (?, ?, ?, ?, ?)",
                    (course_id, kind, pos, raw, term["id"] if term else None),
                )

    # ------------------------------------------------------------------
    # 重复发布
    # ------------------------------------------------------------------

    def mark_duplicate(self, entity_id: str, canonical_id: str, *, kind: str = "job", actor: str = "staff") -> dict[str, Any]:
        table = "jobs" if kind == "job" else "courses"
        self._row(table, entity_id)
        self._row(table, canonical_id)
        if entity_id == canonical_id:
            raise ValidationError("不能把记录标记为自身的重复")
        self.db.execute(
            f"UPDATE {table} SET status='duplicate', duplicate_of=?, updated_at=? WHERE id=?",
            (canonical_id, iso_now(), entity_id),
        )
        record_event(self.db, kind, entity_id, "duplicate_confirmed", actor=actor, after={"duplicate_of": canonical_id})
        # 该重复记录上的未确认推荐必须改到主记录或释放，不能继续占名额。
        rows = self.db.query(
            "SELECT referral FROM referrals WHERE job_id=? AND state NOT IN ('released','cancelled','rejected','confirmed')"
            if kind == "job"
            else "SELECT referral FROM referrals WHERE course_id=? AND state NOT IN ('released','cancelled','rejected','confirmed')",
            (entity_id,),
        )
        for row in rows:
            self._mark_review(
                row["referral"],
                [f"duplicate_{kind}"],
                {"duplicate_of": canonical_id},
                contact_company=(kind == "job"),
            )
        self.db.commit()
        return self._row(table, entity_id)

    def find_exact_duplicates(self, kind: str = "job") -> list[list[str]]:
        table = "jobs" if kind == "job" else "courses"
        rows = self.db.query(
            f"SELECT fingerprint, GROUP_CONCAT(id) AS ids FROM {table} "
            "WHERE fingerprint IS NOT NULL AND status != 'duplicate' "
            "GROUP BY fingerprint HAVING COUNT(*) > 1"
        )
        return [str(row["ids"]).split(",") for row in rows]

    def detect_duplicates(self, kind: str | None = None) -> dict[str, list[list[str]]]:
        kinds = [kind] if kind else ["job", "course"]
        return {item: self.find_exact_duplicates(item) for item in kinds}

    find_duplicates = detect_duplicates

    def deduplicate_exact(self, kind: str = "job", *, actor: str = "staff") -> list[dict[str, str]]:
        """把完全相同指纹的后发布记录标记为最早记录的重复发布。"""
        merged = []
        for group in self.find_exact_duplicates(kind):
            canonical = sorted(group)[0]
            for entity_id in group:
                if entity_id == canonical:
                    continue
                self.mark_duplicate(entity_id, canonical, kind=kind, actor=actor)
                merged.append({"duplicate": entity_id, "canonical": canonical, "kind": kind})
        return merged

    deduplicate = deduplicate_exact

    # ------------------------------------------------------------------
    # 推荐和匹配
    # ------------------------------------------------------------------

    def _job_basis(self, job_id: str) -> dict[str, Any]:
        job = self._row("jobs", job_id)
        effective = self.job_effective_values(job_id)
        return {
            "job_id": job_id,
            "company": job.get("company"),
            "title": job.get("title"),
            "location": effective["location"],
            "hiring_status": effective["hiring_status"] or job.get("status"),
            "job_status": job.get("status"),
            "required_skills": effective["required_skills"],
            "source_url": job.get("source_url"),
            "source_updated_at": job.get("source_updated_at"),
            "current_version_id": job.get("current_version_id"),
            "effective": effective,
        }

    def _course_basis(self, course_id: str) -> dict[str, Any]:
        course = self._row("courses", course_id)
        effective = self.course_effective_values(course_id)
        return {
            "course_id": course_id,
            "provider": course.get("provider"),
            "title": course.get("title"),
            "equipment": effective["equipment"],
            "covered_skills": effective["covered_skills"],
            "prerequisites": effective["prerequisites"],
            "start_date": effective["start_date"],
            "capacity": effective["capacity"],
            "source_url": course.get("source_url"),
            "source_updated_at": course.get("source_updated_at"),
            "current_version_id": course.get("current_version_id"),
            "effective": effective,
        }

    def recommend_for_job(
        self,
        job_id: str,
        *,
        course_ids: Iterable[str] | None = None,
        start_from: str | None = None,
        start_to: str | None = None,
        strict: bool = False,
    ) -> dict[str, Any]:
        job = self.get_job(job_id)
        basis = self._job_basis(job_id)
        eligible, eligibility_reason = self._job_recommendation_eligibility(job)
        candidates = self.list_courses()
        if course_ids is not None:
            wanted = {str(c) for c in course_ids}
            candidates = [c for c in candidates if c["id"] in wanted]
        start_from = coerce_date(start_from)
        start_to = coerce_date(start_to)
        if start_from:
            candidates = [c for c in candidates if c.get("start_date") and c["start_date"] >= start_from]
        if start_to:
            candidates = [c for c in candidates if c.get("start_date") and c["start_date"] <= start_to]

        recommendations = []
        for course in candidates:
            course_basis = self._course_basis(course["id"])
            match = self.governance.match_course(
                basis["required_skills"],
                course_basis["covered_skills"],
                course_basis["equipment"],
            )
            occupancy = self.course_occupancy(course["id"])
            item = {
                "job_id": job_id,
                "course_id": course["id"],
                "provider": course.get("provider"),
                "start_date": course_basis["start_date"],
                "capacity": occupancy["capacity"],
                "occupied_seats": occupancy["occupied_seats"],
                "free_seats": occupancy["free_seats"],
                "capacity_known": occupancy["capacity_known"],
                "match": match,
                "can_satisfy": match["satisfies"],
                "has_partial_coverage": match["partial_count"] > 0,
                "pending_facts": {
                    "job": pending_facts_for(self.db, "job", job_id),
                    "course": pending_facts_for(self.db, "course", course["id"]),
                },
            }
            recommendations.append(item)

        recommendations.sort(
            key=lambda item: (
                not item["can_satisfy"],
                -item["match"]["covered_count"],
                item["match"]["partial_count"],
                item["free_seats"] if item["free_seats"] is not None else -10**9,
                item["start_date"] or "9999",
            )
        )
        if strict:
            recommendations = [item for item in recommendations if item["can_satisfy"]]
        return {
            "job_id": job_id,
            "eligible": eligible,
            "eligibility_reason": eligibility_reason,
            "basis": basis,
            "recommendations": recommendations,
        }

    @staticmethod
    def _job_recommendation_eligibility(job: dict[str, Any]) -> tuple[bool, str | None]:
        if job["status"] == "withdrawn":
            return False, "岗位已经企业联络员确认撤回/停招"
        if job["status"] == "source_deleted":
            return False, "岗位已从完整来源目录消失，需企业确认后才能作为有效岗位"
        if job["status"] == "duplicate":
            return False, "该记录是重复发布，请使用 canonical job"
        effective_status = str(job.get("effective", {}).get("hiring_status") or job.get("source_state") or "").lower()
        if effective_status in {"closed", "stopped", "filled", "withdrawn", "停招", "已停招"}:
            return False, "岗位当前状态为停招/关闭"
        return True, None

    def recommend(
        self,
        job_id: str,
        course_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """推荐入口别名；提供 ``course_id`` 时只评估该课程。"""
        course_ids = [course_id] if course_id else kwargs.pop("course_ids", None)
        return self.recommend_for_job(job_id, course_ids=course_ids, **kwargs)

    def create_recommendation(
        self,
        job_id: str,
        course_id: str,
        *,
        person: str | None = None,
        referral_id: str | None = None,
        actor: str = "advisor",
        note: str | None = None,
        allow_partial: bool = True,
        override_unverified: bool = False,
        override_reason: str | None = None,
    ) -> dict[str, Any]:
        job = self.get_job(job_id)
        course = self.get_course(course_id)
        eligible, reason = self._job_recommendation_eligibility(job)
        if not eligible:
            raise ValidationError(reason)
        if course["status"] in {"source_deleted", "duplicate", "cancelled"}:
            raise ValidationError(f"课程 {course_id} 当前状态 {course['status']}，不能推荐")

        basis = self._job_basis(job_id)
        course_basis = self._course_basis(course_id)
        match = self.governance.match_course(
            basis["required_skills"], course_basis["covered_skills"], course_basis["equipment"]
        )
        if not match["satisfies"]:
            if match["partial_count"] and allow_partial:
                pass
            elif override_unverified and override_reason:
                match["advisor_override"] = override_reason
            else:
                raise ValidationError(
                    "没有足够的已确认完整覆盖依据；文本相似不能判定满足。"
                    "部分覆盖需登记为治理规则，人工特批需 override_unverified 和 override_reason。"
                )

        rid = referral_id or new_id("R-")
        now = iso_now()
        coverage_warnings = []
        if not match["satisfies"]:
            coverage_warnings.append("coverage_not_full")
        reason_payload = {
            "recommended_by": actor,
            "note": note,
            "coverage_warnings": coverage_warnings,
            "match": match,
            "job_basis": basis,
            "course_basis": course_basis,
        }
        if match.get("advisor_override"):
            reason_payload["advisor_override"] = match["advisor_override"]
        self.db.execute(
            """
            INSERT INTO referrals(
                referral, job_id, course_id, person, state, review_status,
                review_reasons_json, reason_json, seats, seat_held,
                created_at, updated_at, updated
            ) VALUES (?, ?, ?, ?, 'proposed', 'ok', '[]', ?, 1, 0, ?, ?, ?)
            """,
            (
                rid,
                job_id,
                course_id,
                person,
                json.dumps(reason_payload, ensure_ascii=False),
                now,
                now,
                now[:10],
            ),
        )
        record_event(
            self.db, "referral", rid, "recommendation_created", actor=actor,
            related_id=job_id, reason=note,
            after={"job_id": job_id, "course_id": course_id, "person": person, "match": match},
        )
        self.db.commit()
        return self.get_referral(rid)

    # ------------------------------------------------------------------
    # 容量、预留和确认
    # ------------------------------------------------------------------

    def course_occupancy(self, course_id: str) -> dict[str, Any]:
        if not self.db.query_one("SELECT id FROM courses WHERE id=?", (course_id,)):
            raise NotFoundError(course_id)
        # 若课程负责人确认过容量，则确认值优先；否则使用目录来源的最新容量。
        capacity = self.course_effective_values(course_id).get("capacity")
        row = self.db.query_one(
            "SELECT COALESCE(SUM(seats), 0) AS occupied FROM referrals WHERE course_id=? AND seat_held=1",
            (course_id,),
        )
        occupied = int(row["occupied"])
        capacity_known = capacity is not None
        free = int(capacity) - occupied if capacity_known else None
        return {
            "capacity": capacity,
            "occupied_seats": occupied,
            "free_seats": free,
            "capacity_known": capacity_known,
        }

    def _assert_capacity(self, course_id: str, seats: int, exclude_referral: str | None = None) -> None:
        if seats < 0:
            raise ValidationError("席位数不能为负")
        if not self.db.query_one("SELECT id FROM courses WHERE id=?", (course_id,)):
            raise NotFoundError(course_id)
        capacity = self.course_effective_values(course_id).get("capacity")
        if capacity is None:
            return
        params: list[Any] = [course_id]
        sql = "SELECT COALESCE(SUM(seats), 0) AS occupied FROM referrals WHERE course_id=? AND seat_held=1"
        if exclude_referral:
            sql += " AND referral <> ?"
            params.append(exclude_referral)
        occupied = int(self.db.query_one(sql, tuple(params))["occupied"])
        available = int(capacity) - occupied
        if seats > available:
            raise CapacityExceededError(course_id, seats, available)

    def hold_reservation(
        self,
        referral_id: str,
        *,
        seats: int | None = None,
        actor: str = "advisor",
        note: str | None = None,
    ) -> dict[str, Any]:
        row = self._referral_row(referral_id)
        if row["state"] == "held" and seats is None:
            return self.get_referral(referral_id)
        if row["state"] == "confirmed":
            raise ValidationError("已确认报名不能改为待确认预留；如需调整请先按机构流程取消")
        if row["state"] in TERMINAL_STATES:
            raise ValidationError(f"推荐已为终态 {row['state']}，不能预留")
        if row["state"] == "needs_review" and row["review_status"] == "required":
            raise ReviewRequiredError("推荐因来源变化待重新核对，核对完成前不能新增/调整预留")
        requested = int(seats if seats is not None else row["seats"] or 1)
        self._assert_capacity(row["course_id"], requested, referral_id)
        self.db.execute(
            """
            UPDATE referrals SET state='held', previous_state=NULL, seats=?, seat_held=1,
                                 updated_at=?, updated=? WHERE referral=?
            """,
            (requested, iso_now(), iso_now()[:10], referral_id),
        )
        record_event(self.db, "referral", referral_id, "reservation_held", actor=actor, reason=note, after={"seats": requested})
        self.db.commit()
        return self.get_referral(referral_id)

    def reserve_seat(
        self,
        *,
        referral_id: str | None = None,
        job_id: str | None = None,
        course_id: str | None = None,
        person: str | None = None,
        seats: int = 1,
        actor: str = "advisor",
        **kwargs: Any,
    ) -> dict[str, Any]:
        if referral_id is not None:
            existing = self.db.query_one("SELECT referral FROM referrals WHERE referral=?", (referral_id,))
            if existing:
                result = self.hold_reservation(referral_id, seats=seats, actor=actor)
                if person and not result.get("person"):
                    self.db.execute("UPDATE referrals SET person=? WHERE referral=?", (person, referral_id))
                    self.db.commit()
                    result = self.get_referral(referral_id)
                return result
            if not job_id or not course_id:
                raise NotFoundError(f"推荐不存在: {referral_id}")
            referral = self.create_recommendation(
                job_id, course_id, person=person, referral_id=referral_id, actor=actor, **kwargs
            )
            return self.hold_reservation(referral["referral"], seats=seats, actor=actor)

        if not job_id or not course_id:
            raise ValidationError("必须提供 referral_id，或同时提供 job_id/course_id")
        referral = self.create_recommendation(job_id, course_id, person=person, actor=actor, **kwargs)
        referral_id = referral["referral"]
        return self.hold_reservation(referral_id, seats=seats, actor=actor)

    def confirm_referral(
        self,
        referral_id: str,
        *,
        person: str | None = None,
        actor: str = "advisor",
        note: str | None = None,
        allow_review_override: bool = False,
        override_reason: str | None = None,
    ) -> dict[str, Any]:
        row = self._referral_row(referral_id)
        if row["state"] == "confirmed":
            if person and row["person"] and person != row["person"]:
                raise ValidationError("已确认推荐的学员不能被替换")
            return self.get_referral(referral_id)
        if row["state"] in TERMINAL_STATES:
            raise ValidationError(f"推荐状态为 {row['state']}，不能确认")
        if row["review_status"] == "required" and not allow_review_override:
            reasons = self._loads(row["review_reasons_json"], [])
            raise ReviewRequiredError(
                f"推荐 {referral_id} 因 {', '.join(reasons)} 待重新核对"
            )
        if row["review_status"] == "required" and allow_review_override and not override_reason:
            raise ValidationError("跳过待核对状态必须提供 override_reason")

        person_value = person or row["person"]
        if not person_value:
            raise ValidationError("确认前必须提供学员标识 person")
        if person and row["person"] and person != row["person"]:
            raise ValidationError(
                f"推荐 {referral_id} 已登记学员 {row['person']}，不能改用 {person} 确认"
            )

        course = self._row("courses", row["course_id"])
        provider = course.get("provider") or row["course_id"]

        # 立即事务检查学员唯一机构选择和容量。
        self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            if person and not row["person"]:
                self.db.execute("UPDATE referrals SET person=? WHERE referral=?", (person, referral_id))
            existing = self.db.query_one(
                "SELECT * FROM confirmed_learner_providers WHERE person=?",
                (person_value,),
            )
            if existing and existing["provider"] != provider:
                raise DuplicateLearnerConfirmationError(person_value, existing["provider"], provider)

            requested = int(row["seats"] or 1)
            self._assert_capacity(row["course_id"], requested, referral_id)

            snapshot = {
                "confirmed_at": iso_now(),
                "job_basis": self._job_basis(row["job_id"]),
                "course_basis": self._course_basis(row["course_id"]),
                "original_reason": self._loads(row["reason_json"], {}),
                "review_override": override_reason,
                "policy": "已确认报名保留确认时的岗位要求、课程覆盖、开班期和治理规则依据",
            }
            now = iso_now()
            self.db.execute(
                """
                UPDATE referrals
                SET state='confirmed', seat_held=1, confirmed_at=?, basis_snapshot_json=?,
                    review_status=?, updated_at=?, updated=?
                WHERE referral=?
                """,
                (
                    now,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    "resolved" if allow_review_override else row["review_status"],
                    now,
                    now[:10],
                    referral_id,
                ),
            )
            self.db.execute(
                """
                INSERT INTO confirmed_learner_providers(person, provider, referral_id, confirmed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(person) DO UPDATE SET provider=excluded.provider
                WHERE confirmed_learner_providers.provider = excluded.provider
                """,
                (person_value, provider, referral_id, now),
            )
            record_event(
                self.db, "referral", referral_id, "referral_confirmed", actor=actor,
                reason=note or override_reason,
                after={"person": person_value, "provider": provider, "snapshot": snapshot},
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.get_referral(referral_id)

    def release_referral(
        self,
        referral_id: str,
        *,
        actor: str = "advisor",
        reason: str = "顾问或机构释放席位",
        new_state: str = "released",
    ) -> dict[str, Any]:
        row = self._referral_row(referral_id)
        if row["state"] in TERMINAL_STATES:
            return self.get_referral(referral_id)
        now = iso_now()
        self.db.execute(
            "UPDATE referrals SET state=?, seat_held=0, updated_at=?, updated=? WHERE referral=?",
            (new_state, now, now[:10], referral_id),
        )
        record_event(self.db, "referral", referral_id, "referral_released", actor=actor, reason=reason)
        self.db.commit()
        self._reconcile_learner_provider(row.get("person"))
        return self.get_referral(referral_id)

    def cancel_referral(
        self,
        referral_id: str,
        *,
        actor: str = "advisor",
        reason: str = "顾问取消推荐",
    ) -> dict[str, Any]:
        return self.release_referral(
            referral_id, actor=actor, reason=reason, new_state="cancelled"
        )

    def _reconcile_learner_provider(self, person: str | None) -> None:
        if not person:
            return
        row = self.db.query_one(
            """
            SELECT provider FROM referrals r JOIN courses c ON c.id=r.course_id
            WHERE r.person=? AND r.state='confirmed'
            ORDER BY r.confirmed_at DESC LIMIT 1
            """,
            (person,),
        )
        if row:
            self.db.execute(
                "UPDATE confirmed_learner_providers SET provider=? WHERE person=?",
                (row["provider"], person),
            )
        else:
            self.db.execute("DELETE FROM confirmed_learner_providers WHERE person=?", (person,))
        self.db.commit()

    def _referral_row(self, referral_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM referrals WHERE referral=?", (referral_id,))
        if not row:
            raise NotFoundError(f"推荐不存在: {referral_id}")
        return dict(row)

    def get_referral(self, referral_id: str) -> dict[str, Any]:
        row = self._row("referrals", referral_id, id_column="referral")
        job = self.get_job(row["job_id"])
        course = self.get_course(row["course_id"])
        row["job"] = {
            "id": job["id"],
            "company": job.get("company"),
            "title": job.get("title"),
            "status": job.get("status"),
            "source_url": job.get("source_url"),
            "effective": job.get("effective"),
        }
        row["course"] = {
            "id": course["id"],
            "provider": course.get("provider"),
            "title": course.get("title"),
            "start_date": course.get("start_date"),
            "source_url": course.get("source_url"),
            "free_seats": course.get("free_seats"),
            "capacity": course.get("capacity"),
            "occupied_seats": course.get("occupied_seats"),
            "effective": course.get("effective"),
        }
        row["free_seats"] = course.get("free_seats")
        row["pending_facts"] = {
            "job": pending_facts_for(self.db, "job", row["job_id"]),
            "course": pending_facts_for(self.db, "course", row["course_id"]),
        }
        row["needs_recheck"] = row.get("review_status") == "required" or row.get("state") == "needs_review"
        row["needs_contact"] = bool(row.get("contact_company") or row.get("contact_learner"))
        contact_party = []
        if row.get("contact_company"):
            contact_party.append("company")
        if row.get("contact_learner"):
            contact_party.append("learner")
        row["contact_party"] = contact_party
        row["history"] = self.history_for("referral", referral_id)
        return row

    def reserve(self, referral_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.reserve_seat(referral_id=referral_id, **kwargs)

    def confirm(self, referral_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.confirm_referral(referral_id, **kwargs)

    # ------------------------------------------------------------------
    # 变更传播和重新核对
    # ------------------------------------------------------------------

    def after_record_change(self, change: dict[str, Any]) -> None:
        kind = change.get("subject_type")
        subject_id = change.get("subject_id")
        change_kind = change.get("change_kind")
        if not kind or not subject_id or change_kind == "unchanged":
            return
        if kind == "job":
            if change_kind in {"deleted"}:
                old = change.get("before") or {}
                new = self._job_basis(subject_id)
                self._propagate_job_change(subject_id, "job_source_deleted", old, new, actor="collector")
            elif change_kind in {"created", "restored"}:
                return
            elif change_kind == "edited":
                self._propagate_source_job_edit(change)
        elif kind == "course" and change_kind == "edited":
            self._propagate_source_course_edit(change)
        elif kind == "course" and change_kind == "deleted":
            self._propagate_course_change(subject_id, "course_source_deleted", {}, self._course_basis(subject_id), actor="collector")

    def _previous_normalized(self, version_id: Any) -> dict[str, Any] | None:
        if not version_id:
            return None
        # version_id 可能是旧 jobs.current_version_id。
        row = self.db.query_one("SELECT normalized_json FROM record_versions WHERE id=?", (version_id,))
        if row:
            return self._loads(row["normalized_json"], {})
        return None

    def _propagate_source_job_edit(self, change: dict[str, Any]) -> None:
        job_id = change["subject_id"]
        before_row = change.get("before") or {}
        old_norm = self._previous_normalized(before_row.get("current_version_id")) or {}
        new_norm = change.get("normalized") or {}
        old_skills = {normalize_term(s) for s in split_values(old_norm.get("skills"))}
        new_skills = {normalize_term(s) for s in split_values(new_norm.get("skills"))}
        old_state = Collector._normalize_state(old_norm.get("state"))
        new_state = Collector._normalize_state(new_norm.get("state"))
        old = {"required_skills": split_values(old_norm.get("skills")), "hiring_status": old_state}
        new = {"required_skills": split_values(new_norm.get("skills")), "hiring_status": new_state}
        if old_skills != new_skills:
            record_event(
                self.db, "job", job_id, "job_skills_reported_changed", actor="collector",
                reason="来源报告必须技能变化，尚待企业联络员确认", before=old, after=new,
                run_id=change.get("run_id"),
            )
            self._propagate_job_change(job_id, "job_skills_changed", old, new, actor="collector")
        if new_state == "closed" and old_state != "closed":
            self._propagate_job_change(job_id, "job_closed_reported", old, new, actor="collector", company_already_contacted=False)

    def _propagate_source_course_edit(self, change: dict[str, Any]) -> None:
        course_id = change["subject_id"]
        before_row = change.get("before") or {}
        old_norm = self._previous_normalized(before_row.get("current_version_id")) or {}
        new_norm = change.get("normalized") or {}
        old_start = coerce_date(old_norm.get("start"))
        new_start = coerce_date(new_norm.get("start"))
        old_skills = {
            normalize_term(s)
            for s in split_values(old_norm.get("skills")) + split_values(old_norm.get("equipment"))
        }
        new_skills = {
            normalize_term(s)
            for s in split_values(new_norm.get("skills")) + split_values(new_norm.get("equipment"))
        }
        old = {"start_date": old_start, "covered_skills": split_values(old_norm.get("skills")), "equipment": split_values(old_norm.get("equipment"))}
        new = {"start_date": new_start, "covered_skills": split_values(new_norm.get("skills")), "equipment": split_values(new_norm.get("equipment"))}
        if old_start != new_start and new_start:
            record_event(self.db, "course", course_id, "course_rescheduled_reported", actor="collector", before=old, after=new)
            self._propagate_course_change(course_id, "course_rescheduled", old, new, actor="collector")
        if old_skills != new_skills:
            self._propagate_course_change(course_id, "course_coverage_changed", old, new, actor="collector")

    def _mark_review(
        self,
        referral_id: str,
        reasons: list[str],
        assessment: dict[str, Any],
        *,
        contact_company: bool | None = None,
        contact_learner: bool | None = None,
    ) -> None:
        row = self._referral_row(referral_id)
        merged = list(dict.fromkeys(list(self._loads(row["review_reasons_json"], [])) + reasons))
        previous = row["state"] if row["state"] != "needs_review" else row.get("previous_state") or "proposed"
        sets = {
            "state": "needs_review",
            "previous_state": previous,
            "review_status": "required",
            "review_reasons_json": json.dumps(merged, ensure_ascii=False),
            "auto_assessment_json": json.dumps(assessment, ensure_ascii=False, sort_keys=True),
            "updated_at": iso_now(),
            "updated": iso_now()[:10],
        }
        if contact_company is not None:
            sets["contact_company"] = 1 if contact_company else row["contact_company"]
        if contact_learner is not None:
            sets["contact_learner"] = 1 if contact_learner else row["contact_learner"]
        assignments = ", ".join(f"{k}=?" for k in sets)
        self.db.execute(f"UPDATE referrals SET {assignments} WHERE referral=?", tuple(sets.values()) + (referral_id,))

    def _propagate_job_change(
        self,
        job_id: str,
        change_type: str,
        old: dict[str, Any],
        new: dict[str, Any],
        *,
        actor: str = "system",
        company_already_contacted: bool = False,
    ) -> None:
        rows = self.db.query("SELECT referral FROM referrals WHERE job_id=? ORDER BY referral", (job_id,))
        for row in rows:
            rid = row["referral"]
            current = self._referral_row(rid)
            if current["state"] in TERMINAL_STATES:
                continue
            course_basis = self._course_basis(current["course_id"])
            assessment: dict[str, Any] = {"change_type": change_type, "rechecked_at": iso_now()}
            if change_type in {"job_withdrawn", "job_source_deleted", "job_closed_reported"}:
                assessment["job_available"] = False
                reasons = [change_type]
                if current["state"] == "confirmed":
                    # 已确认报名不改状态、不替换快照，只提示顾问跟进。
                    self.db.execute(
                        "UPDATE referrals SET contact_learner=1, contact_company=?, updated_at=? WHERE referral=?",
                        (0 if company_already_contacted else 1, iso_now(), rid),
                    )
                else:
                    self._mark_review(
                        rid,
                        reasons,
                        assessment,
                        contact_company=not company_already_contacted,
                        contact_learner=False,
                    )
            elif change_type == "job_skills_changed":
                match = self.governance.match_course(
                    new.get("required_skills", []),
                    course_basis["covered_skills"],
                    course_basis["equipment"],
                )
                assessment["match"] = match
                assessment["still_satisfied"] = match["satisfies"]
                assessment["has_partial"] = match["partial_count"] > 0
                if current["state"] == "confirmed":
                    material = not match["satisfies"]
                    self.db.execute(
                        "UPDATE referrals SET contact_company=?, contact_learner=?, updated_at=? WHERE referral=?",
                        (1 if material else current["contact_company"], 1 if material else current["contact_learner"], iso_now(), rid),
                    )
                else:
                    self._mark_review(rid, ["job_skills_changed"], assessment)
            record_event(
                self.db, "referral", rid, "referral_change_propagated", actor=actor,
                reason=change_type, before=old, after=new, related_id=job_id,
            )
        self.db.commit()

    def _propagate_course_change(
        self,
        course_id: str,
        change_type: str,
        old: dict[str, Any],
        new: dict[str, Any],
        *,
        actor: str = "system",
    ) -> None:
        rows = self.db.query("SELECT referral FROM referrals WHERE course_id=? ORDER BY referral", (course_id,))
        for row in rows:
            rid = row["referral"]
            current = self._referral_row(rid)
            if current["state"] in TERMINAL_STATES:
                continue
            job_basis = self._job_basis(current["job_id"])
            assessment = {"change_type": change_type, "rechecked_at": iso_now()}
            if change_type == "course_rescheduled":
                assessment["start_date"] = new.get("start_date")
                if current["state"] == "confirmed":
                    self.db.execute(
                        "UPDATE referrals SET contact_learner=1, updated_at=? WHERE referral=?",
                        (iso_now(), rid),
                    )
                else:
                    self._mark_review(rid, ["course_rescheduled"], assessment, contact_learner=False)
            elif change_type in {"course_coverage_changed", "course_source_deleted"}:
                match = self.governance.match_course(
                    job_basis["required_skills"], new.get("covered_skills", []), new.get("equipment", [])
                )
                assessment["match"] = match
                assessment["still_satisfied"] = match["satisfies"]
                if current["state"] != "confirmed":
                    self._mark_review(rid, [change_type], assessment)
                elif change_type == "course_source_deleted" or not match["satisfies"]:
                    # 课程来源消失，或变化后不再覆盖旧岗位要求，已确认学员也需要顾问联系。
                    self.db.execute(
                        "UPDATE referrals SET contact_learner=1, updated_at=? WHERE referral=?",
                        (iso_now(), rid),
                    )
            record_event(
                self.db, "referral", rid, "referral_change_propagated", actor=actor,
                reason=change_type, before=old, after=new, related_id=course_id,
            )
        self.db.commit()

    def recheck_referral(self, referral_id: str) -> dict[str, Any]:
        row = self._referral_row(referral_id)
        job = self._job_basis(row["job_id"])
        course = self._course_basis(row["course_id"])
        match = self.governance.match_course(job["required_skills"], course["covered_skills"], course["equipment"])
        occupancy = self.course_occupancy(row["course_id"])
        assessment = {
            "rechecked_at": iso_now(),
            "job_available": job["job_status"] not in {"withdrawn", "source_deleted", "duplicate"}
            and str(job.get("hiring_status") or "").lower() not in {"closed", "withdrawn"},
            "match": match,
            "occupancy": occupancy,
            "start_date": course["start_date"],
        }
        self.db.execute(
            "UPDATE referrals SET auto_assessment_json=?, updated_at=? WHERE referral=?",
            (json.dumps(assessment, ensure_ascii=False, sort_keys=True), iso_now(), referral_id),
        )
        record_event(self.db, "referral", referral_id, "referral_rechecked", actor="advisor", after=assessment)
        self.db.commit()
        return self.get_referral(referral_id)

    def resolve_review(
        self,
        referral_id: str,
        *,
        action: str = "keep",
        actor: str = "advisor",
        note: str | None = None,
    ) -> dict[str, Any]:
        row = self._referral_row(referral_id)
        if row["review_status"] != "required":
            return self.get_referral(referral_id)
        if action in {"release", "cancel"}:
            return self.release_referral(referral_id, actor=actor, reason=note or "重新核对后释放")
        if action not in {"keep", "resolve", "confirm"}:
            raise ValidationError("action 必须是 keep、confirm 或 release")
        restore_state = row.get("previous_state") or "proposed"
        if restore_state not in {"proposed", "held"}:
            restore_state = "proposed"
        self.db.execute(
            """
            UPDATE referrals SET state=?, review_status='resolved', review_reasons_json='[]',
                                 contact_company=0, contact_learner=0, updated_at=?, updated=?
            WHERE referral=?
            """,
            (restore_state, iso_now(), iso_now()[:10], referral_id),
        )
        record_event(self.db, "referral", referral_id, "review_resolved", actor=actor, reason=note or "顾问重新核对后保留")
        self.db.commit()
        if action == "confirm":
            return self.confirm_referral(referral_id, actor=actor, note=note)
        return self.get_referral(referral_id)

    # ------------------------------------------------------------------
    # 历史和顾问查询
    # ------------------------------------------------------------------

    def history_for(self, aggregate_type: str, aggregate_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM change_events WHERE aggregate_type=? AND aggregate_id=? ORDER BY occurred_at, rowid",
            (aggregate_type, aggregate_id),
        )
        result = []
        for row in rows:
            item = dict(row)
            item["before"] = self._loads(item.pop("before_json", None), None)
            item["after"] = self._loads(item.pop("after_json", None), None)
            result.append(item)
        return result

    def record_versions(self, subject_type: str, subject_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT * FROM record_versions
            WHERE subject_type=? AND subject_id=?
            ORDER BY version_no, rowid
            """,
            (subject_type, subject_id),
        )
        result = []
        for row in rows:
            item = dict(row)
            item["raw"] = self._loads(item.pop("raw_json", None), None)
            item["normalized"] = self._loads(item.pop("normalized_json", None), {})
            result.append(item)
        return result

    def provenance(self, subject_type: str, subject_id: str) -> dict[str, Any]:
        table = "jobs" if subject_type == "job" else "courses"
        row = self._row(table, subject_id)
        return self._provenance(subject_type, row)

    def query_recommendations(
        self,
        *,
        company: str | None = None,
        skill: str | None = None,
        start_from: str | None = None,
        start_to: str | None = None,
        start_period: str | None = None,
        person: str | None = None,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        period_from, period_to = period_bounds(start_period)
        start_from = start_from or period_from
        start_to = start_to or period_to
        sql = """
            SELECT r.referral
            FROM referrals r
            JOIN jobs j ON j.id = r.job_id
            JOIN courses c ON c.id = r.course_id
            WHERE 1=1
        """
        params: list[Any] = []
        if company:
            sql += " AND j.company = ?"
            params.append(company)
        if person:
            sql += " AND r.person = ?"
            params.append(person)
        if state:
            sql += " AND r.state = ?"
            params.append(state)
        # 开班期在读取后按“有效开班日期”过滤：若课程负责人已确认改期，
        # 不能继续用来源表中等待确认的旧/新列值误导顾问。
        sql += " ORDER BY r.referral"
        ids = [row["referral"] for row in self.db.query(sql, tuple(params))]
        results = [self.get_referral(rid) for rid in ids]
        if start_from:
            start_from_date = coerce_date(start_from)
            results = [
                r for r in results
                if r.get("course", {}).get("start_date")
                and r["course"]["start_date"] >= (start_from_date or "")
            ]
        if start_to:
            start_to_date = coerce_date(start_to)
            results = [
                r for r in results
                if r.get("course", {}).get("start_date")
                and r["course"]["start_date"] <= (start_to_date or "9999")
            ]
        if skill:
            term = self.governance.resolve(skill)
            wanted_norm = normalize_term(skill)
            term_id = term["id"] if term else None

            def has_skill(item: dict[str, Any]) -> bool:
                job = item["job"]
                skills = (job.get("effective") or {}).get("required_skills") or []
                for raw in skills:
                    if normalize_term(raw) == wanted_norm:
                        return True
                    resolved = self.governance.resolve(raw)
                    if term_id and resolved and int(resolved["id"]) == int(term_id):
                        return True
                return False

            results = [item for item in results if has_skill(item)]
        return results

    def advisor_report(self, **filters: Any) -> dict[str, Any]:
        referrals = self.query_recommendations(**filters)
        pending = {
            "jobs": [dict(r) for r in self.db.query("SELECT * FROM fact_confirmations WHERE subject_type='job' AND status='pending' ORDER BY subject_id, field")],
            "courses": [dict(r) for r in self.db.query("SELECT * FROM fact_confirmations WHERE subject_type='course' AND status='pending' ORDER BY subject_id, field")],
        }
        return {
            "generated_at": iso_now(),
            "filters": filters,
            "count": len(referrals),
            "recommendations": [self._report_item(r) for r in referrals],
            "pending_fact_count": len(pending["jobs"]) + len(pending["courses"]),
            "pending_facts": pending,
        }

    def _report_item(self, referral: dict[str, Any]) -> dict[str, Any]:
        match = (referral.get("reason") or {}).get("match") or referral.get("auto_assessment", {}).get("match")
        return {
            "referral": referral["referral"],
            "state": referral["state"],
            "person": referral.get("person"),
            "company": referral["job"].get("company"),
            "job": referral["job_id"],
            "job_title": referral["job"].get("title"),
            "course": referral["course_id"],
            "provider": referral["course"].get("provider"),
            "start_date": referral["course"].get("start_date"),
            "free_seats": referral.get("free_seats"),
            "contact_company": bool(referral.get("contact_company")),
            "contact_learner": bool(referral.get("contact_learner")),
            "needs_contact": bool(referral.get("needs_contact")),
            "contact_party": referral.get("contact_party", []),
            "needs_recheck": bool(referral.get("needs_recheck")),
            "review_reasons": referral.get("review_reasons", []),
            "recommendation_reason": match,
            "pending_facts": referral.get("pending_facts"),
            "history": referral.get("history", []),
            "basis_snapshot": referral.get("basis_snapshot"),
        }

    def change_history(
        self,
        *,
        company: str | None = None,
        skill: str | None = None,
        start_from: str | None = None,
        start_to: str | None = None,
        start_period: str | None = None,
        person: str | None = None,
        state: str | None = None,
        aggregate_type: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM change_events ORDER BY occurred_at, rowid"
        )
        events = []
        allowed_referrals = None
        if any([company, skill, start_from, start_to, start_period, person, state]):
            allowed_referrals = {
                r["referral"]
                for r in self.query_recommendations(
                    company=company,
                    skill=skill,
                    start_from=start_from,
                    start_to=start_to,
                    start_period=start_period,
                    person=person,
                    state=state,
                )
            }
        for row in rows:
            event = dict(row)
            event["before"] = self._loads(event.pop("before_json", None), None)
            event["after"] = self._loads(event.pop("after_json", None), None)
            if aggregate_type and event["aggregate_type"] != aggregate_type:
                continue
            if allowed_referrals is not None:
                # 与筛选推荐相关的岗位/课程事件，或推荐自身事件。
                related = False
                if event["aggregate_type"] == "referral" and event["aggregate_id"] in allowed_referrals:
                    related = True
                elif event.get("related_id") and event["aggregate_type"] in {"job", "course"}:
                    refs = self.db.query(
                        "SELECT referral FROM referrals WHERE job_id=? OR course_id=?",
                        (event["aggregate_id"], event["aggregate_id"]),
                    )
                    related = any(r["referral"] in allowed_referrals for r in refs)
                if not related:
                    continue
            events.append(event)
        return events
