from flask import Flask, request, jsonify, send_file, Response
import json, hmac, hashlib, os, time, secrets, subprocess, threading, collections
import ssl as ssl_lib
import requests as req
from pathlib import Path
from datetime import datetime
from functools import wraps
from werkzeug.serving import make_server

app = Flask(__name__)

# ── paths ─────────────────────────────────────────────────────────────────────
BASE                  = Path(__file__).parent
USERS_FILE            = BASE / "users.json"
VERSION_FILE          = BASE / "version.txt"
CODE_FILE             = BASE / "main.pyw"
LAUNCHER_FILE         = BASE / "launcher.py"
LAUNCHER_VERSION_FILE = BASE / "launcher_version.txt"
RATTLETRAP_FILE       = BASE / "rattletrap.exe"
LOGO_FILE             = BASE / "logo.ico"
APP_EXE_FILE          = BASE / "BallchasingUploader.exe"
SECRET_FILE           = BASE / "secret.key"
CERT_FILE             = BASE / "server.crt"
KEY_FILE              = BASE / "server.key"
ADMIN_PW_FILE         = BASE / "admin_pw.hash"

# ── config ────────────────────────────────────────────────────────────────────
ADMIN_GUID = "c18d4cf2-b171-45d8-9e79-8fb33c37ca61"

# ── session store (in-memory, cleared on restart) ─────────────────────────────
_sessions: dict = {}  # token → expiry timestamp

# ── rate limiting ─────────────────────────────────────────────────────────────
_rl_store: dict  = {}  # (ip, endpoint) → deque of timestamps
_rl_lock         = threading.Lock()

RATE_LIMITS = {
    "register":    (20,  3600),  # 5 per hour       — you only ever register once
    "verify":      (30,  60),    # 30 per minute     — normal app launches
    "code":        (10,  60),    # 10 per minute     — code downloads
    "launcher":    (5,   60),    # 5  per minute     — launcher downloads
    "admin_login": (5,   900),   # 5  per 15 minutes — brute-force protection
}

def check_rate_limit(ip: str, endpoint: str) -> bool:
    """Returns True if the request is allowed, False if the IP has exceeded the limit."""
    if endpoint not in RATE_LIMITS:
        return True
    limit, window = RATE_LIMITS[endpoint]
    key = (ip, endpoint)
    now = time.time()
    with _rl_lock:
        if key not in _rl_store:
            _rl_store[key] = collections.deque()
        dq = _rl_store[key]
        while dq and dq[0] < now - window:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True

# ── log buffer ────────────────────────────────────────────────────────────────
_logs: collections.deque = collections.deque(maxlen=500)

def log(event: str, detail: str, ip: str = "", user: str = "", level: str = "info"):
    _logs.appendleft({
        "time":   datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "event":  event,
        "detail": detail,
        "ip":     ip,
        "user":   user,
        "level":  level,
    })

def new_session() -> str:
    token = secrets.token_hex(24)
    _sessions[token] = time.time() + 3600
    # prune expired sessions
    for t in list(_sessions):
        if _sessions[t] < time.time():
            del _sessions[t]
    return token

def is_valid_session(token: str) -> bool:
    exp = _sessions.get(token)
    return bool(exp and time.time() < exp)

# ── helpers ───────────────────────────────────────────────────────────────────
def get_secret() -> bytes:
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(os.urandom(32).hex())
    return SECRET_FILE.read_text().strip().encode()

def hash_id(client_id: str) -> str:
    """One-way hash of the client ID — this is the key stored in users.json."""
    return hashlib.sha256(client_id.encode()).hexdigest()

def make_token(client_id: str) -> str:
    return hmac.new(get_secret(), client_id.encode(), hashlib.sha256).hexdigest()  # type: ignore

def verify_token(client_id: str, token: str) -> bool:
    return hmac.compare_digest(make_token(client_id), token)

def load_users() -> dict:
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text())
    return {}

def save_users(users: dict):
    USERS_FILE.write_text(json.dumps(users, indent=2))

