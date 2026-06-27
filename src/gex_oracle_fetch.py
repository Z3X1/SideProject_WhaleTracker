#!/usr/bin/env python3
"""GEX Oracle 數據抓取 - 多源fallback"""
import requests, json, os, time
from datetime import datetime, timezone

UA = {"User-Agent": "Mozilla/5.0 GEX-Oracle/2.0"}

def get(url, **kw):
    try:
        r = requests.get(url, timeout=10, headers=UA, **kw)
        return r.json()
    except:
        return None

def fetch_all():
    data = {}

    # ── SPOT ────────────────────────────────────────────────
    # 嘗試多個來源
    spot_sources = [
        ("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", lambda d: float(d["price"])),
        ("https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT", lambda d: float(d["price"])),
        ("https://api.coinbase.com/v2/prices/BTC-USD/spot", lambda d: float(d["data"]["amount"])),
        ("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", lambda d: float(d["bitcoin"]["usd"])),
        ("https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD", lambda d: float(d["USD"])),
        ("https://api.kraken.com/0/public/Ticker?pair=XBTUSD", lambda d: float(list(d["result"].values())[0]["c"][0])),
        ("https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_usd", lambda d: float(d["result"]["index_price"])),
    ]
    for url, parser in spot_sources:
        try:
            d = get(url)
            val = parser(d)
            if val and val > 10000:
                data["spot"] = val
                print(f"Spot: ${val:,.2f} ✅ ({url[:40]})")
                break
        except Exception as e:
            print(f"Spot fail {url[:40]}: {e}")

    # ── FUNDING RATE ────────────────────────────────────────
    fr_sources = [
        ("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", lambda d: float(d["lastFundingRate"])),
        ("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1", lambda d: float(d[0]["fundingRate"])),
        ("https://open-api.coinglass.com/public/v2/funding?symbol=BTC", lambda d: float([x for x in d["data"] if x["exchangeName"]=="Binance"][0]["rate"])/100),
        # CoinGlass另一端點
        ("https://open-api.coinglass.com/api/pro/v1/futures/funding-rate?symbol=BTC", lambda d: float(d["data"]["Binance"]["rate"])/100),
    ]
    for url, parser in fr_sources:
        try:
            d = get(url)
            val = parser(d)
            if val is not None:
                data["fr"] = val
                print(f"FR: {val*100:+.5f}% ✅ ({url[:40]})")
                break
        except Exception as e:
            print(f"FR fail {url[:40]}: {e}")

    if "fr" not in data:
        data["fr"] = 0.0
        print("FR: fallback 0.0%")

    # ── OPEN INTEREST ───────────────────────────────────────
    oi_sources = [
        ("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT", lambda d: float(d["openInterest"])/10000),
        ("https://open-api.coinglass.com/public/v2/open_interest?symbol=BTC", lambda d: float([x for x in d["data"] if x["exchangeName"]=="Binance"][0]["openInterest"])/10000),
        ("https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=5m&limit=1", lambda d: float(d[0]["sumOpenInterest"])/10000),
    ]
    for url, parser in oi_sources:
        try:
            d = get(url)
            val = parser(d)
            if val and val > 0:
                data["oi"] = val
                print(f"OI: {val:.2f}萬 ✅ ({url[:40]})")
                break
        except Exception as e:
            print(f"OI fail {url[:40]}: {e}")

    if "oi" not in data:
        data["oi"] = 10.5
        print("OI: fallback 10.5萬")

    # ── LONG/SHORT ──────────────────────────────────────────
    ls_sources = [
        ("https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=1", lambda d: float(d[0]["longShortRatio"])),
        ("https://fapi.binance.com/futures/data/topLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=1", lambda d: float(d[0]["longShortRatio"])),
        ("https://open-api.coinglass.com/public/v2/ls_ratio?symbol=BTC&period=5m", lambda d: float(d["data"][0]["longRatio"])/float(d["data"][0]["shortRatio"])),
    ]
    for url, parser in ls_sources:
        try:
            d = get(url)
            val = parser(d)
            if val and val > 0:
                data["ls"] = val
                print(f"L/S: {val:.4f} ✅ ({url[:40]})")
                break
        except Exception as e:
            print(f"L/S fail {url[:40]}: {e}")

    if "ls" not in data:
        data["ls"] = 2.0
        print("L/S: fallback 2.0")

    # ── DVOL ────────────────────────────────────────────────
    dvol_sources = [
        ("https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&resolution=3600&count=2", lambda d: float(d["result"]["data"][-1][4])),
        ("https://www.deribit.com/api/v2/public/get_index_price_names", None),  # 探测
    ]
    for url, parser in dvol_sources:
        if parser is None:
            continue
        try:
            d = get(url)
            val = parser(d)
            if val and 10 < val < 300:
                data["dvol"] = val
                print(f"DVOL: {val:.2f}% ✅")
                break
        except Exception as e:
            print(f"DVOL fail: {e}")

    if "dvol" not in data:
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
        return dif[-1], dea[-1], macd[-1]

    data["macd"] = {}
    data["ema"] = {}

    kline_sources = [
        "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval={tf}&limit=100",
        "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={tf}&limit=100",
        "https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval={kraken_tf}&count=100",
    ]
    tf_map = {"15m": ("15m", 15), "4h": ("4h", 240), "1d": ("1d", 1440)}

    for tf, (binance_tf, kraken_tf) in tf_map.items():
        got = False
        # 嘗試Binance
        for url_tpl in [
            f"https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval={binance_tf}&limit=100",
            f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={binance_tf}&limit=100",
        ]:
            try:
                d = get(url_tpl)
                if isinstance(d, list) and len(d) > 30:
                    closes = [float(k[4]) for k in d]
                    dif, dea, macd = calc_macd(closes)
                    data["macd"][tf] = {"dif": round(dif,2), "dea": round(dea,2), "macd": round(macd,2)}
                    data["ema"][tf] = {str(p): round(ema(closes,p),1) for p in [5,10,30,200] if len(closes)>=p}
                    print(f"MACD {tf}: DIF={dif:.2f} ✅")
                    got = True
                    break
            except Exception as e:
                print(f"Kline {tf} fail: {e}")
        
        if not got:
            # Kraken fallback
            try:
                d = get(f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval={kraken_tf}&count=100")
                pairs = d.get("result", {})
                key = [k for k in pairs if k != "last"][0]
                closes = [float(row[4]) for row in pairs[key]]
                if len(closes) > 30:
                    dif, dea, macd = calc_macd(closes)
                    data["macd"][tf] = {"dif": round(dif,2), "dea": round(dea,2), "macd": round(macd,2)}
                    data["ema"][tf] = {str(p): round(ema(closes,p),1) for p in [5,10,30,200] if len(closes)>=p}
                    print(f"MACD {tf}: DIF={dif:.2f} ✅ (Kraken)")
                    got = True
            except Exception as e:
                print(f"Kraken {tf} fail: {e}")

        time.sleep(0.3)

    # ── OPTIONS ─────────────────────────────────────────────
    data["options"] = {}
    try:
        d = get("https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option")
        items = d.get("result", []) if d else []
        for expiry in ["3JUL26", "31JUL26", "25SEP26"]:
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
                oi = float(item.get("open_interest",0))
                iv = float(item.get("mark_iv",0))
                if opt_type=="C": opts[strike]["call_oi"]=oi; opts[strike]["call_iv"]=iv
                else: opts[strike]["put_oi"]=oi; opts[strike]["put_iv"]=iv
            if opts:
                data["options"][expiry] = opts
                print(f"Opts {expiry}: {len(opts)} strikes ✅")
    except Exception as e:
        print(f"Options fail: {e}")

    data["timestamp"] = datetime.now(timezone.utc).isoformat()

    # 補Spot：若所有API都失敗，從Deribit index取
    if not data.get("spot"):
        try:
            d = get("https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_usd")
            val = float(d["result"]["index_price"])
            if val > 10000:
                data["spot"] = val
                print(f"Spot (Deribit fallback): ${val:,.2f} ✅")
        except:
            pass

    os.makedirs("data", exist_ok=True)
    with open("data/oracle_market_data.json","w") as f:
        json.dump(data, f, indent=2)

    print(f"\n=== 最終數據摘要 ===")
    print(f"Spot:  ${data.get('spot',0):,.2f}")
    print(f"FR:    {data.get('fr',0)*100:+.5f}%")
    print(f"OI:    {data.get('oi',0):.2f}萬")
    print(f"L/S:   {data.get('ls',0):.4f}")
    print(f"DVOL:  {data.get('dvol',0):.2f}%")
    print(f"MACD:  {list(data.get('macd',{}).keys())}")
    print(f"Opts:  {list(data.get('options',{}).keys())}")
    return data

if __name__ == "__main__":
    fetch_all()
