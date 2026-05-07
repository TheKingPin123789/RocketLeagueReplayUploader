"""
Deploy a new version of main.pyw to the app server.
Run from your local machine: python deploy.py
"""
import requests, sys, winreg, shutil
from pathlib import Path
from datetime import datetime

SERVER      = "http://46.101.184.78:8766"
SCRIPT      = Path(__file__).parent.parent / "src" / "main.pyw"
VERSION_FILE= Path(__file__).parent / "version.txt"
BACKUP_DIR  = Path(__file__).parent / "backups"

def get_admin_guid() -> str:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
        return winreg.QueryValueEx(k, "MachineGuid")[0]

def get_version() -> str:
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text().strip()
    # try reading from main.pyw directly
    for line in SCRIPT.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("VERSION"):
            return line.split("=")[1].strip().strip('"').strip("'")
    return "unknown"

def main():
    admin_guid = get_admin_guid()
    version    = get_version()

    print(f"Deploying {SCRIPT.name}  (version {version})")
    print(f"Server: {SERVER}")
    print(f"Admin GUID: {admin_guid}")
    confirm = input("Continue? (y/n): ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    BACKUP_DIR.mkdir(exist_ok=True)
    backup = BACKUP_DIR / f"main_{version}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pyw"
    shutil.copy2(SCRIPT, backup)
    print(f"Backup saved: {backup.name}")

    with open(SCRIPT, "rb") as f:
        resp = requests.post(
            f"{SERVER}/admin/push_code",
            data={"admin_guid": admin_guid, "version": version},
            files={"file": ("main.pyw", f, "application/octet-stream")},
            timeout=30,
        )

    if resp.status_code == 200:
        print(f"Done. Server is now on version {resp.json().get('version')}")
    else:
        print(f"Failed: {resp.status_code} — {resp.text}")

if __name__ == "__main__":
    main()
