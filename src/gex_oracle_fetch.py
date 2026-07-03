#!/usr/bin/env python3
"""GEX Oracle 數據抓取 - 動態到期日 + 真實FR/OI/L/S"""
import requests, json, os, time
from datetime import datetime, timedelta, timezone

UA = {"User-Agent": "Mozilla/5.0 GEX-Oracle/2.0"}

def get(url, **kw):
    try:
        r = requests.get(url, timeout=12, headers=UA, **kw)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  ERR {url[:50]}: {e}")
    return None

def next_friday(from_date=None):
    """下一個週五"""
    d = from_date or datetime.now(timezone.utc).date()
    days_ahead = 4 - d.weekday()  # 週五=4
    if days_ahead <= 0:
        days_ahead += 7
    return d + timedelta(days=days_ahead)

def deribit_expiry_format(date):
    """date → Deribit格式 e.g. 4JUL26"""
    months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
    return f"{date.day}{months[date.month-1]}{str(date.year)[2:]}"

def get_dynamic_expiries():
    """
    動態取得到期日：下週五週選 + 月選（當月最後週五）+ 季選（3/6/9/12月最後週五）
    從Deribit實際存在的到期日中選
    """
    # 先從Deribit取所有可用到期日
    d = get("https://www.deribit.com/api/v2/public/get_instruments?currency=BTC&kind=option&expired=false")
    if not d:
        # fallback：靜態計算
        today = datetime.now(timezone.utc).date()
        w = next_friday(today)
        # 月選：當月或下月最後週五
        m_candidates = []
        for delta_m in [0, 1, 2]:
            month = (today.month + delta_m - 1) % 12 + 1
            year = today.year + (today.month + delta_m - 1) // 12
            # 該月最後一天
            last_day = (datetime(year, month % 12 + 1, 1) - timedelta(days=1)).date() if month < 12 else datetime(year, 12, 31).date()
            # 往回找週五
            while last_day.weekday() != 4:
                last_day -= timedelta(days=1)
            if last_day > today:
                m_candidates.append(last_day)
        m = m_candidates[0] if m_candidates else w + timedelta(weeks=4)
        # 季選：下一個季末週五（3/6/9/12月）
        q_months = [3, 6, 9, 12]
        q = None
        for delta_q in range(1, 8):
            check_month = (today.month + delta_q - 1) % 12 + 1
            if check_month in q_months:
                check_year = today.year + (today.month + delta_q - 1) // 12
                last_day = (datetime(check_year, check_month % 12 + 1, 1) - timedelta(days=1)).date() if check_month < 12 else datetime(check_year, 12, 31).date()
                while last_day.weekday() != 4:
                    last_day -= timedelta(days=1)
                if last_day > today:
                    q = last_day
                    break
        expiries = list(dict.fromkeys([
            deribit_expiry_format(w),
            deribit_expiry_format(m if m != w else m_candidates[1] if len(m_candidates)>1 else m+timedelta(weeks=4)),
            deribit_expiry_format(q) if q else "25SEP26"
        ]))
        print(f"動態到期日(fallback計算): {expiries}")
        return expiries

    # 從實際instruments取唯一到期日
    instruments = d.get("result", [])
    expiry_dates = set()
    for inst in instruments:
        exp = inst.get("expiration_timestamp", 0) / 1000
        if exp > 0:
            expiry_dates.add(datetime.fromtimestamp(exp, tz=timezone.utc).date())

    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    # 到期日當天在 UTC 08:00 前仍納入（結算在 08:00，之前仍有效）
    cutoff = datetime(today.year, today.month, today.day, 8, 0, 0, tzinfo=timezone.utc)
    if now_utc < cutoff:
        future_dates = sorted([e for e in expiry_dates if e >= today])
    else:
        future_dates = sorted([e for e in expiry_dates if e > today])

    if not future_dates:
        return ["3JUL26", "31JUL26", "25SEP26"]

    # 週選：最近的週五
    weekly = next((d for d in future_dates if d.weekday() == 4 and (d - today).days <= 14), future_dates[0])

    # 月選：最近的月末週五（非週選）
    monthly = None
    for d in future_dates:
        if d == weekly: continue
        # 是否為月末週五（下一週就是下個月了）
        next_week = d + timedelta(days=7)
        if next_week.month != d.month and d.weekday() == 4:
            monthly = d
            break
    if not monthly:
        monthly = next((d for d in future_dates if d != weekly and (d - today).days > 14), None)

    # 季選：3/6/9/12月末週五
    quarterly = None
    for d in future_dates:
        if d in [weekly, monthly]: continue
        if d.month in [3, 6, 9, 12]:
            next_week = d + timedelta(days=7)
            if next_week.month != d.month and d.weekday() == 4:
                quarterly = d
                break
    if not quarterly:
        quarterly = next((d for d in future_dates if d not in [weekly, monthly] and (d - today).days > 60), None)

    result = []
    for d in [weekly, monthly, quarterly]:
        if d:
            exp_str = deribit_expiry_format(d)
            if exp_str not in result:
                result.append(exp_str)

    print(f"動態到期日: {result} (週選/月選/季選)")
    return result if result else ["3JUL26", "31JUL26", "25SEP26"]

