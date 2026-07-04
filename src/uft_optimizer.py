#!/usr/bin/env python3
"""
UFT Dynamic Weight Optimizer v3.0
──────────────────────────────────
迭代學習架構：
  Layer 1：梯度下降（原有，修正double-weight bug）
  Layer 2：信號貢獻度回測（哪個信號在哪種Regime下最有預測力）
  Layer 3：Regime分層權重（POS/NEG各自一套最優權重）
  Layer 4：貝葉斯先驗更新（每次結算後更新各分量的先驗分布）
  Layer 5：滾動視窗衰減（越近的結算誤差權重越高）

收斂條件：
  平均誤差 < 0.3σ 且連續3次優化改善 < 1% → 收斂，凍結權重
  重大市場結構變化（Regime切換/DVOL突變）→ 解凍重學

記錄結構擴充：
  - weights_used（記錄產生預測時的實際權重，用於正確反推 raw_center）
  - signal_snapshot（完整信號值快照，用於信號貢獻度分析）
  - regime_at_prediction（預測時的 Regime）
"""

import json, os, math, requests, re
from datetime import datetime, date, timezone

DEFAULT_WEIGHTS = {
    "gbm": 0.30, "gex": 0.18,
    "behavior": 0.28, "bayesian": 0.12, "timedecay": 0.12
}  # v3.0 baseline — must sum to 1.00

# Regime分層：POS/NEG各自維護一套權重
DEFAULT_REGIME_WEIGHTS = {
    "POS": {"gbm": 0.28, "gex": 0.20, "behavior": 0.28, "bayesian": 0.12, "timedecay": 0.12},
    "NEG": {"gbm": 0.25, "gex": 0.15, "behavior": 0.32, "bayesian": 0.16, "timedecay": 0.12},
}

LOG_PATH = "data/settlement_log.json"
KEYS = ["gbm", "gex", "behavior", "bayesian", "timedecay"]

MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
          "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


# ── 工具函數 ──────────────────────────────────────────────────

def _norm(w_dict):
    """歸一化 dict，sum→1.00，修正浮點誤差"""
    total = sum(w_dict.values())
    if total == 0: return DEFAULT_WEIGHTS.copy()
    w = {k: max(0.02, v / total) for k, v in w_dict.items()}
    total2 = sum(w.values())
    w = {k: v / total2 for k, v in w.items()}
    # 精確修正（最大項補差）
    diff = 1.0 - sum(w.values())
    if abs(diff) > 1e-9:
        max_k = max(w, key=w.get)
        w[max_k] = round(w[max_k] + diff, 6)
    return {k: round(w[k], 5) for k in KEYS}

def _raw_center(record, key, fallback_w):
    """還原 raw_center = component[key] / weight_used[key]（消除 double-weight）"""
    c = record.get("components", {})
    w_orig = record.get("weights_used", fallback_w)
    w_o = float(w_orig.get(key, fallback_w.get(key, 0.01)))
    return c.get(key, 0) / w_o if w_o != 0 else 0

def _predict(weights_list, record, fallback_w):
    """用 weights_list（list）對單筆 record 計算 UFT 預測值"""
    return sum(weights_list[i] * _raw_center(record, KEYS[i], fallback_w)
               for i in range(len(KEYS)))

def _recent_weight(record_idx, total, half_life=10):
    """滾動視窗衰減：越近的樣本權重越高，half_life=10條記錄"""
    age = total - 1 - record_idx  # 0=最新, 越大越舊
    return math.exp(-0.693 * age / half_life)  # 0.693 = ln(2)

def _cluster_weights(samples):
    """
    週期聚類權重：每個結算週期（expiry）總權重相等，
    避免單週期大量快照主導損失函數。
    週期內：T 值大的樣本權重高（T-7d 預測比 T-0d 更有資訊量，
    T-0d 時 Spot≈結算價，預測無難度）。
    返回與 samples 等長的權重 list（總和=1）。
    """
    from collections import defaultdict
    by_cycle = defaultdict(list)
    for i, r in enumerate(samples):
        by_cycle[r["expiry"]].append(i)
    n_cycles = len(by_cycle)
    weights = [0.0] * len(samples)
    for expiry, idxs in by_cycle.items():
        cycle_total = 1.0 / n_cycles
        # 週期內按 t_days 加權（T大者權重高）；缺 t_days 用均分
        t_vals = []
        for i in idxs:
            td = samples[i].get("t_days_at_record")
            t_vals.append(max(0.5, td) if td is not None else 1.0)
        t_sum = sum(t_vals)
        for j, i in enumerate(idxs):
            weights[i] = cycle_total * (t_vals[j] / t_sum)
    return weights

