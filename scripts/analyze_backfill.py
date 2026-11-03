"""回補後的分布分析：檢查覆蓋、各檢查結果、F4、H3、T5 的實際分布。

用法：python scripts/analyze_backfill.py [資料目錄，預設 ~/twchip-data]
輸出是一段精簡文字，整段貼回對話即可。
"""
import json
import os
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

D = Path(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/twchip-data"))
con = duckdb.connect()
pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)


def src(ds):
    return (f"(select *, regexp_extract(filename, 'curated/[^/]+/([^/]+)/', 1) as mkt "
            f"from read_parquet('{D}/curated/{ds}/*/*/*.parquet', union_by_name=true, filename=true))")


def q(sql):
    return con.sql(sql).df()


print("== 1. 覆蓋（每個資料單位的交易日數與區間）")
rows = []
for ds in ("quotes", "inst_flows", "foreign_holdings", "broker_top", "broker_top_summary"):
    if list((D / "curated" / ds).glob("*/*/*.parquet")):
        rows.append(q(f"select '{ds}' ds, mkt, count(distinct date) n_days, min(date)::varchar d0, max(date)::varchar d1, "
                      f"count(*) n_rows from {src(ds)} group by mkt"))
print(pd.concat(rows).to_string(index=False))
d_all = q(f"select distinct date::varchar d from {src('quotes')} order by 1")["d"].tolist()
print("quotes 交易日（前 15 天）：", ", ".join(d_all[:15]))

print("\n== 2. 檢查結果統計（品質報告）")
stat, wfails = {}, []
for p in sorted((D / "quality").glob("*/*.json")):
    j = json.loads(p.read_text(encoding="utf-8"))
    for unit, u in (j.get("units") or {}).items():
        groups = [(unit, u.get("checks", []))] + [(f"{unit}:{c}", s.get("checks", [])) for c, s in (u.get("stocks") or {}).items()]
        for name, cl in groups:
            for c in cl:
                k = (c["id"], c["level"])
                stat.setdefault(k, {"pass": 0, "fail": 0, "skip": 0})[c["status"]] += 1
                if c["status"] == "fail":
                    wfails.append((j["date"], name, c["id"], c["detail"][:90]))
st = pd.DataFrame([{"check": k[0], "level": k[1], **v} for k, v in sorted(stat.items())])
print(st.to_string(index=False))
print(f"\n未通過明細（共 {len(wfails)} 筆，列出 T5 以外的全部，T5 只列前 5 筆）：")
non_t5 = [w for w in wfails if w[2] != "T5"]
for w in non_t5[:40] + [w for w in wfails if w[2] == "T5"][:5]:
    print("  ", " | ".join(map(str, w)))

print("\n== 3. F4：法人買進／賣出超過當日成交股數的檔數比例（每日）")
f4 = q(f"""
  with f as (select * from {src('inst_flows')}), qq as (select date, mkt, code, volume_shares v from {src('quotes')})
  select f.mkt, f.date::varchar d,
         avg(((greatest(coalesce(fx_buy,0), coalesce(fx_sell,0), coalesce(fd_buy,0), coalesce(fd_sell,0),
               coalesce(trust_buy,0), coalesce(trust_sell,0), coalesce(ds_buy,0), coalesce(ds_sell,0),
               coalesce(dh_buy,0), coalesce(dh_sell,0)) > v))::int) rate
  from f join qq using (date, mkt, code) group by 1, 2""")
print(f4.groupby("mkt")["rate"].describe(percentiles=[.5, .9, .99]).round(4).to_string())
print("最高的 5 天：")
print(f4.sort_values("rate", ascending=False).head(5).round(4).to_string(index=False))

