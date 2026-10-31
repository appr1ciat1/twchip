# twchip：台股籌碼資料庫

三大法人買賣超、外資持股、個股日成交、券商分點。**沒有通過驗證的資料不發布。**

設計與完整的驗證矩陣見 [docs/DESIGN.md](docs/DESIGN.md)。

## 它和舊專案有什麼不同

| 舊專案的問題 | 這裡的做法 |
|---|---|
| 用「執行當下的台北日期」判斷目標日，排程延遲就跨日失敗，券商分點永久漏 38 天 | 待辦由資料狀態決定；每小時重試；富邦用單日查詢補回任何一天 |
| 休市日存了他日資料、端點忽略日期回傳舊快照 | G1 回應日期必須等於請求日期；G5 當日內容不可與前一交易日相同 |
| TPEX 外資比率解析成 0，一年沒人發現 | H1 比率必須等於 持股／發行股數 |
| 自營商欄位錯抓成 0 | F3 外資＋投信＋自營 必須等於官方合計 |
| 只存淨額 | 保留官方每一類的買進、賣出、買賣超與合計 |
| 缺值補 0 | 缺值就是 NULL |
| 抓到就存，事後才驗 | 原始回應永久保存 → 檢查 → 通過才寫入 curated，否則隔離並告警 |

## 資料流

```
官方／富邦 ──► raw/（gzip＋sha256，只增不改）
                │
                ▼
             解析（依欄位名，不猜位置）
                │
                ▼
     單表檢查 G1–G7、Q、F、H、T ──► 跨表檢查（F4、H3、T5、T6）
                │                         │
        全部通過 ▼                  有阻擋性失敗 ▼
           curated/                    quarantine/ ＋ 工作流程失敗（寄信）
```

## 一次性設定

1. 在 GitHub 建立新 repo，把這個目錄推上去。
2. 在 Hugging Face 建立 **寫入權限** 的 access token。
3. GitHub repo → Settings → Secrets and variables → Actions：
   - Secret `HF_TOKEN`：上一步的 token
   - Variable `TWCHIP_HF_REPO`：例如 `yourname/twchip-data`（不存在會自動建立為私有）
4. Actions → **probe** → Run workflow。檢查輸出：每個來源的欄位、解析結果、檢查是否通過。
   這一步是唯一真正驗證「端點與欄位」的地方，請務必看過再往下。
5. Actions → **backfill**：起日 `2025-09-11`、迄日今天。逐月回補並上傳。
6. 本機匯入舊專案資料並比對：

   ```bash
   pip install -e ".[app,dev]"
   export TWCHIP_HF_REPO=yourname/twchip-data HF_TOKEN=...
   twchip hf-pull
   twchip legacy-import /path/to/tw-inst-archive
   twchip legacy-compare
   twchip hf-push --since 0 --message "legacy import"
   ```

之後 **daily** 會在平日台北 16:17 到 00:17 每小時執行，**audit** 每週日抽樣重抓比對。

## 指令

```
twchip probe [--date D] [--codes 2330,6488]   每個來源抓一次，印出解析與檢查結果（不寫入）
twchip run [--lookback 10]                    處理近期尚未完成的交易日
twchip backfill START END [--datasets ...]    依日期順序回補
twchip status [--days 30]                     每日各單位狀態
twchip sla                                    逾時或需人工確認 → 非零結束
twchip audit [--n 10] [--days 90]             隨機重抓過去資料逐列比對
twchip legacy-import ARCHIVE / legacy-compare 舊資料匯入與比對
twchip hf-pull / hf-push --since EPOCH        Hugging Face 同步
twchip bsr CODE                               證交所日報表人工稽核（需輸入驗證碼）
```

## 儀表板

```bash
TWCHIP_DATA_DIR=data streamlit run app/streamlit_app.py
```

分頁：資料健康、三大法人排行、個股、券商分點、檢查結果。只讀已通過驗證的資料。

## 資料佈局（Hugging Face dataset）

```
raw/{source}/{YYYY}/{YYYYMMDD}/{key}__{sha12}.{ext}.gz
curated/{dataset}/{market}/{YYYY}/{YYYY-MM-DD}.parquet
quarantine/{dataset}/{market}/{YYYY}/{YYYY-MM-DD}.parquet
quality/{YYYY}/{YYYY-MM-DD}.json
meta/calendar.parquet、meta/holidays.parquet
legacy/
```

用 DuckDB 直接查：

```sql
select date, code, foreign_net/1000 as 外資張數
from read_parquet('data/curated/inst_flows/*/*/*.parquet')
where code = '2330' order by date desc limit 10;
```

## 已知限制與待確認

- **端點欄位未經實站驗證。** 開發環境連不到官方網站。TWSE、TPEX 的欄位名稱依公開文件與開源實作建構，富邦頁面用真實數值測過。第一次請先跑 probe。
- **T5 容許誤差（3%）需校正。** 「佔成交比重」的分母是否含盤後與鉅額交易尚未確認，probe 會印出實際差距，可用 `TWCHIP_BROKER_VOLUME_TOL` 調整。
- **證交所日報表不自動辨識驗證碼。** 驗證碼的目的就是阻擋自動化。`twchip bsr` 讓你人工輸入，用於每日抽樣稽核。表單欄位是推定的，第一次請在本機手動跑通。
- **券商分點只有前 15 名。** 沒上榜的日子沒有資料，統計時不能當 0。
- **富邦是第三方網站。** 請自行確認使用條款；限速設定在 `config.py`。