def load_log():
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH) as f:
            log = json.load(f)
        # 向後相容：補充新欄位
        if "regime_weights" not in log:
            log["regime_weights"] = DEFAULT_REGIME_WEIGHTS.copy()
        if "signal_contributions" not in log:
            log["signal_contributions"] = {}
        if "convergence" not in log:
            log["convergence"] = {"converged": False, "frozen": False, "consecutive_no_improve": 0}
        if "optimization_history" not in log:
            log["optimization_history"] = []
        return log
    return {
        "records": [],
        "current_weights": DEFAULT_WEIGHTS.copy(),
        "regime_weights": DEFAULT_REGIME_WEIGHTS.copy(),
        "signal_contributions": {},   # {signal_name: {accuracy, avg_contribution}}
        "weight_history": [],
        "optimization_history": [],
        "last_optimized": None,
        "convergence": {
            "converged": False,
            "frozen": False,
            "consecutive_no_improve": 0,
            "avg_error_sigma_history": [],
        }
    }

def save_log(log):
    os.makedirs("data", exist_ok=True)
    with open(LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


# ── Layer 1：梯度下降（帶滾動衰減）────────────────────────────

def _gradient_descent(completed, initial_w_dict, iterations=800, lr=0.0008, regime_filter=None):
    """
    梯度下降最小化加權 MSE。
    滾動衰減：越近的樣本損失權重越高（half_life=10）。
    regime_filter: 'POS'/'NEG'/None（None=全量）
    """
    samples = [r for r in completed
               if (regime_filter is None or r.get("regime_at_prediction") == regime_filter)]
    if len(samples) < 5:
        return initial_w_dict  # 樣本不足，不優化

    fallback_w = initial_w_dict
    w = [initial_w_dict.get(k, 0.2) for k in KEYS]
    n = len(samples)

    # 週期聚類權重（每週期等權，週期內按T值分配）× 時間衰減
    cluster_w = _cluster_weights(samples)
    time_w = [_recent_weight(i, n) for i in range(n)]
    decay = [c * t for c, t in zip(cluster_w, time_w)]
    _ds = sum(decay)
    decay = [x / _ds for x in decay]
    decay_sum = 1.0

    def weighted_mse(weights):
        total = 0.0
        for i, r in enumerate(samples):
            pred = _predict(weights, r, fallback_w)
            err = (pred - r["actual_settlement"]) ** 2
            total += decay[i] * err
        return total / decay_sum

    best_err = weighted_mse(w)
    best_w = w.copy()
    no_improve = 0

    for it in range(iterations):
        grad = [0.0] * 5
        for i, r in enumerate(samples):
            rc = [_raw_center(r, k, fallback_w) for k in KEYS]
            pred = sum(w[j] * rc[j] for j in range(5))
            diff = 2 * (pred - r["actual_settlement"]) * decay[i] / decay_sum
            for j in range(5):
                grad[j] += diff * rc[j]

        # 自適應學習率（誤差大時步長大）
        adaptive_lr = lr * (1 + best_err / 1e8)
        w_new = [max(0.02, w[j] - adaptive_lr * grad[j]) for j in range(5)]
        w_new_norm = [x / sum(w_new) for x in w_new]

        new_err = weighted_mse(w_new_norm)
        if new_err < best_err:
            best_err = new_err
            best_w = w_new_norm.copy()
            no_improve = 0
        else:
            no_improve += 1
            if no_improve > 80:
                lr *= 0.5  # 學習率衰減
                no_improve = 0
        w = w_new_norm

    result = {KEYS[i]: best_w[i] for i in range(5)}
    return _norm(result)


# ── Layer 2：信號貢獻度分析 ───────────────────────────────────

def _analyze_signal_contributions(completed):
    """
    分析每個信號（FR/Skew/PCR/MACD/Regime/Basis）的預測貢獻度。
    方法：對每筆已結算記錄，計算各信號值與誤差方向的相關性。
    輸出：{signal: {direction_accuracy, avg_abs_value, n}}
    """
    if len(completed) < 5:
        return {}

    signal_keys = ["fr", "skew", "dvol", "pcr_main", "macd_4h", "regime_pos", "gamma_flip", "contradiction"]
    contributions = {k: {"correct": 0, "total": 0, "sum_abs": 0.0} for k in signal_keys}

    for r in completed:
        actual = r.get("actual_settlement", 0)
        predicted = r.get("predicted_median", 0)
        error_direction = 1 if actual > predicted else -1  # 實際比預測高=正方向

        signals = r.get("signals", {})
        for sk in signal_keys:
            val = signals.get(sk)
            if val is None:
                continue
            val = float(val)
            # 判斷信號方向與誤差方向是否一致
            if sk == "fr":
                sig_dir = 1 if val > 0 else -1
            elif sk == "skew":
                sig_dir = -1 if val > 2 else (1 if val < -2 else 0)  # 正skew=空
            elif sk == "regime_pos":
                sig_dir = 1 if val > 0.5 else -1  # POS=多
            elif sk == "contradiction":
                sig_dir = -1 if val > 0.5 else 0  # 矛盾=不確定偏空
            elif sk == "macd_4h":
                sig_dir = 1 if val > 0 else -1
            elif sk == "pcr_main":
                sig_dir = -1 if val > 1.3 else (1 if val < 0.6 else 0)
            else:
                sig_dir = 0

            if sig_dir != 0:
                contributions[sk]["total"] += 1
                if sig_dir == error_direction:
                    contributions[sk]["correct"] += 1
                contributions[sk]["sum_abs"] += abs(val)

    result = {}
    for sk, v in contributions.items():
        if v["total"] > 0:
            result[sk] = {
                "direction_accuracy": round(v["correct"] / v["total"], 3),
                "avg_abs_value": round(v["sum_abs"] / v["total"], 4),
                "n": v["total"]
            }
    return result


# ── Layer 3：Regime分層優化 ───────────────────────────────────

def _optimize_regime_weights(completed, log):
    """
    POS Regime 和 NEG Regime 各自跑梯度下降，維護兩套最優權重。
    若某 Regime 樣本不足，沿用全局最優權重。
    """
    global_w = log.get("current_weights", DEFAULT_WEIGHTS)
    regime_weights = log.get("regime_weights", DEFAULT_REGIME_WEIGHTS.copy())

    pos_w = _gradient_descent(completed, regime_weights.get("POS", global_w), regime_filter="POS")
    neg_w = _gradient_descent(completed, regime_weights.get("NEG", global_w), regime_filter="NEG")

    # 若優化結果退化（全量表現更差），保留原始
    return {"POS": pos_w, "NEG": neg_w}


# ── Layer 4：貝葉斯先驗更新 ───────────────────────────────────

def _bayesian_weight_update(completed, current_weights, prior_strength=3.0):
    """
    貝葉斯先驗：以 DEFAULT_WEIGHTS 為先驗，用歷史誤差更新。
    prior_strength：先驗樣本數等效（越大越保守，越不容易偏離先驗）。
    方法：
      posterior_weight[k] = (prior_strength * prior[k] + evidence_weight[k])
                           / (prior_strength + sum(evidence_weight))
    evidence_weight[k] ∝ 1 / (MSE contribution of removing component k)
    """
    if len(completed) < 3:
        return current_weights

    fallback_w = current_weights
    prior = DEFAULT_WEIGHTS

    # 計算每個 component 的預測貢獻（leave-one-out 敏感度）
    n = len(completed)
    cluster_w = _cluster_weights(completed)
    time_w = [_recent_weight(i, n) for i in range(n)]
    decay = [c * t for c, t in zip(cluster_w, time_w)]
    decay_sum = sum(decay)

    base_mse = 0.0
    for i, r in enumerate(completed):
        pred = sum(current_weights.get(k, 0.2) * _raw_center(r, k, fallback_w) for k in KEYS)
        base_mse += decay[i] * (pred - r["actual_settlement"]) ** 2
    base_mse /= decay_sum

    evidence = {}
    for drop_k in KEYS:
        # 移除 drop_k（設 weight=0，其他歸一）
        w_drop = {k: (0 if k == drop_k else current_weights.get(k, 0.2)) for k in KEYS}
        s = sum(w_drop.values())
        w_drop = {k: v / s for k, v in w_drop.items()}
        mse_drop = 0.0
        for i, r in enumerate(completed):
            pred = sum(w_drop.get(k, 0) * _raw_center(r, k, fallback_w) for k in KEYS)
            mse_drop += decay[i] * (pred - r["actual_settlement"]) ** 2
        mse_drop /= decay_sum
        # 移除 drop_k 後誤差增加越多 → drop_k 越重要
        evidence[drop_k] = max(0.001, mse_drop - base_mse)

    # 貝葉斯更新
    evidence_total = sum(evidence.values())
    posterior = {}
    for k in KEYS:
        prior_contribution = prior_strength * prior.get(k, 0.2)
        evidence_contribution = evidence.get(k, 0.001) / evidence_total
        posterior[k] = (prior_contribution + evidence_contribution) / (prior_strength + 1.0)

    return _norm(posterior)


# ── Layer 5：收斂偵測 ─────────────────────────────────────────

def _check_convergence(log, new_error_sigma, old_error_sigma):
    """
    收斂條件：
      1. avg_error_sigma < 0.3σ（絕對收斂）
      2. 或：連續3次優化改善 < 1%（相對收斂）
    解凍條件：
      新樣本的 avg_error > 0.8σ（市場結構可能已變化）
    """
    conv = log.get("convergence", {})
    history = conv.get("avg_error_sigma_history", [])
    history.append(round(new_error_sigma, 4))
    if len(history) > 10:
        history = history[-10:]  # 只保留最近10次
    conv["avg_error_sigma_history"] = history

    # 解凍檢測
    if conv.get("frozen") and new_error_sigma > 0.8:
        print(f"[Convergence] 誤差惡化({new_error_sigma:.2f}σ)→解凍重學")
        conv["frozen"] = False
        conv["converged"] = False
        conv["consecutive_no_improve"] = 0

    # 收斂檢測
    if not conv.get("frozen"):
        abs_converged = new_error_sigma < 0.3
        if len(history) >= 3:
            improvements = [abs(history[i-1] - history[i]) / max(history[i-1], 1e-6)
                           for i in range(1, len(history))]
            rel_converged = all(imp < 0.01 for imp in improvements[-3:])
        else:
            rel_converged = False

        if abs_converged or rel_converged:
            conv["converged"] = True
            conv["frozen"] = True
            print(f"[Convergence] 已收斂 (error={new_error_sigma:.3f}σ) → 凍結權重")
        else:
            if old_error_sigma is not None and new_error_sigma >= old_error_sigma * 0.99:
                conv["consecutive_no_improve"] = conv.get("consecutive_no_improve", 0) + 1
            else:
                conv["consecutive_no_improve"] = 0

    log["convergence"] = conv
    return conv.get("frozen", False)


# ── 主優化入口 ────────────────────────────────────────────────

def optimize_weights(min_samples=10, min_cycles=3):
    """
    5層迭代學習：
    L1 梯度下降（滾動衰減+週期聚類降權） → L2 信號貢獻度 → L3 Regime分層
    → L4 貝葉斯更新 → L5 收斂偵測
    融合策略：L1(0.5) + L4(0.3) + 當前(0.2)

    統計門檻（雙重）：
      1. 總樣本 >= min_samples
      2. 不同結算週期（expiry）>= min_cycles
    理由：同一到期日的多筆快照高度自相關（actual 相同、資訊重疊），
    有效自由度 ≈ 週期數，不是快照數。單週期 n=17 統計上 ≈ n=1。

    污染樣本：corrupted_gex=True 的記錄完全剔除
    （S23-S40 GEX Pin=Spot bug 期間，gex/timedecay 分量無效）。
    """
    log = load_log()
    completed = [r for r in log["records"]
                 if r.get("actual_settlement") is not None
                 and not r.get("corrupted_gex", False)]  # 剔除污染樣本

    n_cycles = len(set(r["expiry"] for r in completed))

    if len(completed) < min_samples or n_cycles < min_cycles:
        print(f"樣本不足 (records={len(completed)}/{min_samples}, "
              f"cycles={n_cycles}/{min_cycles})，跳過優化")
        return log["current_weights"]

    # 若已收斂且未解凍，直接返回
    if log.get("convergence", {}).get("frozen"):
        print("[Optimizer] 權重已收斂凍結，跳過")
        return log["current_weights"]

    print(f"\n[Optimizer v3.0] 開始5層迭代，樣本數: {len(completed)}")

    current = log.get("current_weights", DEFAULT_WEIGHTS)

    # ── L1：全量梯度下降（帶滾動衰減）──
    l1_w = _gradient_descent(completed, current)
    print(f"  L1 梯度下降: {l1_w}")

    # ── L2：信號貢獻度分析（寫入log供Dashboard顯示）──
    sig_contrib = _analyze_signal_contributions(completed)
    log["signal_contributions"] = sig_contrib
    print(f"  L2 信號貢獻度: {sig_contrib}")

    # ── L3：Regime分層優化 ──
    regime_w = _optimize_regime_weights(completed, log)
    log["regime_weights"] = regime_w
    print(f"  L3 Regime POS: {regime_w.get('POS')}")
    print(f"  L3 Regime NEG: {regime_w.get('NEG')}")

    # ── L4：貝葉斯更新 ──
    l4_w = _bayesian_weight_update(completed, l1_w)
    print(f"  L4 Bayesian: {l4_w}")

    # ── 融合（L1×0.50 + L4×0.30 + 當前×0.20）──
    fused = {}
    for k in KEYS:
        fused[k] = 0.50 * l1_w.get(k, 0) + 0.30 * l4_w.get(k, 0) + 0.20 * current.get(k, 0)
    new_weights = _norm(fused)
    print(f"  融合結果: {new_weights}")

    # ── 計算誤差改善 ──
    fallback = current
    n = len(completed)
    cluster_w = _cluster_weights(completed)
    time_w = [_recent_weight(i, n) for i in range(n)]
    decay = [c * t for c, t in zip(cluster_w, time_w)]
    decay_sum = sum(decay)

    def mse_with_weights(w_dict):
        total = 0.0
        for i, r in enumerate(completed):
            pred = sum(w_dict.get(k, 0) * _raw_center(r, k, fallback) for k in KEYS)
            total += decay[i] * (pred - r["actual_settlement"]) ** 2
        return total / decay_sum

    old_mse = mse_with_weights(current)
    new_mse = mse_with_weights(new_weights)
    old_sigma = math.sqrt(old_mse) / (sum(r.get("sigma", 4000) for r in completed) / n)
    new_sigma = math.sqrt(new_mse) / (sum(r.get("sigma", 4000) for r in completed) / n)

    print(f"\n  誤差: {math.sqrt(old_mse):,.0f} → {math.sqrt(new_mse):,.0f} USD")
    print(f"  誤差σ: {old_sigma:.3f} → {new_sigma:.3f}")

    # ── L5：收斂偵測 ──
    old_sigma_prev = log.get("convergence", {}).get("avg_error_sigma_history", [None])[-1]
    frozen = _check_convergence(log, new_sigma, old_sigma_prev)

    # ── 保存 ──
    log["weight_history"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "samples": n,
        "old_weights": current.copy(),
        "new_weights": new_weights,
        "regime_weights": regime_w,
        "l1_weights": l1_w,
        "l4_weights": l4_w,
        "avg_error_usd": round(math.sqrt(new_mse), 2),
        "avg_error_sigma": round(new_sigma, 4),
        "improvement_pct": round((old_mse - new_mse) / max(old_mse, 1) * 100, 2),
        "frozen": frozen,
    })
    log["optimization_history"].append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "n": n, "err_sigma": round(new_sigma, 4),
        "frozen": frozen
    })
    log["current_weights"] = new_weights
    log["last_optimized"] = datetime.now(timezone.utc).isoformat()
    save_log(log)

    return new_weights


