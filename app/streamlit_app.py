"""twchip 儀表板。只讀已通過驗證的 curated 資料。

執行：TWCHIP_DATA_DIR=data streamlit run app/streamlit_app.py
若本機沒有資料，且設定了 TWCHIP_HF_REPO 與 HF_TOKEN，可在側欄一鍵下載。
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

DATA = Path(os.environ.get("TWCHIP_DATA_DIR", "data"))
CUR = DATA / "curated"

st.set_page_config(page_title="台股籌碼資料庫", page_icon="📊", layout="wide")


def glob(ds: str) -> str:
    return str(CUR / ds / "*" / "*" / "*.parquet")


def has(ds: str) -> bool:
    return any((CUR / ds).glob("*/*/*.parquet")) if (CUR / ds).exists() else False


@st.cache_resource
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect()
    for ds in ("quotes", "inst_flows", "foreign_holdings", "broker_top", "broker_top_summary"):
        if has(ds):
            c.execute(f"create or replace view {ds} as select * from read_parquet('{glob(ds)}', union_by_name=true)")
    return c


@st.cache_data(ttl=600)
def q(sql: str) -> pd.DataFrame:
    return con().sql(sql).df()


# ------------------------------------------------------------------ 側欄
with st.sidebar:
    st.title("台股籌碼資料庫")
    st.caption(f"資料目錄：`{DATA}`")
    if os.environ.get("TWCHIP_HF_REPO") and st.button("從 Hugging Face 更新資料"):
        from twchip import hf
        from twchip.config import Config
        with st.spinner("下載中…"):
            hf.pull(Config(data_dir=DATA))
        st.cache_resource.clear(); st.cache_data.clear()
        st.rerun()
    st.markdown("所有數字皆已通過驗證閘門。未通過的資料在 `quarantine/`，不會出現在這裡。")

if not has("quotes"):
    st.info("尚無資料。請先執行 `twchip run` 或從 Hugging Face 下載。")
    st.stop()

latest = q("select max(date) d from quotes")["d"].iloc[0]
tab_health, tab_rank, tab_stock, tab_broker, tab_checks = st.tabs(
    ["資料健康", "三大法人排行", "個股", "券商分點", "檢查結果"])

# ------------------------------------------------------------------ 資料健康
with tab_health:
    st.subheader("每日發布狀態")
    rows = []
    for p in sorted((DATA / "quality").glob("*/*.json"))[-60:]:
        j = json.loads(p.read_text(encoding="utf-8"))
        r = {"日期": j.get("date"), "日曆": j.get("calendar", {}).get("status", "")}
        r.update({k: v.get("status", "") for k, v in (j.get("units") or {}).items()})
        rows.append(r)
    if rows:
        df = pd.DataFrame(rows).set_index("日期").sort_index(ascending=False)
        colors = {"published": "#d7f0dd", "cached": "#d7f0dd", "quarantined": "#f8d0d0", "blocked": "#fde7c8",
                  "pending": "#fde7c8", "not_published": "#eeeeee", "error": "#f8d0d0"}
        st.dataframe(df.style.map(lambda v: f"background-color: {colors.get(v, '')}"), width="stretch")
    qdir = DATA / "quarantine"
    files = sorted(qdir.rglob("*.parquet")) if qdir.exists() else []
    st.subheader(f"隔離區（{len(files)} 個檔案）")
    if files:
        st.write(pd.DataFrame({"檔案": [str(f.relative_to(qdir)) for f in files]}))
    lc = DATA / "quality" / "legacy_compare.parquet"
    if lc.exists():
        st.subheader("舊專案資料比對")
        c = pd.read_parquet(lc)
        st.write(c.groupby("dataset")[["both", "mismatched", "only_official", "only_legacy"]].sum())

# ------------------------------------------------------------------ 三大法人排行
with tab_rank:
    c1, c2, c3, c4 = st.columns(4)
    win = c1.selectbox("視窗（交易日）", [1, 5, 10, 20, 60], index=1)
    mkt = c2.selectbox("市場", ["全部", "TWSE", "TPEX"])
    who = c3.selectbox("法人", {"合計": "total_net", "外資": "foreign_net", "投信": "trust_net", "自營商": "dealer_net"})
    no_etf = c4.checkbox("排除 ETF（00 開頭）", value=True)
    col = {"合計": "total_net", "外資": "foreign_net", "投信": "trust_net", "自營商": "dealer_net"}[who]
    where = []
    if mkt != "全部":
        where.append(f"market = '{mkt}'")
    if no_etf:
        where.append("code not like '00%'")
    w = (" and " + " and ".join(where)) if where else ""
    days = q(f"select distinct date from inst_flows order by date desc limit {win}")["date"]
    if len(days):
        d0 = min(days)
        # TWSE 的外資合計是 不含自營 + 外資自營；兩市場欄位定義一致
        df = q(f"""select code, any_value(name) name, market,
                   round(sum(coalesce(foreign_net, fx_net + fd_net))/1000) 外資,
                   round(sum(trust_net)/1000) 投信, round(sum(dealer_net)/1000) 自營商,
                   round(sum(total_net)/1000) 合計
                   from inst_flows where date >= '{d0}' {w} group by code, market""")
        key = {"total_net": "合計", "foreign_net": "外資", "trust_net": "投信", "dealer_net": "自營商"}[col]
        st.caption(f"{d0} ～ {max(days)}，共 {len(days)} 個交易日；單位：張")
        a, b = st.columns(2)
        a.markdown("**買超**"); a.dataframe(df.sort_values(key, ascending=False).head(50), hide_index=True)
        b.markdown("**賣超**"); b.dataframe(df.sort_values(key).head(50), hide_index=True)

# ------------------------------------------------------------------ 個股
with tab_stock:
    code = st.text_input("股票代號", "2330").strip()
    px = q(f"select date, close, volume_shares/1000 as 成交張數 from quotes where code='{code}' order by date")
    if px.empty:
        st.warning("查無行情")
    else:
        name = q(f"select name from quotes where code='{code}' order by date desc limit 1")["name"].iloc[0]
        st.subheader(f"{code} {name}")
        st.line_chart(px.set_index("date")["close"], height=220)
        fl = q(f"""select date, coalesce(foreign_net, fx_net+fd_net)/1000 外資, trust_net/1000 投信, dealer_net/1000 自營商
                  from inst_flows where code='{code}' order by date""")
        if not fl.empty:
            st.markdown("**三大法人買賣超（張）**")
            st.bar_chart(fl.set_index("date"), height=220)
        if has("foreign_holdings"):
            fh = q(f"select date, foreign_ratio 外資持股比率 from foreign_holdings where code='{code}' order by date")
            if not fh.empty:
                st.markdown("**外資持股比率（%）**")
                st.line_chart(fh.set_index("date"), height=180)
        if has("broker_top"):
            bt = q(f"select * from broker_top where code='{code}' order by date desc, side, rank")
            if not bt.empty:
                d = bt["date"].iloc[0]
                st.markdown(f"**{d} 主力進出前 15 名（張）**")
                x = bt[bt["date"] == d]
                a, b = st.columns(2)
                a.dataframe(x[x.side == "buy"][["rank", "broker_name", "buy_lots", "sell_lots", "net_lots", "pct"]], hide_index=True)
                b.dataframe(x[x.side == "sell"][["rank", "broker_name", "buy_lots", "sell_lots", "net_lots", "pct"]], hide_index=True)

# ------------------------------------------------------------------ 券商分點
with tab_broker:
    if not has("broker_top"):
        st.info("尚無券商分點資料")
    else:
        st.caption("只包含分點進入某檔股票前 15 名的日子；沒上榜的日子沒有資料，不是 0。分點以代號（BHID）識別，名稱取最新一次。")
        bk = q("""select bhid, arg_max(broker_name, date) 名稱, count(*) 上榜次數, count(distinct code) 股票數,
                  sum(net_lots) 淨買賣超張數 from broker_top group by bhid order by 上榜次數 desc""")
        pick = st.selectbox("分點", bk["bhid"], format_func=lambda b: f"{b} {bk.set_index('bhid').loc[b, '名稱']}")
        x = q(f"select date, code, net_lots from broker_top where bhid='{pick}' order by date")
        st.dataframe(bk[bk.bhid == pick], hide_index=True)
        if not x.empty:
            st.bar_chart(x.pivot_table(index="date", columns="code", values="net_lots", aggfunc="sum"), height=260)
            names = q(f"select distinct broker_name, min(date) 首見, max(date) 最後 from broker_top where bhid='{pick}' group by 1")
            if len(names) > 1:
                st.warning("此代號曾有多個名稱（券商合併或改名）：")
                st.dataframe(names, hide_index=True)

# ------------------------------------------------------------------ 檢查結果
with tab_checks:
    rows = []
    for p in sorted((DATA / "quality").glob("*/*.json"))[-60:]:
        j = json.loads(p.read_text(encoding="utf-8"))
        for unit, u in (j.get("units") or {}).items():
            for c in u.get("checks", []):
                rows.append({"date": j["date"], "unit": unit, **{k: c[k] for k in ("id", "level", "status", "detail")}})
            for code, sv in (u.get("stocks") or {}).items():
                for c in sv.get("checks", []):
                    rows.append({"date": j["date"], "unit": f"{unit}:{code}", **{k: c[k] for k in ("id", "level", "status", "detail")}})
    if rows:
        df = pd.DataFrame(rows)
        st.subheader("各檢查通過率（近 60 日）")
        piv = df.pivot_table(index="id", columns="status", values="date", aggfunc="count", fill_value=0)
        st.dataframe(piv, width="stretch")
        st.subheader("失敗明細")
        st.dataframe(df[df.status == "fail"].sort_values("date", ascending=False), hide_index=True, width="stretch")
