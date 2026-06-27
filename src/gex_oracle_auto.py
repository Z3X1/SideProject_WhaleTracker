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
    print("📡 Collecting data...")
    data = {}

    # Binance
    data["spot"] = fetch_binance_spot()
    print(f"  Spot: ${data['spot']:,.0f}")

    data["fr"] = fetch_binance_fr()
    print(f"  FR: {data['fr']*100:.5f}%")

    data["oi"] = fetch_binance_oi()
    print(f"  OI: {data['oi']:.2f}萬")

    data["ls"] = fetch_binance_ls()
    print(f"  L/S: {data.get('ls') or 'N/A'}")

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
    """計算GEX Structure：Pin水位、PCR、Gamma Flip"""
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

    # BehaviorSignal成分（L/S已移除，用FR+PCR+Skew）
    expiries = data.get("expiries", ["3JUL26","31JUL26","25SEP26"])
    fr = data.get("fr", 0)
    oi_change = (data.get("oi",0) - prev_data.get("oi",0)) if prev_data else 0
    skew_main = data.get("skew", {}).get(expiries[0] if expiries else "3JUL26", 0) or 0
    # FR信號方向
    fr_signal = 1 if fr > 0 else -1
    fr_strength = min(abs(fr) / 0.0001, 1.0)
    # Skew信號（正skew=偏空，負skew=偏多）
    skew_signal = -1 if skew_main > 2 else (1 if skew_main < -2 else 0)
    # PCR信號（PCR>1=偏空，PCR<0.7=偏多）
    pcr_val = data.get("options", {}).get(expiries[0] if expiries else "3JUL26", {})
    tc = sum(float(v.get("call_oi",0)) for v in pcr_val.values()) if pcr_val else 1
    tp = sum(float(v.get("put_oi",0)) for v in pcr_val.values()) if pcr_val else 1
    pcr_ratio = tp/tc if tc > 0 else 1
    pcr_signal = -1 if pcr_ratio > 1.2 else (1 if pcr_ratio < 0.7 else 0)
    # 合成行為信號
    raw_signal = (fr_signal * fr_strength * 0.5 + skew_signal * 0.3 + pcr_signal * 0.2)
    # 矛盾檢測：FR多但Skew強烈偏空
    contradiction = (fr_signal > 0 and skew_main > 5) or (fr_signal < 0 and skew_main < -5)
    weight = 0.5 if contradiction else 1.0
    behavior_raw = raw_signal * weight
    behavior_center = spot + behavior_raw * sigma * 0.3

    # 使用精確Gamma Flip
    expiries = data.get("expiries", ["3JUL26","31JUL26","25SEP26"])
    gf_main = data.get("gamma_flip_main") or data.get("gamma_flip", {}).get(expiries[0] if expiries else "3JUL26", spot-2000)
    regime = "POS" if spot > gf_main else "NEG"
    # GBM成分
    m4h = data.get("macd_4h") or data.get("macd", {}).get("4h", {})
    macd_4h_val = float(m4h.get("macd", 0)) if m4h else 0
    gbm_bias = -0.05 if macd_4h_val < -100 else (0.05 if macd_4h_val > 100 else 0)
    # NEG Regime加強下行偏移
    if regime == "NEG":
        gbm_bias -= 0.03
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
        "regime": regime,
        "gamma_flip": gf_main,
        "behavior_contradiction": contradiction,
        "behavior_weight": weight,
        "skew_main": skew_main,
        "pcr_signal": pcr_signal,
        "fr_signal": fr_signal,
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
R#2 MACD壽命：15min≥6.5h/4h≥104h/1D≥26天。負值域Bullish X=0.5x
R#5 FR穿越0%=最重要觸發信號
R#10 POS Regime(Spot>GF)=穩定器/NEG(Spot<GF)=放大器
R#14 主到期日結算後概率重置
R#15 FR正+L/S同降=Contradictory Signal，BehaviorSignal x0.5
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
- FR: {data['fr']*100:+.5f}%（{'正值，Longs pay' if data['fr']>0 else '負值，Shorts pay'}）
- L/S: {data.get('ls') or 'N/A'}
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
        resp = r.json()
        # 處理各種回應格式
        if "error" in resp:
            print(f"Claude API error: {resp['error']}")
            return None
        content_blocks = resp.get("content", [])
        if not content_blocks:
            print(f"Claude API: 空回應 {resp}")
            return None
        text = ""
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")
        if not text:
            print(f"Claude API: 無text block")
            return None
        # 清理JSON
        text = text.strip()
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    text = part
                    break
        return json.loads(text.strip())
    except Exception as e:
        print(f"Claude API錯誤: {e}")
        return None

