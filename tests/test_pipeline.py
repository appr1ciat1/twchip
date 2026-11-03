"""管線整合測試：乾淨資料全數發布；舊專案發生過的每一種錯誤都被擋下；重跑冪等；待辦與告警邏輯正確。"""
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from twchip.config import TPE, Config
from twchip.pipeline import Pipeline
from twchip.sources import Parsed
from tests.synth import Market, trading_days

DAYS = trading_days(date(2026, 3, 2), 12)
CODES = ["1101", "1102", "6101"]


def make(tmp_path, market: Market, faults=None, now=None, broker_faults=None):
    cfg = Config(data_dir=tmp_path, broker_codes=CODES)
    faults, broker_faults = faults or {}, broker_faults or {}
    units = {}
    for ds in ("quotes", "inst_flows", "foreign_holdings"):
        for m in ("TWSE", "TPEX"):
            def fn(d, ds=ds, m=m):
                f = faults.get((ds, m, d))
                return f(market) if f else market.parsed(ds, m, d)
            units[(ds, m)] = fn

    def bfn(code, d):
        f = broker_faults.get((code, d))
        return f(market) if f else market.broker(code, d)
    now = now or (lambda: datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=TPE) + timedelta(hours=23))
    p = Pipeline(cfg, units, bfn, now=now, log=lambda s: None)
    p.cal.holidays.clear()  # 合成日期不受真實休市表影響
    return p


def status(p, d):
    return {k: v.get("status") for k, v in p.store.read_quality(d)["units"].items()}


def test_clean_run_publishes_everything_and_is_idempotent(tmp_path):
    mk = Market(DAYS)
    p = make(tmp_path, mk)
    p.run(DAYS[0], DAYS[-1], fetch_holidays=False)
    for d in DAYS:
        st = status(p, d)
        assert set(st.values()) == {"published"}, (d, st)
    assert p.overdue() == []
    files = sorted((tmp_path / "curated").rglob("*.parquet"))
    mtimes = [f.stat().st_mtime for f in files]
    p2 = make(tmp_path, mk)
    p2.run(DAYS[0], DAYS[-1], fetch_holidays=False)
    assert [f.stat().st_mtime for f in files] == mtimes   # 第二次全部走快取，不重寫
    assert {s for d in DAYS for s in status(p2, d).values()} <= {"cached", "published"}


def _copy_of(ds, m, other, served=None):
    """他日資料（舊專案 12/26 = 12/18 的情形）。served 為官方回應自稱的日期。"""
    def f(mk):
        src = mk.parsed(ds, m, other)
        df = src.df.copy()
        return Parsed(ds, m, df, served, extras=src.extras)
    return f


