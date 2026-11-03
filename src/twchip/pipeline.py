"""每日管線：抓取 → 檢查 → 通過才發布。

待辦由「資料狀態」決定，而不是執行當下的時間：
  pending = 日曆上的交易日（含尚未公布的平日）− 已發布的 (資料集, 市場, 日期)
因此排程延遲幾小時、重跑幾次都沒關係。
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import pandas as pd

from .calendar import SEED_UNSCHEDULED, Calendar
from .checks import CheckResult, any_blocking, bad, ok
from .checks import rules
from .config import TPE, Config
from .http import FetchError, Fetcher
from .rawstore import RawStore
from .sources import Parsed
from .store import Store

MARKETS = ("TWSE", "TPEX")
UnitFn = Callable[[date], Parsed]
BrokerFn = Callable[[str, date], Parsed]


def default_sources(fx: Fetcher) -> tuple[dict[tuple[str, str], UnitFn], BrokerFn]:
    from .sources import fubon, tpex, twse
    units = {
        ("quotes", "TWSE"): lambda d: twse.fetch_mi_index(fx, d),
        ("quotes", "TPEX"): lambda d: tpex.fetch_otc_quotes(fx, d),
        ("inst_flows", "TWSE"): lambda d: twse.fetch_t86(fx, d),
        ("inst_flows", "TPEX"): lambda d: tpex.fetch_daily_trade(fx, d),
        ("foreign_holdings", "TWSE"): lambda d: twse.fetch_mi_qfiis(fx, d),
        ("foreign_holdings", "TPEX"): lambda d: tpex.fetch_qfii(fx, d),
    }
    return units, lambda code, d: fubon.fetch_broker_top(fx, code, d)


@dataclass
class UnitOutcome:
    status: str                      # published | quarantined | not_published | blocked | error | cached | skipped
    detail: str = ""
    rows: int = 0
    served_date: Optional[str] = None
    checks: list[dict] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.status in ("published", "cached")


class Pipeline:
    def __init__(self, cfg: Config, units: Optional[dict] = None, broker_fn: Optional[BrokerFn] = None,
                 now: Optional[Callable[[], datetime]] = None, log: Callable[[str], None] = print):
        self.cfg = cfg
        self.store = Store(cfg)
        self.raw = RawStore(cfg.data_dir)
        self.fx = Fetcher(self.raw, cfg.min_interval)
        if units is None or broker_fn is None:
            du, db = default_sources(self.fx)
            units, broker_fn = units or du, broker_fn or db
        self.units, self.broker_fn = units, broker_fn
        self.now = now or (lambda: datetime.now(TPE))
        self.log = log
        self.cal = Calendar(cfg.meta_dir / "calendar.parquet")
        self.cal.add_seed()

    # ------------------------------------------------------------ 公開介面

    def ensure_holidays(self, years: list[int]) -> None:
        from .sources import twse
        for y in years:
            if any(d.year == y and not r.startswith("seed:") for d, r in self.cal.holidays.items()):
                continue
            try:
                self.cal.add_holidays(twse.fetch_holidays(self.fx, y))
                self.log(f"[calendar] 已載入 {y} 官方休市表")
            except Exception as e:  # noqa: BLE001
                self.log(f"[calendar] {y} 官方休市表抓取失敗，暫用種子：{e}")

    def run(self, start: date, end: date, datasets: Optional[list[str]] = None, force: bool = False,
            fetch_holidays: bool = True) -> dict[date, dict]:
        datasets = datasets or ["quotes", "inst_flows", "foreign_holdings", "broker_top"]
        if fetch_holidays:
            self.ensure_holidays(sorted({start.year, end.year}))
        out = {}
        for d in self.cal.days(start, end, ("trading", "expected", "conflict"), self.now()):
            try:
                out[d] = self.run_day(d, datasets, force)
            except Exception:  # noqa: BLE001  單日失敗不影響其他日
                self.log(f"[{d}] 未預期錯誤：\n{traceback.format_exc()}")
                out[d] = {"error": traceback.format_exc()}
            finally:
                self.cal.save()
        return out

    def run_day(self, d: date, datasets: list[str], force: bool = False) -> dict:
        rep = self.store.read_quality(d) or {"date": d.isoformat(), "units": {}}
        rep["run_at"] = self.now().isoformat()
        units: dict[str, dict] = rep.setdefault("units", {})

        # 1) 行情：決定這天是不是交易日，也是所有跨表檢查的基準
        for m in MARKETS:
            o = self._unit(d, "quotes", m, force)
            units[f"quotes/{m}"] = o.__dict__
        tw = self._has(d, "quotes", "TWSE", units)
        tp = self._has(d, "quotes", "TPEX", units)
        self.cal.observe(d, tw, tp)
        st = self.cal.status(d, self.now())
        rep["calendar"] = {"status": st.status, "reason": st.reason}
        if st.status != "trading":
            self.log(f"[{d}] 日曆狀態 {st.status}（{st.reason}），其餘資料集暫不處理")
            self.store.write_quality(d, rep)
            return rep

        # 2) 其餘資料集，依相依順序
        for ds in ("inst_flows", "foreign_holdings"):
            if ds in datasets:
                for m in MARKETS:
                    units[f"{ds}/{m}"] = self._unit(d, ds, m, force).__dict__
        if "broker_top" in datasets:
            units["broker_top/ALL"] = self._broker(d, force)
        self.store.write_quality(d, rep)
        done = sum(1 for u in units.values() if u.get("status") in ("published", "cached"))
        self.log(f"[{d}] 完成 {done}/{len(units)} 個單位：" +
                 ", ".join(f"{k}={v.get('status')}" for k, v in units.items()))
        return rep

    # ------------------------------------------------------------ 單位處理

    def _has(self, d, ds, m, units) -> Optional[bool]:
        if self.store.has_curated(ds, m, d):
            return True
        st = units.get(f"{ds}/{m}", {}).get("status")
        if st == "not_published":
            return False
        if st == "quarantined":
            return True  # 有資料，只是沒通過檢查
        return None

    def _deps(self, d: date, ds: str, m: str) -> tuple[Optional[str], dict]:
        """回傳 (缺少的相依, 跨表檢查需要的資料)。"""
        prev = self.cal.previous_trading_day(d)
        ctx = {"prev": self.store.read_curated(ds, m, prev), "prev_date": prev}
        if ds == "inst_flows":
            q = self.store.read_curated("quotes", m, d)
            if q is None:
                return "quotes 尚未發布", ctx
            ctx["quotes"] = q
        if ds == "foreign_holdings":
            fl = self.store.read_curated("inst_flows", m, d)
            if fl is None:
                return "inst_flows 尚未發布", ctx
            ctx["flows_d"] = fl
            ctx["flows_prev"] = self.store.read_curated("inst_flows", m, prev)
        return None, ctx

    def _waivers(self) -> list[dict]:
        if not hasattr(self, "_waiver_cache"):
            import json
            p = self.cfg.waivers_path
            self._waiver_cache = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        return self._waiver_cache

    def _apply_waivers(self, d: date, unit: str, res: list[CheckResult]) -> list[CheckResult]:
        """人工豁免：只對清單中指定的 (日期, 單位, 檢查)，且違反筆數不超過記錄值時，把阻擋改成警告。
        資料若在豁免之後變了（違反筆數變多），豁免自動失效，照樣隔離。"""
        out = []
        for r in res:
            w = next((w for w in self._waivers() if r.blocking_fail and w["date"] == d.isoformat()
                      and w["unit"] == unit and w["check"] == r.id and r.n_bad <= int(w["max_n_bad"])), None)
            if w is not None:
                r = CheckResult(r.id, r.name, "W", r.status, f"{r.detail}［人工豁免：{w['reason']}］",
                                r.n_bad, r.examples)
                self.log(f"[{d}] {unit} {r.id} 依豁免清單改為警告：{w['reason']}")
            out.append(r)
        return out

    def _unit(self, d: date, ds: str, m: str, force: bool) -> UnitOutcome:
        if self.store.has_curated(ds, m, d) and not force:
            return UnitOutcome("cached")
        missing, ctx = self._deps(d, ds, m)
        if missing:
            return UnitOutcome("blocked", missing)
        try:
            p = self.units[(ds, m)](d)
        except FetchError as e:
            return UnitOutcome("error", str(e))
        raw = [str(r.path) for r in p.raw]
        if p.no_data:
            return UnitOutcome("not_published", "官方回應無資料", served_date=str(p.served_date), raw=raw)
        res = rules.generic(p, d, ds, self.store.recent_counts(ds, m, d), ctx["prev"])
        if ds == "quotes":
            res += rules.quotes(p.df, ctx["prev"])
        elif ds == "inst_flows":
            res += rules.inst_flows(p.df, ctx["quotes"], self.cfg.f4_tol_for(m))
        elif ds == "foreign_holdings":
            res += rules.foreign_holdings(p.df, ctx["prev"], ctx["flows_d"], ctx.get("flows_prev"))
        res = self._apply_waivers(d, f"{ds}/{m}", res)
        checks = [r.to_dict() for r in res]
        if any_blocking(res):
            self.store.write_quarantine(ds, m, d, p.df)
            fails = [f"{r.id}:{r.detail}" for r in res if r.blocking_fail]
            self.log(f"[{d}] {ds}/{m} 隔離：{fails}")
            return UnitOutcome("quarantined", "; ".join(fails), len(p.df), str(p.served_date), checks, raw)
        self.store.write_curated(ds, m, d, p.df)
        return UnitOutcome("published", "", len(p.df), str(p.served_date), checks, raw)

    def _broker(self, d: date, force: bool) -> dict:
        prev_rep = (self.store.read_quality(d).get("units") or {}).get("broker_top/ALL", {})
        exempt = set(prev_rep.get("exempt", {}))
        cur = None if force else self.store.read_curated("broker_top", "ALL", d)
        cur_sum = None if force else self.store.read_curated("broker_top_summary", "ALL", d)
        have = set(cur["code"]) if cur is not None else set()
        quotes = pd.concat([x for x in (self.store.read_curated("quotes", m, d) for m in MARKETS) if x is not None]
                           or [pd.DataFrame()], ignore_index=True)
        if quotes.empty:
            return {"status": "blocked", "detail": "quotes 尚未發布"}
        qidx = quotes.drop_duplicates("code").set_index("code")
        prev = self.store.read_curated("broker_top", "ALL", self.cal.previous_trading_day(d))
        ref = self._t5_reference(d, quotes)
        rows, sums, per, quarantine = [], [], {}, []
        for code in self.cfg.broker_codes:
            if code in have or code in exempt:
                continue
            quote = qidx.loc[code] if code in qidx.index else None
            if quote is None or (pd.notna(quote.get("volume_shares")) and quote["volume_shares"] == 0):
                exempt.add(code)
                per[code] = {"status": "exempt", "detail": "當日無成交"}
                continue
            try:
                p = self.broker_fn(code, d)
            except FetchError as e:
                per[code] = {"status": "error", "detail": str(e)}
                continue
            summ = p.extras.get("summary", {})
            if not p.df.empty:
                summ["implied_vol_lots"] = rules.implied_volume_lots(p.df)
            res = rules.broker_stock(p.df, summ, p.served_date, d, quote, self.cfg.broker_volume_tol,
                                     ref.get(code), self.cfg.t5_level, self.cfg.t5_rel_tol) \
                if not p.df.empty else [bad("T2", "買超、賣超各 ≤ 15 列且非空", "B", "頁面無資料")]
            if prev is not None and not p.df.empty:
                cols = rules.FINGERPRINT_COLS["broker_top"]
                a = set(map(tuple, p.df[cols].astype(str).values))
                b = set(map(tuple, prev[prev["code"] == code][cols].astype(str).values))
                frac = len(a & b) / len(a) if a else 0
                res.append((ok if frac <= 0.5 else bad)("G5", "與前一交易日不同", "B", f"{frac:.0%} 列相同"))
            blocked = any_blocking(res)
            per[code] = {"status": "quarantined" if blocked else "published",
                         "checks": [r.to_dict() for r in res], "raw": [str(r.path) for r in p.raw]}
            if blocked:
                quarantine.append(p.df)
                self.log(f"[{d}] broker {code} 隔離：{[f'{r.id}:{r.detail}' for r in res if r.blocking_fail]}")
            else:
                rows.append(p.df)
                sums.append(summ)
        if rows:
            new = pd.concat(([cur] if cur is not None else []) + rows, ignore_index=True)
            self.store.write_curated("broker_top", "ALL", d, new)
            ns = pd.DataFrame(sums)
            self.store.write_curated("broker_top_summary", "ALL", d,
                                     pd.concat(([cur_sum] if cur_sum is not None else []) + [ns], ignore_index=True))
        if quarantine:
            self.store.write_quarantine("broker_top", "ALL", d, pd.concat(quarantine, ignore_index=True))
        have |= {c for c, v in per.items() if v["status"] == "published"}
        todo = [c for c in self.cfg.broker_codes if c not in have and c not in exempt]
        merged_per = {**prev_rep.get("stocks", {}), **per}
        return {"status": "published" if not todo else ("quarantined" if quarantine else "pending"),
                "detail": f"完成 {len(have)}，豁免 {len(exempt)}，待處理 {todo}",
                "stocks": merged_per, "exempt": {c: "當日無成交" for c in exempt}}

    def _t5_reference(self, d: date, quotes_today: pd.DataFrame) -> dict[str, float]:
        """各股近 20 個交易日（不含 d）「隱含成交量 ÷ 官方成交量」的中位數；天數不足的股票不列入。"""
        start = d - timedelta(days=45)
        summ = self.store.read_range("broker_top_summary", "ALL", start, d - timedelta(days=1))
        if summ.empty or "implied_vol_lots" not in summ.columns:
            return {}
        q = self.store.read_range("quotes", None, start, d - timedelta(days=1))
        if q.empty:
            return {}
        j = summ.merge(q[["date", "code", "volume_shares"]], on=["date", "code"])
        j = j[j["implied_vol_lots"].notna() & (j["volume_shares"] > 0)]
        j["ratio"] = j["implied_vol_lots"].astype(float) / (j["volume_shares"].astype(float) / 1000.0)
        j = j.sort_values("date").groupby("code").tail(20)
        g = j.groupby("code")["ratio"].agg(["median", "size"])
        g = g[g["size"] >= self.cfg.t5_min_history]
        return g["median"].to_dict()

    # ------------------------------------------------------------ 告警

    def overdue(self, lookback_days: int = 10) -> list[str]:
        """超過 SLA 仍未完成的單位，以及尚未人工確認的臨時休市。"""
        now = self.now()
        end = now.date()
        issues = []
        for d in self.cal.days(end - timedelta(days=lookback_days), end,
                               ("trading", "expected", "conflict", "closed_unscheduled"), now):
            st = self.cal.status(d, now)
            if st.status == "conflict":
                issues.append(f"{d} 日曆矛盾：{st.reason}")
                continue
            if st.status == "closed_unscheduled":
                if d.isoformat() not in SEED_UNSCHEDULED:
                    issues.append(f"{d} 推定為臨時休市（上市上櫃皆無行情），請人工確認")
                continue
            age_h = (now - datetime.combine(d, datetime.min.time(), tzinfo=TPE)).total_seconds() / 3600
            if age_h < self.cfg.sla_hours:
                continue
            units = self.store.read_quality(d).get("units", {})
            expected = [f"{ds}/{m}" for ds in ("quotes", "inst_flows", "foreign_holdings") for m in MARKETS] + \
                       ["broker_top/ALL"]
            for u in expected:
                ds, m = u.split("/")
                if not self.store.has_curated(ds, m, d) or (u == "broker_top/ALL" and units.get(u, {}).get("status") != "published"):
                    issues.append(f"{d} {u} 逾時未完成（{units.get(u, {}).get('status', '未執行')}：{units.get(u, {}).get('detail', '')}）")
        return issues
