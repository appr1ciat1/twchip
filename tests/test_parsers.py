"""解析器測試。TWSE/TPEX 的回應格式依公開文件與開源實作建構；富邦頁面使用 2026-10-21 台積電的真實數值。"""
import json
from datetime import date

from twchip.rawstore import RawRef
from twchip.sources import fubon, tpex, twse
from twchip.util import parse_any_date, to_number

D = date(2026, 10, 30)


class FakeResp:
    def __init__(self, content: bytes):
        self.content, self.status, self.url = content, 200, "x"
        self.raw = RawRef("raw/x", "0", len(content))


class FakeFetcher:
    def __init__(self, payloads):
        self.payloads = payloads  # list，依序回傳
        self.calls = []

    def get(self, url, params, **kw):
        self.calls.append((url, params))
        p = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        return FakeResp(p if isinstance(p, bytes) else json.dumps(p, ensure_ascii=False).encode())


def test_util_numbers_and_dates():
    assert to_number("1,234") == 1234 and to_number("(1,234)") == -1234
    assert to_number("12.34%") == 12.34 and to_number("--") is None and to_number("") is None
    assert to_number("<p style='color:red'>+</p>") is None
    for s in ("20261030", "2026/10/30", "115/10/30", "115年10月30日", "2026-10-30"):
        assert parse_any_date(s) == D, s


def test_decode_b():
    assert fubon.decode_b("0039004100300030") == "9A00"
    assert fubon.decode_b("1590") == "1590"


T86_FIELDS = ["證券代號", "證券名稱", "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)",
              "外陸資買賣超股數(不含外資自營商)", "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數",
              "投信買進股數", "投信賣出股數", "投信買賣超股數", "自營商買賣超股數", "自營商買進股數(自行買賣)",
              "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)", "自營商買進股數(避險)", "自營商賣出股數(避險)",
              "自營商買賣超股數(避險)", "三大法人買賣超股數"]


def test_t86():
    row = ["2330", "台積電", "10,000", "4,000", "6,000", "0", "0", "0", "500", "800", "-300", "1,100",
           "1,000", "0", "1,000", "200", "100", "100", "6,800"]
    fx = FakeFetcher([{"stat": "OK", "date": "20261030", "fields": T86_FIELDS, "data": [row, ["合計", "", *["0"] * 17]]}])
    p = twse.fetch_t86(fx, D)
    assert p.served_date == D and not p.field_issues and len(p.df) == 1
    r = p.df.iloc[0]
    assert r.fx_net == 6000 and r.dealer_net == 1100 and r.foreign_net == 6000 and r.total_net == 6800
    assert r.dealer_buy == 1200 and r.dealer_sell == 100


def test_t86_missing_field_is_reported():
    fx = FakeFetcher([{"stat": "OK", "date": "20261030", "fields": T86_FIELDS[:-1], "data": [["2330", "x"] + ["1"] * 16]}])
    p = twse.fetch_t86(fx, D)
    assert any("total_net" in s for s in p.field_issues)


def test_t86_holiday():
    p = twse.fetch_t86(FakeFetcher([{"stat": "很抱歉，沒有符合條件的資料!"}]), D)
    assert p.no_data


def test_mi_index_sign_and_ex_right():
    fields = ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價",
              "漲跌(+/-)", "漲跌價差", "本益比"]
    data = [["2330", "台積電", "30,000,000", "50,000", "72,000,000,000", "2,400.00", "2,420.00", "2,390.00",
             "2,410.00", "<p style= color:green>-</p>", "5.00", "20"],
            ["2317", "鴻海", "1,000", "10", "200,000", "200", "201", "199", "200", "X", "0.00", ""]]
    j = {"stat": "OK", "date": "20261030", "tables": [{"title": "指數"}, {"title": "115年10月30日 每日收盤行情(全部)", "fields": fields, "data": data}]}
    p = twse.fetch_mi_index(FakeFetcher([j]), D)
    r = p.df.set_index("code")
    assert r.loc["2330", "change"] == -5.0 and bool(r.loc["2317", "ex_right"]) and r.isna().loc["2317", "change"]


def test_tpex_qfii_percent_and_date_fallback():
    fields = ["排行", "代號", "名稱", "發行股數(A)", "僑外資及陸資尚可投資股數B=A*F-C", "僑外資及陸資持有股數(C)",
              "僑外資及陸資尚可投資比率(D=B/A)", "僑外資及陸資持股比率(E=C/A)", "法令投資上限比率(F)", "備註"]
    good = {"date": "20261030", "tables": [{"date": "115/10/30", "fields": fields,
            "data": [["1", "8069", "元太", "1,154,360,555", "679,227,674", "475,132,881", "58.84%", "41.16%", "100.00%", ""]]}]}
    stale = {"date": "20261029", "tables": [{"date": "115/10/29", "fields": fields, "data": good["tables"][0]["data"]}]}
    fx = FakeFetcher([stale, good])  # 第一次（西元格式）回傳他日 → 改用民國格式重試
    p = tpex.fetch_qfii(fx, D)
    assert len(fx.calls) == 2 and p.served_date == D
    assert p.df.iloc[0].foreign_ratio == 41.16   # 舊專案此欄全為 0 的 bug


