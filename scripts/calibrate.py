"""T5、F4、H3 門檻校正：用真實資料的反事實配對量測「誤報率」與「偵測率」。

方法（事先決定，不看結果再改）：
  1. 切分：校正集 = 交易日 ≤ SPLIT；驗證集 = 之後。門檻只用校正集決定。
  2. 正常配對：D 日的資料互相比對（真實情況）。
     錯誤配對：D 日資料配上 D±k 日的另一份資料（模擬「抓到他日資料」），k ∈ {±1, ±2, ±5} 個交易日。
  3. 門檻選擇依樣本數分兩種：
     - 每檔每日的檢查（T5，校正樣本數千筆）：校正集誤報率 ≤ 0.2% 的最嚴格值。
     - 每市場每日的檢查（F4、H3，校正樣本約 180 筆）：0.2% 無法從這麼少的樣本估計，
       改為「校正集零誤報」且與最差正常值保持安全距離（F4：門檻 ≥ 最大值 × 1.5；H3：門檻 ≤ 最小值 − 0.05）。
     驗證集誤報率須 ≤ 0.5%。每個門檻都列出誤報率的 95% 信賴上界（零誤報時約為 3/n）。
  4. T5 只有在驗證集偵測率 ≥ 80% 時才建議改為阻擋（B）。
  5. 校正集中超過門檻的正常資料逐筆列出，必須人工查原始頁面。
  T5 的 20 日視窗與至少 10 日歷史是固定參數，不調整。

用法：python scripts/calibrate.py [資料目錄，預設 ~/twchip-data] [SPLIT，預設 2026-06-30]
"""
import os
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

D = Path(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/twchip-data"))
SPLIT = pd.Timestamp(sys.argv[2] if len(sys.argv) > 2 else "2026-06-30").date()
OFFSETS = [-5, -2, -1, 1, 2, 5]
FPR_CAL, FPR_VAL, TPR_MIN = 0.002, 0.005, 0.80
WINDOW, MIN_HIST = 20, 10

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)
con = duckdb.connect()


def load(ds, cols):
    g = D / "curated" / ds
    if not list(g.glob("*/*/*.parquet")):
        return pd.DataFrame()
    df = con.sql(f"""select {cols}, regexp_extract(filename, 'curated/[^/]+/([^/]+)/', 1) as mkt
                     from read_parquet('{g}/*/*/*.parquet', union_by_name=true, filename=true)""").df()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


quotes = load("quotes", "date, code, volume_shares")
flows = load("inst_flows", "date, code, fx_buy, fx_sell, fd_buy, fd_sell, trust_buy, trust_sell, "
                           "ds_buy, ds_sell, dh_buy, dh_sell, coalesce(foreign_net, fx_net + fd_net) as fnet")
hold = load("foreign_holdings", "date, code, foreign_shares")
summ = load("broker_top_summary", "date, code, implied_vol_lots")

days = sorted(quotes["date"].unique())
idx = {d: i for i, d in enumerate(days)}


def shift(d, k):
    i = idx.get(d)
    j = None if i is None else i + k
    return days[j] if j is not None and 0 <= j < len(days) else None


def split_name(d):
    return "cal" if d <= SPLIT else "val"


def upper95(k, n):
    """二項比例的 95% 單尾信賴上界（Clopper–Pearson）。零失敗時約為 3/n。
    以對數空間計算二項分布，n 很大時不會溢位。"""
    if n == 0:
        return np.nan
    if k >= n:
        return 1.0
    if k == 0:
        return 1 - 0.05 ** (1 / n)
    from math import lgamma
    i = np.arange(k + 1)
    logc = np.array([lgamma(n + 1) - lgamma(j + 1) - lgamma(n - j + 1) for j in i])
    lo, hi = k / n, 1.0
    for _ in range(60):  # 二分法解 P(X ≤ k | p) = 0.05
        mid = (lo + hi) / 2
        cdf = float(np.exp(logc + i * np.log(mid) + (n - i) * np.log1p(-mid)).sum())
        lo, hi = (mid, hi) if cdf > 0.05 else (lo, mid)
    return hi


