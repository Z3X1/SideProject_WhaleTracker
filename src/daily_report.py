"""
daily_report.py
GEX Oracle — Whale On-Chain Behavior Dashboard Generator v2.0
Outputs to docs/ for GitHub Pages hosting.
Runs every hour (not just daily).

Dashboard structure (5 tabs):
  Tab 1 — Overview     : signal score, KPIs, hourly time series
  Tab 2 — Addresses    : Top 41 whale balance snapshot + delta
  Tab 3 — Transactions : All large txs (>100 BTC), exchange flow breakdown
  Tab 4 — Signals      : Sync events, dormant wakes, hard triggers
  Tab 5 — Oracle       : Unified Field equation behavior component
"""

import sqlite3, json
from pathlib import Path
from datetime import datetime, timezone, timedelta

DB_PATH   = Path("data/whale.db")
DOCS_DIR  = Path("docs")
OUT_PATH  = DOCS_DIR / "index.html"

def load_data(hours: int = 24) -> dict:
    if not DB_PATH.exists():
        return {}
    conn   = sqlite3.connect(DB_PATH)
    c      = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    # Hourly signal scores (24h)
    c.execute("""SELECT ts, signal_score, net_exchange_flow,
                        exchange_inflow, exchange_outflow,
                        sync_event_count, dormant_wake_count, top_signal
                 FROM hourly_summary WHERE ts >= ? ORDER BY ts ASC""", (cutoff,))
    hourly = [{"ts": r[0], "score": r[1], "net_flow": r[2],
               "inflow": r[3], "outflow": r[4],
               "sync": r[5], "dormant": r[6], "top": r[7]}
              for r in c.fetchall()]

    # Current address snapshot
    c.execute("""SELECT address, label, balance_btc, balance_btc_total,
                        tx_count, balance_delta, rank, source
                 FROM address_snapshots
                 WHERE ts = (SELECT MAX(ts) FROM address_snapshots)
                 ORDER BY rank ASC""")
    addresses = [{"address": r[0], "label": r[1], "balance": r[2],
                  "balance_total": r[3], "tx_count": r[4],
                  "delta": r[5], "rank": r[6], "source": r[7]}
                 for r in c.fetchall()]

    # Large transactions (>10 BTC) last 24h
    c.execute("""SELECT txid, address, ts_block, direction, value_btc,
                        counterparty, fee_sat, input_count, output_count, block_height
                 FROM transactions
                 WHERE ts_block >= ? AND value_btc >= 10
                 ORDER BY value_btc DESC LIMIT 100""", (cutoff,))
    large_txs = [{"txid": r[0], "address": r[1][:20]+"...",
                  "ts": r[2], "dir": r[3], "btc": r[4],
                  "cpty": r[5], "fee": r[6], "ins": r[7],
                  "outs": r[8], "block": r[9]}
                 for r in c.fetchall()]

    # Exchange flow breakdown (24h)
    c.execute("""SELECT direction, counterparty,
                        COUNT(*) as n, SUM(value_btc) as vol,
                        MAX(value_btc) as max_btc
                 FROM transactions
                 WHERE ts_block >= ? AND counterparty IS NOT NULL
                 GROUP BY direction, counterparty
                 ORDER BY vol DESC""", (cutoff,))
    ex_flows = [{"dir": r[0], "exchange": r[1], "count": r[2],
                 "vol": r[3], "max": r[4]}
                for r in c.fetchall()]

    # Behavior signals (24h)
    c.execute("""SELECT ts, signal_type, strength, address_count,
                        btc_volume, direction, description
                 FROM behavior_signals WHERE ts >= ?
                 ORDER BY ts DESC""", (cutoff,))
    signals = [{"ts": r[0], "type": r[1], "strength": r[2],
                "n": r[3], "btc": r[4], "dir": r[5], "desc": r[6]}
               for r in c.fetchall()]

    # Oracle signal
    oracle = {}
    try:
        with open("data/oracle_signal.json") as f:
            oracle = json.load(f)
    except: pass

    conn.close()
    return {"hourly": hourly, "addresses": addresses,
            "large_txs": large_txs, "ex_flows": ex_flows,
            "signals": signals, "oracle": oracle}


