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
            store.write_curated("inst_flows", m, d, x)
    res = compare(cfg)
    f = res[res["dataset"] == "inst_flows"]
    assert int(f["mismatched"].sum()) == 1
    bad = f[f["mismatched"] > 0].iloc[0]
    assert str(bad["date"]) == "2026-10-30" and bad["market"] == "TWSE"
    assert int(f["only_legacy"].sum()) == 0
