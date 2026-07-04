#!/usr/bin/env python3
import requests, base64, os, json, hashlib

TOKEN = os.environ.get("GH_TOKEN", "")
REPO = os.environ.get("GH_REPO", "Z3X1/SideProject_WhaleTracker")
HEADERS = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github.v3+json"}
# 密碼 hash 從環境變數注入（不硬碼在公開 repo）
# GitHub Secret: ORACLE_PW_HASH = sha256(password)
# 本地測試: export ORACLE_PW_HASH=$(echo -n "yourpassword" | sha256sum | cut -d" " -f1)
PW_HASH = os.environ.get(
    "ORACLE_PW_HASH",
    "3ac22acab4270f1d078564ef14475d2ad239398b61104e839cb73b7c1f65eb63"  # fallback（建議移除並設 Secret）
)

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
    """
    密碼保護頁。
    核心問題：html_str 含 </script> 標籤，直接嵌入 JS 字串會截斷外層 <script>。
    修法：把 HTML 用 base64 編碼存入 JS，decode 後用 Blob URL + iframe 顯示。
    這樣 HTML 內容完全不接觸 JS 解析器，任何特殊字元都安全。
    """
    import base64 as _b64
    html_b64 = _b64.b64encode(html_str.encode('utf-8')).decode('ascii')
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
        "border-radius:6px;font-size:14px;cursor:pointer;margin-top:12px;width:100%}\n"
        ".err{color:#ef4444;margin-top:10px;font-size:12px;height:16px}\n"
        "#frame{display:none;position:fixed;top:0;left:0;width:100%;height:100%;"
        "border:none;background:#0a0e17}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<div class=\"box\" id=\"box\">\n"
        "<h2>GEX ORACLE</h2>\n"
        "<input type=\"password\" id=\"pw\" placeholder=\"Password\""
        " onkeydown=\"if(event.key==='Enter')check()\">\n"
        "<button onclick=\"check()\">ENTER</button>\n"
        "<div class=\"err\" id=\"err\"></div>\n"
        "</div>\n"
        "<iframe id=\"frame\"></iframe>\n"
        "<script>\n"
        # HTML 以 base64 存儲：完全繞過 JS 解析器，</script> 等特殊字元不影響
        "const HASH=\"" + pw_hash + "\";\n"
        "const B64=\"" + html_b64 + "\";\n"
        "function b64decode(s){\n"
        "  const bin=atob(s);\n"
        "  const bytes=new Uint8Array(bin.length);\n"
        "  for(let i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);\n"
        "  return new TextDecoder('utf-8').decode(bytes);\n"
        "}\n"
        "async function sha256(s){\n"
        "  const b=new TextEncoder().encode(s);\n"
        "  const h=await crypto.subtle.digest('SHA-256',b);\n"
        "  return Array.from(new Uint8Array(h)).map(x=>x.toString(16).padStart(2,'0')).join('');\n"
        "}\n"
        "async function check(){\n"
        "  const pw=document.getElementById('pw').value;\n"
        "  const h=await sha256(pw);\n"
        "  if(h===HASH){\n"
        # 用 Blob URL 載入 iframe，完全隔離：不用 document.write，不用 srcdoc（有長度限制）
        "    const html=b64decode(B64);\n"
        "    const blob=new Blob([html],{type:'text/html'});\n"
        "    const url=URL.createObjectURL(blob);\n"
        "    const frame=document.getElementById('frame');\n"
        "    frame.src=url;\n"
        "    frame.style.display='block';\n"
        "    document.getElementById('box').style.display='none';\n"
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
    # 防護：若 html 已包含 wrap（例如手動推的），不要雙重包裝
    if "createObjectURL" in html and "const B64=" in html:
        protected = html
        print("HTML already wrapped, skipping wrap_password")
    else:
        protected = wrap_password(html)
        # 驗證 wrap 正確
        if "createObjectURL" not in protected or "const B64=" not in protected:
            print("ERROR: wrap_password failed, pushing raw HTML as fallback")
            protected = html
        else:
            print(f"wrap_password OK: {len(protected):,} bytes (inner: {len(html):,})")
    push_bytes(
        "docs/oracle/index.html",
        protected.encode("utf-8"),
        "auto: GEX Oracle dashboard"
    )
else:
    print(f"NOT FOUND: {local}")

for fname in ["data/oracle_market_data.json", "data/snapshot_counter.json", "data/settlement_log.json", "data/skew_history.json"]:
    if os.path.exists(fname):
        with open(fname, "rb") as f:
            push_bytes(fname, f.read(), f"auto: {fname.split('/')[-1]}")
