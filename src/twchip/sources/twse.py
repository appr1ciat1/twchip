"""臺灣證券交易所（上市）資料源。全部使用 rwd JSON 端點，欄位依名稱對應。"""
from __future__ import annotations

import json
import re
from datetime import date

import pandas as pd

from ..http import Fetcher
from ..util import float_series, int_series, norm_code, parse_any_date, parse_compact_date, strip_tags, ymd
from . import CODE_RE, FieldMap, Parsed

BASE = "https://www.twse.com.tw/rwd/zh"

T86_SPEC = {
    "code": ["證券代號"], "name": ["證券名稱"],
    "fx_buy": ["外陸資買進股數(不含外資自營商)", "外資及陸資(不含外資自營商)買進股數", "外資及陸資買進股數(不含外資自營商)"],
    "fx_sell": ["外陸資賣出股數(不含外資自營商)", "外資及陸資(不含外資自營商)賣出股數", "外資及陸資賣出股數(不含外資自營商)"],
    "fx_net": ["外陸資買賣超股數(不含外資自營商)", "外資及陸資(不含外資自營商)買賣超股數", "外資及陸資買賣超股數(不含外資自營商)"],
    "fd_buy": ["外資自營商買進股數"], "fd_sell": ["外資自營商賣出股數"], "fd_net": ["外資自營商買賣超股數"],
    "trust_buy": ["投信買進股數"], "trust_sell": ["投信賣出股數"], "trust_net": ["投信買賣超股數"],
    "dealer_net": ["自營商買賣超股數"],
    "ds_buy": ["自營商買進股數(自行買賣)"], "ds_sell": ["自營商賣出股數(自行買賣)"], "ds_net": ["自營商買賣超股數(自行買賣)"],
    "dh_buy": ["自營商買進股數(避險)"], "dh_sell": ["自營商賣出股數(避險)"], "dh_net": ["自營商買賣超股數(避險)"],
    "total_net": ["三大法人買賣超股數"],
}

QFIIS_SPEC = {
    "code": ["證券代號"], "name": ["證券名稱"],
    "issued_shares": ["發行股數"],
    "foreign_available_shares": ["外資及陸資尚可投資股數"],
    "foreign_shares": ["全體外資及陸資持有股數"],
    "available_ratio": ["外資及陸資尚可投資比率"],
    "foreign_ratio": ["全體外資及陸資持股比率"],
    "limit_ratio": ["外資及陸資共用法令投資上限比率", "外資及陸資法令投資上限比率"],
}

QUOTE_SPEC = {
    "code": ["證券代號"], "name": ["證券名稱"],
    "volume_shares": ["成交股數"], "trades": ["成交筆數"], "value_twd": ["成交金額"],
    "open": ["開盤價"], "high": ["最高價"], "low": ["最低價"], "close": ["收盤價"],
    "sign": ["漲跌(+/-)"], "change_abs": ["漲跌價差"],
}


BLOCK_SPEC = {
    "code": ["證券代號"], "name": ["證券名稱"], "trade_type": ["交易別"],
    "price": ["成交價"], "volume_shares": ["成交股數"], "value_twd": ["成交金額"],
}


def _json(resp) -> dict:
    try:
        return json.loads(resp.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"stat": "INVALID_RESPONSE", "_text": resp.content[:200].decode("utf-8", "ignore")}


def _no_data(dataset: str, d: date, j: dict, resp) -> Parsed:
    return Parsed(dataset, "TWSE", pd.DataFrame(), parse_compact_date(j.get("date")), no_data=True,
                  extras={"stat": j.get("stat")}, raw=[resp.raw])


def _rows(j: dict) -> tuple[list[str], list[list]]:
    return [str(f) for f in j.get("fields") or []], j.get("data") or []


def fetch_t86(fx: Fetcher, d: date) -> Parsed:
    resp = fx.get(f"{BASE}/fund/T86", {"date": ymd(d), "selectType": "ALLBUT0999", "response": "json"},
                  source="twse_t86", d=d, key="all", ext="json")
    j = _json(resp)
    if j.get("stat") != "OK":
        return _no_data("inst_flows", d, j, resp)
    fields, rows = _rows(j)
    if not rows:
        return _no_data("inst_flows", d, j, resp)
    fm = FieldMap(fields, T86_SPEC)
    df = pd.DataFrame({"code": [norm_code(x) for x in fm.col(rows, "code")],
                       "name": [strip_tags(x) for x in fm.col(rows, "name")]})
    for c in T86_SPEC:
        if c not in ("code", "name"):
            df[c] = int_series(fm.col(rows, c))
    df = df[df["code"].str.match(CODE_RE)].copy()
    df.insert(0, "market", "TWSE")
    df.insert(0, "date", d)
    # TWSE 沒有外資合計欄：外資買賣超 = 不含自營 + 自營（官方定義），買進／賣出同理。
    df["foreign_net"] = df["fx_net"] + df["fd_net"]
    df["foreign_buy"] = df["fx_buy"] + df["fd_buy"]
    df["foreign_sell"] = df["fx_sell"] + df["fd_sell"]
    df["dealer_buy"] = df["ds_buy"] + df["dh_buy"]
    df["dealer_sell"] = df["ds_sell"] + df["dh_sell"]
    return Parsed("inst_flows", "TWSE", df, parse_compact_date(j.get("date")), fields=fields,
                  field_issues=fm.missing, extras={"title": j.get("title"), "derived": ["foreign_*", "dealer_buy", "dealer_sell"]},
                  raw=[resp.raw])


