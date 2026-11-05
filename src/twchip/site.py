"""產生 GitHub Pages 靜態網站：把已驗證的 curated 資料預先算成 JSON，前端只負責顯示。

輸出結構（相對於 out_dir）：
  index.html, app.js, style.css
  data/meta.json          最新日、交易日清單、視窗、建置時間
  data/stocks.json        代號、名稱、市場（搜尋用）
  data/rank_{w}.json      w 日累計三大法人買賣超（張）＋外資持股變化＋區間漲跌
  data/stock/{code}.json  個股時序與券商分點
  data/broker/index.json, data/broker/{分點鍵}.json（以分點分組；鍵見 fubon.branch_key）
  data/health.json        每日發布狀態、檢查通過率、近期失敗

只讀 curated（通過驗證的資料）。數字單位一律在 JSON 的 fields 中標明。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from importlib import resources
from pathlib import Path

import duckdb
import pandas as pd

from .config import TPE, Config
from .sources.fubon import branch_id, branch_key

WINDOWS = [1, 5, 10, 20, 60]
QUALITY_DAYS = 60


def _dump(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")


def _num(x, nd=None):
    if x is None or (isinstance(x, float) and pd.isna(x)) or x is pd.NA:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return round(float(x), nd) if nd is not None else int(round(float(x)))


def _df(con, sql: str) -> pd.DataFrame:
    """查詢結果的 date 欄一律轉成 'YYYY-MM-DD' 字串，避免 Timestamp 帶出時間。"""
    df = con.sql(sql).df()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df


def _has(cur: Path, ds: str) -> bool:
    return (cur / ds).exists() and any((cur / ds).glob("*/*/*.parquet"))


def _views(con, cur: Path) -> set[str]:
    have = set()
    for ds in ("quotes", "inst_flows", "foreign_holdings", "broker_top", "broker_top_summary"):
        if _has(cur, ds):
            g = str(cur / ds / "*" / "*" / "*.parquet")
            # market 由路徑帶入（broker_top 的 market 目錄是 ALL）
            con.execute(f"""create or replace view {ds} as select * replace (cast(date as date) as date)
                            from read_parquet('{g}', union_by_name=true, filename=true)""")
            have.add(ds)
    return have


def build(cfg: Config, out: Path, include_broker: bool = True) -> dict:
    out = Path(out)
    cur = cfg.curated_dir
    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)
    tpl = resources.files("twchip") / "site_template"
    for name in ("index.html", "app.js", "style.css"):
        (out / name).write_bytes((tpl / name).read_bytes())
    (out / ".nojekyll").write_text("")

    con = duckdb.connect()
    have = _views(con, cur)
    stats = {"stocks": 0, "brokers": 0}
    if "quotes" not in have:
        _dump({"built_at": datetime.now(TPE).isoformat(), "empty": True}, out / "data" / "meta.json")
        _dump(_health(cfg), out / "data" / "health.json")
        return stats

    days = [d for (d,) in con.sql("select distinct date from quotes order by 1").fetchall()]
    flow_days = [d for (d,) in con.sql("select distinct date from inst_flows order by 1").fetchall()] \
        if "inst_flows" in have else []
    latest = flow_days[-1] if flow_days else days[-1]

    # ---------- 股票清單：近 60 個交易日出現過的代號，名稱取最新
    since = days[max(0, len(days) - 60)]
    stocks = con.sql(f"""select code, arg_max(name, date) name, arg_max(market, date) market
                         from quotes where date >= '{since}' group by code order by code""").df()
    _dump({"fields": ["code", "name", "market"], "rows": stocks.values.tolist()}, out / "data" / "stocks.json")

    # ---------- 排行
    windows = [w for w in WINDOWS if w <= len(flow_days)]
    for w in windows:
        win = flow_days[-w:]
        d0, d1 = win[0], win[-1]
        prev = days[days.index(d0) - 1] if days.index(d0) > 0 else None
        fh = "foreign_holdings" in have
        con.register("stocks_df", stocks)
        sql = f"""
            with f as (
              select code, market,
                     sum(coalesce(foreign_net, fx_net + fd_net)) fn, sum(trust_net) tn,
                     sum(dealer_net) dn, sum(total_net) tot
              from inst_flows where date between '{d0}' and '{d1}' group by code, market),
            q1 as (select code, market, close c1, name from quotes where date = '{d1}'),
            q0 as (select code, market, close c0 from quotes where date = '{prev}')
            {", h1 as (select code, market, foreign_ratio r1 from foreign_holdings where date = '" + str(d1) + "')" if fh else ""}
            {", h0 as (select code, market, foreign_ratio r0 from foreign_holdings where date = '" + str(prev) + "')" if fh and prev else ""}
            select f.code, coalesce(q1.name, s.name) name, f.market,
                   round(fn/1000) foreign_lots, round(tn/1000) trust_lots, round(dn/1000) dealer_lots,
                   round(tot/1000) total_lots, q1.c1 close,
                   case when q0.c0 > 0 then round((q1.c1/q0.c0 - 1)*100, 2) end chg_pct,
                   {"h1.r1" if fh else "null"} foreign_ratio,
                   {"round(h1.r1 - h0.r0, 2)" if fh and prev else "null"} foreign_ratio_chg
            from f
            left join q1 using (code, market) left join q0 using (code, market)
            {"left join h1 using (code, market)" if fh else ""}
            {"left join h0 using (code, market)" if fh and prev else ""}
            left join stocks_df s using (code)
        """
        df = con.sql(sql).df()
        rows = [[r.code, r.name, r.market, _num(r.foreign_lots), _num(r.trust_lots), _num(r.dealer_lots),
                 _num(r.total_lots), _num(r.close, 2), _num(r.chg_pct, 2), _num(r.foreign_ratio, 2),
                 _num(r.foreign_ratio_chg, 2)] for r in df.itertuples()]
        _dump({"window": w, "start": d0, "end": d1, "trading_days": len(win), "unit": "張",
               "fields": ["code", "name", "market", "foreign", "trust", "dealer", "total", "close",
                          "chg_pct", "foreign_ratio", "foreign_ratio_chg"], "rows": rows},
              out / "data" / f"rank_{w}.json")

    # ---------- 個股
    q = _df(con, "select code, market, date, close, volume_shares from quotes order by code, date")
    fl = _df(con, """select code, market, date, coalesce(foreign_net, fx_net + fd_net) f, trust_net t,
                    dealer_net d from inst_flows""") if "inst_flows" in have else pd.DataFrame()
    hd = _df(con, "select code, market, date, foreign_ratio r from foreign_holdings") \
        if "foreign_holdings" in have else pd.DataFrame()
    m = q
    if not fl.empty:
        m = m.merge(fl, on=["code", "market", "date"], how="left")
    if not hd.empty:
        m = m.merge(hd, on=["code", "market", "date"], how="left")
    bt = _df(con, "select * from broker_top order by date, code, side, rank") \
        if include_broker and "broker_top" in have else pd.DataFrame()
    if not bt.empty:
        bt["branch"] = [branch_id(b, h) for b, h in zip(bt["broker_b"].fillna(""), bt["bhid"].fillna(""))]
        bt["key"] = bt["branch"].map(branch_key)
    bt_by = dict(tuple(bt.groupby("code"))) if not bt.empty else {}
    names = dict(zip(stocks["code"], stocks["name"]))
    keep = set(stocks["code"])
    for (code, market), g in m.groupby(["code", "market"], sort=False):
        if code not in keep:
            continue
        g = g.sort_values("date")
        doc = {"code": code, "name": names.get(code, ""), "market": market,
               "date": [str(x) for x in g["date"]],
               "close": [_num(x, 2) for x in g["close"]],
               "vol_lots": [_num(x / 1000) if pd.notna(x) else None for x in g["volume_shares"]],
               "foreign": [_num(x / 1000) if pd.notna(x) else None for x in g.get("f", pd.Series(dtype=float))] if "f" in g else [],
               "trust": [_num(x / 1000) if pd.notna(x) else None for x in g["t"]] if "t" in g else [],
               "dealer": [_num(x / 1000) if pd.notna(x) else None for x in g["d"]] if "d" in g else [],
               "foreign_ratio": [_num(x, 2) for x in g["r"]] if "r" in g else []}
        if code in bt_by:
            b = bt_by[code]
            recent = sorted(b["date"].unique())[-20:]
            doc["broker"] = {
                "fields": ["rank", "broker_name", "key", "buy_lots", "sell_lots", "net_lots", "pct"],
                "days": {str(d): {s: b[(b["date"] == d) & (b["side"] == s)][
                    ["rank", "broker_name", "key", "buy_lots", "sell_lots", "net_lots", "pct"]]
                    .astype(object).where(lambda x: x.notna(), None).values.tolist() for s in ("buy", "sell")}
                    for d in recent}}
        _dump(doc, out / "data" / "stock" / f"{code}.json")
        stats["stocks"] += 1

    # ---------- 券商分點：以「分點」分組（富邦連結的 b 參數，區分大小寫），不是以總公司（BHID）分組
    if not bt.empty:
        idx = bt.groupby("branch").agg(hq=("bhid", "last"), name=("broker_name", "last"),
                                       names=("broker_name", lambda s: sorted(set(s))),
                                       appearances=("rank", "size"), stocks=("code", "nunique"),
                                       net=("net_lots", "sum"), first=("date", "min"), last=("date", "max")).reset_index()
        idx = idx[idx["branch"].astype(str) != ""]
        _dump({"fields": ["key", "branch", "hq", "name", "aliases", "appearances", "stocks", "net_lots", "first", "last"],
               "tracked_codes": cfg.broker_codes,
               "rows": [[branch_key(r.branch), r.branch, r.hq, r.name, [n for n in r.names if n != r.name],
                         int(r.appearances), int(r.stocks), int(r.net), str(r.first), str(r.last)]
                        for r in idx.itertuples()]},
              out / "data" / "broker" / "index.json")
        for br, g in bt.groupby("branch"):
            if not str(br):
                continue
            _dump({"branch": br, "fields": ["date", "code", "name", "side", "rank", "net_lots", "pct"],
                   "rows": [[str(r.date), r.code, names.get(r.code, ""), r.side, int(r.rank), _num(r.net_lots),
                             _num(r.pct, 2)] for r in g.sort_values(["date", "code"]).itertuples()]},
                  out / "data" / "broker" / f"{branch_key(br)}.json")
            stats["brokers"] += 1

    _dump({"built_at": datetime.now(TPE).isoformat(), "latest": latest, "first": days[0],
           "trading_days": len(days), "windows": windows, "broker": bool(stats["brokers"]),
           "broker_codes": cfg.broker_codes}, out / "data" / "meta.json")
    _dump(_health(cfg), out / "data" / "health.json")
    return stats


def _health(cfg: Config) -> dict:
    files = sorted(cfg.quality_dir.glob("*/*.json"))[-QUALITY_DAYS:] if cfg.quality_dir.exists() else []
    days, checks, fails = [], {}, []
    for p in files:
        j = json.loads(p.read_text(encoding="utf-8"))
        units = j.get("units") or {}
        days.append({"date": j.get("date"), "calendar": (j.get("calendar") or {}).get("status", ""),
                     "units": {k: v.get("status", "") for k, v in units.items()}})
        for unit, u in units.items():
            lists = [(unit, u.get("checks", []))] + [(f"{unit}:{c}", s.get("checks", []))
                                                     for c, s in (u.get("stocks") or {}).items()]
            for name, cl in lists:
                for c in cl:
                    k = c["id"]
                    st = checks.setdefault(k, {"id": k, "name": c["name"], "level": c["level"],
                                               "pass": 0, "fail": 0, "skip": 0})
                    st[c["status"]] = st.get(c["status"], 0) + 1
                    if c["status"] == "fail":
                        fails.append([j.get("date"), name, c["id"], c["level"], c["detail"]])
    qdir = cfg.quarantine_dir
    quarantine = sorted(str(p.relative_to(qdir)) for p in qdir.rglob("*.parquet")) if qdir.exists() else []
    lc = cfg.quality_dir / "legacy_compare.parquet"
    legacy = None
    if lc.exists():
        x = pd.read_parquet(lc).groupby("dataset")[["both", "mismatched", "only_official", "only_legacy"]].sum()
        legacy = {k: {c: int(v) for c, v in row.items()} for k, row in x.iterrows()}
    return {"days": days[::-1], "checks": sorted(checks.values(), key=lambda c: c["id"]),
            "fails": sorted(fails, reverse=True)[:300], "quarantine": quarantine, "legacy": legacy}
