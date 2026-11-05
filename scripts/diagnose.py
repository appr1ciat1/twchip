"""回補校正後的三項診斷。每一項都用官方原始資料回答一個具體問題。

  A. 2026-09-18 上櫃三大法人為什麼被 F4 隔離？是他日資料，還是當天真的有異常？
  B. 上市 H5（發行股數變動檔數）每天 5~7%：是不是 ETF 每日申購買回造成的？
  C. T5 大幅偏離：官方成交股數是否包含鉅額交易與盤後定價，而富邦的分母不含？
  D. C 之後仍無法解釋的 4 筆：是否來自「證券組合」鉅額交易？盤後定價的數量單位是股還是張？
     （D 要指定才會跑：python scripts/diagnose.py ~/twchip-data D）
  E. 各官方來源中，哪些證券代號不符合本專案的代號格式（CODE_RE）而被略過？
     （E 要指定才會跑：python scripts/diagnose.py ~/twchip-data E [YYYY-MM-DD ...]）

用法：python scripts/diagnose.py [資料目錄，預設 ~/twchip-data] [A|B|C ...]
C 會向證交所抓鉅額交易與盤後定價（每個日期 2 次請求，間隔 3.2 秒），約 2~3 分鐘。
原始回應照常存進 資料目錄/raw。
"""
import json
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ARGS = [a for a in sys.argv[1:] if a not in ("A", "B", "C", "D", "E")]
D = Path(os.path.expanduser(ARGS[0] if ARGS else "~/twchip-data"))
PARTS = [a for a in sys.argv[1:] if a in ("A", "B", "C", "D", "E")] or ["A", "B", "C"]
os.environ.setdefault("TWCHIP_DATA_DIR", str(D))
pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 30)


def cur(ds, mkt, d):
    p = D / "curated" / ds / mkt / f"{d:%Y}" / f"{d.isoformat()}.parquet"
    return pd.read_parquet(p) if p.exists() else None


def trading_days():
    fs = sorted((D / "curated" / "quotes" / "TWSE").glob("*/*.parquet"))
    return [date.fromisoformat(f.stem) for f in fs]


DAYS = trading_days()


def f4_rate(flows, quotes):
    j = flows.merge(quotes[["code", "volume_shares"]], on="code")
    over = pd.Series(False, index=j.index)
    for g in ("fx", "fd", "trust", "ds", "dh"):
        over |= (j[f"{g}_buy"].fillna(0) > j["volume_shares"]) | (j[f"{g}_sell"].fillna(0) > j["volume_shares"])
    return over.mean(), j, over


def _diagnose_a(flows, d, i):
    print("\n  把這份法人資料配上前後各天的上櫃行情，算 F4 超量比例（他日資料會在別天最低）：")
    for k in range(-3, 4):
        if 0 <= i + k < len(DAYS):
            q = cur("quotes", "TPEX", DAYS[i + k])
            if q is not None:
                r, _, _ = f4_rate(flows, q)
                print(f"    配 {DAYS[i + k]}（{k:+d}）：{r:.2%}")
    print("\n  與前後交易日已發布的法人資料逐列比對（完全相同的列數）：")
    keys = ["code", "fx_buy", "fx_sell", "trust_buy", "trust_sell", "dh_buy", "dh_sell"]
    for k in (-2, -1, 1, 2):
        o = cur("inst_flows", "TPEX", DAYS[i + k])
        if o is not None:
            same = flows[keys].merge(o[keys], on=keys)
            print(f"    與 {DAYS[i + k]} 相同：{len(same)}/{len(flows)}")
    q = cur("quotes", "TPEX", d)
    r, j, over = f4_rate(flows, q)
    j = j[over].copy()
    j["max_inst"] = j[[f"{g}_{s}" for g in ("fx", "fd", "trust", "ds", "dh") for s in ("buy", "sell")]].max(axis=1)
    j["x"] = j["max_inst"] / j["volume_shares"]
    print(f"\n  當日超量 {int(over.sum())} 檔，倍數最高的 15 檔（max_inst ÷ 成交股數）：")
    print(j.sort_values("x", ascending=False)[["code", "volume_shares", "max_inst", "x"]].head(15).to_string(index=False))


