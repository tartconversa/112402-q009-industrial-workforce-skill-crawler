"""技能治理：规范术语、确认同义词和覆盖关系。

关键原则：``difflib`` 只用于给治理人员提供候选提示，推荐判定只接受已确认的
别名或覆盖规则。
"""

from __future__ import annotations

import difflib
from typing import Any, Iterable

from .db import Database, new_id
from .timeutils import iso_now, normalize_term, unique_list


class SkillGovernance:
    def __init__(self, db: Database):
        self.db = db

    # ---------- 术语和同义表达 ----------

    def add_term(
        self,
        canonical: str,
        kind: str = "skill",
        created_by: str = "skill-governance",
        note: str | None = None,
    ) -> int:
        if not canonical or not str(canonical).strip():
            raise ValueError("术语不能为空")
        if kind not in {"skill", "equipment", "any"}:
            raise ValueError("kind 必须是 skill、equipment 或 any")
        normalized = normalize_term(canonical)
        existing = self.db.query_one(
            "SELECT * FROM terms WHERE normalized = ?", (normalized,)
        )
        if existing:
            return int(existing["id"])
        # 可能已经作为别名存在；直接返回其所属术语，避免分裂。
        alias = self.db.query_one("SELECT term_id FROM aliases WHERE normalized = ?", (normalized,))
        if alias:
            return int(alias["term_id"])
        cur = self.db.execute(
            """
            INSERT INTO terms(canonical, normalized, kind, created_by, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(canonical).strip(), normalized, kind, created_by, note, iso_now()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def get_term(self, term_or_alias: str) -> dict[str, Any] | None:
        normalized = normalize_term(term_or_alias)
        if not normalized:
            return None
        row = self.db.query_one("SELECT * FROM terms WHERE normalized = ?", (normalized,))
        if row:
            return dict(row)
        row = self.db.query_one(
            """
            SELECT t.* FROM aliases a JOIN terms t ON t.id = a.term_id
            WHERE a.normalized = ?
            """,
            (normalized,),
        )
        return dict(row) if row else None

    def require_term(self, term_or_alias: str, kind: str | None = None) -> dict[str, Any]:
        term = self.get_term(term_or_alias)
        if not term:
            raise KeyError(f"未经技能治理登记的术语或同义表达: {term_or_alias}")
        if kind and term.get("kind") not in {kind, "any"}:
            # 类型不符不直接报错：设备/技能可能交叉；由调用方决定。
            term["requested_kind"] = kind
        return term

    def add_alias(
        self,
        expression: str,
        canonical: str | int,
        created_by: str = "skill-governance",
    ) -> int:
        if isinstance(canonical, int):
            term = self.db.query_one("SELECT * FROM terms WHERE id = ?", (canonical,))
            if not term:
                raise KeyError(canonical)
            term_id = int(term["id"])
        else:
            term_id = self.add_term(canonical, created_by=created_by)
        normalized = normalize_term(expression)
        if not normalized:
            raise ValueError("同义表达不能为空")
        existing = self.db.query_one("SELECT * FROM aliases WHERE normalized = ?", (normalized,))
        if existing:
            if int(existing["term_id"]) != term_id:
                raise ValueError(f"同义表达 {expression!r} 已映射到其他术语")
            return int(existing["id"])
        # 如果表达本身是另一个规范术语，不允许悄悄制造歧义。
        term = self.db.query_one("SELECT id FROM terms WHERE normalized = ?", (normalized,))
        if term and int(term["id"]) != term_id:
            raise ValueError(
                f"{expression!r} 已是另一个规范术语；请先合并术语或建立明确覆盖规则"
            )
        cur = self.db.execute(
            """
            INSERT INTO aliases(term_id, alias, normalized, created_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (term_id, str(expression).strip(), normalized, created_by, iso_now()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def add_synonym(
        self,
        expression: str,
        canonical: str,
        created_by: str = "skill-governance",
    ) -> int:
        """登记工作人员确认的同义说法。"""
        return self.add_alias(expression, canonical, created_by)

    def add_synonym_pair(
        self,
        first: str,
        second: str,
        preferred: str | None = None,
        created_by: str = "skill-governance",
    ) -> int:
        """把两个已确认等价的说法连到同一规范术语。"""
        canonical = preferred or first
        term_id = self.add_term(canonical, created_by=created_by)
        for expression in (first, second):
            if normalize_term(expression) != normalize_term(canonical):
                self.add_alias(expression, term_id, created_by)
        return term_id

    def resolve(self, expression: str) -> dict[str, Any] | None:
        return self.get_term(expression)

    def resolve_all(self, expressions: Iterable[str]) -> list[dict[str, Any] | None]:
        return [self.resolve(item) for item in expressions]

    # ---------- 已确认覆盖关系 ----------

    def add_coverage_rule(
        self,
        requirement: str | int,
        covering: str | int,
        coverage: str = "partial",
        confirmer: str = "skill-governance",
        note: str | None = None,
        rule_kind: str = "coverage",
    ) -> str:
        if coverage not in {"full", "partial"}:
            raise ValueError("coverage 必须是 full 或 partial")
        req_id = self._term_id(requirement)
        cov_id = self._term_id(covering)
        existing = self.db.query_one(
            """
            SELECT id FROM coverage_rules
            WHERE requirement_term_id=? AND covering_term_id=?
              AND coverage=? AND rule_kind=?
            """,
            (req_id, cov_id, coverage, rule_kind),
        )
        if existing:
            return str(existing["id"])
        rule_id = new_id("CR-")
        self.db.execute(
            """
            INSERT INTO coverage_rules(
                id, requirement_term_id, covering_term_id, coverage, rule_kind,
                confirmer, note, active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (rule_id, req_id, cov_id, coverage, rule_kind, confirmer, note, iso_now()),
        )
        self.db.commit()
        return rule_id

    def add_partial_coverage(self, requirement: str, covering: str, **kwargs: Any) -> str:
        return self.add_coverage_rule(requirement, covering, "partial", **kwargs)

    def add_full_coverage(self, requirement: str, covering: str, **kwargs: Any) -> str:
        return self.add_coverage_rule(requirement, covering, "full", **kwargs)

    def _term_id(self, value: str | int) -> int:
        if isinstance(value, int):
            return value
        term = self.get_term(value)
        if not term:
            # 治理人员新增关系时，未登记术语先建档，关系本身仍须人工确认。
            return self.add_term(value)
        return int(term["id"])

    def coverage_rules_for(self, requirement_term_id: int) -> list[dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT r.*, ct.canonical AS covering_name, rt.canonical AS requirement_name
            FROM coverage_rules r
            JOIN terms ct ON ct.id = r.covering_term_id
            JOIN terms rt ON rt.id = r.requirement_term_id
            WHERE r.requirement_term_id = ? AND r.active = 1
            """,
            (requirement_term_id,),
        )
        return [dict(row) for row in rows]

    # ---------- 匹配 ----------

    def match_course(
        self,
        required_skills: Iterable[str],
        course_skills: Iterable[str],
        equipment: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        required = unique_list(required_skills or [])
        covered = unique_list(list(course_skills or []) + list(equipment or []))

        course_terms: dict[int, dict[str, Any]] = {}
        course_raw_norm: dict[str, str] = {}
        unresolved_course: list[str] = []
        for raw in covered:
            raw_norm = normalize_term(raw)
            course_raw_norm[raw_norm] = raw
            term = self.resolve(raw)
            if term:
                course_terms[int(term["id"])] = term
            else:
                unresolved_course.append(raw)

        lines: list[dict[str, Any]] = []
        for requirement_raw in required:
            req_norm = normalize_term(requirement_raw)
            req_term = self.resolve(requirement_raw)
            line: dict[str, Any] = {
                "requirement": requirement_raw,
                "normalized": req_norm,
                "term_id": req_term["id"] if req_term else None,
                "term": req_term["canonical"] if req_term else None,
                "status": "unmapped" if not req_term else "not_covered",
                "strength": None,
                "matches": [],
                "text_similarity_hint": self.text_similarity_hint(
                    requirement_raw, covered
                ),
            }
            # 完全相同的规范化词形是精确匹配，不是“相似度推断”。已确认别名会在
            # resolve() 中落到同一 term；未登记但两边逐字一致也可作为最低风险依据。
            if req_norm in course_raw_norm:
                line["status"] = "covered"
                line["strength"] = "full"
                line["matches"].append(
                    {
                        "covering": course_raw_norm[req_norm],
                        "via": "exact-normalized-text",
                        "coverage": "full",
                        "governed": bool(req_term),
                    }
                )
            if req_term:
                req_id = int(req_term["id"])
                if req_id in course_terms and line["status"] != "covered":
                    course_term = course_terms[req_id]
                    line["status"] = "covered"
                    line["strength"] = "full"
                    line["matches"].append(
                        {
                            "covering": course_term["canonical"],
                            "via": "canonical-or-confirmed-synonym",
                            "coverage": "full",
                        }
                    )
                for rule in self.coverage_rules_for(req_id):
                    covering_id = int(rule["covering_term_id"])
                    if covering_id in course_terms:
                        course_term = course_terms[covering_id]
                        # full 规则优先于 partial。
                        match = {
                            "rule_id": rule["id"],
                            "covering": course_term["canonical"],
                            "via": "confirmed-coverage-rule",
                            "coverage": rule["coverage"],
                            "confirmer": rule["confirmer"],
                            "note": rule["note"],
                        }
                        line["matches"].append(match)
                        if rule["coverage"] == "full":
                            line["status"] = "covered"
                            line["strength"] = "full"
                        elif line["status"] != "covered":
                            line["status"] = "partial"
                            line["strength"] = "partial"
            lines.append(line)

        full = [line for line in lines if line["status"] == "covered"]
        partial = [line for line in lines if line["status"] == "partial"]
        unmapped = [line for line in lines if line["status"] == "unmapped"]
        not_covered = [line for line in lines if line["status"] == "not_covered"]
        # 只有所有明确要求都找到“完整/等价”依据才算满足。部分覆盖、未登记词、
        # 纯文本相似均不能判为满足。
        satisfies = bool(required) and not partial and not unmapped and not not_covered
        return {
            "satisfies": satisfies,
            "has_requirements": bool(required),
            "covered_count": len(full),
            "partial_count": len(partial),
            "unmapped_count": len(unmapped),
            "not_covered_count": len(not_covered),
            "lines": lines,
            "unresolved_course_terms": unresolved_course,
            "policy": "仅采用规范术语、确认同义词和确认覆盖规则；文本相似度只作治理提示",
        }

    def match(self, required_skills: Iterable[str], course_skills: Iterable[str], equipment: Iterable[str] | None = None) -> dict[str, Any]:
        return self.match_course(required_skills, course_skills, equipment)

    @staticmethod
    def text_similarity_hint(target: str, candidates: Iterable[str], limit: int = 3) -> list[dict[str, Any]]:
        """返回文本相似候选，但调用方不得把它当作满足依据。"""
        target_norm = normalize_term(target)
        hints = []
        for candidate in unique_list(candidates):
            candidate_norm = normalize_term(candidate)
            if not target_norm or not candidate_norm:
                continue
            score = difflib.SequenceMatcher(None, target_norm, candidate_norm).ratio()
            if score >= 0.55:
                hints.append({"candidate": candidate, "similarity": round(score, 3)})
        hints.sort(key=lambda item: item["similarity"], reverse=True)
        return hints[:limit]

    def governance_queue(self) -> dict[str, list[dict[str, Any]]]:
        """列出未登记术语和仅有文本相似提示、等待治理确认的词。"""
        terms = [dict(row) for row in self.db.query("SELECT * FROM terms ORDER BY canonical")]
        aliases = [dict(row) for row in self.db.query("SELECT * FROM aliases ORDER BY alias")]
        rules = [
            dict(row)
            for row in self.db.query(
                """
                SELECT r.*, rt.canonical AS requirement, ct.canonical AS covering
                FROM coverage_rules r
                JOIN terms rt ON rt.id = r.requirement_term_id
                JOIN terms ct ON ct.id = r.covering_term_id
                ORDER BY r.created_at DESC
                """
            )
        ]
        return {"terms": terms, "aliases": aliases, "coverage_rules": rules}
