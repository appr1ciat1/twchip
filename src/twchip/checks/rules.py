"""所有檢查的實作。輸入都是標準欄位的 DataFrame，輸出 CheckResult 列表。"""
from __future__ import annotations

from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from ..schemas import KEYS
from ..sources import Parsed
from . import CheckResult, bad, ok, skip

FINGERPRINT_COLS = {
    "quotes": ["code", "close", "volume_shares"],
    "inst_flows": ["code", "fx_net", "trust_net", "dealer_net"],
    "foreign_holdings": ["code", "foreign_shares", "issued_shares"],
    "broker_top": ["code", "side", "rank", "bhid", "net_lots"],
}


def _ex(df: pd.DataFrame, mask, cols) -> list:
    return df.loc[mask, cols].head(5).astype(str).to_dict("records")


# ---------------------------------------------------------------- 通用 G1~G5, G7

def generic(p: Parsed, d: date, dataset: str, recent_counts: list[int],
            prev_df: Optional[pd.DataFrame]) -> list[CheckResult]:
    df, out = p.df, []
    if p.served_date == d:
        out.append(ok("G1", "回應日期 = 請求日期", "B", str(d)))
    else:
        out.append(bad("G1", "回應日期 = 請求日期", "B", f"請求 {d}，官方回應 {p.served_date}"))
    if p.field_issues:
        out.append(bad("G2", "欄位名稱符合預期", "B", "; ".join(p.field_issues)))
    else:
        out.append(ok("G2", "欄位名稱符合預期", "B"))
    keys = [k for k in KEYS[dataset] if k in df.columns]
    nulls = int(df[keys].isna().any(axis=1).sum()) if len(df) else 0
    dups = int(df.duplicated(keys).sum()) if len(df) else 0
    out.append((ok if nulls == dups == 0 else bad)("G3", "主鍵唯一且非 NULL", "B",
               f"NULL {nulls} 列，重複 {dups} 列"))
    if len(recent_counts) >= 5:
        med = float(np.median(recent_counts))
        dev = abs(len(df) - med) / med if med else 1.0
        out.append((ok if dev <= 0.15 else bad)("G4", "列數在近期中位數 ±15%", "B",
                   f"今日 {len(df)}，中位數 {med:.0f}，偏差 {dev:.1%}"))
    else:
        out.append(skip("G4", "列數在近期中位數 ±15%", "B", f"歷史不足（{len(recent_counts)} 日）"))
    cols = FINGERPRINT_COLS[dataset]
    if prev_df is None or prev_df.empty or df.empty:
        out.append(skip("G5", "與前一交易日不同", "B", "無前一交易日資料"))
    else:
        val = [c for c in cols if c != "code"]
        nz = df[df[val].fillna(0).ne(0).any(axis=1)]
        a = set(map(tuple, nz[cols].astype(str).values))
        b = set(map(tuple, prev_df[cols].astype(str).values))
        frac = len(a & b) / len(a) if a else 0.0
        out.append((ok if frac <= 0.5 else bad)("G5", "與前一交易日不同", "B",
                   f"{frac:.1%} 的非零列與前一交易日完全相同"))
    rt = p.extras.get("reported_total")
    if rt not in (None, ""):
        try:
            rt = int(rt)
            got = int(p.extras.get("raw_rows", len(df)))  # 過濾代號前的原始筆數
            out.append((ok if rt == got else bad)("G7", "官方總筆數 = 取得筆數（無分頁遺漏）", "B",
                       f"官方 {rt}，取得 {got}"))
        except (TypeError, ValueError):
            pass
    return out


# ---------------------------------------------------------------- quotes

