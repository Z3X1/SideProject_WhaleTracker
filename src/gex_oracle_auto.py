import json
#!/usr/bin/env python3
"""
GEX Oracle 自動化引擎 v2.0
每6h自動執行：數據抓取 → UFT計算 → Claude碰撞 → HTML生成 → Telegram推送
"""

import os, json, math, time, requests, sqlite3
from datetime import datetime, timezone

# ============================================================
# 1. 數據抓取層
# ============================================================

def fetch_binance_spot():
    """Spot價格"""
    r = requests.get(
        "https://fapi.binance.com/fapi/v1/ticker/price",
        params={"symbol": "BTCUSDT"}, timeout=10
    )
    d = r.json()
    return float(d.get("price") or d.get("markPrice") or d.get("lastPrice"))

def fetch_binance_fr():
    """資金費率"""
    r = requests.get(
        "https://fapi.binance.com/fapi/v1/premiumIndex",
        params={"symbol": "BTCUSDT"}, timeout=10
    )
    d = r.json()
    return float(d.get("lastFundingRate") or d.get("interestRate") or 0)

def fetch_binance_oi():
    """持倉量（萬張）"""
    r = requests.get(
        "https://fapi.binance.com/fapi/v1/openInterest",
        params={"symbol": "BTCUSDT"}, timeout=10
    )
    d = r.json()
    oi = d.get("openInterest") or d.get("sumOpenInterest") or 0
    return float(oi) / 10000

def fetch_binance_ls():
    """大戶多空比"""
    try:
        r = requests.get(
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol": "BTCUSDT", "period": "5m", "limit": 1}, timeout=10
        )
        return float(r.json()[0]["longShortRatio"])
    except:
        r = requests.get(
            "https://fapi.binance.com/futures/data/topLongShortAccountRatio",
            params={"symbol": "BTCUSDT", "period": "5m", "limit": 1}, timeout=10
        )
        return float(r.json()[0]["longShortRatio"])

def fetch_binance_klines(interval="4h", limit=100):
    """K線數據（用於計算EMA/MACD）"""
    r = requests.get(
        "https://fapi.binance.com/fapi/v1/klines",
        params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
        timeout=10
    )
    data = r.json()
    if isinstance(data, list) and len(data) > 0:
        closes = [float(k[4]) for k in data]
    else:
        # fallback: spot market
        r2 = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
            timeout=10
        )
        closes = [float(k[4]) for k in r2.json()]
    return closes

def calc_ema(prices, period):
    """指數移動平均"""
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_macd(prices):
    """MACD(12,26,9)"""
    def ema_series(prices, period):
        k = 2 / (period + 1)
        result = [prices[0]]
        for p in prices[1:]:
            result.append(p * k + result[-1] * (1 - k))
        return result

    ema12 = ema_series(prices, 12)
    ema26 = ema_series(prices, 26)
    dif = [e12 - e26 for e12, e26 in zip(ema12, ema26)]

    dea_k = 2 / (9 + 1)
    dea = [dif[0]]
    for d in dif[1:]:
        dea.append(d * dea_k + dea[-1] * (1 - dea_k))

    macd = [(d - e) * 2 for d, e in zip(dif, dea)]
    return dif[-1], dea[-1], macd[-1]

def fetch_deribit_dvol():
    """DVOL（BTC期權隱含波動率指數）"""
    r = requests.get(
        "https://www.deribit.com/api/v2/public/get_index",
        params={"currency": "BTC"}, timeout=10
    )
    # DVOL單獨端點
    try:
        r2 = requests.get(
            "https://www.deribit.com/api/v2/public/get_volatility_index_data",
            params={"currency": "BTC", "resolution": "3600", "count": 1},
            timeout=10
        )
        dvol = r2.json()["result"]["data"][-1][4]  # close值
        return float(dvol)
    except:
        return 46.0  # fallback

