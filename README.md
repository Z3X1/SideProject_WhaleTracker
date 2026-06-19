# 🐳 GEX Oracle — 鯨魚鏈上行為追蹤系統

## 系統架構

```
GitHub Actions (每小時觸發)
        │
        ▼
whale_tracker.py          ← 主採集引擎
  ├─ Blockchair API        ← Top 100 地址快照 + 交易詳情
  ├─ mempool.space API     ← 即時交易顆粒度（無速率限制）
  └─ SQLite (data/whale.db) ← 本地持久化
        │
        ▼
signal_bridge.py          ← GEX Oracle 信號橋接層
  ├─ 交易所流量計算         ← 正值=淨流出=看多
  ├─ 同步移動檢測           ← 機構協調行為
  ├─ 休眠甦醒檢測           ← 稀有高強度信號
  └─ data/oracle_signal.json ← Oracle 可消費信號包
        │
        ▼
daily_report.py           ← 每日報告（UTC 00:00）
  └─ dashboard/whale_dashboard.html ← 視覺化 Dashboard
```

## 數據顆粒度

| 層級 | 數據 | 更新頻率 |
|------|------|---------|
| L1：地址快照 | 餘額、TX 數、首/末交易時間、Δ 變化 | 每小時 |
| L2：交易記錄 | TXID、方向、金額、區塊時間、對手方標籤 | 每小時（最近 50 筆/地址） |
| L3：行為信號 | 同步移動、交易所流量、休眠甦醒 | 每小時衍生 |
| L4：Oracle 信號 | 統一場方程式行為分項（-1 ~ +1） | 每小時 |
| L5：每日報告 | 24h 聚合 + HTML Dashboard | 每日 UTC 00:00 |

## 統一場方程式整合

```
P(結算在X) = 0.40×GBM + 0.10×GEX + 0.28×行為信號
                                      ↑
                         鯨魚行為 = 行為信號的子項
                         子項權重：
                           交易所流量  45%
                           同步移動    30%
                           休眠甦醒    15%
                           餘額變化    10%
           + 0.12×貝葉斯 + 0.10×時間衰減
```

## 硬性觸發條件（觸發 GEX Oracle 更新）

| 條件 | 閾值 |
|------|------|
| 交易所淨流量 | ≥ ±1,000 BTC/h |
| 鯨魚同步移動 | ≥ 15 個地址/h |
| 休眠地址甦醒 | ≥ 3 個/h |
| 信號評分急變 | 1h 內 Δ ≥ 0.3 |

## 部署步驟

### 1. Fork / Clone 到你的 GitHub repo

```bash
# 複製這些文件到 SideProject_Options repo：
cp -r .github/        ../SideProject_Options/
cp -r src/            ../SideProject_Options/src/whale/
cp requirements.txt   ../SideProject_Options/
```

### 2. 設定 GitHub Secret

在 repo Settings → Secrets → Actions：
```
GH_PAT = <你的 Personal Access Token（需要 repo write 權限）>
```

### 3. 確認 Actions 已啟用

repo → Actions → Enable workflows

### 4. 手動觸發第一次測試

Actions → GEX Oracle - Whale Tracker → Run workflow

## 本地測試

```bash
pip install -r requirements.txt
python src/whale_tracker.py    # 執行一次採集
python src/signal_bridge.py   # 生成信號包
python src/daily_report.py    # 生成每日報告
```

## 輸出文件

| 文件 | 說明 |
|------|------|
| `data/whale.db` | SQLite 主數據庫（不 commit，用 Actions cache） |
| `data/latest_summary.json` | 最新小時摘要（每小時 commit） |
| `data/oracle_signal.json` | Oracle 信號包（每小時 commit） |
| `data/daily_YYYY-MM-DD.json` | 每日報告 JSON |
| `dashboard/whale_dashboard.html` | 每日 HTML Dashboard |

## 已知限制

- Blockchair 免費 tier：30 req/min，100 地址 × 2 API 呼叫 = ~7 分鐘/批次
- 交易所標籤需手動維護（EXCHANGE_LABELS 字典）
- 「倉位」無法從鏈上獲取，只能追蹤持幣量變化
- GitHub Actions 免費 tier：2,000 min/月，每小時 45min = ~1,080 min/月（安全）