def build_html(d: dict) -> str:
    now      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hourly   = d.get("hourly", [])
    addrs    = d.get("addresses", [])
    txs      = d.get("large_txs", [])
    ex_flows = d.get("ex_flows", [])
    signals  = d.get("signals", [])
    oracle   = d.get("oracle", {})

    # KPIs
    latest_score = hourly[-1]["score"] if hourly else 0
    total_inflow  = sum(h["inflow"]  for h in hourly)
    total_outflow = sum(h["outflow"] for h in hourly)
    net_flow      = total_outflow - total_inflow
    total_btc_tracked = sum(a["balance"] for a in addrs if a["balance"])

    score_color = "#3fb950" if latest_score > 0 else "#f85149" if latest_score < 0 else "#8b949e"
    flow_color  = "#3fb950" if net_flow > 0 else "#f85149"

    # Chart data
    h_labels = json.dumps([h["ts"][11:16] for h in hourly])
    h_scores = json.dumps([h["score"]    for h in hourly])
    h_flows  = json.dumps([h["net_flow"] for h in hourly])
    h_inflow = json.dumps([h["inflow"]   for h in hourly])
    h_outflow= json.dumps([h["outflow"]  for h in hourly])

    # Address rows
    addr_rows = ""
    for a in addrs:
        delta_color = "#3fb950" if (a["delta"] or 0) > 0 else "#f85149" if (a["delta"] or 0) < 0 else "#8b949e"
        label = a["label"] or "—"
        addr_rows += f"""
        <tr>
          <td>#{a['rank']}</td>
          <td class="mono">{(a['address'] or '')[:22]}...</td>
          <td style="color:#58a6ff">{label}</td>
          <td class="num">{(a['balance'] or 0):,.2f}</td>
          <td class="num" style="color:{delta_color}">{(a['delta'] or 0):+,.4f}</td>
          <td class="num">{(a['tx_count'] or 0):,}</td>
          <td style="color:#8b949e;font-size:11px">{a['source'] or '—'}</td>
        </tr>"""

    # TX rows
    tx_rows = ""
    for t in txs[:50]:
        dir_color = "#3fb950" if t["dir"] == "out" else "#f85149"
        ts_str = (t["ts"] or "")[:16].replace("T"," ")
        fee_str = f"{t['fee']:,}" if t["fee"] else "—"
        tx_rows += f"""
        <tr>
          <td class="mono" style="font-size:10px">{(t['txid'] or '')[:18]}...</td>
          <td class="mono" style="font-size:10px">{t['address']}</td>
          <td>{ts_str}</td>
          <td style="color:{dir_color};font-weight:600">{t['dir'].upper()}</td>
          <td class="num" style="color:#d29922;font-weight:600">{t['btc']:,.2f}</td>
          <td style="color:#58a6ff">{t['cpty'] or '—'}</td>
          <td class="num" style="color:#8b949e">{fee_str}</td>
          <td style="color:#8b949e">{t['ins'] or 0}→{t['outs'] or 0}</td>
          <td style="color:#8b949e">{t['block'] or '—'}</td>
        </tr>"""

    # Exchange flow rows
    ef_rows = ""
    for e in ex_flows:
        dir_color = "#3fb950" if e["dir"] == "out" else "#f85149"
        ef_rows += f"""
        <tr>
          <td style="color:#58a6ff">{e['exchange']}</td>
          <td style="color:{dir_color};font-weight:600">{e['dir'].upper()}</td>
          <td class="num">{e['count']:,}</td>
          <td class="num" style="color:#d29922">{(e['vol'] or 0):,.2f}</td>
          <td class="num">{(e['max'] or 0):,.2f}</td>
        </tr>"""

    # Signal rows
    sig_rows = ""
    for s in signals[:30]:
        scolor = "#3fb950" if s["dir"]=="bull" else "#f85149" if s["dir"]=="bear" else "#8b949e"
        strength_pct = int((s["strength"] or 0) * 100)
        sig_rows += f"""
        <tr>
          <td style="font-size:10px;color:#8b949e">{(s['ts'] or '')[:16]}</td>
          <td style="color:#bc8cff;font-weight:600">{s['type']}</td>
          <td>
            <div style="background:#21262d;border-radius:3px;height:8px;width:80px">
              <div style="background:{scolor};height:8px;border-radius:3px;width:{strength_pct}%"></div>
            </div>
          </td>
          <td class="num">{s['n'] or 1}</td>
          <td class="num">{(s['btc'] or 0):,.1f}</td>
          <td style="color:{scolor}">{(s['dir'] or '').upper()}</td>
          <td style="font-size:11px;color:#8b949e">{s['desc'] or ''}</td>
        </tr>"""

    # Oracle component breakdown
    ws    = oracle.get("whale_signal", {})
    comps = ws.get("components", {})
    oracle_rows = ""
    comp_labels = {"exchange_flow": "Exchange Flow", "sync_move": "Sync Move",
                   "dormant_wake": "Dormant Wake", "balance_delta": "Balance Delta"}
    for key, label in comp_labels.items():
        comp = comps.get(key, {})
        sc   = comp.get("score", 0)
        wt   = comp.get("weight", 0)
        contrib = sc * wt
        bar_color = "#3fb950" if sc > 0 else "#f85149"
        bar_w = int(abs(sc) * 50)
        oracle_rows += f"""
        <tr>
          <td style="color:#c9d1d9">{label}</td>
          <td class="num" style="color:#8b949e">{wt:.0%}</td>
          <td>
            <div style="display:flex;align-items:center;gap:4px">
              <div style="width:50px;text-align:right;color:#8b949e;font-size:10px">
                {"◀" if sc<0 else ""}</div>
              <div style="background:#21262d;border-radius:3px;height:10px;width:100px;position:relative">
                <div style="background:{bar_color};height:10px;border-radius:3px;
                     width:{bar_w}px;{'margin-left:'+(str(50-bar_w))+'px' if sc<0 else ''}"></div>
              </div>
              <div style="width:50px;color:#8b949e;font-size:10px">{"▶" if sc>0 else ""}</div>
            </div>
          </td>
          <td class="num" style="color:{'#3fb950' if sc>0 else '#f85149'}">{sc:+.3f}</td>
          <td class="num" style="color:#d29922">{contrib:+.4f}</td>
        </tr>"""

    hard_triggers = ws.get("hard_triggers", [])
    trigger_html = ""
    if hard_triggers:
        for t in hard_triggers:
            trigger_html += f'<div class="trigger">⚡ [{t["type"]}] {t["message"]}</div>'
    else:
        trigger_html = '<div style="color:#3fb950">✅ No hard triggers active</div>'

    narrative = oracle.get("narrative", "Insufficient data — building signal history...")
    behavior_component = oracle.get("oracle_input", {}).get("behavior_component", 0)
    confidence = ws.get("confidence", 0)
    trend = ws.get("trend", "UNKNOWN")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GEX Oracle — Whale Tracker</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg:#0d1117; --surface:#161b22; --surface2:#1c2128;
    --border:#30363d; --border2:#21262d;
    --text:#c9d1d9; --muted:#8b949e;
    --green:#3fb950; --red:#f85149; --yellow:#d29922;
    --blue:#58a6ff; --purple:#bc8cff;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px}}
  header{{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 24px;
          display:flex;align-items:center;justify-content:space-between}}
  header h1{{font-size:15px;font-weight:600;color:var(--blue)}}
  header .meta{{color:var(--muted);font-size:11px}}
  .tabs{{background:var(--surface);border-bottom:1px solid var(--border);
         display:flex;gap:0;padding:0 24px}}
  .tab{{padding:10px 18px;cursor:pointer;font-size:12px;color:var(--muted);
        border-bottom:2px solid transparent;transition:all .15s}}
  .tab:hover{{color:var(--text)}}
  .tab.active{{color:var(--blue);border-bottom-color:var(--blue)}}
  .pane{{display:none;padding:20px 24px;max-width:1400px;margin:0 auto}}
  .pane.active{{display:block}}
  .grid3{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:16px}}
  .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px}}
  .grid4{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:16px}}
  .card{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px}}
  .card h2{{font-size:11px;text-transform:uppercase;letter-spacing:.08em;
            color:var(--muted);margin-bottom:10px}}
  .kpi{{font-size:26px;font-weight:700}}
  .sub{{font-size:11px;color:var(--muted);margin-top:4px}}
  table{{width:100%;border-collapse:collapse}}
  th{{text-align:left;color:var(--muted);font-size:10px;text-transform:uppercase;
      padding:6px 8px;border-bottom:1px solid var(--border);white-space:nowrap}}
  td{{padding:6px 8px;border-bottom:1px solid var(--border2);vertical-align:middle}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:var(--surface2)}}
  .mono{{font-family:'SF Mono',Consolas,monospace}}
  .num{{text-align:right;font-variant-numeric:tabular-nums}}
  .badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600}}
  .badge-bull{{background:rgba(63,185,80,.15);color:var(--green)}}
  .badge-bear{{background:rgba(248,81,73,.15);color:var(--red)}}
  .badge-neutral{{background:rgba(139,148,158,.15);color:var(--muted)}}
  .trigger{{background:rgba(210,153,34,.1);border:1px solid rgba(210,153,34,.3);
            border-radius:6px;padding:8px 12px;margin-bottom:8px;color:var(--yellow)}}
  .score-big{{font-size:48px;font-weight:800;line-height:1}}
  canvas{{width:100%!important}}
  .section-title{{font-size:12px;font-weight:600;color:var(--muted);
                  text-transform:uppercase;letter-spacing:.08em;
                  margin:20px 0 10px;padding-bottom:6px;
                  border-bottom:1px solid var(--border)}}