def report(name, null, alt, grid, fails_if, unit, margin_ok=None):
    """null/alt：DataFrame，含 date、score；fails_if(score, t) → 是否判定失敗。
    margin_ok(t, 校正集正常分數)：小樣本檢查的安全距離規則；有給時改用「零誤報＋安全距離」選門檻。"""
    null = null.assign(part=null["date"].map(split_name))
    alt = alt.assign(part=alt["date"].map(split_name))
    rows = []
    for t in grid:
        r = {"threshold": t}
        for part in ("cal", "val"):
            n = null[null.part == part]
            a = alt[alt.part == part]
            nf = int(fails_if(n["score"], t).sum())
            r[f"FPR_{part}"] = nf / len(n) if len(n) else np.nan
            r[f"FPR_{part}_ub95"] = upper95(nf, len(n))
            r[f"TPR_{part}"] = float(fails_if(a["score"], t).mean()) if len(a) else np.nan
        rows.append(r)
    tab = pd.DataFrame(rows)
    cal_scores = null.loc[null.part == "cal", "score"]
    if margin_ok is None:
        ok = tab[tab["FPR_cal"] <= FPR_CAL]
        rule = "校正集誤報率 ≤ 0.2%"
    else:
        ok = tab[(tab["FPR_cal"] == 0) & tab["threshold"].map(lambda t: margin_ok(t, cal_scores))]
        rule = "校正集零誤報＋安全距離"
    print(f"\n== {name}（樣本：正常 校正 {int((null.part == 'cal').sum())}／驗證 {int((null.part == 'val').sum())}；"
          f"錯誤配對 校正 {int((alt.part == 'cal').sum())}／驗證 {int((alt.part == 'val').sum())}；單位：{unit}）")
    print(f"   選擇規則：{rule}；校正集正常分數範圍 [{cal_scores.min():.4f}, {cal_scores.max():.4f}]")
    print(tab.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    if ok.empty:
        print("→ 沒有任何門檻的校正集誤報率 ≤ 0.2%")
        return None, tab
    best = ok.iloc[0]  # grid 由嚴到鬆排列，第一個符合者即最嚴格
    verdict = "通過" if best["FPR_val"] <= FPR_VAL else "未通過（需放寬）"
    print(f"→ 建議門檻 {best['threshold']}：校正誤報 {best['FPR_cal']:.3%}（95% 上界 {best['FPR_cal_ub95']:.2%}），"
          f"驗證誤報 {best['FPR_val']:.3%}（{verdict}），驗證偵測率 {best['TPR_val']:.1%}")
    by_k = alt[alt.part == "val"].groupby("k")["score"].apply(lambda s: float(fails_if(s, best["threshold"]).mean()))
    print("   各錯位天數 k 的驗證偵測率：" + "，".join(f"k={k:+d}: {v:.0%}" for k, v in by_k.items()))
    flagged = null[(null.part == "cal") & fails_if(null["score"], best["threshold"])]
    print(f"   校正集中超過門檻的正常資料（共 {len(flagged)} 筆，需逐筆查原始頁面）：")
    if len(flagged):
        print(flagged.drop(columns="part").head(30).to_string(index=False))
    return best, tab


# ------------------------------------------------------------------ T5
if not summ.empty and summ["implied_vol_lots"].notna().any():
    v = quotes[["date", "code", "volume_shares"]].drop_duplicates(["date", "code"])
    s = summ.dropna(subset=["implied_vol_lots"]).merge(v, on=["date", "code"])
    s = s[s["volume_shares"] > 0].sort_values(["code", "date"]).copy()
    s["ratio"] = s["implied_vol_lots"] / (s["volume_shares"] / 1000.0)
    s["ref"] = s.groupby("code")["ratio"].transform(lambda x: x.shift(1).rolling(WINDOW, min_periods=MIN_HIST).median())
    base = s.dropna(subset=["ref"])
    null = base.assign(score=base["ratio"] / base["ref"] - 1)[["date", "code", "ratio", "ref", "score"]]
    imp = s.set_index(["code", "date"])["implied_vol_lots"]
    alts = []
    for k in OFFSETS:
        b = base[["date", "code", "volume_shares", "ref"]].copy()
        b["src"] = b["date"].map(lambda d: shift(d, k))
        b["implied_other"] = [imp.get((c, x), np.nan) for c, x in zip(b["code"], b["src"])]
        b = b.dropna(subset=["implied_other"])
        b["score"] = (b["implied_other"] / (b["volume_shares"] / 1000.0)) / b["ref"] - 1
        alts.append(b.assign(k=k)[["date", "code", "k", "score"]])
    alt = pd.concat(alts, ignore_index=True)
    grid = [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20]
    best, _ = report("T5 隱含成交量比值偏離（|偏離| > 門檻 即失敗）", null, alt, grid,
                     lambda x, t: x.abs() > t, "每檔每日")
    if best is not None:
        lvl = "B（阻擋）" if best["TPR_val"] >= TPR_MIN and best["FPR_val"] <= FPR_VAL else "W（維持警告）"
        print(f"   T5 建議等級：{lvl}（規則：驗證偵測率 ≥ 80% 且驗證誤報率 ≤ 0.5% 才改阻擋）")
else:
    print("\n== T5：沒有 implied_vol_lots，略過")

# ------------------------------------------------------------------ F4
if not flows.empty:
    cols = ["fx_buy", "fx_sell", "fd_buy", "fd_sell", "trust_buy", "trust_sell", "ds_buy", "ds_sell", "dh_buy", "dh_sell"]
    f = flows.copy()
    f["mx"] = f[cols].fillna(0).max(axis=1)
    v = quotes[["date", "mkt", "code", "volume_shares"]]

    def f4_rate(pairs):
        x = pairs.merge(v, on=["date", "mkt", "code"])
        x = x[x["volume_shares"].notna()]
        return x.assign(over=(x["mx"] > x["volume_shares"]).astype(float)).groupby(["date", "mkt"])["over"].mean()

    for mkt in sorted(f["mkt"].unique()):
        fm = f[f["mkt"] == mkt][["date", "mkt", "code", "mx"]]
        null = f4_rate(fm).reset_index().rename(columns={"over": "score"})
        alts = []
        for k in OFFSETS:
            other = fm.copy()
            other["date"] = other["date"].map(lambda d: shift(d, -k))  # D+k 的法人資料 → 配到 D 的成交量
            other = other.dropna(subset=["date"])
            alts.append(f4_rate(other).reset_index().rename(columns={"over": "score"}).assign(k=k))
        alt = pd.concat(alts, ignore_index=True)
        grid = [0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.07, 0.10]
        report(f"F4 {mkt} 法人買賣超過成交量的檔數比例（> 門檻 即失敗）", null, alt, grid,
               lambda x, t: x > t, "每市場每日", margin_ok=lambda t, c: t >= 1.5 * float(c.max()))

# ------------------------------------------------------------------ H3
if not hold.empty and not flows.empty:
    h = hold.sort_values(["mkt", "code", "date"]).copy()
    h["dh"] = h.groupby(["mkt", "code"])["foreign_shares"].diff()
    # 只用「前一筆 = 前一個交易日」的變化，與管線一致。前一日被隔離時，跨兩天的變化不算（否則同號率被低估）。
    h["pd"] = h.groupby(["mkt", "code"])["date"].shift(1)
    h = h[h["pd"] == h["date"].map(lambda d: shift(d, -1))].dropna(subset=["dh"])
    fn = flows[["date", "mkt", "code", "fnet"]]

    def agree(pairs):
        x = h[["date", "mkt", "code", "dh"]].merge(pairs, on=["date", "mkt", "code"])
        x = x[(x["dh"] != 0) & (x["fnet"].fillna(0) != 0)]
        g = x.assign(a=(np.sign(x["dh"]) == np.sign(x["fnet"])).astype(float)).groupby(["date", "mkt"])
        out = g["a"].mean().to_frame("score")
        out["n"] = g.size()
        return out[out["n"] >= 50].reset_index()

    for mkt in sorted(h["mkt"].unique()):
        fm = fn[fn["mkt"] == mkt]
        null = agree(fm)
        alts = []
        for k in OFFSETS:
            other = fm.copy()
            other["date"] = other["date"].map(lambda d: shift(d, -k))
            alts.append(agree(other.dropna(subset=["date"])).assign(k=k))
        alt = pd.concat(alts, ignore_index=True)
        grid = [0.95, 0.92, 0.90, 0.88, 0.85, 0.80, 0.75, 0.70, 0.60]
        report(f"H3 {mkt} 外資持股變化與外資買賣超同號率（< 門檻 即失敗）", null, alt, grid,
               lambda x, t: x < t, "每市場每日", margin_ok=lambda t, c: t <= float(c.min()) - 0.05)
