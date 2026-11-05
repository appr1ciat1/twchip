"""集中設定。所有值都可用環境變數覆寫，方便 CI 與本機共用。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

TPE = ZoneInfo("Asia/Taipei")

# F4 依市場的校正值（2026-11-05，273 個交易日；見 docs/DESIGN.md §4.6）。
# 上市 273 天每天都是 0 檔超量；上櫃正常最高 1.8%，乘 1.5 倍安全距離後取 3%。
F4_TOL_DEFAULT = {"TWSE": 0.005, "TPEX": 0.03}
F4_TOL_FALLBACK = 0.03   # 未知市場（理論上不會發生）

# 預設追蹤券商分點的股票。可用 TWCHIP_BROKER_CODES="2330,2317" 覆寫。
DEFAULT_BROKER_CODES = [
    "2330", "2317", "2454", "2308", "2382", "2891", "2881", "2882", "2303", "3711",
    "2603", "2609", "2615", "2618", "3037", "2345", "3006", "2379", "3034", "2412",
]


def _env_list(name: str, default: list[str]) -> list[str]:
    v = os.environ.get(name, "").strip()
    return [x.strip() for x in v.split(",") if x.strip()] if v else list(default)


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("TWCHIP_DATA_DIR", "data")))
    hf_repo: str = field(default_factory=lambda: os.environ.get("TWCHIP_HF_REPO", ""))
    broker_codes: list[str] = field(default_factory=lambda: _env_list("TWCHIP_BROKER_CODES", DEFAULT_BROKER_CODES))
    # 各主機兩次請求的最小間隔（秒）。TWSE 每 5 秒超過約 3 次會封鎖 IP。
    min_interval: dict[str, float] = field(default_factory=lambda: {
        "www.twse.com.tw": float(os.environ.get("TWCHIP_TWSE_INTERVAL", "3.2")),
        "www.tpex.org.tw": float(os.environ.get("TWCHIP_TPEX_INTERVAL", "2.0")),
        "fubon-ebrokerdj.fbs.com.tw": float(os.environ.get("TWCHIP_FUBON_INTERVAL", "1.5")),
        "bsr.twse.com.tw": 2.0,
    })
    # 交易日 D 的資料若到 D 之後這麼多小時仍未通過驗證，就告警（工作流程失敗）。
    sla_hours: float = float(os.environ.get("TWCHIP_SLA_HOURS", "30"))
    # T5：富邦隱含成交量與官方成交量的容許相對誤差。首次 probe 後可依實測校正。
    broker_volume_tol: float = float(os.environ.get("TWCHIP_BROKER_VOLUME_TOL", "0.03"))
    # 校正用門檻。預設值就是 2026-11-05 校正後的正式值（docs/DESIGN.md §4.6），
    # 所以即使 GitHub Variables 被刪除，也不會悄悄放寬。Variables 只用來臨時調整；空字串視同未設定。
    # T5：當日「隱含成交量 ÷（官方成交量 − 鉅額交易）」與該股近 20 個交易日中位數的相對差距上限。
    t5_rel_tol: float = field(default_factory=lambda: float(os.environ.get("TWCHIP_T5_REL_TOL") or "0.15"))
    # T5 等級：W（只警告）或 B（阻擋）。驗證集偵測率 77.1% < 80%，依規則維持 W。
    t5_level: str = field(default_factory=lambda: (os.environ.get("TWCHIP_T5_LEVEL") or "W").upper())
    # 計算 T5 基準所需的最少歷史天數；不足時退回與官方成交量直接比較（僅警告）。
    t5_min_history: int = field(default_factory=lambda: int(os.environ.get("TWCHIP_T5_MIN_HISTORY") or "10"))
    # F4：法人買進／賣出超過當日成交股數的檔數比例上限。TWCHIP_F4_TOL 會同時套用到兩個市場；
    # 沒設定（None）時用 F4_TOL_DEFAULT 依市場的校正值。
    f4_tol: Optional[float] = field(default_factory=lambda: float(os.environ["TWCHIP_F4_TOL"])
                                    if os.environ.get("TWCHIP_F4_TOL") else None)

    # 部分官方資料在當晚會先發布「未定稿」的數字，之後才修正。這些單位要等到交易日 0 時之後
    # 這麼多小時才抓。證據：舊專案 2026-07-03 起改為當晚約 20:00~22:40 抓上櫃外資持股，
    # 7/3~8/26 每天 40%~70% 的股票持股數比定稿值少（中位數 0.26%）；改在 23:00 後或隔日抓的日子全部吻合。
    settle_hours: dict = field(default_factory=lambda: {("foreign_holdings", "TPEX"): 24.0})
    # 人工豁免清單（放在 repo，受版本控制）。格式見 waivers.json。
    waivers_path: Path = field(default_factory=lambda: Path(os.environ.get("TWCHIP_WAIVERS") or "waivers.json"))

    def f4_tol_for(self, market: str) -> float:
        """F4 門檻，越具體的設定優先：TWCHIP_F4_TOL_{市場} → TWCHIP_F4_TOL → 依市場的校正值。
        回補實測：上市 273 天全部是 0；上櫃最高 1.8%（櫃買行情不含盤後定價，法人買賣有含）。"""
        v = os.environ.get(f"TWCHIP_F4_TOL_{market.upper()}")
        if v:
            return float(v)
        if self.f4_tol is not None:
            return self.f4_tol
        return F4_TOL_DEFAULT.get(market.upper(), F4_TOL_FALLBACK)

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def curated_dir(self) -> Path:
        return self.data_dir / "curated"

    @property
    def quarantine_dir(self) -> Path:
        return self.data_dir / "quarantine"

    @property
    def quality_dir(self) -> Path:
        return self.data_dir / "quality"

    @property
    def meta_dir(self) -> Path:
        return self.data_dir / "meta"

    @property
    def legacy_dir(self) -> Path:
        return self.data_dir / "legacy"
