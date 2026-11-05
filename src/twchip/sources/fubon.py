"""富邦 e-Broker（MoneyDJ 系統）主力進出：每檔股票單一交易日的買超、賣超前 15 名分點。

使用 `zco.djhtm?a=代號&e=日期&f=日期` 查詢單一歷史日。已實測：頁面「最後更新日」會等於請求日期，
2026-10-21 的 2330 重抓結果與舊專案存檔 30 列完全一致。
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import parse_qs, urlparse

import pandas as pd
from bs4 import BeautifulSoup

from ..http import Fetcher
from ..util import parse_any_date, to_int, to_number
from . import Parsed

URL = "https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco.djhtm"
_FOOTER = {
    "total_buy_net_lots": r"合計買超張數\s*[:：]?\s*([\d,]+)",
    "total_sell_net_lots": r"合計賣超張數\s*[:：]?\s*([\d,]+)",
    "avg_buy_cost": r"平均買超成本\s*[:：]?\s*([\d,.]+)",
    "avg_sell_cost": r"平均賣超成本\s*[:：]?\s*([\d,.]+)",
}


def decode_b(b: str) -> str:
    """MoneyDJ 把含英文字母的分點代號編成 UTF-16 十六進位，例如 0039004100300030 → 9A00。
    分點代號區分大小寫：918G（群益金鼎-新莊）與 918g（群益金鼎-大甲）是不同分點，不可轉大寫。
    （2026-11-05 實測：轉大寫會讓 820 個分點撞成 756 個。）"""
    if re.fullmatch(r"(?:[0-9a-fA-F]{4}){2,}", b or "") and len(b) % 4 == 0 and len(b) >= 8:
        try:
            s = bytes.fromhex(b).decode("utf-16-be")
            if re.fullmatch(r"[0-9A-Za-z]{2,6}", s):
                return s
        except (ValueError, UnicodeDecodeError):
            pass
    return b or ""


def _broker(cell) -> tuple[str, str, str]:
    a = cell.find("a")
    name = (a or cell).get_text(strip=True)
    if a is None:
        return name, "", ""
    href = a.get("href", "")
    q = parse_qs(urlparse(href).query)
    b = (q.get("b") or [""])[0]
    bhid = (q.get("BHID") or [""])[0] or decode_b(b)
    if not bhid:
        m = re.search(r"GenLink2stk\('[^']*','([^']+)'", href)  # 少數頁面用 JS 連結
        bhid = decode_b(m.group(1)) if m else ""
    return name, bhid, b


def parse_page(html: bytes, code: str, d: date) -> tuple[pd.DataFrame, dict, date | None]:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    m = re.search(r"最後更新日\s*[:：]\s*(\d{4}/\d{1,2}/\d{1,2})", text)
    served = parse_any_date(m.group(1)) if m else None

    rows, started = [], False
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 10:  # 有些版型把 td 包在巢狀元素裡
            tds = [td for td in tr.find_all("td") if td.find("td") is None]
        texts = [td.get_text(strip=True) for td in tds]
        if len(tds) >= 10 and texts[0].startswith("買超券商") and texts[5].startswith("賣超券商"):
            started = True
            continue
        if not started or len(tds) < 10:
            continue
        rank = len(rows) + 1
        for side, off in (("buy", 0), ("sell", 5)):
            name, bhid, b = _broker(tds[off])
            if not name:
                continue
            net = to_int(texts[off + 3])
            rows.append({
                "date": d, "code": code, "side": side, "rank": rank,
                "broker_name": name, "bhid": bhid, "broker_b": b,
                "buy_lots": to_int(texts[off + 1]), "sell_lots": to_int(texts[off + 2]),
                # 頁面上賣超欄顯示正數；統一存成帶號的買賣超。
                "net_lots": None if net is None else (abs(net) if side == "buy" else -abs(net)),
                "pct": to_number(texts[off + 4]),
            })
    df = pd.DataFrame(rows)
    if not df.empty:  # 名次以「各方向」各自計數
        df["rank"] = df.groupby("side").cumcount() + 1
    summary = {"date": d, "code": code}
    for k, pat in _FOOTER.items():
        mm = re.search(pat, text)
        summary[k] = (to_int(mm.group(1)) if "lots" in k else to_number(mm.group(1))) if mm else None
    summary["n_buy"] = int((df["side"] == "buy").sum()) if not df.empty else 0
    summary["n_sell"] = int((df["side"] == "sell").sum()) if not df.empty else 0
    return df, summary, served


def fetch_broker_top(fx: Fetcher, code: str, d: date) -> Parsed:
    iso = d.isoformat()
    resp = fx.get(URL, {"a": code, "e": iso, "f": iso}, source="fubon_zco", d=d, key=code, ext="html")
    df, summary, served = parse_page(resp.content, code, d)
    return Parsed("broker_top", None, df, served, no_data=df.empty,
                  extras={"summary": summary}, raw=[resp.raw])


def branch_id(broker_b: str, bhid: str = "") -> str:
    """分點代號（區分大小寫）。富邦連結的 b 參數是分點；BHID 參數是券商總公司，兩者不同。"""
    return decode_b(broker_b or "") or (bhid or "")


def branch_key(branch: str) -> str:
    """可安全當檔名與網址的分點鍵：小寫字母前加底線（918g → 918_g），在不分大小寫的檔案系統也不會撞名。"""
    return "".join("_" + c if c.islower() else c for c in branch)