def test_faults_are_quarantined(tmp_path):
    mk = Market(DAYS)
    d5, d6, d7, d8, d9, d10 = DAYS[5:11]

    def dealer_zero(mk):  # 6/15 前的欄位錯抓：自營商全 0，但官方合計仍是真值
        p = mk.parsed("inst_flows", "TWSE", d6)
        for c in ("ds_buy", "ds_sell", "ds_net", "dh_buy", "dh_sell", "dh_net", "dealer_buy", "dealer_sell", "dealer_net"):
            p.df[c] = 0
        return p

    def ratio_zero(mk):   # TPEX 外資比率「%」解析失敗 → 0
        p = mk.parsed("foreign_holdings", "TPEX", d7)
        p.df["foreign_ratio"] = 0.0
        return p

    def paging(mk):       # 只拿到第一頁
        p = mk.parsed("inst_flows", "TPEX", d9)
        p.df = p.df.head(20)
        p.extras["reported_total"] = 60
        p.extras["raw_rows"] = 20
        return p

    faults = {
        ("inst_flows", "TWSE", d5): _copy_of("inst_flows", "TWSE", DAYS[1], served=d5),   # 標籤正確但內容是他日
        ("inst_flows", "TWSE", d6): dealer_zero,
        ("foreign_holdings", "TPEX", d7): ratio_zero,
        ("foreign_holdings", "TWSE", d8): _copy_of("foreign_holdings", "TWSE", DAYS[7], served=d8),  # 前日快照
        ("inst_flows", "TPEX", d9): paging,
        ("quotes", "TPEX", d10): _copy_of("quotes", "TPEX", d10, served=DAYS[9]),          # 官方自稱他日
    }

    def broker_wrong_date(mk):
        p = mk.broker("1101", d6)
        return Parsed("broker_top", None, p.df, DAYS[2], extras=p.extras)

    def broker_bad_footer(mk):
        p = mk.broker("1102", d6)
        p.extras["summary"]["total_buy_net_lots"] += 50
        return p

    def broker_other_volume(mk):  # 佔比對應的是另一天的成交量 → 隱含成交量對不上
        p = mk.broker("6101", d7)
        p.df["pct"] = (p.df["pct"] * 1.6).round(2)
        return p

    bf = {("1101", d6): broker_wrong_date, ("1102", d6): broker_bad_footer, ("6101", d7): broker_other_volume}
    p = make(tmp_path, mk, faults, broker_faults=bf)
    p.run(DAYS[0], DAYS[-1], fetch_holidays=False)

    def failed(d, unit):
        u = p.store.read_quality(d)["units"][unit]
        return u["status"], {c["id"] for c in u.get("checks", []) if c["status"] == "fail" and c["level"] == "B"}

    st, ids = failed(d5, "inst_flows/TWSE"); assert st == "quarantined" and "F4" in ids, ids
    st, ids = failed(d6, "inst_flows/TWSE"); assert st == "quarantined" and "F3" in ids, ids
    st, ids = failed(d7, "foreign_holdings/TPEX"); assert st == "quarantined" and "H1" in ids, ids
    st, ids = failed(d8, "foreign_holdings/TWSE"); assert st == "quarantined" and "G5" in ids, ids
    st, ids = failed(d9, "inst_flows/TPEX"); assert st == "quarantined" and "G7" in ids, ids
    st, ids = failed(d10, "quotes/TPEX"); assert st == "quarantined" and "G1" in ids, ids
    # 隔離的單位不得出現在 curated；相依它的單位被擋住
    assert not p.store.has_curated("inst_flows", "TWSE", d5)
    assert status(p, d5)["foreign_holdings/TWSE"] == "blocked"
    assert status(p, d10)["inst_flows/TPEX"] == "blocked"
    # 券商分點：逐檔判定，壞的那檔隔離、其他照常發布
    stocks6 = p.store.read_quality(d6)["units"]["broker_top/ALL"]["stocks"]
    t1 = {c["id"] for c in stocks6["1101"]["checks"] if c["status"] == "fail"}
    t4 = {c["id"] for c in stocks6["1102"]["checks"] if c["status"] == "fail"}
    assert stocks6["1101"]["status"] == "quarantined" and "T1" in t1
    assert stocks6["1102"]["status"] == "quarantined" and "T4" in t4
    assert stocks6["6101"]["status"] == "published"
    stocks7 = p.store.read_quality(d7)["units"]["broker_top/ALL"]["stocks"]
    assert "T5" in {c["id"] for c in stocks7["6101"]["checks"] if c["status"] == "fail"}
    cur6 = p.store.read_curated("broker_top", "ALL", d6)
    assert set(cur6["code"]) == {"6101"}
    # 未完成的單位會被 SLA 告警列出
    issues = p.overdue(lookback_days=30)
    assert any(str(d5) in i and "inst_flows/TWSE" in i for i in issues)


def test_retry_after_fix_publishes(tmp_path):
    """官方修正後重跑：隔離的單位被重新抓取並發布，隔離檔移除。"""
    mk = Market(DAYS)
    d = DAYS[4]
    p = make(tmp_path, mk, {("inst_flows", "TWSE", d): _copy_of("inst_flows", "TWSE", DAYS[0], served=d)})
    p.run(DAYS[0], DAYS[-1], fetch_holidays=False)
    assert status(p, d)["inst_flows/TWSE"] == "quarantined"
    p2 = make(tmp_path, mk)
    p2.run(d, d, fetch_holidays=False)
    assert status(p2, d)["inst_flows/TWSE"] == "published"
    assert not list((tmp_path / "quarantine" / "inst_flows").rglob(f"{d}.parquet"))


