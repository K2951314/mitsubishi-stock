import sys, io, os, re, socket

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
"""
三菱库存手机查询服务

纯 HTTP GWT-RPC 版，不依赖浏览器。
手机和电脑同 WiFi（或通过 EasyTier），手机浏览器打开地址即可查库存。

部署方式：
  pip install flask requests
  python mobile_server.py

如需外网访问，可配合 EasyTier/Tailscale 或部署到 Railway/VPS。
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
GWT_STRONG_NAME = "3F3B9BCCE5E51AE9BE17DA4486C9A825"
GWT_APP_SERVICE = "2662763268C21D40B75661AEA3EB2E3C"
RPC_HEADERS = {
    "Content-Type": "text/x-gwt-rpc; charset=UTF-8",
    "X-GWT-Permutation": GWT_PERM,
    "X-GWT-Module-Base": GWT_MODULE_URL,
}

app = Flask(__name__)

# ── GWT-RPC 查询引擎 ──────────────────────────────────────────


def load_account():
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


def _gwt_payload(model_val, material_val):
    """构造 GWT-RPC search 请求体。"""

    def hdr(pc):
        return (
            f"7|0|13|{GWT_MODULE_URL}|"
            f"{GWT_STRONG_NAME}|"
            f"jp.co.mmc.concerto.mmsc.ec.web.gwt.client.uc.orderByItem.rpc.OrderByItemRemoteService|"
            f"executeProcess|java.lang.String/2004016611|"
            f"jp.co.mmc.concerto.core.shared.dto.ISharedDto|search|"
            f"jp.co.mmc.concerto.mmsc.ec.shared.dto.OrderByItemSharedDto/2995465772|"
            f"{model_val}|java.lang.Boolean/476441737|"
            f"java.util.ArrayList/4159755760|{pc}|"
            f"java.util.LinkedHashMap/3008245022|"
            f"1|2|3|4|2|5|6|7|8|0|0|0|9|10|1|0|0|0|0|0|0|11|0|0|12|0|0|0|500|0|0|0|0|13|0|0|0|0|0|0|0|"
        )

    return hdr(material_val) if material_val else hdr("")


def _parse_gwt(text):
    """解析 GWT-RPC 响应 → {success, strings[], error}"""
    if text.startswith("//EX"):
        m = re.search(r"'([^']*)'", text[4:])
        return {"success": False, "error": m.group(1) if m else "未知服务器错误"}
    if not text.startswith("//OK"):
        return {"success": False, "error": "非正常响应"}

    body = text[4:]
    bs, be = body.rfind("["), body.rfind("]")
    if bs < 0 or be < 0:
        return {"success": False, "error": "响应格式异常"}

    strings = []
    for q in ('"', "'"):
        strings = [m.group(1) for m in re.finditer(rf"{q}([^{q}]*){q}", body[bs + 1 : be])]
        if strings:
            break

    return {"success": True, "strings": strings}


def _extract_stock(strings):
    """从 GWT 字符串表提取 (shanghai, japan)。

    库存值在字符串表中是头两个数值（不含 DTO 类型标记的第4位起）。
    中间可能穿插 Boolean/Long 等类型标记，不能用固定偏移。
    """

    def clean(s):
        try:
            return int(float(str(s)))
        except (ValueError, TypeError):
            return 0

    def is_stock(s):
        return bool(s) and bool(re.match(r"^-?\d+(\.\d+)?$", s)) and 0 <= float(s) < 999999

    # 从 index 4 开始扫描，跳过类型标记，取前两个数值
    vals = [clean(s) for s in strings[4:] if is_stock(s)]
    return (vals[0], vals[1]) if len(vals) >= 2 else (vals[0] if vals else 0, 0)


class QueryEngine:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        self._ready = False

    def _login(self, username, password):
        self.session.get(BASE_URL + "/login.jsp", timeout=30)
        r = self.session.post(
            BASE_URL + "/j_spring_security_check",
            data={"j_username": username.upper(), "j_password": password},
            timeout=30,
            allow_redirects=True,
        )
        if "login" in r.url.lower():
            return False
        self.session.post(
            BASE_URL + "/gwtModule/rpc/common/appRemoteService",
            data=f"7|0|4|{GWT_MODULE_URL}|{GWT_APP_SERVICE}|jp.co.mmc.concerto.mmsc.ec.web.gwt.client.widgets.rpc.AppRemoteService|getAppClientCacheDto|1|2|3|4|0|",
            headers=RPC_HEADERS,
            timeout=30,
        )
        return True

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
        self._ready = self._login(u, p)
        return self._ready

    def search(self, model_val, material_val):
        payload = _gwt_payload(model_val, material_val)
        try:
            r = self.session.post(
                BASE_URL + "/gwtModule/rpc/orderByItem/orderByItemRemoteService",
                data=payload,
                headers=RPC_HEADERS,
                timeout=30,
            )

            if r.status_code in (302, 401):
                self._ready = False
                if self.ensure_ready():
                    r = self.session.post(
                        BASE_URL + "/gwtModule/rpc/orderByItem/orderByItemRemoteService",
                        data=payload,
                        headers=RPC_HEADERS,
                        timeout=30,
                    )

            if r.status_code != 200:
                return 0, 0, f"HTTP {r.status_code}"

            resp = _parse_gwt(r.text)
            if not resp["success"]:
                err = resp.get("error", "")
                if bool(material_val) and "ClassNotFound" in err:
                    return self.search(model_val, "")
                return 0, 0, err

            stock = _extract_stock(resp["strings"])
            return *stock, None

        except requests.Timeout:
            return 0, 0, "查询超时"
        except requests.ConnectionError:
            return 0, 0, "连接失败"
        except Exception as e:
            return 0, 0, str(e)


engine = QueryEngine()

# ── 查询行解析 ──────────────────────────────────────────────


def parse_query_line(line):
    if "|" in line:
        parts = [p.strip() for p in line.split("|", 1)]
        return parts[0], (parts[1] if len(parts) > 1 else ""), False

    tokens = line.split()
    if not tokens:
        return "", "", False
    if len(tokens) == 1:
        return tokens[0], "", False

    start = int(bool(re.match(r"^\d+\.\d+", tokens[0])))
    end = len(tokens)
    for i in range(len(tokens) - 1, start - 1, -1):
        if "含税" in tokens[i]:
            end = i
            break

    middle = tokens[start:end]
    has_prefix = start > 0 or end < len(tokens)
    return (middle[0] if middle else ""), (middle[1] if len(middle) > 1 else ""), has_prefix


# ── Flask 路由 ────────────────────────────────────────────────


@app.route("/")
def index():
    return HTML


@app.route("/api/query", methods=["POST"])
def api_query():
    try:
        if not engine.ensure_ready():
            return jsonify({"error": "登录失败，请检查账号密码"}), 401

        data = request.get_json() or {}
        lines = data.get("queries", "").strip().split("\n")

        results = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            model, material, prefixed = parse_query_line(line)
            if not model:
                continue

            shanghai, japan, error = engine.search(model, material)

            parts = []
            if shanghai > 0:
                parts.append(f"上海库存{shanghai}")
            if japan > 0:
                parts.append(f"日本库存{japan}")

            inv = " ".join(parts) if parts else ("无货" if not error else "")

            if error:
                label = line if prefixed else f"{model}{' ' + material if material else ''}"
                results.append(f"{label} {error}")
            elif prefixed:
                results.append(f"{line}{inv}")
            else:
                tag = f" {material}" if material else ""
                results.append(f"{model}{tag} {inv}")

        return jsonify({"results": results, "count": len(results)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/health")
def health():
    return jsonify({"ok": True})


# ── HTML 模板 ─────────────────────────────────────────────────


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<title>三菱库存查询</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100dvh;padding:12px}
h1{font-size:18px;font-weight:600;margin-bottom:12px;color:#f1f5f9}
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
.btn-copy{background:#334155;color:#e2e8f0;font-size:13px;padding:10px}
#status{text-align:center;padding:8px;font-size:13px;display:none}
#status.loading{display:block;color:#60a5fa}
#status.error{display:block;color:#f87171;font-size:12px;padding:10px;background:#1e293b;border-radius:8px}
#results{background:#0f172a;border-radius:8px;padding:10px;font-family:monospace;font-size:13px;line-height:1.8;white-space:pre-wrap;word-break:break-all;border:1px solid #334155;min-height:40px;display:none}
#results.show{display:block}
.rr{color:#86efac}
.rh{font-size:12px;color:#94a3b8;margin-bottom:6px;padding-bottom:6px;border-bottom:1px solid #334155}
.spin{display:inline-block;width:14px;height:14px;border:2px solid #334155;border-top-color:#60a5fa;border-radius:50%;animation:s .6s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes s{to{transform:rotate(360deg)}}
</style>
</head>
<body>

<h1>三菱库存查询 <small>手机版</small></h1>

<div class="card">
  <label>输入查询（每行一条）</label>
  <textarea id="q" placeholder="CNMG120408-MA MP7135&#10;DVAS0150X02S040 DP1120&#10;01.01.24680 SEEN1203AFTN1 NX2525 含税27.6"></textarea>
  <div class="hint">管道符 |、含税价自动识别</div>
  <button class="btn btn-primary" id="b" onclick="go()">查询库存</button>
</div>

<div id="s"></div>

<div class="card" id="rc" style="display:none">
  <div class="rh" id="rh"></div>
  <div id="r"></div>
  <button class="btn btn-copy" onclick="cp()" id="cp">复制结果</button>
</div>

<script>
let last='';

async function go(){
  const v = document.getElementById('q').value.trim();
  if(!v) return;
  const b = document.getElementById('b'), s = document.getElementById('s');
  b.disabled = true; b.textContent = '查询中...';
  s.className = 'loading';
  s.innerHTML = '<span class="spin"></span>正在查询...';
  document.getElementById('rc').style.display = 'none';
  try{
    const ac = new AbortController();
    const to = setTimeout(() => { ac.abort(); }, 60000);
    const r = await fetch('/api/query', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({queries:v}), signal:ac.signal
    });
    clearTimeout(to);
    const d = await r.json();
    s.className = ''; s.textContent = '';
    if(d.results && d.results.length){
      last = d.results.join('\n');
      document.getElementById('rh').textContent = '共 ' + d.count + ' 条';
      document.getElementById('r').innerHTML = d.results.map(x =>
        '<div class="rr">' + x.replace(/</g,'&lt;') + '</div>'
      ).join('');
      document.getElementById('r').className = 'show';
      document.getElementById('rc').style.display = 'block';
    } else {
      s.className = 'error'; s.textContent = d.error || '无结果';
    }
  } catch(e){
    s.className = 'error';
    s.textContent = e.name==='AbortError' ? '超时' : '失败: ' + e.message;
  } finally {
    b.disabled = false; b.textContent = '查询库存';
  }
}

async function cp(){
  if(!last) return;
  const b = document.getElementById('cp');
  try{
    await navigator.clipboard.writeText(last);
    b.textContent = '已复制！';
    setTimeout(() => b.textContent = '复制结果', 1500);
  } catch {
    const t = document.createElement('textarea');
    t.value = last; t.style.cssText = 'position:fixed;left:-9999px;top:-9999px';
    document.body.appendChild(t); t.select();
    document.execCommand('copy');
    document.body.removeChild(t);
    b.textContent = '已复制！';
    setTimeout(() => b.textContent = '复制结果', 1500);
  }
}

document.getElementById('q').addEventListener('keydown', e => {
  if((e.ctrlKey||e.metaKey) && e.key==='Enter'){ e.preventDefault(); go(); }
});
</script>
</body>
</html>"""

# ── 启动 ─────────────────────────────────────────────────────


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"

    print()
    print(f"  三菱库存手机查询服务  本机 http://127.0.0.1:{port}")
    print(f"                       手机 http://{ip}:{port}")
    print(f"  同局域网 / EasyTier / Railway 均可访问")
    print()

    app.run(host="0.0.0.0", port=port, debug=False)