def fetch_deribit_options(expiry_label):
    """
    抓取指定到期日的期權鏈
    expiry_label: 例如 "3JUL26", "31JUL26", "25SEP26"
    返回: {strike: {call_oi, put_oi, call_iv, put_iv}}
    """
    # Deribit到期日格式轉換（3JUL26 → 26JUL3 → 3JUL26格式）
    r = requests.get(
        "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
        params={"currency": "BTC", "kind": "option"},
        timeout=15
    )
    data = r.json().get("result", [])

    result = {}
    for item in data:
        name = item.get("instrument_name", "")
        # 過濾指定到期日
        # instrument格式: BTC-3JUL26-60000-C
        parts = name.split("-")
        if len(parts) != 4:
            continue
        _, exp, strike_str, opt_type = parts
        if exp.upper() != expiry_label.upper():
            continue

        strike = int(strike_str)
        oi = float(item.get("open_interest", 0))
        iv = float(item.get("mark_iv", 0))

        if strike not in result:
            result[strike] = {"call_oi": 0, "put_oi": 0, "call_iv": 0, "put_iv": 0}

        if opt_type == "C":
            result[strike]["call_oi"] = oi
            result[strike]["call_iv"] = iv
        else:
            result[strike]["put_oi"] = oi
            result[strike]["put_iv"] = iv

    return result

def collect_all_data():
    """主數據收集函數"""
    print("📡 開始收集數據...")
    data = {}

    # Binance
    data["spot"] = fetch_binance_spot()
    print(f"  Spot: ${data['spot']:,.0f}")

    data["fr"] = fetch_binance_fr()
    print(f"  FR: {data['fr']*100:.5f}%")

    data["oi"] = fetch_binance_oi()
    print(f"  OI: {data['oi']:.2f}萬")

    data["ls"] = fetch_binance_ls()
    print(f"  L/S: {data['ls']:.4f}")

    # MACD計算
    for tf, interval in [("15m", "15m"), ("4h", "4h"), ("1d", "1d")]:
        closes = fetch_binance_klines(interval=interval, limit=100)
        emas = {}
        for p in [5, 10, 30, 200]:
            if len(closes) >= p:
                emas[p] = calc_ema(closes, p)
        dif, dea, macd = calc_macd(closes)
        data[f"ema_{tf}"] = emas
        data[f"macd_{tf}"] = {"dif": dif, "dea": dea, "macd": macd}
        print(f"  {tf} MACD: DIF={dif:.2f}, MACD={macd:.2f}")
        time.sleep(0.3)

    # Deribit
    data["dvol"] = fetch_deribit_dvol()
    print(f"  DVOL: {data['dvol']:.2f}%")

    # 期權鏈（三個到期日）
    for expiry in ["3JUL26", "31JUL26", "25SEP26"]:
        try:
            opts = fetch_deribit_options(expiry)
            data[f"options_{expiry}"] = opts
            print(f"  {expiry}: {len(opts)} strikes")
            time.sleep(0.5)
        except Exception as e:
            print(f"  {expiry} 失敗: {e}")
            data[f"options_{expiry}"] = {}

    data["timestamp"] = datetime.now(timezone.utc).isoformat()
    return data

# ============================================================
# 2. UFT計算層
# ============================================================

def calc_gex_structure(options, spot):
    """計算GEX結構：Pin水位、PCR、Gamma Flip"""
    if not options:
        return {"pin": spot, "pcr": 1.0, "gamma_flip": spot - 2000}

    # PCR（OI加權）
    total_call_oi = sum(v["call_oi"] for v in options.values())
    total_put_oi = sum(v["put_oi"] for v in options.values())
    pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 1.0

    # ATM Put Wall（最大Put OI在Spot附近）
    atm_range = {k: v for k, v in options.items() if abs(k - spot) < 5000}
    if atm_range:
        max_put_strike = max(atm_range, key=lambda k: atm_range[k]["put_oi"])
        pin = max_put_strike
    else:
        pin = round(spot / 1000) * 1000  # 最近千位

    return {
        "pin": pin,
        "pcr": pcr,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
    }

