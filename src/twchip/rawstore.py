"""原始回應的不可變儲存。

每個回應以內容雜湊命名：同內容不重複存，不同內容永遠並存。
任何 curated 數字都能追溯到這裡的原始位元組，並用同一版解析器重現。
"""
from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class RawRef:
    path: Path          # 相對於 data_dir
    sha256: str
    bytes: int


class RawStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "raw"

    def put(self, source: str, d: date, key: str, content: bytes, ext: str,
            meta: Optional[dict] = None) -> RawRef:
        digest = hashlib.sha256(content).hexdigest()
        rel = Path("raw") / source / f"{d:%Y}" / f"{d:%Y%m%d}" / f"{key}__{digest[:12]}.{ext}.gz"
        p = self.data_dir / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_bytes(gzip.compress(content, mtime=0))  # mtime=0：同內容得到同位元組
            tmp.replace(p)
        # 每天一個日誌檔，避免單檔無限成長、每次同步都要重傳。
        log = self.root / "_log" / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(), "source": source, "date": d.isoformat(),
                "key": key, "sha256": digest, "bytes": len(content), "path": str(rel), **(meta or {}),
            }, ensure_ascii=False) + "\n")
        return RawRef(rel, digest, len(content))

    def read(self, ref_path: Path | str) -> bytes:
        with gzip.open(self.data_dir / ref_path, "rb") as f:
            return f.read()

    def list(self, source: str, d: date, key: Optional[str] = None) -> list[Path]:
        folder = self.root / source / f"{d:%Y}" / f"{d:%Y%m%d}"
        if not folder.exists():
            return []
        pat = f"{key}__*.gz" if key else "*.gz"
        return sorted(folder.glob(pat), key=lambda p: p.stat().st_mtime)
