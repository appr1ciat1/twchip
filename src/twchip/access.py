"""網站保護檢查：確認網址在未登入時會被 Cloudflare Access 擋下。

部署真正的資料之前一定先跑這個檢查。任何一個網址沒有被擋，就不部署資料（寧可擋錯，也不外洩）。

判斷規則（未帶任何登入憑證的請求，不跟隨轉址）：
- 轉址（3xx）到 *.cloudflareaccess.com：受保護。這是 Access 的登入頁。
- 401 或 403：受保護。
- 其他狀態（例如 200、404）、轉址到別處、連線失敗：視為「沒有確認受保護」。
"""
from __future__ import annotations

from typing import Callable, Optional
from urllib.parse import urlparse

import requests

ACCESS_SUFFIX = ".cloudflareaccess.com"


def judge(status: Optional[int], location: str = "") -> tuple[bool, str]:
    """依回應狀態與 Location 判斷是否受保護。status 為 None 代表連線失敗。"""
    if status is None:
        return False, "連線失敗，無法確認"
    if 300 <= status < 400:
        host = (urlparse(location).hostname or "").lower()
        if host.endswith(ACCESS_SUFFIX):
            return True, f"{status} → Access 登入頁（{host}）"
        return False, f"{status} 轉址到 {location or '（空白）'}，不是 Access 登入頁"
    if status in (401, 403):
        return True, f"{status} 拒絕存取"
    return False, f"{status}：未登入也拿得到內容"


def check(url: str, get: Optional[Callable] = None) -> tuple[bool, str]:
    get = get or (lambda u: requests.get(u, allow_redirects=False, timeout=20,
                                         headers={"User-Agent": "twchip-access-check"}))
    try:
        r = get(url)
    except Exception as e:  # noqa: BLE001
        return judge(None)[0], f"連線失敗（{type(e).__name__}），無法確認"
    return judge(r.status_code, r.headers.get("Location", ""))


def check_all(urls: list[str], get: Optional[Callable] = None) -> bool:
    """逐一檢查並印出結果。全部受保護才回傳 True。"""
    ok_all = True
    for u in urls:
        ok, why = check(u, get)
        print(f"{'受保護  ' if ok else '未受保護'} {u}：{why}")
        ok_all &= ok
    return ok_all