def calc_behavior_signal(fr, ls, oi_change, prev_ls=None):
    """
    行為信號計算
    Rule#15: FR正+L/S同升=全權重；矛盾=×0.5
    """
    fr_direction = 1 if fr > 0 else -1
    ls_direction = 1 if (prev_ls is None or ls > prev_ls) else -1

    # 矛盾檢測
    contradiction = (fr_direction != ls_direction)
    weight = 0.5 if contradiction else 1.0

    # 信號強度
    fr_signal = min(abs(fr) / 0.0001, 1.0) * fr_direction  # 正規化
    ls_signal = (ls - 2.0) / 0.5  # 2.0為中性基準

    raw_signal = (fr_signal * 0.4 + ls_signal * 0.6) * weight
    return raw_signal, contradiction, weight

def calc_uft(data, prev_data=None):
    """UFT統一場方程計算"""
    spot = data["spot"]
    dvol = data["dvol"] / 100
    T = 7 / 365  # 3JUL26
    sigma = spot * dvol * math.sqrt(T)

    # GEX成分
    opts_3jul = data.get("options_3JUL26", {})
    gex = calc_gex_structure(opts_3jul, spot)
    gex_center = gex["pin"]

    # BehaviorSignal成分
    prev_ls = prev_data["ls"] if prev_data else None
    oi_change = (data["oi"] - prev_data["oi"]) if prev_data else 0
    behavior_raw, contradiction, weight = calc_behavior_signal(
        data["fr"], data["ls"], oi_change, prev_ls
    )
    behavior_center = spot + behavior_raw * sigma * 0.3

    # GBM成分
    macd_4h = data["macd_4h"]["macd"]
    gbm_bias = -0.05 if macd_4h < -100 else (0.05 if macd_4h > 100 else 0)
    gbm_center = spot + gbm_bias * sigma

    # 貝葉斯成分（簡化）
    macd_1d = data["macd_1d"]["macd"]
    if macd_1d > 0:
        bayes_center = spot * 1.005  # 偏多
    else:
        bayes_center = spot * 0.997  # 偏空

    # TimeDecay成分
    timedecay_center = gex_center  # Pin水位

    # UFT合成
    uft = (
        0.40 * gbm_center +
        0.10 * gex_center +
        0.28 * behavior_center +
        0.12 * bayes_center +
        0.10 * timedecay_center
    )

    return {
        "uft_median": uft,
        "uft_mode": gex_center,
        "uft_emh": spot,
        "sigma": sigma,
        "gex": gex,
        "behavior_contradiction": contradiction,
        "behavior_weight": weight,
        "gbm_center": gbm_center,
        "behavior_center": behavior_center,
        "bayes_center": bayes_center,
        "components": {
            "gbm": 0.40 * gbm_center,
            "gex": 0.10 * gex_center,
            "behavior": 0.28 * behavior_center,
            "bayesian": 0.12 * bayes_center,
            "timedecay": 0.10 * timedecay_center,
        }
    }

# ============================================================
# 3. Claude API碰撞層
# ============================================================

UFT_SYSTEM_PROMPT = """你是GEX Oracle分析引擎，使用統一場論(UFT) v2.0對抗性碰撞框架分析BTC期權市場。

核心規則：
R#1 Put Wall三態：OTM(Gamma≈0)/ATM(最不穩定)/ITM(動態支撐)
R#2 MACD壽命：15min≥6.5h/4h≥104h/1D≥26天。負值域金叉=0.5x
R#5 FR穿越0%=最重要觸發信號
R#10 POS Regime(Spot>GF)=穩定器/NEG(Spot<GF)=放大器
R#14 主到期日結算後概率重置
R#15 FR正+L/S同降=矛盾信號，BehaviorSignal×0.5
R#16 ≥3個到期日同一行權價最大Put OI=強力磁吸Pin

UFT方程：P(X) = 0.40×GBM + 0.10×GEX + 0.28×BehaviorSignal + 0.12×Bayesian + 0.10×TimeDecay

輸出格式：JSON，包含以下欄位：
{
  "layer1_bull": ["論點1", "論點2"],
  "layer1_bear": ["論點1", "論點2"],
  "layer1_verdict": "BULL 0.XX / BEAR 0.XX",
  "layer2_bull": [...],
  "layer2_bear": [...],
  "layer2_verdict": "...",
  "layer3_bull": [...],
  "layer3_bear": [...],
  "layer3_verdict": "...",
  "layer4_bull": [...],
  "layer4_bear": [...],
  "layer4_verdict": "...",
  "oracle_verdict": "BULL/BEAR 0.XX",
  "key_insight": "本快照最重要的一句話洞察",
  "next_trigger": "最需要監控的下一個觸發條件"
}
只輸出JSON，不要其他文字。"""

