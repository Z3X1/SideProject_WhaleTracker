#!/usr/bin/env python3
"""
Run guard — 每小時 cron 的守門邏輯。
背景：GitHub 免費 runner 對 schedule 延遲 1~4h（實測 02:00→05:41）。
解法：cron 改每小時 15 分觸發（避開整點壅塞），本腳本判斷是否「該跑」：

  目標時段邊界：UTC 02:00 / 08:00 / 14:00 / 20:00（台灣 10/16/22/04）
  規則：找出最近一個 <= now 的邊界 B；
        若 data/oracle_market_data.json 的 timestamp < B → 該時段還沒服務過 → RUN
        否則 → SKIP（快速退出，不耗資源）

  workflow_dispatch（手動觸發）一律 RUN（由 yml 的 if 條件處理，不經此腳本）。

輸出：寫 GITHUB_OUTPUT 的 run=true/false，並 print 診斷。
"""
import json, os
from datetime import datetime, timezone, timedelta

SLOTS_UTC = [2, 8, 14, 20]
DATA_PATH = "data/oracle_market_data.json"

now = datetime.now(timezone.utc)

# 最近一個 <= now 的時段邊界（可能在昨天，例如 now=01:30 → 昨日 20:00）
candidates = []
for d_off in (0, -1):
    day = (now + timedelta(days=d_off)).date()
    for h in SLOTS_UTC:
        b = datetime(day.year, day.month, day.day, h, 0, tzinfo=timezone.utc)
        if b <= now:
            candidates.append(b)
boundary = max(candidates)

last_ts = None
try:
    with open(DATA_PATH) as f:
        raw = json.load(f).get("timestamp", "")
    last_ts = datetime.fromisoformat(raw)
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
except Exception as e:
    print(f"guard: no readable last timestamp ({e}) → RUN")

should_run = last_ts is None or last_ts < boundary

print(f"guard: now={now.isoformat()[:19]} boundary={boundary.isoformat()[:19]} "
      f"last={last_ts.isoformat()[:19] if last_ts else 'N/A'} → {'RUN' if should_run else 'SKIP'}")

out = os.environ.get("GITHUB_OUTPUT")
if out:
    with open(out, "a") as f:
        f.write(f"run={'true' if should_run else 'false'}\n")
