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
    store, rows, samples, shift_stats = Store(cfg), [], [], {}
    specs = {
        "flows": ("inst_flows", ["date", "market", "code"],
                  {"foreign_net": "foreign_net", "trust_net": "trust_net", "dealer_net": "dealer_net"}),
        # 對照表的方向一律是 {官方欄位: 舊資料欄位}
        "foreign_holdings": ("foreign_holdings", ["date", "market", "code"],
                             {"foreign_shares": "foreign_shares", "issued_shares": "total_shares"}),
        "broker_trades": ("broker_top", ["date", "market", "code", "side", "rank"],
                          # 分點代號要比 branch（富邦連結的 b 參數）。官方的 bhid 欄是 BHID 參數＝券商總公司代號，
                          # 舊資料的 bhid 是由 b 參數解碼而來＝分點代號，兩者意義不同，不能直接比。
                          {"net_lots": "net_vol", "buy_lots": "buy_vol", "sell_lots": "sell_vol", "branch": "bhid"}),
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
        if "market" not in off.columns:   # 券商分點不分市場，兩邊都標為 ALL
            off["market"] = "ALL"
        if ds == "broker_top":
            off["branch"] = off["broker_b"].fillna("").map(decode_b)
        if ds == "inst_flows":
            # 舊專案的 foreign_net 是「外資合計」（含外資自營商）。上市官方沒有合計欄（NULL），用 fx + fd 推得。
            if {"fx_net", "fd_net"} <= set(off.columns):
                tot = off["fx_net"] + off["fd_net"]
                off["foreign_net"] = off["foreign_net"].fillna(tot) if "foreign_net" in off.columns else tot
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
        relabel_col = "branch" if ds == "broker_top" else None
        for oc in colmap:
            a = j[f"{oc}__off"].astype("string").str.replace(r"\.0$", "", regex=True)
            b = j[f"{oc}__leg"].astype("string").str.replace(r"\.0$", "", regex=True)
            d_col = (a != b).fillna(True) & (j["_merge"] == "both")
            j[f"mm_{oc}"] = d_col            # 每個欄位各自的不符列數，方便追查原因
            if oc != relabel_col:
                diff |= d_col
        j["diff"] = diff
        if relabel_col:
            # 張數完全相同、只有分點代號不同：券商合併後代號重編（例：元富 5920 → 台新 9B20），不算數值差異
            j["branch_relabel"] = j[f"mm_{relabel_col}"] & ~diff
        agg = {"both": ("_merge", lambda s: int((s == "both").sum())),
               "only_official": ("_merge", lambda s: int((s == "left_only").sum())),
               "only_legacy": ("_merge", lambda s: int((s == "right_only").sum())),
               "mismatched": ("diff", "sum")}
        agg.update({f"mm_{oc}": (f"mm_{oc}", "sum") for oc in colmap})
        if relabel_col:
            agg["branch_relabel"] = ("branch_relabel", "sum")
        g = j.groupby(["market", "date"]).agg(**agg).reset_index()
        g.insert(0, "dataset", ds)
        rows.append(g)
        shift_stats[ds] = _shift_explain(o, j[j["diff"]], keys, colmap)
        bad = j[j["diff"] | (j["_merge"] == "right_only") | j.get("branch_relabel", False)].copy()
        bad = bad.sort_values("_merge")          # 數值不符（both）排前面，只有舊資料有的排後面
        bad.insert(0, "dataset", ds)
        samples.append(bad.drop(columns=[c for c in bad.columns if c.startswith("mm_") or c in ("diff", "branch_relabel")])
                       .astype(str))
    res = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if not res.empty:
        p = cfg.quality_dir / "legacy_compare.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        res.to_parquet(p, index=False)
        if samples:   # 全部不符與只有舊資料有的列，供逐筆追查
            pd.concat(samples, ignore_index=True).to_parquet(cfg.quality_dir / "legacy_compare_rows.parquet",
                                                             index=False)
        (cfg.quality_dir / "legacy_compare_shift.json").write_text(
            json.dumps(shift_stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def _shift_explain(o: pd.DataFrame, mism: pd.DataFrame, keys: list[str], colmap: dict) -> dict:
    """不符的列中，舊資料的值是否其實等於官方「前後幾個交易日」的值？等於的話，代表舊資料日期錯位。
    回傳 {"n": 不符列數, "k=-1": 舊資料 D 等於官方 D-1 的比例, ...}。"""
    out = {"n": int(len(mism))}
    if mism.empty:
        return out
    days = sorted(set(o["date"]))
    idx = {d: i for i, d in enumerate(days)}
    other = [k for k in keys if k != "date"]
    leg_cols = [f"{c}__leg" for c in colmap]
    m = mism[keys + leg_cols].copy()
    for k in (-2, -1, 1, 2):
        sh = o.copy()
        # 官方 D+k 的值，對到日期 D
        sh["date"] = sh["date"].map(lambda x: days[idx[x] - k] if 0 <= idx[x] - k < len(days) else None)
        sh = sh.dropna(subset=["date"])
        jj = m.merge(sh, on=["date"] + other, how="inner")
        eq = pd.Series(True, index=jj.index)
        for c in colmap:
            a = jj[f"{c}__off"].astype("string").str.replace(r"\.0$", "", regex=True)
            b = jj[f"{c}__leg"].astype("string").str.replace(r"\.0$", "", regex=True)
            eq &= (a == b).fillna(False)
        out[f"k={k:+d}"] = round(float(eq.sum()) / len(m), 4)
    return out