def test_not_yet_published_is_pending_not_failure(tmp_path):
    """官方尚未公布：不報錯、不寫 curated；未超過 SLA 不告警，超過才告警。"""
    days = DAYS[:3]
    mk = Market(days[:2])  # 第三天還沒有資料
    last = days[2]
    early = lambda: datetime.combine(last, datetime.min.time(), tzinfo=TPE) + timedelta(hours=17)
    p = make(tmp_path, mk, now=early)
    p.run(days[0], last, fetch_holidays=False)
    assert p.store.read_quality(last)["calendar"]["status"] == "expected"
    assert not any(str(last) in i for i in p.overdue())
    late = lambda: datetime.combine(last, datetime.min.time(), tzinfo=TPE) + timedelta(hours=40)
    p2 = make(tmp_path, mk, now=late)
    p2.run(last, last, fetch_holidays=False)
    issues = p2.overdue()
    assert any(str(last) in i for i in issues), issues   # 隔日 16:00 後兩市場皆無 → 推定臨時休市，需人工確認


def test_holiday_with_data_is_not_ingested(tmp_path):
    """休市表上的日期，即使端點回傳他日資料也不寫入（舊專案 5/1 = 5/18 的情形）。"""
    mk = Market(DAYS)
    hol = DAYS[3]
    leak = {(ds, m, hol): _copy_of(ds, m, DAYS[2], served=DAYS[2])
            for ds in ("quotes", "inst_flows", "foreign_holdings") for m in ("TWSE", "TPEX")}
    p = make(tmp_path, mk, leak)
    p.cal.holidays[hol] = "test:假日"
    p.run(DAYS[0], DAYS[-1], fetch_holidays=False)
    for ds in ("quotes", "inst_flows", "foreign_holdings"):
        for m in ("TWSE", "TPEX"):
            assert not p.store.has_curated(ds, m, hol)


def test_t5_uses_each_stocks_own_history(tmp_path):
    """T5 與該股近 20 日比值中位數比較：固定的口徑偏差不報錯；某天突然偏離才擋（等級設為 B 時）。"""
    mk = Market(DAYS)
    last = DAYS[-1]

    def biased(code, d, factor):
        p = mk.broker(code, d)
        p.df["pct"] = (p.df["pct"] * factor).round(2)
        return p

    # 6101 每天的佔比都固定偏 +8%（像 6488 那樣的口徑差異）；最後一天 1101 突然偏 +7%
    bf = {("6101", d): (lambda m, d=d: biased("6101", d, 1 / 1.08)) for d in DAYS}
    bf[("1101", last)] = lambda m: biased("1101", last, 1 / 1.07)
    p = make(tmp_path, mk, broker_faults=bf)
    p.cfg.t5_level, p.cfg.t5_rel_tol, p.cfg.t5_min_history = "B", 0.03, 10
    p.run(DAYS[0], DAYS[-1], fetch_holidays=False)

    def t5(d, code):
        st = p.store.read_quality(d)["units"]["broker_top/ALL"]["stocks"][code]
        return st["status"], next(c for c in st["checks"] if c["id"] == "T5")

    # 前 10 天歷史不足：直接比較、只警告；6101 因固定偏差而「警告未通過」但仍發布
    st, c = t5(DAYS[2], "6101")
    assert st == "published" and c["level"] == "W" and c["status"] == "fail"
    # 有歷史之後：6101 的固定偏差被抵消 → 通過
    st, c = t5(last, "6101")
    assert st == "published" and c["status"] == "pass" and "近 20 日" in c["detail"], c
    # 1101 最後一天突然偏離 7% → T5（B）未通過 → 隔離
    st, c = t5(last, "1101")
    assert st == "quarantined" and c["level"] == "B" and c["status"] == "fail", c
    assert "1101" not in set(p.store.read_curated("broker_top", "ALL", last)["code"])


def test_f4_tolerance_can_differ_by_market(monkeypatch):
    """F4 門檻可依市場設定；未設定（或 GitHub 傳入空字串）時退回 TWCHIP_F4_TOL。"""
    monkeypatch.setenv("TWCHIP_F4_TOL", "0.03")
    monkeypatch.setenv("TWCHIP_F4_TOL_TWSE", "0.005")
    monkeypatch.setenv("TWCHIP_F4_TOL_TPEX", "")
    cfg = Config()
    assert cfg.f4_tol_for("TWSE") == 0.005
    assert cfg.f4_tol_for("TPEX") == 0.03


