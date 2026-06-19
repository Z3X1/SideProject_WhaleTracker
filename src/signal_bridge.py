"""
signal_bridge.py
鯨魚信號 → GEX Oracle 統一場論 橋接層 v1.0

將鏈上行為信號轉換為 GEX Oracle 框架可消費的結構化輸入：
  - 更新行為信號權重（統一場方程式 0.28 權重項）
  - 觸發硬性觸發條件評估
  - 輸出 Oracle 可讀的信號摘要
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

DB_PATH      = Path("data/whale.db")
OUTPUT_PATH  = Path("data/oracle_signal.json")

# ── GEX Oracle 統一場方程式權重 ──────────────────────────────────────────────
# P(結算在X) = 0.40×GBM + 0.10×GEX + 0.28×行為信號 + 0.12×貝葉斯 + 0.10×時間衰減
BEHAVIOR_WEIGHT = 0.28

# 鯨魚信號在行為信號分項中的子權重
WHALE_SUB_WEIGHTS = {
    "EXCHANGE_FLOW":  0.45,   # 交易所流量（最直接的方向信號）
    "SYNC_MOVE":      0.30,   # 同步移動（機構協調行為）
    "DORMANT_WAKE":   0.15,   # 休眠甦醒（稀有，高強度）
    "BALANCE_DELTA":  0.10,   # 餘額變化趨勢
}

# 觸發 GEX Oracle 硬性觸發條件的閾值
HARD_TRIGGER_THRESHOLDS = {
    "net_exchange_flow_btc": 1000,   # 淨流量 > 1000 BTC → 觸發
    "sync_whale_count":      15,     # 同時移動 > 15 個鯨魚 → 觸發
    "dormant_wake_count":    3,      # 3 個以上休眠地址甦醒 → 觸發
    "signal_score_change":   0.3,    # 評分 1 小時內變化 > 0.3 → 觸發
}


def load_latest_signals(hours: int = 6) -> dict:
    """從 DB 讀取最近 N 小時的信號摘要"""
    if not DB_PATH.exists():
        return {}

    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    # 小時摘要
    c.execute("""
        SELECT ts, signal_score, exchange_inflow, exchange_outflow,
               net_exchange_flow, sync_event_count, dormant_wake_count, top_signal
        FROM hourly_summary
        WHERE ts >= ?
        ORDER BY ts DESC
    """, (cutoff,))
    summaries = [
        {"ts": r[0], "signal_score": r[1], "exchange_inflow": r[2],
         "exchange_outflow": r[3], "net_exchange_flow": r[4],
         "sync_event_count": r[5], "dormant_wake_count": r[6], "top_signal": r[7]}
        for r in c.fetchall()
    ]

    # 最新餘額快照的 Top 10 地址（按 balance_delta 排序）
    c.execute("""
        SELECT address, label, balance_btc, balance_delta, rank
        FROM address_snapshots
        WHERE ts = (SELECT MAX(ts) FROM address_snapshots)
        ORDER BY ABS(balance_delta) DESC
        LIMIT 10
    """)
    top_movers = [
        {"address": r[0], "label": r[1], "balance_btc": r[2],
         "balance_delta": r[3], "rank": r[4]}
        for r in c.fetchall()
    ]

    # 最近 1 小時大額交易（> 100 BTC）
    c.execute("""
        SELECT txid, address, ts_block, direction, value_btc, counterparty
        FROM transactions
        WHERE ts_fetched >= ?
          AND value_btc >= 100
        ORDER BY value_btc DESC
        LIMIT 20
    """, (cutoff,))
    large_txs = [
        {"txid": r[0], "address": r[1][:16]+"...", "ts_block": r[2],
         "direction": r[3], "value_btc": r[4], "counterparty": r[5]}
        for r in c.fetchall()
    ]

    conn.close()
    return {
        "hourly_summaries": summaries,
        "top_movers":       top_movers,
        "large_txs":        large_txs,
    }


def compute_whale_behavior_score(signals: dict) -> dict:
    """
    計算鯨魚行為在統一場方程式中的貢獻值
    回傳：
      raw_score       : 鯨魚行為原始分 (-1 ~ +1)
      weighted_score  : × BEHAVIOR_WEIGHT 後的值（加入統一場方程式）
      components      : 各子項分解
      trend           : 6h 趨勢方向
      hard_triggers   : 是否觸發任何硬性條件
    """
    summaries = signals.get("hourly_summaries", [])
    large_txs = signals.get("large_txs", [])
    top_movers = signals.get("top_movers", [])

    if not summaries:
        return {
            "raw_score": 0.0, "weighted_score": 0.0,
            "components": {}, "trend": "UNKNOWN",
            "hard_triggers": [], "confidence": 0.0
        }

    latest = summaries[0]

    # ── 子項1：交易所流量分 ───────────────────────────────────────────────────
    net_flow = latest.get("net_exchange_flow", 0)
    # 正值（淨流出）= 看多，±2000 BTC = 滿分
    exchange_score = max(-1.0, min(1.0, net_flow / 2000))

    # ── 子項2：同步移動分 ─────────────────────────────────────────────────────
    sync_count = latest.get("sync_event_count", 0)
    # 同步移動本身無方向，結合流量方向
    sync_direction = 1 if exchange_score >= 0 else -1
    sync_score = sync_direction * min(1.0, sync_count / 10)

    # ── 子項3：休眠甦醒分 ─────────────────────────────────────────────────────
    dormant_count = latest.get("dormant_wake_count", 0)
    # 休眠甦醒默認看空（拋售壓力），但需結合流向
    dormant_score = -min(1.0, dormant_count / 5)

    # ── 子項4：餘額變化分 ─────────────────────────────────────────────────────
    accumulation = sum(m["balance_delta"] for m in top_movers if m["balance_delta"] > 0)
    distribution = sum(abs(m["balance_delta"]) for m in top_movers if m["balance_delta"] < 0)
    net_accumulation = accumulation - distribution
    balance_score = max(-1.0, min(1.0, net_accumulation / 500))

    # ── 加權合成 ─────────────────────────────────────────────────────────────
    raw_score = (
        WHALE_SUB_WEIGHTS["EXCHANGE_FLOW"] * exchange_score +
        WHALE_SUB_WEIGHTS["SYNC_MOVE"]     * sync_score     +
        WHALE_SUB_WEIGHTS["DORMANT_WAKE"]  * dormant_score  +
        WHALE_SUB_WEIGHTS["BALANCE_DELTA"] * balance_score
    )
    raw_score = round(max(-1.0, min(1.0, raw_score)), 3)
    weighted_score = round(raw_score * BEHAVIOR_WEIGHT, 4)

    # ── 6h 趨勢 ───────────────────────────────────────────────────────────────
    if len(summaries) >= 3:
        recent_scores = [s["signal_score"] for s in summaries[:3]]
        if recent_scores[0] > recent_scores[-1] + 0.1:
            trend = "IMPROVING_BULL"
        elif recent_scores[0] < recent_scores[-1] - 0.1:
            trend = "DETERIORATING_BEAR"
        else:
            trend = "STABLE"
    else:
        trend = "INSUFFICIENT_DATA"

    # ── 硬性觸發檢查 ──────────────────────────────────────────────────────────
    hard_triggers = []
    if abs(net_flow) >= HARD_TRIGGER_THRESHOLDS["net_exchange_flow_btc"]:
        hard_triggers.append({
            "type":    "WHALE_EXCHANGE_FLOW",
            "value":   net_flow,
            "message": f"鯨魚交易所淨流量 {net_flow:+.0f} BTC 超過閾值 ±{HARD_TRIGGER_THRESHOLDS['net_exchange_flow_btc']} BTC"
        })
    if dormant_count >= HARD_TRIGGER_THRESHOLDS["dormant_wake_count"]:
        hard_triggers.append({
            "type":    "DORMANT_WAKE_CLUSTER",
            "value":   dormant_count,
            "message": f"{dormant_count} 個休眠鯨魚同時甦醒"
        })
    if len(summaries) >= 2:
        score_change = abs(summaries[0]["signal_score"] - summaries[1]["signal_score"])
        if score_change >= HARD_TRIGGER_THRESHOLDS["signal_score_change"]:
            hard_triggers.append({
                "type":    "SIGNAL_SCORE_SPIKE",
                "value":   score_change,
                "message": f"鯨魚信號評分 1 小時急變 {score_change:.2f}"
            })

    # ── 信心度（數據質量評估）────────────────────────────────────────────────
    confidence = min(1.0, len(summaries) / 6 * 0.5 + len(large_txs) / 10 * 0.5)

    return {
        "ts":             latest["ts"],
        "raw_score":      raw_score,
        "weighted_score": weighted_score,
        "components": {
            "exchange_flow": {"score": exchange_score, "net_btc": net_flow,
                              "weight": WHALE_SUB_WEIGHTS["EXCHANGE_FLOW"]},
            "sync_move":     {"score": sync_score,   "event_count": sync_count,
                              "weight": WHALE_SUB_WEIGHTS["SYNC_MOVE"]},
            "dormant_wake":  {"score": dormant_score, "count": dormant_count,
                              "weight": WHALE_SUB_WEIGHTS["DORMANT_WAKE"]},
            "balance_delta": {"score": balance_score, "net_btc": net_accumulation,
                              "weight": WHALE_SUB_WEIGHTS["BALANCE_DELTA"]},
        },
        "trend":          trend,
        "hard_triggers":  hard_triggers,
        "confidence":     round(confidence, 2),
        "large_tx_count": len(large_txs),
        "largest_tx":     large_txs[0] if large_txs else None,
    }


def generate_oracle_signal() -> dict:
    """主函數：生成 GEX Oracle 可消費的信號包"""
    signals    = load_latest_signals(hours=6)
    whale_signal = compute_whale_behavior_score(signals)

    # 生成 Oracle 可讀的文字摘要（繁體中文）
    score = whale_signal["raw_score"]
    if score > 0.3:
        narrative = f"鯨魚行為偏多：{score:+.3f}，交易所淨流出為主要驅動力"
    elif score < -0.3:
        narrative = f"鯨魚行為偏空：{score:+.3f}，交易所淨流入或休眠甦醒壓制"
    else:
        narrative = f"鯨魚行為中性：{score:+.3f}，無明確方向性信號"

    output = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "version":       "signal_bridge_v1.0",
            "framework":     "GEX Oracle 統一場論",
        },
        "whale_signal":   whale_signal,
        "narrative":      narrative,
        "oracle_input": {
            # 直接插入統一場方程式的值
            "behavior_component": whale_signal["weighted_score"],
            "hard_triggers":      whale_signal["hard_triggers"],
            "confidence":         whale_signal["confidence"],
        },
        "raw_data": {
            "hourly_summaries": signals.get("hourly_summaries", [])[:3],
            "large_txs":        signals.get("large_txs", [])[:5],
            "top_movers":       signals.get("top_movers", [])[:5],
        }
    }

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[signal_bridge] 信號包已輸出 → {OUTPUT_PATH}")
    print(f"  鯨魚行為分: {score:+.3f} | 加權貢獻: {whale_signal['weighted_score']:+.4f}")
    print(f"  趨勢: {whale_signal['trend']} | 信心度: {whale_signal['confidence']:.0%}")
    if whale_signal["hard_triggers"]:
        for trigger in whale_signal["hard_triggers"]:
            print(f"  ⚡ 硬性觸發: {trigger['message']}")

    return output


if __name__ == "__main__":
    generate_oracle_signal()
