"""舊專案（tw-institutional-stocker）資料的匯入與比對。

舊資料不直接當真值：匯入 legacy/ 保留出處與品質旗標，之後與官方重抓逐列比對。
來源是先前整理的封存庫 tw-inst-archive（30_curated 與 manifest.json）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .config import Config
from .sources.fubon import decode_b
from .store import Store


def import_archive(cfg: Config, archive: Path) -> dict[str, int]:
    archive = Path(archive)
    man = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    head = man.get("source_refs", {}).get("refs/heads/main", "")[:9]
    out_dir = cfg.legacy_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name in ("flows", "foreign_holdings", "broker_trades"):
        df = pd.read_parquet(archive / "30_curated" / name)  # hive 分區自動帶回 market/year
        df = df.drop(columns=[c for c in ("year",) if c in df.columns])
        if "market" in df.columns:
            df["market"] = df["market"].astype(str)
        df["legacy_source"] = f"tw-institutional-stocker@{head}"
        if name == "broker_trades":
            df["bhid"] = df["broker_id"].map(decode_b)
        p = out_dir / f"{name}.parquet"
        df.to_parquet(p, index=False, compression="zstd")
        counts[name] = len(df)
    (out_dir / "PROVENANCE.json").write_text(json.dumps({
        "source_repo": man.get("source_repo"), "source_refs": man.get("source_refs"),
        "archive_built_at": man.get("built_at"), "files": counts,
        "note": "quality_action ∈ {keep, recompute} 為舊封存庫判定可用；其餘列僅供稽核。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return counts


def compare(cfg: Config) -> pd.DataFrame:
    """逐日比對 legacy 與已驗證的官方資料。回傳每 (資料集, 市場, 日期) 的比對統計。"""
    store, rows = Store(cfg), []
    specs = {
        "flows": ("inst_flows", ["date", "market", "code"],
                  {"foreign_net": "foreign_net", "trust_net": "trust_net", "dealer_net": "dealer_net"}),
        "foreign_holdings": ("foreign_holdings", ["date", "market", "code"],
                             {"foreign_shares": "foreign_shares", "total_shares": "issued_shares"}),
        "broker_trades": ("broker_top", ["date", "code", "side", "rank"],
                          {"net_lots": "net_vol", "buy_lots": "buy_vol", "sell_lots": "sell_vol", "bhid": "bhid"}),
    }
    for lname, (ds, keys, colmap) in specs.items():
        p = cfg.legacy_dir / f"{lname}.parquet"
        if not p.exists():
            continue
        leg = pd.read_parquet(p)
        if lname == "broker_trades":
            leg = leg.rename(columns={"trade_date": "date", "stock_code": "code"})
            leg["market"] = "ALL"
        leg["date"] = pd.to_datetime(leg["date"]).dt.date
        usable = leg["quality_action"].isin(["keep", "recompute"]) if "quality_action" in leg else \
            leg.get("quality_flags", pd.Series("", index=leg.index)).fillna("").eq("")
        off = store.read_range(ds)
        if off.empty:
            continue
        o = off[keys + list(colmap)].rename(columns={c: f"{c}__off" for c in colmap})
        lg = leg.loc[usable, keys + list(colmap.values())].rename(
            columns={lc: f"{oc}__leg" for oc, lc in colmap.items()})
        for k in keys:
            o[k], lg[k] = o[k].astype(str), lg[k].astype(str)
        j = o.merge(lg, on=keys, how="outer", indicator=True)
        # 只比對兩邊都有資料的日期，避免「官方尚未回補」被誤算成差異
        common = set(o["date"]) & set(lg["date"])
        j = j[j["date"].isin(common)]
        diff = pd.Series(False, index=j.index)
        for oc in colmap:
            a = j[f"{oc}__off"].astype("string").str.replace(r"\.0$", "", regex=True)
            b = j[f"{oc}__leg"].astype("string").str.replace(r"\.0$", "", regex=True)
            diff |= (a != b).fillna(True) & (j["_merge"] == "both")
        j["diff"] = diff
        g = j.groupby(["market", "date"]).agg(
            both=("_merge", lambda s: int((s == "both").sum())),
            only_official=("_merge", lambda s: int((s == "left_only").sum())),
            only_legacy=("_merge", lambda s: int((s == "right_only").sum())),
            mismatched=("diff", "sum")).reset_index()
        g.insert(0, "dataset", ds)
        rows.append(g)
    res = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if not res.empty:
        p = cfg.quality_dir / "legacy_compare.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        res.to_parquet(p, index=False)
    return res