# ============================================================
# 4. HTML生成層
# ============================================================

def generate_html(data, uft_result, collision, snapshot_num):
    import math, json as _json
    from datetime import datetime, timezone

    spot    = float(data.get("spot") or 0)
    fr      = float(data.get("fr") or 0) * 100
    oi      = float(data.get("oi") or 0)
    dvol    = float(data.get("dvol") or 0)
    ts      = str(data.get("timestamp",""))[:16].replace("T"," ")
    expiries = data.get("expiries", ["3JUL26","31JUL26","25SEP26"])
    exp0 = expiries[0] if len(expiries)>0 else "N/A"
    exp1 = expiries[1] if len(expiries)>1 else "N/A"
    exp2 = expiries[2] if len(expiries)>2 else "N/A"

    uft_med  = float(uft_result.get("uft_median") or spot)
    uft_mode = float(uft_result.get("uft_mode") or spot)
    sigma    = float(uft_result.get("sigma") or 0)
    contra   = bool(uft_result.get("behavior_contradiction", False))
    comps    = uft_result.get("components", {})
    regime   = uft_result.get("regime", "POS")
    gf_main  = int(uft_result.get("gamma_flip", uft_mode) or uft_mode)
    skew_main = float(uft_result.get("skew_main") or 0)
    weights  = data.get("uft_weights", {"gbm":0.40,"gex":0.10,"behavior":0.28,"bayesian":0.12,"timedecay":0.10})

    def mstat(key_flat, key_nested):
        m = data.get(key_flat) or data.get("macd",{}).get(key_nested,{})
        dif = float(m.get("dif",0)); dea = float(m.get("dea",0)); mac = float(m.get("macd",0))
        bull = dif > dea
        return ("BULL X" if bull else "BEAR X"), ("#10b981" if bull else "#ef4444"), dif, dea, mac

    s15,c15,d15,e15,m15 = mstat("macd_15m","15m")
    s4h,c4h,d4h,e4h,m4h = mstat("macd_4h","4h")
    s1d,c1d,d1d,e1d,m1d = mstat("macd_1d","1d")

    fr_col  = "#10b981" if fr >= 0 else "#ef4444"
    fr_sign = "+" if fr >= 0 else ""
    regime_col = "#10b981" if regime == "POS" else "#ef4444"
    r15_txt = "Rule#15 CLEARED - full weight" if not contra else "Rule#15 TRIGGERED - x0.5 weight"
    r15_col = "#10b981" if not contra else "#f59e0b"

    oracle_txt  = collision.get("oracle_verdict","N/A") if collision else "N/A"
    insight_txt = collision.get("key_insight","Claude API not configured") if collision else "Claude API not configured"
    next_trig   = collision.get("next_trigger","") if collision else ""

    # Options stats
    opts = data.get("options",{})
    skews = data.get("skew", {})
    gflips = data.get("gamma_flip", {})

    def opt_stats(exp):
        o = opts.get(exp,{})
        if not o: return 0,0,0,0,0
        tc = sum(float(v.get("call_oi",0)) for v in o.values())
        tp = sum(float(v.get("put_oi",0)) for v in o.values())
        pcr = round(tp/tc,3) if tc>0 else 0
        mc = max(o.items(), key=lambda x: x[1].get("call_oi",0), default=(0,{}))
        mp = max(o.items(), key=lambda x: x[1].get("put_oi",0), default=(0,{}))
        return tc, tp, pcr, int(mc[0]), int(mp[0])

    tc0,tp0,pcr0,cw0,pw0 = opt_stats(exp0)
    tc1,tp1,pcr1,cw1,pw1 = opt_stats(exp1)
    tc2,tp2,pcr2,cw2,pw2 = opt_stats(exp2)

    sk0 = skews.get(exp0); sk1 = skews.get(exp1); sk2 = skews.get(exp2)
    sk0_str = f"{sk0:+.1f}%" if sk0 is not None else "N/A"
    sk1_str = f"{sk1:+.1f}%" if sk1 is not None else "N/A"
    sk2_str = f"{sk2:+.1f}%" if sk2 is not None else "N/A"
    sk0_col = "#ef4444" if (sk0 or 0)>2 else ("#10b981" if (sk0 or 0)<-2 else "var(--mut)")
    sk1_col = "#ef4444" if (sk1 or 0)>2 else ("#10b981" if (sk1 or 0)<-2 else "var(--mut)")
    sk2_col = "#ef4444" if (sk2 or 0)>2 else ("#10b981" if (sk2 or 0)<-2 else "var(--mut)")
    gf0 = gflips.get(exp0, gf_main); gf1 = gflips.get(exp1, 0); gf2 = gflips.get(exp2, 0)

    # Scenario probabilities
    if sigma > 0:
        def ncdf(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
        c = uft_med
        p_A = round((1-ncdf((c+sigma*0.5-spot)/sigma))*100,1)
        p_B = round((ncdf((c+sigma*0.5-spot)/sigma)-ncdf((c-sigma*0.5-spot)/sigma))*100,1)
        p_C = round((ncdf((c-sigma*0.5-spot)/sigma)-ncdf((c-sigma-spot)/sigma))*100,1)
        p_D = round(ncdf((c-sigma-spot)/sigma)*100,1)
    else:
        p_A,p_B,p_C,p_D = 20,50,20,10

    # Countdown to exp0
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    try:
        import re
        m = re.match(r"(\d+)([A-Z]+)(\d+)", exp0)
        if m:
            from datetime import date, timedelta
            exp_date = date(2000+int(m.group(3)), months[m.group(2)], int(m.group(1)))
            today = date.today()
            days_left = (exp_date - today).days
            countdown_str = f"T-{days_left}d" if days_left > 0 else "EXPIRY TODAY"
        else:
            countdown_str = ""
    except:
        countdown_str = ""

    # Settlement log
    settlement_html = ""
    try:
        import os
        log_path = "data/settlement_log.json"
        if os.path.exists(log_path):
            with open(log_path) as f:
                log = _json.load(f)
            records = log.get("records", [])[-8:]  # last 8
            rows = ""
            for rec in reversed(records):
                snum = rec.get("snapshot_num","?")
                exp = rec.get("expiry","")
                pred = rec.get("predicted_median",0)
                actual = rec.get("actual_settlement")
                err_s = rec.get("error_sigma")
                if actual:
                    err_str = f"${abs(actual-pred):,.0f} ({err_s:.2f}s)" if err_s else f"${abs(actual-pred):,.0f}"
                    err_col = "#10b981" if (err_s or 99) < 0.5 else ("#f59e0b" if (err_s or 99) < 1.0 else "#ef4444")
                    actual_str = f"${actual:,.0f}"
                else:
                    err_str = "pending"
                    err_col = "var(--mut)"
                    actual_str = "-"
                rows += f'<tr><td>S{snum}</td><td>{exp}</td><td>${pred:,.0f}</td><td>{actual_str}</td><td style="color:{err_col}">{err_str}</td></tr>'
            if rows:
                settlement_html = f'''
<div style="padding:0 10px 10px">
<div class="card">
<div class="ct">SETTLEMENT LOG - UFT Accuracy Tracker</div>
<table>
<thead><tr><th>S#</th><th>Expiry</th><th>Predicted</th><th>Actual</th><th>Error</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<div style="font-size:9px;color:var(--mut);margin-top:6px">Optimizer: {len([r for r in log.get("records",[]) if r.get("actual_settlement")])}/10 samples for weight optimization</div>
</div>
</div>'''
    except: pass

    # Active Rules
    rules_triggered = []
    if contra: rules_triggered.append("R#15 Contradictory signal")
    if regime == "NEG": rules_triggered.append("R#10 NEG Regime - MM Amplifier")
    if fr > 0.005: rules_triggered.append("R#5 FR bullish (>0.005%)")
    elif fr < -0.005: rules_triggered.append("R#5 FR bearish (<-0.005%)")
    if (sk0 or 0) > 5: rules_triggered.append(f"R#Skew Strong bearish skew +{sk0:.1f}%")
    if spot > gf_main: rules_triggered.append(f"R#10 POS Regime (Spot above GF ${gf_main:,})")
    rules_html = "".join(f'<div style="font-size:9px;padding:2px 0;border-bottom:1px solid var(--border)">{r}</div>' for r in rules_triggered) if rules_triggered else '<div style="font-size:9px;color:var(--mut)">No major rules triggered</div>'

    # Time since last update
    try:
        from datetime import datetime, timezone
        last_ts = datetime.fromisoformat(data.get("timestamp","").replace("Z","+00:00"))
        now = datetime.now(timezone.utc)
        mins_ago = int((now - last_ts).total_seconds() / 60)
        age_str = f"{mins_ago}m ago" if mins_ago < 60 else f"{mins_ago//60}h {mins_ago%60}m ago"
        age_col = "var(--green)" if mins_ago < 30 else ("var(--yel)" if mins_ago < 120 else "var(--red)")
    except:
        age_str = "unknown"; age_col = "var(--mut)"

    # Options chain top strikes
    strike_rows = ""
    o0 = opts.get(exp0,{})
    if o0:
        top8 = sorted(o0.items(), key=lambda x: x[1].get("call_oi",0)+x[1].get("put_oi",0), reverse=True)[:8]
        for strike, v in sorted(top8, key=lambda x: x[0]):
            c_oi = float(v.get("call_oi",0)); p_oi = float(v.get("put_oi",0))
            pcr_s = round(p_oi/c_oi,2) if c_oi>0 else 0
            atm = ' style="background:rgba(59,130,246,.08)"' if abs(int(strike)-spot)<1500 else ""
            gf_mark = " *GF*" if abs(int(strike)-gf_main) < 500 else ""
            strike_rows += f'<tr{atm}><td>${int(strike):,}{gf_mark}</td><td>{c_oi:.0f}</td><td>{p_oi:.0f}</td><td>{pcr_s}</td></tr>'

    beh_w = float(weights.get("behavior",0.28)) * (0.5 if contra else 1.0)

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta name="google" content="notranslate">
<title>GEX Oracle S""" + str(snapshot_num) + """</title>
<style>
:root{--bg:#0a0e17;--panel:#111827;--border:#1e293b;--acc:#3b82f6;--green:#10b981;--red:#ef4444;--yel:#f59e0b;--pur:#8b5cf6;--cyan:#06b6d4;--txt:#e2e8f0;--mut:#64748b}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:Consolas,monospace;font-size:12px}
.hdr{background:linear-gradient(135deg,#0f172a,#1e1b4b);border-bottom:2px solid var(--acc);padding:12px 16px;display:flex;justify-content:space-between;align-items:flex-start}
.ht{font-size:16px;color:var(--acc);letter-spacing:2px;font-weight:bold}
.hs{color:var(--mut);font-size:10px;margin-top:2px}
.spot{font-size:24px;font-weight:bold;color:var(--yel)}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:10px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:0 10px 10px}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;padding:0 10px 10px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:10px}
.ct{font-size:9px;color:var(--mut);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid var(--border)}
.kv{font-size:18px;font-weight:bold;text-align:center;padding:6px 0}
.kl{font-size:9px;color:var(--mut);text-align:center;letter-spacing:1px}
.al{border-radius:5px;padding:7px 10px;margin:0 10px 8px;font-size:11px}
.row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border);font-size:10px}
.row:last-child{border-bottom:none}
.big{font-size:20px;font-weight:bold;color:var(--yel);text-align:center;padding:6px 0}
.sm{color:var(--mut);font-size:9px;text-align:center}
.pb{height:8px;background:var(--border);border-radius:4px;overflow:hidden;margin:2px 0 4px}
.pf{height:100%;border-radius:4px}
table{width:100%;border-collapse:collapse;font-size:10px}
th{color:var(--mut);text-align:right;padding:3px 5px;font-size:9px;border-bottom:1px solid var(--border)}
th:first-child{text-align:center}
td{padding:3px 5px;text-align:right;border-bottom:1px solid rgba(30,41,59,.5)}
td:first-child{text-align:center;font-weight:bold;color:var(--cyan)}
.foot{text-align:center;padding:8px;color:var(--mut);font-size:9px}
.skew-bar{height:6px;border-radius:3px;margin-top:2px}
</style>
</head>
<body>

