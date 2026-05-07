"""
Build the release zip and upload it to the server.
Run: python build_release.py
"""
import io, zipfile, sys, subprocess
from pathlib import Path

ROOT      = Path(__file__).parent.parent
OUT_DIR   = Path(__file__).parent / "server" / "download"
OUT_FILE  = OUT_DIR / "BallchasingUploader.zip"

SERVER    = "root@46.101.184.78"
REMOTE    = "/var/www/html/download/BallchasingUploader.zip"

APP_FILES = [
    "start.bat",
    "launcher.py",
    "requirements.txt",
    "analyze_replay.py",
]


def main():
    missing = [f for f in APP_FILES if not (ROOT / f).exists()]
    if missing:
        print(f"Missing files: {missing}")
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in APP_FILES:
            zf.write(ROOT / name, f"BallchasingUploader/{name}")
        zf.writestr("BallchasingUploader/src/.keep", "")

    OUT_FILE.write_bytes(buf.getvalue())
    print(f"Built {OUT_FILE.name} — {OUT_FILE.stat().st_size:,} bytes")

    confirm = input("Upload to server? (y/n): ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    key = Path.home() / ".ssh" / "do_key"
    result = subprocess.run(
        ["scp", "-i", str(key), str(OUT_FILE), f"{SERVER}:{REMOTE}"],
        text=True,
    )

    if result.returncode == 0:
        print("Done. Zip is live at http://46.101.184.78/download/BallchasingUploader.zip")
    else:
        print("Upload failed.")


if __name__ == "__main__":
    main()
