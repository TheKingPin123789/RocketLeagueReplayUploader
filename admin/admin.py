"""
admin.py — open the admin dashboard.
  python admin.py                → authenticate and open dashboard in browser
  python admin.py --set-password → set or change the admin password
"""
import sys, hashlib, hmac, getpass, winreg, webbrowser
import requests
from pathlib import Path

SERVER    = "https://46.101.184.78:8767"
CERT_FILE = Path(__file__).parent / "server.crt"


def get_machine_guid() -> str:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
        return winreg.QueryValueEx(k, "MachineGuid")[0]


def fetch_cert():
    """Trust-on-first-use: download and pin the server cert."""
    if CERT_FILE.exists():
        return
    print("First run — downloading server certificate (trust on first use)...")
    r = requests.get("http://46.101.184.78:8766/cert", timeout=10)
    if r.status_code != 200:
        print("Failed to download certificate from server.")
        sys.exit(1)
    CERT_FILE.write_bytes(r.content)
    fp = hashlib.sha256(r.content).hexdigest()
    print(f"Certificate saved.")
    print(f"Fingerprint (SHA256): {fp[:16]}...{fp[-16:]}")
    print("If the server cert ever changes you will be prompted to re-pin.\n")


def make_proof(guid: str, password: str):
    """Returns (pw_hash, proof). proof = hmac(guid, pw_hash) — machine-bound."""
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    proof   = hmac.new(guid.encode(), pw_hash.encode(), hashlib.sha256).hexdigest()
    return pw_hash, proof


def post(path: str, payload: dict) -> requests.Response:
    try:
        return requests.post(f"{SERVER}{path}", json=payload, verify=str(CERT_FILE), timeout=10)
    except requests.exceptions.SSLError:
        print("\nSSL error: server certificate has changed or is invalid.")
        print(f"Delete {CERT_FILE} and re-run to re-pin the certificate.")
        sys.exit(1)
    except requests.RequestException as e:
        print(f"\nConnection error: {e}")
        sys.exit(1)


def cmd_set_password(guid: str):
    new_pw  = getpass.getpass("New password: ")
    confirm = getpass.getpass("Confirm password: ")
    if new_pw != confirm:
        print("Passwords do not match.")
        sys.exit(1)

    pw_hash, proof = make_proof(guid, new_pw)
    payload = {"guid": guid, "proof": proof, "pw_hash": pw_hash}

    r = post("/admin/set_password", payload)

    if r.status_code == 200:
        print("Password set successfully.")
    elif r.json().get("error") == "wrong current password":
        old_pw = getpass.getpass("Current password: ")
        _, old_proof = make_proof(guid, old_pw)
        payload["old_proof"] = old_proof
        r2 = post("/admin/set_password", payload)
        if r2.status_code == 200:
            print("Password changed successfully.")
        else:
            print(f"Failed: {r2.json().get('error', r2.status_code)}")
    else:
        print(f"Failed: {r.json().get('error', r.status_code)}")


def cmd_login(guid: str):
    password = getpass.getpass("Admin password: ")
    _, proof = make_proof(guid, password)

    r = post("/admin/login", {"guid": guid, "proof": proof})

    if r.status_code == 200:
        url = SERVER + r.json()["url"]
        print(f"Authenticated. Opening dashboard...")
        webbrowser.open(url)
    elif r.status_code == 403:
        err = r.json().get("error", "")
        if err == "invalid password":
            print("Wrong password.")
        else:
            print("Unauthorized — this machine is not the admin machine.")
    else:
        print(f"Error: {r.status_code} — {r.text}")


def main():
    guid = get_machine_guid()
    fetch_cert()

    if "--set-password" in sys.argv:
        cmd_set_password(guid)
    else:
        cmd_login(guid)


if __name__ == "__main__":
    main()