# ------------------------------------------------------------------ A
if "A" in PARTS:
    d = date.fromisoformat(os.environ.get("TWCHIP_DIAG_DATE", "2026-09-18"))
    print(f"== A. {d} 上櫃三大法人（inst_flows/TPEX）")
    qj = json.loads((D / "quality" / f"{d:%Y}" / f"{d}.json").read_text(encoding="utf-8"))
    u = qj["units"]["inst_flows/TPEX"]
    for c in u.get("checks", []):
        if c["status"] != "pass":
            print(f"  {c['status'].upper()} {c['level']} {c['id']} {c['name']}：{c['detail']}")
            if c.get("examples"):
                print("   例：", json.dumps(c["examples"][:5], ensure_ascii=False))
    rel = f"quarantine/inst_flows/TPEX/{d:%Y}/{d}.parquet"
    if not (D / rel).exists() and os.environ.get("TWCHIP_HF_REPO"):
        try:
            from huggingface_hub import hf_hub_download
            hf_hub_download(os.environ["TWCHIP_HF_REPO"], rel, repo_type="dataset", local_dir=str(D))
        except Exception as e:  # noqa: BLE001
            print(f"  從 HF 下載隔離檔失敗：{e}")
    from twchip.config import Config
    from twchip.http import Fetcher
    from twchip.rawstore import RawStore
    from twchip.sources import tpex
    cfg = Config()
    fresh = tpex.fetch_daily_trade(Fetcher(RawStore(cfg.data_dir), cfg.min_interval), d)
    print(f"  今天重抓：{len(fresh.df)} 列，官方自稱日期 {fresh.served_date}")
    versions = {"今天重抓": fresh.df}
    if (D / rel).exists():
        versions = {"當時隔離": pd.read_parquet(D / rel), **versions}
        a, b = versions["當時隔離"], fresh.df
        cols = [c for c in a.columns if c in b.columns and c not in ("name",)]
        same = a[cols].astype(str).merge(b[cols].astype(str), on=cols)
        print(f"  當時隔離 {len(a)} 列 vs 今天重抓 {len(b)} 列，完全相同 {len(same)} 列")
    else:
        print("  沒有當時的隔離檔，只分析今天重抓的版本。")
    i = DAYS.index(d)
    for label, flows in versions.items():
        print(f"\n  ---- {label} ----")
        _diagnose_a(flows, d, i)

# ------------------------------------------------------------------ B
if "B" in PARTS:
    print("\n== B. 上市發行股數每日變動的是哪些證券")
    rows = []
    for d0, d1 in zip(DAYS[-6:-1], DAYS[-5:]):
        a, b = cur("foreign_holdings", "TWSE", d0), cur("foreign_holdings", "TWSE", d1)
        if a is None or b is None:
            continue
        m = a[["code", "issued_shares"]].merge(b[["code", "issued_shares"]], on="code", suffixes=("_0", "_1"))
        ch = m[m["issued_shares_0"] != m["issued_shares_1"]]
        etf = ch["code"].str.startswith("00")
        rows.append({"date": d1, "total": len(m), "changed": len(ch), "etf_like(00開頭)": int(etf.sum()),
                     "others": int((~etf).sum()), "others_examples": ",".join(ch.loc[~etf, "code"].head(8))})
    print(pd.DataFrame(rows).to_string(index=False))