def fetch_mi_qfiis(fx: Fetcher, d: date) -> Parsed:
    resp = fx.get(f"{BASE}/fund/MI_QFIIS", {"date": ymd(d), "selectType": "ALLBUT0999", "response": "json"},
                  source="twse_mi_qfiis", d=d, key="all", ext="json")
    j = _json(resp)
    if j.get("stat") != "OK":
        return _no_data("foreign_holdings", d, j, resp)
    fields, rows = _rows(j)
    if not rows:
        return _no_data("foreign_holdings", d, j, resp)
    fm = FieldMap(fields, QFIIS_SPEC)
    df = pd.DataFrame({"code": [norm_code(x) for x in fm.col(rows, "code")],
                       "name": [strip_tags(x) for x in fm.col(rows, "name")]})
    for c in ("issued_shares", "foreign_available_shares", "foreign_shares"):
        df[c] = int_series(fm.col(rows, c))
    for c in ("available_ratio", "foreign_ratio", "limit_ratio"):
        df[c] = float_series(fm.col(rows, c))
    df = df[df["code"].str.match(CODE_RE)].copy()
    df.insert(0, "market", "TWSE")
    df.insert(0, "date", d)
    return Parsed("foreign_holdings", "TWSE", df, parse_compact_date(j.get("date")), fields=fields,
                  field_issues=fm.missing, extras={"title": j.get("title")}, raw=[resp.raw])


def fetch_mi_index(fx: Fetcher, d: date) -> Parsed:
    resp = fx.get(f"{BASE}/afterTrading/MI_INDEX", {"date": ymd(d), "type": "ALLBUT0999", "response": "json"},
                  source="twse_mi_index", d=d, key="all", ext="json")
    j = _json(resp)
    if j.get("stat") != "OK":
        return _no_data("quotes", d, j, resp)
    tables = j.get("tables") or []
    table = next((t for t in tables if "每日收盤行情" in str(t.get("title", ""))), None)
    if table is None or not table.get("data"):
        return _no_data("quotes", d, j, resp)
    fields, rows = [str(f) for f in table.get("fields") or []], table["data"]
    fm = FieldMap(fields, QUOTE_SPEC)
    df = pd.DataFrame({"code": [norm_code(x) for x in fm.col(rows, "code")],
                       "name": [strip_tags(x) for x in fm.col(rows, "name")]})
    for c in ("open", "high", "low", "close"):
        df[c] = float_series(fm.col(rows, c))
    for c in ("volume_shares", "trades", "value_twd"):
        df[c] = int_series(fm.col(rows, c))
    sign = pd.Series([strip_tags(x) for x in fm.col(rows, "sign")])
    absv = float_series(fm.col(rows, "change_abs"))
    df["ex_right"] = (sign.str.upper() == "X").astype("boolean").values
    chg = absv.where(sign.values != "-", -absv)
    df["change"] = chg.where(~df["ex_right"].values).values
    df = df[df["code"].str.match(CODE_RE)].copy()
    df.insert(0, "market", "TWSE")
    df.insert(0, "date", d)
    return Parsed("quotes", "TWSE", df, parse_compact_date(j.get("date")), fields=fields,
                  field_issues=fm.missing, extras={"title": table.get("title")}, raw=[resp.raw])


def fetch_holidays(fx: Fetcher, year: int) -> pd.DataFrame:
    """證交所年度休市表。回傳 date, name, description；分類交給 calendar 模組。"""
    resp = fx.get(f"{BASE}/holidaySchedule/holidaySchedule", {"response": "json", "queryYear": year},
                  source="twse_holiday", d=date(year, 1, 1), key=str(year), ext="json")
    j = _json(resp)
    out = []
    for row in j.get("data") or []:
        dd = parse_any_date(row[0]) if row else None
        if dd is None:
            continue
        out.append({"date": dd, "name": strip_tags(row[1]) if len(row) > 1 else "",
                    "description": strip_tags(row[2]) if len(row) > 2 else ""})
    return pd.DataFrame(out, columns=["date", "name", "description"])


def fetch_block_trades(fx: Fetcher, d: date) -> Parsed:
    """鉅額交易日成交資訊（單一證券）。selectType 實測不影響回應（2026-11-03）。"""
    resp = fx.get(f"{BASE}/block/BFIAUU", {"date": ymd(d), "selectType": "S", "response": "json"},
                  source="twse_bfiauu", d=d, key="single", ext="json")
    j = _json(resp)
    tables = j.get("tables") or ([j] if j.get("fields") else [])
    table = next((t for t in tables if "鉅額交易" in str(t.get("title", "")) and t.get("fields")), None)
    if j.get("stat") != "OK" or table is None:
        return _no_data("block_trades", d, j, resp)
    fields, rows = [str(f) for f in table.get("fields") or []], table.get("data") or []
    fm = FieldMap(fields, BLOCK_SPEC)
    df = pd.DataFrame({"code": [norm_code(x) for x in fm.col(rows, "code")],
                       "name": [strip_tags(x) for x in fm.col(rows, "name")],
                       "trade_type": [strip_tags(x) for x in fm.col(rows, "trade_type")]})
    df["price"] = float_series(fm.col(rows, "price"))
    for c in ("volume_shares", "value_twd"):
        df[c] = int_series(fm.col(rows, c))
    df.insert(0, "seq", range(1, len(df) + 1))
    df.insert(0, "market", "TWSE")
    df.insert(0, "date", d)
    served = parse_compact_date(j.get("date"))
    if served is None:  # 沒有 date 欄位時，改用標題中的民國日期（例：114年09月30日 鉅額交易日成交資訊-單一證券）
        mt = re.search(r"\d{2,3}年\d{1,2}月\d{1,2}日", str(table.get("title", "")))
        served = parse_any_date(mt.group()) if mt else None
    return Parsed("block_trades", "TWSE", df, served, fields=fields,
                  field_issues=fm.missing, extras={"title": table.get("title")}, raw=[resp.raw])
