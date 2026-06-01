# Ballchasing Auto Uploader

A Windows desktop app that automatically uploads Rocket League replays to [ballchasing.com](https://ballchasing.com) as soon as they're saved.

---

## Features

- **Auto-upload** — watches your Rocket League demos folder and uploads new replays instantly
- **Smart duplicate detection** — checks each replay against existing uploads before sending
- **Bulk download** — fetch your existing Ballchasing replays into the local cache
- **Replay stats** — view boost, positioning, movement and ball stats from Ballchasing in-app
- **Proper titles** — replays are automatically named (e.g. *2026-05-29 Ranked Doubles*) instead of UUID filenames
- **Deleted replay detection** — notifies you when a replay has been removed from Ballchasing
- **Low Priority Mode** — runs at below-normal CPU priority in the background so Rocket League always gets priority
- **Auto-update** — app and launcher update themselves silently on launch

---

## Installation

1. Create a permanent folder for the app, e.g. `C:\BallchasingUploader`
2. Download `start.bat` into that folder
3. Run `start.bat` — it installs dependencies and downloads everything automatically
4. On first launch, enter your **Ballchasing API key** and select your **demos folder**

> **Do not run from Downloads or Desktop.** The app creates files alongside `start.bat`.

### Getting your API key
Go to [ballchasing.com](https://ballchasing.com) → Profile → API key

---

## Usage

| Action | How |
|---|---|
| Start auto-uploading | Click **Start Watching** |
| Upload a specific replay | Click the **⬆ Upload** button on any red-bordered card |
| View stats | Click any replay card → detail view |
| Bulk download from Ballchasing | Settings → Actions → Download Replays |
| Full duplicate scan | Settings → Actions → Full Duplicate Scan |

### Card border colours
| Colour | Meaning |
|---|---|
| None | Uploaded to Ballchasing |
| **Orange** | Ballchasing scan in progress |
| **Red** | Not on Ballchasing |

---

## Settings

| Setting | Description |
|---|---|
| API Key | Your Ballchasing API key |
| Main Demos Folder | Where Rocket League saves replays |
| Secondary Demos Folder | Optional mirror folder (replays moved to main) |
| Visibility | Public / Unlisted / Private |
| Auto Upload | Upload new replays automatically when detected |
| Start with Windows | Launch the app when Windows starts |
| Desktop Shortcut | Create/remove the desktop shortcut |
| Launch with Rocket League | Start watching when RL is running |
| Auto-fetch Stats | Fetch Ballchasing stats for new replays |
| Low Priority Mode | Background CPU yielding for low-end PCs |
| Replay Folder Limit | Cap folder size (deletes oldest, minimum 50) |

---

## Architecture

```
BallchasingUploader.exe     ← frozen Python launcher (PyInstaller)
    └── launcher.py         ← handles auth, version check, downloads main.pyw
        └── src/main.pyw    ← the actual application (auto-updated from server)

src/
├── main.pyw                ← application UI and logic
├── config.json             ← user settings + internal state
├── uploaded.json           ← set of uploaded replay filenames
├── upload_ids.json         ← filename → ballchasing replay UUID
├── rattletrap.exe          ← replay parser (downloaded on first run)
├── logo.ico
└── cache/
    ├── {filename}.json     ← parsed replay metadata
    └── bc_{uuid}.json      ← cached Ballchasing stats
```

### Update flow
- **main.pyw** — checked on every launch, auto-downloaded if newer version available
- **BallchasingUploader.exe** — update button appears in UI when a new version is available

---

## Building the exe

Requires Python 3.12 and PyInstaller. Run `admin/build_exe.bat` from the project root.

> ⚠️ Never use `ctypes.windll.*` in launcher.py code that runs at startup — it causes a libffi crash in frozen executables. Use `Path.home()` or subprocess alternatives instead.

To release a new exe version:
1. Bump `LAUNCHER_VERSION` in `launcher.py`
2. Run `admin\build_exe.bat`
3. Test the new exe locally on a non-C: drive
4. `python admin/deploy_server.py --launcher-only`
5. `scp BallchasingUploader.exe root@server:/opt/app-server/BallchasingUploader.exe`

---

## Development

The server-side code lives in `admin/server/app_server.py` and runs on the VPS.  
Deploy changes with `python admin/deploy_server.py --code`.

See `admin/CODE_REVIEW.txt` for a full architectural overview.
