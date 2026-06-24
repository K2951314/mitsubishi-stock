#!/usr/bin/env python
# -*- coding: utf-8 -*-
import sys, io, os, re, json, socket
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
"""
三菱库存手机查询服务

纯 HTTP GWT-RPC 版，不依赖浏览器。
手机和电脑同 WiFi（或通过 EasyTier），手机浏览器打开地址即可查库存。

部署方式：
  pip install flask requests
  python mobile_server.py

如需外网访问，可配合 EasyTier/Tailscale 或部署到 VPS。
"""

import configparser
import requests
from flask import Flask, request, jsonify
from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
BASE_URL = "https://mcweb.mitsubishi-materials.com/concerto-mmsc-ec"
GWT_MODULE_URL = BASE_URL + "/gwtModule/"
GWT_PERM = "3709873CCCCE1BD5AF7C55E4A0C5C0F3"

app = Flask(__name__)

# ===================================================================
# GWT-RPC 查询引擎
# ===================================================================

def load_account():
    """从环境变量或 config.ini 读取账号。环境变量优先（用于 Railway 部署）。"""
    u = os.environ.get("MMC_USERNAME", "").strip()
    p = os.environ.get("MMC_PASSWORD", "").strip()
    c = os.environ.get("MMC_COOKIE", "").strip()
    if u and p:
        return u, p, c
    cfg = configparser.RawConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    u = cfg.get("account", "username", fallback="").strip() or u
    p = cfg.get("account", "password", fallback="").strip() or p
    c = cfg.get("account", "cookie", fallback="").strip() or c
    return u, p, c


def build_search_payload(model_val, material_val):
    if material_val:
        return (
            f"7|0|13|{GWT_MODULE_URL}|"
            f"3F3B9BCCE5E51AE9BE17DA4486C9A825|"
            f"jp.co.mmc.concerto.mmsc.ec.web.gwt.client.uc.orderByItem.rpc.OrderByItemRemoteService|"
            f"executeProcess|java.lang.String/2004016611|"
            f"jp.co.mmc.concerto.core.shared.dto.ISharedDto|search|"
            f"jp.co.mmc.concerto.mmsc.ec.shared.dto.OrderByItemSharedDto/2995465772|"
            f"{model_val}|java.lang.Boolean/476441737|"
            f"java.util.ArrayList/4159755760|{material_val}|"
            f"java.util.LinkedHashMap/3008245022|"
            f"1|2|3|4|2|5|6|7|8|0|0|0|9|10|1|0|0|0|0|0|0|11|0|0|12|0|0|0|500|0|0|0|0|13|0|0|0|0|0|0|0|"
        )
    return (
        f"7|0|13|{GWT_MODULE_URL}|"
        f"3F3B9BCCE5E51AE9BE17DA4486C9A825|"
        f"jp.co.mmc.concerto.mmsc.ec.web.gwt.client.uc.orderByItem.rpc.OrderByItemRemoteService|"
        f"executeProcess|java.lang.String/2004016611|"
        f"jp.co.mmc.concerto.core.shared.dto.ISharedDto|search|"
        f"jp.co.mmc.concerto.mmsc.ec.shared.dto.OrderByItemSharedDto/2995465772|"
        f"{model_val}|java.lang.Boolean/476441737|"
        f"java.util.ArrayList/4159755760||"
        f"java.util.LinkedHashMap/3008245022|"
        f"1|2|3|4|2|5|6|7|8|0|0|0|9|10|1|0|0|0|0|0|0|11|0|0|12|0|0|0|500|0|0|0|0|13|0|0|0|0|0|0|0|"
    )


def parse_gwt_response(text):
    """解析 GWT-RPC 响应。失败时返回 error 信息。"""
    if text.startswith("//EX"):
        # 提取错误信息
        err_match = re.search(r"'([^']*)'", text[4:])
        err_msg = err_match.group(1) if err_match else "未知服务器错误"
        return {"success": False, "error": err_msg}
    if not text.startswith("//OK"):
        return {"success": False, "error": "非正常响应"}
    body = text[4:]
    bs = body.rfind('['); be = body.rfind(']')
    if bs < 0 or be < 0:
        return {"success": False, "error": "响应格式异常"}
    str_part = body[bs+1:be]
    strings = []
    for m in re.finditer(r'"([^"]*)"', str_part):
        strings.append(m.group(1))
    if not strings:
        for m in re.finditer(r"'([^']*)'", str_part):
            strings.append(m.group(1))
    return {"success": True, "strings": strings}


