"""各資料集的標準欄位與型別。所有 parquet 依此寫出，型別固定、缺值為 NULL。"""
from __future__ import annotations

import pandas as pd

_I = "Int64"
_F = "Float64"
_S = "string"

SCHEMAS: dict[str, dict[str, str]] = {
    "quotes": {
        "date": "date", "market": _S, "code": _S, "name": _S,
        "open": _F, "high": _F, "low": _F, "close": _F, "change": _F, "ex_right": "boolean",
        "volume_shares": _I, "value_twd": _I, "trades": _I,
    },
    "inst_flows": {
        "date": "date", "market": _S, "code": _S, "name": _S,
        # 外資及陸資（不含外資自營商）
        "fx_buy": _I, "fx_sell": _I, "fx_net": _I,
        # 外資自營商
        "fd_buy": _I, "fd_sell": _I, "fd_net": _I,
        # 外資合計（TPEX 有官方值；TWSE 為 NULL，用 fx + fd 推得）
        "foreign_buy": _I, "foreign_sell": _I, "foreign_net": _I,
        "trust_buy": _I, "trust_sell": _I, "trust_net": _I,
        # 自營商（自行買賣）、自營商（避險）
        "ds_buy": _I, "ds_sell": _I, "ds_net": _I,
        "dh_buy": _I, "dh_sell": _I, "dh_net": _I,
        # 自營商合計（TWSE 只有官方買賣超；TPEX 三者皆有）
        "dealer_buy": _I, "dealer_sell": _I, "dealer_net": _I,
        # 官方三大法人買賣超合計
        "total_net": _I,
    },
    "foreign_holdings": {
        "date": "date", "market": _S, "code": _S, "name": _S,
        "issued_shares": _I, "foreign_available_shares": _I, "foreign_shares": _I,
        "available_ratio": _F, "foreign_ratio": _F, "limit_ratio": _F,
    },
    "broker_top": {
        "date": "date", "code": _S, "side": _S, "rank": _I,
        "broker_name": _S, "bhid": _S, "broker_b": _S,
        "buy_lots": _I, "sell_lots": _I, "net_lots": _I, "pct": _F,
    },
    "broker_top_summary": {
        "date": "date", "code": _S, "n_buy": _I, "n_sell": _I,
        "total_buy_net_lots": _I, "total_sell_net_lots": _I,
        "avg_buy_cost": _F, "avg_sell_cost": _F,
        "implied_vol_lots": _F,   # 由佔成交比重反推的成交張數（T5 校正用）
    },
    # 證交所鉅額交易日成交資訊（單一證券）。每筆成交一列；seq 為官方回應中的順序。
    # 證交所每日收盤行情的成交股數包含鉅額交易，富邦分點頁的佔比分母不含，T5 需扣除。
    "block_trades": {
        "date": "date", "market": _S, "code": _S, "name": _S, "seq": _I, "trade_type": _S,
        "price": _F, "volume_shares": _I, "value_twd": _I,
    },
    "broker_bsr": {
        "date": "date", "code": _S, "seq": _I, "broker_id": _S, "broker_name": _S,
        "price": _F, "buy_shares": _I, "sell_shares": _I,
    },
}

KEYS: dict[str, list[str]] = {
    "quotes": ["date", "market", "code"],
    "inst_flows": ["date", "market", "code"],
    "foreign_holdings": ["date", "market", "code"],
    "broker_top": ["date", "code", "side", "rank"],
    "broker_top_summary": ["date", "code"],
    "broker_bsr": ["date", "code", "seq"],
    "block_trades": ["date", "market", "code", "seq"],
}

# 每日管線依此順序處理：後面的資料集的跨表檢查依賴前面的。
DAILY_DATASETS = ["quotes", "inst_flows", "foreign_holdings", "block_trades", "broker_top"]
# broker_top_summary 與 broker_top 同頁產生、同進同出。
COMPANIONS = {"broker_top": ["broker_top_summary"]}


def conform(df: pd.DataFrame, dataset: str) -> pd.DataFrame:
    """補齊欄位、轉型、排序。多出的欄位直接報錯，避免悄悄丟資料。"""
    schema = SCHEMAS[dataset]
    extra = set(df.columns) - set(schema)
    if extra:
        raise ValueError(f"{dataset}: 未定義的欄位 {sorted(extra)}")
    out = pd.DataFrame(index=df.index)
    for col, dtype in schema.items():
        s = df[col] if col in df.columns else pd.Series(pd.NA, index=df.index)
        if dtype == "date":
            out[col] = pd.to_datetime(s).dt.date
        else:
            out[col] = s.astype(dtype)
    return out.sort_values(KEYS[dataset]).reset_index(drop=True)
