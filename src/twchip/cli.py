"""命令列介面。

  twchip probe [--date D] [--codes 2330,2317]   每個資料源抓一次，印出解析與檢查結果，不寫入 curated
  twchip run [--lookback 10] [--force]          處理最近 N 個日曆日中尚未完成的交易日（每日排程用）
  twchip backfill START END [--datasets ...]    依日期順序回補
  twchip status [--days 30]                     各單位每日狀態表
  twchip sla                                    逾時或待確認項目；有就以非零代碼結束（讓 CI 告警）
  twchip legacy-import ARCHIVE_DIR              匯入舊專案封存庫
  twchip legacy-compare                         legacy 與官方資料逐列比對
  twchip hf-pull [--years 2025 2026]            從 Hugging Face 下載
  twchip hf-push --since EPOCH                  上傳該時間後變更的檔案
  twchip audit [--n 10] [--days 90]             隨機重抓過去資料逐列比對（偵測事後改寫）
  twchip bsr CODE                               證交所日報表人工稽核（需輸入驗證碼）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta

import pandas as pd

from .config import TPE, Config


def _d(s: str) -> date:
    return date.fromisoformat(s)


def cmd_probe(cfg: Config, args) -> int:
    from .checks import rules
    from .http import Fetcher
    from .rawstore import RawStore
    from .sources import fubon, tpex, twse
    d = _d(args.date) if args.date else datetime.now(TPE).date()
    fx = Fetcher(RawStore(cfg.data_dir), cfg.min_interval)
    print(f"== probe {d}（原始回應存於 {cfg.raw_dir}）")
    results, quotes = {}, {}
    for name, fn in [("quotes/TWSE", twse.fetch_mi_index), ("quotes/TPEX", tpex.fetch_otc_quotes),
                     ("inst_flows/TWSE", twse.fetch_t86), ("inst_flows/TPEX", tpex.fetch_daily_trade),
                     ("foreign_holdings/TWSE", twse.fetch_mi_qfiis), ("foreign_holdings/TPEX", tpex.fetch_qfii)]:
        try:
            p = fn(fx, d)
        except Exception as e:  # noqa: BLE001
            print(f"\n[{name}] 抓取失敗：{e}")
            continue
        ds, m = name.split("/")
        print(f"\n[{name}] rows={len(p.df)} served={p.served_date} no_data={p.no_data} raw={[str(r.path) for r in p.raw]}")
        print(f"  fields={p.fields}")
        if p.field_issues:
            print(f"  ⚠ 欄位問題：{p.field_issues}")
        if not p.df.empty:
            print(p.df.head(3).to_string(max_cols=12))
            res = rules.generic(p, d, ds, [], None)
            if ds == "quotes":
                quotes[m] = p.df
                res += rules.quotes(p.df, None)
            elif ds == "inst_flows":
                res += rules.inst_flows(p.df, quotes.get(m))
            for r in res:
                print(f"  {r.status.upper():4} {r.level} {r.id} {r.name}：{r.detail}")
        results[name] = p
    qall = pd.concat(list(quotes.values())) if quotes else pd.DataFrame()
    for code in (args.codes.split(",") if args.codes else cfg.broker_codes[:2]):
        try:
            p = fubon.fetch_broker_top(fx, code, d)
        except Exception as e:  # noqa: BLE001
            print(f"\n[broker {code}] 抓取失敗：{e}")
            continue
        print(f"\n[broker {code}] rows={len(p.df)} served={p.served_date} summary={p.extras.get('summary')}")
        if not p.df.empty:
            print(p.df.head(4).to_string())
            q = qall.set_index("code").loc[code] if not qall.empty and code in set(qall["code"]) else None
            for r in rules.broker_stock(p.df, p.extras["summary"], p.served_date, d, q, cfg.broker_volume_tol):
                print(f"  {r.status.upper():4} {r.level} {r.id} {r.name}：{r.detail}")
    return 0


def _pipeline(cfg: Config):
    from .pipeline import Pipeline
    return Pipeline(cfg)


def cmd_run(cfg: Config, args) -> int:
    end = datetime.now(TPE).date()
    p = _pipeline(cfg)
    p.run(end - timedelta(days=args.lookback), end, args.datasets, args.force)
    return 0


def cmd_backfill(cfg: Config, args) -> int:
    p = _pipeline(cfg)
    p.run(_d(args.start), _d(args.end), args.datasets, args.force)
    return 0


def cmd_status(cfg: Config, args) -> int:
    from .store import Store
    s = Store(cfg)
    end = datetime.now(TPE).date()
    rows = []
    for i in range(args.days, -1, -1):
        d = end - timedelta(days=i)
        q = s.read_quality(d)
        if not q:
            continue
        r = {"date": d.isoformat(), "calendar": q.get("calendar", {}).get("status", "")}
        for k, v in (q.get("units") or {}).items():
            r[k] = v.get("status", "")
        rows.append(r)
    print(pd.DataFrame(rows).to_string(index=False) if rows else "尚無品質紀錄")
    return 0


def cmd_sla(cfg: Config, args) -> int:
    issues = _pipeline(cfg).overdue(args.lookback)
    for i in issues:
        print("⚠", i)
    if not issues:
        print("OK：沒有逾時或待確認項目")
    return 1 if issues else 0


def cmd_legacy_import(cfg: Config, args) -> int:
    from .legacy import import_archive
    print(import_archive(cfg, args.archive))
    return 0


def cmd_legacy_compare(cfg: Config, args) -> int:
    from .legacy import compare
    res = compare(cfg)
    if res.empty:
        print("沒有可比對的重疊日期")
        return 0
    s = res.groupby("dataset")[["both", "mismatched", "only_official", "only_legacy"]].sum()
    print(s.to_string())
    bad = res[(res["mismatched"] > 0) | (res["only_legacy"] > 0)]
    print("\n有差異的日期：")
    print(bad.to_string(index=False) if not bad.empty else "（無）")
    return 0


def cmd_hf_pull(cfg: Config, args) -> int:
    from . import hf
    hf.pull(cfg, args.years, args.raw)
    return 0


def cmd_hf_push(cfg: Config, args) -> int:
    from . import hf
    hf.ensure_repo(cfg)
    files = hf.changed_since(cfg, args.since)
    n = hf.push(cfg, files, args.message) if files else 0
    print(f"已上傳 {n} 個檔案")
    return 0


def cmd_audit(cfg: Config, args) -> int:
    """隨機抽過去已發布的 (單位, 日期) 重新向官方／富邦抓取，逐列比對。有差異就非零結束。"""
    import random
    from .http import Fetcher
    from .pipeline import default_sources
    from .rawstore import RawStore
    from .schemas import KEYS
    from .store import Store
    s = Store(cfg)
    units, broker_fn = default_sources(Fetcher(RawStore(cfg.data_dir), cfg.min_interval))
    end = datetime.now(TPE).date()
    pool = [(ds, m, d) for (ds, m) in units for d in s.curated_dates(ds, m)
            if end - timedelta(days=args.days) <= d < end - timedelta(days=1)]
    pool += [("broker_top", "ALL", d) for d in s.curated_dates("broker_top", "ALL")
             if end - timedelta(days=args.days) <= d < end - timedelta(days=1)]
    random.seed(args.seed)
    picks = random.sample(pool, min(args.n, len(pool)))
    diffs = 0
    for ds, m, d in sorted(picks, key=lambda x: x[2]):
        cur = s.read_curated(ds, m, d)
        if ds == "broker_top":
            code = random.choice(sorted(set(cur["code"])))
            new, cur = broker_fn(code, d).df, cur[cur["code"] == code]
        else:
            new = units[(ds, m)](d).df
        keys = [k for k in KEYS[ds] if k in new.columns and k in cur.columns]
        vals = [c for c in cur.columns if c in new.columns and c not in keys and c != "name"]
        a = cur[keys + vals].astype(str).set_index(keys).sort_index()
        b = new[keys + vals].astype(str).set_index(keys).sort_index() if not new.empty else a.iloc[0:0]
        n_diff = len(a.index.symmetric_difference(b.index))
        common = a.index.intersection(b.index)
        n_diff += int((a.loc[common] != b.loc[common]).any(axis=1).sum())
        diffs += n_diff
        print(f"{'OK  ' if n_diff == 0 else 'DIFF'} {ds}/{m} {d}{' ' + code if ds == 'broker_top' else ''}：{n_diff} 列不同")
    return 1 if diffs else 0


def cmd_bsr(cfg: Config, args) -> int:
    from .http import Fetcher
    from .rawstore import RawStore
    from .sources import bsr
    d = _d(args.date) if args.date else datetime.now(TPE).date()
    fx = Fetcher(RawStore(cfg.data_dir), cfg.min_interval)

    def ask(img):
        print(f"驗證碼圖片：{img}（請打開後輸入）")
        return input("驗證碼：")

    p = bsr.fetch_interactive(fx, args.code, d, ask, cfg.data_dir / ".bsr_tmp")
    print(f"rows={len(p.df)} served={p.served_date} raw={[str(r.path) for r in p.raw]}")
    if not p.df.empty:
        agg = p.df.groupby(["broker_id", "broker_name"])[["buy_shares", "sell_shares"]].sum()
        agg["net_lots"] = ((agg["buy_shares"] - agg["sell_shares"]) / 1000).round().astype(int)
        print("全分點買進合計", int(p.df["buy_shares"].sum()), "賣出合計", int(p.df["sell_shares"].sum()))
        print(agg.sort_values("net_lots").head(15).to_string())
        print(agg.sort_values("net_lots").tail(15).to_string())
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="twchip")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("probe"); a.add_argument("--date"); a.add_argument("--codes")
    a = sub.add_parser("run"); a.add_argument("--lookback", type=int, default=10)
    a.add_argument("--datasets", nargs="*"); a.add_argument("--force", action="store_true")
    a = sub.add_parser("backfill"); a.add_argument("start"); a.add_argument("end")
    a.add_argument("--datasets", nargs="*"); a.add_argument("--force", action="store_true")
    a = sub.add_parser("status"); a.add_argument("--days", type=int, default=30)
    a = sub.add_parser("sla"); a.add_argument("--lookback", type=int, default=10)
    a = sub.add_parser("legacy-import"); a.add_argument("archive")
    sub.add_parser("legacy-compare")
    a = sub.add_parser("hf-pull"); a.add_argument("--years", nargs="*", type=int); a.add_argument("--raw", action="store_true")
    a = sub.add_parser("hf-push"); a.add_argument("--since", type=float, required=True)
    a.add_argument("--message", default="data update")
    a = sub.add_parser("audit"); a.add_argument("--n", type=int, default=10)
    a.add_argument("--days", type=int, default=90); a.add_argument("--seed", type=int, default=None)
    a = sub.add_parser("bsr"); a.add_argument("code"); a.add_argument("--date")
    args = ap.parse_args(argv)
    cfg = Config()
    fn = {"probe": cmd_probe, "run": cmd_run, "backfill": cmd_backfill, "status": cmd_status, "sla": cmd_sla,
          "legacy-import": cmd_legacy_import, "legacy-compare": cmd_legacy_compare, "hf-pull": cmd_hf_pull,
          "hf-push": cmd_hf_push, "audit": cmd_audit, "bsr": cmd_bsr}[args.cmd]
    return fn(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
