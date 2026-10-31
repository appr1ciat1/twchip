"""集中設定。所有值都可用環境變數覆寫，方便 CI 與本機共用。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

TPE = ZoneInfo("Asia/Taipei")

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
