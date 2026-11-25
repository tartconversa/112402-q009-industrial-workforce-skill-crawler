"""来源访问策略、HTTP 抓取、分页适配和采集对账。

网络层通过 ``transport`` 注入，因此测试可以完全不访问外网。采集成功完成所有
分页后才会把“本次未见”的旧记录标记为来源删除；临时中断只令本次 run 失败，
绝不据此批量删除岗位或课程。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from .db import Database, new_id
from .errors import CollectionInterruptedError, PermanentFetchError, TransientFetchError
from .facts import record_event, upsert_collected_fact
from .timeutils import canonical_json, coerce_date, coerce_datetime, iso_now, safe_int, split_values


# ---------------- 访问策略和 HTTP ----------------


@dataclass
class AccessPolicy:
    """单个来源的访问规则。"""

    min_interval_seconds: float = 1.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.5
    backoff_factor: float = 2.0
    timeout_seconds: float = 10.0
    user_agent: str = "ParkBridgeBot/1.0 (+local industrial-park training coordination)"
    respect_robots_txt: bool = True
    allowed_domains: list[str] = field(default_factory=list)
    max_pages: int = 100
    requests_per_minute: int | None = None

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "AccessPolicy":
        config = dict(config or {})
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {key: value for key, value in config.items() if key in known}
        if "rate_limit_seconds" in config and "min_interval_seconds" not in kwargs:
            kwargs["min_interval_seconds"] = config["rate_limit_seconds"]
        if "requests_per_minute" in kwargs and kwargs["requests_per_minute"]:
            rpm = int(kwargs["requests_per_minute"])
            interval = 60.0 / rpm
            kwargs["min_interval_seconds"] = max(float(kwargs.get("min_interval_seconds", 0)), interval)
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_interval_seconds": self.min_interval_seconds,
            "max_retries": self.max_retries,
            "backoff_base_seconds": self.backoff_base_seconds,
            "backoff_factor": self.backoff_factor,
            "timeout_seconds": self.timeout_seconds,
            "user_agent": self.user_agent,
            "respect_robots_txt": self.respect_robots_txt,
            "allowed_domains": self.allowed_domains,
            "max_pages": self.max_pages,
            "requests_per_minute": self.requests_per_minute,
        }


@dataclass
class TransportResponse:
    url: str
    status_code: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes | None = None


class Transport(Protocol):
    def get(self, url: str, headers: dict[str, str], timeout: float) -> TransportResponse: ...


class UrllibTransport:
    def get(self, url: str, headers: dict[str, str], timeout: float) -> TransportResponse:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                content = response.read()
                encoding = response.headers.get_content_charset() or "utf-8"
                status_code = response.getcode() or getattr(response, "status", None) or 200
                return TransportResponse(
                    url=response.geturl(),
                    status_code=int(status_code),
                    text=content.decode(encoding, errors="replace"),
                    headers={key: value for key, value in response.headers.items()},
                    content=content,
                )
        except urllib.error.HTTPError as exc:
            content = exc.read() if hasattr(exc, "read") else b""
            text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
            return TransportResponse(
                url=url,
                status_code=exc.code,
                text=text,
                headers={key: value for key, value in (exc.headers or {}).items()},
                content=content if isinstance(content, bytes) else text.encode(),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransientFetchError(str(exc)) from exc


class DictTransport:
    """测试/离线演示用：``routes`` 可为 URL 到字符串/响应/异常的映射。"""

    def __init__(self, routes: dict[str, Any] | None = None):
        self.routes = dict(routes or {})
        self.requests: list[dict[str, Any]] = []

    def add(self, url: str, body: Any, status_code: int = 200, headers: dict[str, str] | None = None) -> None:
        if isinstance(body, TransportResponse):
            self.routes[url] = body
        else:
            self.routes[url] = TransportResponse(url, status_code, body, headers or {})

    def get(self, url: str, headers: dict[str, str], timeout: float) -> TransportResponse:
        self.requests.append({"url": url, "headers": headers, "timeout": timeout})
        if url not in self.routes:
            # 也允许 query/path 宽松匹配前缀。
            for route, value in self.routes.items():
                if url.startswith(route):
                    url = route
                    break
        value = self.routes.get(url)
        if value is None:
            raise PermanentFetchError(f"DictTransport 未配置地址: {url}")
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(url)
        if isinstance(value, TransportResponse):
            return value
        return TransportResponse(url, 200, str(value), {})


class HttpFetcher:
    def __init__(
        self,
        policy: AccessPolicy | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ):
        self.policy = policy or AccessPolicy()
        self.transport = transport or UrllibTransport()
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._last_request_at: float | None = None
        self._robots: dict[str, "urllib.robotparser.RobotFileParser"] = {}

    def get(self, url: str, policy: AccessPolicy | None = None) -> TransportResponse:
        pol = policy or self.policy
        self._check_domain(url, pol)
        if pol.respect_robots_txt and urlparse_scheme(url).scheme in {"http", "https"}:
            if not self._allowed_by_robots(url, pol):
                raise PermanentFetchError(f"robots.txt 或来源规则禁止访问: {url}")

        last_error: Exception | None = None
        for attempt in range(pol.max_retries + 1):
            self._rate_limit(pol)
            try:
                response = self.transport.get(
                    url,
                    {"User-Agent": pol.user_agent, "Accept": "application/json,text/csv,text/html,*/*"},
                    pol.timeout_seconds,
                )
                if 200 <= response.status_code < 300:
                    return response
                if response.status_code in {408, 429, 500, 502, 503, 504}:
                    last_error = TransientFetchError(f"HTTP {response.status_code}: {url}")
                    if attempt < pol.max_retries:
                        self._sleep(self._retry_delay(response, attempt, pol))
                        continue
                    raise last_error
                raise PermanentFetchError(f"HTTP {response.status_code}: {url}")
            except (TransientFetchError, OSError) as exc:
                last_error = exc if isinstance(exc, TransientFetchError) else TransientFetchError(str(exc))
                if attempt < pol.max_retries:
                    self._sleep(pol.backoff_base_seconds * (pol.backoff_factor ** attempt))
                    continue
                if isinstance(exc, TransientFetchError):
                    raise
                raise last_error from exc
        assert last_error is not None
        raise last_error

    def _rate_limit(self, policy: AccessPolicy) -> None:
        now = self._monotonic()
        if self._last_request_at is not None and policy.min_interval_seconds > 0:
            wait_for = policy.min_interval_seconds - (now - self._last_request_at)
            if wait_for > 0:
                self._sleep(wait_for)
        self._last_request_at = self._monotonic()

    @staticmethod
    def _retry_delay(response: TransportResponse, attempt: int, policy: AccessPolicy) -> float:
        retry_after = response.headers.get("Retry-After") or response.headers.get("retry-after")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return policy.backoff_base_seconds * (policy.backoff_factor ** attempt)

    @staticmethod
    def _check_domain(url: str, policy: AccessPolicy) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in {"file", ""}:
            return
        if parsed.scheme not in {"http", "https"}:
            raise PermanentFetchError(f"不支持的 URL scheme: {url}")
        if not policy.allowed_domains:
            return
        host = (parsed.hostname or "").lower()
        allowed = [d.lower() for d in policy.allowed_domains]
        if not any(host == d or host.endswith("." + d) for d in allowed):
            raise PermanentFetchError(f"域名不在来源访问规则允许列表: {host}")

    def _allowed_by_robots(self, url: str, policy: AccessPolicy) -> bool:
        import urllib.robotparser

        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            robots_url = urllib.parse.urljoin(origin, "/robots.txt")
            parser = urllib.robotparser.RobotFileParser()
            try:
                response = self.transport.get(robots_url, {"User-Agent": policy.user_agent}, policy.timeout_seconds)
            except Exception:
                # 无法取得 robots 时不绕过临时故障；由上层决定是否中断。
                raise TransientFetchError(f"无法读取 robots.txt: {robots_url}")
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            elif response.status_code in {401, 403}:
                parser.parse(["User-agent: *", "Disallow: /"])
            else:
                parser.parse([])
            self._robots[origin] = parser
        agent = policy.user_agent.split("/", 1)[0] or "*"
        return parser.can_fetch(agent, url)


def urlparse_scheme(url: str) -> urllib.parse.ParseResult:
    return urllib.parse.urlparse(url)


# ---------------- 分页与适配 ----------------


@dataclass
class Page:
    items: list[dict[str, Any]]
    next_cursor: str | None = None
    next_url: str | None = None
    raw: Any = None


class SourceAdapter:
    source_id: str = "source"
    kind: str = "job"

    def __init__(
        self,
        source_id: str,
        kind: str,
        list_url: str,
        field_map: dict[str, str] | None = None,
        policy: AccessPolicy | dict[str, Any] | None = None,
    ):
        self.source_id = source_id
        if kind not in {"job", "course"}:
            raise ValueError("kind 必须是 job 或 course")
        self.kind = kind
        self.list_url = list_url
        self.field_map = field_map or {}
        self.policy = policy if isinstance(policy, AccessPolicy) else AccessPolicy.from_config(policy)

    def build_url(self, cursor: str | None = None) -> str:  # pragma: no cover - simple override
        if not cursor:
            return self.list_url
        sep = "&" if "?" in self.list_url else "?"
        return f"{self.list_url}{sep}cursor={urllib.parse.quote(cursor)}"

    def parse_page(self, text: str, url: str) -> Page:  # pragma: no cover - interface
        raise NotImplementedError

    def normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        mapping = self.default_field_map()
        mapping.update(self.field_map or {})
        result: dict[str, Any] = {}
        for canonical, source_field in mapping.items():
            if isinstance(source_field, (list, tuple)):
                value = None
                for field_name in source_field:
                    value = self._lookup(item, field_name)
                    if value not in (None, ""):
                        break
            else:
                value = self._lookup(item, source_field)
            result[canonical] = value
        result["raw"] = item
        result.setdefault("external_id", result.get("id"))
        return result

    def default_field_map(self) -> dict[str, str | list[str]]:
        if self.kind == "job":
            return {
                "external_id": ["external_id", "id", "job_id", "position_id"],
                "url": ["url", "link", "source_url", "detail_url"],
                "company": ["company", "company_name", "employer"],
                "title": ["title", "job_title", "position"],
                "location": ["location", "work_location", "city"],
                "skills": ["skills", "required_skills", "must_skills", "skill_tags"],
                "updated": ["updated", "updated_at", "last_modified", "post_date", "published_at"],
                "state": ["state", "status", "hiring_status"],
            }
        return {
            "external_id": ["external_id", "id", "course_id", "code"],
            "url": ["url", "link", "source_url", "portal_url"],
            "provider": ["provider", "training_provider", "organization"],
            "title": ["title", "course_name", "name"],
            "equipment": ["equipment", "equipment_tags", "devices"],
            "skills": ["skills", "covered_skills", "skill_tags"],
            "prerequisites": ["prerequisites", "prerequisite", "required_before"],
            "seats": ["seats", "capacity", "free_seats"],
            "start": ["start", "start_date", "opening_date", "class_start"],
            "updated": ["updated", "updated_at", "last_modified"],
            "state": ["state", "status"],
        }

    @staticmethod
    def _lookup(item: dict[str, Any], path: str) -> Any:
        current: Any = item
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current


class JsonPagedAdapter(SourceAdapter):
    def __init__(
        self,
        *args: Any,
        pagination: dict[str, Any] | None = None,
        items_path: str = "items",
        next_url_path: str | None = None,
        next_cursor_path: str | None = None,
        has_more_path: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.items_path = items_path
        self.pagination = pagination or {"mode": "none"}
        if next_url_path:
            self.pagination["next_url_path"] = next_url_path
        if next_cursor_path:
            self.pagination["next_cursor_path"] = next_cursor_path
        if has_more_path:
            self.pagination["has_more_path"] = has_more_path

    def build_url(self, cursor: str | int | None = None) -> str:
        mode = self.pagination.get("mode", "none")
        if cursor is None or mode == "none":
            return self.list_url
        params: dict[str, Any] = {}
        if mode == "page":
            param = self.pagination.get("page_param", "page")
            params[param] = cursor
            size_param = self.pagination.get("page_size_param")
            if size_param and self.pagination.get("page_size"):
                params[size_param] = self.pagination["page_size"]
        elif mode == "offset":
            param = self.pagination.get("offset_param", "offset")
            params[param] = cursor
        else:
            param = self.pagination.get("cursor_param", "cursor")
            params[param] = cursor
        parts = urllib.parse.urlparse(self.list_url)
        query = urllib.parse.parse_qsl(parts.query)
        query.extend((key, str(value)) for key, value in params.items())
        return urllib.parse.urlunparse(parts._replace(query=urllib.parse.urlencode(query)))

    def parse_page(self, text: str, url: str) -> Page:
        payload = json.loads(text)
        items = self._lookup(payload, self.items_path) if self.items_path else payload
        if items is None:
            items = []
        if not isinstance(items, list):
            raise ValueError(f"items_path={self.items_path!r} 未指向列表")
        next_cursor = None
        next_url = None
        pager = self.pagination
        if pager.get("next_cursor_path"):
            next_cursor = self._lookup(payload, pager["next_cursor_path"])
            next_cursor = None if next_cursor in (None, "") else str(next_cursor)
        if pager.get("next_url_path"):
            next_url = self._lookup(payload, pager["next_url_path"])
            if next_url:
                next_url = urllib.parse.urljoin(url, str(next_url))
        if pager.get("has_more_path"):
            has_more = self._lookup(payload, pager["has_more_path"])
            if not has_more:
                next_cursor = None
                next_url = None
        if next_cursor is None and pager.get("mode") == "page" and items:
            # 若未提供显式 next 路径，且本页达到 page_size，则尝试下一页。
            page_size = int(pager.get("page_size", len(items)))
            current_page = int(pager.get("start_page", 1))
            # URL 中读取当前页较繁琐；采集器会把当前 cursor 放入 Page。
            if len(items) >= page_size:
                next_cursor = "__next_page__"
        return Page(items=[dict(item) for item in items if isinstance(item, dict)], next_cursor=next_cursor, next_url=next_url, raw=payload)


class CsvAdapter(SourceAdapter):
    def parse_page(self, text: str, url: str) -> Page:
        reader = csv.DictReader(io.StringIO(text))
        return Page(items=[dict(row) for row in reader], next_cursor=None, next_url=None, raw=text)


# ---------------- 采集入库 ----------------


class CollectionHooks:
    """服务层可实现此接口，在记录变化后传播推荐复核。"""

    def after_record_change(self, change: dict[str, Any]) -> None:  # pragma: no cover - protocol
        pass


class Collector:
    def __init__(self, db: Database, fetcher: HttpFetcher | None = None, hooks: CollectionHooks | None = None):
        self.db = db
        self.fetcher = fetcher or HttpFetcher()
        self.hooks = hooks

    def register_source(
        self,
        source_id: str,
        kind: str,
        name: str | None = None,
        base_url: str | None = None,
        config: dict[str, Any] | AccessPolicy | None = None,
        enabled: bool = True,
    ) -> None:
        policy = config if isinstance(config, AccessPolicy) else AccessPolicy.from_config(config)
        existing = self.db.query_one("SELECT id FROM sources WHERE id=?", (source_id,))
        values = {
            "kind": kind,
            "name": name or source_id,
            "base_url": base_url,
            "config_json": canonical_json(policy.to_dict()),
            "enabled": 1 if enabled else 0,
            "updated_at": iso_now(),
        }
        if existing:
            sets = ", ".join(f"{key}=?" for key in values)
            self.db.execute(
                f"UPDATE sources SET {sets} WHERE id=?",
                tuple(Database._param(v) for v in values.values()) + (source_id,),
            )
        else:
            self.db.insert(
                "sources",
                {"id": source_id, "created_at": iso_now(), **values},
            )
        self.db.commit()

    def collect(self, adapter: SourceAdapter, *, resume: bool = False) -> dict[str, Any]:
        source = self.db.query_one("SELECT * FROM sources WHERE id=?", (adapter.source_id,))
        if not source:
            self.register_source(
                adapter.source_id,
                adapter.kind,
                base_url=urllib.parse.urlparse(adapter.list_url)._replace(query="", fragment="").geturl(),
                config=adapter.policy,
            )
            source = self.db.query_one("SELECT * FROM sources WHERE id=?", (adapter.source_id,))
        assert source is not None
        policy = AccessPolicy.from_config(json.loads(source["config_json"] or "{}"))

        resumed_from = None
        cursor: str | None = None
        resume_url: str | None = None
        resumed_seen: set[str] = set()
        if resume:
            failed = self.db.query_one(
                """
                SELECT * FROM fetch_runs WHERE source_id=? AND status='failed'
                ORDER BY started_at DESC, rowid DESC LIMIT 1
                """,
                (adapter.source_id,),
            )
            if failed and (failed["next_cursor"] or failed["next_url"]):
                resumed_from = failed["id"]
                cursor = failed["next_cursor"]
                resume_url = failed["next_url"]
                try:
                    resumed_seen = set(json.loads(failed["seen_ids_json"] or "[]"))
                except (TypeError, json.JSONDecodeError):
                    resumed_seen = set()

        run_id = new_id("RUN-")
        self.db.insert(
            "fetch_runs",
            {
                "id": run_id,
                "source_id": adapter.source_id,
                "started_at": iso_now(),
                "status": "running",
                "complete": 0,
                "next_cursor": cursor,
                "next_url": resume_url,
                "resumed_from": resumed_from,
            },
        )
        self.db.commit()

        seen: set[str] = set(resumed_seen)
        pages = 0
        records_seen = 0
        if resume and resumed_from:
            previous = self.db.query_one("SELECT pages, records_seen FROM fetch_runs WHERE id=?", (resumed_from,))
            if previous:
                # 计数用于运行报告；去重集合才是删除对账依据。
                pages = 0
                records_seen = int(previous["records_seen"] or 0)
        current_url: str | None = None
        next_cursor = cursor
        next_url: str | None = resume_url
        try:
            while True:
                if pages >= policy.max_pages:
                    raise PermanentFetchError(f"超过来源 {adapter.source_id} 最大分页数 {policy.max_pages}")
                if next_url:
                    current_url = next_url
                    request_cursor = None
                else:
                    # JsonPagedAdapter 对自动页码使用特殊标记；维护具体页码。
                    request_cursor = next_cursor
                    if next_cursor == "__next_page__":
                        request_cursor = str(pages + 1)
                    current_url = adapter.build_url(request_cursor)
                response = self.fetcher.get(current_url, policy)
                doc_id = self._store_raw_document(run_id, adapter.source_id, current_url, pages, response)
                page = adapter.parse_page(response.text, response.url or current_url)
                pages += 1

                for raw_item in page.items:
                    normalized = adapter.normalize_item(raw_item)
                    external_id = str(normalized.get("external_id") or "").strip()
                    if not external_id:
                        raise ValidationErrorLike(f"来源 {adapter.source_id} 存在缺少稳定 ID 的记录")
                    if external_id in seen:
                        record_event(
                            self.db,
                            adapter.kind,
                            external_id,
                            "duplicate_listing_in_fetch",
                            reason="同一外部记录在分页中重复出现，已幂等忽略",
                            run_id=run_id,
                        )
                    seen.add(external_id)
                    records_seen += 1
                    change = self.upsert_record(adapter, normalized, raw_item, response.url, doc_id, run_id)
                    if self.hooks and change.get("change_kind") in {"created", "edited", "restored"}:
                        self.hooks.after_record_change(change)

                next_url = page.next_url
                next_cursor = page.next_cursor
                if next_cursor == "__next_page__":
                    next_cursor = str(pages + 1)
                if next_url:
                    next_cursor = None
                self.db.execute(
                    """
                    UPDATE fetch_runs
                    SET pages=?, records_seen=?, next_cursor=?, next_url=?, seen_ids_json=?
                    WHERE id=?
                    """,
                    (
                        pages,
                        records_seen,
                        next_cursor,
                        next_url,
                        canonical_json(sorted(seen)),
                        run_id,
                    ),
                )
                self.db.commit()

                if next_url:
                    continue
                if next_cursor:
                    continue
                break

            self.db.execute(
                """
                UPDATE fetch_runs SET finished_at=?, status='success', complete=1,
                       pages=?, records_seen=?, next_cursor=NULL, next_url=NULL WHERE id=?
                """,
                (iso_now(), pages, records_seen, run_id),
            )
            self.db.commit()
            deleted = self.reconcile_deletions(adapter.source_id, adapter.kind, seen, run_id)
            return {
                "run_id": run_id,
                "source_id": adapter.source_id,
                "pages": pages,
                "records_seen": records_seen,
                "seen_external_ids": sorted(seen),
                "marked_deleted": deleted,
                "status": "success",
            }
        except Exception as exc:
            self.db.execute(
                "UPDATE fetch_runs SET finished_at=?, status='failed', error=? WHERE id=?",
                (iso_now(), str(exc), run_id),
            )
            self.db.commit()
            if isinstance(exc, (TransientFetchError, PermanentFetchError)):
                if isinstance(exc, PermanentFetchError) and not isinstance(exc, TransientFetchError):
                    raise
                raise CollectionInterruptedError(adapter.source_id, run_id, exc) from exc
            raise CollectionInterruptedError(adapter.source_id, run_id, exc) from exc

    def _store_raw_document(
        self, run_id: str, source_id: str, url: str, page: int, response: TransportResponse
    ) -> str:
        doc_id = new_id("RAW-")
        content_hash = hashlib.sha256(response.text.encode("utf-8")).hexdigest()
        self.db.insert(
            "raw_documents",
            {
                "id": doc_id,
                "run_id": run_id,
                "source_id": source_id,
                "url": url,
                "page_key": str(page),
                "status_code": response.status_code,
                "content_type": response.headers.get("Content-Type") or response.headers.get("content-type"),
                "content_hash": content_hash,
                "content": response.text,
                "fetched_at": iso_now(),
            },
        )
        return doc_id

    def upsert_record(
        self,
        adapter: SourceAdapter,
        normalized: dict[str, Any],
        raw_item: dict[str, Any],
        response_url: str,
        raw_doc_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        kind = adapter.kind
        table = "jobs" if kind == "job" else "courses"
        external_id = str(normalized["external_id"])
        source_url = normalized.get("url") or response_url
        source_updated = coerce_datetime(normalized.get("updated"))
        raw_json = canonical_json(raw_item)
        content_hash = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        existing = self.db.query_one(
            f"SELECT * FROM {table} WHERE source_id=? AND external_id=?",
            (adapter.source_id, external_id),
        )
        now = iso_now()

        if existing and existing["content_hash"] == content_hash:
            restored = existing["status"] == "source_deleted"
            if restored:
                source_state = self._normalize_state(normalized.get("state"))
                if kind == "job":
                    new_status = "closed" if source_state in {"closed", "stopped", "filled", "offline"} else "active"
                    if existing["status"] in {"withdrawn", "duplicate"}:
                        new_status = existing["status"]
                else:
                    new_status = "cancelled" if source_state in {"cancelled", "canceled", "closed"} else "active"
                    if existing["status"] in {"duplicate", "cancelled"}:
                        new_status = existing["status"]
                self.db.execute(
                    f"UPDATE {table} SET status=?, deleted_at=NULL, last_successful_run_id=?, updated_at=? WHERE id=?",
                    (new_status, run_id, iso_now(), existing["id"]),
                )
                record_event(
                    self.db,
                    kind,
                    existing["id"],
                    "source_restored",
                    reason="相同原始内容在完整来源中重新出现",
                    run_id=run_id,
                )
            else:
                self.db.execute(
                    f"UPDATE {table} SET last_successful_run_id=? WHERE id=?",
                    (run_id, existing["id"]),
                )
            self.db.commit()
            return {
                "change_kind": "restored" if restored else "unchanged",
                "subject_type": kind,
                "subject_id": existing["id"],
            }

        if existing:
            subject_id = str(existing["id"])
            version_no = int(
                self.db.query_one(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no FROM record_versions WHERE subject_type=? AND subject_id=?",
                    (kind, subject_id),
                )["next_no"]
            )
            change_kind = "edited"
            before = dict(existing)
        else:
            supplied_id = normalized.get("id")
            candidate_id = str(supplied_id) if supplied_id else external_id
            collision = self.db.query_one("SELECT source_id, external_id FROM jobs WHERE id=?", (candidate_id,)) if kind == "job" else self.db.query_one("SELECT source_id, external_id FROM courses WHERE id=?", (candidate_id,))
            if collision and (collision["source_id"] != adapter.source_id or collision["external_id"] != external_id):
                candidate_id = f"{'J' if kind == 'job' else 'C'}-{adapter.source_id}-{external_id}"
            subject_id = candidate_id
            version_no = 1
            change_kind = "created"
            before = None

        normalized_for_store = {key: value for key, value in normalized.items() if key != "raw"}
        prior_version_with_hash = self.db.query_one(
            """
            SELECT id, version_no FROM record_versions
            WHERE subject_type=? AND subject_id=? AND content_hash=?
            ORDER BY version_no LIMIT 1
            """,
            (kind, subject_id, content_hash),
        ) if existing else None
        if prior_version_with_hash:
            # 内容回退到 A→B→A 中的 A：复用原始版本，避免唯一哈希冲突并保留原始版本号。
            version_id = str(prior_version_with_hash["id"])
            version_no = int(prior_version_with_hash["version_no"])
        else:
            version_id = new_id("VER-")
            self.db.insert(
                "record_versions",
                {
                    "id": version_id,
                    "subject_type": kind,
                    "subject_id": subject_id,
                    "version_no": version_no,
                    "source_id": adapter.source_id,
                    "external_id": external_id,
                    "source_url": source_url,
                    "content_hash": content_hash,
                    "raw_json": raw_json,
                    "normalized_json": canonical_json(normalized_for_store),
                    "source_updated_at": source_updated,
                    "fetched_at": now,
                    "created_at": now,
                },
            )

        if kind == "job":
            values = self._job_values(normalized, adapter.source_id, source_url, source_updated, now, version_id, content_hash, run_id)
        else:
            values = self._course_values(normalized, adapter.source_id, source_url, source_updated, now, version_id, content_hash, run_id)

        if existing:
            assignments = ", ".join(f"{key}=:{key}" for key in values)
            self.db.execute(
                f"UPDATE {table} SET {assignments} WHERE id=:where_id",
                {**Database._param_dict(values), "where_id": subject_id},
            )
        else:
            self.db.insert(table, {"id": subject_id, "collected_at": now, **values})

        self._replace_tags(kind, subject_id, normalized)
        self._store_source_facts(kind, subject_id, normalized, adapter.source_id, version_id, source_updated)

        if change_kind == "edited":
            after = self.db.query_one(f"SELECT * FROM {table} WHERE id=?", (subject_id,))
            changed_fields = self._diff_rows(before, dict(after) if after else values)
            event_type = "source_edited"
            reason = "来源记录发生编辑"
            if kind == "job" and "status" in changed_fields and values.get("source_state"):
                event_type = "job_reported_state_changed"
                reason = f"来源报告岗位状态为 {values['source_state']}"
            record_event(
                self.db,
                kind,
                subject_id,
                event_type,
                reason=reason,
                before={"changed_fields": changed_fields, "before": self._json_safe(before)},
                after=self._json_safe(values),
                run_id=run_id,
            )
        else:
            record_event(
                self.db,
                kind,
                subject_id,
                "source_discovered",
                reason="首次采集到来源记录",
                after=self._json_safe(values),
                run_id=run_id,
            )

        # 删除后重新出现。
        if existing and existing["status"] == "source_deleted":
            self.db.execute(f"UPDATE {table} SET deleted_at=NULL WHERE id=?", (subject_id,))
            record_event(self.db, kind, subject_id, "source_restored", reason="记录重新出现在完整分页中", run_id=run_id)
            change_kind = "restored"

        self.db.commit()
        after_row = self.db.query_one(f"SELECT * FROM {table} WHERE id=?", (subject_id,))
        return {
            "change_kind": change_kind,
            "subject_type": kind,
            "subject_id": subject_id,
            "external_id": external_id,
            "version_id": version_id,
            "run_id": run_id,
            "normalized": normalized_for_store,
            "before": before,
            "after": dict(after_row) if after_row else values,
        }

    def _job_values(self, normalized, source_id, source_url, source_updated, now, version_id, content_hash, run_id):
        skills = split_values(normalized.get("skills"))
        company = normalized.get("company")
        title = normalized.get("title")
        location = normalized.get("location")
        source_state = self._normalize_state(normalized.get("state"))
        fingerprint = hashlib.sha256(
            canonical_json([company, title, location, sorted(skills)]).encode()
        ).hexdigest()
        status = "closed" if source_state in {"closed", "stopped", "filled", "offline", "已停招", "停招", "关闭"} else "active"
        current = self.db.query_one("SELECT status FROM jobs WHERE source_id=? AND external_id=?", (source_id, normalized["external_id"]))
        if current and current["status"] in {"withdrawn", "duplicate"}:
            status = current["status"]
        return {
            "source_id": source_id,
            "external_id": str(normalized["external_id"]),
            "source_url": source_url,
            "company": str(company) if company is not None else None,
            "title": str(title) if title is not None else None,
            "location": str(location) if location is not None else None,
            "source_state": source_state,
            "source_updated_at": source_updated,
            "updated_at": now,
            "current_version_id": version_id,
            "content_hash": content_hash,
            "fingerprint": fingerprint,
            "status": status,
            "last_successful_run_id": run_id,
        }

    def _course_values(self, normalized, source_id, source_url, source_updated, now, version_id, content_hash, run_id):
        equipment = split_values(normalized.get("equipment"))
        skills = split_values(normalized.get("skills"))
        prerequisites = split_values(normalized.get("prerequisites"))
        seats = safe_int(normalized.get("seats"))
        start = coerce_date(normalized.get("start"))
        source_state = self._normalize_state(normalized.get("state"))
        fingerprint = hashlib.sha256(
            canonical_json([normalized.get("provider"), normalized.get("title"), sorted(equipment), sorted(skills), start]).encode()
        ).hexdigest()
        status = "cancelled" if source_state in {"cancelled", "canceled", "closed", "取消", "停开"} else "active"
        current = self.db.query_one("SELECT status FROM courses WHERE source_id=? AND external_id=?", (source_id, normalized["external_id"]))
        if current and current["status"] in {"duplicate", "cancelled"}:
            status = current["status"]
        return {
            "source_id": source_id,
            "external_id": str(normalized["external_id"]),
            "source_url": source_url,
            "provider": str(normalized["provider"]) if normalized.get("provider") is not None else None,
            "title": str(normalized["title"]) if normalized.get("title") is not None else None,
            "equipment_json": canonical_json(equipment),
            "skills_json": canonical_json(skills),
            "prerequisites_json": canonical_json(prerequisites),
            "seats": seats,
            "start_date": start,
            "source_state": source_state,
            "source_updated_at": source_updated,
            "updated_at": now,
            "current_version_id": version_id,
            "content_hash": content_hash,
            "fingerprint": fingerprint,
            "status": status,
            "last_successful_run_id": run_id,
        }

    @staticmethod
    def _normalize_state(value: Any) -> str | None:
        if value is None or value == "":
            return None
        text = str(value).strip().casefold()
        mapping = {
            "open": "open",
            "active": "open",
            "hiring": "open",
            "招聘中": "open",
            "在招": "open",
            "closed": "closed",
            "stopped": "closed",
            "filled": "closed",
            "offline": "closed",
            "停招": "closed",
            "已停招": "closed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "取消": "cancelled",
        }
        return mapping.get(text, str(value).strip())

    def _replace_tags(self, kind: str, subject_id: str, normalized: dict[str, Any]) -> None:
        if kind == "job":
            self.db.execute("DELETE FROM job_skill_tags WHERE job_id=?", (subject_id,))
            for pos, raw in enumerate(split_values(normalized.get("skills"))):
                term = self.db.query_one("SELECT id FROM terms WHERE normalized=?", (__import__("parkbridge.timeutils", fromlist=["normalize_term"]).normalize_term(raw),))
                self.db.execute(
                    "INSERT INTO job_skill_tags(job_id, position, raw_text, term_id) VALUES (?, ?, ?, ?)",
                    (subject_id, pos, raw, term["id"] if term else None),
                )
            return
        self.db.execute("DELETE FROM course_tag_sets WHERE course_id=?", (subject_id,))
        for tag_kind, key in (("equipment", "equipment"), ("skill", "skills"), ("prerequisite", "prerequisites")):
            for pos, raw in enumerate(split_values(normalized.get(key))):
                from .timeutils import normalize_term

                term = self.db.query_one("SELECT id FROM terms WHERE normalized=?", (normalize_term(raw),))
                self.db.execute(
                    """
                    INSERT INTO course_tag_sets(course_id, tag_kind, position, raw_text, term_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (subject_id, tag_kind, pos, raw, term["id"] if term else None),
                )

    def _store_source_facts(self, kind, subject_id, normalized, source_id, version_id, reported_at):
        if kind == "job":
            fields = {
                "company": normalized.get("company"),
                "title": normalized.get("title"),
                "location": normalized.get("location"),
                "hiring_status": self._normalize_state(normalized.get("state")),
                "required_skills": split_values(normalized.get("skills")),
            }
        else:
            fields = {
                "provider": normalized.get("provider"),
                "title": normalized.get("title"),
                "equipment": split_values(normalized.get("equipment")),
                "covered_skills": split_values(normalized.get("skills")),
                "prerequisites": split_values(normalized.get("prerequisites")),
                "capacity": safe_int(normalized.get("seats")),
                "start_date": coerce_date(normalized.get("start")),
            }
        for field, value in fields.items():
            upsert_collected_fact(
                self.db,
                subject_type=kind,
                subject_id=subject_id,
                field=field,
                value=value,
                source_id=source_id,
                raw_version_id=version_id,
                reported_at=reported_at,
            )

    def reconcile_deletions(self, source_id: str, kind: str, seen: set[str], run_id: str) -> list[str]:
        table = "jobs" if kind == "job" else "courses"
        rows = self.db.query(
            f"SELECT id, external_id, status FROM {table} WHERE source_id=? AND status NOT IN ('source_deleted','duplicate','withdrawn','cancelled')",
            (source_id,),
        )
        deleted: list[str] = []
        for row in rows:
            if row["external_id"] not in seen:
                self.db.execute(
                    f"UPDATE {table} SET status='source_deleted', deleted_at=?, updated_at=? WHERE id=?",
                    (iso_now(), iso_now(), row["id"]),
                )
                record_event(
                    self.db,
                    kind,
                    row["id"],
                    "source_deleted",
                    reason="完整分页成功后，该记录不再出现在来源目录",
                    run_id=run_id,
                )
                deleted.append(row["id"])
        self.db.commit()
        if self.hooks:
            for subject_id in deleted:
                self.hooks.after_record_change(
                    {
                        "change_kind": "deleted",
                        "subject_type": kind,
                        "subject_id": subject_id,
                        "run_id": run_id,
                    }
                )
        return deleted

    @staticmethod
    def _diff_rows(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
        ignore = {"updated_at", "collected_at", "last_successful_run_id", "current_version_id", "content_hash"}
        changed = {}
        for key, new_value in after.items():
            if key in ignore:
                continue
            old_value = before.get(key)
            if str(old_value) != str(new_value):
                changed[key] = {"before": Collector._safe(old_value), "after": Collector._safe(new_value)}
        return changed

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            return {key: Collector._json_safe(item) for key, item in value.items() if key != "content"}
        try:
            json.dumps(value)
            return value
        except TypeError:
            if hasattr(value, "keys"):
                return {key: Collector._json_safe(value[key]) for key in value.keys()}
            return str(value)

    @staticmethod
    def _safe(value: Any) -> Any:
        try:
            json.dumps(value)
            return value
        except TypeError:
            return str(value)


class ValidationErrorLike(Exception):
    pass