print("\n== 4. H3：外資持股日變化與當日外資買賣超的同號率（每日）")
h3 = q(f"""
  with td as (select date, lag(date) over (order by date) ptd from (select distinct date from {src('quotes')})),
       h0 as (select date, mkt, code,
                     foreign_shares - lag(foreign_shares) over (partition by mkt, code order by date) dh,
                     lag(date) over (partition by mkt, code order by date) pd
              from {src('foreign_holdings')}),
       h as (select h0.date, mkt, code, dh from h0 join td on h0.date = td.date where h0.pd = td.ptd),
       f as (select date, mkt, code, coalesce(foreign_net, fx_net + fd_net) fn from {src('inst_flows')})
  select h.mkt, h.date::varchar d, avg((sign(dh) = sign(fn))::int) agree, count(*) n
  from h join f using (date, mkt, code) where dh <> 0 and fn <> 0 group by 1, 2""")
print(h3.groupby("mkt")["agree"].describe(percentiles=[.05, .5]).round(3).to_string())
print("最低的 5 天：")
print(h3.sort_values("agree").head(5).round(3).to_string(index=False))

if list((D / "curated" / "broker_top_summary").glob("*/*/*.parquet")):
    print("\n== 5. T5：富邦隱含成交量 ÷ 官方成交量（每檔股票的分布）")
    t5 = q(f"""select s.code, s.date::varchar d, s.implied_vol_lots / (qq.volume_shares / 1000.0) ratio
              from {src('broker_top_summary')} s join {src('quotes')} qq using (date, code)
              where s.implied_vol_lots is not null and qq.volume_shares > 0""")
    if t5.empty:
        print("（沒有 implied_vol_lots：這段回補是在加入該欄位之前跑的）")
    else:
        t5["dev_from_stock_median"] = t5["ratio"] / t5.groupby("code")["ratio"].transform("median") - 1
        g = t5.groupby("code").agg(days=("ratio", "size"), median=("ratio", "median"),
                                   p05=("ratio", lambda s: s.quantile(.05)), p95=("ratio", lambda s: s.quantile(.95)),
                                   max_abs_dev=("dev_from_stock_median", lambda s: s.abs().max()))
        print(g.round(3).to_string())
        a = t5["dev_from_stock_median"].abs()
        print(f"\n相對各股中位數的偏差：中位數 {a.median():.3%}，P95 {a.quantile(.95):.3%}，P99 {a.quantile(.99):.3%}，最大 {a.max():.3%}")
        print("偏差最大的 5 筆：")
        print(t5.reindex(a.sort_values(ascending=False).index).head(5).round(4).to_string(index=False))

        print("\n== 6. 新版 T5 會看到的偏離：今日比值 ÷ 該股前 20 日中位數 − 1（至少 10 日歷史）")
        t5 = t5.sort_values(["code", "d"]).copy()
        t5["ref"] = t5.groupby("code")["ratio"].transform(
            lambda s: s.shift(1).rolling(20, min_periods=10).median())
        r = t5.dropna(subset=["ref"]).copy()
        r["dev"] = r["ratio"] / r["ref"] - 1
        a = r["dev"].abs()
        print(f"樣本 {len(r)} 筆（{r['code'].nunique()} 檔）")
        print(f"|偏離|：中位數 {a.median():.3%}，P90 {a.quantile(.9):.3%}，P95 {a.quantile(.95):.3%}，"
              f"P99 {a.quantile(.99):.3%}，P99.9 {a.quantile(.999):.3%}，最大 {a.max():.3%}")
        for tol in (0.02, 0.03, 0.05, 0.08, 0.10):
            print(f"  門檻 {tol:.0%}：會擋下 {int((a > tol).sum())} 筆（{(a > tol).mean():.2%}）")
        print("各股 |偏離| P99：")
        print(r.groupby("code")["dev"].apply(lambda s: s.abs().quantile(.99)).round(4).sort_values().to_string())
        print("偏離最大的 10 筆：")
        print(r.reindex(a.sort_values(ascending=False).index).head(10)[["code", "d", "ratio", "ref", "dev"]]
              .round(4).to_string(index=False))