# ------------------------------------------------------------------ C
if "C" in PARTS:
    print("\n== C. T5 偏離：官方成交股數 − 鉅額交易 − 盤後定價 是否等於富邦的分母")
    from twchip.config import Config
    from twchip.http import Fetcher
    from twchip.rawstore import RawStore
    from twchip.util import ymd
    cfg = Config()
    fx = Fetcher(RawStore(cfg.data_dir), cfg.min_interval)
    BASE = "https://www.twse.com.tw/rwd/zh"

    def load(ds, cols):
        fs = sorted((D / "curated" / ds).glob("*/*/*.parquet"))
        return pd.concat([pd.read_parquet(f, columns=cols) for f in fs], ignore_index=True)

    s = load("broker_top_summary", ["date", "code", "implied_vol_lots"])
    qq = load("quotes", ["date", "code", "volume_shares"])
    for x in (s, qq):
        x["date"] = pd.to_datetime(x["date"]).dt.date
    s = s.dropna().merge(qq.drop_duplicates(["date", "code"]), on=["date", "code"])
    s = s[s["volume_shares"] > 0].sort_values(["code", "date"]).copy()
    s["ratio"] = s["implied_vol_lots"] / (s["volume_shares"] / 1000)
    s["ref"] = s.groupby("code")["ratio"].transform(lambda x: x.shift(1).rolling(20, min_periods=10).median())
    s = s.dropna(subset=["ref"])
    s["dev"] = s["ratio"] / s["ref"] - 1
    top = s.reindex(s["dev"].abs().sort_values(ascending=False).index).head(12)
    ctrl = s[s["dev"].abs() < 0.005].sample(6, random_state=0)
    pick = pd.concat([top.assign(kind="偏離"), ctrl.assign(kind="對照")])

    def table(j):
        tabs = j.get("tables") or ([{"fields": j.get("fields"), "data": j.get("data"), "title": j.get("title")}]
                                   if j.get("fields") else [])
        return [t for t in tabs if t.get("fields") and t.get("data")]

    def vol_by_code(url, param_list, d, key):
        """依序試幾組參數；端點名稱或參數不對時印出官方回應的 stat，不中斷。"""
        tabs, msg = [], ""
        for params in param_list:
            try:
                r = fx.get(url, params, source="twse_diag", d=d, key=key, ext="json")
                j = json.loads(r.content.decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                msg += f" {params}: {e}"
                continue
            tabs = table(j)
            if tabs:
                break
            msg += f" {params}: stat={j.get('stat')}"
        if not tabs:
            return None, msg.strip()
        t = tabs[0]
        f = [str(x) for x in t["fields"]]
        ci = next(i for i, x in enumerate(f) if "代號" in x)
        vi = next((i for i, x in enumerate(f) if "成交股數" in x or "成交數量" in x), None)
        if vi is None:
            return None, f"找不到成交股數欄：{f}"
        out = {}
        for row in t["data"]:
            c = str(row[ci]).strip()
            v = str(row[vi]).replace(",", "").strip()
            if v.replace(".", "").isdigit():
                out[c] = out.get(c, 0) + float(v)
        return out, f"{t.get('title')}｜欄位 {f}"

    res, shown = [], set()
    for d in sorted(set(pick["date"])):
        blk, info_b = vol_by_code(f"{BASE}/block/BFIAUU", [
            {"date": ymd(d), "selectType": "S", "response": "json"},
            {"date": ymd(d), "response": "json"}], d, "bfiauu")
        aft, info_a = vol_by_code(f"{BASE}/afterTrading/BFT41U", [
            {"date": ymd(d), "selectType": "ALL", "response": "json"},
            {"date": ymd(d), "response": "json"}], d, "bft41u")
        if "b" not in shown and blk is not None:
            print("  鉅額交易：", info_b); shown.add("b")
        if "a" not in shown and aft is not None:
            print("  盤後定價：", info_a); shown.add("a")
        for _, r in pick[pick["date"] == d].iterrows():
            b = (blk or {}).get(r["code"], 0.0)
            a = (aft or {}).get(r["code"], 0.0)
            reg = r["volume_shares"] - b - a
            adj = r["implied_vol_lots"] / (reg / 1000) if reg > 0 else np.nan
            res.append({"kind": r["kind"], "code": r["code"], "date": d, "official_lots": r["volume_shares"] / 1000,
                        "block_lots": b / 1000, "afterhr_lots": a / 1000, "implied_lots": r["implied_vol_lots"],
                        "ratio": r["ratio"], "adj_ratio": adj, "ref": r["ref"], "dev": r["dev"],
                        "blk_ok": blk is not None, "aft_ok": aft is not None,
                        "note": "" if blk is not None and aft is not None else f"{info_b if blk is None else ''} {info_a if aft is None else ''}"})
    out = pd.DataFrame(res)
    print(out.round(4).to_string(index=False))
    ok = out.dropna(subset=["adj_ratio"])
    print(f"\n  |ratio − 1| 中位數：{(ok['ratio'] - 1).abs().median():.4f}；"
          f"|adj_ratio − 1| 中位數：{(ok['adj_ratio'] - 1).abs().median():.4f}")
    print("  判讀：若偏離列的 adj_ratio 都接近 1，代表富邦分母 = 一般交易成交量，T5 可改用扣除後的官方量做精確比對。")

# ------------------------------------------------------------------ D
if "D" in PARTS:
    print("\n== D. 鉅額交易的各種 selectType，以及盤後定價的原始列")
    from twchip.config import Config
    from twchip.http import Fetcher
    from twchip.rawstore import RawStore
    from twchip.util import ymd
    cfg = Config()
    fx = Fetcher(RawStore(cfg.data_dir), cfg.min_interval)
    BASE = "https://www.twse.com.tw/rwd/zh"
    cases = [("2412", date(2025, 9, 30)), ("2330", date(2026, 6, 1)), ("2891", date(2026, 8, 28)),
             ("2330", date(2026, 9, 1)), ("2379", date(2025, 12, 29))]   # 最後一筆是 C 已解釋的對照
    for d in sorted({x for _, x in cases}):
        codes = [c for c, x in cases if x == d]
        print(f"\n  ---- {d}（{', '.join(codes)}）----")
        for st in ("S", "P", "C", "T", "A", "ALL", "MS", "SM"):
            try:
                r = fx.get(f"{BASE}/block/BFIAUU", {"date": ymd(d), "selectType": st, "response": "json"},
                           source="twse_diag", d=d, key=f"bfiauu_{st}", ext="json")
                j = json.loads(r.content.decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                print(f"    BFIAUU selectType={st}: 失敗 {e}")
                continue
            tabs = j.get("tables") or ([j] if j.get("fields") else [])
            tabs = [t for t in tabs if t.get("data")]
            if not tabs:
                print(f"    BFIAUU selectType={st}: stat={j.get('stat')}")
                continue
            for t in tabs:
                f = [str(x) for x in t["fields"]]
                hit = [row for row in t["data"] if any(str(c) in [str(v).strip() for v in row] for c in codes)]
                print(f"    BFIAUU selectType={st}: {t.get('title')}｜{len(t['data'])} 列｜欄位 {f}")
                for row in hit[:6]:
                    print("      ", row)
                if not hit:
                    print("       （沒有這幾檔）前 2 列：", t["data"][:2])
        r = fx.get(f"{BASE}/afterTrading/BFT41U", {"date": ymd(d), "selectType": "ALL", "response": "json"},
                   source="twse_diag", d=d, key="bft41u", ext="json")
        j = json.loads(r.content.decode("utf-8"))
        tabs = j.get("tables") or ([j] if j.get("fields") else [])
        for t in [t for t in tabs if t.get("data")]:
            print(f"    BFT41U：{t.get('title')}｜欄位 {t['fields']}")
            for row in t["data"]:
                if str(row[0]).strip() in codes:
                    print("      ", row)

# ------------------------------------------------------------------ E
if "E" in PARTS:
    import re as _re
    from twchip.config import Config
    from twchip.http import Fetcher
    from twchip.rawstore import RawStore
    from twchip.sources import CODE_RE
    from twchip.util import ymd
    cfg = Config()
    fx = Fetcher(RawStore(cfg.data_dir), cfg.min_interval)
    dates = [date.fromisoformat(a) for a in ARGS[1:]] or [date(2026, 2, 9), DAYS[-1]]
    TW, TP = "https://www.twse.com.tw/rwd/zh", "https://www.tpex.org.tw/www/zh-tw"

    def tables(j):
        out = [t for t in (j.get("tables") or []) if t.get("fields") and t.get("data")]
        if j.get("fields") and j.get("data"):
            out.append({"title": j.get("title"), "fields": j["fields"], "data": j["data"]})
        return out

    def scan(name, url, params, d):
        try:
            r = fx.get(url, params, source="diag_codes", d=d, key=name.replace("/", "_"), ext="json")
            j = json.loads(r.content.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"    {name}: 失敗 {e}")
            return
        found = False
        for t in tables(j):
            f = [str(x) for x in t["fields"]]
            ci = next((i for i, x in enumerate(f) if "代號" in x), None)
            ni = next((i for i, x in enumerate(f) if "名稱" in x), None)
            if ci is None:
                continue
            found = True
            codes = [_re.sub("<[^>]+>", "", str(row[ci])).strip() for row in t["data"]]
            badc = [(c, str(t["data"][k][ni]).strip() if ni is not None else "") for k, c in enumerate(codes)
                    if c and not _re.match(CODE_RE, c) and c not in ("總計", "合計")]
            print(f"    {name}｜{t.get('title')}｜{len(codes)} 列，不符格式 {len(badc)} 列：{badc[:20]}")
        if not found:
            print(f"    {name}: stat={j.get('stat')}，沒有含「代號」欄的表")

    for d in dates:
        print(f"\n== E. {d}：不符合代號格式 {CODE_RE} 的證券")
        scan("TWSE/MI_INDEX", f"{TW}/afterTrading/MI_INDEX", {"date": ymd(d), "type": "ALLBUT0999", "response": "json"}, d)
        scan("TWSE/T86", f"{TW}/fund/T86", {"date": ymd(d), "selectType": "ALLBUT0999", "response": "json"}, d)
        scan("TWSE/MI_QFIIS", f"{TW}/fund/MI_QFIIS", {"date": ymd(d), "selectType": "ALLBUT0999", "response": "json"}, d)
        scan("TWSE/BFIAUU", f"{TW}/block/BFIAUU", {"date": ymd(d), "selectType": "S", "response": "json"}, d)
        scan("TPEX/otc", f"{TP}/afterTrading/otc", {"type": "EW", "date": f"{d:%Y/%m/%d}", "response": "json"}, d)
        scan("TPEX/dailyTrade", f"{TP}/insti/dailyTrade", {"type": "Daily", "sect": "EW", "date": f"{d:%Y/%m/%d}",
                                                          "response": "json"}, d)
        scan("TPEX/qfii", f"{TP}/insti/qfii", {"date": f"{d:%Y/%m/%d}", "response": "json"}, d)
