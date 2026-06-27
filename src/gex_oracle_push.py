"""
用GitHub API直接推送所有輸出檔案，不依賴git push
"""
import requests, base64, os, sys

TOKEN = os.environ.get("GH_TOKEN", "")
REPO = os.environ.get("GH_REPO", "Z3X1/SideProject_WhaleTracker")
HEADERS = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github.v3+json"}

def push(local_path, gh_path, message):
    if not os.path.exists(local_path):
        print(f"skip {local_path} (not found)")
        return
    with open(local_path, "rb") as f:
        content = base64.b64encode(f.read()).decode()
    r = requests.get(f"https://api.github.com/repos/{REPO}/contents/{gh_path}", headers=HEADERS)
    sha = r.json().get("sha") if r.status_code == 200 else None
    payload = {"message": message, "content": content}
    if sha:
        payload["sha"] = sha
    r2 = requests.put(
        f"https://api.github.com/repos/{REPO}/contents/{gh_path}",
        headers=HEADERS, json=payload
    )
    ok = r2.status_code in [200, 201]
    print(f"{'✅' if ok else '❌'} {gh_path}: {r2.status_code}")
    return ok

# 包裝密碼保護層
if os.path.exists("docs/oracle/index.html"):
    with open("docs/oracle/index.html", "r") as f:
        inner_html = f.read()
    # base64編碼內容
    import base64 as b64
    encoded = b64.b64encode(inner_html.encode()).decode()
    pw_hash = "3ac22acab4270f1d078564ef14475d2ad239398b61104e839cb73b7c1f65eb63"
    protected = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>GEX Oracle</title>
<style>
body{{background:#0a0e17;color:#e2e8f0;font-family:monospace;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}}
.box{{text-align:center;padding:40px;border:1px solid #1e293b;border-radius:12px;background:#111827}}
h2{{color:#3b82f6;margin-bottom:20px;letter-spacing:3px}}
input{{background:#0a0e17;border:1px solid #3b82f6;color:#e2e8f0;padding:10px 20px;border-radius:6px;font-size:16px;width:200px;text-align:center}}
button{{background:#3b82f6;color:white;border:none;padding:10px 30px;border-radius:6px;font-size:14px;cursor:pointer;margin-top:12px;display:block;width:100%}}
.err{{color:#ef4444;margin-top:10px;font-size:12px}}
</style>
</head>
<body>
<div class="box">
<h2>⚡ GEX ORACLE</h2>
<input type="password" id="pw" placeholder="Password" onkeydown="if(event.key==='Enter')check()">
<button onclick="check()">Enter</button>
<div class="err" id="err"></div>
</div>
<script>
const HASH="{pw_hash}";
const DATA="{encoded}";
async function sha256(s){{
  const b=new TextEncoder().encode(s);
  const h=await crypto.subtle.digest('SHA-256',b);
  return Array.from(new Uint8Array(h)).map(x=>x.toString(16).padStart(2,'0')).join('');
}}
async function check(){{
  const pw=document.getElementById('pw').value;
  const h=await sha256(pw);
  if(h===HASH){{
    document.open();
    const bytes = Uint8Array.from(atob(DATA), c => c.charCodeAt(0));
    const decoded = new TextDecoder('utf-8').decode(bytes);
    document.write(decoded);
    document.close();
  }}else{{
    document.getElementById('err').textContent='Wrong password';
    document.getElementById('pw').value='';
  }}
}}
</script>
</body>
</html>"""
    with open("docs/oracle/index.html", "w") as f:
        f.write(protected)

push("docs/oracle/index.html", "docs/oracle/index.html", "auto: GEX Oracle dashboard (protected)")
push("data/oracle_market_data.json", "data/oracle_market_data.json", "auto: Oracle market data")