def test_tpex_daily_trade_positional():
    fields = ["代號", "名稱"] + ["買進股數", "賣出股數", "買賣超股數"] * 7 + ["三大法人買賣超股數合計"]
    row = ["6488", "環球晶"] + ["100", "40", "60", "10", "0", "10", "110", "40", "70", "5", "5", "0",
                                 "3", "1", "2", "0", "0", "0", "3", "1", "2"] + ["72"]
    p = tpex.fetch_daily_trade(FakeFetcher([{"date": "20261030", "tables": [{"fields": fields, "data": [row]}]}]), D)
    r = p.df.iloc[0]
    assert r.foreign_net == 70 and r.trust_net == 0 and r.dealer_net == 2 and r.total_net == 72


def test_tpex_daily_trade_rejects_unexpected_layout():
    p = tpex.fetch_daily_trade(FakeFetcher([{"date": "20261030", "tables": [{"fields": ["代號", "名稱", "x"], "data": [["1", "2", "3"]]}]}]), D)
    assert p.field_issues and p.df.empty


# ---- 富邦：2026-10-21 台積電真實數值（與舊專案存檔 30 列一致）
BUY = [("美林", "1440", 1761, 1045, 716, 2.98), ("永豐金證券", "0039004100300030", 859, 229, 630, 2.62),
       ("大和國泰", "8890", 553, 50, 503, 2.09), ("花旗環球", "1590", 990, 512, 478, 1.99),
       ("富邦證券", "9600", 793, 342, 451, 1.88), ("兆豐證券", "7000", 436, 7, 429, 1.79),
       ("香港上海匯豐", "8960", 535, 154, 381, 1.59), ("國泰-敦南", "8888", 333, 71, 262, 1.09),
       ("中國信託", "9A9C", 304, 100, 204, 0.85), ("群益金鼎", "9100", 224, 24, 200, 0.83),
       ("統一", "5850", 506, 330, 176, 0.73), ("台灣企銀", "6110", 102, 0, 102, 0.42),
       ("富邦-台南", "9661", 97, 4, 93, 0.39), ("台新", "8150", 163, 75, 88, 0.37), ("第一金-高雄", "5385", 77, 1, 76, 0.32)]
SELL = [("美商高盛", "1480", 1196, 3304, 2108, 8.77), ("摩根大通", "8440", 1169, 2730, 1561, 6.5),
        ("台灣摩根士丹利", "1470", 1639, 2815, 1176, 4.89), ("凱基-台北", "9268", 311, 1460, 1149, 4.78),
        ("元大證券", "9800", 981, 1897, 916, 3.81), ("凱基", "9200", 141, 1002, 861, 3.58),
        ("港商野村", "1560", 185, 810, 625, 2.6), ("新加坡商瑞銀", "1650", 1476, 2012, 536, 2.23),
        ("聯邦-大業", "7008", 7, 203, 196, 0.82), ("華南永昌", "9300", 78, 137, 59, 0.25),
        ("元大-台北", "9851", 14, 50, 36, 0.15), ("中國信託-松江", "9A92", 6, 28, 22, 0.09),
        ("群益金鼎-大甲", "9147", 2, 18, 16, 0.07), ("台中銀-豐原", "6464", 6, 21, 15, 0.06),
        ("元大-松江", "9883", 5, 16, 11, 0.05)]


def fubon_html(served="2026/10/21", footer=(4786, 9286, 2417.07, 2416.32)) -> bytes:
    def cell(n, b):
        return f'<td class="t4t1"><a href="/z/zc/zco/zco0/zco0.djhtm?a=2330&b={b}&BHID={fubon.decode_b(b)}">{n}</a></td>'
    rows = "".join(
        "<tr>" + cell(bn, bb) + "".join(f'<td class="t3n1">{x:,}</td>' for x in (bbuy, bsell, bnet)) + f"<td>{bp}%</td>"
        + cell(sn, sb) + "".join(f'<td class="t3n1">{x:,}</td>' for x in (sbuy, ssell, snet)) + f"<td>{sp}%</td></tr>"
        for (bn, bb, bbuy, bsell, bnet, bp), (sn, sb, sbuy, ssell, snet, sp) in zip(BUY, SELL))
    return f"""<html><head><meta charset="big5"></head><body>
    <div>單位：張　最後更新日：{served}</div>
    <table class="t01"><tr><td>買超券商</td><td>買進</td><td>賣出</td><td>買超</td><td>佔成交比重</td>
    <td>賣超券商</td><td>買進</td><td>賣出</td><td>賣超</td><td>佔成交比重</td></tr>{rows}
    <tr><td colspan="5">合計買超張數：{footer[0]:,}</td><td colspan="5">合計賣超張數：{footer[1]:,}</td></tr>
    <tr><td colspan="5">平均買超成本：{footer[2]}</td><td colspan="5">平均賣超成本：{footer[3]}</td></tr>
    </table></body></html>""".encode("cp950")


