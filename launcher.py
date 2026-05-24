"""
Launcher — version check and starts main.pyw.
Never deleted by any cleanup flow. Called by start.bat.
"""
import sys, os, json, uuid
import tkinter as tk
from tkinter import messagebox
from pathlib import Path
from datetime import datetime
import ssl as _ssl_mod

# ── paths (frozen-aware) ───────────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BASE = Path(sys.executable).parent
else:
    BASE = Path(__file__).parent

LIB_DIR = BASE / "src" / "lib"
if LIB_DIR.exists() and str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import requests

# ── paths ──────────────────────────────────────────────────────────────────────
CONFIG_FILE  = BASE / "src" / "config.json"
SCRIPT       = BASE / "src" / "main.pyw"    # plaintext script — exec'd directly
SCRIPT_ENC   = BASE / "src" / "main.enc"    # old encrypted blob (migrated away)
CERT_FILE       = BASE / "server.crt"
RATTLETRAP_FILE = BASE / "src" / "rattletrap.exe"
LOGO_FILE       = BASE / "src" / "logo.ico"
SERVER_HTTP  = "http://46.101.184.78:8766"
SERVER_HTTPS = "https://46.101.184.78:8767"
LAUNCHER_VERSION = "1.7"

# ── certificate management ────────────────────────────────────────────────────
def _cert_days_remaining() -> int:
    if not CERT_FILE.exists():
        return -1
    try:
        info       = _ssl_mod._ssl._test_decode_cert(str(CERT_FILE))
        expiry_str = info["notAfter"]
        expiry     = datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z")
        return (expiry - datetime.utcnow()).days
    except Exception:
        return -1

def _ensure_cert() -> bool:
    if _cert_days_remaining() > 30:
        return True
    try:
        r = requests.get(f"{SERVER_HTTP}/cert", timeout=8)
        if r.status_code == 200:
            CERT_FILE.write_bytes(r.content)
            return True
    except Exception:
        pass
    return CERT_FILE.exists()

def _post(path: str, **kwargs) -> requests.Response:
    if CERT_FILE.exists():
        try:
            return requests.post(f"{SERVER_HTTPS}{path}", verify=str(CERT_FILE), **kwargs)
        except requests.exceptions.SSLError:
            pass
    return requests.post(f"{SERVER_HTTP}{path}", **kwargs)

# ── helpers ────────────────────────────────────────────────────────────────────
def get_or_create_client_id(cfg: dict) -> str:
    """Return the app-specific client ID, generating a random UUID on first launch."""
    if "_client_id" not in cfg:
        cfg["_client_id"] = str(uuid.uuid4())
        cfg.pop("_auth_token",    None)
        cfg.pop("_local_version", None)
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

def _migrate_enc():
    """Remove the old encrypted main.enc so the plaintext version gets downloaded."""
    if SCRIPT_ENC.exists():
        SCRIPT_ENC.unlink(missing_ok=True)

def _ensure_rattletrap():
    """Download rattletrap.exe from server if missing."""
    if RATTLETRAP_FILE.exists():
        return
    try:
        r = requests.get(f"{SERVER_HTTP}/rattletrap", timeout=30)
        if r.status_code == 200:
            RATTLETRAP_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = RATTLETRAP_FILE.with_suffix(".tmp")
            tmp.write_bytes(r.content)
            tmp.replace(RATTLETRAP_FILE)
    except Exception:
        pass  # not fatal — app will show parse errors but still run

def _ensure_logo():
    """Download logo.ico from server if missing."""
    if LOGO_FILE.exists():
        return
    try:
        r = requests.get(f"{SERVER_HTTP}/logo", timeout=10)
        if r.status_code == 200:
            LOGO_FILE.parent.mkdir(parents=True, exist_ok=True)
            LOGO_FILE.write_bytes(r.content)
    except Exception:
        pass

