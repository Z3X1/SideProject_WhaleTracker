#!/usr/bin/env python3
# GEX Oracle Auto Engine v2.0




import os, json, math, time, requests, sqlite3
from datetime import datetime, timezone

# ============================================================
# 1. 數據抓取層
# ============================================================


# ── Helper: 解析到期日字串 → 剩餘天數（單一真實來源）────────────────
import re as _re_exp
_MONTHS_EXP = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
               "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def parse_days_to_expiry(expiry_str):
    """'3JUL26' → 剩餘天數(int)，最小1天"""
    from datetime import date as _date_exp
    try:
        m = _re_exp.match(r"(\d+)([A-Z]+)(\d+)", expiry_str.upper())
        if m:
            d = _date_exp(2000 + int(m.group(3)), _MONTHS_EXP[m.group(2)], int(m.group(1)))
            return max(1, (d - _date_exp.today()).days)
    except Exception:
        pass
    return 7  # fallback


def fetch_binance_spot():
    # Spot價格
    r = requests.get(
        "https://fapi.binance.com/fapi/v1/ticker/price",
        params={"symbol": "BTCUSDT"}, timeout=10
    )
    d = r.json()
    return float(d.get("price") or d.get("markPrice") or d.get("lastPrice"))

def fetch_binance_fr():
    # 資金費率
    r = requests.get(
        "https://fapi.binance.com/fapi/v1/premiumIndex",
        params={"symbol": "BTCUSDT"}, timeout=10
    )
    d = r.json()
    return float(d.get("lastFundingRate") or d.get("interestRate") or 0)

def fetch_binance_oi():
    # 持倉量(萬張)
    r = requests.get(
        "https://fapi.binance.com/fapi/v1/openInterest",
        params={"symbol": "BTCUSDT"}, timeout=10
    )
    d = r.json()
    oi = d.get("openInterest") or d.get("sumOpenInterest") or 0
    return float(oi) / 10000

def fetch_binance_ls():
    # 大戶多空比
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
    # K線數據(用於計算EMA/MACD)
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
    # 指數移動平均
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_macd(prices):
    # MACD(12,26,9)
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
    # DVOL(BTC期權隱含波動率指數)
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
    # 抓取指定到期日的期權鏈
    # expiry_label: 例如 "3JUL26", "31JUL26", "25SEP26"
    # 返回: {strike: {call_oi, put_oi, call_iv, put_iv}}
    # Deribit到期日格式轉換(3JUL26 → 26JUL3 → 3JUL26格式)
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
    # 主數據收集函數
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

    # 期權鏈(三個到期日)
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
    # 計算GEX Structure:Pin水位,PCR,Gamma Flip
    if not options:
        return {"pin": spot, "pcr": 1.0, "gamma_flip": spot - 2000}

    # PCR(OI加權)
    total_call_oi = sum(v["call_oi"] for v in options.values())
    total_put_oi = sum(v["put_oi"] for v in options.values())
    pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 1.0

    # ATM Put Wall(最大Put OI在Spot附近)
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
    # 行為信號計算
    # Rule#15: FR正+L/S同升=全權重;矛盾=×0.5
    fr_direction = 1 if fr > 0 else -1
    ls_direction = 1 if (prev_ls is None or ls > prev_ls) else -1

    # 矛盾檢測
    contradiction = (fr_direction != ls_direction)
    weight = 0.7 if contradiction else 1.0

    # 信號強度
    fr_signal = min(abs(fr) / 0.0001, 1.0) * fr_direction  # 正規化
    ls_signal = (ls - 2.0) / 0.5  # 2.0為中性基準

    raw_signal = (fr_signal * 0.4 + ls_signal * 0.6) * weight
    return raw_signal, contradiction, weight

def calc_uft(data, prev_data=None):
    # UFT統一場方程計算
    spot = data["spot"]
    dvol = data["dvol"] / 100
    T = 7 / 365  # 暫時，後面動態覆蓋
    sigma = spot * dvol * math.sqrt(T)

    # GEX成分
    opts_3jul = data.get("options_3JUL26", {})
    gex = calc_gex_structure(opts_3jul, spot)
    gex_center = gex["pin"]

    # BehaviorSignal成分(L/S已移除,用FR+PCR+Skew)
    expiries = data.get("expiries", ["3JUL26","31JUL26","25SEP26"])
    _exp0 = data.get("expiries", ["3JUL26","31JUL26","25SEP26"])[0]
    _dl = parse_days_to_expiry(_exp0)   # 單一真實來源
    T = _dl / 365                       # 動態T覆蓋
    sigma = spot * dvol * math.sqrt(T)  # 重算sigma
    fr = data.get("fr", 0)
    oi_change = (data.get("oi",0) - prev_data.get("oi",0)) if prev_data else 0
    skew_main = data.get("skew", {}).get(expiries[0] if expiries else "3JUL26", 0) or 0
    # FR信號方向
    fr_signal = 1 if fr > 0 else -1
    fr_strength = min(abs(fr) / 0.0001, 1.0)
    # Skew信號(正skew=偏空,負skew=偏多)
    skew_signal = -1 if skew_main > 2 else (1 if skew_main < -2 else 0)
    # T≤7d時Skew信號衰減（結算前Skew多為hedge非方向）
    _skew_decay=0.5 if _dl<=7 else 1.0
    skew_signal=skew_signal*_skew_decay
    # PCR ATM信號(更精確:用ATM PCR而非全局PCR)
    exp_main = expiries[0] if expiries else "3JUL26"
    pcr_atm = data.get(f"pcr_atm_{exp_main}", 0)
    pcr_otm = data.get(f"pcr_otm_{exp_main}", 0)
    # ATM PCR更能反映即時方向
    pcr_use = pcr_atm if pcr_atm > 0 else (pcr_ratio := sum(float(v.get("put_oi",0)) for v in data.get("options",{}).get(exp_main,{}).values()) / max(sum(float(v.get("call_oi",0)) for v in data.get("options",{}).get(exp_main,{}).values()), 1))
    pcr_signal = -1 if pcr_use > 1.3 else (1 if pcr_use < 0.6 else 0)

    # OI變化方向(新增信號)
    oi_change = float(data.get("oi_change", 0) or 0)
    oi_signal = 0
    if abs(oi_change) > 0.1:  # 顯著變化
        oi_signal = -1 if oi_change > 0 else 1  # OI增加+FR正=空頭主導(已處理FR方向)

    # Perp Basis(新增信號)
    basis_pct = float(data.get("perp_basis_pct", 0) or 0)
    basis_signal = 1 if basis_pct > 0.05 else (-1 if basis_pct < -0.05 else 0)

    whale_signal = 0
    try:
        import os as _osw, sqlite3 as _sq
        _db = "data/whale_tracker.db"
        if _osw.path.exists(_db):
            _conn = _sq.connect(_db)
            _sql = ("SELECT SUM(CASE WHEN direction='in' THEN amount ELSE -amount END)"
                   " FROM transfers WHERE timestamp > datetime('now', '-24 hours') AND amount > 100")
            _row = _conn.execute(_sql).fetchone()
            _conn.close()
            if _row and _row[0]:
                _net = float(_row[0])
                whale_signal = -1 if _net > 500 else (1 if _net < -500 else 0)
    except: pass
    raw_signal = (fr_signal * fr_strength * 0.35 + skew_signal * 0.25
                + pcr_signal * 0.20 + oi_signal * 0.10
                + basis_signal * 0.05 + whale_signal * 0.05)
    behavior_signal = max(-1, min(1, raw_signal))
    contradiction = bool(fr > 0.005 and skew_main > 5)
    # behavior 懲罰因子（不縮減權重，縮減信號強度）
    behavior_penalty = 0.7 if contradiction else 1.0
    import math as _m2
    exp_main = expiries[0] if expiries else "3JUL26"
    T_main = _dl / 365
    sigma_main = spot * (data.get("dvol", 50) / 100) * _m2.sqrt(T_main)
    gf_dict = data.get("gamma_flip", {})
    gamma_flip_main = int(gf_dict.get(exp_main, gex_center) or gex_center)
    regime = "POS" if spot > gamma_flip_main else "NEG"
    # Bayesian 動態收斂：T 越小越往 GEX Pin 收斂
    _t_factor = max(0.0, min(1.0, _dl / 30))
    _skew_factor = min(1.0, abs(skew_main) / 10) if skew_main != 0 else 0.3
    _regime_signal = 1 if regime == "POS" else -1
    _bayes_offset = _regime_signal * _skew_factor * _t_factor * 0.4
    bayes_center = spot + _bayes_offset * sigma_main
    # ── 權重取得：優先用 Regime 分層最優權重（L3）─────────────
    # 嘗試從 optimizer 取 regime-specific 最優權重
    try:
        import sys as _sys_uft
        _sys_uft.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from uft_optimizer import get_regime_weights as _grw
        bw = _grw(regime)  # POS/NEG 各自最優
    except Exception:
        bw = {"gbm": 0.30, "gex": 0.18, "behavior": 0.28, "bayesian": 0.12, "timedecay": 0.12}
    # 允許 data["uft_weights"] 覆蓋（human override）
    _override = data.get("uft_weights", {})
    if _override and abs(sum(_override.values()) - 1.0) < 0.02:
        bw = dict(_override)
    # 最終歸一（防止浮點誤差）
    _total = sum(bw.values())
    bw = {k: v / _total for k, v in bw.items()}
    # ── UFT 方程式（所有項結構統一：weight × center_estimate）────
    # behavior 信號縮減在 center_estimate 內（signal * penalty），不縮減 weight
    behavior_center = spot + behavior_signal * behavior_penalty * sigma_main
    uft = (bw["gbm"] * spot
           + bw["gex"] * gex_center
           + bw["behavior"] * behavior_center
           + bw["bayesian"] * bayes_center
           + bw["timedecay"] * gex_center)
    return {
        "uft_median": round(uft, 2), "uft_mode": gex_center, "uft_emh": spot,
        "sigma": round(sigma_main, 2), "regime": regime, "gamma_flip": gamma_flip_main,
        "behavior_contradiction": contradiction, "behavior_penalty": behavior_penalty,
        "skew_main": skew_main, "uft_weights": bw,
        "components": {
            "gbm":       round(bw["gbm"] * spot, 2),
            "gex":       round(bw["gex"] * gex_center, 2),
            "behavior":  round(bw["behavior"] * behavior_center, 2),
            "bayesian":  round(bw["bayesian"] * bayes_center, 2),
            "timedecay": round(bw["timedecay"] * gex_center, 2),
        }
    }