def get_regime_weights(regime):
    """給 calc_uft 呼叫：根據當前 Regime 返回最優權重"""
    log = load_log()
    regime_w = log.get("regime_weights", DEFAULT_REGIME_WEIGHTS)
    w = regime_w.get(regime, log.get("current_weights", DEFAULT_WEIGHTS))
    # 驗證 sum=1
    if abs(sum(w.values()) - 1.0) > 0.02:
        return _norm(w)
    return w


# ── record_prediction（擴充 signal_snapshot + regime）─────────

def record_prediction(snapshot_num, expiry, predicted_median, predicted_mode,
                       components, weights, signals, sigma, regime=None, t_days=None,
                       spot_at_record=None):
    """
    記錄預測。
    t_days：記錄時距結算的剩餘天數（固定T值快照核心字段）。
    固定T值設計：每個到期日只在 T=7/3/1/0d 各記一筆，
    跨週期誤差對比才有學習意義。
    去重邏輯：同一 expiry + 同一 T 窗口（±0.3d）只記一次。
    """
    log = load_log()

    # 去重：同一 snapshot_num + expiry 不重複記
    if any(r["snapshot_num"] == snapshot_num and r["expiry"] == expiry
           for r in log["records"]):
        return

    # 固定T值去重：同一 expiry + 相近 t_days（±0.3d）不重複記（硬觸發除外）
    if t_days is not None:
        TARGET_T = [7, 3, 1, 0]
        for r in log["records"]:
            if (r.get("expiry") == expiry and
                r.get("t_days_at_record") is not None):
                # 同一 T 窗口內已有記錄
                if (any(abs(r["t_days_at_record"] - t) <= 0.3 for t in TARGET_T) and
                    any(abs(t_days - t) <= 0.3 for t in TARGET_T) and
                    any(abs(r["t_days_at_record"] - t) <= 0.3 and abs(t_days - t) <= 0.3
                        for t in TARGET_T)):
                    print(f"[SKIP-DUP] {expiry} T={t_days:.1f}d 此窗口已有 S{r['snapshot_num']} T={r['t_days_at_record']:.1f}d")
                    return

    record = {
        "snapshot_num":        snapshot_num,
        "expiry":              expiry,
        "timestamp":           datetime.now(timezone.utc).isoformat(),
        "t_days_at_record":    round(t_days, 2) if t_days is not None else None,
        "predicted_median":    round(predicted_median, 2),
        "predicted_mode":      round(predicted_mode, 2),
        "sigma":               round(sigma, 2),
        "regime_at_prediction":regime,
        "spot_at_record":      round(spot_at_record, 2) if spot_at_record else None,  # EMH基準
        "actual_settlement":   None,
        "components":  {k: round(float(v), 2) for k, v in components.items()},
        "weights_used":{k: round(float(v), 5) for k, v in weights.items()},
        "signals":     {k: (round(float(v), 5) if v is not None else None)
                        for k, v in signals.items()},
        "error_sigma": None,
        "error_usd":   None,
    }
    log["records"].append(record)
    save_log(log)
    print(f"Recorded S{snapshot_num} {expiry} T={t_days:.1f}d: ${predicted_median:,.0f} (regime={regime})")


