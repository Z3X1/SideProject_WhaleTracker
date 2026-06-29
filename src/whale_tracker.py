"""
whale_tracker.py
GEX Oracle — Whale On-Chain Behavior Tracking Engine v2.2
Robust multi-API client with per-request fallback and retry logic.
Primary: mempool.space | Fallback: blockstream.info | Last resort: blockchain.info
"""

import requests, json, time, sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_DIR = Path("data")
DB_PATH  = DATA_DIR / "whale.db"

WHALE_MOVE_BTC      = 100.0
DORMANCY_DAYS       = 30
SYNC_WINDOW_MINUTES = 60
MIN_SYNC_COUNT      = 5

# Known exchange / entity labels
# Sources: CoinCodex, Cointelegraph, BitInfoCharts (verified 2025-2026)
EXCHANGE_LABELS = {
    # Binance
    "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo":                                         "Binance",
    "3M219KR5vEneNb47ewrPfWyb5jQ2DjxRP6":                                         "Binance",
    "3LYJfcfHcvtWqWQx5rXNG7a4JKgmZP5aF5":                                         "Binance",
    # Robinhood
    "bc1ql49ydapnjafl5t2cp9zqpjwe6pdgmxy98859v2":                                 "Robinhood",
    # Bitfinex
    "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97":            "Bitfinex",
    "bc1qd4ysezhmypwty5dnw7c8nqy5h5nxg0xqsvaefd0qn5kq32vwnwqqgv4rzr":            "Bitfinex",
    "3LCGsSmfr24demGvriN4e3ft8wEcDuHFqh":                                         "Bitfinex",
    "1FzWLkAahHooV3kzTgyx6qsswXJ6sCXkSR":                                         "Bitfinex",
    "385cR5DM96n1HvBDMDLaxRErEQPGidsJHo":                                         "Bitfinex",
    # Tether
    "bc1qjasf9z3h7w3jspkhtgatgpyvvzgpa2wwd2lr0eh5tx44reyn2k7sfc27a4":            "Tether",
    # FBI / Gov confiscated
    "bc1qazcm763858nkj2dj986etajv6wquslv8uxwczt":                                 "FBI_Bitfinex",
    "bc1qa5wkgaew2dkv56kfvj49j0av5nml45x9ek9hz6":                                 "FBI_SilkRoad",
    # Mt.Gox hacker
    "1FeexV6bAHb8ybZjqQMjJrcCrHGW9sb6uF":                                         "MtGox_Hacker",
    # Coinbase
    "1P5ZEDWTKTFGxQjZphgWPQUpe554WKDfHQ":                                         "Coinbase",
    "3Cbq7aT1tY8kMxWLbitaG7yT6bPbKChq64":                                         "Coinbase",
    # Kraken
    "3E5L9wBBdFaHRzBkJQrqVCrFMWGqVNGeLH":                                         "Kraken",
    # OKX
    "1AC4fMwgY8j9onSbXEWeH6Zan8QGMSdmtA":                                         "OKX",
    "3DrVotri9MEd2rZMrFJLwBe4mBntxBvhzX":                                         "OKX",
    # Huobi
    "1Kr6QSydW9bFQG1mXiPNNu6WpJGmUa9i1g":                                         "Huobi",
    # Gemini
    "38DN99T4Nz56eBzCKJFkgdekb5NdGzYxWf":                                         "Gemini",
    # Satoshi-era dormant
    "1HQ3Go3ggs8pFnXuHVHRytPCq5fGG8Hbhx":                                         "Satoshi_Dormant",
    "12tkqA9xSoowkzoERHMWNKsTey55YEBqkv":                                          "Satoshi_Dormant",
}