def test_waiver_turns_one_blocking_check_into_warning(tmp_path):
    """豁免只作用在指定的 (日期, 單位, 檢查)，且違反筆數不得超過記錄值；超過就照樣隔離。"""
    import json
    mk = Market(DAYS)
    d = DAYS[5]
    fault = {("inst_flows", "TWSE", d): _copy_of("inst_flows", "TWSE", DAYS[1], served=d)}

    def run(max_n_bad, sub):
        wp = tmp_path / f"w{sub}.json"
        wp.write_text(json.dumps([{"date": d.isoformat(), "unit": "inst_flows/TWSE", "check": "F4",
                                   "max_n_bad": max_n_bad, "reason": "測試"}]), encoding="utf-8")
        p = make(tmp_path / sub, mk, fault)
        p.cfg.waivers_path = wp
        p.run(DAYS[0], DAYS[-1], fetch_holidays=False)
        u = p.store.read_quality(d)["units"]["inst_flows/TWSE"]
        return u["status"], next(c for c in u["checks"] if c["id"] == "F4")

    st, c = run(10_000, "a")
    assert st == "published" and c["level"] == "W" and "人工豁免" in c["detail"], c
    st, c = run(0, "b")           # 實際違反筆數 > 記錄值 → 豁免失效
    assert st == "quarantined" and c["level"] == "B", c


def test_block_trades_are_subtracted_for_t5(tmp_path):
    """官方成交股數含鉅額交易、富邦分母不含：扣除鉅額交易後 T5 通過；不扣就失敗。
    同時測：沒有鉅額交易的日子發布空檔；鉅額股數超過成交股數（K3）會被隔離。"""
    mk = Market(DAYS)
    last, k3_day = DAYS[-1], DAYS[-3]
    X = int(mk.quotes[("TWSE", last)].set_index("code").loc["1101", "volume_shares"] * 0.4)

    def quotes_with_block(m):   # 官方行情：成交股數多了一筆 X 股的鉅額交易
        p = m.parsed("quotes", "TWSE", last)
        i = p.df.index[p.df["code"] == "1101"]
        p.df.loc[i, "value_twd"] += (p.df.loc[i, "close"] * X).astype("int64")
        p.df.loc[i, "volume_shares"] += X
        return p

    def block_fn(d):
        rows = []
        if d == last:
            close = float(mk.quotes[("TWSE", last)].set_index("code").loc["1101", "close"])
            rows = [{"code": "1101", "name": "x", "trade_type": "配對交易", "price": close,
                     "volume_shares": X, "value_twd": int(round(close * X))}]
        if d == k3_day:
            v = int(mk.quotes[("TWSE", d)].set_index("code").loc["1102", "volume_shares"])
            rows = [{"code": "1102", "name": "y", "trade_type": "配對交易", "price": 10.0,
                     "volume_shares": v + 1000, "value_twd": (v + 1000) * 10}]
        if not rows:
            return Parsed("block_trades", "TWSE", pd.DataFrame(), None, no_data=True, extras={"stat": "很抱歉，沒有符合條件的資料!"})
        df = pd.DataFrame(rows)
        df.insert(0, "seq", range(1, len(df) + 1)); df.insert(0, "market", "TWSE"); df.insert(0, "date", d)
        return Parsed("block_trades", "TWSE", df, d)

    def run(with_block, sub):
        p = make(tmp_path / sub, mk, {("quotes", "TWSE", last): quotes_with_block})
        if with_block:
            p.units[("block_trades", "TWSE")] = block_fn
        p.cfg.t5_level, p.cfg.t5_rel_tol = "B", 0.03
        p.run(DAYS[0], DAYS[-1], fetch_holidays=False)
        return p

    p = run(True, "a")
    st = p.store.read_quality(last)["units"]["broker_top/ALL"]["stocks"]["1101"]
    assert st["status"] == "published", st
    assert p.store.read_quality(DAYS[1])["units"]["block_trades/TWSE"]["status"] == "published"   # 無鉅額 → 空檔
    u = p.store.read_quality(k3_day)["units"]["block_trades/TWSE"]
    assert u["status"] == "quarantined" and any(c["id"] == "K3" and c["status"] == "fail" for c in u["checks"])

    p = run(False, "b")
    st = p.store.read_quality(last)["units"]["broker_top/ALL"]["stocks"]["1101"]
    assert st["status"] == "quarantined", st