def generate_rule_based_collision(data, uft_result):
    """當Claude API不可用時，用規則引擎自動生成結論"""
    spot=float(data.get("spot",0))
    fr=float(data.get("fr",0))*100
    dvol=float(data.get("dvol",0))
    regime=uft_result.get("regime","POS")
    uft_med=float(uft_result.get("uft_median",spot))
    sigma=float(uft_result.get("sigma",0))
    contra=uft_result.get("behavior_contradiction",False)
    skew_main=float(uft_result.get("skew_main",0) or 0)
    gf=int(uft_result.get("gamma_flip",spot) or spot)
    expiries=data.get("expiries",["3JUL26"])
    exp0=expiries[0] if expiries else "3JUL26"
    dl = parse_days_to_expiry(exp0)
    opts=data.get("options",{}).get(exp0,{})
    tc=sum(float(v.get("call_oi",0)) for v in opts.values())
    tp=sum(float(v.get("put_oi",0)) for v in opts.values())
    pcr=round(tp/tc,2) if tc>0 else 1.0
    macd_1d=data.get("macd_1d") or data.get("macd",{}).get("1d",{})
    dif_1d=float(macd_1d.get("dif",0))
    macd_4h=data.get("macd_4h") or data.get("macd",{}).get("4h",{})
    dif_4h=float(macd_4h.get("dif",0))
    # 決定Oracle Verdict
    bull_pts=0; bear_pts=0
    if regime=="POS": bull_pts+=2
    else: bear_pts+=2
    if fr>0.005: bull_pts+=1
    elif fr<-0.005: bear_pts+=1
    if skew_main>5: bear_pts+=2
    elif skew_main<-2: bull_pts+=1
    if dif_1d<-500: bear_pts+=2
    elif dif_1d>500: bull_pts+=1
    if dif_4h>0: bull_pts+=1
    else: bear_pts+=1
    if pcr>1.3: bear_pts+=1
    elif pcr<0.6: bull_pts+=1
    if contra: bear_pts+=1
    total=bull_pts+bear_pts
    bull_pct=round(bull_pts/total,2) if total>0 else 0.5
    if bull_pct>=0.6: verdict=f"BULL {bull_pct:.2f}"
    elif bull_pct<=0.4: verdict=f"BEAR {1-bull_pct:.2f}"
    else: verdict=f"NEUTRAL {bull_pct:.2f}"
    # Key Insight
    insights=[]
    if dl<=7: insights.append(f"T-{dl}d結算Pin博弈：GEX Pin ${uft_med:,.0f} vs Spot ${spot:,.0f}（差${abs(spot-uft_med):,.0f}）")
    if skew_main>15: insights.append(f"Skew {skew_main:+.1f}% 極度偏空，市場為下行付高溢價")
    elif skew_main>5: insights.append(f"Skew {skew_main:+.1f}% 偏空，空方防禦需求主導")
    if regime=="POS" and abs(spot-gf)<sigma*0.3: insights.append(f"Spot距GF僅{abs(spot-gf):,.0f}，Regime轉換風險高")
    elif regime=="POS": insights.append(f"POS Regime穩定，GF ${gf:,} 距Spot {abs(spot-gf):,.0f}（{abs(spot-gf)/gf*100:.1f}%）")
    else: insights.append(f"NEG Regime：造市商放大波動，GF ${gf:,} 為關鍵收復目標")
    if contra: insights.append("FR多/Skew空矛盾（Rule#15觸發），行為信號權重×0.5")
    if dif_1d<-1000: insights.append(f"1D MACD DIF={dif_1d:.0f} 深度負值，中期趨勢偏空")
    key_insight=" | ".join(insights[:3]) if insights else f"UFT Median ${uft_med:,.0f}，σ=${sigma:,.0f}，情境B（核心區間）概率最高"
    # Next Trigger
    triggers=[]
    if dl<=3: triggers.append(f"T-{dl}d結算前最後窗口：監控Pin是否移位")
    if regime=="POS" and spot-gf<sigma*0.5: triggers.append(f"Spot跌破GF ${gf:,} → NEG Regime硬觸發")
    if fr>0: triggers.append("FR穿越0%（多→空成本轉換）")
    else: triggers.append("FR穿越-0.01%（空頭信念加深）或反彈穿越0%")
    if skew_main>10: triggers.append("Skew收縮至+10%以下（空方壓力緩解信號）")
    next_trigger=" | ".join(triggers[:2]) if triggers else "監控FR/Skew/Spot vs Pin"
    return {"oracle_verdict":verdict,"key_insight":key_insight,"next_trigger":next_trigger}

def call_claude_collision(data, uft_result):
    api_key=os.environ.get("ANTHROPIC_API_KEY","")
    # 先嘗試Claude API
    if api_key:
        try:
            import urllib.request as _ur
            spot=float(data.get("spot",0)); fr=float(data.get("fr",0))*100
            dvol=float(data.get("dvol",0)); uft_med=float(uft_result.get("uft_median",0))
            sigma=float(uft_result.get("sigma",0)); regime=uft_result.get("regime","POS")
            exp=data.get("expiries",["3JUL26"])[0]
            sk=data.get("skew",{}).get(exp,0) or 0
            prompt=(
                f"Spot: ${spot:,.0f} | FR: {fr:+.5f}% | DVOL: {dvol:.2f}%\n"
                f"Regime: {regime} | Skew {exp}: {sk:+.1f}%\n"
                f"UFT Median: ${uft_med:,.0f} | Sigma: ${sigma:,.0f}\n"
                "Run 4-layer adversarial collision. Output JSON only: "
                "{""oracle_verdict"":""BULL/BEAR 0.XX"",""key_insight"":""one sentence"",""next_trigger"":""next signal""}"
            )
            body=json.dumps({"model":"claude-haiku-4-5-20251001","max_tokens":300,
                "messages":[{"role":"user","content":prompt}]}).encode()
            req=_ur.Request("https://api.anthropic.com/v1/messages",data=body,
                headers={"x-api-key":api_key,"anthropic-version":"2023-06-01","content-type":"application/json"},method="POST")
            with _ur.urlopen(req,timeout=30) as resp: result=json.loads(resp.read())
            if "error" not in result:
                text="".join(b.get("text","") for b in result.get("content",[]) if b.get("type")=="text").strip()
                if "{" in text: text=text[text.find("{"):text.rfind("}")+1]
                parsed=json.loads(text)
                print("Claude collision OK"); return parsed
        except Exception as e: print(f"Claude API fallback to rules: {e}")
    # Fallback：規則引擎
    print("Using rule-based collision engine")
    return generate_rule_based_collision(data, uft_result)

