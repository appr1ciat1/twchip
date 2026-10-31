"""證券櫃檯買賣中心（上櫃）資料源，使用新版 /www/zh-tw/ JSON 端點。

日期參數格式在不同時期的文件中有西元斜線與民國兩種說法。這裡先用西元斜線，
若官方回應的日期不等於請求日期，再用民國格式重試一次；兩次都不符就如實回報，交給 G1 阻擋。
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd

from ..http import Fetcher
from ..util import float_series, int_series, norm_code, norm_field, parse_any_date, roc_slash, slash, strip_tags
from . import CODE_RE, FieldMap, Parsed

BASE = "https://www.tpex.org.tw/www/zh-tw"

QUOTE_SPEC = {
    "code": ["代號"], "name": ["名稱"], "close": ["收盤"], "change": ["漲跌"],
    "open": ["開盤"], "high": ["最高"], "low": ["最低"],
    "volume_shares": ["成交股數"], "value_twd": ["成交金額(元)", "成交金額"], "trades": ["成交筆數"],
}

QFII_SPEC = {
    "code": ["代號"], "name": ["名稱"],
    "issued_shares": ["發行股數(A)", "發行股數"],
    "foreign_available_shares": ["僑外資及陸資尚可投資股數B=A*F-C", "僑外資及陸資尚可投資股數"],
    "foreign_shares": ["僑外資及陸資持有股數(C)", "僑外資及陸資持有股數"],
    "available_ratio": ["僑外資及陸資尚可投資比率(D=B/A)", "僑外資及陸資尚可投資比率"],
    "foreign_ratio": ["僑外資及陸資持股比率(E=C/A)", "僑外資及陸資持股比率"],
    "limit_ratio": ["法令投資上限比率(F)", "法令投資上限比率"],
}

# dailyTrade 的欄位名稱重複（每組都叫 買進股數／賣出股數／買賣超股數），只能依位置對應，
# 因此必須先確認欄位序列與預期完全一致。
FLOW_GROUPS = ["fx", "fd", "foreign", "trust", "ds", "dh", "dealer"]
FLOW_FIELDS = ["代號", "名稱"] + ["買進股數", "賣出股數", "買賣超股數"] * len(FLOW_GROUPS) + ["三大法人買賣超股數合計"]


def _json(resp) -> dict:
    try:
        return json.loads(resp.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_invalid": True}


def _served(j: dict) -> date | None:
    t0 = (j.get("tables") or [{}])[0] if isinstance(j.get("tables"), list) and j.get("tables") else {}
    return parse_any_date(t0.get("date") or j.get("date"))


def _get(fx: Fetcher, url: str, base_params: dict, *, source: str, d: date):
    """回傳 (json, [RawRef])。先試西元，日期不符再試民國。"""
    refs, j = [], {}
    for fmt in (slash, roc_slash):
        resp = fx.get(url, {**base_params, "date": fmt(d), "response": "json"},
                      source=source, d=d, key=f"all_{'roc' if fmt is roc_slash else 'ad'}", ext="json")
        refs.append(resp.raw)
        j = _json(resp)
        if _served(j) == d:
            break
    return j, refs


def _table(j: dict) -> tuple[list[str], list[list], dict]:
    tables = j.get("tables") or []
    if not tables:
        return [], [], {}
    t = tables[0]
    return [str(f) for f in t.get("fields") or []], t.get("data") or [], t


def fetch_otc_quotes(fx: Fetcher, d: date) -> Parsed:
    j, refs = _get(fx, f"{BASE}/afterTrading/otc", {"type": "EW"}, source="tpex_otc", d=d)
    fields, rows, t = _table(j)
    if not rows:
        return Parsed("quotes", "TPEX", pd.DataFrame(), _served(j), no_data=True, raw=refs)
    fm = FieldMap(fields, QUOTE_SPEC)
    df = pd.DataFrame({"code": [norm_code(x) for x in fm.col(rows, "code")],
                       "name": [strip_tags(x) for x in fm.col(rows, "name")]})
    for c in ("open", "high", "low", "close", "change"):
        df[c] = float_series(fm.col(rows, c))
    for c in ("volume_shares", "value_twd", "trades"):
        df[c] = int_series(fm.col(rows, c))
    raw_chg = pd.Series([strip_tags(x) for x in fm.col(rows, "change")])
    df["ex_right"] = raw_chg.str.contains("除", na=False).astype("boolean").values
    df = df[df["code"].str.match(CODE_RE)].copy()
    df.insert(0, "market", "TPEX")
    df.insert(0, "date", d)
    return Parsed("quotes", "TPEX", df, _served(j), fields=fields, field_issues=fm.missing,
                  extras={"reported_total": t.get("totalCount"), "raw_rows": len(rows)}, raw=refs)


def fetch_daily_trade(fx: Fetcher, d: date) -> Parsed:
    j, refs = _get(fx, f"{BASE}/insti/dailyTrade", {"type": "Daily", "sect": "EW"}, source="tpex_daily_trade", d=d)
    fields, rows, t = _table(j)
    if not rows:
        return Parsed("inst_flows", "TPEX", pd.DataFrame(), _served(j), no_data=True, raw=refs)
    norm = [norm_field(f) for f in fields]
    issues = []
    if len(norm) != len(FLOW_FIELDS) or any(not a.endswith(b) for a, b in zip(norm, FLOW_FIELDS)):
        issues.append(f"dailyTrade 欄位序列不符預期：{norm}")
        return Parsed("inst_flows", "TPEX", pd.DataFrame(), _served(j), fields=fields, field_issues=issues, raw=refs)
    df = pd.DataFrame({"code": [norm_code(r[0]) for r in rows], "name": [strip_tags(r[1]) for r in rows]})
    for g, grp in enumerate(FLOW_GROUPS):
        base = 2 + 3 * g
        df[f"{grp}_buy"] = int_series([r[base] for r in rows])
        df[f"{grp}_sell"] = int_series([r[base + 1] for r in rows])
        df[f"{grp}_net"] = int_series([r[base + 2] for r in rows])
    df["total_net"] = int_series([r[23] for r in rows])
    df = df[df["code"].str.match(CODE_RE)].copy()
    df.insert(0, "market", "TPEX")
    df.insert(0, "date", d)
    return Parsed("inst_flows", "TPEX", df, _served(j), fields=fields, field_issues=issues,
                  extras={"reported_total": t.get("totalCount"), "raw_rows": len(rows)}, raw=refs)


def fetch_qfii(fx: Fetcher, d: date) -> Parsed:
    j, refs = _get(fx, f"{BASE}/insti/qfii", {}, source="tpex_qfii", d=d)
    fields, rows, t = _table(j)
    if not rows:
        return Parsed("foreign_holdings", "TPEX", pd.DataFrame(), _served(j), no_data=True, raw=refs)
    fm = FieldMap(fields, QFII_SPEC)
    df = pd.DataFrame({"code": [norm_code(x) for x in fm.col(rows, "code")],
                       "name": [strip_tags(x) for x in fm.col(rows, "name")]})
    for c in ("issued_shares", "foreign_available_shares", "foreign_shares"):
        df[c] = int_series(fm.col(rows, c))
    for c in ("available_ratio", "foreign_ratio", "limit_ratio"):  # 值帶「%」，to_number 會去掉
        df[c] = float_series(fm.col(rows, c))
    df = df[df["code"].str.match(CODE_RE)].copy()
    df.insert(0, "market", "TPEX")
    df.insert(0, "date", d)
    return Parsed("foreign_holdings", "TPEX", df, _served(j), fields=fields, field_issues=fm.missing,
                  extras={"reported_total": t.get("totalCount"), "raw_rows": len(rows)}, raw=refs)
