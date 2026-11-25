"""命令行入口：初始化样例、采集、确认、推荐、预留和顾问查询。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .collectors import AccessPolicy, CsvAdapter, JsonPagedAdapter
from .errors import ParkBridgeError
from .sample import import_samples
from .reporting import export_csv_reports
from .service import ParkService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parkbridge", description=__doc__)
    parser.add_argument("--db", default="data/parkbridge.sqlite3", help="SQLite 数据库路径")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="初始化数据库，可同时导入合成样例")
    p_init.add_argument("--samples", action="store_true")
    p_init.add_argument("--samples-dir")
    p_init.add_argument("--no-governance", action="store_true")

    p_import = sub.add_parser("import-samples")
    p_import.add_argument("--samples-dir")
    p_import.add_argument("--no-governance", action="store_true")

    sub.add_parser("report", help="输出顾问推荐、空余席位、待确认事实和变化历史").add_argument("--company")
    _add_query_args(sub.choices["report"])

    p_export = sub.add_parser("export-csv", help="导出岗位、课程、推荐、待确认事实和历史 CSV")
    p_export.add_argument("--outdir", default="exports")

    p_rec = sub.add_parser("recommend", help="为有效岗位计算课程依据")
    p_rec.add_argument("job")
    p_rec.add_argument("--strict", action="store_true")
    p_rec.add_argument("--start-from")
    p_rec.add_argument("--start-to")
    p_rec.add_argument("--json", action="store_true")

    p_create = sub.add_parser("create-recommendation")
    p_create.add_argument("job")
    p_create.add_argument("course")
    p_create.add_argument("--person")
    p_create.add_argument("--id", help="自定义推荐编号")
    p_create.add_argument("--note")
    p_create.add_argument("--allow-unverified-override", action="store_true")
    p_create.add_argument("--override-reason")

    p_hold = sub.add_parser("hold", help="向机构提出/登记席位预留")
    p_hold.add_argument("referral")
    p_hold.add_argument("--seats", type=int)

    p_confirm = sub.add_parser("confirm", help="学员确认报名；触发全局唯一机构校验")
    p_confirm.add_argument("referral")
    p_confirm.add_argument("--person")
    p_confirm.add_argument("--actor", default="advisor")
    p_confirm.add_argument("--allow-review-override", action="store_true")
    p_confirm.add_argument("--override-reason")

    p_release = sub.add_parser("release")
    p_release.add_argument("referral")
    p_release.add_argument("--reason", default="顾问释放席位")

    p_cj = sub.add_parser("confirm-job")
    p_cj.add_argument("job")
    p_cj.add_argument("--confirmer", default="company-liaison")
    status = p_cj.add_mutually_exclusive_group()
    status.add_argument("--open", dest="is_hiring", action="store_true")
    status.add_argument("--closed", dest="is_hiring", action="store_false")
    p_cj.set_defaults(is_hiring=None)
    p_cj.add_argument("--location")
    p_cj.add_argument("--skills", help="用 | 或逗号分隔必须技能")
    p_cj.add_argument("--note")

    p_cc = sub.add_parser("confirm-course")
    p_cc.add_argument("course")
    p_cc.add_argument("--confirmer", default="course-owner")
    p_cc.add_argument("--equipment")
    p_cc.add_argument("--skills")
    p_cc.add_argument("--prerequisites")
    p_cc.add_argument("--start")
    p_cc.add_argument("--seats", type=int)
    p_cc.add_argument("--note")

    p_syn = sub.add_parser("add-synonym")
    p_syn.add_argument("expression")
    p_syn.add_argument("canonical")
    p_syn.add_argument("--by", default="skill-governance")

    p_partial = sub.add_parser("add-partial-coverage")
    p_partial.add_argument("requirement")
    p_partial.add_argument("covering")
    p_partial.add_argument("--by", default="skill-governance")
    p_partial.add_argument("--note")

    p_full = sub.add_parser("add-full-coverage")
    p_full.add_argument("requirement")
    p_full.add_argument("covering")
    p_full.add_argument("--by", default="skill-governance")
    p_full.add_argument("--note")

    p_collect = sub.add_parser("collect", help="按 JSON 来源配置执行 HTTP/CSV/JSON 采集")
    p_collect.add_argument("--config", required=True)
    p_collect.add_argument("--resume", action="store_true")

    p_history = sub.add_parser("history")
    _add_query_args(p_history)
    p_history.add_argument("--aggregate-type", choices=["job", "course", "referral"])

    sub.add_parser("jobs")
    sub.add_parser("courses")
    return parser


def _add_query_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--skill")
    parser.add_argument("--start-from")
    parser.add_argument("--start-to")
    parser.add_argument("--period", dest="start_period", help="开班期 YYYY-MM 或 YYYY")
    parser.add_argument("--person")
    parser.add_argument("--state")
    parser.add_argument("--json", action="store_true")


def _print(value: Any, as_json: bool = True) -> None:
    # 当前 CLI 面向自动化和园区工作人员的脚本/表格流程，默认输出稳定 JSON。
    # as_json 参数保留给后续人类可读文本模式。
    _ = as_json
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        with ParkService.open(args.db) as service:
            return _dispatch(service, args)
    except ParkBridgeError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except (ValueError, KeyError) as exc:
        print(f"输入或配置错误: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"文件不存在: {exc}", file=sys.stderr)
        return 2


def _dispatch(service: ParkService, args: argparse.Namespace) -> int:
    cmd = args.command
    if cmd == "init":
        service.db.init_schema()
        result = {"db": args.db, "initialized": True}
        if args.samples:
            result["samples"] = import_samples(
                service, args.samples_dir, seed_governance=not args.no_governance
            )
        _print(result)
        return 0
    if cmd == "import-samples":
        result = import_samples(service, args.samples_dir, seed_governance=not args.no_governance)
        _print(result)
        return 0
    if cmd == "report":
        result = service.advisor_report(
            company=args.company,
            skill=args.skill,
            start_from=args.start_from,
            start_to=args.start_to,
            start_period=getattr(args, "start_period", None),
            person=args.person,
            state=args.state,
        )
        _print(result, args.json if hasattr(args, "json") else True)
        return 0
    if cmd == "export-csv":
        _print(export_csv_reports(service, args.outdir))
        return 0
    if cmd == "recommend":
        result = service.recommend_for_job(
            args.job, strict=args.strict, start_from=args.start_from, start_to=args.start_to
        )
        _print(result, args.json)
        return 0
    if cmd == "create-recommendation":
        result = service.create_recommendation(
            args.job,
            args.course,
            person=args.person,
            referral_id=args.id,
            note=args.note,
            override_unverified=args.allow_unverified_override,
            override_reason=args.override_reason,
        )
        _print(result)
        return 0
    if cmd == "hold":
        _print(service.hold_reservation(args.referral, seats=args.seats))
        return 0
    if cmd == "confirm":
        _print(
            service.confirm_referral(
                args.referral,
                person=args.person,
                actor=args.actor,
                allow_review_override=args.allow_review_override,
                override_reason=args.override_reason,
            )
        )
        return 0
    if cmd == "release":
        _print(service.release_referral(args.referral, reason=args.reason))
        return 0
    if cmd == "confirm-job":
        skills = args.skills.replace(",", "|") if args.skills else None
        _print(
            service.confirm_job(
                args.job,
                confirmer=args.confirmer,
                is_hiring=args.is_hiring,
                location=args.location,
                required_skills=skills,
                note=args.note,
            )
        )
        return 0
    if cmd == "confirm-course":
        _print(
            service.confirm_course(
                args.course,
                confirmer=args.confirmer,
                equipment=args.equipment,
                covered_skills=args.skills,
                prerequisites=args.prerequisites,
                start=args.start,
                seats=args.seats,
                note=args.note,
            )
        )
        return 0
    if cmd == "add-synonym":
        _print({"term_id": service.governance.add_synonym(args.expression, args.canonical, args.by)})
        return 0
    if cmd == "add-partial-coverage":
        _print({"rule_id": service.governance.add_partial_coverage(args.requirement, args.covering, confirmer=args.by, note=args.note)})
        return 0
    if cmd == "add-full-coverage":
        _print({"rule_id": service.governance.add_full_coverage(args.requirement, args.covering, confirmer=args.by, note=args.note)})
        return 0
    if cmd == "jobs":
        _print(service.list_jobs())
        return 0
    if cmd == "courses":
        _print(service.list_courses())
        return 0
    if cmd == "history":
        _print(
            service.change_history(
                skill=args.skill,
                start_from=args.start_from,
                start_to=args.start_to,
                start_period=getattr(args, "start_period", None),
                person=args.person,
                state=args.state,
                aggregate_type=args.aggregate_type,
            )
        )
        return 0
    if cmd == "collect":
        _print(run_config_collection(service, args.config, resume=args.resume))
        return 0
    parser.error(f"未知命令: {cmd}")
    return 2


def run_config_collection(service: ParkService, config_path: str, *, resume: bool = False) -> dict[str, Any]:
    path = Path(config_path).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    results = []
    for source in config.get("sources", []):
        url = source["url"]
        parsed = Path(url)
        if not url.startswith(("http://", "https://", "file://")) and not parsed.is_absolute():
            url = (path.parent / url).resolve().as_uri()
        policy = AccessPolicy.from_config(source.get("policy"))
        kind = source["kind"]
        adapter_type = source.get("adapter", "json")
        common = {
            "source_id": source["id"],
            "kind": kind,
            "list_url": url,
            "field_map": source.get("field_map"),
            "policy": policy,
        }
        if adapter_type == "json":
            adapter = JsonPagedAdapter(
                **common,
                items_path=source.get("items_path", "items"),
                pagination=source.get("pagination"),
            )
        elif adapter_type == "csv":
            adapter = CsvAdapter(**common)
        else:
            raise ValueError(f"不支持的 adapter 类型: {adapter_type}")
        results.append(service.collector.collect(adapter, resume=resume))
    return {"config": str(path), "runs": results}


if __name__ == "__main__":
    raise SystemExit(main())