</style>
</head>
<body>
<header>
  <h1>🐳 GEX Oracle — Whale On-Chain Behavior Tracker</h1>
  <div class="meta">Updated: {now} | 41 addresses | Source: mempool.space / blockstream.info</div>
</header>

<div class="tabs">
  <div class="tab active" onclick="show('overview',this)">Overview</div>
  <div class="tab" onclick="show('addresses',this)">Addresses</div>
  <div class="tab" onclick="show('transactions',this)">Transactions</div>
  <div class="tab" onclick="show('signals',this)">Signals</div>
  <div class="tab" onclick="show('oracle',this)">Oracle Input</div>
</div>

<!-- TAB 1: OVERVIEW -->
<div id="overview" class="pane active">
  <div class="grid4">
    <div class="card">
      <h2>Whale Signal Score</h2>
      <div class="kpi" style="color:{score_color}">{latest_score:+.3f}</div>
      <div class="sub">-1.0 bearish ← 0 → +1.0 bullish</div>
    </div>
    <div class="card">
      <h2>24h Exchange Net Flow</h2>
      <div class="kpi" style="color:{flow_color}">{net_flow:+,.0f} BTC</div>
      <div class="sub">↑ outflow = bullish | ↓ inflow = bearish</div>
    </div>
    <div class="card">
      <h2>Total BTC Tracked</h2>
      <div class="kpi" style="color:var(--blue)">{total_btc_tracked:,.0f}</div>
      <div class="sub">{len(addrs)} addresses | ≈ ${total_btc_tracked * 105000:,.0f}M @ $105k</div>
    </div>
    <div class="card">
      <h2>Large Txs (24h)</h2>
      <div class="kpi" style="color:var(--purple)">{len(txs)}</div>
      <div class="sub">Transactions ≥ 10 BTC captured</div>
    </div>
  </div>

  <div class="grid2">
    <div class="card">
      <h2>24h Signal Score</h2>
      <canvas id="scoreChart" height="160"></canvas>
    </div>
    <div class="card">
      <h2>24h Exchange Flow (BTC)</h2>
      <canvas id="flowChart" height="160"></canvas>
    </div>
  </div>

  <div class="grid2">
    <div class="card">
      <h2>Exchange Inflow vs Outflow (BTC)</h2>
      <canvas id="stackChart" height="160"></canvas>
    </div>
    <div class="card">
      <h2>Hourly Sync Events</h2>
      <canvas id="syncChart" height="160"></canvas>
    </div>
  </div>
