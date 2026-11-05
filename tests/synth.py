"""合成市場：數字彼此一致（買賣 = 淨額、法人買賣 ≤ 成交量、Δ外資持股 ≈ 外資買賣超、分點佔比 = 淨額/成交量）。

用來注入舊專案真的發生過的錯誤，驗證每一種都會被檢查擋下。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from twchip.sources import Parsed

GROUP_SUM = {"foreign": ("fx", "fd"), "dealer": ("ds", "dh")}


def trading_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class Market:
    def __init__(self, days: list[date], n_stocks: int = 60, seed: int = 0):
        rng = np.random.default_rng(seed)
        self.days = days
        self.codes = {"TWSE": [f"{1101 + i}" for i in range(n_stocks)], "TPEX": [f"{6101 + i}" for i in range(n_stocks)]}
        self.quotes, self.flows, self.hold = {}, {}, {}
        for m, codes in self.codes.items():
            issued = rng.integers(50_000_000, 2_000_000_000, len(codes))
            fshares = (issued * rng.uniform(0.05, 0.6, len(codes))).astype(np.int64)
            close = rng.uniform(20, 800, len(codes)).round(2)
            for d in days:
                vol = rng.integers(2_000_000, 40_000_000, len(codes))
                ch = (close * rng.uniform(-0.05, 0.05, len(codes))).round(2)
                new_close = (close + ch).round(2)
                hi = (np.maximum(close, new_close) * 1.01).round(2)
                lo = (np.minimum(close, new_close) * 0.99).round(2)
                op = close.copy()
                avgp = ((hi + lo) / 2)
                self.quotes[(m, d)] = pd.DataFrame({
                    "date": d, "market": m, "code": codes, "name": [f"股{c}" for c in codes],
                    "open": op, "high": hi, "low": lo, "close": new_close, "change": ch, "ex_right": False,
                    "volume_shares": vol, "value_twd": (vol * avgp).astype(np.int64), "trades": vol // 1000})
                close = new_close
                f = {"date": d, "market": m, "code": codes, "name": [f"股{c}" for c in codes]}
                for g in ("fx", "fd", "trust", "ds", "dh"):
                    share = {"fx": 0.25, "fd": 0.02, "trust": 0.05, "ds": 0.03, "dh": 0.03}[g]
                    b = (vol * rng.uniform(0, share, len(codes))).astype(np.int64)
                    s = (vol * rng.uniform(0, share, len(codes))).astype(np.int64)
                    f[f"{g}_buy"], f[f"{g}_sell"], f[f"{g}_net"] = b, s, b - s
                for tot, (a, b2) in GROUP_SUM.items():
                    for k in ("buy", "sell", "net"):
                        f[f"{tot}_{k}"] = f[f"{a}_{k}"] + f[f"{b2}_{k}"]
                f["total_net"] = f["foreign_net"] + f["trust_net"] + f["dealer_net"]
                fl = pd.DataFrame(f)
                if m == "TWSE":  # TWSE 官方沒有外資合計、自營買進賣出合計
                    pass
                self.flows[(m, d)] = fl
                fshares = np.clip(fshares + fl["foreign_net"].to_numpy() +
                                  rng.integers(-2000, 2000, len(codes)), 0, issued)
                self.hold[(m, d)] = pd.DataFrame({
                    "date": d, "market": m, "code": codes, "name": [f"股{c}" for c in codes],
                    "issued_shares": issued, "foreign_available_shares": (issued * 1.0 - fshares).astype(np.int64),
                    "foreign_shares": fshares, "available_ratio": ((issued - fshares) / issued * 100).round(2),
                    "foreign_ratio": (fshares / issued * 100).round(2), "limit_ratio": 100.0})

    # ---------- 產生 Parsed ----------
    def parsed(self, ds: str, m: str, d: date) -> Parsed:
        src = {"quotes": self.quotes, "inst_flows": self.flows, "foreign_holdings": self.hold}[ds]
        if (m, d) not in src:
            return Parsed(ds, m, pd.DataFrame(), None, no_data=True)
        return Parsed(ds, m, src[(m, d)].copy(), d)

    def broker(self, code: str, d: date) -> Parsed:
        m = "TWSE" if code in self.codes["TWSE"] else "TPEX"
        q = self.quotes[(m, d)].set_index("code").loc[code]
        vol_lots = q["volume_shares"] / 1000
        rng = np.random.default_rng(int(code) * 1000 + d.toordinal())
        rows = []
        for side in ("buy", "sell"):
            nets = sorted(rng.integers(20, int(vol_lots * 0.06), 15), reverse=True)
            for i, n in enumerate(nets, 1):
                other = int(rng.integers(0, 50))
                buy, sell = (n + other, other) if side == "buy" else (other, n + other)
                rows.append({"date": d, "code": code, "side": side, "rank": i, "broker_name": f"券商{side}{i}",
                             "bhid": f"{9000 + i}{'B' if side == 'buy' else 'S'}", "broker_b": "",
                             **({"bhid": "9100", "broker_b": ("918G" if i == 1 else "918g").encode("utf-16-be").hex()}
                                if i <= 2 else {}),   # 分點代號區分大小寫：同一總公司的兩個不同分點
                             "buy_lots": buy, "sell_lots": sell, "net_lots": n if side == "buy" else -n,
                             "pct": round(n / vol_lots * 100, 2)})
        df = pd.DataFrame(rows)
        mid = float((q["high"] + q["low"]) / 2)
        summary = {"date": d, "code": code, "n_buy": 15, "n_sell": 15,
                   "total_buy_net_lots": int(df.loc[df.side == "buy", "net_lots"].sum()),
                   "total_sell_net_lots": int(-df.loc[df.side == "sell", "net_lots"].sum()),
                   "avg_buy_cost": round(mid, 2), "avg_sell_cost": round(mid, 2)}
        return Parsed("broker_top", None, df, d, extras={"summary": summary})