def extract_stock(strings):
    def clean(s):
        try: return int(float(str(s)))
        except: return 0
    def is_stock(s):
        if not s: return False
        if s in ("-1","0"): return True
        if re.match(r'^-?\d+(\.\d+)?$', s):
            try: return 0 <= float(s) < 999999
            except: return False
        return False
    def is_clean_int(s):
        """是否为整数值（5.0000→5 ✅, 704.8800→❌, 000→0 ✅）"""
        try: return float(s) == int(float(s))
        except: return False

    shanghai, japan = 0, 0

    # strings[4]=上海, strings[5]=日本（适用于大多数产品）
    if len(strings) > 5 and is_stock(strings[4]) and is_stock(strings[5]):
        shanghai = clean(strings[4])
        japan = clean(strings[5])
    # 回退：无材质搜索时 strings[15]=上海
    elif len(strings) > 15 and is_stock(strings[15]):
        shanghai = clean(strings[15])

    # strings[16] = CDC（仅当是整数值如 5.0000，排除价格 704.8800）
    if len(strings) > 16 and is_stock(strings[16]) and is_clean_int(strings[16]):
        cdc = clean(strings[16])
        if cdc > 0:
            shanghai += cdc

    return shanghai, japan


class QueryEngine:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self._ready = False

    def ensure_ready(self):
        if self._ready:
            return True
        u, p, c = load_account()
        if c:
            self.session.headers.update({"Cookie": c})
            r = self.session.get(BASE_URL + "/concerto_i10193.html", timeout=30)
            if "login" in r.url.lower():
                return False
            self._ready = True
            return True
        self.session.get(BASE_URL + "/login.jsp", timeout=30)
        r = self.session.post(BASE_URL + "/j_spring_security_check",
                              data={"j_username": u.upper(), "j_password": p},
                              timeout=30, allow_redirects=True)
        if "login" in r.url.lower():
            return False
        self.session.post(BASE_URL + "/gwtModule/rpc/common/appRemoteService",
            data=f"7|0|4|{GWT_MODULE_URL}|2662763268C21D40B75661AEA3EB2E3C|jp.co.mmc.concerto.mmsc.ec.web.gwt.client.widgets.rpc.AppRemoteService|getAppClientCacheDto|1|2|3|4|0|",
            headers={"Content-Type": "text/x-gwt-rpc; charset=UTF-8", "X-GWT-Permutation": GWT_PERM, "X-GWT-Module-Base": GWT_MODULE_URL},
            timeout=30)
        self._ready = True
        return True

    def search(self, model_val, material_val):
        payload = build_search_payload(model_val, material_val)
        try:
            r = self.session.post(BASE_URL + "/gwtModule/rpc/orderByItem/orderByItemRemoteService",
                data=payload,
                headers={"Content-Type": "text/x-gwt-rpc; charset=UTF-8", "X-GWT-Permutation": GWT_PERM, "X-GWT-Module-Base": GWT_MODULE_URL},
                timeout=30)

            # 会话过期？重新登录再试一次
            if r.status_code == 302 or r.status_code == 401:
                self._ready = False
                if self.ensure_ready():
                    r = self.session.post(
                        BASE_URL + "/gwtModule/rpc/orderByItem/orderByItemRemoteService",
                        data=payload,
                        headers={"Content-Type": "text/x-gwt-rpc; charset=UTF-8",
                                 "X-GWT-Permutation": GWT_PERM,
                                 "X-GWT-Module-Base": GWT_MODULE_URL},
                        timeout=30)

            if r.status_code != 200:
                return 0, 0, f"HTTP {r.status_code}"

            resp = parse_gwt_response(r.text)
            if not resp["success"]:
                err = resp.get("error", "")
                # ClassNotFoundException -> 服务器不认识该材质，降级为无材质搜索
                if bool(material_val) and "ClassNotFound" in err:
                    return self.search(model_val, "")
                return 0, 0, err

            shanghai, japan = extract_stock(resp["strings"])
            return shanghai, japan, None

        except requests.Timeout:
            return 0, 0, "查询超时"
        except requests.ConnectionError:
            return 0, 0, "连接失败"
        except Exception as e:
            return 0, 0, str(e)