</div>

<!-- TAB 2: ADDRESSES -->
<div id="addresses" class="pane">
  <div class="section-title">Whale Address Balance Snapshot — {now}</div>
  <table>
    <thead><tr>
      <th>Rank</th><th>Address</th><th>Label</th>
      <th class="num">Balance (BTC)</th><th class="num">Δ BTC</th>
      <th class="num">TX Count</th><th>API Source</th>
    </tr></thead>
    <tbody>{addr_rows}</tbody>
  </table>
</div>

<!-- TAB 3: TRANSACTIONS -->
<div id="transactions" class="pane">
  <div class="section-title">Exchange Flow Breakdown — 24h</div>
  <table style="margin-bottom:24px">
    <thead><tr>
      <th>Exchange</th><th>Direction</th><th class="num">Count</th>
      <th class="num">Volume (BTC)</th><th class="num">Largest (BTC)</th>
    </tr></thead>
    <tbody>{ef_rows}</tbody>
  </table>

  <div class="section-title">Large Transactions ≥ 10 BTC — 24h (top 50)</div>
  <table>
    <thead><tr>
      <th>TXID</th><th>Address</th><th>Time (UTC)</th><th>Dir</th>
      <th class="num">BTC</th><th>Counterparty</th>
      <th class="num">Fee (sat)</th><th>In→Out</th><th>Block</th>
    </tr></thead>
    <tbody>{tx_rows}</tbody>
  </table>
