"""
daily_report.py
每日鯨魚行為完整報告 + HTML Dashboard 生成器 v1.0
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

DB_PATH   = Path("data/whale.db")
DASH_PATH = Path("dashboard/whale_dashboard.html")

def generate_daily_report() -> dict:
    """生成過去 24 小時完整數據摘要"""
    if not DB_PATH.exists():
        print("[daily_report] 無數據庫，跳過")
        return {}

    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    # 24h 交易所流量
    c.execute("""
        SELECT
            direction,
            counterparty,
            COUNT(*) as tx_count,
            SUM(value_btc) as total_btc,
            AVG(value_btc) as avg_btc,
            MAX(value_btc) as max_btc
        FROM transactions
        WHERE ts_block >= ? AND counterparty IS NOT NULL
        GROUP BY direction, counterparty
        ORDER BY total_btc DESC
    """, (cutoff,))
    exchange_breakdown = [
        {"direction": r[0], "exchange": r[1], "tx_count": r[2],
         "total_btc": round(r[3], 2), "avg_btc": round(r[4], 2), "max_btc": round(r[5], 2)}
        for r in c.fetchall()
    ]

    # Top 10 最大單筆交易
    c.execute("""
        SELECT txid, address, ts_block, direction, value_btc, counterparty
        FROM transactions
        WHERE ts_block >= ?
        ORDER BY value_btc DESC
        LIMIT 10
    """, (cutoff,))
    top_txs = [
        {"txid": r[0][:16]+"...", "address": r[1][:16]+"...", "ts_block": r[2],
         "direction": r[3], "value_btc": round(r[4], 2), "counterparty": r[5]}
        for r in c.fetchall()
    ]

    # 24h 信號評分時序
    c.execute("""
        SELECT ts, signal_score, net_exchange_flow, sync_event_count, dormant_wake_count
        FROM hourly_summary
        WHERE ts >= ?
        ORDER BY ts ASC
    """, (cutoff,))
    hourly_scores = [
        {"ts": r[0], "score": r[1], "net_flow": r[2],
         "sync_events": r[3], "dormant_wakes": r[4]}
        for r in c.fetchall()
    ]

    # 地址餘額排行（當前快照）
    c.execute("""
        SELECT address, label, balance_btc, balance_delta, rank
        FROM address_snapshots
        WHERE ts = (SELECT MAX(ts) FROM address_snapshots)
        ORDER BY rank ASC
        LIMIT 20
    """)
    top_addresses = [
        {"address": r[0], "label": r[1], "balance_btc": round(r[2], 2),
         "balance_delta": round(r[3], 4), "rank": r[4]}
        for r in c.fetchall()
    ]

    conn.close()

    report = {
        "date":               datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "exchange_breakdown": exchange_breakdown,
        "top_txs":            top_txs,
        "hourly_scores":      hourly_scores,
        "top_addresses":      top_addresses,
    }

    date_str = report["date"]
    Path("data").mkdir(exist_ok=True)
    with open(f"data/daily_{date_str}.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 生成 HTML Dashboard
    generate_html_dashboard(report)

    print(f"[daily_report] 完成：data/daily_{date_str}.json + dashboard/whale_dashboard.html")
    return report


def generate_html_dashboard(report: dict):
    """生成深色主題鯨魚行為 Dashboard（與 GEX Oracle 視覺一致）"""
    hourly_labels = json.dumps([s["ts"][11:16] for s in report["hourly_scores"]])
    hourly_scores = json.dumps([s["score"] for s in report["hourly_scores"]])
    hourly_flows  = json.dumps([s["net_flow"] for s in report["hourly_scores"]])

    exchange_rows = "".join(f"""
      <tr>
        <td>{r['exchange']}</td>
        <td class="{'text-green' if r['direction']=='out' else 'text-red'}">{r['direction'].upper()}</td>
        <td>{r['tx_count']}</td>
        <td>{r['total_btc']:,.1f}</td>
        <td>{r['avg_btc']:,.1f}</td>
        <td>{r['max_btc']:,.1f}</td>
      </tr>""" for r in report["exchange_breakdown"])

    top_tx_rows = "".join(f"""
      <tr>
        <td class="mono">{r['txid']}</td>
        <td class="mono small">{r['address']}</td>
        <td>{r['ts_block'][11:16] if r['ts_block'] else '-'}</td>
        <td class="{'text-green' if r['direction']=='out' else 'text-red'}">{r['direction'].upper()}</td>
        <td class="text-yellow">{r['value_btc']:,.1f}</td>
        <td>{r['counterparty'] or '-'}</td>
      </tr>""" for r in report["top_txs"])

    addr_rows = "".join(f"""
      <tr>
        <td>#{r['rank']}</td>
        <td class="mono small">{r['address'][:20]}...</td>
        <td class="text-yellow">{r['label'] or '—'}</td>
        <td>{r['balance_btc']:,.0f}</td>
        <td class="{'text-green' if r['balance_delta'] > 0 else 'text-red' if r['balance_delta'] < 0 else ''}">{r['balance_delta']:+,.2f}</td>
      </tr>""" for r in report["top_addresses"])

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GEX Oracle — 鯨魚鏈上行為 Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg:       #0d1117;
    --surface:  #161b22;
    --border:   #30363d;
    --text:     #c9d1d9;
    --muted:    #8b949e;
    --green:    #3fb950;
    --red:      #f85149;
    --yellow:   #d29922;
    --blue:     #58a6ff;
    --purple:   #bc8cff;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 13px; }}
  header {{ background: var(--surface); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; gap: 12px; }}
  header h1 {{ font-size: 16px; font-weight: 600; color: var(--blue); }}
  header span {{ color: var(--muted); font-size: 12px; }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 20px 24px; }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }}
  .grid-3 {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 16px; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }}
  .card h2 {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-bottom: 12px; }}
  .metric {{ font-size: 28px; font-weight: 700; }}
  .metric.green {{ color: var(--green); }}
  .metric.red   {{ color: var(--red); }}
  .metric.blue  {{ color: var(--blue); }}
  .sub {{ font-size: 11px; color: var(--muted); margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; color: var(--muted); font-size: 11px; text-transform: uppercase; padding: 6px 8px; border-bottom: 1px solid var(--border); }}
  td {{ padding: 7px 8px; border-bottom: 1px solid #21262d; }}
  tr:last-child td {{ border-bottom: none; }}
  .text-green {{ color: var(--green); }}
  .text-red   {{ color: var(--red); }}
  .text-yellow {{ color: var(--yellow); }}
  .mono {{ font-family: 'SF Mono', Consolas, monospace; }}
  .small {{ font-size: 11px; }}
  canvas {{ width: 100% !important; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
  .badge-bull {{ background: rgba(63,185,80,.2); color: var(--green); }}
  .badge-bear {{ background: rgba(248,81,73,.2); color: var(--red); }}
</style>
</head>
<body>
<header>
  <h1>🐳 GEX Oracle — 鯨魚鏈上行為追蹤</h1>
  <span>數據源：Blockchair + mempool.space | 更新：{report['generated_at'][:16]} UTC | 分析週期：24h</span>
</header>

<div class="container">

  <!-- KPI 卡片 -->
  <div class="grid-3">
    <div class="card">
      <h2>鏈上信號評分</h2>
      <div class="metric {'green' if report['hourly_scores'] and report['hourly_scores'][-1]['score'] > 0 else 'red'}">
        {f"{report['hourly_scores'][-1]['score']:+.3f}" if report['hourly_scores'] else "N/A"}
      </div>
      <div class="sub">-1.0（極熊）~ +1.0（極牛）</div>
    </div>
    <div class="card">
      <h2>24h 交易所淨流量</h2>
      <div class="metric {'green' if sum(r['net_flow'] for r in report['hourly_scores']) > 0 else 'red'}">
        {sum(r['net_flow'] for r in report['hourly_scores']):+,.0f} BTC
      </div>
      <div class="sub">正值 = 淨流出（看多）/ 負值 = 淨流入（看空）</div>
    </div>
    <div class="card">
      <h2>追蹤地址 / 今日數據點</h2>
      <div class="metric blue">{len(report['top_addresses'])}</div>
      <div class="sub">Top 100 BTC 富豪地址 | {len(report['hourly_scores'])} 個小時快照</div>
    </div>
  </div>

  <!-- 圖表行 -->
  <div class="grid-2">
    <div class="card">
      <h2>24h 信號評分時序</h2>
      <canvas id="scoreChart" height="160"></canvas>
    </div>
    <div class="card">
      <h2>24h 交易所流量時序（BTC）</h2>
      <canvas id="flowChart" height="160"></canvas>
    </div>
  </div>

  <!-- 交易所流量明細 -->
  <div class="card" style="margin-bottom:16px">
    <h2>交易所流量明細（24h）</h2>
    <table>
      <thead><tr>
        <th>交易所</th><th>方向</th><th>筆數</th>
        <th>總量 (BTC)</th><th>平均 (BTC)</th><th>最大單筆 (BTC)</th>
      </tr></thead>
      <tbody>{exchange_rows}</tbody>
    </table>
  </div>

  <!-- 最大單筆交易 -->
  <div class="card" style="margin-bottom:16px">
    <h2>Top 10 大額交易（24h）</h2>
    <table>
      <thead><tr>
        <th>TXID</th><th>地址</th><th>時間</th>
        <th>方向</th><th>金額 (BTC)</th><th>對手方</th>
      </tr></thead>
      <tbody>{top_tx_rows}</tbody>
    </table>
  </div>

  <!-- 地址持倉排行 -->
  <div class="card">
    <h2>Top 20 鯨魚地址餘額快照</h2>
    <table>
      <thead><tr>
        <th>排名</th><th>地址</th><th>標籤</th>
        <th>餘額 (BTC)</th><th>Δ (BTC)</th>
      </tr></thead>
      <tbody>{addr_rows}</tbody>
    </table>
  </div>

</div>

<script>
const labels     = {hourly_labels};
const scores     = {hourly_scores};
const flows      = {hourly_flows};
const chartOpts  = {{
  responsive: true,
  plugins: {{ legend: {{ display: false }} }},
  scales: {{
    x: {{ ticks: {{ color: '#8b949e', font: {{ size: 10 }} }}, grid: {{ color: '#21262d' }} }},
    y: {{ ticks: {{ color: '#8b949e', font: {{ size: 10 }} }}, grid: {{ color: '#21262d' }} }}
  }}
}};

new Chart(document.getElementById('scoreChart'), {{
  type: 'line',
  data: {{
    labels,
    datasets: [{{
      data: scores,
      borderColor: '#58a6ff',
      backgroundColor: 'rgba(88,166,255,.08)',
      borderWidth: 1.5,
      pointRadius: 2,
      fill: true,
      tension: 0.3
    }}]
  }},
  options: {{ ...chartOpts,
    scales: {{ ...chartOpts.scales,
      y: {{ ...chartOpts.scales.y, min: -1, max: 1 }}
    }}
  }}
}});

new Chart(document.getElementById('flowChart'), {{
  type: 'bar',
  data: {{
    labels,
    datasets: [{{
      data: flows,
      backgroundColor: flows.map(v => v >= 0 ? 'rgba(63,185,80,.6)' : 'rgba(248,81,73,.6)'),
      borderWidth: 0
    }}]
  }},
  options: chartOpts
}});
</script>
</body>
</html>"""

    Path("dashboard").mkdir(exist_ok=True)
    DASH_PATH.write_text(html, encoding="utf-8")
    print(f"[dashboard] HTML 寫入 → {DASH_PATH}")


if __name__ == "__main__":
    generate_daily_report()