<div class="hdr">
  <div>
    <div class="ht">GEX ORACLE AUTO S""" + str(snapshot_num) + """</div>
    <div class="hs">UFT v2.0 | """ + ts + """ UTC | 6h auto | <span style="color:""" + age_col + """">updated """ + age_str + """</span></div>
  </div>
  <div style="text-align:right">
    <div style="font-size:9px;color:var(--mut)">BTC/USDT PERP | Regime: <span style="color:""" + regime_col + """;font-weight:bold">""" + regime + """</span> | GF: $""" + f"{gf_main:,}" + """ | """ + countdown_str + """</div>
    <div class="spot">$""" + f"{spot:,.0f}" + """</div>
    <div style="font-size:10px;color:""" + fr_col + """">FR """ + fr_sign + f"{fr:.5f}" + """% | DVOL """ + f"{dvol:.2f}" + """%</div>
  </div>
</div>

<div class="al" style="background:rgba(59,130,246,.12);border:1px solid rgba(59,130,246,.4);margin-top:8px">
  Oracle: <strong>""" + oracle_txt + """</strong> | sigma=$""" + f"{sigma:,.0f}" + """ | UFT Median=<strong>$""" + f"{uft_med:,.0f}" + """</strong>
</div>
<div class="al" style="background:rgba(245,158,11,.08);border:1px solid """ + r15_col + """">""" + r15_txt + """</div>

<div class="g4">
  <div class="card"><div class="kv" style="color:var(--yel)">$""" + f"{spot:,.0f}" + """</div><div class="kl">SPOT</div></div>
  <div class="card"><div class="kv" style="color:""" + fr_col + """">""" + fr_sign + f"{fr:.5f}" + """%</div><div class="kl">FUNDING RATE</div></div>
  <div class="card"><div class="kv" style="color:""" + sk0_col + """">__SKEW0_STR__</div><div class="kl">SKEW (__EXP0__)</div></div>
  <div class="card"><div class="kv" style="color:var(--mut)">""" + f"{oi:.2f}" + """w</div><div class="kl">OPEN INTEREST</div></div>
</div>

<div class="g3">
  <div class="card">
    <div class="ct">MACD (3 Timeframes)</div>
    <div class="row"><span style="color:var(--cyan)">15m (30%)</span><span style="color:""" + c15 + """">""" + s15 + " " + f"{m15:+.2f}" + """</span><span style="color:var(--mut)">""" + f"{d15:+.0f}" + """</span></div>
    <div class="row"><span style="color:var(--cyan)">4h (62%)</span><span style="color:""" + c4h + """">""" + s4h + " " + f"{m4h:+.2f}" + """</span><span style="color:var(--mut)">""" + f"{d4h:+.0f}" + """</span></div>
    <div class="row"><span style="color:var(--cyan)">1D (70%)</span><span style="color:""" + c1d + """">""" + s1d + " " + f"{m1d:+.2f}" + """</span><span style="color:var(--mut)">""" + f"{d1d:+.0f}" + """</span></div>
  </div>
  <div class="card">
    <div class="ct">UFT v2.0 Equation</div>
    <div class="row"><span>GBM (x""" + f"{weights.get('gbm',0.40):.2f}" + """)</span><span>$""" + f"{comps.get('gbm',0):,.0f}" + """</span></div>
    <div class="row"><span>GEX (x""" + f"{weights.get('gex',0.10):.2f}" + """)</span><span>$""" + f"{comps.get('gex',0):,.0f}" + """</span></div>
    <div class="row"><span>Behavior (x""" + f"{beh_w:.2f}" + """)</span><span>$""" + f"{comps.get('behavior',0):,.0f}" + """</span></div>
    <div class="row"><span>Bayesian (x""" + f"{weights.get('bayesian',0.12):.2f}" + """)</span><span>$""" + f"{comps.get('bayesian',0):,.0f}" + """</span></div>
    <div class="row"><span>TimeDecay (x""" + f"{weights.get('timedecay',0.10):.2f}" + """)</span><span>$""" + f"{comps.get('timedecay',0):,.0f}" + """</span></div>
    <div class="big">$""" + f"{uft_med:,.0f}" + """</div>
    <div class="sm">Mode=$""" + f"{uft_mode:,.0f}" + """ | EMH=$""" + f"{spot:,.0f}" + """</div>
  </div>
  <div class="card">
    <div class="ct">Scenario Probability (__EXP0__)</div>
    <div style="font-size:10px;display:flex;justify-content:space-between"><span style="color:var(--green)">A: Bounce &gt;+0.5s</span><span style="color:var(--green)">""" + f"{p_A}" + """%</span></div>
    <div class="pb"><div class="pf" style="width:""" + f"{min(p_A,100)}" + """%;background:var(--green)"></div></div>
    <div style="font-size:10px;display:flex;justify-content:space-between"><span style="color:var(--yel)">B: Core range</span><span style="color:var(--yel)">""" + f"{p_B}" + """%</span></div>
    <div class="pb"><div class="pf" style="width:""" + f"{min(p_B,100)}" + """%;background:var(--yel)"></div></div>
    <div style="font-size:10px;display:flex;justify-content:space-between"><span style="color:var(--red)">C: Put Wall test</span><span style="color:var(--red)">""" + f"{p_C}" + """%</span></div>
    <div class="pb"><div class="pf" style="width:""" + f"{min(p_C,100)}" + """%;background:var(--red)"></div></div>
    <div style="font-size:10px;display:flex;justify-content:space-between"><span style="color:var(--red)">D: Tail &lt;-1s</span><span style="color:var(--red)">""" + f"{p_D}" + """%</span></div>
    <div class="pb"><div class="pf" style="width:""" + f"{min(p_D,100)}" + """%;background:#7f1d1d"></div></div>
  </div>
</div>

<div class="g2">
  <div>
    <div class="card" style="margin-bottom:10px">
      <div class="ct">GEX Structure + Regime</div>
      <div class="row"><span>Regime (__EXP0__)</span><span style="color:""" + regime_col + """;font-weight:bold">""" + regime + """</span></div>
      <div class="row"><span>Gamma Flip (__EXP0__)</span><span style="color:var(--yel)">$__GF0__</span></div>
      <div class="row"><span>Spot vs GF</span><span style="color:""" + regime_col + """">__SPOTGF__</span></div>
      <div class="row"><span>GEX Pin (__EXP0__)</span><span style="color:var(--yel)">$""" + f"{uft_mode:,.0f}" + """</span></div>
      <div class="row"><span>PCR __EXP0__ (C""" + f"{tc0:.0f}" + """/P""" + f"{tp0:.0f}" + """)</span><span>""" + f"{pcr0:.3f}" + """</span></div>
      <div class="row"><span>PCR __EXP1__ (C""" + f"{tc1:.0f}" + """/P""" + f"{tp1:.0f}" + """)</span><span>""" + f"{pcr1:.3f}" + """</span></div>
      <div class="row"><span>PCR __EXP2__ (C""" + f"{tc2:.0f}" + """/P""" + f"{tp2:.0f}" + """)</span><span>""" + f"{pcr2:.3f}" + """</span></div>
      <div class="row"><span>Call Wall __EXP0__</span><span style="color:var(--green)">$""" + f"{cw0:,}" + """</span></div>
      <div class="row"><span>Put Wall __EXP0__</span><span style="color:var(--red)">$""" + f"{pw0:,}" + """</span></div>
      <div class="row"><span>Call Wall __EXP1__</span><span style="color:var(--green)">$""" + f"{cw1:,}" + """</span></div>
      <div class="row"><span>Put Wall __EXP1__</span><span style="color:var(--red)">$""" + f"{pw1:,}" + """</span></div>
    </div>
    <div class="card">
      <div class="ct">Cross-Expiry Skew</div>
      <div class="row"><span>__EXP0__ (__COUNTDOWN__)</span><span style="color:__SK0_COL__">__SK0_STR__</span></div>
      <div style="background:var(--border);height:6px;border-radius:3px;margin:2px 0 6px;overflow:hidden"><div style="height:100%;width:__SK0_W__;background:__SK0_COL__;border-radius:3px"></div></div>
      <div class="row"><span>__EXP1__</span><span style="color:__SK1_COL__">__SK1_STR__</span></div>
      <div style="background:var(--border);height:6px;border-radius:3px;margin:2px 0 6px;overflow:hidden"><div style="height:100%;width:__SK1_W__;background:__SK1_COL__;border-radius:3px"></div></div>
      <div class="row"><span>__EXP2__</span><span style="color:__SK2_COL__">__SK2_STR__</span></div>
      <div style="background:var(--border);height:6px;border-radius:3px;margin:2px 0 6px;overflow:hidden"><div style="height:100%;width:__SK2_W__;background:__SK2_COL__;border-radius:3px"></div></div>
      <div style="font-size:9px;color:var(--mut);margin-top:4px">Positive skew = market pays premium for downside protection (bearish)</div>
    </div>
  </div>
  <div>
    <div class="card" style="margin-bottom:10px">
      <div class="ct">Options Chain __EXP0__ (Top by OI)</div>
      <table>
        <thead><tr><th>Strike</th><th>Call OI</th><th>Put OI</th><th>PCR</th></tr></thead>
        <tbody>""" + strike_rows + """</tbody>
      </table>
    </div>
    <div class="card" style="margin-bottom:10px">
      <div class="ct">Active Rules</div>
      """ + rules_html + """
    </div>
    <div class="card" style="border-color:var(--acc)">
      <div class="ct">Oracle Insight</div>
      <div style="font-size:10px;line-height:1.7">""" + insight_txt + """</div>
      """ + (f'<div style="font-size:9px;color:var(--cyan);margin-top:6px">Next: {next_trig}</div>' if next_trig else "") + """
    </div>
  </div>
</div>

""" + settlement_html + """

<div class="foot">GEX Oracle v2.0 | S""" + str(snapshot_num) + """ | 6h auto | Not investment advice</div>
</body>
</html>"""

    # Replace placeholders
    html = html.replace("__SKEW0_STR__", skew0_str if "skew0_str" in dir() else sk0_str)
    html = html.replace("__EXP0__", exp0).replace("__EXP1__", exp1).replace("__EXP2__", exp2)
    html = html.replace("__GF0__", f"{gf0:,}").replace("__GF1__", f"{gf1:,}").replace("__GF2__", f"{gf2:,}")
    html = html.replace("__SPOTGF__", f"{spot-gf_main:+,.0f}")
    html = html.replace("__COUNTDOWN__", countdown_str)
    html = html.replace("__SK0_STR__", sk0_str).replace("__SK1_STR__", sk1_str).replace("__SK2_STR__", sk2_str)
    html = html.replace("__SK0_COL__", sk0_col).replace("__SK1_COL__", sk1_col).replace("__SK2_COL__", sk2_col)
    sk0_w = f"{min(abs(sk0 or 0)*4, 100):.0f}%"
    sk1_w = f"{min(abs(sk1 or 0)*4, 100):.0f}%"
    sk2_w = f"{min(abs(sk2 or 0)*4, 100):.0f}%"
    html = html.replace("__SK0_W__", sk0_w).replace("__SK1_W__", sk1_w).replace("__SK2_W__", sk2_w)
    return html