# ===================================================================
# 批量查询
# ===================================================================

def parse_query_line(line):
    """解析单条查询，返回 (model, material, has_prefix)"""
    if "|" in line:
        parts = [p.strip() for p in line.split("|", 1)]
        return parts[0], (parts[1] if len(parts) > 1 else ""), False
    tokens = line.split()
    if not tokens: return "", "", False
    if len(tokens) == 1: return tokens[0], "", False
    start = 1 if re.match(r'^\d+\.\d+', tokens[0]) else 0
    end = len(tokens)
    for i in range(len(tokens)-1, start-1, -1):
        if "含税" in tokens[i]: end = i; break
    middle = tokens[start:end]
    has_prefix = (start > 0 or end < len(tokens))
    return (middle[0] if middle else ""), (middle[1] if len(middle) > 1 else ""), has_prefix


engine = QueryEngine()


# ===================================================================
# 路由
# ===================================================================

@app.route("/")
def index():
    return INDEX_HTML


@app.route("/api/query", methods=["POST"])
def api_query():
    try:
        if not engine.ensure_ready():
            return jsonify({"error": "登录失败，请检查 config.ini 中的账号密码或 cookie"}), 401

        data = request.get_json() or {}
        raw_lines = data.get("queries", "").strip().split("\n")

        results = []
        has_errors = False
        for line in raw_lines:
            line = line.strip()
            if not line: continue
            model, material, has_prefix = parse_query_line(line)
            if not model: continue
            shanghai, japan, error = engine.search(model, material)
            parts = []
            if error:
                has_errors = True
            if shanghai > 0: parts.append(f"上海库存{shanghai}")
            if japan > 0: parts.append(f"日本库存{japan}")
            inv = " ".join(parts) if parts else ("无货" if not error else "")
            if error:
                if has_prefix:
                    results.append(f"{line}{error}")
                else:
                    mat_tag = f" {material}" if material else ""
                    results.append(f"{model}{mat_tag} {error}")
            elif has_prefix:
                results.append(f"{line}{inv}")
            else:
                mat_tag = f" {material}" if material else ""
                results.append(f"{model}{mat_tag} {inv}")

        return jsonify({"results": results, "count": len(results)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/health")
def health():
    return jsonify({"ok": True})


# ===================================================================
# HTML 模板（手机自适应）
# ===================================================================

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<title>三菱库存查询</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100dvh;padding:12px}
h1{font-size:18px;font-weight:600;margin-bottom:12px;color:#f1f5f9;display:flex;align-items:center;gap:8px}
h1 small{font-size:12px;font-weight:400;color:#64748b}
.card{background:#1e293b;border-radius:10px;padding:14px;margin-bottom:12px}
label{display:block;font-size:12px;color:#94a3b8;margin-bottom:6px}
textarea{width:100%;min-height:100px;padding:10px;border-radius:8px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:15px;font-family:monospace;resize:vertical}
textarea:focus{outline:none;border-color:#3b82f6}
.hint{font-size:11px;color:#64748b;margin-top:4px;line-height:1.5}
.btn{display:block;width:100%;padding:12px;border:none;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;margin-top:10px;transition:all .2s;-webkit-tap-highlight-color:transparent}
.btn-primary{background:#3b82f6;color:#fff}
.btn-primary:active{background:#2563eb}
.btn-primary:disabled{background:#334155;color:#64748b;cursor:not-allowed}
.btn-copy{background:#334155;color:#e2e8f0;font-size:13px;padding:10px;margin-top:10px}
.btn-copy:active{background:#475569}
#status{text-align:center;padding:8px;font-size:13px;display:none}
#status.loading{display:block;color:#60a5fa}
#status.error{display:block;color:#f87171;font-size:12px;padding:10px;background:#1e293b;border-radius:8px}
#results{background:#0f172a;border-radius:8px;padding:10px;font-family:monospace;font-size:13px;line-height:1.8;white-space:pre-wrap;word-break:break-all;border:1px solid #334155;min-height:40px;display:none;margin-top:4px}
#results.visible{display:block}
.r{color:#86efac}
.re{color:#fca5a5}
.rh{font-size:12px;color:#94a3b8;margin-bottom:6px;padding-bottom:6px;border-bottom:1px solid #334155}
.loader{display:inline-block;width:14px;height:14px;border:2px solid #334155;border-top-color:#60a5fa;border-radius:50%;animation:s .6s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes s{to{transform:rotate(360deg)}}
</style>
</head>
<body>

<h1>三菱库存查询 <small>手机版</small></h1>

<div class="card">
  <label>输入查询内容（每行一条）</label>
  <textarea id="q" placeholder="CNMG120408-MA MP7135&#10;DVAS0150X02S040 DP1120&#10;01.01.24680 SEEN1203AFTN1 NX2525 含税27.6&#10;ASXC400-050A03R"></textarea>
  <div class="hint">支持空格分隔、管道符 |、含税价自动识别</div>
  <button class="btn btn-primary" id="btn" onclick="q()">查询库存</button>
</div>

<div id="status"></div>

<div class="card" id="rc" style="display:none">
  <div class="rh" id="rh"></div>
  <div id="results"></div>
  <button class="btn btn-copy" onclick="cp()" id="cpb">复制结果</button>
</div>

<script>
let last = '';

function e(i){return document.getElementById(i)}

async function q(){
  const v = e('q').value.trim();
  if(!v){alert('请输入查询内容');return}
  const btn = e('btn'), st = e('status'), rc = e('rc');
  btn.disabled = true; btn.textContent = '查询中...';
  st.className = 'loading';
  st.innerHTML = '<span class="loader"></span>正在查询...';
  rc.style.display = 'none';
  try{
    const c = new AbortController();
    const to = setTimeout(() => c.abort(), 60000);
    const r = await fetch('/api/query', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({queries:v}), signal:c.signal
    });
    clearTimeout(to);
    const d = await r.json();
    st.className = ''; st.textContent = '';
    if(d.results && d.results.length){
      last = d.results.join('\n');
      e('rh').textContent = '共 ' + d.count + ' 条结果';
      e('results').innerHTML = d.results.map(x =>
        '<div class="r">' + x.replace(/</g,'&lt;').replace(/ /g,' ').replace(/（/g,'<br>（') + '</div>'
      ).join('');
      e('results').className = 'visible';
      rc.style.display = 'block';
    } else {
      st.className = 'error';
      st.textContent = d.error || '无结果';
    }
  } catch(e){
    st.className = 'error';
    st.textContent = e.name==='AbortError' ? '查询超时（超过60秒）' : '请求失败: ' + e.message;
  } finally {
    btn.disabled = false; btn.textContent = '查询库存';
  }
}

async function cp(){
  if(!last) return;
  const btn = e('cpb');
  try{
    await navigator.clipboard.writeText(last);
    btn.textContent = '已复制！';
    setTimeout(() => btn.textContent = '复制结果', 1500);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = last; ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px';
    document.body.appendChild(ta); ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    btn.textContent = '已复制！';
    setTimeout(() => btn.textContent = '复制结果', 1500);
  }
}

e('q').addEventListener('keydown', e => {
  if(e.key === 'Enter' && (e.ctrlKey || e.metaKey)){ e.preventDefault(); q(); }
});
</script>
</body>
</html>"""

# ===================================================================
# 启动
# ===================================================================

import os

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"

    print()
    print("=" * 50)
    print("  三菱库存手机查询服务")
    print(f"  本机访问：  http://127.0.0.1:{port}")
    print(f"  手机访问：  http://{ip}:{port}")
    print("=" * 50)
    print("  需在同一 WiFi 下")
    print("  如需外网访问，请配合 EasyTier/Tailscale 或 Railway")
    print("=" * 50)
    print()

    app.run(host="0.0.0.0", port=port, debug=False)