def quotes(df: pd.DataFrame, prev: Optional[pd.DataFrame]) -> list[CheckResult]:
    out = []
    t = df[(df["volume_shares"].fillna(0) > 0) & df[["open", "high", "low", "close"]].notna().all(axis=1)]
    m = (t["low"] > t[["open", "close"]].min(axis=1)) | (t[["open", "close"]].max(axis=1) > t["high"])
    out.append((ok if not m.any() else bad)("Q1", "最低 ≤ 開、收 ≤ 最高", "B", f"{int(m.sum())} 檔違反",
               int(m.sum()), _ex(t, m, ["code", "open", "high", "low", "close"])))
    if prev is not None and not prev.empty:
        j = df.merge(prev[["code", "close"]].rename(columns={"close": "pclose"}), on="code")
        j = j[j["change"].notna() & j["pclose"].notna() & j["close"].notna() & ~j["ex_right"].fillna(False)]
        m2 = (j["close"] - j["change"] - j["pclose"]).abs() > 0.011
        out.append((ok if m2.mean() < 0.01 else bad)("Q2", "收盤 − 漲跌 = 前日收盤", "W",
                   f"{int(m2.sum())}/{len(j)} 檔不符（除權息已排除）", int(m2.sum()),
                   _ex(j, m2, ["code", "close", "change", "pclose"])))
        j3 = j[j["pclose"] > 0]
        m3 = (j3["close"] / j3["pclose"] - 1).abs() > 0.105
        out.append((ok if not m3.any() else bad)("Q3", "漲跌幅 ≤ 10.5%", "W", f"{int(m3.sum())} 檔超過",
                   int(m3.sum()), _ex(j3, m3, ["code", "close", "pclose"])))
    else:
        out.append(skip("Q2", "收盤 − 漲跌 = 前日收盤", "W", "無前日"))
    v = t[(t["value_twd"].fillna(0) > 0)]
    avg = v["value_twd"].astype(float) / v["volume_shares"].astype(float)
    m4 = (avg < v["low"].astype(float) * 0.995) | (avg > v["high"].astype(float) * 1.005)
    rate = float(m4.mean()) if len(v) else 0.0
    out.append((ok if rate <= 0.01 else bad)("Q4", "均價（金額/股數）落在 [最低, 最高]", "B",
               f"{int(m4.sum())}/{len(v)} 檔不符（容許 1%）", int(m4.sum()),
               _ex(v, m4, ["code", "low", "high", "value_twd", "volume_shares"])))
    return out


# ---------------------------------------------------------------- inst_flows

GROUPS = ["fx", "fd", "foreign", "trust", "ds", "dh", "dealer"]


def inst_flows(df: pd.DataFrame, quotes_df: Optional[pd.DataFrame], f4_tol: float = 0.03) -> list[CheckResult]:
    out = []
    bad_rows = pd.Series(False, index=df.index)
    for g in GROUPS:
        b, s, n = df[f"{g}_buy"], df[f"{g}_sell"], df[f"{g}_net"]
        has = b.notna() & s.notna() & n.notna()
        bad_rows |= has & ((b - s) != n).fillna(False)
    out.append((ok if not bad_rows.any() else bad)("F1", "每類 買進 − 賣出 = 買賣超", "B",
               f"{int(bad_rows.sum())} 列違反", int(bad_rows.sum()), _ex(df, bad_rows, ["code"])))

    m = pd.Series(False, index=df.index)
    for tot, parts in (("foreign", ("fx", "fd")), ("dealer", ("ds", "dh"))):
        for k in ("net", "buy", "sell"):
            a, b1, b2 = df[f"{tot}_{k}"], df[f"{parts[0]}_{k}"], df[f"{parts[1]}_{k}"]
            has = a.notna() & b1.notna() & b2.notna()
            m |= has & ((b1 + b2) != a).fillna(False)
    out.append((ok if not m.any() else bad)("F2", "外資 = 不含自營 + 外資自營；自營 = 自行 + 避險", "B",
               f"{int(m.sum())} 列違反", int(m.sum()), _ex(df, m, ["code"])))

    has = df["total_net"].notna() & df["foreign_net"].notna() & df["trust_net"].notna() & df["dealer_net"].notna()
    m3 = has & ((df["foreign_net"] + df["trust_net"] + df["dealer_net"]) != df["total_net"]).fillna(False)
    out.append((ok if not m3.any() else bad)("F3", "外資 + 投信 + 自營 = 官方合計", "B",
               f"{int(m3.sum())}/{int(has.sum())} 列違反", int(m3.sum()),
               _ex(df, m3, ["code", "foreign_net", "trust_net", "dealer_net", "total_net"])))

    if quotes_df is None or quotes_df.empty:
        out.append(skip("F4", "法人買進／賣出 ≤ 當日成交股數", "B", "無當日行情"))
        out.append(skip("F5", "有法人交易的股票都有行情", "W", "無當日行情"))
        return out
    j = df.merge(quotes_df[["code", "volume_shares"]], on="code", how="left")
    q = j[j["volume_shares"].notna()]
    over = pd.Series(False, index=q.index)
    for g in ("fx", "fd", "trust", "ds", "dh"):
        over |= (q[f"{g}_buy"].fillna(0) > q["volume_shares"]) | (q[f"{g}_sell"].fillna(0) > q["volume_shares"])
    rate = float(over.mean()) if len(q) else 0.0
    # 少數股票會因統計口徑不同而超過：櫃買行情的成交股數不含盤後定價交易，法人買賣則有含。
    # 實測 2026-10-30 上櫃 10/917 檔（1.09%）。整日錯位時會大量超過。以比例判定，回補後再依分布校正。
    out.append((ok if rate <= f4_tol else bad)("F4", "法人買進／賣出 ≤ 當日成交股數", "B",
               f"{int(over.sum())}/{len(q)} 檔超過（{rate:.2%}，容許 {f4_tol:.1%}）", int(over.sum()),
               _ex(q, over, ["code", "fx_buy", "trust_buy", "volume_shares"])))
    act = j[(j[[f"{g}_net" for g in ("fx", "fd", "trust", "ds", "dh")]].fillna(0) != 0).any(axis=1)]
    miss = act["volume_shares"].isna()
    out.append((ok if not miss.any() else bad)("F5", "有法人交易的股票都有行情", "W",
               f"{int(miss.sum())} 檔缺行情", int(miss.sum()), _ex(act, miss, ["code", "name"])))
    return out


