"""證交所「買賣日報表查詢系統」：每分點、每價位的真實買賣股數（券商分點的官方真值）。

這個系統要求圖形驗證碼，目的就是阻擋自動化。本模組**不會**自動辨識驗證碼：
它下載驗證碼圖片，由人輸入，再送出查詢並保存原始回應。用途是每天抽 2~3 檔做稽核（檢查 T9）。

狀態：實驗性。表單欄位名稱依公開資料推定，尚未對實站驗證，第一次請在你的電腦上手動跑通。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Callable

import pandas as pd
from bs4 import BeautifulSoup

from ..http import Fetcher
from ..util import norm_code, parse_any_date, to_int, to_number
from . import Parsed

BASE = "https://bsr.twse.com.tw/bshtm/"


def _hidden_fields(soup: BeautifulSoup) -> dict:
    return {i["name"]: i.get("value", "") for i in soup.select("input[type=hidden]") if i.get("name")}


def parse_content(html: bytes, code: str, d: date) -> tuple[pd.DataFrame, date | None]:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    served = None
    m = re.search(r"(\d{2,4}/\d{1,2}/\d{1,2})", text)
    if m:
        served = parse_any_date(m.group(1))
    rows = []
    for tr in soup.find_all("tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        # 每列左右兩組：序、證券商、成交單價、買進股數、賣出股數
        for off in (0, 5):
            grp = tds[off:off + 5]
            if len(grp) == 5 and grp[0].isdigit():
                broker = grp[1]
                bid = broker[:4] if re.match(r"^[0-9A-Z]{4}", broker) else ""
                rows.append({"date": d, "code": norm_code(code), "seq": int(grp[0]), "broker_id": bid,
                             "broker_name": broker[len(bid):].strip(), "price": to_number(grp[2]),
                             "buy_shares": to_int(grp[3]), "sell_shares": to_int(grp[4])})
    return pd.DataFrame(rows).drop_duplicates("seq") if rows else pd.DataFrame(), served


def fetch_interactive(fx: Fetcher, code: str, d: date, ask: Callable[[Path], str], tmp_dir: Path) -> Parsed:
    """d 必須是「今天（最近一個交易日）」：系統只提供當日資料。ask(圖片路徑) 回傳人輸入的驗證碼。"""
    s = fx.session
    menu = s.get(BASE + "bsMenu.aspx", timeout=30)
    soup = BeautifulSoup(menu.content, "lxml")
    img = soup.find("img", src=re.compile("CaptchaImage", re.I))
    if img is None:
        raise RuntimeError("找不到驗證碼圖片；頁面結構可能已變更")
    cap = s.get(BASE + img["src"].lstrip("./"), timeout=30)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    img_path = tmp_dir / f"captcha_{code}.png"
    img_path.write_bytes(cap.content)
    answer = ask(img_path).strip()
    form = {**_hidden_fields(soup), "RadioButton_Normal": "RadioButton_Normal",
            "TextBox_Stkno": code, "CaptchaControl1": answer, "btnOK": "查詢"}
    s.post(BASE + "bsMenu.aspx", data=form, timeout=30)
    resp = fx.get(BASE + "bsContent.aspx", None, source="twse_bsr", d=d, key=code, ext="html")
    df, served = parse_content(resp.content, code, d)
    return Parsed("broker_bsr", "TWSE", df, served, no_data=df.empty, raw=[resp.raw])