# Verified whale address list — 41 addresses, all format-validated, all confirmed
# active or large-balance as of 2025-2026.
# Sources: CoinCodex rich list (Mar 2026), Cointelegraph (Jul 2025), BitInfoCharts
WHALE_ADDRESSES = [
    # ── Top 10 by confirmed BTC balance (2026) ────────────────────────────────
    "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo",                                  # Binance #1        248,598 BTC
    "3M219KR5vEneNb47ewrPfWyb5jQ2DjxRP6",                                  # Binance #2        171,730 BTC
    "bc1ql49ydapnjafl5t2cp9zqpjwe6pdgmxy98859v2",                          # Robinhood         140,575 BTC
    "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97",     # Bitfinex          130,010 BTC
    "bc1qjasf9z3h7w3jspkhtgatgpyvvzgpa2wwd2lr0eh5tx44reyn2k7sfc27a4",     # Tether             96,185 BTC
    "bc1qazcm763858nkj2dj986etajv6wquslv8uxwczt",                          # FBI/Bitfinex       94,643 BTC
    "bc1qd4ysezhmypwty5dnw7c8nqy5h5nxg0xqsvaefd0qn5kq32vwnwqqgv4rzr",     # Bitfinex           91,850 BTC
    "1FeexV6bAHb8ybZjqQMjJrcCrHGW9sb6uF",                                  # MtGox Hacker       79,957 BTC
    "bc1q8yj0herd4r4yxszw3nkfvt53433thk0f5qst4g",                          # Unknown            78,317 BTC
    "bc1qa5wkgaew2dkv56kfvj49j0av5nml45x9ek9hz6",                          # FBI/SilkRoad       69,370 BTC
    # ── Additional confirmed exchange cold wallets ────────────────────────────
    "3LYJfcfHcvtWqWQx5rXNG7a4JKgmZP5aF5",                                  # Binance #3
    "1P5ZEDWTKTFGxQjZphgWPQUpe554WKDfHQ",                                  # Coinbase #1
    "3Cbq7aT1tY8kMxWLbitaG7yT6bPbKChq64",                                  # Coinbase #2
    "3E5L9wBBdFaHRzBkJQrqVCrFMWGqVNGeLH",                                  # Kraken
    "3LCGsSmfr24demGvriN4e3ft8wEcDuHFqh",                                  # Bitfinex legacy #1
    "1FzWLkAahHooV3kzTgyx6qsswXJ6sCXkSR",                                  # Bitfinex legacy #2
    "385cR5DM96n1HvBDMDLaxRErEQPGidsJHo",                                  # Bitfinex legacy #3
    "1AC4fMwgY8j9onSbXEWeH6Zan8QGMSdmtA",                                  # OKX #1
    "3DrVotri9MEd2rZMrFJLwBe4mBntxBvhzX",                                  # OKX #2
    "1Kr6QSydW9bFQG1mXiPNNu6WpJGmUa9i1g",                                  # Huobi
    "38DN99T4Nz56eBzCKJFkgdekb5NdGzYxWf",                                  # Gemini
    # ── Satoshi-era dormant ───────────────────────────────────────────────────
    "1HQ3Go3ggs8pFnXuHVHRytPCq5fGG8Hbhx",
    "12tkqA9xSoowkzoERHMWNKsTey55YEBqkv",
    # ── Individual whales (verified active balances via BitInfoCharts) ────────
    "1LdRcdxfbSnmCYYNdeYpUnztiYzVfBEQeC",
    "1GR9qNz7zgtaW5HwwVpEJWMnGWhsbsieCG",
    "1KFHE7w8BhaENAswwryaoccDb6qcT6DbYY",
    "34HgHatoLRnKaLMpVnMVg4ZJkQkfgouqKs",
    "1NDyJtNTjmwk5xPNhjgAMu4HDHigtobu1s",
    "1NxaBCFQwejSZbQfWcYNwgqML5wWoE3rK4",
    "1KDx1hpNJkHFj9hFZz7aFWDKarCGEMuNiC",
    "14VzHt1MU76xETPJnXW28c7QmGLGFuRkgd",
    "1PMycacnJaSqwwJqjawXBErnLsZ7RkXUAs",
    "1EXoDusjGwvnjZUyKkxZ4UHEf77z6A5S4U",
    "1JwSSubhmg6iPtRjtyqhUYYH7bZg3Lfy1T",
    "19vkiEajfhuZ8bs8Zu2jgmC6oMjR1PZYQi",
    "1MXwcBbLnqqMRYQNYhaqDHDHiLNHkRGEQq",
    "1DBaumZxUkk4im2oENLPW7woaiJTwkYPMd",
    "1DkyBEKt5S2GDtv7aQw6rQepAvnsRyHoYM",
    "3EktnHQD7RiAE6uzMj2ZifT9YgRrkSgzQX",
    "1PSSGeFHDnKNxiEyFrD1wcEaHr9hrQDDWc",
    "1JCe8z4jJVNXSjohjqo2ejAqZXuovjj3PK",
]

# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS address_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        rank INTEGER NOT NULL,
        address TEXT NOT NULL,
        label TEXT,
        balance_btc REAL NOT NULL,
        balance_btc_total REAL,
        tx_count INTEGER NOT NULL,
        tx_count_confirmed INTEGER,
        first_seen TEXT,
        last_seen TEXT,
        balance_delta REAL DEFAULT 0,
        source TEXT)""")  
    c.execute("""CREATE TABLE IF NOT EXISTS transactions (
        txid TEXT NOT NULL, address TEXT NOT NULL, ts_block TEXT,
        ts_fetched TEXT NOT NULL, direction TEXT NOT NULL, value_btc REAL NOT NULL,
        block_height INTEGER, fee_sat INTEGER, input_count INTEGER,
        output_count INTEGER, is_coinbase INTEGER DEFAULT 0, counterparty TEXT,
        PRIMARY KEY (txid, address))""")
    c.execute("""CREATE TABLE IF NOT EXISTS behavior_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
        signal_type TEXT NOT NULL, strength REAL NOT NULL,
        address_count INTEGER, btc_volume REAL, direction TEXT,
        description TEXT, raw_json TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS hourly_summary (
        ts TEXT PRIMARY KEY, total_whale_volume REAL, exchange_inflow REAL,
        exchange_outflow REAL, dormant_wake_count INTEGER, sync_event_count INTEGER,
        net_exchange_flow REAL, signal_score REAL, top_signal TEXT)""")
    # Migration: add new columns if upgrading from older schema
    migrations = [
        "ALTER TABLE address_snapshots ADD COLUMN balance_btc_total REAL",
        "ALTER TABLE address_snapshots ADD COLUMN tx_count_confirmed INTEGER",
        "ALTER TABLE address_snapshots ADD COLUMN source TEXT",
    ]
    for sql in migrations:
        try:
            c.execute(sql)
        except sqlite3.OperationalError:
            pass  # Column already exists — skip

    conn.commit()
    conn.close()
    print("[DB] Initialized")

# ── Robust Multi-API Client ───────────────────────────────────────────────────
class ChainClient:
    """
    Per-request fallback chain with retry logic.
    Each individual request tries all available APIs before giving up.

    Priority order:
      1. mempool.space    (esplora format, primary)
      2. blockstream.info (esplora format, fallback #1)
      3. blockchain.info  (different format, fallback #2)

    Key improvement over v2.1:
      - No longer bails out globally if one host fails probe
      - Each address request independently tries all hosts
      - Exponential backoff on rate-limit (HTTP 429)
      - Success/failure stats printed at end of batch
    """

    ESPLORA_HOSTS = [
        "https://mempool.space",
        "https://blockstream.info",
    ]
    MAX_RETRIES  = 2
    RETRY_DELAYS = [1, 3]      # seconds between retries per host (reduced to avoid timeout bloat)

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "GEX-Oracle-WhaleTracker/2.2"})
        self.stats = {"success": 0, "failed": 0, "fallback_used": 0}

    def _get_esplora(self, path: str) -> dict | list | None:
        """
        Try each esplora host in order with retries.
        Returns parsed JSON on first success, None if all fail.
        """
        for host in self.ESPLORA_HOSTS:
            for attempt, delay in enumerate(self.RETRY_DELAYS, 1):
                try:
                    r = self.session.get(f"{host}{path}", timeout=12)
                    if r.status_code == 200:
                        return r.json()
                    elif r.status_code == 429:
                        # Rate limited — wait and retry same host
                        wait = delay * 2
                        print(f"  [rate-limit] {host} → sleeping {wait}s (attempt {attempt})")
                        time.sleep(wait)
                        continue
                    elif r.status_code in (404, 400):
                        # Invalid address — skip this host entirely
                        break
                    else:
                        time.sleep(delay)
                except requests.exceptions.Timeout:
                    print(f"  [timeout] {host}{path[:40]} (attempt {attempt})")
                    time.sleep(delay)
                except Exception as e:
                    time.sleep(delay)
            # Small pause between hosts
            time.sleep(0.1)
        return None

    def _get_blockchain_info(self, address: str, limit: int = 1) -> dict | None:
        """blockchain.info fallback for address info + txs."""
        for attempt, delay in enumerate(self.RETRY_DELAYS, 1):
            try:
                r = self.session.get(
                    f"https://blockchain.info/rawaddr/{address}",
                    params={"limit": limit}, timeout=12)
                if r.status_code == 200:
                    return r.json()
                elif r.status_code == 429:
                    time.sleep(delay * 2)
                    continue
                else:
                    time.sleep(delay)
            except Exception:
                time.sleep(delay)
        return None

    def get_address_info(self, address: str) -> dict:
        """
        Returns balance (BTC) and total TX count. Tries all APIs.

        Handles all address types:
          P2PKH  (1...)    — legacy
          P2SH   (3...)    — script hash
          P2WPKH (bc1q..42) — native segwit
          P2WSH  (bc1q..62) — native segwit script (multisig/exchange cold wallets)
          P2TR   (bc1p...)  — taproot
        All share the same esplora endpoint; chain_stats structure is identical.
        """
        # Try esplora hosts first
        data = self._get_esplora(f"/api/address/{address}")
        if data and isinstance(data, dict):
            cs = data.get("chain_stats", {})
            ms = data.get("mempool_stats", {})
            # chain_stats present = valid response for ALL address types
            if "funded_txo_sum" in cs or "tx_count" in cs:
                confirmed_bal = (cs.get("funded_txo_sum", 0) - cs.get("spent_txo_sum", 0)) / 1e8
                # Include unconfirmed mempool balance
                mempool_delta = (ms.get("funded_txo_sum", 0) - ms.get("spent_txo_sum", 0)) / 1e8
                self.stats["success"] += 1
                return {
                    "balance_btc":        confirmed_bal,
                    "balance_btc_total":  confirmed_bal + mempool_delta,
                    "tx_count":           cs.get("tx_count", 0) + ms.get("tx_count", 0),
                    "tx_count_confirmed": cs.get("tx_count", 0),
                    "source":             "esplora",
                }

        # blockchain.info fallback (supports P2WSH via rawaddr endpoint)
        data = self._get_blockchain_info(address, limit=1)
        if data and isinstance(data, dict) and "final_balance" in data:
            self.stats["fallback_used"] += 1
            return {
                "balance_btc":        data.get("final_balance", 0) / 1e8,
                "balance_btc_total":  data.get("final_balance", 0) / 1e8,
                "tx_count":           data.get("n_tx", 0),
                "tx_count_confirmed": data.get("n_tx", 0),
                "source":             "blockchain.info",
            }

        self.stats["failed"] += 1
        return {}

    def get_address_txs(self, address: str, limit: int = 50) -> list:
        """
        Returns the most recent N transactions with full per-tx granularity:
          - txid, block_time, block_height
          - direction (in/out relative to this address)
          - net value_btc for this address
          - fee_sat, input_count, output_count
          - counterparty (known exchange label if present)
        """
        # Try esplora hosts
        data = self._get_esplora(f"/api/address/{address}/txs")
        if data:
            return data[:limit]

        # blockchain.info fallback — convert to esplora format
        data = self._get_blockchain_info(address, limit=limit)
        if data:
            return self._convert_blockchain_txs(data.get("txs", []))

        return []

    def _convert_blockchain_txs(self, raw_txs: list) -> list:
        """Normalize blockchain.info tx format to esplora format."""
        result = []
        for tx in raw_txs:
            vin  = [{"prevout": {
                        "scriptpubkey_address": inp.get("prev_out", {}).get("addr", ""),
                        "value":                inp.get("prev_out", {}).get("value", 0),
                    }} for inp in tx.get("inputs", [])]
            vout = [{"scriptpubkey_address": out.get("addr", ""),
                     "value":                out.get("value", 0)}
                    for out in tx.get("out", [])]
            result.append({
                "txid": tx.get("hash", ""),
                "fee":  tx.get("fee", 0),
                "status": {
                    "confirmed":    tx.get("block_height") is not None,
                    "block_height": tx.get("block_height"),
                    "block_time":   tx.get("time"),
                },
                "vin":  vin,
                "vout": vout,
            })
        return result

    def print_stats(self):
        total = self.stats["success"] + self.stats["failed"]
        print(f"  [API stats] success={self.stats['success']}/{total} | "
              f"fallback={self.stats['fallback_used']} | "
              f"failed={self.stats['failed']}")

# ── Behavior Analysis Engine ──────────────────────────────────────────────────
class BehaviorAnalyzer:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH)

    def close(self):
        self.conn.close()

    def detect_sync_moves(self) -> list:
        c      = self.conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=SYNC_WINDOW_MINUTES)).isoformat()
        c.execute("""
            SELECT strftime('%Y-%m-%dT%H:00:00Z', ts_block) AS hb,
                   COUNT(DISTINCT address) AS n,
                   SUM(value_btc) AS vol,
                   GROUP_CONCAT(DISTINCT address) AS addrs
            FROM transactions
            WHERE ts_block >= ? AND value_btc >= ?
            GROUP BY hb HAVING n >= ?
            ORDER BY ts_block DESC
        """, (cutoff, WHALE_MOVE_BTC, MIN_SYNC_COUNT))
        return [{
            "signal_type":   "SYNC_MOVE",
            "ts":            hb,
            "strength":      min(1.0, n / 20),
            "address_count": n,
            "btc_volume":    vol or 0,
            "direction":     "neutral",
            "description":   f"{n} whales moved in the same hour — total {(vol or 0):.1f} BTC",
            "addresses":     (addrs or "").split(","),
        } for hb, n, vol, addrs in c.fetchall()]

    def detect_exchange_flows(self) -> dict:
        c      = self.conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        c.execute("""
            SELECT direction, counterparty, SUM(value_btc)
            FROM transactions
            WHERE ts_block >= ? AND counterparty IS NOT NULL AND value_btc >= 1.0
            GROUP BY direction, counterparty
        """, (cutoff,))
        rows    = c.fetchall()
        inflow  = sum(r[2] for r in rows if r[0] == "in")
        outflow = sum(r[2] for r in rows if r[0] == "out")
        net     = outflow - inflow
        by_ex   = {}
        for d, ex, vol in rows:
            if ex not in by_ex:
                by_ex[ex] = {"in": 0, "out": 0}
            by_ex[ex][d] += vol
        # 語義定義（極致顆粒度）：
        # inflow  = 鯨魚「收到」BTC（來自交易所 = 提幣 = 看漲）
        # outflow = 鯨魚「發出」BTC（去往交易所 = 入金/拋售 = 看跌）
        # net = outflow - inflow
        # net > 0 → 鯨魚淨發出 → 去往交易所 → 拋壓 → BEARISH
        # net < 0 → 鯨魚淨收到 → 來自交易所 → 提幣 → BULLISH
        whale_net_direction = "bear" if net > 0 else ("bull" if net < 0 else "neutral")
        return {
            "inflow":      round(inflow,  2),   # 鯨魚收到 BTC（提幣）
            "outflow":     round(outflow, 2),   # 鯨魚發出 BTC（入金/拋售）
            "net":         round(net,     2),   # outflow - inflow，正=拋壓
            "by_exchange": by_ex,
            "signal":      "EXCHANGE_INFLOW"   if net > 100  else   # 鯨魚發出→交易所收到
                           "EXCHANGE_OUTFLOW"  if net < -100 else   # 交易所發出→鯨魚收到
                           "NEUTRAL",
            "direction":   whale_net_direction,
        }

    def detect_dormant_wake(self) -> list:
        c         = self.conn.cursor()
        threshold = (datetime.now(timezone.utc) - timedelta(days=DORMANCY_DAYS)).isoformat()
        c.execute("""
            SELECT a1.address, a1.last_seen, a2.last_seen, a2.balance_btc
            FROM address_snapshots a1
            JOIN address_snapshots a2 ON a1.address = a2.address
            WHERE a1.ts = (SELECT MAX(ts) FROM address_snapshots WHERE ts < a2.ts)
              AND a1.last_seen <= ? AND a2.last_seen > ?
        """, (threshold, threshold))
        return [{
            "signal_type": "DORMANT_WAKE",
            "address":     address,
            "label":       EXCHANGE_LABELS.get(address, "unknown"),
            "balance_btc": bal,
            "strength":    min(1.0, bal / 10000),
            "direction":   "bear",
            "description": f"Address dormant {DORMANCY_DAYS}+ days just moved — balance {bal:.1f} BTC",
        } for address, _, _, bal in c.fetchall()]

    def compute_signal_score(self, ef: dict, se: list, dw: list) -> float:
        """
        合成信號分數 [-1, +1]。
        三個子信號各自歸一化到 [-1, +1] 後加權求和，總權重 = 1.0。
          - exchange_flow (0.55): net BTC 流出 > 0 → bull（提幣看漲）
          - sync_move     (0.30): 多鯨同步移動 → 方向不明，取弱正（謹慎看漲）
          - dormant_wake  (0.15): 休眠鯨魚甦醒 → 偏空（拋售風險）
        """
        ex_signal   = max(-1.0, min(1.0, ef.get("net", 0) / 1000))
        sync_signal = min(1.0, len(se) / 10) * 0.5   # 同步移動方向不明，衰減 0.5
        dorm_signal = -min(1.0, len(dw) / 5)          # 休眠甦醒 = 負信號
        score = (0.55 * ex_signal +
                 0.30 * sync_signal +
                 0.15 * dorm_signal)
        return round(max(-1.0, min(1.0, score)), 3)

# ── Main Hourly Batch ─────────────────────────────────────────────────────────
def run_hourly_batch():
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n{'='*60}")
    print(f"[START] GEX Oracle Whale Tracker v2.2 | {ts_now}")
    print(f"{'='*60}")

    init_db()
    client = ChainClient()
    conn   = sqlite3.connect(DB_PATH)
    c      = conn.cursor()

    # Load previous balances for delta calculation
    prev_balances = {}
    c.execute("""SELECT address, balance_btc FROM address_snapshots
                 WHERE ts = (SELECT MAX(ts) FROM address_snapshots)""")
    for row in c.fetchall():
        prev_balances[row[0]] = row[1]

    # ── Step 1: Address Snapshots ─────────────────────────────────────────────
    print(f"\n[1/4] Address snapshots ({len(WHALE_ADDRESSES)} addresses)...")
    snapshot_count = 0
    for rank, address in enumerate(WHALE_ADDRESSES, 1):
        info = client.get_address_info(address)
        if not info:
            continue

        balance_btc = info["balance_btc"]
        delta       = balance_btc - prev_balances.get(address, balance_btc)
        label       = EXCHANGE_LABELS.get(address)

        c.execute("""INSERT INTO address_snapshots
            (ts, rank, address, label, balance_btc, balance_btc_total,
             tx_count, tx_count_confirmed, balance_delta, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts_now, rank, address, label,
             info["balance_btc"], info.get("balance_btc_total", info["balance_btc"]),
             info["tx_count"], info.get("tx_count_confirmed", info["tx_count"]),
             delta, info.get("source", "unknown")))
        snapshot_count += 1

        if rank % 10 == 0:
            print(f"  → {rank}/{len(WHALE_ADDRESSES)} snapshots done")
        time.sleep(0.8)   # 0.8s between requests → ~75 req/min, well under limits

    conn.commit()
    client.print_stats()
    print(f"  → Snapshots complete: {snapshot_count}/{len(WHALE_ADDRESSES)}")

    # Reset stats for TX phase
    client.stats = {"success": 0, "failed": 0, "fallback_used": 0}

    # ── Step 2: Transaction Granularity ───────────────────────────────────────
    print(f"\n[2/4] Transaction collection (50 txs/address)...")
    print(f"      Granularity per tx: txid | direction | amount (BTC) | block time | counterparty | fee | input/output count")
    tx_total = 0

    for i, address in enumerate(WHALE_ADDRESSES, 1):
        txs = client.get_address_txs(address, limit=50)

        for tx in txs:
            txid       = tx.get("txid", "")
            status     = tx.get("status", {})
            block_time = status.get("block_time")
            ts_block   = (datetime.fromtimestamp(block_time, tz=timezone.utc).isoformat()
                          if block_time else None)

            # Compute net flow direction for this specific address
            value_in  = sum(
                inp.get("prevout", {}).get("value", 0)
                for inp in tx.get("vin", [])
                if inp.get("prevout", {}).get("scriptpubkey_address") == address
            ) / 1e8
            value_out = sum(
                out.get("value", 0)
                for out in tx.get("vout", [])
                if out.get("scriptpubkey_address") == address
            ) / 1e8

            direction = "out" if value_in > value_out else "in"
            value_btc = abs(value_in - value_out)

            # Identify known exchange counterparty
            all_addrs    = (
                [inp.get("prevout", {}).get("scriptpubkey_address", "") for inp in tx.get("vin",  [])] +
                [out.get("scriptpubkey_address", "")                    for out in tx.get("vout", [])]
            )
            counterparty = next(
                (EXCHANGE_LABELS[a] for a in all_addrs if a in EXCHANGE_LABELS and a != address),
                None)

            try:
                c.execute("""INSERT OR IGNORE INTO transactions
                    (txid, address, ts_block, ts_fetched, direction, value_btc,
                     block_height, fee_sat, input_count, output_count,
                     is_coinbase, counterparty)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (txid, address, ts_block, ts_now, direction, value_btc,
                     status.get("block_height"), tx.get("fee"),
                     len(tx.get("vin", [])), len(tx.get("vout", [])),
                     1 if tx.get("vin", [{}])[0].get("is_coinbase") else 0,
                     counterparty))
                tx_total += 1
            except Exception:
                pass

        if i % 20 == 0:
            print(f"  → {i}/{len(WHALE_ADDRESSES)} addresses done ({tx_total} txs total)")
        time.sleep(0.8)

    conn.commit()
    client.print_stats()
    print(f"  → Transaction collection complete: {tx_total} new records")

    # ── Step 3: Behavior Analysis ──────────────────────────────────────────────
    print("\n[3/4] Behavior analysis...")
    az = BehaviorAnalyzer()
    se = az.detect_sync_moves()
    ef = az.detect_exchange_flows()
    dw = az.detect_dormant_wake()
    sc = az.compute_signal_score(ef, se, dw)
    az.close()

    for sig in se + dw:
        c.execute("""INSERT INTO behavior_signals
            (ts, signal_type, strength, address_count, btc_volume,
             direction, description, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts_now, sig["signal_type"], sig.get("strength", 0),
             sig.get("address_count", 1), sig.get("btc_volume", 0),
             sig.get("direction", "neutral"), sig.get("description", ""),
             json.dumps(sig)))

    hb  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    top = (se[0]["signal_type"] if se else
           ef["signal"]         if ef["signal"] != "NEUTRAL" else
           "DORMANT_WAKE"       if dw else "NONE")

    c.execute("""INSERT OR REPLACE INTO hourly_summary
        (ts, total_whale_volume, exchange_inflow, exchange_outflow,
         dormant_wake_count, sync_event_count, net_exchange_flow,
         signal_score, top_signal)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (hb, sum(s.get("btc_volume", 0) for s in se),
         ef["inflow"], ef["outflow"], len(dw), len(se), ef["net"], sc, top))

    conn.commit()
    conn.close()

    print(f"  → Sync events: {len(se)} | Exchange net flow: {ef['net']:+.1f} BTC | Dormant wakes: {len(dw)}")
    print(f"  → Composite signal score: {sc:+.3f}")

    # ── Step 4: JSON Output ────────────────────────────────────────────────────
    print("\n[4/4] Writing output...")
    summary = {
        "ts":                ts_now,
        "signal_score":      sc,
        "exchange_flows":    ef,
        "sync_events":       len(se),
        "dormant_wakes":     len(dw),
        "top_signal":        top,
        "addresses_tracked": snapshot_count,
        "tx_records":        tx_total,
    }
    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_DIR / "latest_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"  → data/latest_summary.json written")
    print(f"\n[DONE] addresses={snapshot_count} | txs={tx_total} | score={sc:+.3f}")
    return summary


if __name__ == "__main__":
    run_hourly_batch()
