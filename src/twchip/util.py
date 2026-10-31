"""數值與日期解析。原則：解析不了就是 NULL，絕不補 0。"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

import pandas as pd

_TAG = re.compile(r"<[^>]+>")
_MISSING = {"", "--", "---", "----", "-", "N/A", "nan", "NaN", "None", "null", "除權息", "除權", "除息"}


def strip_tags(s: Any) -> str:
    return _TAG.sub("", str(s)).replace("&nbsp;", " ").strip()


def to_number(s: Any) -> Optional[float]:
    """'1,234' → 1234.0；'(1,234)' → -1234.0；'12.3%' → 12.3；無法解析 → None。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return None if pd.isna(s) else float(s)
    t = strip_tags(s).replace(",", "").replace("−", "-").replace("－", "-").replace("＋", "+")
    t = t.replace("%", "").strip()
    if t in _MISSING:
        return None
    if t.startswith("(") and t.endswith(")"):
        t = "-" + t[1:-1]
    try:
        return float(t)
    except ValueError:
        return None


def to_int(s: Any) -> Optional[int]:
    v = to_number(s)
    if v is None:
        return None
    if abs(v - round(v)) > 1e-9:
        raise ValueError(f"預期整數但得到 {s!r}")
    return int(round(v))


def int_series(values) -> pd.Series:
    return pd.Series([to_int(v) for v in values], dtype="Int64")


def float_series(values) -> pd.Series:
    return pd.Series([to_number(v) for v in values], dtype="Float64")


def parse_any_date(s: Any) -> Optional[date]:
    """支援 20261030、2026/10/30、2026-10-30、115/10/30、115年10月30日。"""
    if s is None:
        return None
    t = strip_tags(s).strip()
    m = re.search(r"(\d{2,4})\s*[年/\-.]?\s*(\d{1,2})\s*[月/\-.]?\s*(\d{1,2})", t)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if len(m.group(1)) == 8:  # 罕見：整串 8 碼被第一組吃掉
        return None
    if y < 1911:
        y += 1911
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_compact_date(s: Any) -> Optional[date]:
    t = str(s or "").strip()
    if re.fullmatch(r"\d{8}", t):
        return datetime.strptime(t, "%Y%m%d").date()
    return parse_any_date(t)


def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def slash(d: date) -> str:
    return d.strftime("%Y/%m/%d")


def roc_slash(d: date) -> str:
    return f"{d.year - 1911:03d}/{d.month:02d}/{d.day:02d}"


def norm_code(s: Any) -> str:
    return strip_tags(s).replace("=", "").replace('"', "").strip()


def norm_field(s: Any) -> str:
    """欄位名正規化：去 HTML、空白、全形括號。"""
    t = strip_tags(s).replace("<br>", "").replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", t)
