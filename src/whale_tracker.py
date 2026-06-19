"""
whale_tracker.py
GEX Oracle 鯨魚鏈上行為追蹤引擎 v1.0
數據源：Blockchair (免費tier) + mempool.space
頻率：每小時批量，GitHub Actions 觸發
"""

import requests
import json
import time
import os
import csv
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── 常數 ──────────────────────────────────────────────────────────────────────

BLOCKCHAIR_BASE = "https://api.blockchair.com/bitcoin"
MEMPOOL_BASE    = "https://mempool.space/api"
DATA_DIR        = Path("data")
DB_PATH         = DATA_DIR / "whale.db"

# Blockchair 免費 tier：30 req/min，無需 API key
# 每次請求之間強制 2.1 秒間隔 → 安全 ~28 req/min
RATE_LIMIT_SLEEP = 2.1

# 每小時批量：最多抓 TOP_N 個地址
TOP_N = 100

# 交易顆粒度：每個地址最多抓最近幾筆 TX
TX_PER_ADDRESS = 50

# 鯨魚行為信號閾值
WHALE_MOVE_BTC       = 100.0   # 單筆移動 ≥ 100 BTC = 重大事件
EXCHANGE_FLOW_BTC    = 500.0   # 流入/流出交易所 ≥ 500 BTC = 強信號
DORMANCY_DAYS        = 30      # 休眠超過 30 天的地址突然移動 = 極強信號
SYNC_WINDOW_MINUTES  = 60      # 共同行為時間窗口（分鐘）
MIN_SYNC_COUNT       = 5       # 同一時間窗口內至少 N 個鯨魚同時移動

# 已知交易所冷錢包標籤（持續維護）
EXCHANGE_LABELS = {
    "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo": "Binance",
    "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97": "Binance",
    "3LYJfcfHcvtWqWQx5rXNG7a4JKgmZP5aF5": "Binance",
    "1P5ZEDWTKTFGxQjZphgWPQUpe554WKDfHQ": "Coinbase",
    "3Cbq7aT1tY8kMxWLbitaG7yT6bPbKChq64": "Coinbase",
    "bc1qazcm763858nkj2dj986etajv6wquslv8uxwczt": "Kraken",
    "3E5L9wBBdFaHRzBkJQrqVCrFMWGqVNGeLH": "Kraken",
    "3LCGsSmfr24demGvriN4e3ft8wEcDuHFqh": "Bitfinex",
    "3JZq4atEAaEy18limMbzNhcgKPDfd8m1QL": "Bitfinex",
    "1FeexV6bAHb8ybZjqQMjJrcCrHGW9sb6uF": "Satoshi(?)_Dormant",
}

# ── 資料庫初始化 ───────────────────────────────────────────────────────────────