def generate_html(data, uft_result, collision, snapshot_num):
    import math, os as _os2
    from datetime import datetime, timezone, date

    spot=float(data.get('spot') or 0); fr=float(data.get('fr') or 0)*100
    oi=float(data.get('oi') or 0); dvol=float(data.get('dvol') or 0)
    ts=str(data.get('timestamp',''))[:16].replace('T',' ')
    expiries=data.get('expiries',['3JUL26','31JUL26','25SEP26'])
    exp0=expiries[0] if expiries else 'N/A'
    exp1=expiries[1] if len(expiries)>1 else 'N/A'
    exp2=expiries[2] if len(expiries)>2 else 'N/A'
    uft_med=float(uft_result.get('uft_median') or spot)
    uft_mode=float(uft_result.get('uft_mode') or spot)
    sigma=float(uft_result.get('sigma') or 0)
    contra=bool(uft_result.get('behavior_contradiction',False))
    comps=uft_result.get('components',{})
    regime=uft_result.get('regime','POS')
    gf_main=int(uft_result.get('gamma_flip',uft_mode) or uft_mode)
    weights=uft_result.get('uft_weights',{'gbm':0.30,'gex':0.18,'behavior':0.28,'bayesian':0.12,'timedecay':0.12})
    def ms(kf,kn):
        m=data.get(kf) or data.get('macd',{}).get(kn,{})
        dif=float(m.get('dif',0)); dea=float(m.get('dea',0)); mac=float(m.get('macd',0))
        b=dif>dea
        return ('BULL X' if b else 'BEAR X'),('#10b981' if b else '#ef4444'),dif,dea,mac
    s15,c15,d15,e15,m15=ms('macd_15m','15m')
    s4h,c4h,d4h,e4h,m4h=ms('macd_4h','4h')
    s1d,c1d,d1d,e1d,m1d=ms('macd_1d','1d')
    frc='#10b981' if fr>=0 else '#ef4444'; frs='+' if fr>=0 else ''
    rc='#10b981' if regime=='POS' else '#ef4444'
    r15t='Rule#15 CLEARED' if not contra else 'Rule#15 TRIGGERED - x0.5'
    r15c='#10b981' if not contra else '#f59e0b'
    ot=collision.get('oracle_verdict','N/A') if collision else 'N/A'
    it=collision.get('key_insight','Claude API not configured') if collision else 'Claude API not configured'
    nt=collision.get('next_trigger','') if collision else ''
    opts=data.get('options',{}); sk=data.get('skew',{}); gfmap=data.get('gamma_flip',{})
    def os2(exp):
        o=opts.get(exp,{})
        if not o: return 0,0,0,0,0
        tc=sum(float(v.get('call_oi',0)) for v in o.values())
        tp=sum(float(v.get('put_oi',0)) for v in o.values())
        mc=max(o.items(),key=lambda x:x[1].get('call_oi',0),default=(0,{}))
        mp=max(o.items(),key=lambda x:x[1].get('put_oi',0),default=(0,{}))
        return tc,tp,round(tp/tc,3) if tc>0 else 0,int(mc[0]),int(mp[0])
    tc0,tp0,pcr0,cw0,pw0=os2(exp0)
    tc1,tp1,pcr1,cw1,pw1=os2(exp1)
    tc2,tp2,pcr2,cw2,pw2=os2(exp2)
    sk0=sk.get(exp0); sk1=sk.get(exp1); sk2=sk.get(exp2)
    s0s=f'{sk0:+.1f}%' if sk0 is not None else 'N/A'
    s1s=f'{sk1:+.1f}%' if sk1 is not None else 'N/A'
    s2s=f'{sk2:+.1f}%' if sk2 is not None else 'N/A'
    s0c='#ef4444' if (sk0 or 0)>2 else ('#10b981' if (sk0 or 0)<-2 else 'var(--mut)')
    s1c='#ef4444' if (sk1 or 0)>2 else ('#10b981' if (sk1 or 0)<-2 else 'var(--mut)')
    s2c='#ef4444' if (sk2 or 0)>2 else ('#10b981' if (sk2 or 0)<-2 else 'var(--mut)')
    gf0=gfmap.get(exp0,gf_main); gf1=gfmap.get(exp1,0); gf2=gfmap.get(exp2,0)
    if sigma>0:
        def nc(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
        pA=round((1-nc((uft_med+sigma*.5-spot)/sigma))*100,1)
        pB=round((nc((uft_med+sigma*.5-spot)/sigma)-nc((uft_med-sigma*.5-spot)/sigma))*100,1)
        pC=round((nc((uft_med-sigma*.5-spot)/sigma)-nc((uft_med-sigma-spot)/sigma))*100,1)
        pD=round(nc((uft_med-sigma-spot)/sigma)*100,1)
    else: pA,pB,pC,pD=20,50,20,10
    dl = parse_days_to_expiry(exp0)
    cd = f'T-{dl}d' if dl > 0 else 'TODAY'
    try:
        now=datetime.now(timezone.utc)
        nxt=min((h for h in [0,8,16] if h>now.hour),default=24)
        hfr=nxt-now.hour; facc=round(fr*(8-hfr),6); fns=f'{hfr}h{now.minute:02d}m'
    except: facc=0; fns='N/A'
    st=''; stc='var(--mut)'; st3=''
    try:
        if _os2.path.exists('data/skew_history.json'):
            import json as j3
            with open('data/skew_history.json') as f3: shd=j3.load(f3)
            if len(shd)>=2:
                sp2=shd[-2].get('skew',{}).get(exp0); sc2=shd[-1].get('skew',{}).get(exp0)
                if sp2 and sc2:
                    dd=sc2-sp2
                    st=(f'^{abs(dd):.1f}%' if dd>0 else f'v{abs(dd):.1f}%') if abs(dd)>0.5 else 'stable'
                    stc='#ef4444' if dd>0 else '#10b981'
            if len(shd)>=3:
                sv=[h.get('skew',{}).get(exp0) for h in shd[-3:] if h.get('skew',{}).get(exp0)]
                st3=f'{sv[0]:.1f}->{sv[1]:.1f}->{sv[2]:.1f}%' if len(sv)==3 else ''
    except: pass
    gfd=abs(spot-gf_main); gfs=gfd/sigma if sigma>0 else 0
    gfss='STABLE' if gfs>0.3 else 'UNSTABLE'; gfsc='#10b981' if gfs>0.3 else '#f59e0b'
    pd2=abs(spot-uft_mode); mpv=int(data.get(f'max_pain_{exp0}',uft_mode) or uft_mode)
    psc=max(0,100-pd2/10-abs(spot-mpv)/20)
    pr='HIGH' if psc>70 else ('MEDIUM' if psc>40 else 'LOW')
    prc='#ef4444' if psc>70 else ('#f59e0b' if psc>40 else '#10b981')
    cla=False; cli=[]; dcl=dl
    if 0<dl<=7:
        cla=True
        cli=[
            ('FR stable','OK' if abs(fr)>0.002 else '?','#10b981' if abs(fr)>0.002 else '#f59e0b'),
            ('Put Wall holding','OK' if spot>pw0 else 'X','#10b981' if spot>pw0 else '#ef4444'),
            ('Spot above GF','OK' if regime=="POS" else 'X','#10b981' if regime=="POS" else '#ef4444'),
            ('GEX Pin stable','OK' if pd2<500 else '?','#10b981' if pd2<500 else '#f59e0b'),
            ('Skew not expanding','OK' if '^' not in st else '!','#10b981' if '^' not in st else '#ef4444'),
            (f'GF POS (${gf_main:,})','OK' if regime=="POS" else 'X','#10b981' if regime=="POS" else '#ef4444'),
        ]
    try:
        ts2=data.get('timestamp','')
        if ts2: age=int((datetime.now(timezone.utc)-datetime.fromisoformat(ts2.replace('Z','+00:00'))).total_seconds()/60)
        else: age=0
        ags=f'{age}m ago' if age<60 else f'{age//60}h{age%60:02d}m ago'
        agc='var(--green)' if age<30 else ('var(--yel)' if age<120 else 'var(--red)')
    except: ags='unknown'; agc='var(--mut)'
    ru=[]
    if contra: ru.append('R#15 Contradictory signal')
    if regime=='NEG': ru.append('R#10 NEG Regime - MM Amplifier')
    if regime=='POS': ru.append(f'R#10 POS Regime (GF ${gf_main:,})')
    if fr>0.005: ru.append('R#5 FR bullish (>0.005%)')
    elif fr<-0.005: ru.append('R#5 FR bearish (<-0.005%)')
    if (sk0 or 0)>5: ru.append(f'R#Skew Strong bearish +{sk0:.1f}%')
    dif1=float((data.get('macd_1d') or data.get('macd',{}).get('1d',{})).get('dif',0))
    if dif1<-1000: ru.append(f'R#2 1D DIF deeply negative ({dif1:.0f})')
    aiv=float(data.get(f'atm_iv_{exp0}',dvol) or dvol); ivp=aiv-dvol
    if abs(ivp)>8: ru.append(f'R#IV ATM-DVOL divergence: {ivp:+.1f}%')
    patm=float(data.get(f'pcr_atm_{exp0}',0) or 0); potm=float(data.get(f'pcr_otm_{exp0}',0) or 0)
    if patm>1.0 and potm<0.5: ru.append(f'R#PCR ATM({patm:.2f}) vs OTM({potm:.2f}) mixed')
    if abs(mpv-uft_mode)>1000: ru.append(f'R#MaxPain-GEXPin ${abs(mpv-uft_mode):,.0f}')
    ruh=''.join(f'<div class="row"><span>{x}</span></div>' for x in ru) if ru else '<div style="font-size:9px;color:var(--mut)">None</div>'
    sr=''
    o0=opts.get(exp0,{})
    if o0:
        t8=sorted(o0.items(),key=lambda x:x[1].get('call_oi',0)+x[1].get('put_oi',0),reverse=True)[:8]
        for stk,v in sorted(t8,key=lambda x:x[0]):
            co=float(v.get('call_oi',0)); po=float(v.get('put_oi',0))
            ci=float(v.get('call_iv',0)); pi=float(v.get('put_iv',0))
            ps=round(po/co,2) if co>0 else 0
            ivs=f'{ci:.0f}/{pi:.0f}' if ci>0 or pi>0 else '-'
            ivc='#ef4444' if max(ci,pi)>60 else ('var(--yel)' if max(ci,pi)>45 else 'var(--mut)')
            at=' style="background:rgba(59,130,246,.08)"' if abs(int(stk)-spot)<1500 else ''
            gm=' *GF*' if abs(int(stk)-gf_main)<500 else ''
            mm=' *MP*' if abs(int(stk)-mpv)<500 else ''
            sr+=f'<tr{at}><td>${int(stk):,}{gm}{mm}</td><td>{co:.0f}</td><td>{po:.0f}</td><td>{ps}</td><td style="color:{ivc}">{ivs}</td></tr>'
    slh=''
    try:
        if _os2.path.exists('data/settlement_log.json'):
            import json as j4
            with open('data/settlement_log.json') as f4: lg=j4.load(f4)
            rs=lg.get('records',[])[-8:]; rws=''
            for rec in reversed(rs):
                sn=rec.get('snapshot_num','?'); ex=rec.get('expiry','')
                prv=rec.get('predicted_median',0); ac=rec.get('actual_settlement'); es=rec.get('error_sigma')
                if ac:
                    es2=f'${abs(ac-prv):,.0f} ({es:.2f}s)' if es else f'${abs(ac-prv):,.0f}'
                    ec='#10b981' if (es or 99)<0.5 else ('#f59e0b' if (es or 99)<1.0 else '#ef4444')
                    as2=f'${ac:,.0f}'
                else: es2='pending'; ec='var(--mut)'; as2='-'
                rws+=f'<tr><td>S{sn}</td><td>{ex}</td><td>${prv:,.0f}</td><td>{as2}</td><td style="color:{ec}">{es2}</td></tr>'
            nd=len([x for x in lg.get('records',[]) if x.get('actual_settlement')])
            slh=(f'<div style="padding:0 10px 10px"><div class="card"><div class="ct">SETTLEMENT LOG - UFT ACCURACY TRACKER</div>'
                 f'<table><thead><tr><th>S#</th><th>Expiry</th><th>Predicted</th><th>Actual</th><th>Error</th></tr></thead>'
                 f'<tbody>{rws}</tbody></table>'
                 f'<div style="font-size:9px;color:var(--mut);margin-top:4px">Optimizer: {nd}/10 samples for weight optimization</div></div></div>')
    except: pass
    clh=''
    if cla:
        cr=''.join(f'<div class="row"><span>{i}</span><span style="color:{c}">{s}</span></div>' for i,s,c in cli)
        clh=(f'<div style="padding:0 10px 10px"><div class="card" style="border-color:#f59e0b">'
             f'<div class="ct" style="color:#f59e0b">T-{dcl}d PRE-SETTLEMENT CHECKLIST ({exp0})</div>{cr}</div></div>')
    bw=float(weights.get('behavior',0.28))*(0.5 if contra else 1.0)
    sw0=f'{min(abs(sk0 or 0)*4,100):.0f}%'; sw1=f'{min(abs(sk1 or 0)*4,100):.0f}%'; sw2=f'{min(abs(sk2 or 0)*4,100):.0f}%'
    css='<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><meta name="google" content="notranslate">'
    css+=f'<title>GEX Oracle S{snapshot_num}</title>'
    css+=('<style>:root{--bg:#0a0e17;--panel:#111827;--border:#1e293b;--acc:#3b82f6;--green:#10b981;--red:#ef4444;'
          '--yel:#f59e0b;--pur:#8b5cf6;--cyan:#06b6d4;--txt:#e2e8f0;--mut:#64748b}'
          '*{box-sizing:border-box;margin:0;padding:0}body{background:var(--bg);color:var(--txt);font-family:Consolas,monospace;font-size:12px}'
          '.hdr{background:linear-gradient(135deg,#0f172a,#1e1b4b);border-bottom:2px solid var(--acc);padding:12px 16px;display:flex;justify-content:space-between;align-items:flex-start}'
          '.ht{font-size:16px;color:var(--acc);letter-spacing:2px;font-weight:bold}.hs{color:var(--mut);font-size:10px;margin-top:2px}'
          '.spot{font-size:24px;font-weight:bold;color:var(--yel)}'
          '.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:10px}'
          '.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:0 10px 10px}'
          '.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;padding:0 10px 10px}'
          '.card{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:10px}'
          '.ct{font-size:9px;color:var(--mut);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid var(--border)}'
          '.kv{font-size:18px;font-weight:bold;text-align:center;padding:6px 0}.kl{font-size:9px;color:var(--mut);text-align:center;letter-spacing:1px}'
          '.al{border-radius:5px;padding:7px 10px;margin:0 10px 8px;font-size:11px}'
          '.row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border);font-size:10px}.row:last-child{border-bottom:none}'
          '.big{font-size:20px;font-weight:bold;color:var(--yel);text-align:center;padding:6px 0}.sm{color:var(--mut);font-size:9px;text-align:center}'
          '.pb{height:8px;background:var(--border);border-radius:4px;overflow:hidden;margin:2px 0 4px}.pf{height:100%;border-radius:4px}'
          'table{width:100%;border-collapse:collapse;font-size:10px}'
          'th{color:var(--mut);text-align:right;padding:3px 5px;font-size:9px;border-bottom:1px solid var(--border)}th:first-child{text-align:center}'
          'td{padding:3px 5px;text-align:right;border-bottom:1px solid rgba(30,41,59,.5)}td:first-child{text-align:center;font-weight:bold;color:var(--cyan)}'
          '.foot{text-align:center;padding:8px;color:var(--mut);font-size:9px}</style></head><body>')
    # Tab導航欄放在 body 最開頭
    _tab_js2=('function showTab(n){["main","glossary","learning"].forEach(function(t){var e=document.getElementById(t);var b=document.getElementById("tab-"+t);if(e)e.style.display=(t===n?"block":"none");if(b){b.style.background=(t===n?"var(--acc)":"var(--panel)");b.style.color=(t===n?"#fff":"var(--mut)");}});}')
    _ta2='background:var(--acc);color:#fff;border:none'
    _ti2='background:var(--panel);color:var(--mut);border:1px solid var(--border);border-bottom:none'
    _tb2='padding:6px 12px;border-radius:4px 4px 0 0;font-size:10px;cursor:pointer;font-family:inherit'
    css+=(
        f'<div style="display:flex;gap:4px;padding:8px 10px 0;background:var(--bg);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100">'
        f'<button onclick="showTab(\'main\')" id="tab-main" style="{_ta2};{_tb2}">主要分析</button>'
        f'<button onclick="showTab(\'glossary\')" id="tab-glossary" style="{_ti2};{_tb2}">📖 名詞解釋</button>'
        f'<button onclick="showTab(\'learning\')" id="tab-learning" style="{_ti2};{_tb2}">🧠 學習狀態</button>'
        f'</div><script>{_tab_js2}</script>'
        f'<div id="main">'
    )
    css+=f'<div class="hdr"><div><div class="ht">GEX ORACLE AUTO S{snapshot_num}</div>'
    css+=f'<div class="hs">UFT v2.0 | {ts} UTC | 6h | <span style="color:{agc}">updated {ags}</span></div>'
    css+=f'<div class="hs">FR next: <span style="color:var(--cyan)">{fns}</span> | Acc: <span style="color:{frc}">{facc:+.5f}%</span> | Pin Risk: <span style="color:{prc};font-weight:bold">{pr}</span></div>'
    css+='</div>'
    css+=f'<div style="text-align:right"><div style="font-size:9px;color:var(--mut)">BTC/USDT PERP | Regime: <span style="color:{rc};font-weight:bold">{regime}</span> | GF: ${gf_main:,} | {cd}</div>'
    css+=f'<div class="spot">${spot:,.0f}</div>'
    css+=f'<div style="font-size:10px;color:{frc}">FR {frs}{fr:.5f}% | DVOL {dvol:.2f}%</div>'
    css+=f'<div style="font-size:9px;color:var(--mut)">OI {oi:.2f}w'
    if data.get('oi_change') is not None: css+=f' ({data.get("oi_change",0):+.3f}w {data.get("oi_change_pct",0):+.1f}%)'
    css+=' | Basis'
    if data.get('perp_basis') is not None: css+=f' ${data.get("perp_basis",0):+.0f} ({data.get("perp_basis_pct",0):+.3f}%)'
    else: css+=' N/A'
    css+='</div></div></div>'
    css+=f'<div class="al" style="background:rgba(59,130,246,.12);border:1px solid rgba(59,130,246,.4);margin-top:8px">Oracle: <strong>{ot}</strong> | sigma=${sigma:,.0f} | UFT Median=<strong>${uft_med:,.0f}</strong></div>'
    css+=f'<div class="al" style="background:rgba(245,158,11,.08);border:1px solid {r15c}">{r15t}</div>'
    css+=(f'<div class="g4">'
          f'<div class="card"><div class="kv" style="color:var(--yel)">${spot:,.0f}</div><div class="kl">SPOT</div></div>'
          f'<div class="card"><div class="kv" style="color:{frc}">{frs}{fr:.5f}%</div><div class="kl">FUNDING RATE</div></div>'
          f'<div class="card"><div class="kv" style="color:{s0c}">{s0s}</div><div class="kl">SKEW ({exp0})</div></div>'
          f'<div class="card"><div class="kv" style="color:var(--mut)">{oi:.2f}w</div><div class="kl">OPEN INTEREST</div></div></div>')
    css+=f'<div class="g3">'
    css+=(f'<div class="card"><div class="ct">MACD (3 Timeframes)</div>'
          f'<div class="row"><span style="color:var(--cyan)">15m (30%)</span><span style="color:{c15}">{s15} {m15:+.2f}</span><span style="color:var(--mut)">{d15:+.0f}</span></div>'
          f'<div class="row"><span style="color:var(--cyan)">4h (62%)</span><span style="color:{c4h}">{s4h} {m4h:+.2f}</span><span style="color:var(--mut)">{d4h:+.0f}</span></div>'
          f'<div class="row"><span style="color:var(--cyan)">1D (70%)</span><span style="color:{c1d}">{s1d} {m1d:+.2f}</span><span style="color:var(--mut)">{d1d:+.0f}</span></div>'
          f'<div style="border-top:1px solid var(--border);margin-top:4px;padding-top:4px">'
          f'<div class="row"><span style="color:var(--mut)">ATM IV ({exp0})</span><span style="color:var(--pur)">{data.get(f"atm_iv_{exp0}",dvol) or dvol:.2f}%</span></div>'
          f'<div class="row"><span style="color:var(--mut)">DVOL Index</span><span style="color:var(--pur)">{dvol:.2f}%</span></div>'
          f'<div class="row"><span style="color:var(--mut)">IV Premium</span><span style="color:var(--pur)">{(data.get(f"atm_iv_{exp0}",dvol) or dvol)-dvol:+.2f}%</span></div>'
          f'</div></div>')
    css+=(f'<div class="card"><div class="ct">UFT v2.0 Equation</div>'
          f'<div class="row"><span>GBM (x{weights.get("gbm",0.40):.2f})</span><span>${comps.get("gbm",0):,.0f}</span></div>'
          f'<div class="row"><span>GEX (x{weights.get("gex",0.10):.2f})</span><span>${comps.get("gex",0):,.0f}</span></div>'
          f'<div class="row"><span>Behavior (x{bw:.2f})</span><span>${comps.get("behavior",0):,.0f}</span></div>'
          f'<div class="row"><span>Bayesian (x{weights.get("bayesian",0.12):.2f})</span><span>${comps.get("bayesian",0):,.0f}</span></div>'
          f'<div class="row"><span>TimeDecay (x{weights.get("timedecay",0.10):.2f})</span><span>${comps.get("timedecay",0):,.0f}</span></div>'
          f'<div class="big">${uft_med:,.0f}</div><div class="sm">Mode=${uft_mode:,.0f} | EMH=${spot:,.0f}</div></div>')
    css+=(f'<div class="card"><div class="ct">Scenario Probability ({exp0})</div>'
          f'<div style="font-size:10px;display:flex;justify-content:space-between"><span style="color:var(--green)">A: Bounce &gt;+0.5s</span><span style="color:var(--green)">{pA}%</span></div>'
          f'<div class="pb"><div class="pf" style="width:{min(pA,100):.0f}%;background:var(--green)"></div></div>'
          f'<div style="font-size:10px;display:flex;justify-content:space-between"><span style="color:var(--yel)">B: Core range</span><span style="color:var(--yel)">{pB}%</span></div>'
          f'<div class="pb"><div class="pf" style="width:{min(pB,100):.0f}%;background:var(--yel)"></div></div>'
          f'<div style="font-size:10px;display:flex;justify-content:space-between"><span style="color:var(--red)">C: Put Wall test</span><span style="color:var(--red)">{pC}%</span></div>'
          f'<div class="pb"><div class="pf" style="width:{min(pC,100):.0f}%;background:var(--red)"></div></div>'
          f'<div style="font-size:10px;display:flex;justify-content:space-between"><span style="color:var(--red)">D: Tail &lt;-1s</span><span style="color:var(--red)">{pD}%</span></div>'
          f'<div class="pb"><div class="pf" style="width:{min(pD,100):.0f}%;background:#7f1d1d"></div></div></div>')
    css+='</div>'
    css+='<div class="g2">'
    css+=(f'<div><div class="card" style="margin-bottom:10px"><div class="ct">GEX Structure + Regime</div>'
          f'<div class="row"><span>Regime ({exp0})</span><span style="color:{rc};font-weight:bold">{regime}</span></div>'
          f'<div class="row"><span>Gamma Flip ({exp0})</span><span style="color:var(--yel)">${gf0:,}</span></div>'
          f'<div class="row"><span>Spot vs GF</span><span style="color:{rc}">{spot-gf_main:+,.0f} ({abs(spot-gf_main)/gf_main*100:.1f}%)</span></div>'
          f'<div class="row"><span>GF Stability</span><span style="color:{gfsc}">{gfss} ({gfs:.2f}s)</span></div>'
          f'<div class="row"><span>Pin Risk</span><span style="color:{prc};font-weight:bold">{pr} ({psc:.0f})</span></div>'
          f'<div class="row"><span>Spot vs Put Wall</span><span style="color:var(--red)">+${spot-pw0:,.0f} (+{(spot-pw0)/pw0*100 if pw0 else 0:.1f}%)</span></div>'
          f'<div class="row"><span>Spot vs Call Wall</span><span style="color:var(--green)">-${cw0-spot:,.0f} (-{(cw0-spot)/cw0*100 if cw0 else 0:.1f}%)</span></div>'
          f'<div class="row"><span>Max Pain ({exp0})</span><span style="color:var(--pur)">${mpv:,}</span></div>'
          f'<div class="row"><span>GEX Pin ({exp0})</span><span style="color:var(--yel)">${uft_mode:,.0f}</span></div>'
          f'<div class="row"><span>OI Concentration</span><span>{data.get(f"oi_concentration_{exp0}",0) or 0:.1f}% in top3</span></div>'
          f'<div class="row"><span>PCR {exp0} ATM</span><span style="color:var(--cyan)">{data.get(f"pcr_atm_{exp0}",pcr0) or pcr0:.3f}</span></div>'
          f'<div class="row"><span>PCR {exp0} OTM</span><span>{data.get(f"pcr_otm_{exp0}",0) or 0:.3f}</span></div>'
          f'<div class="row"><span>PCR {exp1}</span><span>{pcr1:.3f}</span></div>'
          f'<div class="row"><span>PCR {exp2}</span><span>{pcr2:.3f}</span></div>'
          f'<div class="row"><span>Call Wall {exp0}</span><span style="color:var(--green)">${cw0:,}</span></div>'
          f'<div class="row"><span>Put Wall {exp0}</span><span style="color:var(--red)">${pw0:,}</span></div>'
          f'<div class="row"><span>Call Wall {exp1}</span><span style="color:var(--green)">${cw1:,}</span></div>'
          f'<div class="row"><span>Put Wall {exp1}</span><span style="color:var(--red)">${pw1:,}</span></div></div>')
    css+=(f'<div class="card"><div class="ct">Cross-Expiry Skew</div>'
          f'<div class="row"><span>{exp0} ({cd})</span><span><span style="color:{s0c}">{s0s}</span> <span style="color:{stc}">{st}</span></span></div>'
          f'<div style="font-size:9px;color:var(--mut);margin-bottom:2px">{st3}</div>'
          f'<div style="background:var(--border);height:6px;border-radius:3px;margin:2px 0 6px;overflow:hidden"><div style="height:100%;width:{sw0};background:{s0c};border-radius:3px"></div></div>'
          f'<div class="row"><span>{exp1}</span><span style="color:{s1c}">{s1s}</span></div>'
          f'<div style="background:var(--border);height:6px;border-radius:3px;margin:2px 0 6px;overflow:hidden"><div style="height:100%;width:{sw1};background:{s1c};border-radius:3px"></div></div>'
          f'<div class="row"><span>{exp2}</span><span style="color:{s2c}">{s2s}</span></div>'
          f'<div style="background:var(--border);height:6px;border-radius:3px;margin:2px 0 6px;overflow:hidden"><div style="height:100%;width:{sw2};background:{s2c};border-radius:3px"></div></div>'
          f'<div style="font-size:9px;color:var(--mut);margin-top:4px">Positive skew = bearish (market pays for downside protection)</div></div></div>')
    css+=(f'<div><div class="card" style="margin-bottom:10px"><div class="ct">Options Chain {exp0} (Top by OI)</div>'
          f'<table><thead><tr><th>Strike</th><th>Call OI</th><th>Put OI</th><th>PCR</th><th>IV C/P%</th></tr></thead><tbody>{sr}</tbody></table></div>'
          f'<div class="card" style="margin-bottom:10px"><div class="ct">Active Rules</div>{ruh}</div>'
          f'<div class="card" style="border-color:var(--acc)"><div class="ct">Oracle Insight</div>'
          f'<div style="font-size:10px;line-height:1.7">{it}</div>')
    if nt: css+=f'<div style="font-size:9px;color:var(--cyan);margin-top:6px">Next: {nt}</div>'
    css+='</div></div></div>'
    # Oracle Conclusion區塊
    coh=""
    if collision or True:
        _ov=ot; _ki=it; _nt=nt
        _regime_txt="POS Regime (造市商穩定器)" if regime=="POS" else "NEG Regime (造市商放大器)"
        _skew_txt=f"全期限偏空（{s0s}/{s1s}/{s2s}）" if (sk0 or 0)>5 else (f"全期限偏多（{s0s}/{s1s}/{s2s}）" if (sk0 or 0)<-5 else f"混合（{s0s}/{s1s}/{s2s}）")
        _pin_txt=f"GEX Pin ${uft_mode:,.0f} vs Max Pain ${mpv:,}，差距 ${abs(mpv-uft_mode):,.0f}"
        _settle_txt=f"T-{dl}d 進入結算收斂期" if dl<=7 else f"T-{dl}d 結算仍遠"
        coh=(
            f'<div style="padding:0 10px 10px"><div class="card" style="border-color:#8b5cf6">'
            f'<div class="ct" style="color:#8b5cf6">ORACLE CONCLUSION — S{snapshot_num}</div>'
            f'<div style="font-size:10px;line-height:1.9;color:var(--txt)">'
            f'<div class="row"><span style="color:var(--mut)">Regime</span><span style="color:{rc};font-weight:bold">{_regime_txt}</span></div>'
            f'<div class="row"><span style="color:var(--mut)">Skew結構</span><span style="color:{s0c}">{_skew_txt}</span></div>'
            f'<div class="row"><span style="color:var(--mut)">Pin博弈</span><span style="color:var(--yel)">{_pin_txt}</span></div>'
            f'<div class="row"><span style="color:var(--mut)">結算倒數</span><span style="color:var(--cyan)">{_settle_txt}</span></div>'
            f'<div class="row"><span style="color:var(--mut)">UFT中位</span><span style="color:var(--yel)">${uft_med:,.0f} | Mode=${uft_mode:,.0f} | σ=${sigma:,.0f}</span></div>'
            f'<div class="row"><span style="color:var(--mut)">情境分布</span><span>A={pA}% B={pB}% C={pC}% D={pD}%</span></div>'
            f'<div class="row"><span style="color:var(--mut)">Active Rules</span><span style="color:#f59e0b">{len(ru)}個觸發</span></div>'
            f'<div style="margin-top:8px;padding-top:6px;border-top:1px solid var(--border)">'
            f'<div style="font-size:9px;color:var(--mut);margin-bottom:4px">ORACLE VERDICT</div>'
            f'<div style="font-size:11px;color:var(--acc);font-weight:bold">{_ov}</div>'
            f'</div>'
            f'<div style="margin-top:6px;padding-top:6px;border-top:1px solid var(--border)">'
            f'<div style="font-size:9px;color:var(--mut);margin-bottom:4px">KEY INSIGHT</div>'
            f'<div style="font-size:10px;line-height:1.7;color:var(--txt)">{_ki}</div>'
            f'</div>'
            f'<div style="margin-top:6px;padding-top:6px;border-top:1px solid var(--border)">'
            f'<div style="font-size:9px;color:var(--mut);margin-bottom:4px">NEXT TRIGGER</div>'
            f'<div style="font-size:10px;color:var(--cyan)">{_nt if _nt else "Monitor FR/Skew/Spot vs Pin"}</div>'
            f'</div></div></div></div>'
        )
    # coh+slh+clh+footer 在 #main div 內
    css += coh + slh + clh
    css += f'<div class="foot">GEX Oracle v2.0 | S{snapshot_num} | 6h auto | Not investment advice</div>'
    css += '</div>'  # close #main

    # ── Glossary Tab ─────────────────────────────────────────────
    glossary_terms = [
        ("UFT (Unified Field Theory)", "統一場論", "GEX Oracle 核心預測框架。P(X)=0.30×GBM+0.18×GEX+0.28×行為信號+0.12×貝葉斯+0.12×時間衰減。五個分量加權合成結算價中位估計。"),
        ("GBM", "幾何布朗運動", "金融標準隨機遊走。假設 BTC 以 Spot 為起點按 DVOL 定義的波動率做隨機擴散。GBM 中心=Spot，是最保守的 EMH 基準。權重 0.30。"),
        ("GEX", "Gamma 暴露", "造市商被迫對沖的 Delta 方向。GEX>0：MM 持有正 Gamma，價格偏離 Pin 時反向買賣→穩定化。GEX<0：MM 放大波動。權重 0.18。"),
        ("BehaviorSignal", "行為信號", "FR方向×強度(35%)+Skew偏態(25%)+PCR(20%)+OI變化(10%)+Basis(5%)+鯨魚鏈上(5%)。範圍[-1,+1]，+1=極度多頭行為。權重 0.28。"),
        ("Bayesian", "貝葉斯分量", "基於 Regime 方向和 Skew 的先驗估計。T越小越往 GEX Pin 收斂（結算吸引力）。POS Regime 時偏 Spot 上方，NEG 時偏下方。權重 0.12。"),
        ("TimeDecay", "時間衰減分量", "臨近結算時 GEX Pin 吸引力增強效應。T→0 時 Gamma 極度集中於 Pin 附近，Pin 磁鐵效應最強。中心估計=GEX Pin。權重 0.12。"),
        ("UFT Median", "UFT 中位數", "五個分量加權合成的結算價中位估計。歷史誤差：26MAR26=0.41σ，27MAR26=0.02σ，3APR26≈0σ。"),
        ("UFT Mode", "UFT 眾數", "概率分布峰值，等同於 GEX Pin（最大 Gamma 集中點）。結算前 T-3d 內 Pin 磁鐵效應最強。"),
        ("σ (Sigma)", "一個標準差", "σ=Spot×(DVOL/100)×√T。BTC 有 68% 概率在到期日停留在 [Spot-σ, Spot+σ]。"),
        ("EMH", "效率市場假說基準", "EMH 下最優預測=當前現貨價。UFT 框架 alpha 在條件概率分布形狀和風控邊界識別，而非點預測精度。"),
        ("Gamma Flip (GF)", "Gamma 翻轉點", "造市商 Gamma 暴露從正轉負的臨界價位。Spot>GF→POS Regime（穩定器）；Spot<GF→NEG Regime（放大器）。最重要的 Regime 邊界。"),
        ("POS Regime", "正向 Gamma 機制", "現貨在 GF 以上。MM 持正 Gamma：下跌時買入、上漲時賣出→均值回歸、低波動、Pin 磁鐵有效。"),
        ("NEG Regime", "負向 Gamma 機制", "現貨跌破 GF。MM 持負 Gamma：下跌時追賣→放大下跌。趨勢延伸、高波動、Pin 磁鐵失效。"),
        ("GEX Pin", "Gamma 磁鐵點", "Put OI 最集中的行使價（ATM 附近）。造市商在此 Gamma 對沖需求最大，傾向把現貨固定在此附近，尤其結算前 48h。"),
        ("Max Pain", "最大痛苦點", "使期權買方總虧損最大的行使價。理論上造市商有動力把現貨推向此點。GEX Pin 通常比 Max Pain 更有實際吸引力。"),
        ("Pin Risk", "Pin 風險", "現貨被 Pin 在某行使價附近的風險。對空頭 Gamma 持有者有爆炸性損失風險。本系統用 GEX Pin 距 Spot 距離量化。"),
        ("Call Wall", "看漲阻力牆", "Call OI 最集中的行使價。MM 在此附近空 Gamma，上漲過此點需大量買入對沖→形成自然阻力，突破可能加速（Gamma Squeeze）。"),
        ("Put Wall", "看跌支撐牆", "Put OI 最集中的行使價。三態：OTM=支撐弱；ATM（Spot≈PW）=最強支撐；ITM（Spot<PW）=MM 轉為買入=動態支撐。"),
        ("PCR", "Put/Call 比率", "Put OI÷Call OI。>1.3：空頭防禦主導；<0.6：多頭進攻主導。ATM PCR 比全域 PCR 更能反映即時方向，OTM PCR 反映尾部風險需求。"),
        ("OI Concentration", "OI 集中度", "最大3個行使價的 OI 佔總 OI 比例。集中度越高→Pin 效應越強。通常結算前 OI 向 ATM 集中。"),
        ("Cross-Expiry Skew", "跨期 Skew 結構", "近端 Skew > 遠端為正常。倒掛（遠端>近端）→市場預期中長期尾部風險更高，可能為機構對沖需求。"),
        ("IV (Implied Volatility)", "隱含波動率", "從期權市場價格反推的未來波動率預期。ATM IV 最能反映市場共識，OTM IV 反映尾部風險定價。"),
        ("DVOL Index", "Deribit 波動率指數", "Deribit 官方 BTC 期權市場整體 IV 指數（類似 VIX）。30天期限年化波動率，多行使價加權合成。上升=市場恐慌/對沖需求增加。"),
        ("ATM IV", "平值隱含波動率", "最接近當前 Spot 的期權 IV。ATM IV - DVOL = IV Premium，正值表示近端比市場整體更貴。"),
        ("Skew", "波動率偏態", "Put IV - Call IV（OTM）。正值：市場為下行保護付更高溢價，空頭情緒主導（BTC 通常正 Skew）。"),
        ("Gamma", "期權 Gamma", "Delta 對 Spot 的二階導數。衡量 MM Delta 對沖需求隨 Spot 變化的速度。ATM Gamma 最大，結算前 Gamma 最集中。"),
        ("Delta", "期權 Delta", "期權價值對標的物價格的一階導數。Call Delta∈[0,1]，Put Delta∈[-1,0]。MM 通常 Delta 中性但 Gamma 暴露無法消除。"),
        ("FR (Funding Rate)", "資金費率", "永續合約每 8h 結算一次。FR>0：多方付費給空方（現貨溢價/多頭主導）；FR<0：空方付費（反向溢價/空頭主導）。觸發閾值±0.005%。"),
        ("Perp Basis", "永續基差", "永續合約價格-現貨價格。正值=合約溢價（多頭情緒）；負值=折價（空頭情緒）。是 FR 的即時領先指標。"),
        ("L/S Ratio", "多空比", "大戶帳戶多頭/空頭持倉比例。>2.0=大戶明顯偏多，<1.0=偏空。FR正+L/S上升=全權重確認；矛盾=行為信號×0.7懲罰。"),
        ("OI (Open Interest)", "未平倉量", "市場上未平倉合約總量（萬張）。OI增加+FR正→新多頭進場；OI增加+FR負→新空頭進場；OI下降=舊倉位平倉（趨勢末段）。"),
        ("MACD (12,26,9)", "移動平均匯聚背離", "DIF=EMA12-EMA26；DEA=DIF的EMA9；MACD=(DIF-DEA)×2。DIF>DEA=金叉（多）；DIF<DEA=死叉（空）。金叉在DIF<0領域→信號×0.5。"),
        ("DIF", "MACD 快慢線差", "DIF=EMA12-EMA26。DIF>0且上升=上升動能加速；DIF<0且下降=下跌動能加速。1D DIF<-1000觸發Rule#2深度負值警告。"),
        ("RSI", "相對強弱指數", "範圍[0,100]。RSI<30=超賣（潛在反彈），>70=超買（潛在回調）。需區分真實超賣（RSI隨Spot下跌>1根K棒）和機械滾動（效果×0.5）。"),
        ("EMA", "指數移動平均", "近期價格權重更高的移動平均。本系統使用 EMA5/10/30/200。Spot 在所有 EMA 下方=全線空頭排列。"),
        ("Rule#5 v2b", "FR 確認規則", "FR 須持續正值 16h 以上（=兩個完整 8h 結算週期）才算多頭確認。單週期正 FR 可能是噪音。"),
        ("Rule#14", "FR 最小分析單位", "FR 是每 8h 結算機制，sub-8h 的 FR 分析在方法論上無效。最小有效單位=一個 8h 結算週期。"),
        ("Rule#15 矛盾懲罰", "行為信號矛盾檢測", "FR>+0.005%且Skew>+5%（FR偏多但Skew偏空），behavior_penalty=0.7×。基礎權重0.28不變，信號強度縮減。"),
        ("Regime 分層", "POS/NEG 機制分離", "POS：Layer1+Layer2合併分析，造市商提供自然支撐。NEG：兩層嚴格分離，造市商變放大器，每個信號需更高確信度。"),
        ("Pin 博弈", "結算日 Pin 動態", "結算前 T-3d 進入結算收斂期。GEX Pin 和 Max Pain 爭奪現貨落點。兩點差距越小，Pin 效應越確定；差距>$2000時不確定性高。"),
        ("UFT Optimizer L1-L5", "五層迭代學習", "L1:梯度下降(滾動衰減) L2:信號貢獻度分析 L3:Regime分層(POS/NEG各自最優) L4:貝葉斯更新(防過擬合) L5:收斂偵測(error<0.3σ凍結，>0.8σ解凍)。"),
        ("Signal Direction Accuracy", "信號方向準確率", "每個信號預測方向與實際結算偏差的一致率。>60%=有效；<40%=反向有效；≈50%=無效（隨機）。"),
        ("Rolling Decay Weight", "滾動衰減", "梯度下降計算損失時，越近期結算給越高損失權重（half-life=10條）。公式：w=exp(-0.693×age/10)。防止過度依賴遠期歷史。"),
        ("Convergence", "收斂狀態", "收斂：均方誤差<0.3σ（絕對）或連續3次優化改善<1%（相對）→凍結權重。解凍：新樣本均誤>0.8σ（市場結構變化，重新學習）。"),
        ("Settlement Log", "結算記錄", "每次 UFT 計算記錄預測，到期日後自動從 Deribit 拉取結算價並計算誤差。誤差<0.5σ=綠；0.5-1.0σ=黃；>1.0σ=紅。累積10筆啟動首次優化。"),
    ]

    categories = [
        ("UFT 方程式分量", ["UFT (Unified Field Theory)", "GBM", "GEX", "BehaviorSignal", "Bayesian", "TimeDecay", "UFT Median", "UFT Mode", "σ (Sigma)", "EMH"]),
        ("GEX 結構與 Regime", ["Gamma Flip (GF)", "POS Regime", "NEG Regime", "GEX Pin", "Max Pain", "Pin Risk", "Call Wall", "Put Wall", "PCR", "OI Concentration", "Cross-Expiry Skew"]),
        ("期權基礎概念", ["IV (Implied Volatility)", "DVOL Index", "ATM IV", "Skew", "Gamma", "Delta"]),
        ("行為信號", ["FR (Funding Rate)", "Perp Basis", "L/S Ratio", "OI (Open Interest)"]),
        ("K線技術分析", ["MACD (12,26,9)", "DIF", "RSI", "EMA"]),
        ("框架規則", ["Rule#5 v2b", "Rule#14", "Rule#15 矛盾懲罰", "Regime 分層", "Pin 博弈"]),
        ("迭代學習系統", ["UFT Optimizer L1-L5", "Signal Direction Accuracy", "Rolling Decay Weight", "Convergence", "Settlement Log"]),
    ]
    term_dict = {t[0]: (t[1], t[2]) for t in glossary_terms}

    glos_html = f'<div id="glossary" style="display:none;padding:0 10px 20px">'
    glos_html += f'<div style="font-size:10px;color:var(--mut);padding:8px 0 12px">共 {len(glossary_terms)} 個術語 | 按類別排列 | 點擊展開</div>'
    for cat_name, cat_terms in categories:
        glos_html += f'<div style="font-size:11px;color:var(--acc);font-weight:bold;margin:16px 0 8px;padding-bottom:4px;border-bottom:1px solid var(--border)">{cat_name}</div>'
        for term in cat_terms:
            if term not in term_dict: continue
            cn, desc = term_dict[term]
            glos_html += (
                f'<details style="margin-bottom:6px;background:var(--panel);border:1px solid var(--border);border-radius:4px">'
                f'<summary style="padding:8px 10px;cursor:pointer;list-style:none;display:flex;justify-content:space-between">'
                f'<span><span style="color:var(--yel);font-weight:bold;font-size:11px">{term}</span>'
                f'<span style="color:var(--mut);font-size:10px;margin-left:8px">{cn}</span></span>'
                f'<span style="color:var(--mut);font-size:9px">&#9660;</span></summary>'
                f'<div style="padding:8px 10px 10px;font-size:10px;line-height:1.8;color:var(--txt);border-top:1px solid var(--border)">{desc}</div>'
                f'</details>'
            )
    glos_html += '</div>'
    css += glos_html

    # ── 學習狀態 Tab ─────────────────────────────────────────────
    learn_html = '<div id="learning" style="display:none;padding:0 10px 20px">'
    try:
        if _os2.path.exists('data/settlement_log.json'):
            import json as _jl2
            with open('data/settlement_log.json') as _fl2:
                _ll2 = _jl2.load(_fl2)
            _cw2 = _ll2.get('current_weights', {"gbm":0.30,"gex":0.18,"behavior":0.28,"bayesian":0.12,"timedecay":0.12})
            _rw2 = _ll2.get('regime_weights', {})
            _cv2 = _ll2.get('convergence', {})
            _sc2 = _ll2.get('signal_contributions', {})
            _wh2 = _ll2.get('weight_history', [])
            _cn2 = len([r for r in _ll2.get('records',[]) if r.get('actual_settlement')])
            _tn2 = len(_ll2.get('records',[]))
            _frozen2 = _cv2.get('frozen', False)
            _conv_s = '已收斂凍結' if _frozen2 else f'學習中 ({_cn2}/10樣本)'
            _conv_c2 = '#10b981' if _frozen2 else '#f59e0b'
            _err_h2 = _cv2.get('avg_error_sigma_history', [])
            learn_html += f'<div class="card" style="margin-bottom:10px"><div class="ct">學習系統狀態</div>'
            learn_html += f'<div class="row"><span>收斂狀態</span><span style="color:{_conv_c2};font-weight:bold">{_conv_s}</span></div>'
            learn_html += f'<div class="row"><span>已結算樣本</span><span>{_cn2} / {_tn2} 筆</span></div>'
            learn_html += f'<div class="row"><span>優化次數</span><span>{len(_wh2)} 次</span></div>'
            if _err_h2:
                learn_html += f'<div class="row"><span>誤差σ趨勢</span><span style="color:var(--cyan)">{" → ".join(f"{v:.3f}" for v in _err_h2[-5:])}</span></div>'
            learn_html += '</div>'
            learn_html += '<div class="card" style="margin-bottom:10px"><div class="ct">全局最優權重 (L1+L4 Fusion)</div>'
            for k2, v2 in _cw2.items():
                bw2 = f'{min(v2*100*4,100):.0f}%'
                learn_html += f'<div class="row"><span>{k2}</span><span style="color:var(--yel)">{v2:.4f} ({v2*100:.1f}%)</span></div>'
                learn_html += f'<div style="background:var(--border);height:4px;border-radius:2px;margin:1px 0 4px;overflow:hidden"><div style="height:100%;width:{bw2};background:var(--yel);border-radius:2px"></div></div>'
            learn_html += '</div>'
            if _rw2:
                learn_html += '<div class="card" style="margin-bottom:10px"><div class="ct">Regime 分層權重 (L3)</div>'
                for _rn2, _rd2 in _rw2.items():
                    _rc3 = '#10b981' if _rn2 == 'POS' else '#ef4444'
                    learn_html += f'<div style="font-size:10px;color:{_rc3};font-weight:bold;margin:6px 0 3px">{_rn2} Regime</div>'
                    for k3, v3 in _rd2.items():
                        learn_html += f'<div class="row"><span style="color:var(--mut)">{k3}</span><span style="color:{_rc3}">{v3:.4f}</span></div>'
                learn_html += '</div>'
            if _sc2:
                learn_html += '<div class="card" style="margin-bottom:10px"><div class="ct">信號方向準確率 (L2)</div>'
                learn_html += '<div style="font-size:9px;color:var(--mut);margin-bottom:6px">&gt;60%=有效 | &lt;40%=反向有效 | ≈50%=無效</div>'
                for sg2, sv2 in sorted(_sc2.items(), key=lambda x: x[1].get('direction_accuracy',0), reverse=True):
                    ac2 = sv2.get('direction_accuracy', 0)
                    ns2 = sv2.get('n', 0)
                    ac2c = '#10b981' if ac2 > 0.6 else ('#ef4444' if ac2 < 0.4 else 'var(--mut)')
                    ac2b = f'{ac2*100:.0f}%'
                    learn_html += f'<div class="row"><span>{sg2}</span><span style="color:{ac2c}">{ac2*100:.0f}% (n={ns2})</span></div>'
                    learn_html += f'<div style="background:var(--border);height:4px;border-radius:2px;margin:1px 0 4px;overflow:hidden"><div style="height:100%;width:{ac2b};background:{ac2c};border-radius:2px"></div></div>'
                learn_html += '</div>'
            if _wh2:
                learn_html += '<div class="card"><div class="ct">最近優化記錄 (最多5次)</div>'
                for h2 in _wh2[-5:][::-1]:
                    _ts_h = str(h2.get('timestamp',''))[:16]
                    _frz2 = '🔒' if h2.get('frozen') else '🔄'
                    learn_html += (f'<div style="font-size:9px;padding:4px 0;border-bottom:1px solid var(--border)">'
                                   f'{_frz2} {_ts_h} | n={h2.get("samples",0)} | '
                                   f'err=${h2.get("avg_error_usd",0):,.0f} ({h2.get("avg_error_sigma",0):.3f}σ) | '
                                   f'改善{h2.get("improvement_pct",0):+.1f}%</div>')
                learn_html += '</div>'
        else:
            learn_html += '<div class="card"><div style="font-size:10px;color:var(--mut);padding:12px">尚無 settlement_log.json，首次結算後自動建立</div></div>'
    except Exception as _le2:
        learn_html += f'<div class="card"><div style="font-size:10px;color:var(--red);padding:12px">載入錯誤: {_le2}</div></div>'
    learn_html += '</div>'
    css += learn_html
    css += '</body></html>'
    return css


# ── Main ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import os as _os_main, json as _json_main, sys as _sys_main

    OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "docs/oracle")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs("data", exist_ok=True)

    # 1. 讀市場數據
    data_path = "data/oracle_market_data.json"
    if not os.path.exists(data_path):
        print(f"ERROR: {data_path} not found"); sys.exit(1)
    with open(data_path) as f:
        data = json.load(f)
    print(f"Spot: ${data.get('spot',0):,.2f}  FR: {data.get('fr',0)*100:+.5f}%")

    # 2. 讀/更新 snapshot counter + 硬性觸發判斷
    counter_path = "data/snapshot_counter.json"
    if os.path.exists(counter_path):
        with open(counter_path) as f:
            counter = json.load(f)
    else:
        counter = {"last_snapshot": 27, "count": 27, "last_spot": 0,
                   "last_fr": 0, "last_ls": 0, "last_oi": 0}

    prev_num = max(counter.get("last_snapshot", 0), counter.get("count", 0))

    # ── 硬性觸發條件（任一滿足 → S++）────────────────────────────
    spot      = data.get("spot", 0)
    fr        = data.get("fr", 0) * 100          # 轉換為百分比
    ls        = data.get("ls") or 0
    oi        = data.get("oi", 0)
    sigma     = spot * (data.get("dvol", 50) / 100) * math.sqrt(
                    parse_days_to_expiry(data.get("expiries", ["7D"])[0]) / 365)

    last_spot = counter.get("last_spot", spot)
    last_fr   = counter.get("last_fr", fr)
    last_ls   = counter.get("last_ls", ls)
    last_oi   = counter.get("last_oi", oi)

    hard_triggers = []

    # FR 穿越：-0.01 / -0.005 / 0 / +0.005 / +0.01（符號改變或越過閾值）
    fr_thresholds = [-0.01, -0.005, 0.0, 0.005, 0.01]
    for thr in fr_thresholds:
        if (last_fr < thr <= fr) or (last_fr > thr >= fr):
            hard_triggers.append(f"FR穿越{thr:+.3f}%（{last_fr:+.5f}→{fr:+.5f}）")

    # Spot 移動 > ±0.5σ
    if sigma > 0 and abs(spot - last_spot) > 0.5 * sigma:
        hard_triggers.append(f"Spot移動{spot-last_spot:+,.0f}（>{0.5*sigma:,.0f}=0.5σ）")

    # L/S 整數穿越：1.5 / 2.0 / 2.5 / 3.0
    if ls > 0 and last_ls > 0:
        for thr in [1.5, 2.0, 2.5, 3.0]:
            if (last_ls < thr <= ls) or (last_ls > thr >= ls):
                hard_triggers.append(f"L/S穿越{thr}（{last_ls:.3f}→{ls:.3f}）")

    # OI 跳動 > ±300張（配合 FR 方向）
    oi_change = abs(oi - last_oi) * 10000  # 萬→張
    if oi_change > 300:
        hard_triggers.append(f"OI跳動{oi_change:+,.0f}張（{last_oi:.2f}→{oi:.2f}萬）")

    # 時間強制：T = 24h / 6h / 2h（由 generate_html T-checklist 已處理，這裡用 T<=1d 強制觸發）
    dl = parse_days_to_expiry(data.get("expiries", ["7D"])[0])
    if dl in [1, 0]:
        hard_triggers.append(f"T={dl}d強制觸發（結算前最後窗口）")

    # 優化觸發（optimizer 本次跑完後若有新權重，也算觸發）
    # → 在 step 6 optimizer 跑完後補充判斷，這裡先留 flag
    _optimizer_triggered = False

    is_hard_trigger = len(hard_triggers) > 0

    if is_hard_trigger:
        snapshot_num = prev_num + 1
        trigger_str = " | ".join(hard_triggers)
        print(f"[HARD TRIGGER] {trigger_str}")
        print(f"Snapshot: S{snapshot_num} (+1 觸發)")
    else:
        snapshot_num = prev_num  # 保持不變，本次只更新 dashboard 不記錄新快照
        print(f"[NO TRIGGER] 本次無硬性觸發，snapshot 維持 S{snapshot_num}")

    # 更新 counter（無論是否觸發都更新最新市場數據）
    counter.update({
        "last_snapshot": snapshot_num,
        "count": snapshot_num,
        "last_spot": spot,
        "last_fr": fr,
        "last_ls": ls,
        "last_oi": oi,
        "hard_triggers": hard_triggers,
        "last_trigger_ts": data.get("timestamp", ""),
    })
    with open(counter_path, "w") as f:
        json.dump(counter, f, indent=2)
    print(f"Snapshot: S{snapshot_num}")

    # 3. 讀上一筆數據（用於行為信號計算）
    prev_data = None
    prev_path = "data/oracle_prev_data.json"
    if os.path.exists(prev_path):
        with open(prev_path) as f:
            prev_data = json.load(f)
    with open(prev_path, "w") as f:
        json.dump(data, f)

    # 4. UFT 計算
    uft_result = calc_uft(data, prev_data)
    print(f"UFT Median: ${uft_result['uft_median']:,.0f}  Regime: {uft_result['regime']}")

    # 5. 碰撞分析
    collision = generate_rule_based_collision(data, uft_result)

    # 6. 記錄預測到 settlement_log（核心修復）
    try:
        _sys_main.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from uft_optimizer import record_prediction, check_and_record_settlement, optimize_weights
        expiries_list = data.get("expiries", [])
        main_expiry = expiries_list[0] if expiries_list else "N/A"
        record_prediction(
            snapshot_num=snapshot_num,
            expiry=main_expiry,
            predicted_median=uft_result["uft_median"],
            predicted_mode=uft_result["uft_mode"],
            components=uft_result.get("components", {}),
            weights=uft_result.get("uft_weights", {}),
            signals={
                "fr":           data.get("fr"),
                "skew":         uft_result.get("skew_main"),
                "dvol":         data.get("dvol"),
                "pcr_main":     data.get(f"pcr_atm_{main_expiry}"),
                "macd_4h":      (data.get("macd", {}).get("4h") or {}).get("dif"),
                "regime_pos":   1.0 if uft_result.get("regime") == "POS" else 0.0,
                "gamma_flip":   float(uft_result.get("gamma_flip") or 0),
                "contradiction":1.0 if uft_result.get("behavior_contradiction") else 0.0,
            },
            sigma=uft_result.get("sigma", 4000),
            regime=uft_result.get("regime", "POS"),
        )
        print(f"Recorded S{snapshot_num} → {main_expiry} ${uft_result['uft_median']:,.0f}")
        # 自動結算檢查
        check_and_record_settlement()
        # 10筆以上嘗試優化；若有新權重產生 → 觸發 S++
        _old_w_str = str(json.load(open("data/settlement_log.json")).get("current_weights", {})) if os.path.exists("data/settlement_log.json") else ""
        optimize_weights(min_samples=10)
        _new_w_str = str(json.load(open("data/settlement_log.json")).get("current_weights", {})) if os.path.exists("data/settlement_log.json") else ""
        if _old_w_str and _new_w_str and _old_w_str != _new_w_str:
            # 優化觸發：權重有變化 → S++
            _optimizer_triggered = True
            if not is_hard_trigger:
                snapshot_num += 1
                counter["last_snapshot"] = snapshot_num
                counter["count"] = snapshot_num
                with open(counter_path, "w") as f:
                    json.dump(counter, f, indent=2)
                print(f"[OPTIMIZER TRIGGER] 權重更新 → S{snapshot_num}")
    except Exception as _e:
        print(f"record_prediction error: {_e}")

    # 7. 生成 HTML
    html = generate_html(data, uft_result, collision, snapshot_num)
    out_path = os.path.join(OUTPUT_DIR, "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML written: {out_path} ({len(html):,} bytes)")