def call_claude_collision(data, uft_result):
    """呼叫Claude API進行對抗性碰撞"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("⚠️  未設置ANTHROPIC_API_KEY，跳過Claude碰撞")
        return None

    # 構建輸入摘要
    macd_15 = data["macd_15m"]
    macd_4h = data["macd_4h"]
    macd_1d = data["macd_1d"]

    user_prompt = f"""
當前快照數據（UTC: {data['timestamp']}）：

基本數據：
- Spot: ${data['spot']:,.0f}
- DVOL: {data['dvol']:.2f}%
- FR: {data['fr']*100:+.5f}%（{'正值，多頭付費' if data['fr']>0 else '負值，空頭付費'}）
- L/S: {data['ls']:.4f}
- OI: {data['oi']:.2f}萬

MACD（15min）: DIF={macd_15['dif']:.2f}, DEA={macd_15['dea']:.2f}, MACD={macd_15['macd']:.2f}
MACD（4h）: DIF={macd_4h['dif']:.2f}, DEA={macd_4h['dea']:.2f}, MACD={macd_4h['macd']:.2f}
MACD（1D）: DIF={macd_1d['dif']:.2f}, DEA={macd_1d['dea']:.2f}, MACD={macd_1d['macd']:.2f}

UFT計算結果：
- σ(7天): ${uft_result['sigma']:,.0f}
- GEX Pin: ${uft_result['gex']['pin']:,}
- PCR(3JUL26): {uft_result['gex']['pcr']:.3f}
- BehaviorSignal矛盾: {'是（權重×0.5）' if uft_result['behavior_contradiction'] else '否（全權重）'}
- UFT Median: ${uft_result['uft_median']:,.0f}

