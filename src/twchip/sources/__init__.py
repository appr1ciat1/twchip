"""資料源。每個 fetch_* 只做三件事：抓（並存原始回應）、依欄位名解析、回報官方自稱的日期。

不在這裡做任何「修正」。檢查與判斷全部在 checks/ 完成。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

from ..rawstore import RawRef
from ..util import norm_field

CODE_RE = r"^\d{4,6}[A-Z]{0,2}$"


@dataclass
class Parsed:
    dataset: str
    market: Optional[str]
    df: pd.DataFrame
    served_date: Optional[date]        # 官方回應自稱的資料日期（G1 用）
    no_data: bool = False              # 官方明確表示當日無資料（休市或尚未公布）
    fields: list[str] = field(default_factory=list)
    field_issues: list[str] = field(default_factory=list)  # G2：缺少的必要欄位
    extras: dict = field(default_factory=dict)
    raw: list[RawRef] = field(default_factory=list)


class FieldMap:
    """依「正規化後的欄位名」對應到標準欄位。找不到必要欄位時記錄，不猜。"""

    def __init__(self, fields: list[str], spec: dict[str, list[str]], optional: set[str] = frozenset()):
        self.fields = [norm_field(f) for f in fields]
        self.idx: dict[str, int] = {}
        self.missing: list[str] = []
        for canon, cands in spec.items():
            found = None
            for c in cands:
                c = norm_field(c)
                if c in self.fields:
                    found = self.fields.index(c)
                    break
            if found is None:
                if canon not in optional:
                    self.missing.append(f"{canon} ← {cands}")
            else:
                self.idx[canon] = found

    def col(self, rows: list[list], canon: str) -> list:
        i = self.idx.get(canon)
        return [r[i] if i is not None and i < len(r) else None for r in rows]