def _create_shortcut():
    """Create/update the desktop shortcut pointing to start.bat with the app icon."""
    try:
        import subprocess
        start_bat = BASE / "start.bat"
        if not start_bat.exists():
            return
        icon_str = str(LOGO_FILE) if LOGO_FILE.exists() else ""
        icon_clause = f"$s.IconLocation='{icon_str},0'; " if icon_str else ""
        ps = (
            f"$ws=New-Object -ComObject WScript.Shell; "
            f"$s=$ws.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\\Ballchasing Uploader.lnk'); "
            f"$s.TargetPath='{start_bat}'; "
            f"$s.WorkingDirectory='{BASE}'; "
            f"$s.Description='Ballchasing Auto Uploader'; "
            f"{icon_clause}"
            f"$s.Save()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, timeout=10
        )
    except Exception:
        pass

def launch():
    if not SCRIPT.exists():
        alert("Missing File", "Application file not found. Please reinstall.")
        sys.exit(1)
    _ensure_rattletrap()
    _ensure_logo()
    _create_shortcut()
    ns = {"__file__": str(SCRIPT), "__name__": "__main__"}
    exec(compile(SCRIPT.read_bytes(), str(SCRIPT), "exec"), ns)  # noqa: S102
    sys.exit(0)

# ── main flow ──────────────────────────────────────────────────────────────────
def main():
    # Clean up old encrypted blob if present (one-time migration)
    _migrate_enc()

    cfg       = load_config()
    client_id = get_or_create_client_id(cfg)

    if cfg.get("_launcher_version") != LAUNCHER_VERSION:
        cfg["_launcher_version"] = LAUNCHER_VERSION
        save_config(cfg)

    token = cfg.get("_auth_token", "")

    _ensure_cert()

    # ── step 1: register if no token ──────────────────────────────────────────
    if not token:
        try:
            r = _post("/register", json={"client_id": client_id}, timeout=8)
            if r.status_code == 200:
                token = r.json().get("token", "")
                cfg["_auth_token"] = token
                save_config(cfg)
            else:
                alert("Registration Failed",
                      f"Could not register with the server ({r.status_code}).\n"
                      "Please visit http://46.101.184.78 to download the app.")
                sys.exit(1)
        except requests.RequestException:
            if not SCRIPT.exists():
                alert("No Internet",
                      "Could not reach the server and no local copy found.\n"
                      "Please connect to the internet and try again.")
                sys.exit(1)
            launch()
            return

    # ── step 2: verify token + get version ────────────────────────────────────
    try:
        r = _post("/verify", json={"client_id": client_id, "token": token}, timeout=8)

        if r.status_code == 200:
            data    = r.json()
            version = data.get("version", "")
            cfg["_server_launcher_version"] = data.get("launcher_version", "")
            save_config(cfg)

            local_version = cfg.get("_local_version", "")
            if not SCRIPT.exists() or (version and version != local_version):
                _download_code(client_id, token, version, cfg)
            else:
                launch()

        elif r.status_code == 403:
            if SCRIPT.exists():
                launch()
            else:
                alert("Access Denied", "Access denied by server. Please contact support.")
                sys.exit(0)
        else:
            if SCRIPT.exists():
                launch()
            else:
                alert("Server Error",
                      f"Server returned {r.status_code}. Please try again later.")
                sys.exit(1)

    except requests.RequestException:
        if SCRIPT.exists():
            launch()
        else:
            alert("No Internet",
                  "Could not reach the server and no local copy found.\n"
                  "Please connect to the internet and try again.")
            sys.exit(1)


def _download_code(client_id: str, token: str, version: str, cfg: dict):
    try:
        r = _post("/code", json={"client_id": client_id, "token": token}, timeout=30)
        if r.status_code == 200:
            SCRIPT.parent.mkdir(parents=True, exist_ok=True)
            tmp = SCRIPT.with_suffix(".tmp")
            tmp.write_bytes(r.content)
            tmp.replace(SCRIPT)
            cfg["_local_version"] = version
            save_config(cfg)
            launch()
        else:
            if SCRIPT.exists():
                launch()
            else:
                alert("Download Failed",
                      f"Could not download the app ({r.status_code}).\n"
                      "Please try again later.")
                sys.exit(1)
    except requests.RequestException:
        if SCRIPT.exists():
            launch()
        else:
            alert("No Internet",
                  "Could not reach the server.\n"
                  "Please connect to the internet and try again.")
            sys.exit(1)


if __name__ == "__main__":
    main()