請執行4層對抗性碰撞並輸出JSON。
"""

    headers = {
        "x-api-key": api_key,
        "content-type": "application/json",
        "anthropic-version": "2023-06-01"
    }
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1500,
        "system": UFT_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}]
    }

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers, json=body, timeout=60
        )
        text = r.json()["content"][0]["text"]
        # 清理JSON
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        print(f"Claude API錯誤: {e}")
        return None

# ============================================================
# 4. HTML生成層
# ============================================================

def generate_html(data, uft_result, collision, snapshot_num):
    """生成完整Dashboard HTML"""
    spot = data["spot"]
    ts = data["timestamp"]
    now_utc8 = datetime.now().strftime("%Y-%m-%d %H:%M UTC+8")

    fr_pct = data["fr"] * 100
    fr_color = "var(--green)" if fr_pct > 0 else "var(--red)"
    fr_sign = "+" if fr_pct > 0 else ""

    macd_15 = data["macd_15m"]
    macd_4h = data["macd_4h"]
    macd_1d = data["macd_1d"]

    # 15min判斷
    m15_status = "金叉" if macd_15["dif"] > macd_15["dea"] else "死叉"
    m15_color = "var(--green)" if m15_status == "金叉" else "var(--red)"
    m4h_status = "金叉" if macd_4h["dif"] > macd_4h["dea"] else "死叉"
    m4h_color = "var(--green)" if m4h_status == "金叉" else "var(--red)"
    m1d_status = "金叉" if macd_1d["dif"] > macd_1d["dea"] else "死叉"
    m1d_color = "var(--green)" if m1d_status == "金叉" else "var(--red)"

    # 碰撞結果
    oracle = collision.get("oracle_verdict", "N/A") if collision else "N/A"
    key_insight = collision.get("key_insight", "Claude API未啟用") if collision else "Claude API未啟用"
    next_trigger = collision.get("next_trigger", "") if collision else ""

    uft_med = uft_result["uft_median"]
    uft_mode = uft_result["uft_mode"]
    sigma = uft_result["sigma"]
    contradiction = uft_result["behavior_contradiction"]
    rule15_text = "⚠️ Rule#15：矛盾信號，BehaviorSignal×0.5" if contradiction else "✅ Rule#15：信號一致，全權重"
    rule15_color = "var(--yellow)" if contradiction else "var(--green)"

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="refresh" content="21600">
<title>GEX Oracle S{snapshot_num} | Auto</title>
<style>
:root{{--bg:#0a0e17;--panel:#111827;--border:#1e293b;--accent:#3b82f6;--green:#10b981;--red:#ef4444;--yellow:#f59e0b;--purple:#8b5cf6;--cyan:#06b6d4;--text:#e2e8f0;--muted:#64748b}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:'SF Mono','Consolas',monospace;font-size:12px}}
.header{{background:linear-gradient(135deg,#0f172a,#1e1b4b);border-bottom:2px solid var(--accent);padding:14px 20px;display:flex;justify-content:space-between;align-items:center}}
.header h1{{font-size:18px;color:var(--accent);letter-spacing:2px}}
.spot{{font-size:28px;font-weight:bold;color:var(--yellow)}}
.grid-4{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;padding:14px}}
.grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:0 14px 14px}}
.card{{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px}}
.card-title{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;border-bottom:1px solid var(--border);padding-bottom:5px}}
.kpi{{text-align:center}}
.kpi-val{{font-size:20px;font-weight:bold}}
.kpi-lbl{{font-size:9px;color:var(--muted);margin-top:2px}}
.kpi-delta{{font-size:10px;margin-top:3px}}
.bull{{color:var(--green)}}.bear{{color:var(--red)}}.neutral{{color:var(--yellow)}}.info{{color:var(--cyan)}}
.alert{{border-radius:6px;padding:8px 12px;margin:0 14px 10px;font-size:11px;display:flex;align-items:flex-start;gap:8px}}
.alert-info{{background:rgba(59,130,246,.15);border:1px solid rgba(59,130,246,.4)}}
.alert-warn{{background:rgba(245,158,11,.15);border:1px solid rgba(245,158,11,.4)}}
.settlement-box{{background:linear-gradient(135deg,rgba(59,130,246,.1),rgba(139,92,246,.1));border:2px solid var(--accent);border-radius:10px;padding:14px;text-align:center;margin:14px}}
.settlement-main{{font-size:32px;font-weight:bold;color:var(--yellow)}}
.auto-badge{{background:rgba(16,185,129,.2);color:var(--green);padding:2px 8px;border-radius:10px;font-size:9px;font-weight:bold}}
.insight-box{{background:rgba(139,92,246,.08);border:1px solid rgba(139,92,246,.3);border-radius:6px;padding:10px;margin:0 14px 10px;font-size:11px;line-height:1.7}}
hr{{border:none;border-top:1px solid var(--border);margin:8px 0}}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>⚡ GEX ORACLE AUTO <span class="auto-badge">S{snapshot_num}</span></h1>
    <div style="color:var(--muted);font-size:10px;margin-top:2px">UFT v2.0 自動化 ｜ {now_utc8}</div>
  </div>
  <div style="text-align:right">
    <div style="font-size:10px;color:var(--muted)">BTC/USDT 永續</div>
    <div class="spot">${spot:,.0f}</div>
    <div style="font-size:10px;color:{fr_color}">FR {fr_sign}{fr_pct:.5f}% ｜ DVOL {data['dvol']:.2f}%</div>
  </div>
</div>

<div class="alert alert-info" style="margin-top:10px">
  <span>⚡</span>
  <span><strong>Oracle裁決：{oracle}</strong> ｜ σ(3JUL26)=${sigma:,.0f} ｜ UFT Median=${uft_med:,.0f}</span>
</div>

<div class="alert" style="margin:0 14px 10px;background:rgba(245,158,11,.1);border:1px solid {rule15_color};border-radius:6px;padding:8px 12px;font-size:11px;display:flex;gap:8px;align-items:center">
  <span style="color:{rule15_color}">{rule15_text}</span>
</div>

<div class="grid-4">
  <div class="card kpi">
    <div class="kpi-val" style="color:var(--yellow)">${spot:,.0f}</div>
    <div class="kpi-lbl">SPOT</div>
  </div>
  <div class="card kpi">
    <div class="kpi-val" style="color:{fr_color}">{fr_sign}{fr_pct:.5f}%</div>
    <div class="kpi-lbl">資金費率 FR</div>
  </div>
  <div class="card kpi">
    <div class="kpi-val {'bull' if data['ls'] > 2.1 else 'neutral'}">{data['ls']:.4f}</div>
    <div class="kpi-lbl">大戶多空比 L/S</div>
  </div>
  <div class="card kpi">
    <div class="kpi-val neutral">{data['oi']:.2f}萬</div>
    <div class="kpi-lbl">持倉量 OI</div>
  </div>
</div>

<div class="grid-2">
  <div>
    <div class="card" style="margin-bottom:10px">
      <div class="card-title">📈 三框架 MACD</div>
      <div style="font-size:10px;line-height:2">
        <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">
          <span class="info">15min (30%)</span>
          <span style="color:{m15_color};font-weight:bold">{m15_status} {macd_15['macd']:+.2f}</span>
          <span style="color:var(--muted)">DIF {macd_15['dif']:+.1f}</span>
        </div>
        <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">
          <span class="info">4h (62%)</span>
          <span style="color:{m4h_color};font-weight:bold">{m4h_status} {macd_4h['macd']:+.2f}</span>
          <span style="color:var(--muted)">DIF {macd_4h['dif']:+.1f}</span>
        </div>
        <div style="display:flex;justify-content:space-between;padding:4px 0">
          <span class="info">1D (70%)</span>
          <span style="color:{m1d_color};font-weight:bold">{m1d_status} {macd_1d['macd']:+.2f}</span>
          <span style="color:var(--muted)">DIF {macd_1d['dif']:+.1f}</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">🎯 GEX結構</div>
      <div style="font-size:10px;line-height:2">
        <div>GEX Pin (3JUL26): <strong>${uft_mode:,}</strong></div>
        <div>PCR: <strong>{uft_result['gex']['pcr']:.3f}</strong></div>
        <div>Spot vs Pin: <strong>{spot - uft_mode:+,.0f}</strong></div>
      </div>
    </div>
  </div>

  <div>
    <div class="card">
      <div class="card-title">⚗️ UFT v2.0 分解</div>
      <div style="font-size:10px;line-height:2;font-family:monospace">
        <div>GBM(×0.40) = ${uft_result['components']['gbm']:,.0f}</div>
        <div>GEX(×0.10) = ${uft_result['components']['gex']:,.0f}</div>
        <div>Behavior(×0.28{'×0.5' if contradiction else ''}) = ${uft_result['components']['behavior']:,.0f}</div>
        <div>Bayesian(×0.12) = ${uft_result['components']['bayesian']:,.0f}</div>
        <div>TimeDecay(×0.10) = ${uft_result['components']['timedecay']:,.0f}</div>
        <hr>
        <div style="font-size:13px;color:var(--yellow);font-weight:bold">Median = ${uft_med:,.0f}</div>
        <div style="color:var(--muted)">Mode = ${uft_mode:,} | EMH = ${spot:,}</div>
      </div>
    </div>
  </div>
</div>

<div class="insight-box">
  <strong style="color:var(--purple)">💡 Oracle洞察：</strong>{key_insight}
  {"<br><strong style='color:var(--cyan)'>📍 監控：</strong>" + next_trigger if next_trigger else ""}
</div>

<div style="text-align:center;padding:10px;color:var(--muted);font-size:10px">
  自動生成 ｜ GEX Oracle v2.0 ｜ 每6h更新 ｜ 非投資建議
</div>

</body>
</html>"""

    return html