</div>

<!-- TAB 4: SIGNALS -->
<div id="signals" class="pane">
  <div class="section-title">Hard Trigger Status</div>
  <div style="margin-bottom:20px">{trigger_html}</div>

  <div class="section-title">Behavior Signal Log — 24h</div>
  <table>
    <thead><tr>
      <th>Time</th><th>Type</th><th>Strength</th><th class="num">Addresses</th>
      <th class="num">BTC Volume</th><th>Direction</th><th>Description</th>
    </tr></thead>
    <tbody>{sig_rows if sig_rows else '<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:20px">No signals yet — accumulating data...</td></tr>'}</tbody>
  </table>
</div>

<!-- TAB 5: ORACLE INPUT -->
<div id="oracle" class="pane">
  <div class="grid3" style="margin-bottom:20px">
    <div class="card">
      <h2>Whale Behavior Raw Score</h2>
      <div class="score-big" style="color:{score_color}">{ws.get('raw_score',0):+.3f}</div>
      <div class="sub">Trend: {trend}</div>
    </div>
    <div class="card">
      <h2>Weighted Contribution</h2>
      <div class="score-big" style="color:var(--yellow)">{behavior_component:+.4f}</div>
      <div class="sub">= score × 0.28 (behavior weight in Unified Field Eq.)</div>
    </div>
    <div class="card">
      <h2>Signal Confidence</h2>
      <div class="score-big" style="color:var(--blue)">{confidence:.0%}</div>
      <div class="sub">Based on data depth (hours × tx coverage)</div>
    </div>
  </div>

  <div class="section-title">Unified Field Equation</div>
  <div class="card" style="margin-bottom:20px;font-family:monospace;font-size:12px;line-height:2">
    P(settlement at X) =<br>
    &nbsp;&nbsp;0.40 × GBM<br>
    &nbsp;&nbsp;0.10 × GEX<br>
    &nbsp;&nbsp;<span style="color:var(--yellow)">0.28 × BehaviorSignal ← whale on-chain ({behavior_component:+.4f})</span><br>
    &nbsp;&nbsp;0.12 × Bayesian<br>
    &nbsp;&nbsp;0.10 × TimeDecay
  </div>

  <div class="section-title">Component Breakdown</div>
  <table>
    <thead><tr>
      <th>Component</th><th class="num">Weight</th>
      <th>Score Bar</th><th class="num">Score</th><th class="num">Contribution</th>
    </tr></thead>
    <tbody>{oracle_rows}</tbody>
  </table>

  <div class="section-title">Narrative</div>
  <div class="card" style="color:var(--text);line-height:1.6">{narrative}</div>
