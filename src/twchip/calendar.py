"""交易日曆：所有資料集對齊的基礎。

判定優先序（設計文件 §3）：
  1. 週末 → weekend
  2. 官方休市表 → holiday
  3. 平日且已觀察到行情 → trading
  4. 平日、非休市表、已過隔日 16:00 仍無任何行情 → closed_unscheduled（颱風等臨時休市）
  5. 其餘 → expected（尚未公布）
休市表與實際觀察矛盾時記成 conflict，交給檢查 G6 處理。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from .config import TPE

# 官方表中「不是休市」的項目（例如開始交易日）。
_NOT_CLOSED = ("開始交易", "最後交易", "開市", "封關")
_CLOSED = ("休市", "放假", "補假", "無交易", "停止交易", "紀念日", "節", "國慶", "元旦", "春節", "除夕")

# 備用種子：官方休市表抓不到時使用。2026 年依證交所 115 年行事曆；颱風休市為新聞查證。
SEED_CLOSURES = {
    "2025-09-29": "教師節補假", "2025-10-06": "中秋節", "2025-10-10": "國慶日", "2025-10-24": "光復節",
    "2025-12-25": "行憲紀念日", "2026-01-01": "開國紀念日", "2026-02-12": "春節前無交易",
    "2026-02-13": "春節前無交易", "2026-02-16": "春節", "2026-02-17": "春節", "2026-02-18": "春節",
    "2026-02-19": "春節", "2026-02-20": "春節補假", "2026-02-27": "和平紀念日補假", "2026-04-03": "兒童節補假",
    "2026-04-06": "清明補假", "2026-05-01": "勞動節", "2026-06-19": "端午節", "2026-09-25": "中秋節",
    "2026-09-28": "教師節", "2026-10-09": "國慶日補假", "2026-10-26": "光復節補假", "2026-12-25": "行憲紀念日",
}
SEED_UNSCHEDULED = {"2026-07-10": "颱風休市（巴威）", "2026-10-23": "颱風休市"}


def _b(x) -> Optional[bool]:
    if x is None or x is pd.NA:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return bool(x)


def is_closure(name: str, desc: str = "") -> bool:
    t = f"{name} {desc}"
    if any(k in t for k in _NOT_CLOSED) and "無交易" not in t:
        return False
    return any(k in t for k in _CLOSED)


@dataclass
class DayStatus:
    date: date
    status: str          # weekend | holiday | trading | closed_unscheduled | expected | conflict
    reason: str = ""


class Calendar:
    COLUMNS = ["date", "status", "reason", "observed_twse", "observed_tpex", "updated_at"]

    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path.exists():
            df = pd.read_parquet(self.path)
            df["date"] = pd.to_datetime(df["date"]).dt.date
            self.df = df.set_index("date")
        else:
            self.df = pd.DataFrame(columns=self.COLUMNS).set_index("date")
        self.holidays: dict[date, str] = {}
        hp = self.path.with_name("holidays.parquet")
        if hp.exists():
            h = pd.read_parquet(hp)
            self.holidays = {pd.Timestamp(x).date(): r for x, r in zip(h["date"], h["reason"])}

    # ---------- 輸入 ----------
    def add_holidays(self, df: pd.DataFrame, source: str = "twse") -> None:
        for r in df.itertuples():
            if is_closure(r.name, getattr(r, "description", "")):
                self.holidays[r.date] = f"{source}:{r.name}"

    def add_seed(self) -> None:
        for k, v in SEED_CLOSURES.items():
            self.holidays.setdefault(date.fromisoformat(k), f"seed:{v}")

    def observe(self, d: date, twse_has_quotes: Optional[bool], tpex_has_quotes: Optional[bool]) -> None:
        cur = self.df.loc[d] if d in self.df.index else None
        tw = twse_has_quotes if twse_has_quotes is not None else (None if cur is None else _b(cur["observed_twse"]))
        tp = tpex_has_quotes if tpex_has_quotes is not None else (None if cur is None else _b(cur["observed_tpex"]))
        self.df.loc[d, ["observed_twse", "observed_tpex", "updated_at"]] = [tw, tp, datetime.now(TPE).isoformat()]
        st = self.status(d)
        self.df.loc[d, ["status", "reason"]] = [st.status, st.reason]

    # ---------- 查詢 ----------
    def status(self, d: date, now: Optional[datetime] = None) -> DayStatus:
        now = now or datetime.now(TPE)
        if d.weekday() >= 5:
            return DayStatus(d, "weekend")
        obs = self.df.loc[d] if d in self.df.index else None
        tw = _b(obs["observed_twse"]) if obs is not None else None
        tp = _b(obs["observed_tpex"]) if obs is not None else None
        seen = tw is True or tp is True
        both_empty = tw is False and tp is False
        if d in self.holidays:
            if seen:
                return DayStatus(d, "conflict", f"休市表 {self.holidays[d]}，但有行情")
            return DayStatus(d, "holiday", self.holidays[d])
        if seen:
            return DayStatus(d, "trading")
        deadline = datetime.combine(d + timedelta(days=1), time(16, 0), tzinfo=TPE)
        if both_empty and now >= deadline:
            return DayStatus(d, "closed_unscheduled", SEED_UNSCHEDULED.get(d.isoformat(), "平日無行情（臨時休市？）"))
        return DayStatus(d, "expected")

    def days(self, start: date, end: date, statuses: Iterable[str] = ("trading", "expected"),
             now: Optional[datetime] = None) -> list[date]:
        out, cur = [], start
        statuses = set(statuses)
        while cur <= end:
            if self.status(cur, now).status in statuses:
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def previous_trading_day(self, d: date, lookback: int = 20) -> Optional[date]:
        cur = d - timedelta(days=1)
        for _ in range(lookback):
            if self.status(cur).status == "trading":
                return cur
            cur -= timedelta(days=1)
        return None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"date": list(self.holidays), "reason": list(self.holidays.values())}).sort_values(
            "date").to_parquet(self.path.with_name("holidays.parquet"), index=False)
        out = self.df.reset_index().rename(columns={"index": "date"})
        for c in ("observed_twse", "observed_tpex"):
            out[c] = out[c].astype("boolean")
        out.to_parquet(self.path, index=False)