# ============================================================
# 5. Telegram推送層
# ============================================================

def send_telegram(data, uft_result, collision, snapshot_num):
    """推送簡要摘要到Telegram"""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        print("⚠️  未設置Telegram，跳過推送")
        return

    spot = data["spot"]
    fr_pct = data["fr"] * 100
    oracle = collision.get("oracle_verdict", "N/A") if collision else "N/A"
    key_insight = collision.get("key_insight", "—") if collision else "—"
    next_trigger = collision.get("next_trigger", "—") if collision else "—"
    uft_med = uft_result["uft_median"]
    contradiction = uft_result["behavior_contradiction"]

    macd_1d = data["macd_1d"]
    m1d = "📈金叉" if macd_1d["dif"] > macd_1d["dea"] else "📉死叉"

    r15 = "⚠️矛盾(×0.5)" if contradiction else "✅一致(全權重)"

    msg = f"""⚡ *GEX Oracle S{snapshot_num}* 自動更新

💰 Spot: `${spot:,.0f}`
📊 FR: `{fr_pct:+.5f}%` | L/S: `{data['ls']:.4f}` | OI: `{data['oi']:.2f}萬`

🎯 UFT Median: `${uft_med:,.0f}`
⚔️ Oracle: `{oracle}`
📅 1D MACD: {m1d}
🔀 Rule#15: {r15}

💡 *洞察*: {key_insight}
📍 *監控*: {next_trigger}

_{datetime.now().strftime('%Y-%m-%d %H:%M')} UTC+8_"""

    requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
        timeout=10
    )
    print("✅ Telegram推送完成")

