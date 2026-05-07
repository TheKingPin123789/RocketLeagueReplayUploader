"""
Launcher — handles auth, version check, and starts main.pyw.
Never deleted by the expiry flow. Called by start.bat.
"""
import sys, os, json, winreg, socket, hashlib, hmac, base64, time
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

# ── add bundled libs ───────────────────────────────────────────────────────────
LIB_DIR = Path(__file__).parent / "src" / "lib"
if LIB_DIR.exists() and str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import requests

# ── paths ──────────────────────────────────────────────────────────────────────
BASE        = Path(__file__).parent
CONFIG_FILE = BASE / "src" / "config.json"
SCRIPT      = BASE / "src" / "main.pyw"
SERVER      = "http://46.101.184.78:8766"

# ── helpers ────────────────────────────────────────────────────────────────────
def sign_expiry(expiry: str, tier: str, guid: str) -> str:
    payload = f"{expiry}|{tier}"
    sig = hmac.new(guid.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.b64encode(f"{payload}|{sig}".encode()).decode()

def verify_expiry(signed: str, guid: str):
    """Returns (expiry, tier) if valid, None if tampered. Empty string → free_tester."""
    if not signed:
        return ("", "free_tester")
    try:
        decoded = base64.b64decode(signed.encode()).decode()
        expiry, tier, sig = decoded.rsplit("|", 2)
        payload = f"{expiry}|{tier}"
        expected = hmac.new(guid.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, sig):
            return (expiry, tier)
    except Exception:
        pass
    return None

def get_machine_guid() -> str:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
        return winreg.QueryValueEx(k, "MachineGuid")[0]

def get_username() -> str:
    try:
        return os.getlogin()
    except Exception:
        return "unknown"

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

def ask(title: str, msg: str) -> bool:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    result = messagebox.askyesno(title, msg)
    root.destroy()
    return result

def delete_app():
    keep = ask("Subscription", "Do you want to keep your saved replay data for when you reconnect?")
    if not keep:
        import shutil
        cache = BASE / "src" / "cache"
        if cache.exists():
            shutil.rmtree(cache, ignore_errors=True)
        uploaded = BASE / "src" / "uploaded.json"
        if uploaded.exists():
            uploaded.unlink(missing_ok=True)
    if SCRIPT.exists():
        SCRIPT.unlink(missing_ok=True)

def launch():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    import subprocess
    subprocess.Popen([str(pythonw), str(SCRIPT)])
    sys.exit(0)

# ── main flow ──────────────────────────────────────────────────────────────────
def main():
    guid = get_machine_guid()
    cfg  = load_config()

    token = cfg.get("_auth_token", "")

    # ── step 1: register if no token ──────────────────────────────────────────
    if not token:
        try:
            r = requests.post(f"{SERVER}/register", json={
                "machine_guid": guid,
                "username":     get_username(),
            }, timeout=8)
            if r.status_code == 200:
                data  = r.json()
                token = data.get("token", "")
                cfg["_auth_token"] = token
                cfg.pop("_tier", None)
                save_config(cfg)
            else:
                alert("Registration Failed",
                      f"Could not register with the server ({r.status_code}).\n"
                      f"Please visit http://46.101.184.78 to download the app.")
                sys.exit(1)
        except requests.RequestException:
            if not SCRIPT.exists():
                alert("No Internet",
                      "Could not reach the server and no local copy found.\n"
                      "Please connect to the internet and visit http://46.101.184.78 to download the app.")
                sys.exit(1)
            # has local copy, fall through to expiry check
            _run_with_local_expiry_check(cfg)
            return

    # ── step 2: verify token + get version ────────────────────────────────────
    try:
        r = requests.post(f"{SERVER}/verify", json={
            "machine_guid": guid,
            "token":        token,
        }, timeout=8)

        if r.status_code == 200:
            data    = r.json()
            tier    = data.get("tier", "free_tester")
            expiry  = data.get("expiry")
            version = data.get("version", "")
            cfg.pop("_tier", None)
            cfg["_signed_expiry"] = sign_expiry(expiry or "", tier, guid)
            save_config(cfg)

            # ── step 3: check version, download if outdated ───────────────────
            local_version = _read_local_version()
            if not SCRIPT.exists() or (version and version != local_version):
                _download_code(guid, token, version, cfg)
            else:
                launch()

        elif r.status_code == 403:
            status = r.json().get("status", "")
            if status in ("revoked", "expired"):
                if SCRIPT.exists():
                    launch()  # main.pyw shows the revoke screen
                else:
                    alert("Subscription Ended",
                          "Your access has been revoked or your subscription has expired.\n"
                          "Please renew to continue using the app.")
                    sys.exit(0)
            elif status == "unregistered":
                # token mismatch — re-register
                cfg.pop("_auth_token", None)
                save_config(cfg)
                main()  # retry
            else:
                delete_app()
                alert("Access Denied", "Your access has been denied. Please contact support.")
            sys.exit(0)
        else:
            _run_with_local_expiry_check(cfg)

    except requests.RequestException:
        _run_with_local_expiry_check(cfg)


def _download_code(guid: str, token: str, version: str, cfg: dict):
    try:
        r = requests.post(f"{SERVER}/code", json={
            "machine_guid": guid,
            "token":        token,
        }, timeout=30)
        if r.status_code == 200:
            SCRIPT.parent.mkdir(parents=True, exist_ok=True)
            tmp = SCRIPT.with_suffix(".pyw.tmp")
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


def _read_local_version() -> str:
    cfg = load_config()
    return cfg.get("_local_version", "")


def _run_with_local_expiry_check(cfg: dict):
    """Server unreachable — check local signed expiry then run or delete."""
    guid   = get_machine_guid()
    signed = cfg.get("_signed_expiry", "")
    result = verify_expiry(signed, guid)

    if result is None:
        # signature invalid — tampered
        delete_app()
        alert("Verification Failed",
              "Subscription data appears to have been tampered with.\n"
              "Please connect to the internet to re-verify.")
        sys.exit(0)

    expiry, _tier = result
    if expiry:
        try:
            from datetime import datetime
            expiry_ts = datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S UTC").timestamp()
            if time.time() > expiry_ts:
                delete_app()
                alert("Subscription Expired",
                      "Your subscription has expired and the server is unreachable.\n"
                      "Please connect to the internet to verify your subscription.")
                sys.exit(0)
        except Exception:
            pass

    if SCRIPT.exists():
        launch()
    else:
        alert("No Internet",
              "Could not reach the server and no local copy found.\n"
              "Please connect to the internet and visit http://46.101.184.78 to download the app.")
        sys.exit(1)


if __name__ == "__main__":
    main()