# ---------------------------------------------------------------- foreign_holdings

def foreign_holdings(df: pd.DataFrame, prev: Optional[pd.DataFrame], flows_d: Optional[pd.DataFrame],
                     flows_prev: Optional[pd.DataFrame]) -> list[CheckResult]:
    out = []
    v = df[df["issued_shares"].fillna(0) > 0]
    calc = v["foreign_shares"].astype(float) / v["issued_shares"].astype(float) * 100
    m1 = (calc - v["foreign_ratio"].astype(float)).abs() > 0.011
    m1 = m1.fillna(True)
    out.append((ok if m1.mean() <= 0.005 else bad)("H1", "比率 = 持股 / 發行股數（±0.01%）", "B",
               f"{int(m1.sum())}/{len(v)} 檔不符", int(m1.sum()),
               _ex(v, m1, ["code", "foreign_shares", "issued_shares", "foreign_ratio"])))
    m2 = (v["foreign_shares"] > v["issued_shares"]).fillna(False)
    out.append((ok if not m2.any() else bad)("H2", "持股 ≤ 發行股數", "B", f"{int(m2.sum())} 檔違反", int(m2.sum()),
               _ex(v, m2, ["code", "foreign_shares", "issued_shares"])))
    m2b = (v["foreign_ratio"] > v["limit_ratio"] + 0.011).fillna(False)
    out.append((ok if not m2b.any() else bad)("H2b", "持股比率 ≤ 法令上限", "W", f"{int(m2b.sum())} 檔超過",
               int(m2b.sum()), _ex(v, m2b, ["code", "foreign_ratio", "limit_ratio"])))

    if prev is None or prev.empty or flows_d is None or flows_d.empty:
        out.append(skip("H3", "Δ持股與當日外資買賣超同號率 ≥ 0.80", "B", "缺前一交易日持股或當日買賣超"))
        return out
    j = df[["code", "foreign_shares"]].merge(prev[["code", "foreign_shares"]], on="code", suffixes=("", "_p"))
    j["delta"] = j["foreign_shares"] - j["foreign_shares_p"]

    def agree(fl: Optional[pd.DataFrame]) -> tuple[float, int]:
        if fl is None or fl.empty:
            return float("nan"), 0
        k = j.merge(fl[["code", "foreign_net"]], on="code")
        k = k[(k["delta"].fillna(0) != 0) & (k["foreign_net"].fillna(0) != 0)]
        if len(k) == 0:
            return float("nan"), 0
        return float((np.sign(k["delta"].astype(float)) == np.sign(k["foreign_net"].astype(float))).mean()), len(k)

    a_d, n_d = agree(flows_d)
    if n_d < 50:
        out.append(skip("H3", "Δ持股與當日外資買賣超同號率 ≥ 0.80", "B", f"樣本不足（{n_d}）"))
    else:
        out.append((ok if a_d >= 0.80 else bad)("H3", "Δ持股與當日外資買賣超同號率 ≥ 0.80", "B",
                   f"同號率 {a_d:.3f}（{n_d} 檔）"))
    a_p, n_p = agree(flows_prev)
    if n_p >= 50 and n_d >= 50:
        out.append((ok if a_d - a_p >= 0.15 else bad)("H4", "同號率對當日明顯高於對前一日（無日期錯位）", "W",
                   f"當日 {a_d:.3f}，前一日 {a_p:.3f}"))
    chg = j.merge(prev[["code", "issued_shares"]], on="code").merge(df[["code", "issued_shares"]], on="code",
                                                                      suffixes=("_p", ""))
    # ETF（00 開頭）與存託憑證（91 開頭）每天申購買回，單位數本來就會變。只看一般股票。
    # 回補實測：上市每日變動 72~96 檔，其中 70~94 檔是 ETF 或存託憑證，一般股票只有 2~6 檔。
    chg = chg[~chg["code"].astype(str).str.startswith(("00", "91"))]
    r5 = float((chg["issued_shares"] != chg["issued_shares_p"]).mean()) if len(chg) else 0.0
    out.append((ok if r5 < 0.01 else bad)("H5", "一般股票發行股數變動檔數 < 1%（不含 ETF、存託憑證）", "W",
               f"{r5:.2%}"))
    return out