def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 地址快照表（每小時更新）
    c.execute("""
    CREATE TABLE IF NOT EXISTS address_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            TEXT NOT NULL,           -- ISO8601 UTC
        rank          INTEGER NOT NULL,
        address       TEXT NOT NULL,
        label         TEXT,                    -- 交易所標籤或 NULL
        balance_btc   REAL NOT NULL,
        tx_count      INTEGER NOT NULL,
        first_seen    TEXT,
        last_seen     TEXT,
        balance_delta REAL DEFAULT 0           -- vs 上一快照的變化量
    )""")

    # 交易詳情表（顆粒度最高層）
    c.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        txid          TEXT NOT NULL,
        address       TEXT NOT NULL,
        ts_block      TEXT,                    -- 區塊時間 UTC
        ts_fetched    TEXT NOT NULL,           -- 抓取時間 UTC
        direction     TEXT NOT NULL,           -- 'in' | 'out'
        value_btc     REAL NOT NULL,
        block_height  INTEGER,
        fee_sat       INTEGER,
        input_count   INTEGER,
        output_count  INTEGER,
        is_coinbase   INTEGER DEFAULT 0,
        counterparty  TEXT,                    -- 已知標籤的對手方
        PRIMARY KEY (txid, address)
    )""")

    # 行為信號表（衍生層）
    c.execute("""
    CREATE TABLE IF NOT EXISTS behavior_signals (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            TEXT NOT NULL,
        signal_type   TEXT NOT NULL,           -- 見 SIGNAL_TYPES
        strength      REAL NOT NULL,           -- 0.0 ~ 1.0
        address_count INTEGER,                 -- 涉及幾個鯨魚地址
        btc_volume    REAL,
        direction     TEXT,                    -- 'bull' | 'bear' | 'neutral'
        description   TEXT,
        raw_json      TEXT                     -- 完整原始數據 JSON
    )""")

    # 每小時聚合摘要（給 Dashboard 用）
    c.execute("""
    CREATE TABLE IF NOT EXISTS hourly_summary (
        ts                   TEXT PRIMARY KEY,  -- 小時整點 UTC
        total_whale_volume   REAL,
        exchange_inflow      REAL,
        exchange_outflow     REAL,
        dormant_wake_count   INTEGER,
        sync_event_count     INTEGER,
        net_exchange_flow    REAL,              -- 正 = 淨流出（看多）負 = 淨流入（看空）
        signal_score         REAL,             -- -1.0(極熊) ~ +1.0(極牛)
        top_signal           TEXT
    )""")

    conn.commit()
    conn.close()
    print("[DB] 初始化完成")

# ── Blockchair API 封裝 ────────────────────────────────────────────────────────

class BlockchairClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "GEX-Oracle-WhaleTracker/1.0"})
        self._last_call = 0.0

    def _get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """帶速率限制的 GET，失敗時最多重試 3 次"""
        url = f"{BLOCKCHAIR_BASE}{endpoint}"
        for attempt in range(3):
            elapsed = time.time() - self._last_call
            if elapsed < RATE_LIMIT_SLEEP:
                time.sleep(RATE_LIMIT_SLEEP - elapsed)
            try:
                r = self.session.get(url, params=params, timeout=15)
                self._last_call = time.time()
                if r.status_code == 200:
                    return r.json()
                elif r.status_code == 429:
                    print(f"[WARN] Rate limit hit, sleeping 60s (attempt {attempt+1})")
                    time.sleep(60)
                else:
                    print(f"[WARN] HTTP {r.status_code} for {url}")
                    time.sleep(5)
            except Exception as e:
                print(f"[ERROR] {e} (attempt {attempt+1})")
                time.sleep(10)
        return None

    def get_top100_addresses(self) -> list[dict]:
        """
        抓取 BTC 持倉前 100 地址
        Blockchair 端點：/addresses?q=balance(gt:0)&s=balance(desc)&limit=100
        回傳欄位：address, balance, transaction_count, first_seen_sending, last_seen_sending
        """
        data = self._get("/addresses", params={
            "q": "balance(gt:100000000)",  # > 1 BTC（satoshi 單位）
            "s": "balance(desc)",
            "limit": TOP_N,
            "fields": "address,balance,transaction_count,first_seen_sending,last_seen_sending"
        })
        if not data or "data" not in data:
            return []
        results = []
        for row in data["data"]:
            results.append({
                "address":    row.get("address", ""),
                "balance_btc": row.get("balance", 0) / 1e8,
                "tx_count":   row.get("transaction_count", 0),
                "first_seen": row.get("first_seen_sending"),
                "last_seen":  row.get("last_seen_sending"),
            })
        return results

    def get_address_transactions(self, address: str, limit: int = TX_PER_ADDRESS) -> list[dict]:
        """
        抓取單一地址最近 N 筆交易，含完整 UTXO 顆粒度
        Blockchair 端點：/dashboards/address/{address}
        """
        data = self._get(f"/dashboards/address/{address}", params={
            "limit": limit,
            "transaction_details": "true"
        })
        if not data or "data" not in data or address not in data["data"]:
            return []

        addr_data = data["data"][address]
        txs_raw   = addr_data.get("transactions", [])
        utxo_data = addr_data.get("utxo", [])

        # 建立 txid → 方向 mapping（從 UTXO 推導）
        utxo_txids = {u["transaction_hash"] for u in utxo_data}

        results = []
        for tx in txs_raw:
            # tx 在此 API 回傳格式為 txid 字符串（需另查詳情）
            # 先記錄 txid，詳情由 get_tx_detail 補充
            results.append({
                "txid":    tx if isinstance(tx, str) else tx.get("transaction_hash", ""),
                "address": address,
                "is_utxo": (tx if isinstance(tx, str) else tx.get("transaction_hash", "")) in utxo_txids,
            })
        return results

    def get_tx_detail(self, txid: str) -> Optional[dict]:
        """
        抓取單筆交易完整詳情
        含：時間、區塊高度、所有 inputs/outputs、手續費
        """
        data = self._get(f"/dashboards/transaction/{txid}")
        if not data or "data" not in data or txid not in data["data"]:
            return None

        tx = data["data"][txid]
        raw = tx.get("transaction", {})
        inputs  = tx.get("inputs", [])
        outputs = tx.get("outputs", [])

        # 識別已知對手方
        all_addresses = (
            [inp.get("recipient", "") for inp in inputs] +
            [out.get("recipient", "") for out in outputs]
        )
        counterparty = next(
            (EXCHANGE_LABELS[a] for a in all_addresses if a in EXCHANGE_LABELS),
            None
        )

        return {
            "txid":         txid,
            "ts_block":     raw.get("time"),
            "block_height": raw.get("block_id"),
            "fee_sat":      raw.get("fee"),
            "input_count":  raw.get("input_count"),
            "output_count": raw.get("output_count"),
            "is_coinbase":  1 if raw.get("is_coinbase") else 0,
            "counterparty": counterparty,
            "inputs":       inputs,
            "outputs":      outputs,
            "total_out_btc": sum(o.get("value", 0) for o in outputs) / 1e8,
        }

# ── mempool.space 封裝（補充即時數據）────────────────────────────────────────

class MempoolClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "GEX-Oracle-WhaleTracker/1.0"})

    def get_address_txs(self, address: str) -> list[dict]:
        """抓取地址最近交易（mempool.space，無速率限制）"""
        try:
            r = self.session.get(f"{MEMPOOL_BASE}/address/{address}/txs", timeout=10)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            print(f"[mempool] {address}: {e}")
        return []

    def get_block_fee_rates(self) -> dict:
        """當前手續費率（sat/vB），用於判斷鯨魚緊急程度"""
        try:
            r = self.session.get(f"{MEMPOOL_BASE}/v1/fees/recommended", timeout=10)
            if r.status_code == 200:
                return r.json()
        except:
            pass
        return {}

    def get_mempool_stats(self) -> dict:
        """mempool 積壓狀態"""
        try:
            r = self.session.get(f"{MEMPOOL_BASE}/mempool", timeout=10)
            if r.status_code == 200:
                return r.json()
        except:
            pass
        return {}

# ── 行為分析引擎 ──────────────────────────────────────────────────────────────

class BehaviorAnalyzer:
    """
    從原始交易數據中提取鯨魚共同行為信號
    信號類型（SIGNAL_TYPES）：
      SYNC_MOVE        - 多個鯨魚在同一時窗同時移動
      EXCHANGE_INFLOW  - 淨流入交易所（看空信號）
      EXCHANGE_OUTFLOW - 淨流出交易所（看多信號）
      DORMANT_WAKE     - 休眠地址甦醒
      BALANCE_DRAIN    - 大額餘額快速流出（>10% 持倉）
      ACCUMULATION     - 地址餘額持續增加（3個快照週期）
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def detect_sync_moves(self, window_minutes: int = SYNC_WINDOW_MINUTES) -> list[dict]:
        """
        檢測同步移動事件：
        在 window_minutes 內，有 MIN_SYNC_COUNT 個以上鯨魚地址同時發生交易
        """
        conn = self._conn()
        c = conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()

        c.execute("""
            SELECT
                strftime('%Y-%m-%dT%H:00:00Z', ts_block) as hour_bucket,
                COUNT(DISTINCT address) as whale_count,
                SUM(value_btc) as total_btc,
                GROUP_CONCAT(DISTINCT address) as addresses
            FROM transactions
            WHERE ts_block >= ?
              AND value_btc >= ?
            GROUP BY hour_bucket
            HAVING whale_count >= ?
            ORDER BY ts_block DESC
        """, (cutoff, WHALE_MOVE_BTC, MIN_SYNC_COUNT))

        rows = c.fetchall()
        conn.close()

        signals = []
        for row in rows:
            hour_bucket, whale_count, total_btc, addresses = row
            signals.append({
                "signal_type":   "SYNC_MOVE",
                "ts":            hour_bucket,
                "strength":      min(1.0, whale_count / 20),  # 20 個鯨魚 = 最強
                "address_count": whale_count,
                "btc_volume":    total_btc,
                "direction":     "neutral",  # 需結合流向判斷
                "description":   f"{whale_count} 個鯨魚在同一小時移動，合計 {total_btc:.1f} BTC",
                "addresses":     addresses.split(",") if addresses else []
            })
        return signals

    def detect_exchange_flows(self) -> dict:
        """
        計算最近 1 小時的交易所淨流量
        正值 = 淨流出（看多）/ 負值 = 淨流入（看空）
        """
        conn = self._conn()
        c = conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        c.execute("""
            SELECT
                direction,
                counterparty,
                SUM(value_btc) as volume
            FROM transactions
            WHERE ts_block >= ?
              AND counterparty IS NOT NULL
              AND value_btc >= ?
            GROUP BY direction, counterparty
        """, (cutoff, 1.0))  # ≥ 1 BTC 涉及已知交易所的交易

        rows = c.fetchall()
        conn.close()

        inflow  = sum(r[2] for r in rows if r[0] == "in")
        outflow = sum(r[2] for r in rows if r[0] == "out")
        by_exchange = {}
        for direction, exchange, volume in rows:
            if exchange not in by_exchange:
                by_exchange[exchange] = {"in": 0, "out": 0}
            by_exchange[exchange][direction] += volume

        return {
            "inflow":      inflow,
            "outflow":     outflow,
            "net":         outflow - inflow,
            "by_exchange": by_exchange,
            "signal":      "EXCHANGE_OUTFLOW" if (outflow - inflow) > 100 else
                           "EXCHANGE_INFLOW"  if (inflow - outflow) > 100 else "NEUTRAL",
            "direction":   "bull" if (outflow - inflow) > 0 else "bear"
        }

    def detect_dormant_wake(self, dormancy_days: int = DORMANCY_DAYS) -> list[dict]:
        """
        檢測休眠地址甦醒：
        last_seen 距今超過 dormancy_days 天，但本次快照有新 TX
        """
        conn = self._conn()
        c = conn.cursor()
        threshold = (datetime.now(timezone.utc) - timedelta(days=dormancy_days)).isoformat()

        # 找出：前一快照 last_seen 早於閾值，但當前快照 last_seen 晚於閾值的地址
        c.execute("""
            SELECT
                a1.address,
                a1.last_seen as prev_last_seen,
                a2.last_seen as curr_last_seen,
                a2.balance_btc
            FROM address_snapshots a1
            JOIN address_snapshots a2 ON a1.address = a2.address
            WHERE a1.ts = (SELECT MAX(ts) FROM address_snapshots WHERE ts < a2.ts)
              AND a1.last_seen <= ?
              AND a2.last_seen > ?
        """, (threshold, threshold))

        rows = c.fetchall()
        conn.close()

        signals = []
        for address, prev_ts, curr_ts, balance_btc in rows:
            label = EXCHANGE_LABELS.get(address, "unknown")
            signals.append({
                "signal_type":  "DORMANT_WAKE",
                "address":      address,
                "label":        label,
                "balance_btc":  balance_btc,
                "dormant_since": prev_ts,
                "strength":     min(1.0, balance_btc / 10000),  # 10k BTC = 最強
                "direction":    "bear",  # 休眠鯨魚甦醒傾向拋售
                "description":  f"休眠 {dormancy_days}+ 天地址甦醒，持倉 {balance_btc:.1f} BTC"
            })
        return signals

    def compute_signal_score(self,
                              exchange_flows: dict,
                              sync_events: list,
                              dormant_wakes: list) -> float:
        """
        綜合信號評分：-1.0（極熊）~ +1.0（極牛）
        權重：
          交易所淨流量  40%
          同步移動方向  30%
          休眠甦醒     20%
          費率緊急度    10%（暫以 0 代替）
        """
        # 交易所流量分
        net_flow = exchange_flows.get("net", 0)
        flow_score = max(-1.0, min(1.0, net_flow / 1000))  # ±1000 BTC = 極端

        # 同步移動（無法判斷方向時用 0）
        sync_score = 0.0
        if sync_events:
            # 若有大量同步移動且主要流出，傾向看多
            sync_score = 0.2 * len(sync_events) / 10

        # 休眠甦醒（預設看空，但需結合流向）
        dormant_score = -0.3 * min(1.0, len(dormant_wakes) / 5)

        score = (0.40 * flow_score +
                 0.30 * sync_score +
                 0.20 * dormant_score)
        return round(max(-1.0, min(1.0, score)), 3)

