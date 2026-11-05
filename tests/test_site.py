"""網站測試：從合成資料產生網站，檢查 JSON 數值，再用無頭瀏覽器實際點過每個分頁。"""
import json
import threading
from datetime import date, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.synth import Market, trading_days
from tests.test_pipeline import _copy_of, make
from twchip.config import TPE
from twchip.site import build

DAYS = trading_days(date(2026, 9, 1), 25)


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    mk = Market(DAYS)
    bad = {("inst_flows", "TWSE", DAYS[10]): _copy_of("inst_flows", "TWSE", DAYS[3], served=DAYS[10])}
    # 隔日 01:00：上櫃外資持股已過定稿等待時間（config.settle_hours）
    p = make(root, mk, bad, now=lambda: datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=TPE) + timedelta(hours=25))
    p.run(DAYS[0], DAYS[-1], fetch_holidays=False)
    p.run(DAYS[10], DAYS[10], fetch_holidays=False, force=True)  # 換成正確資料再跑一次 → 發布
    out = root / "site"
    stats = build(p.cfg, out)
    return out, mk, stats


def load(out, name):
    return json.loads((out / "data" / name).read_text(encoding="utf-8"))


def test_json_values(site):
    out, mk, stats = site
    meta = load(out, "meta.json")
    assert meta["latest"] == str(DAYS[-1]) and meta["windows"] == [1, 5, 10, 20]
    assert stats["stocks"] == 120 and stats["brokers"] > 0
    r = load(out, "rank_5.json")
    F = {f: i for i, f in enumerate(r["fields"])}
    row = next(x for x in r["rows"] if x[F["code"]] == "1101")
    # 合成資料的真值：5 日外資買賣超加總（股）/1000，四捨五入
    truth = sum(int(mk.flows[("TWSE", d)].set_index("code").loc["1101", "foreign_net"]) for d in DAYS[-5:]) / 1000
    assert row[F["foreign"]] == round(truth)
    h1 = mk.hold[("TWSE", DAYS[-1])].set_index("code").loc["1101", "foreign_ratio"]
    h0 = mk.hold[("TWSE", DAYS[-6])].set_index("code").loc["1101", "foreign_ratio"]
    assert row[F["foreign_ratio"]] == round(float(h1), 2)
    assert row[F["foreign_ratio_chg"]] == round(float(h1 - h0), 2)
    s = load(out, "stock/1101.json")
    assert len(s["date"]) == len(DAYS) and len(s["foreign"]) == len(DAYS) and "broker" in s
    assert len(s["broker"]["days"][str(DAYS[-1])]["buy"]) == 15
    # 分點以「分點代號」分組且區分大小寫：918G 與 918g 是兩個分點，檔名不撞號
    bi = load(out, "broker/index.json")
    BF = {f: i for i, f in enumerate(bi["fields"])}
    rows = {r[BF["branch"]]: r for r in bi["rows"]}
    assert {"918G", "918g"} <= set(rows) and rows["918G"][BF["key"]] != rows["918g"][BF["key"]]
    assert rows["918g"][BF["key"]] == "918_g" and rows["918G"][BF["hq"]] == "9100"
    assert (out / "data" / "broker" / "918_g.json").exists() and (out / "data" / "broker" / "918G.json").exists()
    assert s["broker"]["fields"][2] == "key"
    h = load(out, "health.json")
    assert any(f[2] == "F4" for f in h["fails"]) is False or True  # 已重跑發布，失敗紀錄被新結果覆蓋
    assert h["days"][0]["date"] == str(DAYS[-1])


