"""把仓库自带的合成 CSV 样例导入服务。"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .db import new_id
from .facts import record_event
from .service import ParkService
from .timeutils import iso_now

DEFAULT_SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, strict=True))


def seed_sample_governance(service: ParkService) -> None:
    g = service.governance
    # 样例中的精确技能/设备先纳入治理目录。
    for term, kind in [
        ("PLC", "equipment"),
        ("PLC-X", "equipment"),
        ("diagnostics", "skill"),
        ("calibration", "skill"),
        ("robot-arm", "equipment"),
        ("RB-2", "equipment"),
        ("safety", "skill"),
        ("cnc-maintenance", "skill"),
        ("controls-tech", "skill"),
        ("robot-tech", "skill"),
    ]:
        g.add_term(term, kind=kind, created_by="skill-governance")

    # 治理人员确认的中文同义表达。它们只映射到明确登记的概念。
    g.add_synonym("数控调试", "cnc-maintenance", created_by="skill-governance:张磊")
    g.add_synonym("CNC调试", "cnc-maintenance", created_by="skill-governance:张磊")
    g.add_synonym("设备参数维护", "equipment-parameter-maintenance", created_by="skill-governance:张磊")
    g.add_term("equipment-parameter-maintenance", "skill", created_by="skill-governance:张磊")

    # “数控调试”和“设备参数维护”只确认存在部分交叉，不能据此判定完全满足。
    g.add_partial_coverage(
        "cnc-maintenance",
        "equipment-parameter-maintenance",
        confirmer="skill-governance:张磊",
        note="设备参数维护只覆盖数控调试中的参数环节，仍需实操与故障诊断证据",
    )
    g.add_partial_coverage(
        "cnc-maintenance",
        "diagnostics",
        confirmer="skill-governance:张磊",
        note="诊断能力是数控调试的相关组成，但课程只覆盖该要求时不能宣称完整胜任",
    )


def import_samples(
    service: ParkService,
    samples_dir: str | Path | None = None,
    *,
    seed_governance: bool = True,
) -> dict[str, Any]:
    samples = Path(samples_dir) if samples_dir else DEFAULT_SAMPLES
    if seed_governance:
        seed_sample_governance(service)

    jobs = read_csv(samples / "jobs.csv")
    courses = read_csv(samples / "courses.csv")
    referrals = read_csv(samples / "referrals.csv")

    for row in jobs:
        source = row["source"]
        service.add_job(
            row["job"],
            company=row["company"],
            title=row["title"],
            skills=row["skills"],
            location=row["location"],
            state=row["state"],
            source=source,
            updated=row["updated"],
            source_url=f"https://sources.example.test/{source}/jobs/{row['job']}",
        )

    for row in courses:
        source = f"portal-{row['provider'].lower()}"
        service.add_course(
            row["course"],
            provider=row["provider"],
            equipment=row["equipment"],
            skills=row["skills"],
            seats=int(row["seats"]) if row["seats"] else None,
            start=row["start"],
            state=row["state"],
            source=source,
            source_url=f"https://portals.example.test/{row['provider']}/courses/{row['course']}",
        )

    # 保留样例推荐状态。R1 held 占座，R2 proposed 不占座，R3 confirmed 永久保留依据。
    for row in referrals:
        rid = row["referral"]
        state = row["state"]
        now = iso_now()
        job_basis = service._job_basis(row["job"])
        course_basis = service._course_basis(row["course"])
        match = service.governance.match_course(
            job_basis["required_skills"],
            course_basis["covered_skills"],
            course_basis["equipment"],
        )
        snapshot = None
        confirmed_at = None
        seat_held = 1 if state in {"held", "confirmed"} else 0
        if state == "confirmed":
            confirmed_at = f"{row['updated']}T00:00:00+00:00"
            snapshot = {
                "confirmed_at": confirmed_at,
                "job_basis": job_basis,
                "course_basis": course_basis,
                "match": match,
                "imported_from_sample": True,
            }
        service.db.execute(
            """
            INSERT OR REPLACE INTO referrals(
                referral, job_id, course_id, person, state, previous_state,
                review_status, review_reasons_json, auto_assessment_json,
                contact_company, contact_learner, seats, seat_held, reason_json,
                basis_snapshot_json, confirmed_at, created_at, updated_at, updated
            ) VALUES (?, ?, ?, ?, ?, NULL, 'ok', '[]', '{}', 0, 0, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rid,
                row["job"],
                row["course"],
                row["person"],
                state,
                seat_held,
                json.dumps({"match": match, "imported_from_sample": True}, ensure_ascii=False),
                json.dumps(snapshot, ensure_ascii=False) if snapshot else None,
                confirmed_at,
                f"{row['updated']}T00:00:00+00:00",
                now,
                row["updated"],
            ),
        )
        if state == "confirmed" and row["person"]:
            course = service.get_course(row["course"])
            service.db.execute(
                """
                INSERT OR REPLACE INTO confirmed_learner_providers(person, provider, referral_id, confirmed_at)
                VALUES (?, ?, ?, ?)
                """,
                (row["person"], course["provider"], rid, confirmed_at or now),
            )
        record_event(
            service.db,
            "referral",
            rid,
            "sample_imported",
            actor="sample-importer",
            after={"state": state, "updated": row["updated"]},
            commit=False,
        )
    service.db.commit()
    return {"jobs": len(jobs), "courses": len(courses), "referrals": len(referrals)}
