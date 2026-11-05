"""舊資料匯入與比對：用真實的 tw-inst-archive。比對邏輯必須能抓出逐列差異。"""
import os
from pathlib import Path

import pandas as pd
import pytest

from twchip.config import Config
from twchip.legacy import compare, import_archive
from twchip.store import Store

ARCHIVE = Path(os.environ.get("TWCHIP_TEST_ARCHIVE", "/home/claude/tw-inst-archive"))
pytestmark = pytest.mark.skipif(not (ARCHIVE / "manifest.json").exists(), reason="沒有舊封存庫")


def test_import_and_compare_detects_differences(tmp_path):
    cfg = Config(data_dir=tmp_path)
    counts = import_archive(cfg, ARCHIVE)
    assert counts["flows"] > 500_000 and counts["broker_trades"] > 100_000
    leg = pd.read_parquet(cfg.legacy_dir / "flows.parquet")
    assert {"legacy_source", "quality_action"} <= set(leg.columns)
    # 模擬「官方重抓」：取舊資料兩天的可用列當官方值，再故意改一列
    store = Store(cfg)
    use = leg[leg["quality_action"].isin(["keep", "recompute"])]
    for d in (pd.Timestamp("2026-10-29").date(), pd.Timestamp("2026-10-30").date()):
        for m in ("TWSE", "TPEX"):
            x = use[(pd.to_datetime(use["date"]).dt.date == d) & (use["market"] == m)].copy()
            x["date"] = d
            x = x[["date", "market", "code", "name", "foreign_net", "trust_net", "dealer_net"]]
            if d.day == 30 and m == "TWSE":
                x.loc[x.index[0], "trust_net"] += 1000
            if d.day == 29 and m == "TWSE":   # 上市官方沒有外資合計：foreign_net 為 NULL，由 fx + fd 推得
                x["fx_net"], x["fd_net"], x["foreign_net"] = x["foreign_net"] - 7, 7, None
            store.write_curated("inst_flows", m, d, x)
    res = compare(cfg)
    f = res[res["dataset"] == "inst_flows"]
    assert int(f["mismatched"].sum()) == 1
    bad = f[f["mismatched"] > 0].iloc[0]
    assert str(bad["date"]) == "2026-10-30" and bad["market"] == "TWSE"
    assert int(f["only_legacy"].sum()) == 0
    assert int(f["mm_trust_net"].sum()) == 1 and int(f["mm_foreign_net"].sum()) == 0

    # 外資持股：官方 issued_shares 對舊資料 total_shares
    lh = pd.read_parquet(cfg.legacy_dir / "foreign_holdings.parquet")
    lh = lh[lh["quality_action"].isin(["keep", "recompute"])]
    d = pd.Timestamp("2026-10-30").date()
    x = lh[(pd.to_datetime(lh["date"]).dt.date == d) & (lh["market"] == "TWSE")].copy()
    x["date"] = d
    x = x.rename(columns={"total_shares": "issued_shares"})[["date", "market", "code", "name", "issued_shares",
                                                           "foreign_shares"]]
    x.loc[x.index[0], "issued_shares"] += 1
    store.write_curated("foreign_holdings", "TWSE", d, x)
    res = compare(cfg)
    h = res[res["dataset"] == "foreign_holdings"]
    assert int(h["mm_issued_shares"].sum()) == 1 and int(h["mm_foreign_shares"].sum()) == 0
    rows = pd.read_parquet(cfg.quality_dir / "legacy_compare_rows.parquet")
    assert set(rows["dataset"]) >= {"inst_flows", "foreign_holdings"}



def test_compare_broker_branch_and_date_shift(tmp_path):
    """券商分點比的是分點代號（b 參數），不是總公司代號；並能指出「舊資料日期錯位」。"""
    cfg = Config(data_dir=tmp_path)
    import_archive(cfg, ARCHIVE)
    store = Store(cfg)
    b = pd.read_parquet(cfg.legacy_dir / "broker_trades.parquet")
    b["d"] = pd.to_datetime(b["trade_date"]).dt.date
    d1, d2 = pd.Timestamp("2026-10-29").date(), pd.Timestamp("2026-10-30").date()
    for d in (d1, d2):
        x = b[b["d"] == d].rename(columns={"stock_code": "code", "buy_vol": "buy_lots", "sell_vol": "sell_lots",
                                           "net_vol": "net_lots", "broker_id": "broker_b"})
        x["date"] = d
        x["bhid"] = "ZZ00"          # 總公司代號與分點代號不同，不應造成差異
        store.write_curated("broker_top", "ALL", d, x[["date", "code", "side", "rank", "broker_name", "bhid",
                                                       "broker_b", "buy_lots", "sell_lots", "net_lots", "pct"]])
    res = compare(cfg)
    t = res[res["dataset"] == "broker_top"]
    assert int(t["mismatched"].sum()) == 0 and int(t["both"].sum()) >= 600
    import json as _j
    bb = _j.loads((cfg.quality_dir / "legacy_compare_shift.json").read_text(encoding="utf-8"))["broker_top_by_branch"]
    assert bb["unequal"] == 0 and bb["equal"] > 500 and bb["only_legacy"] == 0, bb

    # 舊資料日期錯位：把官方 10/30 的三大法人換成「舊資料 10/29 的值」→ 舊資料 10/29 等於官方 10/30（k=+1）
    leg = pd.read_parquet(cfg.legacy_dir / "flows.parquet")
    leg = leg[leg["quality_action"].isin(["keep", "recompute"])]
    leg["d"] = pd.to_datetime(leg["date"]).dt.date
    for d, src in ((d1, d2), (d2, d1)):
        x = leg[(leg["d"] == src) & (leg["market"] == "TWSE")].copy()
        x["date"] = d
        store.write_curated("inst_flows", "TWSE", d, x[["date", "market", "code", "name", "foreign_net",
                                                       "trust_net", "dealer_net"]])
    compare(cfg)
    import json
    st = json.loads((cfg.quality_dir / "legacy_compare_shift.json").read_text(encoding="utf-8"))["inst_flows"]
    assert st["n"] > 100 and st["k=+1"] > 0.4 and st["k=-1"] > 0.4, st
