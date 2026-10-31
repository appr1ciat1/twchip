"""本機資料庫：curated／quarantine／quality 的讀寫。

單位（unit）= (資料集, 市場)；檔案以「交易日」為粒度：
  curated/{dataset}/{market}/{YYYY}/{YYYY-MM-DD}.parquet
重跑某日只會整檔替換，天然冪等。寫入一律先寫暫存檔再原子改名。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import Config
from .schemas import conform


class Store:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.written: list[Path] = []   # 本次執行寫過的檔案（供 HF 增量上傳）

    # ---------- 路徑 ----------
    def _p(self, root: Path, dataset: str, market: str, d: date) -> Path:
        return root / dataset / market / f"{d:%Y}" / f"{d.isoformat()}.parquet"

    def curated_path(self, dataset: str, market: str, d: date) -> Path:
        return self._p(self.cfg.curated_dir, dataset, market, d)

    def quality_path(self, d: date) -> Path:
        return self.cfg.quality_dir / f"{d:%Y}" / f"{d.isoformat()}.json"

    # ---------- 寫 ----------
    def _write(self, df: pd.DataFrame, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        df.to_parquet(tmp, index=False, compression="zstd")
        tmp.replace(path)
        self.written.append(path)

    def write_curated(self, dataset: str, market: str, d: date, df: pd.DataFrame) -> Path:
        p = self.curated_path(dataset, market, d)
        self._write(conform(df, dataset), p)
        q = self._p(self.cfg.quarantine_dir, dataset, market, d)
        if q.exists():
            q.unlink()  # 通過後移除舊的隔離檔
        return p

    def write_quarantine(self, dataset: str, market: str, d: date, df: pd.DataFrame) -> Path:
        p = self._p(self.cfg.quarantine_dir, dataset, market, d)
        try:
            df = conform(df, dataset)
        except Exception:  # noqa: BLE001  隔離區保留原樣，方便除錯
            pass
        self._write(df, p)
        return p

    def write_quality(self, d: date, report: dict) -> Path:
        p = self.quality_path(d)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(p)
        self.written.append(p)
        return p

    # ---------- 讀 ----------
    def read_curated(self, dataset: str, market: str, d: Optional[date]) -> Optional[pd.DataFrame]:
        if d is None:
            return None
        p = self.curated_path(dataset, market, d)
        if not p.exists():
            return None
        df = pd.read_parquet(p)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    def has_curated(self, dataset: str, market: str, d: date) -> bool:
        return self.curated_path(dataset, market, d).exists()

    def read_quality(self, d: date) -> dict:
        p = self.quality_path(d)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def curated_dates(self, dataset: str, market: str) -> list[date]:
        root = self.cfg.curated_dir / dataset / market
        return sorted(date.fromisoformat(p.stem) for p in root.glob("*/*.parquet")) if root.exists() else []

    def recent_counts(self, dataset: str, market: str, before: date, n: int = 20) -> list[int]:
        import pyarrow.parquet as pq
        ds = [x for x in self.curated_dates(dataset, market) if x < before][-n:]
        return [pq.ParquetFile(self.curated_path(dataset, market, x)).metadata.num_rows for x in ds]

    def read_range(self, dataset: str, market: Optional[str] = None,
                   start: Optional[date] = None, end: Optional[date] = None) -> pd.DataFrame:
        root = self.cfg.curated_dir / dataset
        files = sorted(root.glob(f"{market or '*'}/*/*.parquet")) if root.exists() else []
        files = [f for f in files if (start is None or date.fromisoformat(f.stem) >= start)
                 and (end is None or date.fromisoformat(f.stem) <= end)]
        if not files:
            return pd.DataFrame()
        parts = []
        for f in files:
            x = pd.read_parquet(f)
            if "market" not in x.columns:
                x["market"] = f.parent.parent.name
            parts.append(x)
        df = pd.concat(parts, ignore_index=True)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df
