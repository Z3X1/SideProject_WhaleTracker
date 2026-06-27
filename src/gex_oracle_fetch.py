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

    today = datetime.now(timezone.utc).date()
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

    # ── LONG/SHORT（真實數據）──────────────────────────────
    ls_sources = [
        # Binance
        ("https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=1",
         lambda d: float(d[0]["longShortRatio"])),
        ("https://fapi.binance.com/futures/data/topLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=1",
         lambda d: float(d[0]["longShortRatio"])),
        ("https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol=BTCUSDT&period=5m&limit=1",
         lambda d: float(d[0]["longShortRatio"])),
        # Bybit
        ("https://api.bybit.com/v5/market/account-ratio?category=linear&symbol=BTCUSDT&period=5min&limit=1",
         lambda d: float(d["result"]["list"][0]["buyRatio"])/float(d["result"]["list"][0]["sellRatio"])),
        ("https://api.bybit.com/v5/market/account-ratio?category=linear&symbol=BTCUSDT&period=1h&limit=1",
         lambda d: float(d["result"]["list"][0]["buyRatio"])/float(d["result"]["list"][0]["sellRatio"])),
        # OKX
        # OKX: longRatio是0-1小數，需轉換為ratio
        ("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio-contract?ccy=BTC&period=5m&limit=1",
         lambda d: float(d["data"][0][1])/(1-float(d["data"][0][1])) if float(d["data"][0][1]) not in [0,1] else None),
        ("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=5m&limit=1",
         lambda d: float(d["data"][0][1])/(1-float(d["data"][0][1])) if float(d["data"][0][1]) not in [0,1] else None),
        # CoinGlass
        ("https://open-api.coinglass.com/public/v2/long_short?symbol=BTC&period=5m",
         lambda d: float(d["data"][0]["longRatio"])/float(d["data"][0]["shortRatio"]) if d.get("data") else None),
        # Bybit 1d period
        ("https://api.bybit.com/v5/market/account-ratio?category=linear&symbol=BTCUSDT&period=1d&limit=1",
         lambda d: float(d["result"]["list"][0]["buyRatio"])/float(d["result"]["list"][0]["sellRatio"])),
    ]
    for url, parser in ls_sources:
        try:
            d = get(url)
            val = parser(d)
            if val and val > 0:
                data["ls"] = round(val, 4)
                print(f"L/S: {val:.4f} ✅")
                break
        except: pass

    if "ls" not in data:
        data["ls"] = 2.0
        print("L/S: fallback 2.0")

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

    data["timestamp"] = datetime.now(timezone.utc).isoformat()

    os.makedirs("data", exist_ok=True)
    with open("data/oracle_market_data.json","w") as f:
        json.dump(data, f, indent=2)

    print(f"\n=== 最終數據摘要 ===")
    print(f"Spot:    ${data.get('spot',0):,.2f}")
    print(f"FR:      {data.get('fr',0)*100:+.5f}%")
    print(f"OI:      {data.get('oi',0):.2f}萬")
    print(f"L/S:     {data.get('ls',0):.4f}")
    print(f"DVOL:    {data.get('dvol',0):.2f}%")
    print(f"MACD:    {list(data.get('macd',{}).keys())}")
    print(f"到期日:  {data.get('expiries',[])} (週選/月選/季選)")
    print(f"Opts:    {list(data.get('options',{}).keys())}")
    return data

if __name__ == "__main__":
    fetch_all()