# ---------------------------------------------------------------- broker_top（每檔股票各自判定）

def implied_volume_lots(rows: pd.DataFrame) -> Optional[float]:
    """由「佔成交比重」反推當日成交張數：|買賣超| ÷ 佔比。取佔比 ≥ 0.5% 的列的中位數，降低捨入誤差。"""
    r = rows[(rows["pct"].fillna(0) > 0) & rows["net_lots"].notna()] if len(rows) else rows
    if r is None or len(r) == 0:
        return None
    implied = r["net_lots"].abs().astype(float) / r["pct"].astype(float) * 100
    big = r["pct"].astype(float) >= 0.5
    return float(implied[big].median()) if big.any() else float(implied.loc[r["pct"].astype(float).idxmax()])


def broker_stock(rows: pd.DataFrame, summary: dict, served: Optional[date], d: date,
                 quote: Optional[pd.Series], tol: float, ref_ratio: Optional[float] = None,
                 t5_level: str = "W", t5_rel_tol: float = 0.03) -> list[CheckResult]:
    """ref_ratio：該股近 20 個交易日「隱含成交量 ÷ 官方成交量」的中位數；有它時 T5 改為與自身歷史比較。"""
    out = []
    out.append((ok if served == d else bad)("T1", "最後更新日 = 請求日期", "B", f"頁面 {served}"))
    nb, ns = int((rows["side"] == "buy").sum()), int((rows["side"] == "sell").sum())
    if nb == 0 and ns == 0 or nb > 15 or ns > 15:
        out.append(bad("T2", "買超、賣超各 ≤ 15 列且非空", "B", f"買 {nb}、賣 {ns}"))
    else:
        out.append((ok if nb == ns == 15 else bad)("T2", "買超、賣超各 15 列", "W" if (nb and ns) else "B",
                   f"買 {nb}、賣 {ns}"))
    m = ((rows["buy_lots"] - rows["sell_lots"]) != rows["net_lots"]).fillna(True)
    out.append((ok if not m.any() else bad)("T3", "買進 − 賣出 = 買賣超", "B", f"{int(m.sum())} 列違反", int(m.sum()),
               _ex(rows, m, ["side", "rank", "broker_name", "buy_lots", "sell_lots", "net_lots"])))
    sb = int(rows.loc[rows["side"] == "buy", "net_lots"].sum())
    ss = int(-rows.loc[rows["side"] == "sell", "net_lots"].sum())
    fb, fs = summary.get("total_buy_net_lots"), summary.get("total_sell_net_lots")
    if fb is None or fs is None:
        out.append(bad("T4", "前 15 名加總 = 頁尾合計（容許捨入）", "B", "頁尾合計解析失敗"))
    else:
        # 實測（2330，2026-10-21）：頁尾 4,786，15 列加總 4,789。頁尾是以「股」加總後才換成張，
        # 每列則各自四捨五入，因此最多差 0.5 張 × 列數。
        tb, ts = 0.5 * nb + 1e-9, 0.5 * ns + 1e-9
        good = abs(sb - fb) <= tb and abs(ss - fs) <= ts
        out.append((ok if good else bad)("T4", "前 15 名加總 = 頁尾合計（容許捨入）", "B",
                   f"加總 買 {sb}／賣 {ss}；頁尾 買 {fb}／賣 {fs}；容許 ±{tb:.1f}／±{ts:.1f}"))
    if quote is None or pd.isna(quote.get("volume_shares")):
        out.append(skip("T5", "隱含成交量與官方成交量一致", "W", "無當日行情"))
        out.append(skip("T6", "平均成本在 [最低, 最高]", "B", "無當日行情"))
    else:
        # T5：富邦「佔成交比重」的分母與官方成交股數口徑不同，而且每檔不同（實測 2330 −4.7%、6488 +10.3%）。
        # 因此有足夠歷史時，比的是「今日比值」與「該股近 20 日比值中位數」，固定的口徑偏差會互相抵消；
        # 某天突然偏離，代表那天的佔比或成交量對不上（例如抓到他日頁面）。歷史不足時退回直接比較，只警告。
        vol_lots = float(quote["volume_shares"]) / 1000.0
        implied = implied_volume_lots(rows)
        if implied is None or vol_lots <= 0:
            out.append(skip("T5", "隱含成交量與官方成交量一致", "W", "無可用佔比"))
        else:
            big = rows["pct"].fillna(0) >= 0.5
            pmin = float(rows.loc[big, "pct"].min()) if big.any() else float(rows["pct"].max())
            rounding = 0.005 / pmin  # 佔比只有兩位小數造成的誤差
            ratio = implied / vol_lots
            if ref_ratio is not None and ref_ratio > 0:
                dev = ratio / ref_ratio - 1
                allow = t5_rel_tol + rounding
                out.append((ok if abs(dev) <= allow else bad)(
                    "T5", "隱含成交量比值與該股近 20 日一致", t5_level,
                    f"今日比值 {ratio:.4f}，近 20 日中位數 {ref_ratio:.4f}，偏離 {dev:+.2%}（容許 {allow:.2%}）"))
            else:
                allow = tol + rounding
                out.append((ok if abs(ratio - 1) <= allow else bad)(
                    "T5", "隱含成交量與官方成交量一致（歷史不足，直接比較）", "W",
                    f"隱含 {implied:,.0f} 張，官方 {vol_lots:,.0f} 張，差 {ratio - 1:+.2%}（容許 {allow:.2%}）"))
        lo, hi = quote.get("low"), quote.get("high")
        bad6 = []
        for k in ("avg_buy_cost", "avg_sell_cost"):
            v = summary.get(k)
            if v is not None and pd.notna(lo) and pd.notna(hi) and not (float(lo) - 0.01 <= v <= float(hi) + 0.01):
                bad6.append(f"{k}={v} 不在 [{lo}, {hi}]")
        out.append((ok if not bad6 else bad)("T6", "平均成本在 [最低, 最高]", "B", "; ".join(bad6) or f"[{lo}, {hi}]"))
    viol = 0
    for side in ("buy", "sell"):
        s = rows[rows["side"] == side].sort_values("rank")["net_lots"].abs().astype(float)
        viol += int((s.diff() > 1).sum())  # 上游以股排序、顯示四捨五入成張，容許 1 張
    out.append((ok if viol == 0 else bad)("T7", "名次依張數遞減（容許 1 張）", "W", f"{viol} 處違反"))
    return out