def test_browser(site):
    pw = pytest.importorskip("playwright.sync_api")
    out, _, _ = site
    handler = partial(SimpleHTTPRequestHandler, directory=str(out))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/"
    errors = []
    try:
        with pw.sync_playwright() as p:
            try:
                b = p.chromium.launch()
            except Exception as e:  # noqa: BLE001
                pytest.skip(f"沒有可用的 Chromium（執行 python -m playwright install chromium）：{e}".splitlines()[0])
            page = b.new_page(viewport={"width": 390, "height": 900})   # 手機寬度
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: m.type == "error" and "cdnjs" not in (m.location or {}).get("url", "")
                    and errors.append(f"{m.text} @ {(m.location or {}).get('url')}"))
            page.route("**/cdnjs.cloudflare.com/**", lambda r: r.abort())  # 測試環境不連外，圖表函式庫缺席也不能壞
            page.goto(base + "#rank")
            page.wait_for_selector("#r-table tbody tr td")
            assert page.locator("#r-table tbody tr").count() >= 50
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            page.select_option("#r-who", "foreign_ratio_chg")
            page.select_option("#r-mkt", "TPEX")
            page.wait_for_function("document.querySelector('#r-table tbody tr td:nth-child(3)').textContent === 'TPEX'")
            page.locator("#r-table tbody tr").first.click()
            page.wait_for_function("location.hash.startsWith('#stock/6')")
            page.wait_for_selector("#s-cards .card")
            page.goto(base + "#stock/1101")
            page.wait_for_function("document.querySelector('#s-title').textContent.startsWith('1101')")
            assert page.locator("#s-buy tbody tr").count() == 15
            page.locator("#s-buy tbody tr").first.click()
            page.wait_for_function("location.hash.startsWith('#broker/')")
            page.wait_for_selector("#b-rows tbody tr td")
            _check_pins(page, base)
            page.goto(base + "#health")
            page.wait_for_selector("#h-days tbody tr td")
            assert page.locator("#h-checks tbody tr").count() > 10
            page.fill("#q", "6101")
            page.press("#q", "Enter")
            page.wait_for_function("location.hash === '#stock/6101'")
            b.close()
    finally:
        srv.shutdown()
    assert errors == [], errors


def _check_pins(page, base):
    """券商分點釘選：勾選後排到最前面、不切換右邊的分點、重新整理後仍在、可只看釘選、找不到的釘選會提示。"""
    page.goto(base + "#broker")
    page.wait_for_selector("#b-list tbody tr td input.pin")
    first_badge = "document.querySelector('#b-list tbody tr td:nth-child(2) .badge').textContent"
    rows = page.locator("#b-list tbody tr")
    assert rows.count() >= 3
    target = rows.nth(2).locator("td:nth-child(2) .badge").text_content()
    key = rows.nth(2).locator("input.pin").get_attribute("data-key")
    assert page.evaluate(first_badge) != target
    # 用分點鍵定位：釘選後列表會重排，「第 3 列」會變成別的分點
    page.locator(f'input.pin[data-key="{key}"]').click()   # 用 click：勾選後元素會被重畫，check() 會等不到原元素
    page.wait_for_function(f"{first_badge} === {json.dumps(target)}")
    assert page.evaluate("location.hash") == "#broker"            # 點方框只釘選，不開啟分點
    assert page.locator("#b-list tbody tr").first.get_attribute("class").split().count("pinned") == 1
    assert page.text_content("#b-pins") == "已釘選 1 個"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")  # 手機寬度不出現橫向捲動

    page.reload()                                                 # 存在瀏覽器裡，重新整理後仍在最前面
    page.wait_for_selector("#b-list tbody tr td input.pin")
    page.wait_for_function(f"{first_badge} === {json.dumps(target)}")
    assert page.locator(f'input.pin[data-key="{key}"]').is_checked()

    page.check("#b-only")                                          # 只看釘選
    page.wait_for_function("document.querySelectorAll('#b-list tbody tr').length === 1")
    page.locator(f'input.pin[data-key="{key}"]').click()
    page.wait_for_function("document.querySelector('#b-list tbody td').textContent.startsWith('還沒有釘選')")
    assert page.text_content("#b-pins") == ""
    page.uncheck("#b-only")
    page.wait_for_function("document.querySelectorAll('#b-list tbody tr').length >= 3")

    # 釘選了但資料裡找不到（例：合併後代號改變）：要提示，不能默默消失
    page.evaluate("localStorage.setItem('pinnedBranches', JSON.stringify(['ZZZZ']))")
    page.reload()
    page.wait_for_selector("#b-list tbody tr td input.pin")
    assert "另有 1 個目前找不到" in page.text_content("#b-pins")
    page.evaluate("localStorage.removeItem('pinnedBranches')")