# ── 主執行流程 ────────────────────────────────────────────────────────────────

def run_hourly_batch():
    """每小時執行一次的完整批量採集"""
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n{'='*60}")
    print(f"[START] GEX Oracle 鯨魚追蹤 | {ts_now}")
    print(f"{'='*60}")

    init_db()
    bc      = BlockchairClient()
    mp      = MempoolClient()
    conn    = sqlite3.connect(DB_PATH)
    c       = conn.cursor()

    # ── Step 1: 抓取 Top 100 地址快照 ────────────────────────────────────────
    print("\n[1/4] 抓取 Top 100 地址快照...")
    addresses = bc.get_top100_addresses()
    print(f"      → 獲得 {len(addresses)} 個地址")

    # 讀取上一次快照（用於計算 delta）
    prev_balances = {}
    c.execute("SELECT address, balance_btc FROM address_snapshots WHERE ts = (SELECT MAX(ts) FROM address_snapshots)")
    for row in c.fetchall():
        prev_balances[row[0]] = row[1]

    for rank, addr in enumerate(addresses, 1):
        label = EXCHANGE_LABELS.get(addr["address"])
        delta = addr["balance_btc"] - prev_balances.get(addr["address"], addr["balance_btc"])
        c.execute("""
            INSERT INTO address_snapshots
              (ts, rank, address, label, balance_btc, tx_count, first_seen, last_seen, balance_delta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ts_now, rank, addr["address"], label,
              addr["balance_btc"], addr["tx_count"],
              addr["first_seen"], addr["last_seen"], delta))

    conn.commit()
    print(f"      → 快照寫入完成，{len(addresses)} 筆")

    # ── Step 2: 抓取每個地址的最近交易 ───────────────────────────────────────
    print(f"\n[2/4] 抓取交易詳情（最多 {TX_PER_ADDRESS} 筆/地址）...")
    tx_total = 0

    for i, addr in enumerate(addresses):
        address = addr["address"]
        print(f"      [{i+1:3d}/{len(addresses)}] {address[:20]}...", end="", flush=True)

        # mempool.space 抓最近交易（快速，無限制）
        mp_txs = mp.get_address_txs(address)
        mp_txids = {tx["txid"] for tx in mp_txs}

        # 從 mempool 數據提取顆粒度
        for tx in mp_txs[:TX_PER_ADDRESS]:
            txid       = tx.get("txid", "")
            block_time = tx.get("status", {}).get("block_time")
            ts_block   = datetime.fromtimestamp(block_time, tz=timezone.utc).isoformat() if block_time else None

            # 計算此地址在這筆交易中的方向和金額
            value_in  = sum(inp.get("prevout", {}).get("value", 0)
                           for inp in tx.get("vin", [])
                           if inp.get("prevout", {}).get("scriptpubkey_address") == address) / 1e8
            value_out = sum(out.get("value", 0)
                           for out in tx.get("vout", [])
                           if out.get("scriptpubkey_address") == address) / 1e8

            # 判斷方向
            if value_in > value_out:
                direction = "out"
                value_btc = value_in - value_out
            else:
                direction = "in"
                value_btc = value_out - value_in

            # 識別對手方（已知交易所）
            all_addrs = (
                [inp.get("prevout", {}).get("scriptpubkey_address", "") for inp in tx.get("vin", [])] +
                [out.get("scriptpubkey_address", "") for out in tx.get("vout", [])]
            )
            counterparty = next(
                (EXCHANGE_LABELS[a] for a in all_addrs if a in EXCHANGE_LABELS),
                None
            )

            try:
                c.execute("""
                    INSERT OR IGNORE INTO transactions
                      (txid, address, ts_block, ts_fetched, direction, value_btc,
                       block_height, fee_sat, input_count, output_count,
                       is_coinbase, counterparty)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    txid, address, ts_block, ts_now,
                    direction, value_btc,
                    tx.get("status", {}).get("block_height"),
                    tx.get("fee"),
                    len(tx.get("vin", [])),
                    len(tx.get("vout", [])),
                    1 if tx.get("vin", [{}])[0].get("is_coinbase") else 0,
                    counterparty
                ))
                tx_total += 1
            except Exception as e:
                pass  # IGNORE = 已存在的 TX 不重複寫入

        print(f" {len(mp_txs)} txs")

    conn.commit()
    print(f"\n      → 共寫入 {tx_total} 筆新交易記錄")

    # ── Step 3: 行為分析 ──────────────────────────────────────────────────────
    print("\n[3/4] 執行行為分析...")
    analyzer = BehaviorAnalyzer()

    sync_events   = analyzer.detect_sync_moves()
    exchange_flows = analyzer.detect_exchange_flows()
    dormant_wakes  = analyzer.detect_dormant_wake()
    signal_score   = analyzer.compute_signal_score(exchange_flows, sync_events, dormant_wakes)

    # 所有信號寫入 DB
    all_signals = sync_events + dormant_wakes
    for sig in all_signals:
        c.execute("""
            INSERT INTO behavior_signals
              (ts, signal_type, strength, address_count, btc_volume, direction, description, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ts_now,
            sig["signal_type"],
            sig.get("strength", 0),
            sig.get("address_count", 1),
            sig.get("btc_volume", 0),
            sig.get("direction", "neutral"),
            sig.get("description", ""),
            json.dumps(sig, ensure_ascii=False)
        ))

    # 小時摘要
    hour_bucket = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    top_signal  = sync_events[0]["signal_type"] if sync_events else \
                  exchange_flows["signal"] if exchange_flows["signal"] != "NEUTRAL" else \
                  "DORMANT_WAKE" if dormant_wakes else "NONE"

    c.execute("""
        INSERT OR REPLACE INTO hourly_summary
          (ts, total_whale_volume, exchange_inflow, exchange_outflow,
           dormant_wake_count, sync_event_count, net_exchange_flow, signal_score, top_signal)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        hour_bucket,
        sum(s.get("btc_volume", 0) for s in sync_events),
        exchange_flows["inflow"],
        exchange_flows["outflow"],
        len(dormant_wakes),
        len(sync_events),
        exchange_flows["net"],
        signal_score,
        top_signal
    ))

    conn.commit()
    conn.close()

    print(f"      → 同步事件: {len(sync_events)}")
    print(f"      → 交易所淨流量: {exchange_flows['net']:+.1f} BTC ({exchange_flows['signal']})")
    print(f"      → 休眠甦醒: {len(dormant_wakes)} 個地址")
    print(f"      → 綜合信號評分: {signal_score:+.3f}")

    # ── Step 4: 輸出 JSON 摘要（給 Dashboard 使用）───────────────────────────
    print("\n[4/4] 輸出 JSON 摘要...")
    summary = {
        "ts":            ts_now,
        "signal_score":  signal_score,
        "exchange_flows": exchange_flows,
        "sync_events":   len(sync_events),
        "dormant_wakes": len(dormant_wakes),
        "top_signal":    top_signal,
        "addresses_tracked": len(addresses)
    }

    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_DIR / "latest_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"      → 寫入 data/latest_summary.json")
    print(f"\n[DONE] 完成 | 耗時數據已記錄 | 信號評分 {signal_score:+.3f}")
    return summary


if __name__ == "__main__":
    run_hourly_batch()
