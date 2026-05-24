"""
Launcher — version check and starts main.pyw.
Never deleted by the expiry flow. Called by start.bat.
"""
import sys, os, json, uuid, hashlib, struct, ssl as _ssl_mod
import tkinter as tk
from tkinter import messagebox
from pathlib import Path
from datetime import datetime

# ── paths (frozen-aware: PyInstaller sets sys.frozen and extracts to a temp dir) -
if getattr(sys, 'frozen', False):
    BASE = Path(sys.executable).parent   # real install folder
else:
    BASE = Path(__file__).parent

LIB_DIR = BASE / "src" / "lib"
if LIB_DIR.exists() and str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import requests

# ── paths ──────────────────────────────────────────────────────────────────────
CONFIG_FILE = BASE / "src" / "config.json"
SCRIPT      = BASE / "src" / "main.enc"     # encrypted on-disk blob
SCRIPT_REF  = BASE / "src" / "main.pyw"     # logical name used for __file__ inside exec
CERT_FILE   = BASE / "server.crt"           # pinned server certificate
SERVER_HTTP  = "http://46.101.184.78:8766"  # cert download + HTTP fallback
SERVER_HTTPS = "https://46.101.184.78:8767" # all auth traffic (encrypted)
LAUNCHER_VERSION = "1.4"

# ── encryption (SHA-256 CTR stream cipher, key = HMAC of machine GUID) ────────
_SALT = b"bcu_enc_v1"

def _derive_key(guid: str) -> bytes:
    import hmac
    return hmac.new(_SALT, guid.encode(), hashlib.sha256).digest()

def _keystream(key: bytes, length: int) -> bytes:
    out = bytearray()
    ctr = 0
    while len(out) < length:
        out.extend(hashlib.sha256(key + struct.pack("<Q", ctr)).digest())
        ctr += 1
    return bytes(out[:length])

def _xcrypt(data: bytes, guid: str) -> bytes:
    """Encrypt or decrypt (XOR is symmetric)."""
    key = _derive_key(guid)
    ks  = _keystream(key, len(data))
    return bytes(a ^ b for a, b in zip(data, ks))

# ── certificate management ────────────────────────────────────────────────────
def _cert_days_remaining() -> int:
    """Returns days until the pinned cert expires, or -1 if missing/unreadable."""
    if not CERT_FILE.exists():
        return -1
    try:
        info       = _ssl_mod._ssl._test_decode_cert(str(CERT_FILE))
        expiry_str = info["notAfter"]                                    # e.g. "May 14 00:00:00 2036 GMT"
        expiry     = datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z")
        return (expiry - datetime.utcnow()).days
    except Exception:
        return -1

def _ensure_cert() -> bool:
    """Download or refresh the server cert when missing or expiring within 30 days.
    Returns True if a usable cert is available, False if download failed entirely."""
    if _cert_days_remaining() > 30:
        return True                                  # cert is healthy, nothing to do
    try:
        r = requests.get(f"{SERVER_HTTP}/cert", timeout=8)
        if r.status_code == 200:
            CERT_FILE.write_bytes(r.content)
            return True
    except Exception:
        pass
    return CERT_FILE.exists()                        # failed to refresh — use existing if present

def _post(path: str, **kwargs) -> requests.Response:
    """POST to the server. Uses HTTPS with the pinned cert when available,
    falls back to plain HTTP if the cert is missing or the TLS handshake fails."""
    if CERT_FILE.exists():
        try:
            return requests.post(f"{SERVER_HTTPS}{path}", verify=str(CERT_FILE), **kwargs)
        except requests.exceptions.SSLError:
            pass                                     # cert mismatch or server not yet restarted
    return requests.post(f"{SERVER_HTTP}{path}", **kwargs)

