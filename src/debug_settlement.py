#!/usr/bin/env python3
"""一次性診斷：Deribit delivery API 響應 + settlement 匹配過程"""
import requests, json, re
from datetime import date

out = {"steps": []}
try:
    with open("data/settlement_log.json") as f:
        log = json.load(f)
    pending = sorted(set(r["expiry"] for r in log["records"] if r.get("actual_settlement") is None))
    out["pending"] = pending

    r = requests.get(
        "https://www.deribit.com/api/v2/public/get_delivery_prices?index_name=btc_usd&offset=0&count=10",
        timeout=10)
    out["api_status"] = r.status_code
    api_data = r.json()
    out["api_raw"] = api_data.get("result", {})

    MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    today = date.today()
    out["today"] = str(today)

    for expiry_str in pending:
        step = {"expiry": expiry_str}
        m = re.match(r"(\d+)([A-Z]+)(\d+)", expiry_str.upper())
        if not m:
            step["error"] = "regex no match"
            out["steps"].append(step); continue
        expiry_date = date(2000 + int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
        step["expiry_date"] = str(expiry_date)
        step["today_lt_expiry"] = today < expiry_date
        if today < expiry_date:
            out["steps"].append(step); continue
        matches = []
        for d in api_data.get("result", {}).get("data", []):
            raw = d.get("date")
            d_date = date.fromisoformat(str(raw)[:10])
            matches.append({"api_date": str(raw), "parsed": str(d_date), "eq": d_date == expiry_date, "price": d.get("delivery_price")})
        step["matches"] = matches
        out["steps"].append(step)
except Exception as e:
    out["fatal"] = f"{type(e).__name__}: {e}"

with open("data/debug_settlement.json", "w") as f:
    json.dump(out, f, indent=2, default=str)
print(json.dumps(out, indent=2, default=str)[:2000])