def fetch_all():
    data = {}

    # ── SPOT ────────────────────────────────────────────────
    spot_sources = [
        ("https://api.coinbase.com/v2/prices/BTC-USD/spot", lambda d: float(d["data"]["amount"])),
        ("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", lambda d: float(d["bitcoin"]["usd"])),
        ("https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD", lambda d: float(d["USD"])),
        ("https://api.kraken.com/0/public/Ticker?pair=XBTUSD", lambda d: float(list(d["result"].values())[0]["c"][0])),
        ("https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_usd", lambda d: float(d["result"]["index_price"])),
        ("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", lambda d: float(d["price"])),
        ("https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT", lambda d: float(d["price"])),
    ]
    for url, parser in spot_sources:
        try:
            d = get(url)
            val = parser(d)
            if val and val > 10000:
                data["spot"] = val
                print(f"Spot: ${val:,.2f} ✅")
                break
        except: pass

    # ── FUNDING RATE（真實數據）────────────────────────────
    # Binance永續合約FR - 多端點嘗試
    fr_sources = [
        ("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", lambda d: float(d["lastFundingRate"])),
        ("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1", lambda d: float(d[0]["fundingRate"])),
        ("https://fapi.binance.com/fapi/v1/fundingInfo?symbol=BTCUSDT", lambda d: float(d[0].get("lastFundingRate", 0))),
        # Bybit作為驗證對照
        ("https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT", lambda d: float(d["result"]["list"][0]["fundingRate"])),
        # OKX
        ("https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USD-SWAP", lambda d: float(d["data"][0]["fundingRate"])),
    ]
    for url, parser in fr_sources:
        try:
            d = get(url)
            val = parser(d)
            if val is not None and val != 0:
                data["fr"] = val
                print(f"FR: {val*100:+.5f}% ✅")
                break
            elif val == 0:
                data["fr"] = val
                print(f"FR: 0.00000% ✅ (真實值)")
                break
        except: pass

    if "fr" not in data:
        # 最後嘗試：Deribit perpetual
        try:
            d = get("https://www.deribit.com/api/v2/public/get_funding_rate_value?instrument_name=BTC-PERPETUAL&start_timestamp=0&end_timestamp=9999999999999")
            val = float(d["result"]) if d else 0
            data["fr"] = val
            print(f"FR: {val*100:+.5f}% ✅ (Deribit perp)")
        except:
            data["fr"] = 0.0
            print("FR: 0.0% (所有來源失敗)")

    # ── OPEN INTEREST（真實數據）───────────────────────────
    spot_price = data.get("spot", 60000)
    oi_sources = [
        ("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT", lambda d: float(d["openInterest"])/10000),
        ("https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=5m&limit=1", lambda d: float(d[0]["sumOpenInterest"])/10000),
        # Bybit: openInterestValue是USD，openInterest是BTC合約數
        ("https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT", lambda d: float(d["result"]["list"][0]["openInterestValue"])/spot_price/10000),
        # Bybit: openInterest是BTC數量
        ("https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT", lambda d: float(d["result"]["list"][0]["openInterest"])/10000),
        ("https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP", lambda d: float(d["data"][0]["oiCcy"])/10000),
    ]
    for url, parser in oi_sources:
        try:
            d = get(url)
            val = parser(d)
            if val and val > 0:
                data["oi"] = round(val, 4)
                print(f"OI: {val:.2f}萬 ✅")
                break
        except: pass

    if "oi" not in data:
        data["oi"] = 10.5
        print("OI: fallback 10.5萬")

    # L/S: unavailable in Actions environment, removed
    # FR is used as primary sentiment proxy instead
    data["ls"] = None

    # ── DVOL ────────────────────────────────────────────────
    dvol_got = False
    for dvol_url, dvol_parser in [
        ("https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&resolution=3600&count=2",
         lambda d: float(d["result"]["data"][-1][4])),
        ("https://www.deribit.com/api/v2/public/get_index_price?index_name=dvol_btc",
         lambda d: float(d["result"]["index_price"])),
        ("https://www.deribit.com/api/v2/public/get_historical_volatility?currency=BTC",
         lambda d: float(d["result"][-1][1])),
    ]:
        try:
            d = get(dvol_url)
            val = dvol_parser(d)
            if 10 < val < 300:
                data["dvol"] = val
                print(f"DVOL: {val:.2f}% ✅")
                dvol_got = True
                break
        except: pass
    if not dvol_got:
        data["dvol"] = 46.5
        print("DVOL: fallback 46.5%")

    # ── KLINES / MACD ───────────────────────────────────────
    def ema(prices, p):
        k = 2/(p+1); e = prices[0]
        for x in prices[1:]: e = x*k + e*(1-k)
        return e

    def ema_series(prices, p):
        k = 2/(p+1); r = [prices[0]]
        for x in prices[1:]: r.append(x*k + r[-1]*(1-k))
        return r

    def calc_macd(closes):
        e12 = ema_series(closes, 12)
        e26 = ema_series(closes, 26)
        dif = [a-b for a,b in zip(e12,e26)]
        k9 = 2/10; dea = [dif[0]]
        for d in dif[1:]: dea.append(d*k9 + dea[-1]*(1-k9))
        macd = [(d-e)*2 for d,e in zip(dif,dea)]
        return round(dif[-1],2), round(dea[-1],2), round(macd[-1],2)

    data["macd"] = {}
    data["ema"] = {}

    tf_map = {"15m": ("15m", 15), "4h": ("4h", 240), "1d": ("1d", 1440)}
    for tf, (binance_tf, kraken_interval) in tf_map.items():
        got = False
        # Binance先試
        for url in [
            f"https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval={binance_tf}&limit=100",
            f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={binance_tf}&limit=100",
        ]:
            try:
                d = get(url)
                if isinstance(d, list) and len(d) > 30:
                    closes = [float(k[4]) for k in d]
                    dif, dea, macd = calc_macd(closes)
                    data["macd"][tf] = {"dif": dif, "dea": dea, "macd": macd}
                    data["ema"][tf] = {str(p): round(ema(closes,p),1) for p in [5,10,30,200] if len(closes)>=p}
                    print(f"MACD {tf}: DIF={dif:.2f} ✅ (Binance)")
                    got = True; break
            except: pass

        if not got:
            # Kraken fallback
            try:
                d = get(f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval={kraken_interval}&count=100")
                pairs = d.get("result", {})
                key = [k for k in pairs if k != "last"][0]
                closes = [float(row[4]) for row in pairs[key]]
                if len(closes) > 30:
                    dif, dea, macd = calc_macd(closes)
                    data["macd"][tf] = {"dif": dif, "dea": dea, "macd": macd}
                    data["ema"][tf] = {str(p): round(ema(closes,p),1) for p in [5,10,30,200] if len(closes)>=p}
                    print(f"MACD {tf}: DIF={dif:.2f} ✅ (Kraken)")
                    got = True
            except Exception as e:
                print(f"MACD {tf} 全部失敗: {e}")
        time.sleep(0.3)

    # ── OPTIONS（動態到期日）────────────────────────────────
    data["options"] = {}
    expiries = get_dynamic_expiries()
    data["expiries"] = expiries

    try:
        d = get("https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option")
        items = d.get("result", []) if d else []
        for expiry in expiries:
            opts = {}
            for item in items:
                name = item.get("instrument_name","")
                parts = name.split("-")
                if len(parts) != 4: continue
                _, exp, strike_str, opt_type = parts
                if exp.upper() != expiry.upper(): continue
                strike = int(strike_str)
                if strike not in opts:
                    opts[strike] = {"call_oi":0,"put_oi":0,"call_iv":0,"put_iv":0}
                oi_val = float(item.get("open_interest",0))
                iv_val = float(item.get("mark_iv",0))
                if opt_type=="C":
                    opts[strike]["call_oi"] = oi_val
                    opts[strike]["call_iv"] = iv_val
                else:
                    opts[strike]["put_oi"] = oi_val
                    opts[strike]["put_iv"] = iv_val
            if opts:
                data["options"][expiry] = opts
                print(f"Opts {expiry}: {len(opts)} strikes ✅")
            else:
                print(f"Opts {expiry}: 0 strikes（已到期或無數據）")
    except Exception as e:
        print(f"Options失敗: {e}")

    # ── OPTIONS SKEW + GAMMA FLIP ──────────────────────────
    spot = data.get("spot", 60000)
    skew_results = {}
    gamma_flip_results = {}

    for expiry in expiries:
        o = data.get("options", {}).get(expiry, {})
        if not o:
            continue

        # ── Options Skew（25-delta skew）──────────────────
        # Skew = Put IV(25delta) - Call IV(25delta)
        # 正值=市場付premium買Put=偏空；負值=偏多
        # 用最接近25 delta的行權價
        sorted_strikes = sorted(o.keys())
        put_ivs_25d = []
        call_ivs_25d = []

        for strike in sorted_strikes:
            v = o[strike]
            c_iv = float(v.get("call_iv", 0))
            call_oi = float(v.get("call_oi", 0))
            put_oi_v = float(v.get("put_oi", 0))
            c_iv_real = float(v.get("call_iv", 0))
            p_iv_real = float(v.get("put_iv", 0)) if "put_iv" in v else 0

            # 近似delta：strike vs spot的位置
            # OTM Call delta ≈ 0.25 when strike ≈ spot * 1.10 (rough)
            # OTM Put delta ≈ -0.25 when strike ≈ spot * 0.90
            moneyness = strike / spot
            if 0.88 <= moneyness <= 0.93 and p_iv_real > 0:  # ~25d Put
                put_ivs_25d.append(p_iv_real)
            if 1.07 <= moneyness <= 1.13 and c_iv_real > 0:  # ~25d Call
                call_ivs_25d.append(c_iv_real)

        if put_ivs_25d and call_ivs_25d:
            skew = sum(put_ivs_25d)/len(put_ivs_25d) - sum(call_ivs_25d)/len(call_ivs_25d)
            skew_results[expiry] = round(skew, 2)
            direction = "BEARISH" if skew > 2 else ("BULLISH" if skew < -2 else "NEUTRAL")
            print(f"Skew {expiry}: {skew:+.2f}% ({direction}) ✅")
        else:
            # fallback: ATM skew用最近ATM行權價的put/call IV差
            atm_strikes = sorted(sorted_strikes, key=lambda x: abs(x - spot))[:3]
            atm_skews = []
            for s in atm_strikes:
                v = o[s]
                c_iv_r = float(v.get("call_iv", 0))
                p_iv_r = float(v.get("put_iv", 0)) if "put_iv" in v else 0
                if c_iv_r > 0 and p_iv_r > 0:
                    atm_skews.append(p_iv_r - c_iv_r)
            if atm_skews:
                skew = sum(atm_skews)/len(atm_skews)
                skew_results[expiry] = round(skew, 2)
                print(f"Skew {expiry}: {skew:+.2f}% (ATM proxy) ✅")

        # ── Gamma Flip 精確計算 ──────────────────────────
        # GEX = sum(Call_OI * Gamma - Put_OI * Gamma) * spot^2 * 0.01
        # Gamma Flip = 行權價使Net GEX = 0
        # 用Black-Scholes近似Gamma（簡化版）
        import math

        def bs_gamma(S, K, T, sigma):
            """Black-Scholes Gamma近似"""
            if T <= 0 or sigma <= 0:
                return 0
            try:
                d1 = (math.log(S/K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
                return math.exp(-0.5 * d1**2) / (S * sigma * math.sqrt(2 * math.pi * T))
            except:
                return 0

        # 計算到期時間（動態：基於今日日期計算剩餘天數）
        import re as _re_gf, math as _math_gf
        from datetime import date as _date_gf
        _mn_gf = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                  "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
        _dl_gf = 7
        try:
            _m_gf = _re_gf.match(r"(\d+)([A-Z]+)(\d+)", expiry.upper())
            if _m_gf:
                _dl_gf = max(1, (_date_gf(2000+int(_m_gf.group(3)),
                             _mn_gf[_m_gf.group(2)], int(_m_gf.group(1)))
                             - _date_gf.today()).days)
        except Exception:
            pass
        T = _dl_gf / 365
        dvol = data.get("dvol", 50) / 100

        # 計算每個行權價的Net GEX
        gex_by_strike = {}
        for strike in sorted_strikes:
            v = o[strike]
            call_oi = float(v.get("call_oi", 0))
            put_oi_v = float(v.get("put_oi", 0))
            # 用各自的IV或DVOL
            c_iv_r = float(v.get("call_iv", 0)) / 100 if v.get("call_iv") else dvol
            p_iv_r = float(v.get("put_iv", 0)) / 100 if v.get("put_iv") else dvol
            if c_iv_r == 0: c_iv_r = dvol
            if p_iv_r == 0: p_iv_r = dvol

            gamma_c = bs_gamma(spot, strike, T, c_iv_r)
            gamma_p = bs_gamma(spot, strike, T, p_iv_r)

            net_gex = (call_oi * gamma_c - put_oi_v * gamma_p) * spot * spot * 0.01
            gex_by_strike[strike] = net_gex

        # 累積GEX從高到低行權價
        cumulative_gex = 0
        gamma_flip = None
        prev_strike = None
        prev_cum = 0

        for strike in sorted(gex_by_strike.keys(), reverse=True):
            cumulative_gex += gex_by_strike[strike]
            if prev_strike is not None and prev_cum * cumulative_gex < 0:
                # 符號改變 = Gamma Flip在這兩個行權價之間
                # 線性插值
                weight = abs(prev_cum) / (abs(prev_cum) + abs(cumulative_gex))
                gamma_flip = int(prev_strike + weight * (strike - prev_strike))
                break
            prev_strike = strike
            prev_cum = cumulative_gex

        if gamma_flip:
            gamma_flip_results[expiry] = gamma_flip
            regime = "POS" if spot > gamma_flip else "NEG"
            print(f"Gamma Flip {expiry}: ${gamma_flip:,} | Regime: {regime} ({'Spot above' if regime=='POS' else 'Spot below'}) ✅")
        else:
            # Improved fallback: find strike where cumulative GEX turns negative
            cum_gex = 0
            best_candidate = None
            for s in sorted(gex_by_strike.keys(), reverse=True):
                cum_gex += gex_by_strike[s]
                if cum_gex < 0 and best_candidate is None:
                    best_candidate = s
            if best_candidate is None:
                below = {k: o[k].get('put_oi',0) for k in sorted_strikes if k < spot}
                best_candidate = max(below, key=below.get) if below else sorted_strikes[len(sorted_strikes)//2]
            gamma_flip_results[expiry] = best_candidate
            print(f"Gamma Flip {expiry}: ${best_candidate:,} (improved fallback)")
    data["skew"] = skew_results
    data["gamma_flip"] = gamma_flip_results

    # 主到期日regime
    main_expiry = expiries[0] if expiries else "3JUL26"
    gf = gamma_flip_results.get(main_expiry, spot - 2000)
    data["regime"] = "POS" if spot > gf else "NEG"
    data["gamma_flip_main"] = gf
    print(f"Main Regime: {data['regime']} (GF=${gf:,}, Spot=${spot:,.0f})")

    # ── 額外顆粒度計算 ──────────────────────────────────────
    # 1. OI變化率（需要上次數據）
    try:
        import os as _os
        prev_path = "data/oracle_market_data.json"
        if _os.path.exists(prev_path):
            with open(prev_path) as f:
                prev = json.load(f)
            prev_oi = float(prev.get("oi", 0))
            prev_fr = float(prev.get("fr", 0))
            curr_oi = float(data.get("oi", 0))
            curr_fr = float(data.get("fr", 0))
            data["oi_change"] = round(curr_oi - prev_oi, 4)
            data["fr_change"] = round((curr_fr - prev_fr) * 100, 6)
            data["oi_change_pct"] = round((curr_oi - prev_oi) / prev_oi * 100, 2) if prev_oi > 0 else 0
            print(f"OI change: {data['oi_change']:+.4f}萬 ({data['oi_change_pct']:+.2f}%)")
            print(f"FR change: {data['fr_change']:+.6f}%")
    except Exception as e:
        print(f"OI/FR change calc: {e}")

    # 2. Perp basis（永續合約溢價）
    try:
        r_perp = requests.get(
            "https://www.deribit.com/api/v2/public/get_book_summary_by_instrument?instrument_name=BTC-PERPETUAL",
            timeout=10, headers=UA
        )
        perp_data = r_perp.json().get("result", [{}])[0]
        perp_mark = float(perp_data.get("mark_price", 0))
        spot_price = float(data.get("spot", 0))
        if perp_mark > 0 and spot_price > 0:
            basis = perp_mark - spot_price
            basis_pct = basis / spot_price * 100
            data["perp_basis"] = round(basis, 2)
            data["perp_basis_pct"] = round(basis_pct, 4)
            print(f"Perp basis: ${basis:+.2f} ({basis_pct:+.4f}%) ✅")
    except Exception as e:
        print(f"Perp basis: {e}")

    # 3. ATM IV精確計算（最近ATM行權價的平均IV）
    for expiry in expiries[:1]:  # 只算主要到期日
        o = data.get("options", {}).get(expiry, {})
        if not o:
            continue
        spot_price = float(data.get("spot", 0))
        atm_strikes = sorted(o.keys(), key=lambda x: abs(x - spot_price))[:3]
        call_ivs = [float(o[s].get("call_iv", 0)) for s in atm_strikes if o[s].get("call_iv")]
        put_ivs = [float(o[s].get("put_iv", 0)) for s in atm_strikes if o[s].get("put_iv")]
        if call_ivs and put_ivs:
            atm_iv = (sum(call_ivs)/len(call_ivs) + sum(put_ivs)/len(put_ivs)) / 2
            data[f"atm_iv_{expiry}"] = round(atm_iv, 2)
            print(f"ATM IV {expiry}: {atm_iv:.2f}% ✅")

    # 4. Put/Call OI比率精細化（ATM vs OTM分開）
    for expiry in expiries:
        o = data.get("options", {}).get(expiry, {})
        if not o:
            continue
        spot_price = float(data.get("spot", 0))
        # ATM: spot ±10%
        atm_range = {k: v for k, v in o.items() if 0.90*spot_price <= k <= 1.10*spot_price}
        otm_calls = {k: v for k, v in o.items() if k > spot_price * 1.10}
        otm_puts = {k: v for k, v in o.items() if k < spot_price * 0.90}
        atm_c = sum(float(v.get("call_oi",0)) for v in atm_range.values())
        atm_p = sum(float(v.get("put_oi",0)) for v in atm_range.values())
        otm_c_oi = sum(float(v.get("call_oi",0)) for v in otm_calls.values())
        otm_p_oi = sum(float(v.get("put_oi",0)) for v in otm_puts.values())
        data[f"pcr_atm_{expiry}"] = round(atm_p/atm_c, 3) if atm_c > 0 else 0
        data[f"pcr_otm_{expiry}"] = round(otm_p_oi/otm_c_oi, 3) if otm_c_oi > 0 else 0

    print(f"PCR ATM/OTM calculated for {len(expiries)} expiries ✅")

    # 5. Max Pain精確計算
    for expiry in expiries:
        o = data.get("options", {}).get(expiry, {})
        if not o or len(o) < 3:
            continue
        strikes = sorted(o.keys())
        min_pain = float("inf")
        max_pain_strike = strikes[0]
        for test_strike in strikes:
            # 對每個行權價計算所有期權的損失
            pain = 0
            for k, v in o.items():
                call_oi = float(v.get("call_oi", 0))
                put_oi = float(v.get("put_oi", 0))
                if test_strike > k:
                    pain += call_oi * (test_strike - k)
                elif test_strike < k:
                    pain += put_oi * (k - test_strike)
            if pain < min_pain:
                min_pain = pain
                max_pain_strike = test_strike
        data[f"max_pain_{expiry}"] = max_pain_strike
        print(f"Max Pain {expiry}: ${max_pain_strike:,} ✅")

    # 6. OI集中度（前3大行權價佔總OI比例）
    for expiry in expiries:
        o = data.get("options", {}).get(expiry, {})
        if not o:
            continue
        oi_by_strike = {k: float(v.get("call_oi",0))+float(v.get("put_oi",0)) for k,v in o.items()}
        total_oi = sum(oi_by_strike.values())
        top3_oi = sum(sorted(oi_by_strike.values(), reverse=True)[:3])
        concentration = round(top3_oi/total_oi*100, 1) if total_oi > 0 else 0
        data[f"oi_concentration_{expiry}"] = concentration
        print(f"OI Concentration {expiry}: {concentration}% in top3 ✅")

    # 7. Skew history snapshot（存入rolling list）
    skew_history_path = "data/skew_history.json"
    try:
        import os as _os2
        skew_hist = []
        if _os2.path.exists(skew_history_path):
            with open(skew_history_path) as f:
                skew_hist = json.load(f)
        skew_entry = {
            "ts": data.get("timestamp","")[:16],
            "skew": {exp: data.get("skew",{}).get(exp) for exp in expiries}
        }
        skew_hist.append(skew_entry)
        skew_hist = skew_hist[-48:]  # 保留最近48個快照（約12天）
        with open(skew_history_path, "w") as f:
            json.dump(skew_hist, f)
        print(f"Skew history saved ({len(skew_hist)} snapshots) ✅")
    except Exception as e:
        print(f"Skew history: {e}")

    data["timestamp"] = datetime.now(timezone.utc).isoformat()

    os.makedirs("data", exist_ok=True)
    with open("data/oracle_market_data.json","w") as f:
        json.dump(data, f, indent=2)

    print(f"\n=== 最終數據摘要 ===")
    print(f"Spot:    ${data.get('spot',0):,.2f}")
    print(f"FR:      {data.get('fr',0)*100:+.5f}%")
    print(f"OI:      {data.get('oi',0):.2f}萬")
    print(f"L/S:     {data.get('ls') or 'N/A'}")
    print(f"DVOL:    {data.get('dvol',0):.2f}%")
    print(f"MACD:    {list(data.get('macd',{}).keys())}")
    print(f"到期日:  {data.get('expiries',[])} (週選/月選/季選)")
    print(f"Opts:    {list(data.get('options',{}).keys())}")
    return data

if __name__ == "__main__":
    fetch_all()
