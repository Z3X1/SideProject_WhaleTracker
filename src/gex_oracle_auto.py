#!/usr/bin/env python3
import json
# GEX Oracle Auto Engine v2.0




import os, json, math, time, requests, sqlite3
from datetime import datetime, timezone

# ============================================================
# 1. 數據抓取層
# ============================================================


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
    weight = 0.5 if contradiction else 1.0

    # 信號強度
    fr_signal = min(abs(fr) / 0.0001, 1.0) * fr_direction  # 正規化
    ls_signal = (ls - 2.0) / 0.5  # 2.0為中性基準

    raw_signal = (fr_signal * 0.4 + ls_signal * 0.6) * weight
    return raw_signal, contradiction, weight

def calc_uft(data, prev_data=None):
    # UFT統一場方程計算
    spot = data["spot"]
    dvol = data["dvol"] / 100
    T = 7 / 365  # 3JUL26
    sigma = spot * dvol * math.sqrt(T)

    # GEX成分
    opts_3jul = data.get("options_3JUL26", {})
    gex = calc_gex_structure(opts_3jul, spot)
    gex_center = gex["pin"]

    # BehaviorSignal成分(L/S已移除,用FR+PCR+Skew)
    expiries = data.get("expiries", ["3JUL26","31JUL26","25SEP26"])
    fr = data.get("fr", 0)
    oi_change = (data.get("oi",0) - prev_data.get("oi",0)) if prev_data else 0
    skew_main = data.get("skew", {}).get(expiries[0] if expiries else "3JUL26", 0) or 0
    # FR信號方向
    fr_signal = 1 if fr > 0 else -1
    fr_strength = min(abs(fr) / 0.0001, 1.0)
    # Skew信號(正skew=偏空,負skew=偏多)
    skew_signal = -1 if skew_main > 2 else (1 if skew_main < -2 else 0)
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
    weight = 0.28 * (0.5 if contradiction else 1.0)
    import math as _m2
    exp_main = expiries[0] if expiries else "3JUL26"
    T_main = data.get("T_main", 7) / 365
    sigma_main = spot * (data.get("dvol", 50) / 100) * _m2.sqrt(T_main)
    gf_dict = data.get("gamma_flip", {})
    gamma_flip_main = int(gf_dict.get(exp_main, gex_center) or gex_center)
    regime = "POS" if spot > gamma_flip_main else "NEG"
    bayes_center = spot + (0.3 if regime == "POS" else -0.3) * sigma_main
    bw = {"gbm":0.40,"gex":0.10,"behavior":weight,"bayesian":0.12,"timedecay":0.10}
    bw.update(data.get("uft_weights", {}))
    uft = (bw["gbm"]*spot + bw["gex"]*gex_center + weight*(spot+behavior_signal*sigma_main)
           + bw["bayesian"]*bayes_center + bw["timedecay"]*gex_center)
    return {
        "uft_median": round(uft,2), "uft_mode": gex_center, "uft_emh": spot,
        "sigma": round(sigma_main,2), "regime": regime, "gamma_flip": gamma_flip_main,
        "behavior_contradiction": contradiction, "behavior_weight": weight,
        "skew_main": skew_main, "uft_weights": bw,
        "components": {
            "gbm": round(bw["gbm"]*spot,2), "gex": round(bw["gex"]*gex_center,2),
            "behavior": round(weight*(spot+behavior_signal*sigma_main),2),
            "bayesian": round(bw["bayesian"]*bayes_center,2),
            "timedecay": round(bw["timedecay"]*gex_center,2),
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
    import re as _re
    from datetime import date
    mn={"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    dl=999
    try:
        m2=_re.match(r"(\d+)([A-Z]+)(\d+)",exp0)
        if m2: dl=(date(2000+int(m2.group(3)),mn[m2.group(2)],int(m2.group(1)))-date.today()).days
    except: pass
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
    weights=data.get('uft_weights',{'gbm':0.40,'gex':0.10,'behavior':0.28,'bayesian':0.12,'timedecay':0.10})
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
    mn={'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}
    cd=''; dl=999
    try:
        import re as re2
        m2=re2.match(r'(\d+)([A-Z]+)(\d+)',exp0)
        if m2:
            ed=date(2000+int(m2.group(3)),mn[m2.group(2)],int(m2.group(1)))
            dl=(ed-date.today()).days; cd=f'T-{dl}d' if dl>0 else 'TODAY'
    except: pass
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
    css+=coh+slh+clh
    css+=f'<div class="foot">GEX Oracle v2.0 | S{snapshot_num} | 6h auto | Not investment advice</div></body></html>'
    return css

def send_telegram(data, uft_result, collision, snapshot_num):
    bot_token=os.environ.get("TELEGRAM_BOT_TOKEN","")
    chat_id=os.environ.get("TELEGRAM_CHAT_ID","")
    if not bot_token or not chat_id: print("[INFO] Telegram not configured"); return
    spot=float(data.get("spot",0)); fr_pct=float(data.get("fr",0))*100
    uft_med=uft_result["uft_median"]; contradiction=uft_result["behavior_contradiction"]
    oracle=collision.get("oracle_verdict","N/A") if collision else "N/A"
    key_insight=collision.get("key_insight","") if collision else ""
    macd_1d=data.get("macd_1d") or data.get("macd",{}).get("1d",{})
    m1d_s="Bullish X" if macd_1d.get("dif",0)>macd_1d.get("dea",0) else "Bearish X"
    r15="[WARN] Contradictory(x0.5)" if contradiction else "[OK] Consistent(full)"
    msg=(f"[GEX Oracle S{snapshot_num}] Update\nSpot: ${spot:,.0f} | FR: {fr_pct:+.5f}%\n"
         f"OI: {data.get("oi",0):.2f}w | DVOL: {data.get("dvol",0):.2f}%\n"
         f"UFT: ${uft_med:,.0f} | Oracle: {oracle}\n1D MACD: {m1d_s}\nInsight: {key_insight}")
    try:
        requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id":chat_id,"text":msg},timeout=10)
        print("Telegram push done")
    except Exception as e: print(f"Telegram error: {e}")

def load_prev_data(db_path="data/gex_oracle.db"):
    prev_data=None; prev_num=22
    try:
        import urllib.request as _ur2
        gh_token=os.environ.get("GH_PAT",os.environ.get("GITHUB_TOKEN",""))
        gh_repo=os.environ.get("GITHUB_REPOSITORY","Z3X1/SideProject_WhaleTracker")
        url=f"https://api.github.com/repos/{gh_repo}/contents/data/snapshot_counter.json"
        req2=_ur2.Request(url,headers={"Authorization":f"token {gh_token}","Accept":"application/vnd.github.v3+json"})
        with _ur2.urlopen(req2,timeout=10) as resp:
            import base64 as b64
            data_raw=json.loads(resp.read())
            counter=json.loads(b64.b64decode(data_raw["content"]).decode())
            prev_num=int(counter.get("last_snapshot",22))
            prev_data=counter.get("last_data")
            print(f"Loaded: S{prev_num}")
    except Exception as e:
        if "404" in str(e): prev_num=23
        print(f"counter: {e}")
    if os.path.exists("data/snapshot_counter.json") and prev_num==22:
        try:
            with open("data/snapshot_counter.json") as f:
                c=json.load(f); prev_num=int(c.get("last_snapshot",22)); prev_data=c.get("last_data")
        except: pass
    try:
        conn=sqlite3.connect(db_path)
        ddl=("CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT,"
             "timestamp TEXT,snapshot_num INTEGER,spot REAL,fr REAL,ls REAL,"
             "oi REAL,dvol REAL,uft_median REAL,oracle_verdict TEXT,data_json TEXT)")
        conn.execute(ddl); conn.commit()
        row=conn.execute("SELECT data_json,snapshot_num FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        if row and row[1]>prev_num: prev_num=row[1]; prev_data=json.loads(row[0])
    except: pass
    return prev_data, prev_num

def save_snapshot(data, uft_result, collision, snapshot_num, db_path="data/gex_oracle.db"):
    conn=sqlite3.connect(db_path)
    oracle=collision.get("oracle_verdict","") if collision else ""
    ins=("INSERT INTO snapshots (timestamp,snapshot_num,spot,fr,ls,oi,dvol,uft_median,oracle_verdict,data_json)"
         " VALUES (?,?,?,?,?,?,?,?,?,?)")
    conn.execute(ins,(data["timestamp"],snapshot_num,data["spot"],data["fr"],
        data.get("ls"),data["oi"],data["dvol"],uft_result["uft_median"],oracle,json.dumps(data)))
    conn.commit(); conn.close()
    print(f"S{snapshot_num} Saved")
    # 更新GitHub counter（跨runner持久化）
    try:
        import urllib.request as _urw, base64 as _b64w
        gh_token=os.environ.get("GH_PAT",os.environ.get("GITHUB_TOKEN",""))
        gh_repo=os.environ.get("GITHUB_REPOSITORY","Z3X1/SideProject_WhaleTracker")
        url=f"https://api.github.com/repos/{gh_repo}/contents/data/snapshot_counter.json"
        # 先GET取SHA
        req_get=_urw.Request(url,headers={"Authorization":f"token {gh_token}","Accept":"application/vnd.github.v3+json"})
        with _urw.urlopen(req_get,timeout=10) as rg:
            existing=json.loads(rg.read())
            file_sha=existing["sha"]
        counter_data={"last_snapshot":snapshot_num,"last_data":{"uft_median":uft_result["uft_median"]}}
        body_w=json.dumps({"message":f"counter: S{snapshot_num}","content":_b64w.b64encode(json.dumps(counter_data).encode()).decode(),"sha":file_sha}).encode()
        req_put=_urw.Request(url,data=body_w,headers={"Authorization":f"token {gh_token}","Accept":"application/vnd.github.v3+json","Content-Type":"application/json"},method="PUT")
        with _urw.urlopen(req_put,timeout=10): pass
        print(f"Counter updated: S{snapshot_num}")
    except Exception as ew: print(f"Counter update error: {ew}")

def main():
    print("="*50)
    print("GEX Oracle 自動化引擎 v2.0")
    print("="*50)

    # 載入上次狀態
    prev_data, prev_num = load_prev_data()
    snapshot_num = prev_num + 1
    print(f"Snapshot: S{snapshot_num}")

    # 1. 優先讀取已抓取的市場數據(由gex_oracle_fetch.py生成)
    market_data_path = "data/oracle_market_data.json"
    if os.path.exists(market_data_path):
        print(f"📂 Loading pre-fetched data: {market_data_path}")
        with open(market_data_path) as f:
            data = json.load(f)
        print(f"  Spot: ${data.get('spot', 0):,.0f}")
        print(f"  FR: {data.get('fr', 0)*100:+.5f}%")
        print(f"  L/S: {data.get('ls') or 'N/A'}")
        print(f"  DVOL: {data.get('dvol', 46):.2f}%")
        # 格式標準化:將 data["macd"]["4h"] 轉為 data["macd_4h"]
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

    # 6. 記錄預測到settlement_log(UFT動態優化)
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
        # 若有足夠樣本,自動優化權重
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
