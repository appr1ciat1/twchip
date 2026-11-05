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
            # 一律從原始 broker_id 重新解碼（區分大小寫），不依賴匯入時存下的 bhid
            leg["bhid"] = leg["broker_id"].fillna("").astype(str).map(decode_b)
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
        if ds == "broker_top":
            shift_stats["broker_top_by_branch"] = _broker_by_branch(off, leg.loc[usable], j)
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


def _broker_by_branch(off: pd.DataFrame, leg: pd.DataFrame, j: pd.DataFrame) -> dict:
    """券商分點改以「(日期, 股票, 方向, 分點)」比對，並套用由資料推得的代號重編對照。

    依名次比對時，券商合併會讓兩個舊分點併成一個新分點（例：元富 5920 與台新 9B00 的部分歷史 → 9B20），
    名次因此整串位移，造成大量假差異。這裡改用分點當鍵。

    重編對照的推得規則（不看名稱，只看資料）：
    1. 候選：依名次對齊時，張數完全相同、只有分點代號不同的列。
    2. 每個舊代號取最常對到的新代號；需出現 ≥ 5 次，且佔該舊代號候選的 ≥ 90%。
    3. 舊代號若仍出現在官方資料中（代號沒被淘汰），不視為重編。
    """
    off, leg = off.copy(), leg.copy()
    both = j[j["_merge"] == "both"]
    same = pd.Series(True, index=both.index)
    for c in ("buy_lots", "sell_lots", "net_lots"):
        same &= both[f"{c}__off"].astype("string") == both[f"{c}__leg"].astype("string")
    cand = both[same & (both["branch__off"].astype(str) != both["branch__leg"].astype(str))]
    alive = set(off["branch"].astype(str))
    mapping = {}
    for old, g in cand.groupby("branch__leg"):
        vc = g["branch__off"].astype(str).value_counts()
        if old not in alive and vc.iloc[0] >= 5 and vc.iloc[0] / vc.sum() >= 0.9:
            mapping[str(old)] = vc.index[0]
    lg = leg.copy()
    lg["branch"] = lg["bhid"].astype(str).map(lambda c: mapping.get(c, c))
    for d in (lg, off):
        d["date"] = d["date"].astype(str)
        d["code"] = d["code"].astype(str)
    k = ["date", "code", "side", "branch"]
    la = lg.groupby(k, as_index=False).agg(buy_leg=("buy_vol", "sum"), sell_leg=("sell_vol", "sum"))
    common = set(off["date"]) & set(la["date"])
    oo = off[off["date"].isin(common)][k + ["buy_lots", "sell_lots"]]
    la = la[la["date"].isin(common)]
    m = oo.merge(la, on=k, how="outer", indicator=True)
    bb = m[m["_merge"] == "both"]
    eq = (bb["buy_lots"].astype("Int64") == bb["buy_leg"].astype("Int64")) & \
         (bb["sell_lots"].astype("Int64") == bb["sell_leg"].astype("Int64"))
    bad = bb[~eq.fillna(False)]
    return {"relabel_map_size": len(mapping), "relabel_map_sample": dict(list(sorted(mapping.items()))[:12]),
            "both": int(len(bb)), "equal": int(eq.fillna(False).sum()), "unequal": int(len(bad)),
            "unequal_dates": bad["date"].value_counts().head(10).to_dict(),
            "only_official": int((m["_merge"] == "left_only").sum()),
            "only_legacy": int((m["_merge"] == "right_only").sum())}
