"""走完整的真實抓取路徑：Fetcher（限速、重試）→ RawStore（gzip 存檔、日誌）→ 解析。

只把最底層的 HTTP 換成假的 session，其餘全部是正式程式碼。
probe 第一次在 GitHub 上執行時，就是這條路徑出錯（gzip.open 不接受 mtime），
而原本的測試都繞過了它。
"""
import gzip
import json
from datetime import date

import pytest

from tests.test_parsers import T86_FIELDS, fubon_html
from twchip.http import FetchError, Fetcher
from twchip.rawstore import RawStore
from twchip.sources import fubon, twse

D = date(2026, 10, 30)


class FakeHTTP:
    def __init__(self, payloads, statuses=None):
        self.payloads, self.statuses, self.calls = payloads, list(statuses or []), []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        status = self.statuses.pop(0) if self.statuses else 200
        body = self.payloads(url, params)

        class R:
            pass
        r = R()
        r.status_code, r.content = status, body
        r.url = url + "?" + "&".join(f"{k}={v}" for k, v in (params or {}).items())
        return r


def make_fetcher(tmp_path, http):
    return Fetcher(RawStore(tmp_path), {}, retries=3, session=http), RawStore(tmp_path)


def test_t86_end_to_end_writes_raw(tmp_path):
    row = ["2330", "台積電", "10,000", "4,000", "6,000", "0", "0", "0", "500", "800", "-300", "1,100",
           "1,000", "0", "1,000", "200", "100", "100", "6,800"]
    body = json.dumps({"stat": "OK", "date": "20261030", "fields": T86_FIELDS, "data": [row]},
                      ensure_ascii=False).encode()
    fx, raw = make_fetcher(tmp_path, FakeHTTP(lambda u, p: body))
    p = twse.fetch_t86(fx, D)
    assert p.served_date == D and len(p.df) == 1
    files = raw.list("twse_t86", D)
    assert len(files) == 1 and gzip.decompress(files[0].read_bytes()) == body
    logs = list((tmp_path / "raw" / "_log").glob("*.jsonl"))
    rec = json.loads(logs[0].read_text().splitlines()[-1])
    assert rec["source"] == "twse_t86" and rec["status"] == 200
    # 同內容再抓一次：不產生新檔，位元組完全相同（mtime=0）
    twse.fetch_t86(fx, D)
    assert raw.list("twse_t86", D) == files


def test_fubon_end_to_end(tmp_path):
    fx, raw = make_fetcher(tmp_path, FakeHTTP(lambda u, p: fubon_html()))
    p = fubon.fetch_broker_top(fx, "2330", date(2026, 10, 21))
    assert len(p.df) == 30 and p.extras["summary"]["total_buy_net_lots"] == 4786
    assert len(raw.list("fubon_zco", date(2026, 10, 21), "2330")) == 1


def test_retry_then_success_and_hard_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    ok = json.dumps({"stat": "OK", "date": "20261030", "fields": T86_FIELDS, "data": []}).encode()
    fx, _ = make_fetcher(tmp_path, FakeHTTP(lambda u, p: ok, statuses=[503, 200]))
    assert twse.fetch_t86(fx, D).no_data   # 第一次 503 → 重試 → 成功（當日無資料列）
    fx2, _ = make_fetcher(tmp_path, FakeHTTP(lambda u, p: b"x", statuses=[503, 503, 503]))
    with pytest.raises(FetchError):
        twse.fetch_t86(fx2, D)