def get_version() -> str:
    return VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else "1.0.0"

def get_launcher_version() -> str:
    return LAUNCHER_VERSION_FILE.read_text().strip() if LAUNCHER_VERSION_FILE.exists() else ""


def get_admin_pw_hash() -> str:
    return ADMIN_PW_FILE.read_text().strip() if ADMIN_PW_FILE.exists() else ""

def check_admin_proof(guid: str, proof: str) -> bool:
    pw_hash = get_admin_pw_hash()
    if not pw_hash:
        return False
    expected = hmac.new(guid.encode(), pw_hash.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, proof)

def ensure_ssl():
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    print("Generating self-signed SSL certificate...")
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
        "-days", "3650", "-nodes", "-subj", "/CN=ballchasingautouploader.com"
    ], check=True, capture_output=True)
    print("Certificate generated.")

# ── decorators ────────────────────────────────────────────────────────────────
def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.content_type and "multipart" in request.content_type:
            guid = request.form.get("admin_guid", "")
        else:
            guid = (request.get_json(silent=True) or {}).get("admin_guid", "")
        if guid != ADMIN_GUID:
            return jsonify({"error": "unauthorized"}), 403
        return f(*args, **kwargs)
    return decorated

def require_session(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_valid_session(kwargs.get("token", "")):
            return jsonify({"error": "session expired"}), 401
        return f(*args, **kwargs)
    return decorated

# ── dashboard HTML ────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0f0f0f; color:#e0e0e0; font-family:'Segoe UI',sans-serif; padding:32px 24px; }
h2 { font-size:16px; font-weight:600; margin-bottom:14px; color:#fff; }
.stats { display:flex; gap:12px; margin-bottom:24px; }
.stat { background:#1a1a1a; border:1px solid #2a2a2a; border-radius:8px; padding:14px 20px; flex:1; }
.stat-value { font-size:26px; font-weight:700; }
.stat-label { font-size:10px; color:#555; margin-top:3px; text-transform:uppercase; letter-spacing:.5px; }
.tabs { display:flex; gap:4px; margin-bottom:16px; }
.tab { padding:7px 18px; border-radius:6px; border:none; cursor:pointer; font-size:12px; font-weight:600; background:#1a1a1a; color:#555; transition:all .15s; }
.tab.active { background:#1a3050; color:#42a5f5; }
.tab:hover:not(.active) { color:#aaa; }
.panel { display:none; }
.panel.active { display:block; }
.section { background:#141414; border:1px solid #1e1e1e; border-radius:8px; padding:20px; margin-bottom:20px; overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th { text-align:left; padding:8px 10px; color:#444; font-weight:500; border-bottom:1px solid #1e1e1e; font-size:10px; text-transform:uppercase; letter-spacing:.5px; white-space:nowrap; }
td { padding:9px 10px; border-bottom:1px solid #181818; vertical-align:middle; }
tr:hover td { background:#161616; }
.badge { display:inline-block; padding:2px 7px; border-radius:3px; font-size:10px; font-weight:700; text-transform:uppercase; }
.free { background:#0e2a0e; color:#4caf50; }
.paid { background:#0e1e2e; color:#42a5f5; }
.revoked { background:#2a0e0e; color:#ef5350; }
.ev-verify   { background:#0e1e2e; color:#42a5f5; }
.ev-register { background:#0e2a0e; color:#4caf50; }
.ev-code     { background:#1a1a2e; color:#7c7cff; }
.ev-admin    { background:#2a2a0e; color:#ffcc42; }
.ev-deploy   { background:#0e2a2a; color:#42f5f5; }
.ev-warn     { background:#2a1a0e; color:#ff9142; }
.guid { font-family:monospace; font-size:10px; color:#555; cursor:default; }
.btn { padding:4px 10px; border-radius:4px; border:none; cursor:pointer; font-size:11px; font-weight:600; transition:opacity .15s; }
.btn:hover { opacity:.8; }
.b-blue { background:#1a3050; color:#42a5f5; }
.b-gray { background:#1e1e1e; color:#666; }
select, input[type=text] { background:#1a1a1a; border:1px solid #2a2a2a; color:#e0e0e0; padding:3px 7px; border-radius:4px; font-size:11px; }
.deploy-row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.msg { margin-top:8px; font-size:12px; color:#666; min-height:18px; }
.msg.ok { color:#4caf50; }
.msg.err { color:#ef5350; }
.expired { padding:40px; color:#ef5350; font-size:14px; }
.log-time { font-family:monospace; font-size:11px; color:#444; white-space:nowrap; }
.log-detail { font-size:12px; color:#888; }
.log-user { font-size:11px; color:#555; }
.log-ip { font-family:monospace; font-size:11px; color:#444; }
</style>
</head>
<body>
<div class="stats">
  <div class="stat"><div class="stat-value" id="s-total">—</div><div class="stat-label">Total Users</div></div>
  <div class="stat"><div class="stat-value" id="s-active">—</div><div class="stat-label">Active</div></div>
  <div class="stat"><div class="stat-value" id="s-revoked">—</div><div class="stat-label">Revoked</div></div>
  <div class="stat"><div class="stat-value" id="s-ver">—</div><div class="stat-label">Version</div></div>
</div>
<div class="tabs">
  <button class="tab active" onclick="switchTab('users')">Users</button>
  <button class="tab" onclick="switchTab('logs')">Logs</button>
  <button class="tab" onclick="switchTab('deploy')">Deploy</button>
</div>
<div id="panel-users" class="panel active">
  <div class="section">
    <table>
      <thead><tr><th>ID (hash)</th><th>Tier</th><th>Expiry</th><th>Registered</th><th>Last IP</th><th>Last Seen</th><th>Actions</th></tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</div>
<div id="panel-logs" class="panel">
  <div class="section">
    <table>
      <thead><tr><th>Time</th><th>Event</th><th>User</th><th>IP</th><th>Detail</th></tr></thead>
      <tbody id="log-tbody"></tbody>
    </table>
  </div>
</div>
<div id="panel-deploy" class="panel">
  <div class="section">
    <h2>Deploy Code</h2>
    <div class="deploy-row">
      <input type="file" id="code-file" accept=".pyw">
      <input type="text" id="code-ver" placeholder="Version e.g. 1.0.4" style="width:150px">
      <button class="btn b-blue" onclick="deploCode()">Deploy</button>
    </div>
    <div class="msg" id="deploy-msg"></div>
  </div>
</div>
<script>
const TOK  = location.pathname.replace(/\/+$/, '').split('/').pop();
const BASE = location.origin + '/admin/' + TOK;
let logInterval = null;
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', ['users','logs','deploy'][i] === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  if (name === 'logs') { loadLogs(); if (!logInterval) logInterval = setInterval(loadLogs, 5000); }
  else { clearInterval(logInterval); logInterval = null; }
}
async function api(path, opts) {
  const r = await fetch(BASE + path, opts || {});
  if (r.status === 401 || r.status === 403) {
    document.body.innerHTML = '<p class="expired">Session expired — close this tab and re-run admin.py.</p>';
    throw new Error('expired');
  }
  return r;
}
const badge = tier => {
  const cls = {paid:'paid', revoked:'revoked'}[tier] || 'free';
  return `<span class="badge ${cls}">${tier}</span>`;
};
const fmtDate = d =>
  d ? `<span style="color:#666">${d.replace(' UTC','')}</span>` : '<span style="color:#2a2a2a">—</span>';
const evBadge = (ev, lv) => {
  const cls = lv === 'warn' ? 'ev-warn' : ({verify:'ev-verify',register:'ev-register',code:'ev-code',admin:'ev-admin',deploy:'ev-deploy'}[ev] || 'ev-verify');
  return `<span class="badge ${cls}">${ev}</span>`;
};
async function load() {
  const [ur, vr] = await Promise.all([api('/users'), fetch(location.origin + '/version')]);
  const users = await ur.json();
  const ver = (await vr.json()).version || '—';
  document.getElementById('s-ver').textContent = ver;
  let total = 0, active = 0, revoked = 0;
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = '';
  for (const [guid, u] of Object.entries(users)) {
    total++; u.tier === 'revoked' ? revoked++ : active++;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="guid" title="${guid}">${guid.slice(0,8)}…</td>
      <td id="t-${guid}">${badge(u.tier)}</td>
      <td id="e-${guid}">${fmtDate(u.expiry)}</td>
      <td style="font-size:11px;color:#444">${(u.registered||'').replace(' UTC','')}</td>
      <td style="font-size:11px;color:#444">${u.ip || '—'}</td>
      <td style="font-size:11px;color:#444">${(u.last_seen||'').replace(' UTC','')}</td>
      <td><div style="display:flex;gap:5px;align-items:center;flex-wrap:wrap">
        <select id="sel-${guid}">
          <option value="free_tester"${u.tier==='free_tester'?' selected':''}>free_tester</option>
          <option value="paid"${u.tier==='paid'?' selected':''}>paid</option>
          <option value="revoked"${u.tier==='revoked'?' selected':''}>revoked</option>
        </select>
        <button class="btn b-blue" onclick="setTier('${guid}')">Set</button>
        <input type="text" id="exp-${guid}" placeholder="YYYY-MM-DD" style="width:90px" value="${u.expiry?u.expiry.slice(0,10):''}">
        <button class="btn b-blue" onclick="setExp('${guid}')">Expiry</button>
        <button class="btn b-gray" onclick="clearExp('${guid}')">Clear</button>
      </div></td>`;
    tbody.appendChild(tr);
  }
  document.getElementById('s-total').textContent = total;
  document.getElementById('s-active').textContent = active;
  document.getElementById('s-revoked').textContent = revoked;
}
async function loadLogs() {
  const r = await api('/logs');
  const logs = await r.json();
  const tbody = document.getElementById('log-tbody');
  tbody.innerHTML = '';
  for (const e of logs) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td class="log-time">${e.time}</td><td>${evBadge(e.event, e.level)}</td><td class="log-user">${e.user||'—'}</td><td class="log-ip">${e.ip||'—'}</td><td class="log-detail">${e.detail}</td>`;
    tbody.appendChild(tr);
  }
}
async function setTier(guid) {
  const tier = document.getElementById('sel-' + guid).value;
  const r = await api('/set_tier', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({guid,tier})});
  if (r.ok) document.getElementById('t-' + guid).innerHTML = badge(tier);
}
async function setExp(guid) {
  const val = document.getElementById('exp-' + guid).value.trim();
  if (!val) return;
  const expiry = val + ' 00:00:00 UTC';
  const r = await api('/set_expiry', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({guid,expiry})});
  if (r.ok) document.getElementById('e-' + guid).innerHTML = fmtDate(expiry);
}
async function clearExp(guid) {
  const r = await api('/set_expiry', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({guid,expiry:null})});
  if (r.ok) { document.getElementById('e-' + guid).innerHTML = fmtDate(null); document.getElementById('exp-' + guid).value = ''; }
}
async function deploCode() {
  const file = document.getElementById('code-file').files[0];
  const ver  = document.getElementById('code-ver').value.trim();
  const msg  = document.getElementById('deploy-msg');
  if (!file) { msg.textContent = 'Select a file first.'; msg.className = 'msg err'; return; }
  const fd = new FormData();
  fd.append('file', file);
  if (ver) fd.append('version', ver);
  msg.textContent = 'Deploying…'; msg.className = 'msg';
  const r = await api('/push_code', {method:'POST',body:fd});
  if (r.ok) { msg.textContent = 'Deployed successfully.'; msg.className = 'msg ok'; load(); }
  else      { msg.textContent = 'Deploy failed.';          msg.className = 'msg err'; }
}
load();
setInterval(load, 30000);
</script>
</body>
</html>"""

# ── app routes ────────────────────────────────────────────────────────────────

@app.route("/launcher", methods=["GET"])
def serve_launcher_public():
    """Public bootstrap download — used by start.bat on first run."""
    if not LAUNCHER_FILE.exists():
        return jsonify({"error": "not available"}), 503
    return send_file(LAUNCHER_FILE, mimetype="text/plain", download_name="launcher.py")


@app.route("/rattletrap", methods=["GET"])
def serve_rattletrap():
    """Public download of rattletrap.exe — fetched by launcher on first run."""
    if not RATTLETRAP_FILE.exists():
        return jsonify({"error": "not available"}), 503
    return send_file(RATTLETRAP_FILE, mimetype="application/octet-stream",
                     download_name="rattletrap.exe")


@app.route("/logo", methods=["GET"])
def serve_logo():
    """Public download of logo.ico — used for the desktop shortcut icon."""
    if not LOGO_FILE.exists():
        return jsonify({"error": "not available"}), 503
    return send_file(LOGO_FILE, mimetype="image/x-icon", download_name="logo.ico")


@app.route("/app", methods=["GET"])
def serve_app_exe():
    """Public download of BallchasingUploader.exe — the compiled launcher stub."""
    if not APP_EXE_FILE.exists():
        return jsonify({"error": "not available"}), 503
    return send_file(APP_EXE_FILE, mimetype="application/octet-stream",
                     download_name="BallchasingUploader.exe")


@app.route("/cert")
def serve_cert():
    if not CERT_FILE.exists():
        return jsonify({"error": "cert not ready"}), 503
    return send_file(CERT_FILE, mimetype="application/x-pem-file")


@app.route("/register", methods=["POST"])
def register():
    data      = request.get_json(silent=True) or {}
    client_id = data.get("client_id", "").strip()
    ip        = request.remote_addr

    if not check_rate_limit(ip, "register"):
        log("register", "rate limited", ip=ip, level="warn")
        return jsonify({"error": "too many requests"}), 429

    if not client_id:
        return jsonify({"error": "missing client_id"}), 400

    hashed = hash_id(client_id)
    users  = load_users()

    if hashed in users:
        token = users[hashed]["token"]
        return jsonify({"token": token, "tier": users[hashed]["tier"], "status": "existing"})

    token = make_token(client_id)
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    users[hashed] = {
        "token":      token,
        "tier":       "free_tester",
        "expiry":     None,
        "ip":         ip,
        "registered": now,
        "last_seen":  now,
    }
    save_users(users)
    log("register", "new user", ip=ip)
    return jsonify({"token": token, "tier": "free_tester", "status": "registered"})


@app.route("/verify", methods=["POST"])
def verify():
    data      = request.get_json(silent=True) or {}
    # Accept client_id (new) or machine_guid (old clients during transition)
    client_id = (data.get("client_id") or data.get("machine_guid") or "").strip()
    token     = data.get("token", "").strip()
    ip        = request.remote_addr

    if not check_rate_limit(ip, "verify"):
        log("verify", "rate limited", ip=ip, level="warn")
        return jsonify({"status": "rate_limited"}), 429

    if not client_id or not token:
        return jsonify({"status": "invalid"}), 400

    hashed = hash_id(client_id)
    users  = load_users()

    if hashed not in users:
        log("verify", "unregistered", ip=ip, level="warn")
        return jsonify({"status": "unregistered"}), 403

    user = users[hashed]

    if not verify_token(client_id, token):
        log("verify", "invalid token", ip=ip, level="warn")
        return jsonify({"status": "invalid_token"}), 403

    tier   = user.get("tier", "free_tester")
    expiry = user.get("expiry")

    if tier == "revoked":
        log("verify", "revoked", ip=ip, level="warn")
        return jsonify({"status": "revoked"}), 403

    users[hashed]["last_seen"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    users[hashed]["ip"] = ip
    save_users(users)
    log("verify", f"ok · {tier}", ip=ip)

    return jsonify({"status": "ok", "tier": tier, "expiry": expiry, "version": get_version(),
                    "launcher_version": get_launcher_version()})


@app.route("/code", methods=["POST"])
def serve_code():
    data      = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or data.get("machine_guid") or "").strip()
    token     = data.get("token", "").strip()

    if not check_rate_limit(request.remote_addr, "code"):
        return jsonify({"error": "too many requests"}), 429

    if not client_id or not token:
        return jsonify({"error": "missing fields"}), 400

    hashed = hash_id(client_id)
    users  = load_users()

    if hashed not in users:
        return jsonify({"error": "unregistered"}), 403
    if not verify_token(client_id, token):
        return jsonify({"error": "invalid token"}), 403
    if users[hashed].get("tier") == "revoked":
        return jsonify({"error": "revoked"}), 403
    if not CODE_FILE.exists():
        return jsonify({"error": "code not available"}), 503

    log("code", "served", ip=request.remote_addr)
    return send_file(CODE_FILE, mimetype="application/octet-stream")


@app.route("/version", methods=["GET"])
def version():
    return jsonify({"version": get_version(), "launcher_version": get_launcher_version()})


@app.route("/launcher", methods=["POST"])
def serve_launcher():
    data      = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or data.get("machine_guid") or "").strip()
    token     = data.get("token", "").strip()

    if not check_rate_limit(request.remote_addr, "launcher"):
        return jsonify({"error": "too many requests"}), 429

    if not client_id or not token:
        return jsonify({"error": "missing fields"}), 400

    hashed = hash_id(client_id)
    users  = load_users()

    if hashed not in users:
        return jsonify({"error": "unregistered"}), 403
    if not verify_token(client_id, token):
        return jsonify({"error": "invalid token"}), 403
    if users[hashed].get("tier") == "revoked":
        return jsonify({"error": "revoked"}), 403
    if not LAUNCHER_FILE.exists():
        return jsonify({"error": "launcher not available"}), 503

    log("launcher", "served", ip=request.remote_addr)
    return send_file(LAUNCHER_FILE, mimetype="text/plain")



# ── admin auth ────────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["POST"])
def admin_login():
    ip    = request.remote_addr
    data  = request.get_json(silent=True) or {}
    guid  = data.get("guid", "").strip()
    proof = data.get("proof", "").strip()
    if not check_rate_limit(ip, "admin_login"):
        log("admin", "rate limited", ip=ip, level="warn")
        return jsonify({"error": "too many requests"}), 429
    if guid != ADMIN_GUID:
        return jsonify({"error": "unauthorized"}), 403
    if not check_admin_proof(guid, proof):
        log("admin", "wrong password", ip=ip, level="warn")
        return jsonify({"error": "invalid password"}), 403
    log("admin", "login", ip=request.remote_addr)
    token = new_session()
    return jsonify({"url": f"/admin/{token}"})


@app.route("/admin/set_password", methods=["POST"])
def admin_set_password():
    data    = request.get_json(silent=True) or {}
    guid    = data.get("guid", "").strip()
    proof   = data.get("proof", "").strip()
    pw_hash = data.get("pw_hash", "").strip()

    if guid != ADMIN_GUID:
        return jsonify({"error": "unauthorized"}), 403

    # verify proof = hmac(guid, pw_hash) so we know they have the password
    expected = hmac.new(guid.encode(), pw_hash.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, proof):
        return jsonify({"error": "invalid proof"}), 403

    # if a password is already set, require the old one too
    if get_admin_pw_hash():
        old_proof = data.get("old_proof", "").strip()
        if not check_admin_proof(guid, old_proof):
            return jsonify({"error": "wrong current password"}), 403

    ADMIN_PW_FILE.write_text(pw_hash)
    return jsonify({"status": "ok"})


# ── dashboard ─────────────────────────────────────────────────────────────────

@app.route("/admin/<token>")
def dashboard(token):
    if not is_valid_session(token):
        return Response("Session expired or invalid.", status=401, mimetype="text/plain")
    return Response(DASHBOARD_HTML, mimetype="text/html")


@app.route("/admin/<token>/users")
@require_session
def dash_users(token):
    return jsonify(load_users())


@app.route("/admin/<token>/set_tier", methods=["POST"])
@require_session
def dash_set_tier(token):
    data = request.get_json(silent=True) or {}
    guid = data.get("guid", "").strip()
    tier = data.get("tier", "").strip()
    if tier not in ("free_tester", "paid", "revoked"):
        return jsonify({"error": "invalid tier"}), 400
    users = load_users()
    if guid not in users:
        return jsonify({"error": "not found"}), 404
    users[guid]["tier"] = tier
    save_users(users)
    return jsonify({"status": "ok"})


@app.route("/admin/<token>/set_expiry", methods=["POST"])
@require_session
def dash_set_expiry(token):
    data   = request.get_json(silent=True) or {}
    guid   = data.get("guid", "").strip()
    expiry = data.get("expiry")
    users  = load_users()
    if guid not in users:
        return jsonify({"error": "not found"}), 404
    users[guid]["expiry"] = expiry
    save_users(users)
    return jsonify({"status": "ok"})


@app.route("/admin/<token>/logs")
@require_session
def dash_logs(token):
    return jsonify(list(_logs))


@app.route("/admin/<token>/push_code", methods=["POST"])
@require_session
def dash_push_code(token):
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    version = request.form.get("version", "")
    CODE_FILE.write_bytes(request.files["file"].read())
    if version:
        VERSION_FILE.write_text(version)
    log("deploy", f"v{version or get_version()}", ip=request.remote_addr)
    return jsonify({"status": "ok", "version": get_version()})



@app.errorhandler(404)
def not_found(e):
    log("scan", f"{request.method} {request.path}", ip=request.remote_addr, level="warn")
    return "", 404


# ── legacy script admin routes (used by deploy.py) ────────────────────────────

@app.route("/admin/users", methods=["POST"])
@require_admin
def list_users():
    return jsonify(load_users())


@app.route("/admin/revoke", methods=["POST"])
@require_admin
def revoke_user():
    guid  = request.json.get("guid", "").strip()
    users = load_users()
    if guid not in users:
        return jsonify({"error": "user not found"}), 404
    users[guid]["tier"] = "revoked"
    save_users(users)
    return jsonify({"status": "revoked"})


@app.route("/admin/restore", methods=["POST"])
@require_admin
def restore_user():
    guid  = request.json.get("guid", "").strip()
    tier  = request.json.get("tier", "free_tester").strip()
    users = load_users()
    if guid not in users:
        return jsonify({"error": "user not found"}), 404
    users[guid]["tier"] = tier
    save_users(users)
    return jsonify({"status": "restored", "tier": tier})


@app.route("/admin/set_expiry", methods=["POST"])
@require_admin
def set_expiry():
    guid   = request.json.get("guid", "").strip()
    expiry = request.json.get("expiry")
    users  = load_users()
    if guid not in users:
        return jsonify({"error": "user not found"}), 404
    users[guid]["expiry"] = expiry
    save_users(users)
    return jsonify({"status": "updated"})


@app.route("/admin/push_code", methods=["POST"])
@require_admin
def push_code():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    version = request.form.get("version")
    CODE_FILE.write_bytes(request.files["file"].read())
    if version:
        VERSION_FILE.write_text(version)
    return jsonify({"status": "updated", "version": get_version()})


# ── startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_ssl()

    def run_http():
        srv = make_server("0.0.0.0", 8766, app, threaded=True)
        print("HTTP  → :8766")
        srv.serve_forever()

    threading.Thread(target=run_http, daemon=True).start()

    ctx = ssl_lib.SSLContext(ssl_lib.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    https_srv = make_server("0.0.0.0", 8767, app, threaded=True, ssl_context=ctx)
    print("HTTPS → :8767")
    https_srv.serve_forever()


@app.route('/uninstall', methods=['GET'])
def serve_uninstall():
    """Public download of uninstall.bat."""
    f = BASE / 'uninstall.bat'
    if not f.exists():
        return jsonify({'error': 'not available'}), 503
    return send_file(f, mimetype='application/octet-stream',
                     download_name='uninstall.bat')
