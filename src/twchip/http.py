"""HTTP 層：每個主機獨立限速、有限重試、每個回應都先存進 RawStore 再交給解析器。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from typing import Optional
from urllib.parse import urlparse

import requests

from .rawstore import RawRef, RawStore

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


class FetchError(RuntimeError):
    """網路或 HTTP 層的錯誤（不是「官方尚未公布」）。"""


@dataclass
class Response:
    content: bytes
    status: int
    url: str
    raw: RawRef


class Fetcher:
    def __init__(self, raw: RawStore, min_interval: Optional[dict[str, float]] = None,
                 retries: int = 3, timeout: float = 30.0, session: Optional[requests.Session] = None):
        self.raw = raw
        self.min_interval = min_interval or {}
        self.retries = retries
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9"})
        self._last: dict[str, float] = {}

    def _throttle(self, host: str) -> None:
        gap = self.min_interval.get(host, 1.0)
        wait = self._last.get(host, 0.0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def get(self, url: str, params: Optional[dict], *, source: str, d: date, key: str, ext: str) -> Response:
        host = urlparse(url).netloc
        last_err: Optional[Exception] = None
        for attempt in range(self.retries):
            self._throttle(host)
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = e
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = FetchError(f"HTTP {r.status_code} {r.url}")
                time.sleep(5 * (attempt + 1))
                continue
            ref = self.raw.put(source, d, key, r.content, ext,
                               meta={"url": r.url, "status": r.status_code})
            if r.status_code != 200:
                raise FetchError(f"HTTP {r.status_code} {r.url}")
            return Response(r.content, r.status_code, r.url, ref)
        raise FetchError(f"{url} 重試 {self.retries} 次仍失敗：{last_err}")