# ============================================================
# 6. 狀態持久化層
# ============================================================

def load_prev_data(db_path="data/gex_oracle.db"):
    """從SQLite載入上次快照"""
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        snapshot_num INTEGER,
        spot REAL, fr REAL, ls REAL, oi REAL, dvol REAL,
        uft_median REAL, oracle_verdict TEXT,
        data_json TEXT
    )""")
    conn.commit()
    row = conn.execute(
        "SELECT data_json, snapshot_num FROM snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row:
        return json.loads(row[0]), row[1]
    return None, 22  # 從S22開始

def save_snapshot(data, uft_result, collision, snapshot_num, db_path="data/gex_oracle.db"):
    """保存快照到SQLite"""
    conn = sqlite3.connect(db_path)
    oracle = collision.get("oracle_verdict", "") if collision else ""
    conn.execute("""INSERT INTO snapshots
        (timestamp, snapshot_num, spot, fr, ls, oi, dvol, uft_median, oracle_verdict, data_json)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", (
        data["timestamp"], snapshot_num,
        data["spot"], data["fr"], data["ls"], data["oi"], data["dvol"],
        uft_result["uft_median"], oracle,
        json.dumps(data)
    ))
    conn.commit()
    conn.close()
    print(f"✅ S{snapshot_num} 保存完成")

# ============================================================
# 主執行流程
# ============================================================

def main():
    print("="*50)
    print("GEX Oracle 自動化引擎 v2.0")
    print("="*50)

    # 載入上次狀態
    prev_data, prev_num = load_prev_data()
    snapshot_num = prev_num + 1
    print(f"快照編號: S{snapshot_num}")

    # 1. 優先讀取已抓取的市場數據（由gex_oracle_fetch.py生成）
    market_data_path = "data/oracle_market_data.json"
    if os.path.exists(market_data_path):
        print(f"📂 讀取預抓取數據: {market_data_path}")
        with open(market_data_path) as f:
            data = json.load(f)
        print(f"  Spot: ${data.get('spot', 0):,.0f}")
        print(f"  FR: {data.get('fr', 0)*100:+.5f}%")
        print(f"  L/S: {data.get('ls', 0):.4f}")
        print(f"  DVOL: {data.get('dvol', 46):.2f}%")
    else:
        print("📡 開始即時抓取數據...")
        data = collect_all_data()

    # 2. UFT計算
    uft_result = calc_uft(data, prev_data)
    print(f"UFT Median: ${uft_result['uft_median']:,.0f}")

    # 3. Claude碰撞
    collision = call_claude_collision(data, uft_result)

    # 4. 生成HTML
    html = generate_html(data, uft_result, collision, snapshot_num)
    output_dir = os.environ.get("OUTPUT_DIR", "docs"); os.makedirs(output_dir, exist_ok=True)
    with open(f"{output_dir}/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("✅ HTML生成完成 → docs/index.html")

    # 5. Telegram推送
    send_telegram(data, uft_result, collision, snapshot_num)

    # 6. 保存狀態
    save_snapshot(data, uft_result, collision, snapshot_num)

    print(f"\n✅ S{snapshot_num} 完成")

if __name__ == "__main__":
    main()