# ── helpers ────────────────────────────────────────────────────────────────────
def get_or_create_client_id(cfg: dict) -> str:
    """Return the app-specific client ID, generating a random UUID on first launch.
    Clears old auth data and removes the old encrypted script (which was keyed to
    the old machine GUID) so it gets re-downloaded and re-encrypted with the new key."""
    if "_client_id" not in cfg:
        cfg["_client_id"] = str(uuid.uuid4())
        cfg.pop("_auth_token",    None)
        cfg.pop("_local_version", None)   # forces version mismatch → fresh download
        SCRIPT.unlink(missing_ok=True)    # remove old file encrypted with wrong key
        save_config(cfg)
    return cfg["_client_id"]

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_config(cfg: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")

def alert(title: str, msg: str):
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    messagebox.showinfo(title, msg)
    root.destroy()

def launch():
    cfg       = load_config()
    client_id = cfg.get("_client_id", "")

    # ── migrate: encrypt any leftover plaintext main.pyw ──────────────────────
    if SCRIPT_REF.exists() and not SCRIPT.exists():
        plaintext = SCRIPT_REF.read_bytes()
        SCRIPT.write_bytes(_xcrypt(plaintext, client_id))
        SCRIPT_REF.unlink(missing_ok=True)

    if not SCRIPT.exists():
        alert("Missing File", "Application file not found. Please reinstall.")
        sys.exit(1)

    code = _xcrypt(SCRIPT.read_bytes(), client_id)

    # run in a clean namespace so __file__ resolves correctly inside main.pyw
    ns = {"__file__": str(SCRIPT_REF), "__name__": "__main__"}
    exec(compile(code, str(SCRIPT_REF), "exec"), ns)  # noqa: S102
    sys.exit(0)

# ── main flow ──────────────────────────────────────────────────────────────────
def main():
    cfg       = load_config()
    client_id = get_or_create_client_id(cfg)   # generates UUID on first launch

    # record our own version so main.pyw can detect when the server has a newer launcher
    if cfg.get("_launcher_version") != LAUNCHER_VERSION:
        cfg["_launcher_version"] = LAUNCHER_VERSION
        save_config(cfg)

    token = cfg.get("_auth_token", "")

    # ── step 0: ensure we have a valid cert before making any HTTPS calls ─────
    _ensure_cert()

    # ── step 1: register if no token ──────────────────────────────────────────
    if not token:
        try:
            r = _post("/register", json={
                "client_id": client_id,
            }, timeout=8)
            if r.status_code == 200:
                data  = r.json()
                token = data.get("token", "")
                cfg["_auth_token"] = token
                save_config(cfg)
            else:
                alert("Registration Failed",
                      f"Could not register with the server ({r.status_code}).\n"
                      f"Please visit http://46.101.184.78 to download the app.")
                sys.exit(1)
        except requests.RequestException:
            if not SCRIPT.exists() and not SCRIPT_REF.exists():
                alert("No Internet",
                      "Could not reach the server and no local copy found.\n"
                      "Please connect to the internet and visit http://46.101.184.78 to download the app.")
                sys.exit(1)
            launch()
            return

    # ── step 2: verify token + get version ────────────────────────────────────
    try:
        r = _post("/verify", json={
            "client_id": client_id,
            "token":     token,
        }, timeout=8)

        if r.status_code == 200:
            data    = r.json()
            version = data.get("version", "")
            cfg["_server_launcher_version"] = data.get("launcher_version", "")
            save_config(cfg)

            # ── step 3: check version, download if outdated ───────────────────
            local_version = _read_local_version()
            has_script = SCRIPT.exists() or SCRIPT_REF.exists()
            if not has_script or (version and version != local_version):
                _download_code(client_id, token, version, cfg)
            else:
                launch()

        elif r.status_code == 403:
            # Server can still revoke bad actors; for everyone else just launch
            if SCRIPT.exists() or SCRIPT_REF.exists():
                launch()
            else:
                alert("Access Denied",
                      "Access denied by server. Please contact support.")
                sys.exit(0)
        else:
            # Server error — run with whatever is cached locally
            if SCRIPT.exists() or SCRIPT_REF.exists():
                launch()
            else:
                alert("Server Error",
                      f"Server returned {r.status_code}. Please try again later.")
                sys.exit(1)

    except requests.RequestException:
        # Offline — run with cached copy
        if SCRIPT.exists() or SCRIPT_REF.exists():
            launch()
        else:
            alert("No Internet",
                  "Could not reach the server and no local copy found.\n"
                  "Please connect to the internet and try again.")
            sys.exit(1)


def _download_code(client_id: str, token: str, version: str, cfg: dict):
    try:
        r = _post("/code", json={
            "client_id": client_id,
            "token":     token,
        }, timeout=30)
        if r.status_code == 200:
            # encrypt with client-specific key before writing to disk
            encrypted = _xcrypt(r.content, client_id)
            SCRIPT.parent.mkdir(parents=True, exist_ok=True)
            tmp = SCRIPT.with_suffix(".tmp")
            tmp.write_bytes(encrypted)
            tmp.replace(SCRIPT)
            # remove any leftover plaintext copy
            if SCRIPT_REF.exists():
                SCRIPT_REF.unlink(missing_ok=True)
            cfg["_local_version"] = version
            save_config(cfg)
            launch()
        else:
            if SCRIPT.exists() or SCRIPT_REF.exists():
                launch()
            else:
                alert("Download Failed",
                      f"Could not download the app ({r.status_code}).\n"
                      "Please try again later.")
                sys.exit(1)
    except requests.RequestException:
        if SCRIPT.exists() or SCRIPT_REF.exists():
            launch()
        else:
            alert("No Internet",
                  "Could not reach the server.\n"
                  "Please connect to the internet and try again.")
            sys.exit(1)


def _read_local_version() -> str:
    cfg = load_config()
    return cfg.get("_local_version", "")


if __name__ == "__main__":
    main()
