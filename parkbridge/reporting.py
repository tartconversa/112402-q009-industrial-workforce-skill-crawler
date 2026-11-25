"""把顾问视图导出为 UTF-8 CSV，便于继续用共享表格接收结果。"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .service import ParkService


def _json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def export_csv_reports(service: ParkService, outdir: str | Path) -> dict[str, str]:
    target = Path(outdir)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}

    jobs = service.list_jobs(include_inactive=True)
    job_fields = [
        "id", "company", "title", "location", "status", "source_state",
        "source_id", "source_url", "source_updated_at", "updated_at",
        "raw_version_no", "effective_skills", "pending_fields",
    ]
    job_path = target / "jobs.csv"
    with job_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=job_fields)
        writer.writeheader()
        for job in jobs:
            writer.writerow({
                "id": job["id"],
                "company": job.get("company") or "",
                "title": job.get("title") or "",
                "location": job.get("location") or "",
                "status": job.get("status") or "",
                "source_state": job.get("source_state") or "",
                "source_id": job.get("source_id") or "",
                "source_url": job.get("source_url") or "",
                "source_updated_at": job.get("source_updated_at") or "",
                "updated_at": job.get("updated_at") or "",
                "raw_version_no": job.get("raw_version_no") or "",
                "effective_skills": "|".join(job.get("effective", {}).get("required_skills") or []),
                "pending_fields": "|".join(f["field"] for f in job.get("pending_facts", [])),
            })
    written["jobs"] = str(job_path)

    courses = service.list_courses(include_inactive=True)
    course_fields = [
        "id", "provider", "title", "status", "start_date", "capacity",
        "occupied_seats", "free_seats", "equipment", "covered_skills",
        "prerequisites", "source_id", "source_url", "source_updated_at",
        "raw_version_no", "pending_fields",
    ]
    course_path = target / "courses.csv"
    with course_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=course_fields)
        writer.writeheader()
        for course in courses:
            writer.writerow({
                "id": course["id"],
                "provider": course.get("provider") or "",
                "title": course.get("title") or "",
                "status": course.get("status") or "",
                "start_date": course.get("start_date") or "",
                "capacity": course.get("capacity") if course.get("capacity") is not None else "",
                "occupied_seats": course.get("occupied_seats", ""),
                "free_seats": course.get("free_seats") if course.get("free_seats") is not None else "",
                "equipment": "|".join(course.get("equipment") or []),
                "covered_skills": "|".join(course.get("skills") or []),
                "prerequisites": "|".join(course.get("prerequisites") or []),
                "source_id": course.get("source_id") or "",
                "source_url": course.get("source_url") or "",
                "source_updated_at": course.get("source_updated_at") or "",
                "raw_version_no": course.get("raw_version_no") or "",
                "pending_fields": "|".join(f["field"] for f in course.get("pending_facts", [])),
            })
    written["courses"] = str(course_path)

    report = service.advisor_report()
    rec_fields = [
        "referral", "state", "person", "company", "job", "course", "provider",
        "start_date", "free_seats", "can_satisfy", "covered_count",
        "partial_count", "unmapped_count", "review_reasons",
        "contact_company", "contact_learner", "recommendation_reason",
    ]
    rec_path = target / "recommendations.csv"
    with rec_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rec_fields)
        writer.writeheader()
        for item in report["recommendations"]:
            match = item.get("recommendation_reason") or {}
            writer.writerow({
                "referral": item["referral"],
                "state": item["state"],
                "person": item.get("person") or "",
                "company": item.get("company") or "",
                "job": item["job"],
                "course": item["course"],
                "provider": item.get("provider") or "",
                "start_date": item.get("start_date") or "",
                "free_seats": "" if item.get("free_seats") is None else item["free_seats"],
                "can_satisfy": match.get("satisfies", ""),
                "covered_count": match.get("covered_count", ""),
                "partial_count": match.get("partial_count", ""),
                "unmapped_count": match.get("unmapped_count", ""),
                "review_reasons": "|".join(item.get("review_reasons") or []),
                "contact_company": item.get("contact_company", False),
                "contact_learner": item.get("contact_learner", False),
                "recommendation_reason": _json_text(match),
            })
    written["recommendations"] = str(rec_path)

    pending_path = target / "pending_facts.csv"
    with pending_path.open("w", encoding="utf-8", newline="") as handle:
        fields = ["subject_type", "subject_id", "field", "value", "origin", "status", "reported_at", "id"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for kind in ("jobs", "courses"):
            for fact in report["pending_facts"][kind]:
                writer.writerow({
                    "subject_type": fact["subject_type"],
                    "subject_id": fact["subject_id"],
                    "field": fact["field"],
                    "value": fact.get("value_text") or "",
                    "origin": fact["origin"],
                    "status": fact["status"],
                    "reported_at": fact.get("reported_at") or "",
                    "id": fact["id"],
                })
    written["pending_facts"] = str(pending_path)

    history_path = target / "change_history.csv"
    events = service.change_history()
    with history_path.open("w", encoding="utf-8", newline="") as handle:
        fields = ["occurred_at", "aggregate_type", "aggregate_id", "event_type", "actor", "reason", "before", "after"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for event in events:
            writer.writerow({
                "occurred_at": event["occurred_at"],
                "aggregate_type": event["aggregate_type"],
                "aggregate_id": event["aggregate_id"],
                "event_type": event["event_type"],
                "actor": event.get("actor") or "",
                "reason": event.get("reason") or "",
                "before": _json_text(event.get("before")),
                "after": _json_text(event.get("after")),
            })
    written["change_history"] = str(history_path)
    return written
