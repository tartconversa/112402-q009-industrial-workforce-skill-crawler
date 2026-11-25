"""时间和文本规范化辅助。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def coerce_datetime(value: Any) -> str | None:
    """把常见日期/时间输入统一为 ISO-8601 字符串。

    仅用于存储和比较；无法识别的非空值保留原文，避免丢失来源事实。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    # 末尾 Z 是常见 UTC 写法。
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except ValueError:
        # 可能是 YYYY/MM/DD 或仅日期。
        for fmt in ("%Y/%m/%d", "%Y.%m.%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
    return text


def coerce_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    parsed = coerce_datetime(text)
    if parsed and re.match(r"^\d{4}-\d{2}-\d{2}", parsed):
        return parsed[:10]
    return text


def normalize_term(value: Any) -> str:
    """规范化用于精确/别名字典匹配的术语。

    这里不做模糊相似度判断，只做大小写、空白和常见分隔符归一。
    """
    if value is None:
        return ""
    text = str(value).casefold().strip()
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"[\s_\-/|,，、;；:：]+", "-", text)
    text = re.sub(r"[^\w一-鿿().+-]+", "", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


def split_values(value: Any) -> list[str]:
    """拆分样本和来源中常见的管道、逗号、分号及空白分隔值。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(split_values(item))
        return result
    text = str(value).strip()
    if not text:
        return []
    parts = re.split(r"[|,，;；、\n\r\t]+|\s{2,}", text)
    result = [p.strip() for p in parts if p and p.strip()]
    # 对 "PLC diagnostics" 这类单词间单空格不拆；样本使用 |。
    return result


def canonical_json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def safe_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def period_bounds(period: str | None) -> tuple[str | None, str | None]:
    """把 ``YYYY-MM`` 或 ``YYYY`` 开班期转成闭区间日期边界。"""
    if not period:
        return None, None
    text = str(period).strip()
    if re.fullmatch(r"\d{4}-\d{2}", text):
        year, month = map(int, text.split("-"))
        if month == 12:
            next_first = date(year + 1, 1, 1)
        else:
            next_first = date(year, month + 1, 1)
        last_day = (next_first - timedelta(days=1)).day
        return f"{text}-01", f"{text}-{last_day:02d}"
    if re.fullmatch(r"\d{4}", text):
        return f"{text}-01-01", f"{text}-12-31"
    return coerce_date(text), coerce_date(text)


def unique_list(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        key = normalize_term(text) or text
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result
