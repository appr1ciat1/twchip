"""驗證閘門。每個檢查回傳 CheckResult；level=B 且 status=fail 的資料不得發布。

檢查 ID 與 docs/DESIGN.md §4 一一對應，改動時兩邊一起改。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CheckResult:
    id: str
    name: str
    level: str          # "B" 阻擋 | "W" 警告
    status: str         # "pass" | "fail" | "skip"
    detail: str = ""
    n_bad: int = 0
    examples: list[Any] = field(default_factory=list)

    @property
    def blocking_fail(self) -> bool:
        return self.level == "B" and self.status == "fail"

    def to_dict(self) -> dict:
        return asdict(self)


def ok(id_, name, level, detail="", n_bad=0, examples=None) -> CheckResult:
    """與 bad() 同簽名，方便寫成 (ok if 條件 else bad)(...)；通過時忽略違反筆數與範例。"""
    return CheckResult(id_, name, level, "pass", detail)


def bad(id_, name, level, detail, n_bad=0, examples=None) -> CheckResult:
    return CheckResult(id_, name, level, "fail", detail, n_bad, list(examples or [])[:5])


def skip(id_, name, level, detail) -> CheckResult:
    return CheckResult(id_, name, level, "skip", detail)


def any_blocking(results: list[CheckResult]) -> bool:
    return any(r.blocking_fail for r in results)