def send_telegram(data, uft_result, collision, snapshot_num):
    """推送簡要摘要到Telegram"""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        print("⚠️  Telegram not configured, skipping")
        return

    spot = data["spot"]
    fr_pct = data["fr"] * 100
    oracle = collision.get("oracle_verdict", "N/A") if collision else "N/A"
    key_insight = collision.get("key_insight", "—") if collision else "—"
    next_trigger = collision.get("next_trigger", "—") if collision else "—"
    uft_med = uft_result["uft_median"]
    contradiction = uft_result["behavior_contradiction"]

    macd_1d = data["macd_1d"]
    m1d = "📈Bullish X" if macd_1d["dif"] > macd_1d["dea"] else "📉Bearish X"

    r15 = "⚠️矛盾(×0.5)" if contradiction else "✅一致(全權重)"

    msg = f"""⚡ *GEX Oracle S{snapshot_num}* 自動更新

💰 Spot: `${spot:,.0f}`
📊 FR: `{fr_pct:+.5f}%` | L/S: `{data.get('ls') or 'N/A'}` | OI: `{data['oi']:.2f}萬`

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
    print("✅ Telegram推送Done")

# ============================================================
# 6. 狀態持久化層
# ============================================================

def load_prev_data(db_path="data/gex_oracle.db"):
    """載入上次快照 - 優先從GitHub API讀取counter（Actions環境無持久化）"""
    os.makedirs("data", exist_ok=True)
    prev_num = 22
    prev_data = None

    # 先嘗試從GitHub API讀取（Actions環境每次是全新，本地檔案不存在）
    gh_token = os.environ.get("GH_TOKEN", "")
    gh_repo = os.environ.get("GH_REPO", "Z3X1/SideProject_WhaleTracker")
    if gh_token:
        try:
            import urllib.request
            url = f"https://api.github.com/repos/{gh_repo}/contents/data/snapshot_counter.json"
            req = urllib.request.Request(url, headers={
                "Authorization": f"token {gh_token}",
                "Accept": "application/vnd.github.v3+json"
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                import base64 as b64
                data_raw = json.loads(resp.read())
                counter = json.loads(b64.b64decode(data_raw["content"]).decode())
                prev_num = int(counter.get("last_snapshot", 22))
                prev_data = counter.get("last_data")
                print(f"Loaded counter from GitHub: S{prev_num}")
        except Exception as e:
            if "404" in str(e): prev_num = 23
            print(f"GitHub counter read: {e}")

    # fallback: 本地檔案
    counter_path = "data/snapshot_counter.json"
    if os.path.exists(counter_path) and prev_num == 22:
        try:
            with open(counter_path) as f:
                c = json.load(f)
                prev_num = int(c.get("last_snapshot", 22))
                prev_data = c.get("last_data")
        except:
            pass
    # 也嘗試SQLite
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, snapshot_num INTEGER,
            spot REAL, fr REAL, ls REAL, oi REAL, dvol REAL,
            uft_median REAL, oracle_verdict TEXT, data_json TEXT
        )""")
        conn.commit()
        row = conn.execute(
            "SELECT data_json, snapshot_num FROM snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row[1] > prev_num:
            prev_num = row[1]
            prev_data = json.loads(row[0])
    except:
        pass
    return prev_data, prev_num

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
    print(f"✅ S{snapshot_num} Saved")

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
    print(f"Snapshot: S{snapshot_num}")

    # 1. 優先讀取已抓取的市場數據（由gex_oracle_fetch.py生成）
    market_data_path = "data/oracle_market_data.json"
    if os.path.exists(market_data_path):
        print(f"📂 Loading pre-fetched data: {market_data_path}")
        with open(market_data_path) as f:
            data = json.load(f)
        print(f"  Spot: ${data.get('spot', 0):,.0f}")
        print(f"  FR: {data.get('fr', 0)*100:+.5f}%")
        print(f"  L/S: {data.get('ls') or 'N/A'}")
        print(f"  DVOL: {data.get('dvol', 46):.2f}%")
        # 格式標準化：將 data["macd"]["4h"] 轉為 data["macd_4h"]
        if "macd" in data and isinstance(data["macd"], dict):
            for tf_key, tf_new in [("15m","15m"), ("4h","4h"), ("1d","1d")]:
                if tf_key in data["macd"]:
                    data[f"macd_{tf_new}"] = data["macd"][tf_key]
            for tf_key, tf_new in [("15m","15m"), ("4h","4h"), ("1d","1d")]:
                if tf_key in data.get("ema", {}):
                    data[f"ema_{tf_new}"] = data["ema"][tf_key]
        # 確保spot存在
        if not data.get("spot") or data["spot"] == 0:
            data["spot"] = 60000
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
    print("✅ HTML生成Done → docs/index.html")

    # 5. Telegram推送
    send_telegram(data, uft_result, collision, snapshot_num)

    # 6. 記錄預測到settlement_log（UFT動態優化）
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from uft_optimizer import record_prediction, check_and_record_settlement, optimize_weights
        expiries_list = data.get("expiries", ["3JUL26","31JUL26","25SEP26"])
        record_prediction(
            snapshot_num=snapshot_num,
            expiry=expiries_list[0] if expiries_list else "N/A",
            predicted_median=uft_result["uft_median"],
            predicted_mode=uft_result["uft_mode"],
            components=uft_result.get("components", {}),
            weights=data.get("uft_weights", {"gbm":0.40,"gex":0.10,"behavior":0.28,"bayesian":0.12,"timedecay":0.10}),
            signals={
                "fr": data.get("fr"),
                "skew": uft_result.get("skew_main"),
                "dvol": data.get("dvol"),
                "pcr_main": uft_result.get("gex",{}).get("pcr"),
                "macd_4h": (data.get("macd_4h") or data.get("macd",{}).get("4h",{})).get("macd"),
                "regime_pos": 1.0 if uft_result.get("regime","POS")=="POS" else 0.0,
                "gamma_flip": float(uft_result.get("gamma_flip", 0) or 0),
                "contradiction": 1.0 if uft_result.get("behavior_contradiction", False) else 0.0,
            },
            sigma=uft_result.get("sigma", 4000)
        )
        # 檢查是否有到期日需要記錄結算價
        check_and_record_settlement()
        # 若有足夠樣本，自動優化權重
        new_weights = optimize_weights(min_samples=10)
        if new_weights:
            data["uft_weights"] = new_weights
            print(f"UFT weights: {new_weights}")
    except Exception as e:
        print(f"Optimizer error: {e}")

    # 7. 保存狀態
    save_snapshot(data, uft_result, collision, snapshot_num)

    print(f"\n✅ S{snapshot_num} Done")

if __name__ == "__main__":
    main()