</div>

<script>
const chartOpts = (yMin, yMax) => ({{
  responsive:true,
  animation:false,
  plugins:{{legend:{{display:false}}}},
  scales:{{
    x:{{ticks:{{color:'#8b949e',font:{{size:10}},maxRotation:0,autoSkip:true,maxTicksLimit:12}},
        grid:{{color:'#21262d'}}}},
    y:{{ticks:{{color:'#8b949e',font:{{size:10}}}},
        grid:{{color:'#21262d'}},
        ...(yMin!==null ? {{min:yMin,max:yMax}} : {{}})}}
  }}
}});

const labels  = {h_labels};
const scores  = {h_scores};
const flows   = {h_flows};
const inflow  = {h_inflow};
const outflow = {h_outflow};
const syncs   = {json.dumps([h["sync"] for h in hourly])};

new Chart(document.getElementById('scoreChart'),{{
  type:'line',
  data:{{labels,datasets:[{{data:scores,borderColor:'#58a6ff',
    backgroundColor:'rgba(88,166,255,.06)',borderWidth:1.5,
    pointRadius:2,fill:true,tension:0.3}}]}},
  options:chartOpts(-1,1)
}});

new Chart(document.getElementById('flowChart'),{{
  type:'bar',
  data:{{labels,datasets:[{{data:flows,
    backgroundColor:flows.map(v=>v>=0?'rgba(63,185,80,.6)':'rgba(248,81,73,.6)'),
    borderWidth:0}}]}},
  options:chartOpts(null,null)
}});

new Chart(document.getElementById('stackChart'),{{
  type:'bar',
  data:{{labels,datasets:[
    {{label:'Inflow',data:inflow.map(v=>-v),backgroundColor:'rgba(248,81,73,.5)',borderWidth:0}},
    {{label:'Outflow',data:outflow,backgroundColor:'rgba(63,185,80,.5)',borderWidth:0}}
  ]}},
  options:{{...chartOpts(null,null),plugins:{{legend:{{display:true,
    labels:{{color:'#8b949e',font:{{size:10}}}}}}}}}}
}});

new Chart(document.getElementById('syncChart'),{{
  type:'bar',
  data:{{labels,datasets:[{{data:syncs,
    backgroundColor:'rgba(188,140,255,.6)',borderWidth:0}}]}},
  options:chartOpts(null,null)
}});

function show(id, el) {{
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  el.classList.add('active');
}}
</script>
</body>
</html>"""


def generate():
    print("[dashboard] Loading data...")
    d = load_data(hours=24)
    if not d:
        print("[dashboard] No data yet — creating placeholder")
        d = {"hourly":[], "addresses":[], "large_txs":[],
             "ex_flows":[], "signals":[], "oracle":{}}

    print("[dashboard] Building HTML...")
    html = build_html(d)

    DOCS_DIR.mkdir(exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"[dashboard] Written → {OUT_PATH} ({len(html):,} bytes)")
    print(f"[dashboard] Live at: https://z3x1.github.io/SideProject_WhaleTracker/")


if __name__ == "__main__":
    generate()