def record_settlement(expiry, actual_price):
    """結算後填入實際價格並計算誤差"""
    log = load_log()
    updated = 0
    for record in log["records"]:
        if record["expiry"] == expiry and record["actual_settlement"] is None:
            record["actual_settlement"] = actual_price
            error_usd = abs(actual_price - record["predicted_median"])
            sigma = record.get("sigma", 4000)
            record["error_usd"] = round(error_usd, 2)
            record["error_sigma"] = round(error_usd / sigma if sigma > 0 else 0, 4)
            # EMH 基準：直接用記錄時 Spot 當預測的誤差（模型必須贏這個才有 alpha）
            spot_rec = record.get("spot_at_record")
            if spot_rec:
                emh_err = abs(actual_price - spot_rec)
                record["emh_error_usd"] = round(emh_err, 2)
                record["beats_emh"] = bool(error_usd < emh_err)
            updated += 1
            print(f"Settlement S{record['snapshot_num']} {expiry}: "
                  f"pred=${record['predicted_median']:,.0f}, actual=${actual_price:,.0f}, "
                  f"err=${error_usd:,.0f} ({record['error_sigma']:.2f}σ)")
    if updated:
        save_log(log)
    return updated


def check_and_record_settlement():
    """自動從 Deribit 拉取結算價"""
    today = date.today()
    log = load_log()
    pending = set(r["expiry"] for r in log["records"] if r["actual_settlement"] is None)
    if not pending:
        return

    for expiry_str in pending:
        try:
            m = re.match(r"(\d+)([A-Z]+)(\d+)", expiry_str.upper())
            if not m:
                continue
            expiry_date = date(2000 + int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
            if today < expiry_date:
                continue
            r_api = requests.get(
                "https://www.deribit.com/api/v2/public/get_delivery_prices"
                "?index_name=btc_usd&offset=0&count=10",
                timeout=10
            )
            deliveries = r_api.json().get("result", {}).get("data", [])
            for d in deliveries:
                # Deribit date 格式：字串 "YYYY-MM-DD"（非 timestamp）
                raw = d.get("date")
                if isinstance(raw, str):
                    d_date = date.fromisoformat(raw[:10])
                else:
                    d_date = date.fromtimestamp(float(raw) / 1000)
                if d_date == expiry_date:
                    n = record_settlement(expiry_str, float(d["delivery_price"]))
                    print(f"Auto-settlement {expiry_str}: ${float(d['delivery_price']):,.2f} ({n}筆)")
                    break
        except Exception as e:
            print(f"Settlement check {expiry_str}: {e}")


if __name__ == "__main__":
    check_and_record_settlement()
    w = optimize_weights(min_samples=5)
    print(f"\n當前最優權重: {w}")
    log = load_log()
    print(f"信號貢獻度: {log.get('signal_contributions', {})}")
