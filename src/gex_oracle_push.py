#!/usr/bin/env python3
import requests, base64, os, json, hashlib

TOKEN = os.environ.get("GH_TOKEN", "")
REPO = os.environ.get("GH_REPO", "Z3X1/SideProject_WhaleTracker")
HEADERS = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github.v3+json"}
PW_HASH = "3ac22acab4270f1d078564ef14475d2ad239398b61104e839cb73b7c1f65eb63"

def get_sha(path):
    r = requests.get(
        f"https://api.github.com/repos/{REPO}/contents/{path}",
        headers=HEADERS
    )
    return r.json().get("sha") if r.status_code == 200 else None

def push_bytes(gh_path, content_bytes, message):
    sha = get_sha(gh_path)
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode()
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(
        f"https://api.github.com/repos/{REPO}/contents/{gh_path}",
        headers=HEADERS, json=payload
    )
    ok = r.status_code in [200, 201]
    print(f"{'OK' if ok else 'FAIL'} {gh_path}: {r.status_code}")
    return ok

def wrap_password(html_str):
    content_json = json.dumps(html_str)
    pw_hash = PW_HASH
    page = (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "<meta charset=\"UTF-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1.0\">\n"
        "<meta name=\"google\" content=\"notranslate\">\n"
        "<title>GEX Oracle</title>\n"
        "<style>\n"
        "*{box-sizing:border-box;margin:0;padding:0}\n"
        "body{background:#0a0e17;color:#e2e8f0;font-family:monospace;"
        "display:flex;justify-content:center;align-items:center;height:100vh}\n"
        ".box{text-align:center;padding:40px;border:1px solid #1e293b;"
        "border-radius:12px;background:#111827;width:300px}\n"
        "h2{color:#3b82f6;margin-bottom:24px;letter-spacing:3px;font-size:18px}\n"
        "input{background:#0a0e17;border:1px solid #3b82f6;color:#e2e8f0;"
        "padding:12px 20px;border-radius:6px;font-size:16px;width:100%;"
        "text-align:center;outline:none}\n"
        "button{background:#3b82f6;color:white;border:none;padding:12px;"
        "border-radius:6px;font-size:14px;cursor:pointer;margin-top:12px;"
        "width:100%}\n"
        ".err{color:#ef4444;margin-top:10px;font-size:12px;height:16px}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<div class=\"box\">\n"
        "<h2>GEX ORACLE</h2>\n"
        "<input type=\"password\" id=\"pw\" placeholder=\"Password\""
        " onkeydown=\"if(event.key==='Enter')check()\">\n"
        "<button onclick=\"check()\">ENTER</button>\n"
        "<div class=\"err\" id=\"err\"></div>\n"
        "</div>\n"
        "<script>\n"
        "const HASH=\"" + pw_hash + "\";\n"
        "const DATA=" + content_json + ";\n"
        "async function sha256(s){\n"
        "  const b=new TextEncoder().encode(s);\n"
        "  const h=await crypto.subtle.digest('SHA-256',b);\n"
        "  return Array.from(new Uint8Array(h)).map(x=>x.toString(16).padStart(2,'0')).join('');\n"
        "}\n"
        "async function check(){\n"
        "  const pw=document.getElementById('pw').value;\n"
        "  const h=await sha256(pw);\n"
        "  if(h===HASH){\n"
        "    document.open('text/html','replace');\n"
        "    document.write(DATA);\n"
        "    document.close();\n"
        "  }else{\n"
        "    document.getElementById('err').textContent='Wrong password';\n"
        "    document.getElementById('pw').value='';\n"
        "  }\n"
        "}\n"
        "document.getElementById('pw').focus();\n"
        "</script>\n"
        "</body>\n"
        "</html>"
    )
    return page

# Main
local = "docs/oracle/index.html"
if os.path.exists(local):
    with open(local, "r", encoding="utf-8") as f:
        html = f.read()
    protected = wrap_password(html)
    push_bytes(
        "docs/oracle/index.html",
        protected.encode("utf-8"),
        "auto: GEX Oracle dashboard"
    )
else:
    print(f"NOT FOUND: {local}")

if os.path.exists("data/oracle_market_data.json"):
    with open("data/oracle_market_data.json", "rb") as f:
        push_bytes("data/oracle_market_data.json", f.read(), "auto: market data")