def test_fubon_real_page_values():
    df, summary, served = fubon.parse_page(fubon_html(), "2330", date(2026, 10, 21))
    assert served == date(2026, 10, 21) and len(df) == 30
    s = df.set_index(["side", "rank"])
    assert s.loc[("buy", 2), "bhid"] == "9A00" and s.loc[("sell", 1), "net_lots"] == -2108
    # 真實頁面：頁尾是以「股」加總後才四捨五入，與逐列四捨五入的加總有小差距（4789 vs 4786，9287 vs 9286）
    assert summary["total_buy_net_lots"] == 4786 and int(df[df.side == "buy"].net_lots.sum()) == 4789
    assert summary["total_sell_net_lots"] == 9286 and int(-df[df.side == "sell"].net_lots.sum()) == 9287
    assert summary["avg_buy_cost"] == 2417.07
    from twchip.checks.rules import broker_stock
    import pandas as pd
    quote = pd.Series({"volume_shares": 24_030_000, "low": 2400.0, "high": 2430.0})  # 716/2.98% ≈ 24,027 張
    res = {r.id: r for r in broker_stock(df, summary, served, date(2026, 10, 21), quote, 0.03)}
    assert res["T4"].status == "pass" and res["T3"].status == "pass" and res["T5"].status == "pass"
    assert res["T7"].status == "pass"
    # T5 是警告：口徑不同時不擋發布，但仍記錄差距
    from twchip.checks.rules import implied_volume_lots
    assert res["T5"].level == "W"
    assert abs(implied_volume_lots(df) - 24_030) < 150


def test_block_trades_real_rows_and_title_date():
    """欄位與數值取自 2026-11-03 實測回應（2379、2412 於 114/09/30、114/12/29）。"""
    fields = ["證券代號", "證券名稱", "交易別", "成交價", "成交股數", "成交金額"]
    j = {"stat": "OK", "tables": [{"title": "114年12月29日 鉅額交易日成交資訊-單一證券", "fields": fields,
                                   "data": [["2379", "瑞昱", "配對交易", "510.00", "26,043,349", "13,282,107,990"],
                                            ["2412", "中華電", "配對交易", "134.16", "710,000", "95,253,600"],
                                            ["總計", "", "", "", "26,753,349", "13,377,361,590"]]}]}
    d = date(2025, 12, 29)
    p = twse.fetch_block_trades(FakeFetcher([j]), d)
    assert p.served_date == d and not p.field_issues
    assert list(p.df["seq"]) == [1, 2] and int(p.df["volume_shares"].iloc[0]) == 26_043_349
    assert int(p.df["value_twd"].iloc[0]) == 13_282_107_990
    assert len(p.df) == 2 and "總計" not in set(p.df["code"])          # 總計列不當成交易
    assert p.extras["reported_total_shares"] == 26_753_349
    from twchip.checks import rules
    res = {r.id: r for r in rules.block_trades(p.df, None, p.extras)}
    assert res["K4"].status == "pass", res["K4"]
    p.extras["reported_total_value"] += 1                              # 金額差 1 元（2 列，容許 ±1.5）→ 通過
    assert {r.id: r for r in rules.block_trades(p.df, None, p.extras)}["K4"].status == "pass"
    p.extras["reported_total_value"] += 1                              # 差 2 元 > 容許 → 擋下
    assert {r.id: r for r in rules.block_trades(p.df, None, p.extras)}["K4"].status == "fail"
    p.extras["reported_total_value"] -= 2
    p.extras["reported_total_shares"] += 1000                          # 漏抓一筆 → K4 擋下
    assert {r.id: r for r in rules.block_trades(p.df, None, p.extras)}["K4"].status == "fail"


def test_block_trades_no_data():
    p = twse.fetch_block_trades(FakeFetcher([{"stat": "很抱歉，沒有符合條件的資料!"}]), D)
    assert p.no_data and p.df.empty


def test_block_k2_ignores_codes_outside_project_scope():
    """範圍外的代號（2887Z1）不要求有行情：K2 通過、K2b 警告；範圍內缺行情仍然擋下。"""
    import pandas as pd
    from twchip.checks import rules
    df = pd.DataFrame({"code": ["2330", "2887Z1"], "price": [10.0, 20.0],
                       "volume_shares": [1000, 2000], "value_twd": [10000, 40000]})
    ex = {"reported_total_shares": 3000, "reported_total_value": 50000}
    q = pd.DataFrame({"code": ["2330"], "volume_shares": [5000]})
    r = {x.id: x for x in rules.block_trades(df, q, ex)}
    assert r["K2"].status == "pass" and r["K2b"].status == "fail" and r["K2b"].level == "W", r
    assert "2887Z1" in r["K2b"].detail
    r = {x.id: x for x in rules.block_trades(df, pd.DataFrame({"code": ["1101"], "volume_shares": [1]}), ex)}
    assert r["K2"].status == "fail" and r["K2"].level == "B"
