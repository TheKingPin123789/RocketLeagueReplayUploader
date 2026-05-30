import sys
import os
import json
import time
import queue
import shutil
import unicodedata
import hashlib
import winreg
import calendar as _cal
import threading
import subprocess
import webbrowser
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk, colorchooser
from collections import deque
from datetime import datetime, date as _date, timedelta, timezone
from pathlib import Path

# bundled dependencies (installed by Setup.bat into src\lib)
_LIB_DIR = Path(__file__).parent / "lib"
if _LIB_DIR.is_dir():
    sys.path.insert(0, str(_LIB_DIR))

# In frozen PyInstaller exes, darkdetect's module-level ctypes argtypes setup
# crashes libffi-8.dll (STATUS_STACK_BUFFER_OVERRUN).  Mock it out before
# customtkinter has a chance to import it.  The app uses the config theme
# setting anyway so auto-detection is not needed.
if getattr(sys, 'frozen', False):
    import types as _types
    _dd = _types.ModuleType('darkdetect')
    _dd.theme    = lambda: 'Dark'
    _dd.isDark   = lambda: True
    _dd.isLight  = lambda: False
    _dd.listener = lambda cb: None
    sys.modules['darkdetect'] = _dd
    del _types, _dd

import customtkinter as ctk
import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── single-instance guard ─────────────────────────────────────────────────────
# Use a socket lock — avoids ctypes/libffi which crashes in frozen Python 3.14
import socket as _socket
_lock_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
try:
    _lock_sock.bind(('127.0.0.1', 47892))
except OSError:
    sys.exit(0)   # port already bound = another instance running



BASE_DIR      = Path(__file__).parent
CONFIG_FILE   = BASE_DIR / "config.json"
UPLOADED_FILE = BASE_DIR / "uploaded.json"
UPLOAD_URL    = "https://ballchasing.com/api/v2/upload"
CACHE_DIR     = BASE_DIR / "cache"
RATTLETRAP    = BASE_DIR / "rattletrap.exe"
VERSION          = "1.0"
APP_SERVER       = "http://46.101.184.78:8766"

def _atomic_write_json(path: Path, data) -> None:
    """Write JSON to a temp file then atomically rename — safe against mid-write crashes."""
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass
        raise

def load_config() -> dict:
    defaults = {"api_key": "", "demos_folder": "", "visibility": "unlisted",
                "auto_upload": False, "upload_on_detect": True, "launch_with_rl": True,
                "auto_fetch_bc": True, "theme": "dark", "desktop_shortcut": True}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return {**defaults, **json.load(f)}
        except Exception:
            # Corrupted config — back it up and return defaults
            try:
                CONFIG_FILE.rename(CONFIG_FILE.with_suffix(".bak"))
            except Exception:
                pass
    return defaults

def save_config(cfg: dict) -> None:
    _atomic_write_json(CONFIG_FILE, cfg)

ctk.set_appearance_mode(load_config().get("theme", "dark"))
ctk.set_default_color_theme("blue")

CACHE_VERSION  = 7      # bump to invalidate all header caches
RENDER_BUFFER    = 800   # px above/below viewport to pre-render (hides load pop-in while scrolling)
CARD_MARGIN_X    = 10    # left/right margin
SCORE_W          = 52    # left score column width
COMPACT_MIN_W    = 300   # minimum compact card width — 3-per-row only when window fits 3×this

_COMPACT = False   # toggled by the compact button; persisted in config

_BELOW_NORMAL_PRIORITY = 0x00004000   # Windows BELOW_NORMAL_PRIORITY_CLASS
_NORMAL_PRIORITY       = 0x00000020   # Windows NORMAL_PRIORITY_CLASS
_IDLE_PRIORITY         = 0x00000040   # Windows IDLE_PRIORITY_CLASS (only runs when CPU is free)
_low_priority_mode     = False        # updated by App when setting changes

def _set_process_priority(low: bool) -> None:
    """Set the whole process to below-normal or normal CPU priority."""
    global _low_priority_mode
    _low_priority_mode = low
    try:
        import ctypes as _ct
        h = _ct.windll.kernel32.GetCurrentProcess()
        _ct.windll.kernel32.SetPriorityClass(h, _BELOW_NORMAL_PRIORITY if low else _NORMAL_PRIORITY)
    except Exception:
        pass

def _dims():
    if _COMPACT:
        return dict(card_pad=3, name_h=20, tag_h=15, meta_h=14, meta2_h=14, player_h=15, footer_h=2,
                    score_font=13, name_font=11, meta_font=9, tag_font=9, player_font=10)
    return     dict(card_pad=6, name_h=34, tag_h=0,  meta_h=22, meta2_h=0,  player_h=18, footer_h=6,
                    score_font=20, name_font=12, meta_font=9, tag_font=9, player_font=10)

_font_cache:    dict = {}
_text_px_cache: dict = {}   # (text, family, size, weight) → pixel width

def _measure_font(family: str, size: int, weight: str = "normal") -> tkfont.Font:
    key = (family, size, weight)
    if key not in _font_cache:
        _font_cache[key] = tkfont.Font(family=family, size=size, weight=weight)
    return _font_cache[key]

def _measure_px(text: str, family: str, size: int, weight: str = "normal") -> int:
    """Cached text width measurement — avoids repeated Tcl roundtrips on resize."""
    key = (text, family, size, weight)
    v = _text_px_cache.get(key)
    if v is None:
        _text_px_cache[key] = v = _measure_font(family, size, weight).measure(text)
    return v

def _fit_text(text: str, family: str, size: int, weight: str, max_px: int) -> str:
    if max_px <= 0:
        return ""
    font = _measure_font(family, size, weight)
    if font.measure(text) <= max_px:
        return text
    while text:
        text = text[:-1]
        if font.measure(text + "…") <= max_px:
            return text + "…"
    return ""

# All colours defined as (light, dark) tuples.
# CTk widgets accept tuples directly; use _c() for canvas/tk drawing.
C_BG          = ("#E4E4E4", "#272727")
C_CARD        = ("#EFEFEF", "#1D1D1F")
C_BORDER      = ("#E0E0E5", "#2C2C2E")
C_BORDER_FAIL   = ("#F5010A", "#F5010A")
C_CARD_FAIL     = ("#FF8888", "#3E0000")
C_BORDER_NO_BC  = ("#CC3300", "#CC3300")   # replay has no Ballchasing ID
C_DIVIDER     = ("#CDCDD8", "#525256")
C_SCORE_COL   = ("#E9E9E9", "#242426")
C_NAME        = ("#000000", "#FFFFFF")
C_BLUE        = ("#0962BB", "#5aabf5")
C_ORANGE      = ("#CA0000", "#F5A623")
C_SCORE       = ("#000000", "#ffffff")
C_DIM         = ("#6C6C6C", "#888888")
C_DATE        = ("#8e8e93", "#8e8e93")
C_CHECK       = ("#00C60F", "#34C759")

def _c(t: tuple) -> str:
    """Return the light or dark colour string based on current appearance mode."""
    return t[0 if ctk.get_appearance_mode() == "Light" else 1]

# Immutable record of the original defaults — used by "Reset to Defaults"
_DEFAULT_COLORS: dict[str, tuple] = {
    "C_BG":          ("#E4E4E4", "#272727"),
    "C_CARD":        ("#EFEFEF", "#1D1D1F"),
    "C_BORDER":      ("#E0E0E5", "#2C2C2E"),
    "C_BORDER_FAIL":  ("#F5010A", "#F5010A"),
    "C_CARD_FAIL":    ("#FF8888", "#3E0000"),
    "C_BORDER_NO_BC": ("#CC3300", "#CC3300"),
    "C_DIVIDER":     ("#CDCDD8", "#525256"),
    "C_SCORE_COL":   ("#E9E9E9", "#242426"),
    "C_NAME":        ("#000000", "#FFFFFF"),
    "C_BLUE":        ("#0962BB", "#5aabf5"),
    "C_ORANGE":      ("#CA0000", "#F5A623"),
    "C_SCORE":       ("#000000", "#ffffff"),
    "C_DIM":         ("#6C6C6C", "#888888"),
    "C_DATE":        ("#8e8e93", "#8e8e93"),
    "C_CHECK":       ("#00C60F", "#34C759"),
}

def _contrasting(hex_color: str) -> str:
    """Return black or white — whichever has better contrast with hex_color."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return "#000000" if (0.299*r + 0.587*g + 0.114*b) / 255 > 0.5 else "#ffffff"

def _load_color_overrides():
    """Replace default C_* tuples with any values saved in config."""
    overrides = load_config().get("custom_colors", {})
    g = globals()
    for name, val in overrides.items():
        if name in g and isinstance(g[name], tuple) and len(val) == 2:
            g[name] = (val[0], val[1])

_load_color_overrides()

def _card_colors() -> dict:
    return {
        "bg":          _c(C_BG),
        "card":        _c(C_CARD),
        "border":      _c(C_BORDER),
        "border_fail":   _c(C_BORDER_FAIL),
        "card_fail":     _c(C_CARD_FAIL),
        "border_no_bc":  _c(C_BORDER_NO_BC),
        "divider":     _c(C_DIVIDER),
        "score_col":   _c(C_SCORE_COL),
        "name":        _c(C_NAME),
        "dim":         _c(C_DIM),
        "date":        _c(C_DATE),
        "blue":        _c(C_BLUE),
        "orange":      _c(C_ORANGE),
    }

RANKED_PLAYLISTS = frozenset({10, 11, 12, 13, 27, 28, 29, 30, 34})
CASUAL_PLAYLISTS = frozenset({1, 2, 3, 4, 6, 7, 31})

def replay_type(info: dict) -> str:
    """Return 'Ranked', 'Casual', 'Private', 'Tournament', or 'Online'."""
    mt  = (info.get("match_type") or "").lower()
    pid = info.get("playlist_id") or 0
    rn  = (info.get("replay_name") or "").lower()
    mtc = (info.get("match_type_class") or "").lower()
    if mt == "private"   or "private"    in mtc: return "Private"
    if mt == "tournament"or "tournament" in mtc: return "Tournament"
    if "ranked"  in mtc:                         return "Ranked"
    if "casual"  in mtc:                         return "Casual"
    if pid in RANKED_PLAYLISTS:                  return "Ranked"
    if pid in CASUAL_PLAYLISTS:                  return "Casual"
    if "ranked" in rn:                           return "Ranked"
    if "casual" in rn:                           return "Casual"
    return "Online"


# ── persistence ───────────────────────────────────────────────────────────────

def load_uploaded() -> set:
    if UPLOADED_FILE.exists():
        try:
            with open(UPLOADED_FILE, encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            try:
                UPLOADED_FILE.rename(UPLOADED_FILE.with_suffix(".bak"))
            except Exception:
                pass
    return set()

def save_uploaded(uploaded: set) -> None:
    _atomic_write_json(UPLOADED_FILE, sorted(uploaded))

UPLOAD_IDS_FILE  = BASE_DIR / "upload_ids.json"
_upload_ids_lock = __import__("threading").Lock()

def load_upload_ids() -> dict:
    if UPLOAD_IDS_FILE.exists():
        try:
            with open(UPLOAD_IDS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_upload_id(filename: str, bc_id: str) -> None:
    with _upload_ids_lock:
        ids = load_upload_ids()
        ids[filename] = bc_id
        _atomic_write_json(UPLOAD_IDS_FILE, ids)


def build_upload_id_index(api_key: str, demos_folder: str = "", log_fn=None) -> int:
    """Fetch the user's replay list from Ballchasing and populate upload_ids.json
    with any entries that match local replay files.  Returns number of new IDs saved.
    Does NOT upload any files — read-only API calls only."""
    if not api_key:
        return 0
    try:
        local_ids = load_upload_ids()
        known_bc_ids = set(local_ids.values())

        # Build rl_id → filename from cache files
        rl_to_file: dict[str, str] = {}
        for cf in CACHE_DIR.glob("*.json"):
            if cf.name.startswith("bc_"):
                continue
            try:
                data = json.loads(cf.read_text(encoding="utf-8"))
                rl_id = (data.get("rl_id") or "").strip()
                if rl_id:
                    # cf.stem for "Name.replay.json" is already "Name.replay"
                    rl_to_file[_norm_rl_id(rl_id)] = cf.stem
            except Exception:
                pass

        # Also map directly from replay filenames — RL names files after their ID
        if demos_folder:
            for rp in Path(demos_folder).glob("*.replay"):
                norm = _norm_rl_id(rp.stem)
                if norm not in rl_to_file:
                    rl_to_file[norm] = rp.name

        # Remove entries that already have a bc_id — nothing to do for them
        rl_to_file = {k: v for k, v in rl_to_file.items() if v not in local_ids}

        local_count = len(rl_to_file)
        if local_count == 0:
            return 0

        # Find the oldest local replay date to use as a scan cutoff.
        # BC returns replays sorted newest-first, so once we pass this date
        # (with a 3-day buffer) the remaining replays can't be here.
        # Always check both cache dates AND file mtimes — on a fresh install the
        # cache only covers recently parsed replays, so cache alone gives a
        # cutoff that's far too recent and stops the scan too early.
        oldest_dt: datetime | None = None
        for cf in CACHE_DIR.glob("*.json"):
            if cf.name.startswith("bc_"):
                continue
            try:
                d = json.loads(cf.read_text(encoding="utf-8")).get("date", "")
                if d:
                    dt = datetime.strptime(d[:19], "%Y-%m-%d %H:%M:%S")
                    if oldest_dt is None or dt < oldest_dt:
                        oldest_dt = dt
            except Exception:
                pass
        # Always check file mtimes too — whichever is older wins
        if demos_folder:
            for rp in Path(demos_folder).glob("*.replay"):
                try:
                    dt = datetime.fromtimestamp(rp.stat().st_mtime)
                    if oldest_dt is None or dt < oldest_dt:
                        oldest_dt = dt
                except Exception:
                    pass
        cutoff_dt = (oldest_dt - timedelta(days=3)) if oldest_dt else None

        if log_fn:
            cutoff_str = cutoff_dt.strftime("%Y-%m-%d") if cutoff_dt else "none"
            log_fn(f"[index] {local_count} local replay(s) without bc_id — "
                   f"scanning Ballchasing (cutoff {cutoff_str})…")

        url = "https://ballchasing.com/api/replays"
        params = {"uploader": "me", "count": 200,
                  "sort-by": "replay-date", "sort-dir": "desc"}
        saved = 0
        scanned = 0
        page = 0
        pages_since_last_match = 0
        MAX_EMPTY_PAGES = 10   # stop after 10 consecutive pages (2 000 replays) with no new matches

        while url:
            try:
                r = requests.get(url, headers={"Authorization": api_key},
                                 params=params if page == 0 else None, timeout=20)
                params = None
                page += 1
                if r.status_code != 200:
                    if log_fn: log_fn(f"[index] API error {r.status_code}", "red")
                    break
                data = r.json()
            except Exception as e:
                if log_fn: log_fn(f"[index] error: {e}", "red")
                break

            page_list = data.get("list", [])
            if not page_list:
                break
            scanned += len(page_list)
            matched_this_page = 0
            for replay in page_list:
                bc_id = replay.get("id", "")
                if not bc_id or bc_id in known_bc_ids:
                    continue
                norm = _norm_rl_id(replay.get("rocket_league_id") or "")
                filename = rl_to_file.get(norm, "")
                if filename and filename not in local_ids:
                    local_ids[filename] = bc_id
                    known_bc_ids.add(bc_id)
                    saved += 1
                    matched_this_page += 1

            if matched_this_page:
                pages_since_last_match = 0
            else:
                pages_since_last_match += 1

            # Stop early once every local replay has been matched
            if saved >= local_count:
                break

            # Stop if many consecutive pages have no matches — remaining replays
            # are likely not on Ballchasing
            if pages_since_last_match >= MAX_EMPTY_PAGES:
                remaining = local_count - saved
                if log_fn:
                    log_fn(f"[index] {remaining} replay(s) not found on Ballchasing after "
                           f"{scanned} scanned — stopping.")
                break

            # Stop when BC replays go older than the oldest local replay minus 3 days
            if cutoff_dt and page_list:
                last_date_str = page_list[-1].get("date", "")
                if last_date_str:
                    try:
                        last_dt = datetime.fromisoformat(
                            last_date_str.replace("Z", "+00:00")
                        ).replace(tzinfo=None)
                        if last_dt < cutoff_dt:
                            if log_fn:
                                log_fn(f"[index] Reached date cutoff "
                                       f"({cutoff_dt.strftime('%Y-%m-%d')}), stopping.")
                            break
                    except Exception:
                        pass

            url = data.get("next", "")

        # Write all matches in a single atomic operation at the end
        if saved:
            with _upload_ids_lock:
                _atomic_write_json(UPLOAD_IDS_FILE, local_ids)

        if log_fn:
            log_fn(f"[index] Done — scanned {scanned} BC replays, matched {saved} / {local_count} local.")
        return saved
    except Exception as e:
        if log_fn: log_fn(f"[index] failed: {e}", "red")
        return 0


def fetch_bc_stats(bc_id: str, api_key: str) -> dict | None:
    """Fetch full replay stats from Ballchasing API and cache locally."""
    if not bc_id or not api_key:
        return None
    cache_file = CACHE_DIR / f"bc_{bc_id}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            # Don't use cache if it was saved while still pending (no player stats)
            if data.get("status") != "ok" or not (
                data.get("blue", {}).get("players") or
                data.get("orange", {}).get("players")
            ):
                cache_file.unlink(missing_ok=True)
            else:
                return data
        except Exception:
            pass
    try:
        r = requests.get(
            f"https://ballchasing.com/api/replays/{bc_id}",
            headers={"Authorization": api_key},
            timeout=30)
        if r.status_code != 200:
            return None
        data = r.json()
        # Only cache if stats are ready
        if data.get("status") == "ok" and (
            data.get("blue", {}).get("players") or
            data.get("orange", {}).get("players")
        ):
            CACHE_DIR.mkdir(exist_ok=True)
            cache_file.write_text(json.dumps(data), encoding="utf-8")
        else:
            return None   # still processing — don't cache, caller should retry
        return data
    except Exception:
        return None


def load_bc_stats(bc_id: str) -> dict | None:
    """Load cached Ballchasing API stats for a replay, None if not available."""
    if not bc_id:
        return None
    cache_file = CACHE_DIR / f"bc_{bc_id}.json"
    if not cache_file.exists():
        return None
    try:
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _norm_rl_id(s: str) -> str:
    """Normalise a Rocket League ID: uppercase, strip dashes and braces."""
    return s.upper().replace("-", "").replace("{", "").replace("}", "")

def _to_uuid(s: str) -> str:
    """Convert a 32-char hex ID to UUID format (8-4-4-4-12) if needed."""
    h = _norm_rl_id(s)
    if len(h) == 32:
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    return s  # already has dashes or wrong length — return as-is

def lookup_bc_id(rl_id: str, api_key: str, _log=None) -> str:
    """Find a replay's Ballchasing ID by its Rocket League internal ID.
    Tries uploader=me first, then falls back to any uploader."""
    if not rl_id or not api_key:
        if _log: _log(f"[lookup] skipped — rl_id={repr(rl_id)} api_key={'set' if api_key else 'missing'}", "red")
        return ""

    norm = _norm_rl_id(rl_id)

    def _search(query_id: str, extra: str) -> str:
        try:
            url = f"https://ballchasing.com/api/replays?rocket-league-id={query_id}&count=1{extra}"
            r = requests.get(url, headers={"Authorization": api_key}, timeout=15)
            if _log: _log(f"[lookup] GET {url} → {r.status_code}")
            if r.status_code != 200:
                return ""
            lst = r.json().get("list", [])
            if not lst:
                return ""
            hit = lst[0]
            returned = _norm_rl_id(hit.get("rocket_league_id") or "")
            if _log: _log(f"[lookup] hit id={hit.get('id')} rocket_league_id={hit.get('rocket_league_id')!r}")
            if returned and returned != norm:
                if _log: _log(f"[lookup] id mismatch: {returned!r} != {norm!r}", "red")
                return ""
            return hit["id"]
        except Exception as e:
            if _log: _log(f"[lookup] exception: {e}", "red")
            return ""

    uuid_fmt = _to_uuid(rl_id)
    # Ballchasing requires UUID format with dashes — try that first, then fallbacks
    for qid in dict.fromkeys([uuid_fmt, rl_id, norm]):
        result = _search(qid, "&uploader=me") or _search(qid, "")
        if result:
            return result
    return ""


MAP_NAMES: dict[str, str] = {
    # ── DFH Stadium ───────────────────────────────────────────────────────────
    "stadium_p":                        "DFH Stadium",
    "stadium_day_p":                    "DFH Stadium (Day)",
    "stadium_foggy_p":                  "DFH Stadium (Stormy)",
    "stadium_race_day_p":               "DFH Stadium (Throwback)",
    "stadium_winter_p":                 "DFH Stadium (Snowy)",
    "stadium_10a_p":                    "DFH Stadium (Anniversary)",
    "stadium_hns_p":                    "DFH Stadium (Halloween)",
    "stadium_circuit_p":                "DFH Stadium (Circuit)",
    # ── Mannfield ─────────────────────────────────────────────────────────────
    "eurostadium_p":                    "Mannfield",
    "eurostadium_night_p":              "Mannfield (Night)",
    "eurostadium_dusk_p":               "Mannfield (Dusk)",
    "eurostadium_snowy_p":              "Mannfield (Snowy)",
    "eurostadium_rainy_p":              "Mannfield (Stormy)",
    "eurostadium_stormy_p":             "Mannfield (Stormy)",
    "eurostadium_snownight_p":          "Mannfield (Frosty)",
    "eurostadium_halloween_p":          "Mannfield (Halloween)",
    # ── Beckwith Park ─────────────────────────────────────────────────────────
    "park_p":                           "Beckwith Park",
    "park_rainy_p":                     "Beckwith Park (Stormy)",
    "park_night_p":                     "Beckwith Park (Midnight)",
    "park_snowy_p":                     "Beckwith Park (Snowy)",
    "park_bman_p":                      "Beckwith Park (Gotham Night)",
    "park_bman_night_p":                "Beckwith Park (Gotham Night)",
    # ── Urban Central ─────────────────────────────────────────────────────────
    "trainstation_p":                   "Urban Central",
    "trainstation_night_p":             "Urban Central (Night)",
    "trainstation_dawn_p":              "Urban Central (Dawn)",
    "trainstation_hw_p":                "Urban Central (Haunted)",
    "trainstation_spooky_p":            "Urban Central (Spooky)",
    # ── Champions Field ───────────────────────────────────────────────────────
    "cs_p":                             "Champions Field",
    "cs_day_p":                         "Champions Field (Day)",
    "cs_hw_p":                          "Champions Field (Halloween)",
    "cs_nfl_p":                         "Champions Field (NFL)",
    "cs_nikefc_p":                      "Champions Field (Nike FC)",
    # ── Utopia Coliseum ───────────────────────────────────────────────────────
    "utopiastadium_p":                  "Utopia Coliseum",
    "utopiastadium_dusk_p":             "Utopia Coliseum (Dusk)",
    "utopiastadium_snow_p":             "Utopia Coliseum (Snowy)",
    "utopiastadium_lux_p":              "Utopia Coliseum (Gilded)",
    "utopiastadium_retro_p":            "Utopia Retro",
    # ── Neo Tokyo ─────────────────────────────────────────────────────────────
    "neotokyo_p":                       "Neo Tokyo",
    "neotokyo_standard_p":              "Neo Tokyo (Standard)",
    "neotokyo_arcade_p":                "Neo Tokyo (Arcade)",
    "neotokyo_comic_p":                 "Neo Tokyo (Comic)",
    "neotokyo_hacked_p":                "Neo Tokyo (Hacked)",
    # ── AquaDome ──────────────────────────────────────────────────────────────
    "underwater_p":                     "AquaDome",
    "underwater_grs_p":                 "AquaDome (Salty Shallows)",
    # ── Salty Shores ──────────────────────────────────────────────────────────
    "beach_p":                          "Salty Shores",
    "beach_night_p":                    "Salty Shores (Night)",
    "beach_lgbt_p":                     "Salty Shores (Salty Fest)",
    "beach_volley_p":                   "Salty Shores (Volley)",
    # ── Wasteland ─────────────────────────────────────────────────────────────
    "wasteland_p":                      "Wasteland",
    "wasteland_night_p":                "Wasteland (Night)",
    "wasteland_grs_p":                  "Wasteland (Pitched)",
    "wasteland_s_p":                    "Wasteland (Standard)",
    "wasteland_s_night_p":              "Wasteland (Standard, Night)",
    # ── Starbase ARC ──────────────────────────────────────────────────────────
    "arc_p":                            "Starbase ARC",
    "arc_daydream_p":                   "Starbase ARC (Aftermath)",
    "arc_standard_p":                   "Starbase ARC (Standard)",
    # ── Farmstead ─────────────────────────────────────────────────────────────
    "woods_p":                          "Farmstead",
    "woods_night_p":                    "Farmstead (Night)",
    "woods_winter_p":                   "Farmstead (Snowy)",
    "woods_day_p":                      "Farmstead (Pitched)",
    "woods_hw_p":                       "Farmstead (Spooky)",
    "woods_strange_p":                  "Farmstead (The Upside Down)",
    "farm_grs_p":                       "Farmstead (Grasslands)",
    # ── Forbidden Temple ──────────────────────────────────────────────────────
    "chn_stadium_p":                    "Forbidden Temple",
    "chn_stadium_day_p":                "Forbidden Temple (Day)",
    "chn_stadium_fireice_p":            "Forbidden Temple (Fire & Ice)",
    # ── Neon Fields ───────────────────────────────────────────────────────────
    "street_p":                         "Neon Fields",
    "street_night_p":                   "Neon Fields (Night)",
    # ── Deadeye Canyon ────────────────────────────────────────────────────────
    "outlaw_oasis_p":                   "Deadeye Canyon",
    "outlaw_oasis_night_p":             "Deadeye Canyon (Oasis)",
    # ── Sovereign Heights ─────────────────────────────────────────────────────
    "uf_p":                             "Sovereign Heights",
    "uf_day_p":                         "Sovereign Heights (Dusk)",
    "uf_dusk_p":                        "Sovereign Heights (Dusk)",
    # ── Paname ────────────────────────────────────────────────────────────────
    "paname_p":                         "Paname",
    "paname_dusk_p":                    "Paname (Dusk)",
    "paname_night_p":                   "Paname (Night)",
    # ── The Block ─────────────────────────────────────────────────────────────
    "mall_p":                           "The Block",
    "mall_day_p":                       "The Block (Dusk)",
    "mall_night_p":                     "The Block (Night)",
    # ── Estadio Vida ──────────────────────────────────────────────────────────
    "estadio_p":                        "Estadio Vida",
    "estadio_dusk_p":                   "Estadio Vida (Dusk)",
    # ── Drift Woods ───────────────────────────────────────────────────────────
    "drift_p":                          "Drift Woods",
    "drift_night_p":                    "Drift Woods (Night)",
    # ── Galleon ───────────────────────────────────────────────────────────────
    "galleon_p":                        "Galleon",
    "galleon_retro_p":                  "Galleon Retro",
    # ── Calavera ──────────────────────────────────────────────────────────────
    "calavera_p":                       "Calavera",
    # ── Rivals Arena ──────────────────────────────────────────────────────────
    "rivals_p":                         "Rivals Arena",
    # ── Throwback Stadium ─────────────────────────────────────────────────────
    "throwbackstadium_p":               "Throwback Stadium",
    "throwbackstadium_winter_p":        "Throwback Stadium (Snowy)",
    # ── Extra modes ───────────────────────────────────────────────────────────
    "hoopsstadium_p":                   "Dunk House",
    "bb_p":                             "Pillars",
    "underpass_p":                      "Underpass",
    "mcdm_p":                           "Octagon",
    "quadron_p":                        "Quadron",
    "corridor_p":                       "Corridor",
    "loophole_p":                       "Loophole",
    "hourglass_p":                      "Hourglass",
    "carbon_p":                         "Carbon",
    "colossus_p":                       "Colossus",
    "barricade_p":                      "Barricade",
    "core707_p":                        "Core 707",
    "basin_p":                          "Basin",
    "cosmic_p":                         "Cosmic",
    "doublegoal_p":                     "Double Goal",
    "doublegoal2_p":                    "Double Goal",
}

def map_display_name(raw: str) -> str:
    """Return a human-readable arena name from the internal map code."""
    if not raw:
        return ""
    looked_up = MAP_NAMES.get(raw.lower())
    if looked_up:
        return looked_up
    # Fallback: strip trailing _P, replace underscores, title-case
    clean = raw
    if clean.upper().endswith("_P"):
        clean = clean[:-2]
    return clean.replace("_", " ").title()

_PLATFORM_LABEL: dict[str, str] = {
    "onlineplatform_steam":   "Steam",
    "onlineplatform_epic":    "Epic",
    "onlineplatform_ps4":     "PS",
    "onlineplatform_ps5":     "PS",
    "onlineplatform_dingo":   "XB",
    "onlineplatform_switch":  "SW",
    "onlineplatform_nnx":     "SW",
}
_PLATFORM_COLOR_DARK: dict[str, str] = {
    "Steam": "#ffffff",
    "Epic":  "#888888",
    "PS":    "#6b8fd0",
    "XB":    "#6aab6a",
    "SW":    "#e06060",
}
_PLATFORM_COLOR_LIGHT: dict[str, str] = {
    "Steam": "#444444",
    "Epic":  "#666666",
    "PS":    "#4a6fba",
    "XB":    "#3a8a3a",
    "SW":    "#cc3030",
}

def _platform_color(plat: str, fallback: str) -> str:
    if ctk.get_appearance_mode() == "Dark":
        return _PLATFORM_COLOR_DARK.get(plat, fallback)
    return _PLATFORM_COLOR_LIGHT.get(plat, fallback)

def platform_label(raw: str) -> str:
    return _PLATFORM_LABEL.get(raw.lower(), "")

_TRACKER_PLATFORM: dict[str, str] = {
    "onlineplatform_steam":  "steam",
    "onlineplatform_epic":   "epic",
    "onlineplatform_ps4":    "psn",
    "onlineplatform_ps5":    "psn",
    "onlineplatform_dingo":  "xbl",
    "onlineplatform_switch": "nintendo-switch",
    "onlineplatform_nnx":    "nintendo-switch",
}

def tracker_url(player: dict) -> str:
    plat = _TRACKER_PLATFORM.get((player.get("raw_platform") or "").lower()) or "epic"
    if plat == "steam":
        uid = player.get("online_id") or ""
    else:
        uid = player.get("name", "")
    if not uid or uid == "0":
        return ""
    return f"https://rocketleague.tracker.network/rocket-league/profile/{plat}/{uid}/overview"

def fmt_date(ts: float) -> str:
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d  %H:%M")

def replay_date_ts(date_str: str) -> float:
    """Convert a replay date string to a UTC unix timestamp for sort comparison.
    Handles colon-time ("2026-05-22 17:39:46"), dash-time ("2026-05-22 17-39-46"),
    ISO ("2026-05-22T17:39:46Z"), and strings without seconds ("2026-05-22 17-39").
    Returns 0.0 if the string is empty or unparseable."""
    if not date_str:
        return 0.0
    s = date_str.strip()[:19]          # trim to "YYYY-MM-DD HH:MM:SS" max
    s = s.replace("T", " ").rstrip("Z").strip()
    # normalise time separators to dashes ("17:39:46" → "17-39-46")
    date_part = s[:10]
    time_part = s[11:].replace(":", "-") if len(s) > 10 else ""
    for fmt in ("%Y-%m-%d %H-%M-%S", "%Y-%m-%d %H-%M"):
        try:
            dt = datetime.strptime(f"{date_part} {time_part}", fmt)
            return float(_cal.timegm(dt.timetuple()))
        except ValueError:
            continue
    return 0.0

def load_cached(name: str) -> dict | None:
    cp = CACHE_DIR / (name + ".json")
    if cp.exists():
        try:
            with open(cp, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None

def save_cache(name: str, info: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    _atomic_write_json(CACHE_DIR / (name + ".json"), {**info, "_v": CACHE_VERSION})

# ── rrrocket parsers ──────────────────────────────────────────────────────────

def _extract_playlist_id(props: dict, replay: dict) -> int:
    pid = int(props.get("PlaylistId") or props.get("Playlist") or 0)
    if pid:
        return pid
    frames  = (replay.get("network_frames") or {}).get("frames", [])
    objects = replay.get("objects", [])
    if not frames:
        return 0
    try:
        gri_idx = next((i for i, o in enumerate(objects)
                        if "ReplicatedGamePlaylist" in o), None)
        if gri_idx is not None:
            for frame in frames[:200]:
                for actor in frame.get("updated_actors", []):
                    if actor.get("object_id") == gri_idx:
                        v = actor.get("attribute", {})
                        p = v.get("Int") or v.get("int") or 0
                        if p:
                            return int(p)
    except Exception:
        pass
    return 0


def parse_card_data(path: Path) -> dict | None:
    """Run rrrocket via stdin and return the fields needed for card display."""
    if not RATTLETRAP.exists():
        return None
    try:
        data = Path(path).read_bytes()
        # In low priority mode use IDLE so rattletrap only runs when CPU is free
        _priority = _IDLE_PRIORITY if _low_priority_mode else 0
        proc = subprocess.run(
            [str(RATTLETRAP)],
            input=data, capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW | _priority,
        )
        if proc.returncode != 0:
            return None
        raw    = json.loads(proc.stdout)
        replay = raw[0] if isinstance(raw, list) else raw
        props  = replay.get("properties", {})

        players = []
        for entry in (props.get("PlayerStats") or []):
            if not isinstance(entry, dict): continue
            pname = entry.get("Name", "")
            if not pname: continue
            t        = entry.get("Team")
            raw_plat = entry.get("Platform") or {}
            if isinstance(raw_plat, dict):
                raw_plat = raw_plat.get("value", "")
            raw_plat   = str(raw_plat)
            plat_lower = raw_plat.lower()
            if "epic" in plat_lower:
                # EpicAccountId is nested at PlayerID.fields.EpicAccountId
                pid_fields = (entry.get("PlayerID") or {}).get("fields") or {}
                online_id = str(pid_fields.get("EpicAccountId") or "")
            elif "steam" in plat_lower:
                uid = entry.get("OnlineID") or entry.get("Uid") or 0
                online_id = str(uid) if uid else ""
            else:
                # PS4/Xbox/Other: OnlineID may be present but PS4 value is unreliable
                uid = entry.get("OnlineID") or entry.get("Uid") or 0
                online_id = str(uid) if uid else ""
            players.append({
                "name":         pname,
                "team":         int(t) if t is not None else -1,
                "platform":     platform_label(raw_plat),
                "raw_platform": raw_plat,
                "online_id":    online_id,
                "score":        int(entry.get("Score",   0) or 0),
                "goals":        int(entry.get("Goals",   0) or 0),
                "assists":      int(entry.get("Assists", 0) or 0),
                "saves":        int(entry.get("Saves",   0) or 0),
                "shots":        int(entry.get("Shots",   0) or 0),
            })

        # Fallback: extract players from Goals when PlayerStats is absent
        if not players:
            seen: set = set()
            for g in (props.get("Goals") or []):
                if not isinstance(g, dict): continue
                pname = g.get("PlayerName", "")
                team  = g.get("PlayerTeam")
                if pname and pname not in seen:
                    seen.add(pname)
                    players.append({
                        "name":         pname,
                        "team":         int(team) if team is not None else -1,
                        "platform":     "",
                        "raw_platform": "",
                        "online_id":    "",
                    })

        dur = props.get("TotalSecondsPlayed")
        wt  = props.get("WinningTeam")

        # Detect ranked/casual/private from MatchType class objects
        # (more reliable than PlaylistId which is often absent in newer replays)
        mtc = ""
        for obj in (replay.get("objects") or []):
            s = str(obj)
            if "MatchType_PublicRanked" in s:  mtc = "ranked";     break
            if "MatchType_PublicCasual" in s:  mtc = "casual";     break
            if "MatchType_Private"      in s:  mtc = "private";    break
            if "MatchType_Tournament"   in s:  mtc = "tournament"; break

        return {
            "team0":             int(props.get("Team0Score") or 0),
            "team1":             int(props.get("Team1Score") or 0),
            "date":              str(props.get("Date")       or ""),
            "replay_name":       str(props.get("ReplayName") or ""),
            "map":               str(props.get("MapName")    or ""),
            "match_type":        str(props.get("MatchType")  or ""),
            "match_type_class":  mtc,
            "players":           players,
            "duration":          float(dur) if dur is not None else None,
            "team_size":         int(props.get("TeamSize")   or 0),
            "playlist_id":       _extract_playlist_id(props, replay),
            "rl_id":             str(props.get("Id")         or ""),
            "winning_team":      int(wt) if wt is not None else -1,
            "forfeit":           bool(props.get("bForfeit") or False),
        }
    except Exception:
        return None


# ── uploader ──────────────────────────────────────────────────────────────────

def wait_for_write(path: Path, timeout: int = 30) -> None:
    prev, deadline = -1, time.time() + timeout
    while time.time() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            time.sleep(1); continue
        if size == prev: return
        prev = size
        time.sleep(1)

def upload(path: Path, config: dict, uploaded: set, on_status, force=False, on_bc_id=None) -> None:
    name = path.name
    if not force and name in uploaded:
        on_status(name, "skipped"); return
    on_status(name, "uploading")
    wait_for_write(path)
    for attempt in range(1, 4):
        try:
            with open(path, "rb") as f:
                resp = requests.post(UPLOAD_URL,
                    headers={"Authorization": config["api_key"]},
                    files={"file": (name, f, "application/octet-stream")},
                    data={"visibility": config.get("visibility", "unlisted")},
                    timeout=60)
            if resp.status_code in (201, 409):
                if on_bc_id:
                    try:
                        bc_id = resp.json().get("id", "")
                        if bc_id:
                            on_bc_id(bc_id)
                    except Exception:
                        pass
                on_status(name, "uploaded" if resp.status_code == 201 else "duplicate"); return
            on_status(name, f"error {resp.status_code}")
        except requests.RequestException:
            on_status(name, f"retry {attempt}/3")
        if attempt < 3: time.sleep(5 * attempt)
    on_status(name, "failed")


class ReplayHandler(FileSystemEventHandler):
    def __init__(self, on_new_replay):
        self.on_new_replay = on_new_replay

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".replay"):
            self.on_new_replay(Path(event.src_path).name)


# ── card helpers ──────────────────────────────────────────────────────────────

def card_height(info: dict | None) -> int:
    d = _dims()
    if not info:
        return d["name_h"] + d["tag_h"] + d["meta_h"] + d["meta2_h"] + d["footer_h"]
    players = info.get("players") or []
    if _COMPACT:
        blue   = len([p for p in players if p["team"] == 0])
        orange = len([p for p in players if p["team"] == 1])
        return d["name_h"] + d["tag_h"] + d["meta_h"] + d["meta2_h"] + (max(blue, 1) + max(orange, 1)) * d["player_h"] + d["footer_h"]
    rows = max(len([p for p in players if p["team"] == 0]),
               len([p for p in players if p["team"] == 1]), 1)
    return d["name_h"] + d["meta_h"] + rows * d["player_h"] + d["footer_h"]

def draw_card(canvas: tk.Canvas, y: int, w: int, entry: dict) -> None:
    d        = _dims()
    cc       = _card_colors()
    info     = entry["info"]
    uploaded = entry["uploaded"]
    mtime    = entry["mtime"]
    filename = entry["filename"]
    NAME_H = d["name_h"]; TAG_H = d["tag_h"]; META_H = d["meta_h"]; META2_H = d["meta2_h"]; PLAYER_H = d["player_h"]

    if _COMPACT:
        cols  = entry.get("grid_cols", 2)
        col   = entry.get("grid_col",  0)
        gap   = d["card_pad"]
        col_w = (w - 2 * CARD_MARGIN_X - (cols - 1) * gap) // cols
        x0    = CARD_MARGIN_X + col * (col_w + gap)
        x1    = x0 + col_w
    else:
        cols  = entry.get("grid_cols", 1)
        col   = entry.get("grid_col",  0)
        gap   = d["card_pad"]
        col_w = (w - 2 * CARD_MARGIN_X - (cols - 1) * gap) // cols
        col_w = min(col_w, entry.get("_max_w", col_w))
        x0    = CARD_MARGIN_X + col * (col_w + gap)
        x1    = x0 + col_w

    h  = entry["height"]
    cx = x0 + 8

    # ── card background + border ──────────────────────────────────────────────
    if entry.get("failed"):
        bg, bdr, bdr_w = cc["card_fail"], cc["border_fail"], 2
    elif not entry.get("bc_id") and not entry.get("parsing"):
        bg, bdr, bdr_w = cc["card"], cc["border_no_bc"], 2
    else:
        bg, bdr, bdr_w = cc["card"], cc["border"], 1
    canvas.create_rectangle(x0, y, x1, y+h, fill=bg, outline=bdr, width=bdr_w)

    if not _COMPACT:
        # ── score columns (normal mode): blue left, orange right ──────────────
        sx_l = x0 + SCORE_W
        sx_r = x1 - SCORE_W
        sx   = sx_l
        cx   = sx_l + 8
        canvas.create_rectangle(x0,   y, sx_l, y+h, fill=cc["score_col"], outline="")
        canvas.create_rectangle(sx_r, y, x1,   y+h, fill=cc["score_col"], outline="")
        canvas.create_line(sx_l, y, sx_l, y+h, fill=cc["divider"])
        canvas.create_line(sx_r, y, sx_r, y+h, fill=cc["divider"])
        if info:
            canvas.create_rectangle(x0,   y, x0+4, y+h, fill=cc["blue"],   outline="")
            canvas.create_rectangle(x1-4, y, x1,   y+h, fill=cc["orange"], outline="")
            score_cx_l = x0 + 4 + (SCORE_W - 4) // 2
            score_cx_r = sx_r + SCORE_W // 2
            _score_y   = y + h // 2
            canvas.create_text(score_cx_l, _score_y, text=str(info["team0"]),
                               anchor="center", fill=cc["blue"],
                               font=("Segoe UI", d["score_font"], "bold"))
            canvas.create_text(score_cx_r, _score_y, text=str(info["team1"]),
                               anchor="center", fill=cc["orange"],
                               font=("Segoe UI", d["score_font"], "bold"))
        else:
            canvas.create_text(x0 + SCORE_W // 2, y + h // 2, text="?",
                               anchor="center", fill=cc["dim"], font=("Segoe UI", 14))
    else:
        sx   = x0
        sx_r = x1  # compact: no right score column

    # ── name row ──────────────────────────────────────────────────────────────
    ny = y + NAME_H // 2
    rname = (info.get("replay_name") or "") if info else ""
    ts       = (info.get("team_size") or 0) if info else 0
    type_tag = (entry.get("type") or replay_type(info)) if info else ""
    mode_str = {1: "Duel", 2: "Doubles", 3: "Standard", 4: "Chaos"}.get(ts, f"{ts}v{ts}" if ts else "")
    if not rname:
        if info:
            date_part = (info.get("date") or "")[:10]
            rname = " ".join(p for p in [date_part, type_tag, mode_str] if p) or Path(filename).stem
        else:
            rname = Path(filename).stem
    tags     = "  ·  ".join(t for t in [type_tag, mode_str] if t)

    # Upload button is shown on cards that have no Ballchasing ID and are not parsing/failed
    _show_upl = not entry.get("bc_id") and not entry.get("parsing") and not entry.get("failed")
    _upl_w    = (58 if not _COMPACT else 20) if _show_upl else 0

    if TAG_H == 0:
        # normal mode: hide tags when upload button is visible (button takes that space)
        if _show_upl:
            name_max = sx_r - cx - _upl_w - 12
        else:
            tag_px   = (_measure_font("Segoe UI", d["tag_font"]).measure(tags) + 14) if tags else 0
            name_max = sx_r - cx - tag_px - 8
    else:
        # compact mode: tags on own row, name takes full width minus button
        name_max = x1 - cx - 6 - (_upl_w + 4 if _show_upl else 0)
    rname = _fit_text(rname, "Segoe UI", d["name_font"], "bold", name_max)

    canvas.create_text(cx, ny, text=rname, anchor="w",
                       fill=cc["name"], font=("Segoe UI", d["name_font"], "bold"))

    if TAG_H == 0 and not _show_upl:
        # normal mode: tags on right side of name row, just inside right score column
        if tags:
            canvas.create_text(sx_r-6, ny, text=tags, anchor="e",
                               fill=cc["date"], font=("Segoe UI", d["tag_font"]))

    if entry.get("parsing"):
        canvas.create_text(cx, y + NAME_H + TAG_H + META_H // 2,
                           text="⟳  parsing…", anchor="w",
                           fill=cc["date"], font=("Segoe UI", d["meta_font"], "italic"))
        return
    if not info:
        return

    # ── quick-upload button ────────────────────────────────────────────────────
    if _show_upl:
        _ubh  = max(12, NAME_H - 8)
        _ubx1 = sx_r - 4
        _ubx0 = _ubx1 - _upl_w
        _uby0 = y + (NAME_H - _ubh) // 2
        _uby1 = _uby0 + _ubh
        _uploading = entry.get("_uploading", False)
        _ubcol = _c(C_DIM) if _uploading else cc["border_no_bc"]
        _ubtxt = "⟳" if _uploading else ("⬆" if _COMPACT else "⬆ Upload")
        canvas.create_rectangle(_ubx0, _uby0, _ubx1, _uby1, fill=_ubcol, outline="")
        canvas.create_text((_ubx0 + _ubx1) // 2, (_uby0 + _uby1) // 2,
                           text=_ubtxt, anchor="center", fill="white",
                           font=("Segoe UI", 8 if _COMPACT else 9, "bold"))
        entry["_upload_btn"] = (_ubx0, _uby0, _ubx1, _uby1)
    else:
        entry.pop("_upload_btn", None)

    if TAG_H > 0:
        # compact mode: tag row below name row
        canvas.create_line(sx, y+NAME_H, sx_r, y+NAME_H, fill=cc["divider"])
        ty = y + NAME_H + TAG_H // 2
        if tags:
            tag_str = _fit_text(tags, "Segoe UI", d["tag_font"], "normal", x1 - cx - 6)
            canvas.create_text(cx, ty, text=tag_str, anchor="w",
                               fill=cc["date"], font=("Segoe UI", d["tag_font"]))

    # ── meta row(s) ──────────────────────────────────────────────────────────
    canvas.create_line(sx, y+NAME_H+TAG_H, sx_r, y+NAME_H+TAG_H, fill=cc["divider"])

    date_raw = info.get("date") or fmt_date(mtime)
    date_str = date_raw[:10]
    time_str = date_raw[11:16] if len(date_raw) >= 16 else ""
    map_name = map_display_name(info.get("map") or "")
    dur      = info.get("duration")
    dur_str  = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else ""
    time_utc = f"{time_str} UTC" if time_str else ""

    if META2_H:
        # compact: line 1 = map | duration, line 2 = date UTC
        my1 = y + NAME_H + TAG_H + META_H // 2
        meta1 = _fit_text("  |  ".join(p for p in [map_name, dur_str] if p),
                           "Segoe UI", d["meta_font"], "normal", x1 - cx - 6)
        canvas.create_text(cx, my1, text=meta1, anchor="w",
                           fill=cc["date"], font=("Segoe UI", d["meta_font"]))

        canvas.create_line(sx, y+NAME_H+TAG_H+META_H, sx_r, y+NAME_H+TAG_H+META_H, fill=cc["divider"])
        my2 = y + NAME_H + TAG_H + META_H + META2_H // 2
        date_line = _fit_text(f"{date_str}  {time_utc}".strip(),
                              "Segoe UI", d["meta_font"], "normal", x1 - cx - 6)
        canvas.create_text(cx, my2, text=date_line, anchor="w",
                           fill=cc["date"], font=("Segoe UI", d["meta_font"]))
    else:
        # normal: single meta line
        my = y + NAME_H + META_H // 2
        meta_parts = [p for p in [map_name, dur_str, f"{date_str} {time_utc}".strip()] if p]
        meta_str   = _fit_text("  |  ".join(meta_parts), "Segoe UI", d["meta_font"], "normal", sx_r - cx - 6)
        canvas.create_text(cx, my, text=meta_str, anchor="w",
                           fill=cc["date"], font=("Segoe UI", d["meta_font"]))

    # ── players ───────────────────────────────────────────────────────────────
    py_start = y + NAME_H + TAG_H + META_H + META2_H
    canvas.create_line(sx, py_start, sx_r, py_start, fill=cc["divider"])
    if not _COMPACT:
        cmid = (x0 + x1) // 2
        canvas.create_line(cmid, py_start, cmid, y+h, fill=cc["divider"])

    blue   = [p for p in info["players"] if p["team"] == 0]
    orange = [p for p in info["players"] if p["team"] == 1]

    if _COMPACT:
        SCORE_W_C = 22
        PLAT_W_C  = 40
        bar_w     = 3
        text_x    = x0 + bar_w + 4 + SCORE_W_C + PLAT_W_C

        actual_blue   = len(blue)
        actual_orange = len(orange)
        # use row-allocated counts so bars/scores line up for all cards in the row
        alloc_b = max(entry.get("_cbr", max(actual_blue, 1)), max(actual_blue, 1))
        alloc_o = max(entry.get("_cor", max(actual_orange, 1)), max(actual_orange, 1))

        blue_sec_h   = alloc_b * PLAYER_H
        orange_sec_h = alloc_o * PLAYER_H
        blue_sec_end   = py_start + blue_sec_h
        orange_sec_end = blue_sec_end + orange_sec_h

        canvas.create_rectangle(x0, py_start,     x0+bar_w, blue_sec_end,   fill=cc["blue"],   outline="")
        canvas.create_rectangle(x0, blue_sec_end, x0+bar_w, orange_sec_end, fill=cc["orange"], outline="")
        canvas.create_line(x0+bar_w, blue_sec_end, x1, blue_sec_end, fill=cc["divider"])

        sx_score = x0 + bar_w + 4
        sx_plat  = sx_score + SCORE_W_C

        # scores centred in their allocated section — same y for all cards in the row
        canvas.create_text(sx_score, py_start + blue_sec_h // 2,
                           text=str(info["team0"]), anchor="w",
                           fill=cc["blue"],   font=("Segoe UI", d["score_font"], "bold"))
        canvas.create_text(sx_score, blue_sec_end + orange_sec_h // 2,
                           text=str(info["team1"]), anchor="w",
                           fill=cc["orange"], font=("Segoe UI", d["score_font"], "bold"))

        pname_max    = x1 - text_x - 6
        # spread players evenly across their full allocated section
        blue_space_c = blue_sec_h   / max(actual_blue,   1)
        oran_space_c = orange_sec_h / max(actual_orange, 1)

        for i, p in enumerate(blue):
            ry   = int(py_start + (i + 0.5) * blue_space_c)
            plat = p.get("platform", "")
            if plat:
                canvas.create_text(sx_plat, ry, text=plat, anchor="w",
                                   fill=_platform_color(plat, cc["date"]),
                                   font=("Segoe UI", d["meta_font"], "bold"))
            canvas.create_text(text_x, ry,
                               text=_fit_text(p["name"], "Segoe UI", d["player_font"], "normal", pname_max),
                               anchor="w", fill=cc["blue"], font=("Segoe UI", d["player_font"]))

        for i, p in enumerate(orange):
            ry   = int(blue_sec_end + (i + 0.5) * oran_space_c)
            plat = p.get("platform", "")
            if plat:
                canvas.create_text(sx_plat, ry, text=plat, anchor="w",
                                   fill=_platform_color(plat, cc["date"]),
                                   font=("Segoe UI", d["meta_font"], "bold"))
            canvas.create_text(text_x, ry,
                               text=_fit_text(p["name"], "Segoe UI", d["player_font"], "normal", pname_max),
                               anchor="w", fill=cc["orange"], font=("Segoe UI", d["player_font"]))
    else:
        # normal: blue left half, orange right half, evenly spaced vertically
        plat_off  = 44
        cmid      = (x0 + x1) // 2
        blue_max  = cmid - cx - plat_off - 6
        blue_max0 = cmid - cx - 6
        oran_max  = (sx_r - 8) - cmid - plat_off - 6
        oran_max0 = (sx_r - 8) - cmid - 6

        # spread each team evenly across the full player area
        FOOTER_H     = d["footer_h"]
        _player_area = h - NAME_H - TAG_H - META_H - META2_H - FOOTER_H
        blue_space   = _player_area / max(len(blue),   1)
        oran_space   = _player_area / max(len(orange), 1)

        for i, p in enumerate(blue):
            ry   = int(py_start + (i + 0.5) * blue_space)
            plat = p.get("platform", "")
            if plat:
                canvas.create_text(cx, ry, text=plat, anchor="w",
                                   fill=_platform_color(plat, cc["date"]),
                                   font=("Segoe UI", d["meta_font"], "bold"))
                canvas.create_text(cx + plat_off, ry,
                                   text=_fit_text(p["name"], "Segoe UI", d["player_font"], "normal", blue_max),
                                   anchor="w", fill=cc["blue"], font=("Segoe UI", d["player_font"]))
            else:
                canvas.create_text(cx, ry,
                                   text=_fit_text(p["name"], "Segoe UI", d["player_font"], "normal", blue_max0),
                                   anchor="w", fill=cc["blue"], font=("Segoe UI", d["player_font"]))

        for i, p in enumerate(orange):
            ry   = int(py_start + (i + 0.5) * oran_space)
            plat = p.get("platform", "")
            if plat:
                canvas.create_text(sx_r - 8, ry, text=plat, anchor="e",
                                   fill=_platform_color(plat, cc["date"]),
                                   font=("Segoe UI", d["meta_font"], "bold"))
                canvas.create_text(sx_r - plat_off - 8, ry,
                                   text=_fit_text(p["name"], "Segoe UI", d["player_font"], "normal", oran_max),
                                   anchor="e", fill=cc["orange"], font=("Segoe UI", d["player_font"]))
            else:
                canvas.create_text(sx_r - 8, ry,
                                   text=_fit_text(p["name"], "Segoe UI", d["player_font"], "normal", oran_max0),
                                   anchor="e", fill=cc["orange"], font=("Segoe UI", d["player_font"]))


# ── Date range picker ────────────────────────────────────────────────────────

class DateRangePicker(tk.Toplevel):
    _BG      = "#1a1a1a"
    _BG_CELL = "#1a1a1a"
    _SEL     = "#3B8ED0"
    _RANGE   = "#1e3a5a"
    _FG_RNG  = "#64b5f6"
    _FG_DAY  = "#cccccc"
    _FG_DIM  = "#555555"

    def __init__(self, parent, from_var: tk.StringVar, to_var: tk.StringVar,
                 on_apply):
        super().__init__(parent)
        self.overrideredirect(True)
        self.configure(bg=self._BG)
        self.grab_set()

        self._from_var = from_var
        self._to_var   = to_var
        self._on_apply = on_apply
        self._start: _date | None = None
        self._end:   _date | None = None

        for var, attr in [(from_var, "_start"), (to_var, "_end")]:
            try:
                v = var.get().strip()
                if v:
                    setattr(self, attr, datetime.strptime(v, "%Y-%m-%d").date())
            except Exception:
                pass

        today = _date.today()
        if today.month == 1:
            self._ym_l = (today.year - 1, 12)
        else:
            self._ym_l = (today.year, today.month - 1)
        self._ym_r = (today.year, today.month)

        self._build()
        self.update_idletasks()

    def _build(self):
        outer = tk.Frame(self, bg=self._BG, bd=1, relief="solid",
                         highlightbackground="#3a3a3a", highlightthickness=1)
        outer.pack(fill="both", expand=True)

        cal_row = tk.Frame(outer, bg=self._BG)
        cal_row.pack(padx=14, pady=(12, 6))

        self._left_frame  = tk.Frame(cal_row, bg=self._BG)
        self._left_frame.pack(side="left", padx=(0, 14))
        self._right_frame = tk.Frame(cal_row, bg=self._BG)
        self._right_frame.pack(side="left")

        sep = tk.Frame(outer, bg="#2a2a2a", height=1)
        sep.pack(fill="x", padx=10)

        bottom = tk.Frame(outer, bg=self._BG)
        bottom.pack(fill="x", padx=14, pady=8)

        self._range_lbl = tk.Label(bottom, text="", bg=self._BG,
                                   fg="#888888", font=("Segoe UI", 10))
        self._range_lbl.pack(side="left")

        tk.Button(bottom, text="Apply", bg=self._SEL, fg="white",
                  relief="flat", font=("Segoe UI", 10), padx=10,
                  command=self._apply).pack(side="right", padx=(6, 0))
        tk.Button(bottom, text="Clear", bg="#2a2a2a", fg="#aaaaaa",
                  relief="flat", font=("Segoe UI", 10), padx=10,
                  command=self._clear).pack(side="right")

        self._render()

    def _render(self):
        for w in self._left_frame.winfo_children():  w.destroy()
        for w in self._right_frame.winfo_children(): w.destroy()
        self._draw_month(self._left_frame,  *self._ym_l, show_prev=True,  show_next=False)
        self._draw_month(self._right_frame, *self._ym_r, show_prev=False, show_next=True)
        self._update_label()

    def _draw_month(self, parent, year, month, show_prev, show_next):
        today = _date.today()

        # ── navigation header ────────────────────────────────────────────────
        nav = tk.Frame(parent, bg=self._BG)
        nav.grid(row=0, column=0, columnspan=7, sticky="ew", pady=(0, 6))

        if show_prev:
            tk.Label(nav, text="‹", bg=self._BG, fg="#aaaaaa",
                     font=("Segoe UI", 14), cursor="hand2"
                     ).pack(side="left")
            nav.winfo_children()[-1].bind("<Button-1>", lambda e: self._shift(-1))

        mn = _cal.month_abbr[month]
        tk.Label(nav, text=f"{mn}  {year}", bg=self._BG, fg="white",
                 font=("Segoe UI", 11, "bold")).pack(side="left", expand=True)

        if show_next:
            tk.Label(nav, text="›", bg=self._BG, fg="#aaaaaa",
                     font=("Segoe UI", 14), cursor="hand2"
                     ).pack(side="right")
            nav.winfo_children()[-1].bind("<Button-1>", lambda e: self._shift(1))

        # ── day-of-week headers ──────────────────────────────────────────────
        for c, h in enumerate(["Su","Mo","Tu","We","Th","Fr","Sa"]):
            tk.Label(parent, text=h, bg=self._BG, fg=_c(C_DIM),
                     font=("Segoe UI", 9, "bold"), width=4
                     ).grid(row=1, column=c, pady=(0, 2))

        # ── day cells ────────────────────────────────────────────────────────
        weeks = _cal.monthcalendar(year, month)   # Mon-first
        s = self._start; e = self._end
        rng_lo = min(s, e) if s and e else None
        rng_hi = max(s, e) if s and e else None

        for r, week in enumerate(weeks):
            rotated = [week[6]] + week[:6]         # Sun-first
            for c, day in enumerate(rotated):
                if day == 0:
                    tk.Label(parent, text="", bg=self._BG, width=4
                             ).grid(row=r+2, column=c, padx=1, pady=1)
                    continue

                dt = _date(year, month, day)
                is_sel = dt in (s, e)
                in_rng = rng_lo and rng_hi and rng_lo < dt < rng_hi
                is_today = dt == today

                if is_sel:
                    bg, fg, font_w = self._SEL, "white", "bold"
                elif in_rng:
                    bg, fg, font_w = self._RANGE, self._FG_RNG, "normal"
                elif is_today:
                    bg, fg, font_w = "#2a2a2a", self._SEL, "bold"
                else:
                    bg, fg, font_w = self._BG_CELL, self._FG_DAY, "normal"

                lbl = tk.Label(parent, text=str(day), bg=bg, fg=fg, width=4,
                               font=("Segoe UI", 10, font_w), cursor="hand2")
                lbl.grid(row=r+2, column=c, padx=1, pady=1)
                lbl.bind("<Button-1>", lambda e, d=dt: self._click(d))

    def _click(self, dt: _date):
        if self._start is None or (self._start and self._end):
            self._start = dt; self._end = None
        else:
            if dt < self._start:
                self._end = self._start; self._start = dt
            else:
                self._end = dt
        self._render()

    def _shift(self, direction: int):
        def inc(ym, n):
            y, m = ym; m += n
            if m > 12: return (y+1, 1)
            if m < 1:  return (y-1, 12)
            return (y, m)
        self._ym_l = inc(self._ym_l, direction)
        self._ym_r = inc(self._ym_r, direction)
        self._render()

    def _update_label(self):
        if self._start and self._end:
            self._range_lbl.configure(
                text=f"{self._start}  –  {self._end}")
        elif self._start:
            self._range_lbl.configure(text=f"{self._start}  –  …")
        else:
            self._range_lbl.configure(text="")

    def _apply(self):
        self._from_var.set(str(self._start) if self._start else "")
        self._to_var.set(str(self._end)   if self._end   else "")
        self.destroy()
        self._on_apply()

    def _clear(self):
        self._start = self._end = None
        self._from_var.set(""); self._to_var.set("")
        self._render()
        self._on_apply()


# ── Win32 icon helper ─────────────────────────────────────────────────────────

def _set_win32_icon(hwnd: int, ico_path: str):
    """Load ICO at exact sizes via LoadImage and set both ICON_SMALL/ICON_BIG."""
    try:
        import ctypes
        user32      = ctypes.windll.user32
        LR_FILE     = 0x10
        IMAGE_ICON  = 1
        WM_SETICON  = 0x0080
        ICON_SMALL  = 0
        ICON_BIG    = 1
        SM_CXSMICON = 49   # system small-icon width metric
        SM_CYSMICON = 50
        SM_CXICON   = 11
        SM_CYICON   = 12
        sw = user32.GetSystemMetrics(SM_CXSMICON)  # 24 at 100% DPI
        bw = user32.GetSystemMetrics(SM_CXICON)    # 32 at 100% DPI
        hs = user32.LoadImageW(None, ico_path, IMAGE_ICON, sw, sw, LR_FILE)
        hb = user32.LoadImageW(None, ico_path, IMAGE_ICON, bw, bw, LR_FILE)
        if hs: user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hs)
        if hb: user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG,   hb)
    except Exception:
        pass


# ── App ───────────────────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Ballchasing Auto Uploader")
        self.geometry("960x880")
        self.resizable(True, True)
        self.minsize(900, 540)

        # ── window icon (Win32 direct — avoids Tk picking the wrong ICO frame) ──
        _ico = BASE_DIR / "logo.ico"
        try:
            if _ico.exists():
                self.iconbitmap(str(_ico))   # title bar fallback
                self.after(50, lambda: _set_win32_icon(self.winfo_id(), str(_ico)))
        except Exception:
            pass

        self.config_data = load_config()
        self.uploaded    = load_uploaded()
        self.observer: Observer | None = None
        self._current_page = "main"
        # Snapshot the exe's launcher version before _check_launcher_update
        # can update it in memory — used by _check_exe_update
        self._exe_launcher_version = self.config_data.get("_launcher_version", "")
        # Low priority mode kicks in AFTER launch, not during startup
        # (startup runs at normal priority so it loads quickly)
        # Sync desktop shortcut immediately (before window renders) so the old
        # frozen launcher's unconditional _create_shortcut() is undone before
        # the user sees the desktop.
        self._sync_desktop_shortcut()

        global _COMPACT
        _COMPACT = bool(self.config_data.get("compact_mode", False))

        self._cards: list[dict] = []
        self._active_cards: list[dict] = []
        self._card_index: dict[str, int] = {}
        self._total_h = _dims()["card_pad"]
        self._normal_card_w: int = 300
        self._normal_base_w: int = 450
        self._parse_queue: list[Path] = []
        self._parse_gen   = 0
        self._parse_done  = 0
        self._redraw_id = None
        self._detail_queue: list[Path] = []
        self._detail_gen = 0

        self._filt_type   = tk.StringVar(value="All")
        self._filt_mode   = tk.StringVar(value="All")
        self._filt_from   = tk.StringVar(value="")
        self._filt_to     = tk.StringVar(value="")
        self._filt_search = tk.StringVar(value="")
        self._filt_name   = ctk.StringVar()
        self._filter_id   = None
        self._my_name     = ""

        self._build_header()
        self._build_main_page()
        self._build_settings_page()
        self._build_replays_page()
        self._build_detail_page()
        self._show_main()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._loading = False
        self._rebuild_id            = None
        self._resize_id             = None
        self._save_uploaded_id      = None
        self._upload_session_enabled = True
        self._download_active        = False
        self._mirror_no_upload: set  = set()   # filenames moved from mirror — skip upload
        self._retry_queue: dict      = {}       # filename → attempt count (failed upload retry)
        self._dot_anim_id            = None     # after() id for status dot pulse animation
        self._bg_cache_busy          = False
        self._cache_progress_line    = None
        self._dl_progress_line       = None
        self._dl_status_line         = None
        self._dl_error_line          = None
        self.after(150, self._apply_canvas_theme)
        self.after(200, self._check_integrity)
        self.after(300, self._fetch_quota)
        self.after(500, self._send_ping)
        self.after(4000, self._check_launcher_update)
        self.after(5000, self._check_self_update)
        self.after(2000, self._check_exe_update)
        self.after(800,  self._load_replays_for_main)
        self.after(1200, self._bg_cache_replays)
        self.after(1800, self._scan_mirror_folder)
        self.after(2500, self._startup_dedup)
        self.after(3500, self._bg_build_index)
        self.after(4500, self._check_first_run)
        # After startup tasks finish, activate focus-aware priority if enabled
        self.after(6000, self._setup_priority_focus)
        if self.config_data.get("launch_with_rl", False):
            self._rl_poll_thread_running = True
            threading.Thread(target=self._rl_poll_loop, daemon=True).start()

    # ── header ────────────────────────────────────────────────────────────────

    def _build_header(self):
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=20, pady=(16, 0))
        self.nav_btn = ctk.CTkButton(hdr, text="⚙", width=36, height=36,
                                     font=ctk.CTkFont(size=18),
                                     fg_color="transparent",
                                     hover_color=("#d0d0d0", "#2a2d2e"),
                                     text_color=("gray20", "gray80"),
                                     command=self._on_nav)
        self.nav_btn.pack(side="right")
        self.status_dot = ctk.CTkLabel(hdr, text="●", font=ctk.CTkFont(size=20),
                                       text_color="#555")
        self.status_dot.pack(side="right", padx=(0, 2))
        self.status_label = ctk.CTkLabel(hdr, text="Stopped", font=ctk.CTkFont(size=13))
        self.status_label.pack(side="right", padx=(0, 6))
        # ── logo + app name on the left ───────────────────────────────────────
        _png = BASE_DIR / "logo.png"
        if _png.exists():
            try:
                from PIL import Image as _PILImage
                from customtkinter import CTkImage as _CTkImage
                _pil = _PILImage.open(str(_png)).resize((34, 34), _PILImage.LANCZOS)
                self._logo_img = _CTkImage(light_image=_pil, dark_image=_pil, size=(34, 34))
                ctk.CTkLabel(hdr, image=self._logo_img, text="").pack(side="left", padx=(0, 8))
            except Exception:
                self._logo_img = None
        ctk.CTkLabel(hdr, text="Ballchasing Auto Uploader",
                     font=ctk.CTkFont(size=15, weight="bold"),
                     text_color=("gray10", "gray90")).pack(side="left")
        ctk.CTkLabel(hdr, text=f"v{VERSION}",
                     font=ctk.CTkFont(size=13),
                     text_color="gray50").pack(side="left", padx=(6, 0))

    # ── main page ─────────────────────────────────────────────────────────────

    def _build_main_page(self):
        self.main_page = ctk.CTkFrame(self, fg_color="transparent")
        self.toggle_btn = ctk.CTkButton(self.main_page, text="Start Watching", height=44,
                                        font=ctk.CTkFont(size=15, weight="bold"),
                                        command=self._toggle_watching)
        self.toggle_btn.pack(fill="x", padx=20, pady=(14, 8))

        # Update button — hidden until a new exe is available
        self._update_btn = ctk.CTkButton(
            self.main_page, text="⬆  App update available — Restart & Update",
            height=34, font=ctk.CTkFont(size=13),
            fg_color="#2a6099", hover_color="#1e4f7a",
            command=self._apply_exe_update)
        # NOT packed here — shown by _prompt_exe_update when needed

        quota_row = ctk.CTkFrame(self.main_page, fg_color="transparent")
        quota_row.pack(fill="x", padx=20, pady=(0, 6))
        ctk.CTkButton(quota_row, text="↻", width=28, height=22,
                      fg_color="transparent", border_width=1, border_color=C_BORDER,
                      text_color=("gray10", "gray90"),
                      font=ctk.CTkFont(size=13),
                      command=self._fetch_quota).pack(side="left", padx=(0, 6))
        self.quota_label = ctk.CTkLabel(quota_row, text="",
                                        font=ctk.CTkFont(family="Consolas", size=13),
                                        text_color=("gray10", "gray90"), anchor="w")
        self.quota_label.pack(side="left", fill="x", expand=True)


        self.log_box = ctk.CTkTextbox(self.main_page, state="disabled",
                                      height=210,
                                      font=ctk.CTkFont(family="Consolas", size=13))
        self.log_box.pack(fill="x", padx=20, pady=(0, 4))
        self.log_box._textbox.tag_config("green", foreground="#4aaa88")
        self.log_box._textbox.tag_config("red",   foreground="#e06060")

        # ── recent replays ────────────────────────────────────────────────────
        recent_hdr = ctk.CTkFrame(self.main_page, fg_color="transparent")
        recent_hdr.pack(fill="x", padx=20, pady=(8, 2))
        ctk.CTkLabel(recent_hdr, text="Recent Replays",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=("gray10", "gray90")).pack(side="left")
        ctk.CTkButton(recent_hdr, text="View all →", width=72, height=22,
                      fg_color="transparent", border_width=1, border_color=C_BORDER,
                      text_color=("gray45", "gray55"),
                      font=ctk.CTkFont(size=11),
                      command=self._show_replays).pack(side="right")
        self._recent_canvas = tk.Canvas(self.main_page, bg=_c(C_BG),
                                        highlightthickness=0, height=40)
        self._recent_canvas.pack(fill="x", padx=20, pady=(0, 8))
        self._recent_canvas.bind("<Configure>",
                                 lambda _: self.after(0, self._update_recent_replays))
        self._recent_canvas.bind("<Button-1>", self._on_recent_click)

        site_link = ctk.CTkLabel(self.main_page, text="Website",
                                 font=ctk.CTkFont(size=11),
                                 text_color="gray45", cursor="hand2", anchor="e")
        site_link.pack(fill="x", padx=20, pady=(0, 10))
        site_link.bind("<Button-1>", lambda _: webbrowser.open("http://46.101.184.78"))

    def _update_recent_replays(self):
        if not hasattr(self, '_recent_canvas'):
            return
        canvas = self._recent_canvas
        canvas.delete("all")
        cc = _card_colors()
        canvas.configure(bg=cc["bg"])

        if not self._cards:
            canvas.configure(height=32)
            canvas.create_text(CARD_MARGIN_X, 16, text="No replays yet",
                               anchor="w", fill=cc["dim"], font=("Segoe UI", 11))
            return

        # sort by Date — same logic as _apply_filters
        def sort_key(c):
            ts = replay_date_ts((c.get("info") or {}).get("date") or "")
            if ts > 0:
                return ts
            if c.get("parsing") or c.get("info") is None:
                return 0.0   # sink unparse cards to the bottom temporarily
            return c.get("mtime", 0.0)
        recent = sorted(self._cards, key=sort_key, reverse=True)[:3]

        w   = canvas.winfo_width()
        if w < 20:
            w = self.winfo_width() - 40   # fallback before widget is laid out

        pad      = _dims()["card_pad"]
        total_h  = pad
        tmp_cards = []
        for card in recent:
            tmp = dict(card)          # shallow copy — don't corrupt replays-grid layout props
            tmp["height"]    = card_height(card.get("info"))
            tmp["grid_cols"] = 1
            tmp["grid_col"]  = 0
            tmp["_max_w"]    = w - 2 * CARD_MARGIN_X
            total_h += tmp["height"] + pad
            tmp_cards.append((tmp, card))   # (display copy, original)

        canvas.configure(height=max(total_h, 40))

        self._recent_hit_rects = []
        y = pad
        for tmp, orig in tmp_cards:
            self._recent_hit_rects.append((y, y + tmp["height"], orig))
            draw_card(canvas, y, w, tmp)
            y += tmp["height"] + pad

    # ── replays page ──────────────────────────────────────────────────────────

    def _build_replays_page(self):
        self.replays_page  = ctk.CTkFrame(self, fg_color="transparent")
        self._stats_visible = False

        bar = ctk.CTkFrame(self.replays_page, fg_color="transparent")
        bar.pack(fill="x", padx=20, pady=(14, 4))

        self.replays_title = ctk.CTkLabel(bar, text="Replays",
                                          font=ctk.CTkFont(size=16, weight="bold"))
        self.replays_title.pack(side="left")

        # Right side: vertical stack of 3 buttons
        btn_stack = ctk.CTkFrame(bar, fg_color="transparent")
        btn_stack.pack(side="right")
        self._stats_toggle_btn = ctk.CTkButton(
            btn_stack, text="Player Stats", width=110, height=24,
            fg_color="transparent", border_width=1, border_color=C_BORDER,
            text_color=("gray10", "gray90"),
            font=ctk.CTkFont(size=12),
            command=self._toggle_stats_view)
        self._stats_toggle_btn.pack(fill="x", pady=(0, 3))
        self.compact_btn = ctk.CTkButton(
            btn_stack,
            text="Normal" if _COMPACT else "Compact",
            width=110, height=24,
            fg_color=C_SCORE_COL if _COMPACT else "transparent",
            border_width=1, border_color=C_BORDER,
            text_color=C_NAME,
            font=ctk.CTkFont(size=12),
            command=self._toggle_compact)
        self.compact_btn.pack(fill="x", pady=(0, 3))
        ctk.CTkButton(
            btn_stack, text="↻  Refresh", width=110, height=24,
            fg_color="transparent", border_width=1,
            border_color=("#3B8ED0", "#1F6AA5"),
            text_color=("gray10", "gray90"),
            font=ctk.CTkFont(size=12),
            command=self._load_replays).pack(fill="x")

        # ── filter bar ────────────────────────────────────────────────────────
        self._replays_fbar = ctk.CTkFrame(self.replays_page, fg_color="transparent")
        self._replays_fbar.pack(fill="x", padx=20, pady=(0, 6))

        _lkw = dict(text_color=("gray30", "gray70"), font=ctk.CTkFont(size=12))
        _seg_kw = dict(height=26, font=ctk.CTkFont(size=12),
                       fg_color=("#d0d0d0", "#2a2a2a"),
                       selected_color="#3B8ED0",
                       selected_hover_color="#3480c0",
                       unselected_color=("#d0d0d0", "#2a2a2a"),
                       unselected_hover_color=("#bbbbbb", "#333333"),
                       text_color=("gray10", "gray90"))

        row1 = ctk.CTkFrame(self._replays_fbar, fg_color="transparent")
        row1.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(row1, text="Type:", **_lkw).pack(side="left", padx=(0, 4))
        ctk.CTkOptionMenu(row1, values=["All", "Ranked", "Casual", "Online", "Private", "Tournament"],
                          variable=self._filt_type,
                          command=lambda _: self._apply_filters(),
                          width=120, height=26,
                          font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 12))
        ctk.CTkLabel(row1, text="Mode:", **_lkw).pack(side="left", padx=(0, 4))
        ctk.CTkSegmentedButton(row1, values=["All", "1s", "2s", "3s"],
                               variable=self._filt_mode,
                               command=lambda _: self._apply_filters(),
                               **_seg_kw).pack(side="left", padx=(0, 12))
        ctk.CTkLabel(row1, text="Player:", **_lkw).pack(side="left", padx=(0, 4))
        srch = ctk.CTkEntry(row1, textvariable=self._filt_search,
                            placeholder_text="name or Steam ID",
                            height=26, width=160, font=ctk.CTkFont(size=12))
        srch.pack(side="left")
        self._filt_search.trace_add("write", lambda *_: self._schedule_filter())

        row2 = ctk.CTkFrame(self._replays_fbar, fg_color="transparent")
        row2.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(row2, text="Date:", **_lkw).pack(side="left", padx=(0, 4))
        self._date_btn = ctk.CTkButton(row2, text="Any date", width=160, height=26,
                                       fg_color="transparent", border_width=1,
                                       border_color=C_BORDER, font=ctk.CTkFont(size=12),
                                       text_color=("gray10", "gray90"),
                                       anchor="w", command=self._open_date_picker)
        self._date_btn.pack(side="left", padx=(0, 12))
        ctk.CTkLabel(row2, text="Name:", **_lkw).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(row2, textvariable=self._filt_name,
                     placeholder_text="replay name",
                     height=26, width=160, font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 8))
        ctk.CTkButton(row2, text="✕ Clear", width=70, height=26,
                      fg_color="transparent", border_width=1, border_color=C_BORDER,
                      text_color=("gray10", "gray90"),
                      font=ctk.CTkFont(size=12),
                      command=self._clear_filters).pack(side="left")
        self._filt_from.trace_add("write", lambda *_: self._update_date_btn())
        self._filt_to.trace_add("write",   lambda *_: self._update_date_btn())
        self._filt_name.trace_add("write", lambda *_: self._schedule_filter())

        # ── canvas (replay list) ──────────────────────────────────────────────
        self._replays_wrap = tk.Frame(self.replays_page, bg=_c(C_BG))
        self._replays_wrap.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        self._vsb_style = ttk.Style()
        self._vsb_style.theme_use("clam")
        self._vsb = ttk.Scrollbar(self._replays_wrap, orient="vertical")
        self._vsb.pack(side="right", fill="y")
        self.canvas = tk.Canvas(self._replays_wrap, bg=_card_colors()["bg"], highlightthickness=0,
                                yscrollcommand=self._vsb.set)
        vsb = self._vsb
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.config(command=self._canvas_yview)
        self.canvas.bind("<Configure>",  lambda e: self._on_canvas_resize())
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>",   lambda e: (self.canvas.yview_scroll(-1,"units"), self._schedule_redraw()))
        self.canvas.bind("<Button-5>",   lambda e: (self.canvas.yview_scroll( 1,"units"), self._schedule_redraw()))
        self.canvas.bind("<Button-1>",   self._on_canvas_click)
        self.canvas.bind("<Button-3>",   self._on_canvas_right_click)

        # ── stats panel (hidden until toggled) ────────────────────────────────
        self._stats_outer = ctk.CTkFrame(self.replays_page, fg_color="transparent")
        # NOT packed here — shown on toggle

        # ── edit-accounts row (hidden by default, shown on demand) ───────────────
        self._id_edit_row = ctk.CTkFrame(self._stats_outer, fg_color="transparent")
        # not packed yet
        self._stats_ids_entry = ctk.CTkEntry(self._id_edit_row,
                                             placeholder_text="Epic:Name, Steam:ID  (max 4)",
                                             font=ctk.CTkFont(size=12))
        self._stats_ids_entry.pack(side="left", padx=(0, 6), fill="x", expand=True)
        self._stats_ids_entry.insert(0, self._ids_to_display_str())
        ctk.CTkButton(self._id_edit_row, text="Save", width=60, height=28,
                      command=self._save_ids_and_hide_edit).pack(side="left", padx=(0, 4))
        ctk.CTkButton(self._id_edit_row, text="✕", width=28, height=28,
                      fg_color="transparent", text_color=C_DIM,
                      command=self._hide_id_edit_row).pack(side="left")

        self.stats_content = ctk.CTkFrame(self._stats_outer, fg_color="transparent")
        self.stats_content.pack(fill="both", expand=True)

    def _autosave_ids_entry(self):
        """Silently save whatever is in the My Accounts entry field."""
        if not hasattr(self, "_stats_ids_entry"):
            return
        raw = self._stats_ids_entry.get().strip()
        ids_list = [x.strip() for x in raw.split(",") if x.strip()][:4]
        self.config_data["my_identities"] = ", ".join(ids_list)
        save_config(self.config_data)

    def _resolve_token_forms(self, token: str) -> set:
        """Return all known equivalent forms of a token (display name ↔ raw ID)."""
        forms = {token}
        id_names = self.config_data.get("_identity_names") or {}
        # if token is a display form "Epic:Name", find the raw-ID form
        if ":" in token:
            plat, _, name = token.partition(":")
            for raw_key, display in id_names.items():
                if display == name and raw_key.startswith(plat + ":"):
                    forms.add(raw_key)
        # if token is a raw-ID form "Epic:hexid", find the display form
        display = id_names.get(token)
        if display and ":" in token:
            plat = token.split(":")[0]
            forms.add(f"{plat}:{display}")
        return forms

    def _toggle_identity(self, token: str):
        forms    = self._resolve_token_forms(token)
        excluded = list(self.config_data.get("_excluded_identities") or [])
        if any(f in excluded for f in forms):
            excluded = [t for t in excluded if t not in forms]
        else:
            excluded.append(token)
        self.config_data["_excluded_identities"] = excluded
        save_config(self.config_data)
        self._run_me_search()

    def _remove_identity(self, token: str):
        forms = self._resolve_token_forms(token)
        # remove all equivalent forms from my_identities
        raw = self.config_data.get("my_identities", "")
        ids = [t.strip() for t in raw.split(",") if t.strip() and t.strip() not in forms]
        self.config_data["my_identities"] = ", ".join(ids)
        # also remove from excluded list
        excluded = [t for t in (self.config_data.get("_excluded_identities") or []) if t not in forms]
        self.config_data["_excluded_identities"] = excluded
        save_config(self.config_data)
        # sync entry field so _autosave_ids_entry doesn't re-add the removed token
        if hasattr(self, "_stats_ids_entry"):
            self._stats_ids_entry.delete(0, "end")
            self._stats_ids_entry.insert(0, self._ids_to_display_str())
        self._run_me_search()

    def _show_id_edit_row(self):
        self._stats_ids_entry.delete(0, "end")
        self._stats_ids_entry.insert(0, self._ids_to_display_str())
        self._id_edit_row.pack(fill="x", pady=(0, 6), before=self.stats_content)

    def _hide_id_edit_row(self):
        self._id_edit_row.pack_forget()

    def _save_ids_and_hide_edit(self):
        self._save_ids_and_refresh()
        self._hide_id_edit_row()

    def _toggle_stats_view(self):
        if self._stats_visible:
            self._autosave_ids_entry()
            self._stats_outer.pack_forget()
            self._replays_fbar.pack(fill="x", padx=20, pady=(0, 6))
            self._replays_wrap.pack(fill="both", expand=True, padx=20, pady=(0, 16))
            self._stats_toggle_btn.configure(text="Player Stats", fg_color="transparent")
            self.replays_title.configure(text=self.replays_title.cget("text").replace("Player Stats", "Replays"))
            self._stats_visible = False
            self._schedule_redraw()
        else:
            self._replays_fbar.pack_forget()
            self._replays_wrap.pack_forget()
            self._stats_outer.pack(fill="both", expand=True, padx=20, pady=(0, 16))
            self._stats_toggle_btn.configure(text="← Replays", fg_color=("#d0d0d0", "#2a2a2a"))
            self._stats_visible = True
            self._run_me_search()

    def _canvas_yview(self, *args):
        self.canvas.yview(*args)
        self._schedule_redraw()
        self._reprioritize_parse_queue()

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._schedule_redraw()
        self._reprioritize_parse_queue()

    def _reprioritize_parse_queue(self):
        if not self._parse_queue:
            return
        vh = self.canvas.winfo_height()
        view_mid = self.canvas.canvasy(vh / 2) if vh > 0 else 0
        card_mid = {c["path"]: c.get("y", 0) + c.get("height", 0) / 2
                    for c in self._active_cards}
        self._parse_queue.sort(key=lambda p: abs(card_mid.get(p, 0) - view_mid))

    def _schedule_redraw(self):
        if self._redraw_id is not None:
            self.after_cancel(self._redraw_id)
        self._redraw_id = self.after(40, self._redraw)

    def _render_buffer(self) -> int:
        d = _dims()
        card_h = d["name_h"] + d["tag_h"] + d["meta_h"] + d["meta2_h"] + d["footer_h"]
        return 20 * max(card_h, 40)

    def _redraw(self):
        self._redraw_id = None
        cw = self.canvas.winfo_width()
        if cw <= 1:
            # Canvas not yet mapped/sized — drawing now would produce negative
            # col_w values and invisible cards.  The <Configure> event that
            # fires when the canvas is actually packed will trigger _do_resize
            # → _apply_filters → _schedule_redraw with real dimensions.
            return
        self.canvas.delete("all")
        if not self._active_cards:
            return
        buf      = self._render_buffer()
        eff_w    = cw
        view_top = self.canvas.canvasy(0) - buf
        view_bot = self.canvas.canvasy(self.canvas.winfo_height()) + buf

        # Keep info loaded for cards within 2000px of the viewport;
        # unload everything farther away to cap RAM regardless of folder size.
        UNLOAD_BUFFER = 2000
        for card in self._active_cards:
            y0, y1 = card["y"], card["y"] + card["height"]
            in_view = y1 >= view_top and y0 <= view_bot
            near    = y0 >= view_top - UNLOAD_BUFFER and y1 <= view_bot + UNLOAD_BUFFER

            if in_view or near:
                # Lazy-load info if it was unloaded but cache exists
                if (card["info"] is None
                        and not card.get("parsing")
                        and not card.get("failed")):
                    info = load_cached(card["filename"])
                    if info:
                        card["info"] = info
                        card["type"] = replay_type(info)
                if in_view:
                    draw_card(self.canvas, y0, eff_w, card)
            else:
                # Unload info for cards far from the viewport
                if (card["info"] is not None
                        and not card.get("parsing")
                        and not card.get("failed")):
                    card["info"] = None

    def _apply_filters(self, sort=True):
        ftype  = self._filt_type.get()
        fmode  = self._filt_mode.get()
        ffrom  = self._filt_from.get().strip()
        fto    = self._filt_to.get().strip()
        fsrch  = self._filt_search.get().strip().lower()
        fname  = self._filt_name.get().strip().lower()

        active = []
        for card in self._cards:
            info = card.get("info") or {}
            ts   = info.get("team_size", 0)
            date = (info.get("date") or "")[:10]
            rt   = card.get("type") or replay_type(info)

            if ftype != "All" and rt != ftype: continue
            if fmode == "1s" and ts != 1: continue
            if fmode == "2s" and ts != 2: continue
            if fmode == "3s" and ts != 3: continue
            if ffrom and (not date or date < ffrom): continue
            if fto   and (not date or date > fto):   continue
            if fsrch:
                players = info.get("players") or []
                if not any(fsrch in (p.get("name") or "").lower() for p in players):
                    continue
            if fname:
                rname = (info.get("replay_name") or "").lower()
                if fname not in rname:
                    continue
            active.append(card)

        if sort:
            # Sort by the Date field embedded in the replay (UTC unix timestamp).
            # Using replay_date_ts() normalises both colon-time and dash-time
            # formats so they compare correctly when mixed in the same folder.
            # For unparse/parsing cards with no date yet: sink to 0.0 (bottom)
            # so they don't corrupt the sort; they'll float up once parse
            # completes and _apply_filters re-runs via _rebuild_positions.
            def sort_key(c):
                ts = replay_date_ts((c.get("info") or {}).get("date") or "")
                if ts > 0:
                    return ts
                if c.get("parsing") or c.get("info") is None:
                    return 0.0   # sink unparse cards temporarily
                return c.get("mtime", 0.0)
            active = sorted(active, key=sort_key, reverse=True)

        self._active_cards = active

        if not _COMPACT and active:
            d          = _dims()
            fn_player  = _measure_font("Segoe UI", d["player_font"], "normal")
            fn_meta    = _measure_font("Segoe UI", d["meta_font"],   "normal")
            N_MIN      = 450
            N_CX       = SCORE_W + 8    # left text offset = 60
            N_PLAT     = 44             # platform badge width
            N_PAD      = 8
            normal_base_w = N_MIN
            for c in active:
                info = c.get("info") or {}
                dur      = info.get("duration")
                dur_str  = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else ""
                map_name = map_display_name(info.get("map") or "")
                raw_date = info.get("date", "")
                date_str = raw_date[:10]
                time_str = raw_date[11:16] if len(raw_date) >= 16 else ""
                meta1    = "  |  ".join(p for p in [map_name, dur_str] if p)
                meta2    = f"{date_str}  {time_str} UTC".strip() if time_str else date_str
                for mt in [meta1, meta2]:
                    if mt:
                        # meta spans between both score columns: left (SCORE_W+8) + right (SCORE_W+8)
                        normal_base_w = max(normal_base_w, N_CX + fn_meta.measure(mt) + SCORE_W + N_PAD)
                for p in info.get("players", []):
                    extra = N_PLAT if p.get("platform") else 0
                    # Cap per-name measurement at 110 px so one outlier name
                    # (e.g. "give me a ******* break.") doesn't inflate
                    # normal_base_w above N_MIN and reduce cols for every card.
                    px    = min(fn_player.measure(p["name"]), 110)
                    normal_base_w = max(normal_base_w, 2 * (N_CX + extra + px + N_PAD))
            _ncw          = max(self.canvas.winfo_width(), 600)   # guard: 1px when not yet shown
            _navail       = _ncw - 2 * CARD_MARGIN_X
            normal_max_w  = min(2 * normal_base_w - 1, _navail)
            self._normal_base_w = normal_base_w
            self._normal_card_w = normal_max_w

        pad = _dims()["card_pad"]
        y   = pad
        if _COMPACT:
            d     = _dims()
            cw    = max(self.canvas.winfo_width(), 600)   # guard: 1px when canvas not yet shown
            avail = cw - 2 * CARD_MARGIN_X

            # minimum width = widest element across all cards (names, players, meta, tags)
            # _measure_px caches every string→pixel result so resize recalcs are fast
            pf  = "Segoe UI"
            psz = d["player_font"]; msz = d["meta_font"]; tsz = d["tag_font"]
            PLAYER_OFF = 3 + 4 + 22 + 40  # bar+gap+score+plat prefix in compact player row
            CX_OFF     = 8                 # cx = x0 + 8
            R_PAD      = 6

            max_content_px = 0
            for c in active:
                info = c.get("info") or {}
                # type/mode tag
                ts = info.get("team_size") or 0
                type_tag  = replay_type(info) if info else ""
                mode_str  = {1:"Duel",2:"Doubles",3:"Standard",4:"Chaos"}.get(ts, f"{ts}v{ts}" if ts else "")
                tags      = "  ·  ".join(t for t in [type_tag, mode_str] if t)
                if tags:
                    max_content_px = max(max_content_px, _measure_px(tags, pf, tsz) + CX_OFF + R_PAD)
                # meta text
                dur = info.get("duration")
                dur_str  = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else ""
                map_name = map_display_name(info.get("map") or "")
                raw_date = info.get("date", "")
                date_str = raw_date[:10]
                time_str = raw_date[11:16] if len(raw_date) >= 16 else ""
                meta1 = "  |  ".join(p for p in [map_name, dur_str] if p)
                meta2 = f"{date_str}  {time_str} UTC".strip() if time_str else date_str
                for mt in [meta1, meta2]:
                    if mt:
                        max_content_px = max(max_content_px, _measure_px(mt, pf, msz) + CX_OFF + R_PAD)
                # player names
                for p in info.get("players", []):
                    max_content_px = max(max_content_px,
                                         PLAYER_OFF + _measure_px(p["name"], pf, psz) + R_PAD)

            min_w = max(COMPACT_MIN_W, max_content_px)

            cols  = max(1, (avail + pad) // (min_w + pad))
            n     = len(self._active_cards)
            i     = 0

            def _team_rows(c, team):
                return max(len([p for p in (c.get("info") or {}).get("players", [])
                                if p["team"] == team]), 1)

            while i < n:
                row_cards      = self._active_cards[i:i+cols]
                row_max_blue   = max(_team_rows(c, 0) for c in row_cards)
                row_max_orange = max(_team_rows(c, 1) for c in row_cards)
                row_h          = (d["name_h"] + d["tag_h"] + d["meta_h"] + d["meta2_h"]
                                  + (row_max_blue + row_max_orange) * d["player_h"]
                                  + d["footer_h"])
                for col, card in enumerate(row_cards):
                    card["y"]         = y
                    card["height"]    = row_h
                    card["grid_col"]  = col
                    card["grid_cols"] = cols
                    card["_cbr"]      = row_max_blue
                    card["_cor"]      = row_max_orange
                y += row_h + pad
                i += cols
        else:
            # normal mode: multi-column when window wide enough
            d       = _dims()
            cw      = max(self.canvas.winfo_width(), 600)   # guard: 1px when canvas not yet shown
            avail   = cw - 2 * CARD_MARGIN_X
            base_w  = getattr(self, "_normal_base_w", 450)
            max_w   = getattr(self, "_normal_card_w", avail)
            cols    = max(1, (avail + pad) // (base_w + pad))
            col_w   = min((avail - (cols - 1) * pad) // cols, max_w)
            n       = len(self._active_cards)
            i       = 0

            def _team_rows_n(c, team):
                return max(len([p for p in (c.get("info") or {}).get("players", [])
                                if p["team"] == team]), 1)

            while i < n:
                row_cards      = self._active_cards[i:i+cols]
                row_max_blue   = max(_team_rows_n(c, 0) for c in row_cards)
                row_max_orange = max(_team_rows_n(c, 1) for c in row_cards)
                row_max        = max(row_max_blue, row_max_orange)
                # 4v4 rows: tight 2-3px gap between names; everything else: 5px gap
                row_player_h   = 15 if row_max >= 4 else 18
                row_h          = (d["name_h"] + d["meta_h"]
                                  + row_max * row_player_h
                                  + d["footer_h"])
                for col, card in enumerate(row_cards):
                    card["y"]         = y
                    card["height"]    = row_h
                    card["grid_col"]  = col
                    card["grid_cols"] = cols
                    card["_max_w"]    = col_w
                    card["_cbr"]      = row_max_blue
                    card["_cor"]      = row_max_orange
                y += row_h + pad
                i += cols
        self._total_h = y
        self.canvas.configure(scrollregion=(0, 0, 0, self._total_h))
        self._schedule_redraw()
        self._update_recent_replays()

    def _finish_loading(self):
        self._loading = False
        self._apply_filters()
        # Sweep for orphaned cache/upload entries left by deleted replays.
        # Runs in a background thread — no MD5 hashing, negligible overhead.
        self.after(500, self._cleanup_orphans)
        self.after(1000, self._enforce_replay_limit)

    def _schedule_filter(self):
        if hasattr(self, "_filter_id") and self._filter_id:
            self.after_cancel(self._filter_id)
        self._filter_id = self.after(300, self._apply_filters)

    def _open_date_picker(self):
        picker = DateRangePicker(self, self._filt_from, self._filt_to,
                                 on_apply=self._apply_filters)
        picker.update_idletasks()
        btn = self._date_btn
        x = btn.winfo_rootx()
        y = btn.winfo_rooty() + btn.winfo_height() + 4
        picker.geometry(f"+{x}+{y}")
        picker.focus_set()

    def _update_date_btn(self):
        f = self._filt_from.get().strip()
        t = self._filt_to.get().strip()
        if f and t:
            self._date_btn.configure(text=f"{f}  –  {t}")
        elif f:
            self._date_btn.configure(text=f"{f}  –  …")
        else:
            self._date_btn.configure(text="Any date")
        self._schedule_filter()

    def _clear_filters(self):
        self._filt_type.set("All")
        self._filt_mode.set("All")
        self._filt_from.set("")
        self._filt_to.set("")
        self._filt_search.set("")
        self._filt_name.set("")
        self._apply_filters()

    def _rebuild_positions(self):
        if self._rebuild_id:
            self.after_cancel(self._rebuild_id)
        self._rebuild_id = self.after(250, self._do_rebuild)

    def _do_rebuild(self):
        self._rebuild_id = None
        self._apply_filters()

    def _extend_positions(self, new_cards: list):
        self._apply_filters(sort=True)

    def _on_canvas_resize(self):
        if self._resize_id is not None:
            self.after_cancel(self._resize_id)
        # Short debounce — text measurements are cached so recalc is fast
        # enough to show live card reflow as the window is dragged
        self._resize_id = self.after(30, self._do_resize)

    def _do_resize(self):
        self._resize_id = None
        # sort=False: card order hasn't changed, just column layout needs recalc
        self._apply_filters(sort=False)

    def _toggle_compact(self):
        global _COMPACT
        _COMPACT = not _COMPACT
        self.compact_btn.configure(
            text="Normal" if _COMPACT else "Compact",
            fg_color=C_SCORE_COL if _COMPACT else "transparent",
            text_color=C_NAME)
        self.config_data["compact_mode"] = _COMPACT
        save_config(self.config_data)
        for card in self._cards:
            card["height"] = card_height(card.get("info"))
        self._apply_filters()

    # ── load + parse ──────────────────────────────────────────────────────────

    def _add_detected_card(self, filename: str, folder: str):
        """Add a watchdog-detected replay to _cards so Recent Replays updates."""
        if filename in self._card_index:
            return  # already present
        path = Path(folder) / filename
        if not path.exists():
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        info = load_cached(filename)
        needs_parse = (info is None or "team_size" not in info
                       or info.get("_v") != CACHE_VERSION)
        card = {
            "filename": filename,
            "path":     path,
            "mtime":    mtime,
            "info":     info if not needs_parse else None,
            "type":     replay_type(info) if info and not needs_parse else "",
            "uploaded": filename in self.uploaded,
            "bc_id":    load_upload_ids().get(filename, ""),
            "height":   card_height(info if not needs_parse else None),
            "parsing":  needs_parse,
            "failed":   False,
            "y":        0,
        }
        self._append_cards([card])
        # If not yet parsed, queue it through the proper parse worker so that
        # _finish_parse is called and the card updates out of "parsing..." state.
        if needs_parse:
            self._start_parse_worker([path], self._parse_gen)

    def _load_replays_for_main(self):
        """Called once on startup so Recent Replays shows without visiting the Replays page."""
        if self._cards:
            return  # already loaded (user navigated to replays page first)
        folder = self.config_data.get("demos_folder", "").strip()
        if not folder or not Path(folder).is_dir():
            return  # no folder configured yet
        self._load_replays()

    def _load_replays(self):
        self._parse_gen  += 1
        self._parse_done  = 0
        self._detail_gen += 1          # cancel any running detail worker
        self._parse_queue.clear()
        self._detail_queue.clear()
        self._cards.clear()
        self._card_index.clear()
        self._total_h = _dims()["card_pad"]
        self.canvas.delete("all")
        self.canvas.configure(scrollregion=(0, 0, 0, 0))
        self.canvas.yview_moveto(0)
        self.replays_title.configure(text="Replays  (scanning…)")

        folder = self.config_data.get("demos_folder", "").strip()
        if not folder or not Path(folder).is_dir():
            self.replays_title.configure(text="Replays")
            cw = self.canvas.winfo_width() or 400
            self.canvas.create_text(
                cw // 2, 80,
                text="Missing replay folder path",
                anchor="center", fill="#e06060",
                font=("Segoe UI", 14, "bold"))
            self.canvas.create_text(
                cw // 2, 108,
                text="Open Settings ⚙ and choose your Rocket League demos folder.",
                anchor="center", fill=_c(C_DIM),
                font=("Segoe UI", 11))
            return

        gen = self._parse_gen

        def worker():
            paths = sorted(
                ((p, p.stat().st_mtime) for p in Path(folder).glob("*.replay")
                 if p.is_file()),
                key=lambda x: x[1], reverse=True,
            )
            total = len(paths)
            self.after(0, lambda: self.replays_title.configure(
                text=f"Replays  ({total})"))

            upload_ids  = load_upload_ids()
            to_parse: list[Path] = []
            batch: list[dict] = []
            for i, (path, mtime) in enumerate(paths):
                info = load_cached(path.name)
                needs_parse = (info is None or "team_size" not in info
                               or info.get("_v") != CACHE_VERSION)
                if needs_parse:
                    to_parse.append(path)
                batch.append({
                    "filename": path.name,
                    "path":     path,
                    "mtime":    mtime,
                    "info":     info,
                    "type":     replay_type(info) if info else "",
                    "uploaded": path.name in self.uploaded,
                    "bc_id":    upload_ids.get(path.name, ""),
                    "height":   card_height(info),
                    "parsing":  needs_parse,
                    "failed":   False,
                    "y":        0,
                })
                if len(batch) >= 40:
                    snap = list(batch); batch.clear()
                    self.after(0, lambda s=snap: self._append_cards(s))

            if batch:
                self.after(0, lambda s=list(batch): self._append_cards(s))

            if to_parse:
                self.after(0, lambda pp=to_parse, g=gen: self._start_parse_worker(pp, g))

            self.after(0, self._finish_loading)

        self._loading = True
        threading.Thread(target=worker, daemon=True).start()

    def _append_cards(self, new_cards: list):
        start = len(self._cards)
        self._cards.extend(new_cards)
        for i, card in enumerate(new_cards):
            self._card_index[card["filename"]] = start + i
        if self._loading and not _COMPACT:
            # fast incremental append — skip full re-filter on every batch
            pad = _dims()["card_pad"]
            y = self._total_h
            for card in new_cards:
                card["y"] = y
                y += card["height"] + pad
            self._total_h = y
            self._active_cards.extend(new_cards)
            self.canvas.configure(scrollregion=(0, 0, 0, self._total_h))
        elif self._current_page == "replays":
            # Only re-layout when the replays page (and its canvas) is
            # actually visible.  If the user is on another page,
            # canvas.winfo_width() returns 1 and _apply_filters would assign
            # grid_cols=1 / wrong _max_w to every card.  _show_replays calls
            # update_idletasks() + _apply_filters() when the user navigates
            # back, so we can safely skip it here.
            self._apply_filters(sort=not self._loading)
        self._schedule_redraw()

    def _start_parse_worker(self, paths: list, gen: int):
        self._parse_queue = list(paths)
        self._parse_next(gen)

    def _remove_card(self, filename: str):
        """Remove a card by filename and clean up its cache entry."""
        idx = self._card_index.pop(filename, None)
        if idx is None:
            return
        self._cards.pop(idx)
        # Rebuild index for all cards that shifted down
        self._card_index = {c["filename"]: i for i, c in enumerate(self._cards)}
        (CACHE_DIR / (filename + ".json")).unlink(missing_ok=True)
        self._rebuild_positions()

    def _parse_next(self, gen: int):
        """Parse one replay with rrrocket; saves only card fields. Next starts when done."""
        if not self._parse_queue or self._parse_gen != gen:
            return
        path = self._parse_queue.pop(0)

        # File may have been deleted since the folder was scanned — skip and
        # remove its card rather than handing a missing file to rrrocket.
        if not path.exists():
            self._remove_card(path.name)
            self._parse_next(gen)
            return

        def do_work():
            info = parse_card_data(path)
            if info:
                save_cache(path.name, info)
            self.after(0, lambda f=path.name, i=info, g=gen: self._finish_parse(f, i, g))

        threading.Thread(target=do_work, daemon=True).start()

    def _finish_parse(self, filename: str, info: dict | None, gen: int, partial: bool = False):
        idx = self._card_index.get(filename)
        if idx is not None:
            card = self._cards[idx]
            if not partial:
                card["parsing"] = False
            if info is not None:
                card["info"] = info
                card["type"] = replay_type(info)
            elif not partial:
                # Parse returned nothing — file is corrupt or unreadable
                if Path(card.get("path", "")).exists():
                    card["failed"] = True
                    card["info"]   = {"_failed": True, "_corrupt": True}
                    self.after(0, self._log,
                               f"[parse] could not read replay: {filename}", "red")
            # Always re-sort after every parse (success or fail) so the final
            # sort fires even when the last card(s) in the queue fail to parse.
            self._rebuild_positions()
            self._schedule_redraw()
        # only advance queue after the full rrrocket parse is done
        if not partial:
            self._parse_done += 1
            if self._parse_done <= 100:
                self._parse_next(gen)          # first 100: full speed
            else:
                self.after(150, self._parse_next, gen)  # rest: ~6/s

    # ── startup background cache ──────────────────────────────────────────────

    def _bg_cache_replays(self):
        if self._bg_cache_busy:
            return
        if not RATTLETRAP.exists():
            return
        folder = self.config_data.get("demos_folder", "").strip()
        if not folder or not Path(folder).is_dir():
            return

        def worker():
            self._bg_cache_busy = True
            self._cache_progress_line = None
            try:
                all_replays = sorted(Path(folder).glob("*.replay"),
                                     key=lambda f: f.stat().st_mtime, reverse=True)
                to_parse = []
                for p in all_replays:
                    c = load_cached(p.name)
                    if c is None or c.get("_v") != CACHE_VERSION:
                        to_parse.append(p)
                total = len(to_parse)
                if not total:
                    return
                self.after(0, lambda: self._update_dl_tracked(
                    "_cache_progress_line", f"[cache] Parsed 0 / {total:,}…"))
                done = 0
                for path in to_parse:
                    if not self._bg_cache_busy:
                        break
                    info = parse_card_data(path)
                    if info:
                        save_cache(path.name, info)
                    done += 1
                    self.after(0, lambda d=done, t=total: self._update_dl_tracked(
                        "_cache_progress_line",
                        f"[cache] Parsed {d:,} / {t:,}" + ("  ✓" if d == t else "…")))
            finally:
                self._bg_cache_busy = False

        threading.Thread(target=worker, daemon=True).start()

    # ── canvas click ──────────────────────────────────────────────────────────

    def _on_recent_click(self, event):
        for y0, y1, card in getattr(self, '_recent_hit_rects', []):
            if y0 <= event.y <= y1:
                if not card.get('parsing'):
                    self._detail_back = "main"
                    self._show_detail(card)
                return
        self._show_replays()

    def _on_canvas_click(self, event):
        cy = self.canvas.canvasy(event.y)
        cw = self.canvas.winfo_width()
        for card in self._active_cards:
            if card["y"] <= cy <= card["y"] + card["height"]:
                cols = card.get("grid_cols", 1)
                if cols > 1:
                    col   = card.get("grid_col", 0)
                    gap   = _dims()["card_pad"]
                    col_w = (cw - 2 * CARD_MARGIN_X - (cols - 1) * gap) // cols
                    if not _COMPACT:
                        col_w = min(col_w, card.get("_max_w", col_w))
                    x0    = CARD_MARGIN_X + col * (col_w + gap)
                    x1    = x0 + col_w
                    if not (x0 <= event.x <= x1):
                        continue
                # Check quick-upload button before opening detail
                btn = card.get("_upload_btn")
                if btn and not card.get("_uploading"):
                    bx0, by0, bx1, by1 = btn
                    if bx0 <= event.x <= bx1 and by0 <= cy <= by1:
                        self._quick_upload_from_card(card)
                        return
                if not card.get("parsing"):
                    self._detail_back = "replays"
                    self._show_detail(card)
                return

    def _quick_upload_from_card(self, card: dict):
        """Upload a replay directly from the card without opening the detail page."""
        if card.get("_uploading") or card.get("bc_id"):
            return
        if not self.config_data.get("api_key", "").strip():
            self._log("[upload] No API key configured.", "red")
            return
        card["_uploading"] = True
        self._schedule_redraw()

        def worker():
            def on_status(name, status):
                self.after(0, self._log, f"[upload] {name}: {status}")
                if status == "failed":
                    card["_uploading"] = False
                    self.after(0, self._schedule_redraw)
            def on_bc_id(bc_id, fn=card["filename"]):
                save_upload_id(fn, bc_id)
                card["_uploading"] = False
                self.after(0, self._set_card_bc_id, fn, bc_id)
            upload(card["path"], self.config_data, self.uploaded, on_status,
                   force=True, on_bc_id=on_bc_id)

        threading.Thread(target=worker, daemon=True).start()

    # ── detail page ───────────────────────────────────────────────────────────

    def _build_detail_page(self):
        self.detail_page  = ctk.CTkFrame(self, fg_color="transparent")
        self._current_card: dict | None = None

        header = ctk.CTkFrame(self.detail_page, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(14, 2))

        # ── row 1: back button + action buttons ───────────────────────────────
        top_row = ctk.CTkFrame(header, fg_color="transparent")
        top_row.pack(fill="x")
        acts = ctk.CTkFrame(top_row, fg_color="transparent")
        acts.pack(side="right")
        _btn_kw = dict(height=28, fg_color="transparent", border_width=1,
                       border_color=C_BORDER, text_color=("gray10", "gray90"))
        self._btn_upload = ctk.CTkButton(acts, text="Upload", width=70,
                                         command=self._detail_upload, **_btn_kw)
        self._btn_upload.pack(side="left", padx=(0, 4))
        self._btn_bc = ctk.CTkButton(acts, text="Ballchasing", width=96,
                                     font=ctk.CTkFont(size=13, underline=True),
                                     command=self._open_on_ballchasing, **_btn_kw)
        self._btn_bc.pack(side="left", padx=(0, 4))
        ctk.CTkButton(acts, text="Rename", width=70,
                      command=self._detail_rename, **_btn_kw).pack(side="left", padx=(0, 4))
        ctk.CTkButton(acts, text="Copy", width=60,
                      command=self._detail_copy, **_btn_kw).pack(side="left", padx=(0, 4))
        ctk.CTkButton(acts, text="Delete", width=65,
                      fg_color="transparent", border_width=1, border_color="#7a2020",
                      text_color="#e06060",
                      command=self._detail_delete).pack(side="left")

        # ── row 2: meta info ──────────────────────────────────────────────────
        self.detail_meta = ctk.CTkLabel(header, text="",
                                        font=ctk.CTkFont(size=13),
                                        text_color=C_DATE, anchor="w")
        self.detail_meta.pack(fill="x", pady=(4, 0))

        self.detail_content = ctk.CTkScrollableFrame(self.detail_page,
                                                     fg_color="transparent")
        self.detail_content.pack(fill="both", expand=True, padx=20, pady=(0, 16))

    def _show_detail(self, card: dict):
        self._current_card = card
        already = card.get("uploaded") or card["filename"] in self.uploaded
        self._btn_upload.configure(state="normal",
                                   text="Uploaded" if already else "Upload")
        bc_id = load_upload_ids().get(card["filename"], "")
        self._btn_bc.configure(state="normal" if bc_id else "disabled",
                               text="Ballchasing")

        cached  = load_cached(card["filename"])
        bc_info = load_bc_stats(bc_id)

        fn = card["filename"]

        # 1. Render immediately with whatever we have
        if cached:
            if not bc_id:
                self._log(f"[detail] {fn} — no Ballchasing ID saved, showing upload button")
            self._render_detail(cached, card, bc_info,
                                show_upload_btn=(not bc_id))

        if bc_id and bc_info is None:
            # bc_id known but stats not cached yet — fetch with retries (Ballchasing
            # may still be processing a freshly uploaded replay)
            self._log(f"[detail] {fn} — fetching stats from Ballchasing…")
            def _fetch(c=card, ci=cached, bid=bc_id):
                api_key = self.config_data.get("api_key", "")
                bc = None
                for attempt in range(6):   # try up to ~30s
                    bc = fetch_bc_stats(bid, api_key)
                    if bc:
                        break
                    if attempt < 5:
                        self.after(0, self._log,
                                   f"[detail] {c['filename']} — stats not ready yet, retrying… ({attempt+1}/6)")
                        time.sleep(5)
                if self._current_card and self._current_card["filename"] == c["filename"]:
                    if bc:
                        self.after(0, self._log, f"[detail] {c['filename']} — stats loaded ✓")
                    else:
                        self.after(0, self._log,
                                   f"[detail] {c['filename']} — could not load stats", "red")
                    self.after(0, lambda: self._render_detail(ci, c, bc,
                                                              show_upload_btn=(bc is None)))
            threading.Thread(target=_fetch, daemon=True).start()


        if not cached:
            # No header cache — parse first, then re-enter the flow above
            self._render_parsing(card)
            def _parse(c=card):
                info = parse_card_data(c["path"])
                if info:
                    save_cache(c["filename"], info)
                    self.after(0, lambda i=info: self._update_card_info(c, i))
                if self._current_card and self._current_card["filename"] == c["filename"]:
                    self.after(0, lambda: self._show_detail(c))
            threading.Thread(target=_parse, daemon=True).start()

    def _update_card_info(self, card: dict, info: dict):
        card["info"]   = info
        card["type"]   = replay_type(info)
        # Do NOT overwrite card["height"] — relayout owns it.
        self._rebuild_positions()
        self._schedule_redraw()

    def _render_parsing(self, card: dict):
        for w in self.detail_content.winfo_children():
            w.destroy()
        self.main_page.pack_forget()
        self.settings_page.pack_forget()
        self.replays_page.pack_forget()
        self.detail_page.pack(fill="both", expand=True)
        self.nav_btn.configure(text="←")
        self._current_page = "detail"
        self.detail_meta.configure(text="")
        ctk.CTkLabel(self.detail_content, text="Parsing replay…",
                     text_color=C_DATE, font=ctk.CTkFont(size=16)
                     ).pack(expand=True, pady=60)

    def _bc_placeholder(self, parent, card: dict, show_upload_btn: bool):
        """Show either an upload button (not on BC) or a 'checking…' label (lookup in progress)."""
        if show_upload_btn:
            ctk.CTkLabel(parent, text="This replay hasn't been uploaded to Ballchasing yet.",
                         text_color=C_DATE, font=ctk.CTkFont(size=13)).pack(pady=(20, 8))
            ctk.CTkButton(parent, text="Upload & fetch stats", width=180, height=34,
                          command=lambda c=card: self._upload_and_fetch(c)
                          ).pack()
        else:
            lbl = ctk.CTkLabel(parent, text="Checking Ballchasing…",
                               text_color=C_DATE, font=ctk.CTkFont(size=13))
            lbl.pack(pady=20)
            dots = ["", ".", "..", "..."]
            def _tick(i=0, w=lbl):
                if not w.winfo_exists():
                    return
                w.configure(text=f"Checking Ballchasing{dots[i % 4]}")
                w.after(500, lambda: _tick(i + 1, w))
            _tick()

    def _upload_and_fetch(self, card: dict):
        """Upload replay to Ballchasing, save bc_id, fetch stats, re-render."""
        api_key = self.config_data.get("api_key", "").strip()
        if not api_key:
            messagebox.showwarning("No API key", "Set your Ballchasing API key in Settings first.")
            return

        def worker():
            bc_id_holder = []

            def on_bc_id(bid):
                bc_id_holder.append(bid)
                save_upload_id(card["filename"], bid)
                self.after(0, self._set_card_bc_id, card["filename"], bid)
                self.after(0, lambda b=bid: self._btn_bc.configure(
                    state="normal", text="Ballchasing"))

            def on_status(name, status):
                self.after(0, self._log, f"[upload] {name}: {status}")

            upload(card["path"], self.config_data, self.uploaded, on_status,
                   force=True, on_bc_id=on_bc_id)

            bc_id = bc_id_holder[0] if bc_id_holder else ""
            if not bc_id:
                # 409 without ID in body (or upload failed) — look up by rl_id
                cached_info = load_cached(card["filename"])
                rl_id = (cached_info or {}).get("rl_id", "")
                if not rl_id:
                    stem = Path(card["path"]).stem
                    if len(stem) >= 16:
                        rl_id = stem
                if rl_id:
                    bc_id = lookup_bc_id(rl_id, api_key, _log=lambda m, t=None: self.after(0, self._log, m, t))
                    if bc_id:
                        save_upload_id(card["filename"], bc_id)
                        self.after(0, lambda b=bc_id: self._btn_bc.configure(
                            state="normal", text="Ballchasing"))
            if bc_id:
                def _mark_uploaded(fn=card["filename"]):
                    self.uploaded.add(fn)
                    self._schedule_save_uploaded()
                    self._btn_upload.configure(text="Uploaded")
                self.after(0, _mark_uploaded)
                bc = fetch_bc_stats(bc_id, api_key)
                cached = load_cached(card["filename"])
                if self._current_card and self._current_card["filename"] == card["filename"]:
                    self.after(0, lambda: self._render_detail(cached, card, bc))
            else:
                self.after(0, self._log, f"[upload] Could not get bc_id for {card['filename']}")

        threading.Thread(target=worker, daemon=True).start()
        self._log(f"[upload] Uploading {card['filename']}…")

    def _render_detail(self, info: dict | None, card: dict, bc_info: dict | None = None, show_upload_btn: bool = False):
        for w in self.detail_content.winfo_children():
            w.destroy()

        self.main_page.pack_forget()
        self.settings_page.pack_forget()
        self.replays_page.pack_forget()
        self.detail_page.pack(fill="both", expand=True)
        self.nav_btn.configure(text="←")
        self._current_page = "detail"

        if info is None:
            msg = ("Could not parse this replay." if RATTLETRAP.exists()
                   else "rattletrap.exe not found — run start.bat to download it.")
            ctk.CTkLabel(self.detail_content, text=msg,
                         text_color=C_DATE).pack(pady=20)
            return

        if info.get("_failed"):
            ctk.CTkLabel(self.detail_content, text="Could not parse this replay.",
                         text_color=C_DATE).pack(pady=20)
            return

        # ── meta bar ─────────────────────────────────────────────────────────
        dur = info.get("duration")
        dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else ""
        raw_date = info.get("date", "")
        date_str = (raw_date[:16] + " UTC") if len(raw_date) >= 16 else raw_date[:10]
        tags = [s for s in [
            date_str, info.get("match_type", ""),
            map_display_name(info.get("map", "")), dur_str,
            "FORFEIT" if info.get("forfeit") else "",
        ] if s]
        self.detail_meta.configure(text="  |  ".join(tags))

        cc = _card_colors()
        CARD_BG   = ("gray95", "#1e1e1e")
        VAL_COLOR = ("gray10", "gray90")
        HDR_COLOR = ("gray40", "gray60")
        DIV_BG    = cc["divider"]

        # ── score banner ──────────────────────────────────────────────────────
        wt = info.get("winning_team", -1)
        banner = ctk.CTkFrame(self.detail_content, fg_color=CARD_BG, corner_radius=6)
        banner.pack(fill="x", pady=(4, 8))
        banner.grid_columnconfigure(1, weight=1)
        replay_name = info.get("replay_name") or card.get("replay_name") or ""
        if replay_name:
            ctk.CTkLabel(banner, text=replay_name,
                         font=ctk.CTkFont(size=16, weight="bold"), text_color=VAL_COLOR
                         ).grid(row=0, column=0, columnspan=3, padx=20, pady=(10, 2), sticky="w")
        score_row = 1 if replay_name else 0
        ctk.CTkLabel(banner,
                     text=f"BLUE{'  ▲' if wt == 0 else ''}",
                     text_color=cc["blue"],
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).grid(row=score_row, column=0, padx=20, pady=(2 if replay_name else 10, 10), sticky="w")
        ctk.CTkLabel(banner,
                     text=f"{info.get('team0',0)}  -  {info.get('team1',0)}",
                     font=ctk.CTkFont(size=32, weight="bold"), text_color=VAL_COLOR
                     ).grid(row=score_row, column=1, pady=(2 if replay_name else 10, 10))
        ctk.CTkLabel(banner,
                     text=f"ORANGE{'  ▲' if wt == 1 else ''}",
                     text_color=cc["orange"],
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).grid(row=score_row, column=2, padx=20, pady=(2 if replay_name else 10, 10), sticky="e")

        # ── tabbed stats ──────────────────────────────────────────────────────
        tabview = ctk.CTkTabview(self.detail_content, fg_color="transparent",
                                 segmented_button_fg_color=("gray85", "#1e1e1e"),
                                 segmented_button_selected_color=("gray70", "#2a2a2a"),
                                 segmented_button_selected_hover_color=("gray60", "#333333"),
                                 segmented_button_unselected_color=("gray85", "#1e1e1e"),
                                 segmented_button_unselected_hover_color=("gray80", "#252525"),
                                 text_color=VAL_COLOR)
        tabview.pack(fill="both", expand=True, pady=(4, 0))
        for t in ("Overall", "Boost", "Positioning", "Movement", "Ball"):
            tabview.add(t)

        def _tab(name):
            return tabview.tab(name)

        # ── Overall tab — single table so columns align across both teams ─────
        ot = _tab("Overall")
        blue   = [p for p in info["players"] if p["team"] == 0]
        orange = [p for p in info["players"] if p["team"] == 1]
        SCOLS   = ["Score", "Goals", "Assists", "Saves", "Shots"]
        HDR_OFF = 2

        tframe = ctk.CTkFrame(ot, fg_color=CARD_BG, corner_radius=6)
        tframe.pack(fill="x", pady=(0, 6))

        def _overall_team_rows(frame, team_players, color, row_start):
            ctk.CTkLabel(frame, text="Player", text_color=color,
                         font=ctk.CTkFont(size=13, weight="bold"), anchor="w"
                         ).grid(row=row_start, column=1, padx=(4, 8), pady=(8, 4), sticky="w")
            for i, hdr in enumerate(SCOLS):
                ctk.CTkLabel(frame, text=hdr, text_color=color,
                             font=ctk.CTkFont(size=12, weight="bold"), justify="right"
                             ).grid(row=row_start, column=HDR_OFF + i,
                                    padx=(6, 6), pady=(8, 4), sticky="e")
            tk.Frame(frame, bg=DIV_BG, height=1).grid(
                row=row_start + 1, column=0,
                columnspan=HDR_OFF + len(SCOLS), sticky="ew", padx=8)
            for r, p in enumerate(team_players, row_start + 2):
                url = tracker_url(p)
                if url:
                    ctk.CTkButton(frame, text="↗", width=26, height=22,
                                  fg_color="transparent", border_width=1,
                                  border_color=C_BORDER, font=ctk.CTkFont(size=11),
                                  text_color=("gray10", "gray90"),
                                  command=lambda u=url: webbrowser.open(u)
                                  ).grid(row=r, column=0, padx=(10, 2), pady=3, sticky="w")
                ctk.CTkLabel(frame, text=p["name"],
                             text_color=color, font=ctk.CTkFont(size=14), anchor="w"
                             ).grid(row=r, column=1, padx=(4, 8), pady=5, sticky="w")
                for i, key in enumerate(["score", "goals", "assists", "saves", "shots"]):
                    ctk.CTkLabel(frame, text=str(p.get(key, 0)),
                                 text_color=VAL_COLOR, font=ctk.CTkFont(size=14), anchor="e"
                                 ).grid(row=r, column=HDR_OFF + i, padx=(6, 6), pady=5, sticky="e")
            return row_start + 2 + len(team_players)

        next_row = _overall_team_rows(tframe, blue, cc["blue"], 0)
        tk.Frame(tframe, bg=DIV_BG, height=2).grid(
            row=next_row, column=0,
            columnspan=HDR_OFF + len(SCOLS), sticky="ew", padx=0, pady=(2, 0))
        _overall_team_rows(tframe, orange, cc["orange"], next_row + 1)

        # ── Boost tab ─────────────────────────────────────────────────────────
        bt = _tab("Boost")
        bc_players = []
        if bc_info:
            for color_key, team_num in [("blue", 0), ("orange", 1)]:
                for p in bc_info.get(color_key, {}).get("players", []):
                    bc_players.append({"name": p["name"], "team": team_num,
                                       "boost": p.get("stats", {}).get("boost", {})})
        if bc_players:
            bframe = ctk.CTkFrame(bt, fg_color=CARD_BG, corner_radius=6)
            bframe.pack(fill="x", pady=(0, 6))
            BCOLS = ["Player", "BPM", "Avg Boost", "Time\n0 boost", "Time\n100 boost",
                     "Collected", "Big pads", "Small pads",
                     "Used at SSL", "Overfill"]
            for col, hdr in enumerate(BCOLS):
                ctk.CTkLabel(bframe, text=hdr, text_color=HDR_COLOR,
                             font=ctk.CTkFont(size=12, weight="bold"),
                             justify="right"
                             ).grid(row=0, column=col,
                                    padx=(14 if col==0 else 6, 6), pady=(10, 4),
                                    sticky="w" if col==0 else "e")
            tk.Frame(bframe, bg=DIV_BG, height=1).grid(
                row=1, column=0, columnspan=len(BCOLS), sticky="ew", padx=8)
            for row, p in enumerate(bc_players, 2):
                b     = p["boost"]
                color = cc["blue"] if p["team"] == 0 else cc["orange"]
                vals  = [p["name"],
                         f"{b.get('bpm', 0):.0f}",
                         f"{b.get('avg_amount', 0):.1f}",
                         f"{b.get('time_zero_boost', 0):.1f}s",
                         f"{b.get('time_full_boost', 0):.1f}s",
                         f"{b.get('amount_collected', 0):.0f}",
                         f"{b.get('amount_collected_big', 0):.0f}",
                         f"{b.get('amount_collected_small', 0):.0f}",
                         f"{b.get('amount_used_while_supersonic', 0):.0f}",
                         f"{b.get('amount_overfill', 0):.0f}"]
                for col, val in enumerate(vals):
                    ctk.CTkLabel(bframe, text=val,
                                 text_color=color if col == 0 else VAL_COLOR,
                                 font=ctk.CTkFont(size=14)
                                 ).grid(row=row, column=col,
                                        padx=(14 if col==0 else 6, 6), pady=5,
                                        sticky="w" if col==0 else "e")
        else:
            self._bc_placeholder(bt, card, show_upload_btn)

        # ── Positioning tab ───────────────────────────────────────────────────
        pt = _tab("Positioning")
        bc_pos_players = []
        if bc_info:
            for color_key, team_num in [("blue", 0), ("orange", 1)]:
                for p in bc_info.get(color_key, {}).get("players", []):
                    bc_pos_players.append({"name": p["name"], "team": team_num,
                                           "pos": p.get("stats", {}).get("positioning", {})})
        if bc_pos_players:
            pframe = ctk.CTkFrame(pt, fg_color=CARD_BG, corner_radius=6)
            pframe.pack(fill="x", pady=(0, 6))
            PCOLS = ["Player", "Def. 1/3", "Neut. 1/3", "Off. 1/3",
                     "Def. 1/2", "Off. 1/2", "Behind ball", "Infront ball"]
            for col, hdr in enumerate(PCOLS):
                ctk.CTkLabel(pframe, text=hdr, text_color=HDR_COLOR,
                             font=ctk.CTkFont(size=12, weight="bold")
                             ).grid(row=0, column=col,
                                    padx=(14 if col==0 else 10, 10), pady=(10, 4),
                                    sticky="w" if col==0 else "e")
            tk.Frame(pframe, bg=DIV_BG, height=1).grid(
                row=1, column=0, columnspan=len(PCOLS), sticky="ew", padx=8)
            for row, p in enumerate(bc_pos_players, 2):
                pos   = p["pos"]
                color = cc["blue"] if p["team"] == 0 else cc["orange"]
                def _pp(pct_key, time_key):
                    pct = pos.get(pct_key, 0)
                    t   = pos.get(time_key, 0)
                    return f"{t:.1f}s\n({pct:.1f}%)"
                vals = [p["name"],
                        _pp("percent_defensive_third",  "time_defensive_third"),
                        _pp("percent_neutral_third",    "time_neutral_third"),
                        _pp("percent_offensive_third",  "time_offensive_third"),
                        _pp("percent_defensive_half",   "time_defensive_half"),
                        _pp("percent_offensive_half",   "time_offensive_half"),
                        _pp("percent_behind_ball",      "time_behind_ball"),
                        _pp("percent_infront_ball",     "time_infront_ball")]
                for col, val in enumerate(vals):
                    ctk.CTkLabel(pframe, text=val,
                                 text_color=color if col == 0 else VAL_COLOR,
                                 font=ctk.CTkFont(size=13), anchor="e",
                                 justify="right"
                                 ).grid(row=row, column=col,
                                        padx=(14 if col==0 else 10, 10), pady=4,
                                        sticky="w" if col==0 else "e")
        else:
            self._bc_placeholder(pt, card, show_upload_btn)

        # ── Movement tab ──────────────────────────────────────────────────────
        mt = _tab("Movement")
        bc_move_players = []
        if bc_info:
            for color_key, team_num in [("blue", 0), ("orange", 1)]:
                for p in bc_info.get(color_key, {}).get("players", []):
                    bc_move_players.append({"name": p["name"], "team": team_num,
                                            "mv": p.get("stats", {}).get("movement", {})})
        if bc_move_players:
            mframe = ctk.CTkFrame(mt, fg_color=CARD_BG, corner_radius=6)
            mframe.pack(fill="x", pady=(0, 6))
            MCOLS = ["Player", "Avg Speed", "Tot. Dist.", "Time slow",
                     "Time boost", "Time SSL", "Time ground",
                     "Time low air", "Time high air",
                     "Powerslide", "PS count"]
            for col, hdr in enumerate(MCOLS):
                ctk.CTkLabel(mframe, text=hdr, text_color=HDR_COLOR,
                             font=ctk.CTkFont(size=12, weight="bold"),
                             justify="right"
                             ).grid(row=0, column=col,
                                    padx=(14 if col==0 else 6, 6), pady=(10, 4),
                                    sticky="w" if col==0 else "e")
            tk.Frame(mframe, bg=DIV_BG, height=1).grid(
                row=1, column=0, columnspan=len(MCOLS), sticky="ew", padx=8)
            for row, p in enumerate(bc_move_players, 2):
                mv    = p["mv"]
                color = cc["blue"] if p["team"] == 0 else cc["orange"]
                vals  = [p["name"],
                         f"{mv.get('avg_speed', 0):.0f} uu/s",
                         f"{int(mv.get('total_distance', 0)):,}",
                         f"{mv.get('time_slow_speed', 0):.1f}s",
                         f"{mv.get('time_boost_speed', 0):.1f}s",
                         f"{mv.get('time_supersonic_speed', 0):.1f}s",
                         f"{mv.get('time_ground', 0):.1f}s",
                         f"{mv.get('time_low_air', 0):.1f}s",
                         f"{mv.get('time_high_air', 0):.1f}s",
                         f"{mv.get('time_powerslide', 0):.1f}s / {mv.get('avg_powerslide_duration', 0):.2f}s",
                         str(mv.get('count_powerslide', 0))]
                for col, val in enumerate(vals):
                    ctk.CTkLabel(mframe, text=val,
                                 text_color=color if col == 0 else VAL_COLOR,
                                 font=ctk.CTkFont(size=13)
                                 ).grid(row=row, column=col,
                                        padx=(14 if col==0 else 6, 6), pady=5,
                                        sticky="w" if col==0 else "e")
        else:
            self._bc_placeholder(mt, card, show_upload_btn)

        # ── Ball tab ──────────────────────────────────────────────────────────
        blt = _tab("Ball")
        bl_blue   = (bc_info or {}).get("blue",   {}).get("stats", {}).get("ball", {})
        bl_orange = (bc_info or {}).get("orange", {}).get("stats", {}).get("ball", {})
        if bl_blue or bl_orange:
            blframe  = ctk.CTkFrame(blt, fg_color=CARD_BG, corner_radius=6)
            blframe.pack(fill="x", pady=(0, 6))

            def _fmt_t(v): return f"{v:.1f}s"
            def _pct(v, total): return f"({100*v/total:.0f}%)" if total else ""

            poss_b = bl_blue.get("possession_time",  0)
            poss_o = bl_orange.get("possession_time", 0)
            side_b = bl_blue.get("time_in_side",     0)
            side_o = bl_orange.get("time_in_side",   0)
            poss_total = poss_b + poss_o or 1
            side_total = side_b + side_o or 1

            rows = [
                ("Possession time",
                 f"{_fmt_t(poss_b)} {_pct(poss_b, poss_total)}",
                 f"{_fmt_t(poss_o)} {_pct(poss_o, poss_total)}"),
                ("Ball in side",
                 f"{_fmt_t(side_b)} {_pct(side_b, side_total)}",
                 f"{_fmt_t(side_o)} {_pct(side_o, side_total)}"),
            ]

            # headers
            for col, (txt, clr) in enumerate([("", "gray60"),
                                              ("BLUE",   cc["blue"]),
                                              ("ORANGE", cc["orange"])]):
                ctk.CTkLabel(blframe, text=txt, text_color=clr,
                             font=ctk.CTkFont(size=12, weight="bold")
                             ).grid(row=0, column=col,
                                    padx=(14 if col==0 else 6, 6), pady=(10, 4),
                                    sticky="w" if col==0 else "e")
            tk.Frame(blframe, bg=DIV_BG, height=1).grid(
                row=1, column=0, columnspan=3, sticky="ew", padx=8)

            for r, (label, bval, oval) in enumerate(rows, 2):
                ctk.CTkLabel(blframe, text=label, text_color=HDR_COLOR,
                             font=ctk.CTkFont(size=13, weight="bold"), anchor="w"
                             ).grid(row=r, column=0, padx=(14, 6), pady=6, sticky="w")
                ctk.CTkLabel(blframe, text=bval, text_color=VAL_COLOR,
                             font=ctk.CTkFont(size=13), anchor="e"
                             ).grid(row=r, column=1, padx=6, pady=6, sticky="e")
                ctk.CTkLabel(blframe, text=oval, text_color=VAL_COLOR,
                             font=ctk.CTkFont(size=13), anchor="e"
                             ).grid(row=r, column=2, padx=(6, 14), pady=6, sticky="e")

            blframe.grid_columnconfigure(0, weight=1)
        else:
            self._bc_placeholder(blt, card, show_upload_btn)

        # ── filename ──────────────────────────────────────────────────────────
        ctk.CTkLabel(self.detail_content, text=card["filename"],
                     text_color=HDR_COLOR, font=ctk.CTkFont(size=11)
                     ).pack(pady=(6, 0))

    # ── detail action buttons ─────────────────────────────────────────────────

    def _open_on_ballchasing(self):
        card = self._current_card
        if not card:
            return
        bc_id = load_upload_ids().get(card["filename"], "")
        if bc_id:
            webbrowser.open(f"https://ballchasing.com/replay/{bc_id}")

    def _refresh_bc_btn(self, bc_id: str):
        self._btn_bc.configure(state="normal" if bc_id else "disabled")

    def _detail_upload(self):
        card = self._current_card
        if not card:
            return
        if not self.config_data.get("api_key"):
            messagebox.showerror("No API key", "Set your Ballchasing API key in Settings.")
            return
        self._btn_upload.configure(state="disabled", text="Uploading…")
        def worker():
            def on_status(name, status):
                if status == "uploading":
                    return
                if status in ("uploaded", "duplicate"):
                    def _mark(n=name):
                        self.uploaded.add(n)
                        save_uploaded(self.uploaded)
                        for c in self._cards:
                            if c["filename"] == n:
                                c["uploaded"] = True
                        if card.get("filename") == n:
                            card["uploaded"] = True
                        self._schedule_redraw()
                    self.after(0, _mark)
                    self.after(2000, self._fetch_quota)
                icons = {"uploaded": "✓", "duplicate": "=", "skipped": "–", "failed": "✗"}
                self.after(0, self._log,
                           f"{icons.get(status, '·')}  {name}  [{status}]")
                lbl = "Uploaded" if status in ("uploaded", "duplicate") else status.capitalize()
                self.after(0, lambda l=lbl: self._btn_upload.configure(state="normal", text=l))
            def on_bc_id(bc_id, fn=card["filename"]):
                save_upload_id(fn, bc_id)
                self.after(0, self._set_card_bc_id, fn, bc_id)
                self.after(0, self._refresh_bc_btn, bc_id)
                api_key = self.config_data.get("api_key", "")
                bc = fetch_bc_stats(bc_id, api_key)
                if bc and self._current_card and self._current_card["filename"] == fn:
                    cached = load_cached(fn)
                    self.after(0, lambda: self._render_detail(cached, self._current_card, bc))
            upload(card["path"], self.config_data, self.uploaded, on_status, force=True,
                   on_bc_id=on_bc_id)
        threading.Thread(target=worker, daemon=True).start()

    def _detail_rename(self):
        card = self._current_card
        if not card:
            return
        old_path = card["path"]
        new_name = simpledialog.askstring("Rename replay",
                                          "New filename (without extension):",
                                          initialvalue=old_path.stem,
                                          parent=self)
        if not new_name or not new_name.strip():
            return
        new_name = new_name.strip()
        if not new_name.endswith(".replay"):
            new_name += ".replay"
        new_path = old_path.parent / new_name
        if new_path.exists():
            messagebox.showerror("Rename failed", f"{new_name} already exists.")
            return
        try:
            old_name = card["filename"]
            old_path.rename(new_path)
            card["path"]     = new_path
            card["filename"] = new_name
            self._current_card = card
            # update card index
            if old_name in self._card_index:
                idx = self._card_index.pop(old_name)
                self._card_index[new_name] = idx
            # transfer uploaded status
            if old_name in self.uploaded:
                self.uploaded.discard(old_name)
                self.uploaded.add(new_name)
                save_uploaded(self.uploaded)
            # transfer Ballchasing link
            ids = load_upload_ids()
            if old_name in ids:
                ids[new_name] = ids.pop(old_name)
                _atomic_write_json(UPLOAD_IDS_FILE, ids)
            self._apply_filters()
        except Exception as e:
            messagebox.showerror("Rename failed", str(e))

    def _detail_copy(self):
        card = self._current_card
        if not card:
            return
        path_str = str(card["path"].resolve()).replace("'", "''")
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$fc = New-Object System.Collections.Specialized.StringCollection; "
            f"$fc.Add('{path_str}') | Out-Null; "
            "[System.Windows.Forms.Clipboard]::SetFileDropList($fc)"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._log(f"Copied to clipboard: {card['filename']}")
        except Exception as e:
            messagebox.showerror("Copy failed", str(e))

    def _detail_delete(self):
        card = self._current_card
        if not card:
            return
        if not messagebox.askyesno("Delete replay",
                                   f"Permanently delete:\n{card['filename']}?",
                                   icon="warning", parent=self):
            return
        try:
            fn = card["filename"]
            card["path"].unlink(missing_ok=True)
            self._cards = [c for c in self._cards if c["filename"] != fn]
            # rebuild card index from scratch since indices shifted
            self._card_index = {c["filename"]: i for i, c in enumerate(self._cards)}
            (CACHE_DIR / (fn + ".json")).unlink(missing_ok=True)
            # clean up uploaded tracking
            if fn in self.uploaded:
                self.uploaded.discard(fn)
                save_uploaded(self.uploaded)
            # clean up Ballchasing link
            ids = load_upload_ids()
            if fn in ids:
                ids.pop(fn)
                _atomic_write_json(UPLOAD_IDS_FILE, ids)
            self._current_card = None
            self._show_replays_from_detail()
            self._apply_filters()
        except Exception as e:
            messagebox.showerror("Delete failed", str(e))

    def _show_replays_from_detail(self):
        self.detail_page.pack_forget()
        self.replays_page.pack(fill="both", expand=True)
        self.nav_btn.configure(text="←")
        self._current_page = "replays"
        self._schedule_redraw()

    _PLAT_ALIASES = {
        "steam": "Steam", "epic": "Epic",
        "ps": "PS", "ps4": "PS", "ps5": "PS", "psn": "PS", "playstation": "PS",
        "xbox": "Xbox", "xb": "Xbox", "xb1": "Xbox", "xboxone": "Xbox",
        "other": "Other", "any": "Any",
    }

    def _parse_my_identities(self) -> list[tuple[str, str]]:
        """Return [(platform, name), ...] from the my_identities config string."""
        raw = self.config_data.get("my_identities", "").strip()
        if not raw:
            return []
        result = []
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            if ":" in token:
                plat, _, name = token.partition(":")
                plat_norm = self._PLAT_ALIASES.get(
                    plat.strip().lower(), plat.strip())
                result.append((plat_norm, name.strip()))
            else:
                result.append(("Any", token))
        return result

    def _save_ids_and_refresh(self):
        raw = self._stats_ids_entry.get().strip()
        ids_list = [x.strip() for x in raw.split(",") if x.strip()]
        if len(ids_list) > 4:
            messagebox.showwarning("Too many identities",
                                   "You can add at most 4 identities. "
                                   "Only the first 4 will be saved.")
            ids_list = ids_list[:4]
            self._stats_ids_entry.delete(0, "end")
            self._stats_ids_entry.insert(0, ", ".join(ids_list))
        self.config_data["my_identities"] = ", ".join(ids_list)
        save_config(self.config_data)
        self._run_me_search()

    def _run_me_search(self):
        self._autosave_ids_entry()
        identities = self._parse_my_identities()
        if not identities:
            for w in self.stats_content.winfo_children():
                w.destroy()
            ctk.CTkLabel(self.stats_content,
                         text='No accounts set.\nEnter your identities above and click Save.',
                         text_color=C_DIM, font=ctk.CTkFont(size=13),
                         justify="center").pack(pady=30)
            return
        for w in self.stats_content.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.stats_content, text="Scanning replays…",
                     text_color=C_DIM, font=ctk.CTkFont(size=13)).pack(pady=20)
        def worker():
            seen, games = set(), []
            upgraded: dict[tuple, str] = {}
            identity_labels: list[str] = []
            identity_tokens: list[str] = []
            id_names: dict[str, str] = dict(self.config_data.get("_identity_names") or {})
            excluded_set = set(self.config_data.get("_excluded_identities") or [])
            names_dirty = False

            for plat, name in identities:
                token = f"{plat}:{name}"
                is_excluded = token in excluded_set

                if not is_excluded:
                    result, resolved_id, display_name = \
                        self._compute_player_stats(name, plat)
                    for g in result:
                        if g["filename"] not in seen:
                            seen.add(g["filename"])
                            games.append(g)
                else:
                    _, resolved_id, display_name = \
                        self._compute_player_stats(name, plat)

                key = f"{plat}:{name}"
                if display_name and id_names.get(key) != display_name:
                    id_names[key] = display_name
                    names_dirty = True

                label_name = display_name or id_names.get(key)
                is_raw_id  = (name.isdigit() and 15 <= len(name) <= 20) or (
                    len(name) == 32 and all(c in "0123456789abcdef" for c in name.lower()))
                if label_name:
                    identity_labels.append(f"{label_name} ({plat})")
                elif not is_raw_id:
                    identity_labels.append(f"{name} ({plat})")
                else:
                    identity_labels.append(f"{plat}:…")
                identity_tokens.append(token)

                if (resolved_id
                        and resolved_id.lower() != name.lower()
                        and plat != "Any"
                        and len(resolved_id) >= 5
                        and resolved_id not in ("0", "00", "000")):
                    upgraded[(plat, name)] = resolved_id

            if upgraded:
                new_tokens = []
                for plat, name in identities:
                    rid = upgraded.get((plat, name))
                    curr_key = f"{plat}:{name}"
                    new_key  = f"{plat}:{rid}" if rid else curr_key
                    # migrate stored display name to new key
                    if rid and curr_key in id_names:
                        id_names[new_key] = id_names.pop(curr_key)
                        names_dirty = True
                    new_tokens.append(f"{plat}:{rid if rid else name}")
                new_str = ", ".join(new_tokens)
                def _apply_upgrade(s=new_str):
                    self.config_data["my_identities"] = s
                    self._sync_identities_entry(s)
                self.after(0, _apply_upgrade)

            if names_dirty:
                def _apply_names(n=id_names):
                    self.config_data["_identity_names"] = n
                    self._update_resolved_names_label()
                self.after(0, _apply_names)

            if upgraded or names_dirty:
                self.after(0, lambda: save_config(self.config_data))

            games.sort(key=lambda g: g["date"], reverse=True)
            self.after(0, lambda lbls=identity_labels, toks=identity_tokens:
                       self._render_player_stats("Me", games, lbls, toks))
        threading.Thread(target=worker, daemon=True).start()

    def _ids_to_display_str(self) -> str:
        """Convert raw my_identities (may contain hex IDs) to Platform:Name display form."""
        raw      = self.config_data.get("my_identities", "").strip()
        id_names = self.config_data.get("_identity_names") or {}
        if not raw:
            return ""
        parts = []
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            display = id_names.get(token)   # token is e.g. "Epic:36abb410..."
            if display:
                plat = token.split(":")[0]
                parts.append(f"{plat}:{display}")
            else:
                parts.append(token)
        return ", ".join(parts)

    def _sync_identities_entry(self, new_str: str):
        if hasattr(self, "_stats_ids_entry"):
            self._stats_ids_entry.delete(0, "end")
            self._stats_ids_entry.insert(0, self._ids_to_display_str())
        self._update_resolved_names_label()

    def _update_resolved_names_label(self):
        if not hasattr(self, "_resolved_names_lbl"):
            return
        identities = self._parse_my_identities()
        id_names   = self.config_data.get("_identity_names") or {}
        parts: list[str] = []
        for plat, name in identities:
            key = f"{plat}:{name}"
            display = id_names.get(key)
            if display:
                parts.append(f"{display} ({plat})")
            else:
                is_raw_id = (name.isdigit() and 15 <= len(name) <= 20) or (
                    len(name) == 32 and all(c in "0123456789abcdef" for c in name.lower()))
                if not is_raw_id:
                    parts.append(f"{name} ({plat})")
        if parts:
            self._resolved_names_lbl.configure(text="  ·  ".join(parts))
        else:
            self._resolved_names_lbl.configure(
                text="Click Me in Player Stats to resolve display names")

    # maps platform label → lowercase strings found in cache "platform" field
    _PLAT_NORM = {
        "Steam": {"steam"},
        "Epic":  {"epic"},
        "PS":    {"ps4", "ps5", "playstation"},
        "Xbox":  {"xbox", "xb1", "xboxone"},
    }
    _PLAT_KNOWN = {"steam", "epic", "ps4", "ps5", "playstation",
                   "xbox", "xb1", "xboxone"}

    def _plat_matches(self, pplat: str, platform: str) -> bool:
        if platform == "Any":   return True
        if platform == "Other": return pplat not in self._PLAT_KNOWN
        return pplat in self._PLAT_NORM.get(platform, set())

    def _compute_player_stats(self, name: str,
                              platform: str = "Any") -> tuple[list, str | None, str | None]:
        """Return (games, resolved_online_id, display_name).
        resolved_online_id is set whenever a stable platform ID was found so
        the caller can upgrade a display-name config entry to the stable ID.
        """
        games = []
        if not CACHE_DIR.exists():
            return games, None, None

        name_lower  = name.strip().lower()
        upload_ids  = load_upload_ids()
        cache_files = sorted(CACHE_DIR.glob("*.json"),
                             key=lambda f: f.stat().st_mtime, reverse=True)

        # ── Direct ID input detection ─────────────────────────────────────────
        # Steam64 / Xbox XUID: all-digit, 15-20 chars
        _is_numeric_id = name_lower.isdigit() and 15 <= len(name_lower) <= 20
        # Epic account UUID: exactly 32 lowercase hex chars (no dashes)
        _is_epic_uuid  = (len(name_lower) == 32
                          and all(c in "0123456789abcdef" for c in name_lower))
        is_direct_id   = _is_numeric_id or _is_epic_uuid

        # ── Resolve stable online_id from most recent matching replay ─────────
        # Works for all platforms — makes the search resilient to name changes.
        resolved_id = None
        if not is_direct_id and platform != "Any":
            for f in cache_files:
                try:
                    with open(f, encoding="utf-8") as fp:
                        info = json.load(fp)
                except Exception:
                    continue
                for p in info.get("players", []):
                    oid = p.get("online_id", "").strip()
                    if (p.get("name", "").strip().lower() == name_lower
                            and self._plat_matches(
                                p.get("platform", "").strip().lower(), platform)
                            and oid and oid not in ("0", "00", "000")
                            and len(oid) >= 5):
                        resolved_id = oid
                        break
                if resolved_id:
                    break

        def _matches(p: dict) -> bool:
            pplat = p.get("platform", "").strip().lower()
            oid   = p.get("online_id", "").strip()

            if is_direct_id:
                return oid == name_lower

            if resolved_id:
                # ID-based: immune to display-name changes
                return oid == resolved_id

            # Fallback: name + optional platform filter
            if p.get("name", "").strip().lower() != name_lower:
                return False
            return self._plat_matches(pplat, platform)

        first_display_name = None
        for f in cache_files:
            try:
                with open(f, encoding="utf-8") as fp:
                    info = json.load(fp)
            except Exception:
                continue
            players = info.get("players", [])
            if not players:
                continue
            p = next((p for p in players if _matches(p)), None)
            if p is None:
                continue
            if first_display_name is None:
                first_display_name = p.get("name", "").strip()
            filename = f.stem
            wt = info.get("winning_team", -1)
            if wt < 0:
                t0 = info.get("team0", 0)
                t1 = info.get("team1", 0)
                if t0 > t1:   wt = 0
                elif t1 > t0: wt = 1
            won = (wt == p.get("team", -2)) if wt >= 0 else None
            ts  = info.get("team_size", 0)
            mode_str = {1: "1v1", 2: "2v2", 3: "3v3", 4: "4v4"}.get(
                ts, f"{ts}v{ts}" if ts else "")
            games.append({
                "date":        (info.get("date") or "")[:10],
                "map":         map_display_name(info.get("map", "")),
                "mode":        mode_str,
                "won":         won,
                "score":       p.get("score",   0),
                "goals":       p.get("goals",   0),
                "assists":     p.get("assists", 0),
                "saves":       p.get("saves",   0),
                "shots":       p.get("shots",   0),
                "demos":       p.get("demos",   0),
                "demoed":      p.get("demoed",  0),
                "bpm":         (p.get("boost") or {}).get("bpm", 0),
                "bc_id":       upload_ids.get(filename, ""),
                "filename":    filename,
                "match_type":  info.get("match_type", ""),
                "playlist_id": info.get("playlist_id", 0),
            })
        return games, resolved_id, first_display_name

    def _render_player_stats(self, name: str, games: list,
                             identity_labels: list | None = None,
                             identity_tokens: list | None = None):
        self._stats_all_games       = games
        self._stats_name            = name
        self._stats_identity_labels = identity_labels or []
        self._stats_identity_tokens = identity_tokens or []
        self._stats_mode_var        = tk.StringVar(value="2v2")
        self._stats_period_var      = tk.StringVar(value="30d")
        self._rebuild_stats_display()

    def _rebuild_stats_display(self):
        for w in self.stats_content.winfo_children():
            w.destroy()

        games = self._stats_all_games
        name  = self._stats_name

        if not games:
            ctk.CTkLabel(self.stats_content,
                         text=f'No replays found for "{name}".\n'
                              'Make sure replays have finished parsing (no ⟳ spinner on cards).',
                         text_color=C_DIM, font=ctk.CTkFont(size=13),
                         justify="center").pack(pady=30)
            return

        # ── mode filter ───────────────────────────────────────────────────────
        mode_row = ctk.CTkFrame(self.stats_content, fg_color="transparent")
        mode_row.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(mode_row, text="Mode:", text_color=C_DIM,
                     font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 6))
        ctk.CTkSegmentedButton(
            mode_row, values=["All", "1v1", "2v2", "3v3"],
            variable=self._stats_mode_var, font=ctk.CTkFont(size=12), height=26,
            command=lambda _: self._rebuild_stats_display(),
        ).pack(side="left")

        # ── identity chips ────────────────────────────────────────────────────
        labels = getattr(self, "_stats_identity_labels", [])
        tokens = getattr(self, "_stats_identity_tokens", [])
        excluded_set = set(self.config_data.get("_excluded_identities") or [])
        id_row = ctk.CTkFrame(self.stats_content, fg_color="transparent")
        id_row.pack(fill="x", pady=(0, 6))
        for lbl, tok in zip(labels, tokens):
            is_excl = tok in excluded_set
            # tracker.gg URL
            url = ""
            if " (" in lbl and lbl.endswith(")"):
                name_part = lbl[:lbl.rfind(" (")]
                plat_part = lbl[lbl.rfind(" (")+2:-1]
                slug = {"Epic": "epic", "Steam": "steam", "PS": "psn",
                        "Xbox": "xbl", "XBL": "xbl"}.get(plat_part, plat_part.lower())
                url = (f"https://rocketleague.tracker.network/rocket-league"
                       f"/profile/{slug}/{name_part}/overview")
            chip = ctk.CTkFrame(id_row, border_width=1,
                                border_color=C_BORDER,
                                fg_color=("#d8d8d8", "#2a2a2a") if not is_excl else "transparent",
                                corner_radius=6)
            chip.pack(side="left", padx=(0, 6))
            # name / toggle button
            name_color = C_NAME if not is_excl else C_DIM
            ctk.CTkButton(chip, text=lbl, height=26, width=0,
                          fg_color="transparent", text_color=name_color,
                          hover_color=C_CARD, font=ctk.CTkFont(size=11),
                          command=lambda t=tok: self._toggle_identity(t)
                          ).pack(side="left", padx=(6, 2))
            # tracker link button
            if url:
                ctk.CTkButton(chip, text="↗", width=22, height=26,
                              fg_color="transparent", text_color=C_DIM,
                              hover_color=C_CARD, font=ctk.CTkFont(size=11),
                              command=lambda u=url: webbrowser.open(u)
                              ).pack(side="left", padx=(0, 2))
            # remove button
            ctk.CTkButton(chip, text="✕", width=22, height=26,
                          fg_color="transparent", text_color=C_DIM,
                          hover_color=C_CARD, font=ctk.CTkFont(size=11),
                          command=lambda t=tok: self._remove_identity(t)
                          ).pack(side="left", padx=(0, 4))
        # edit-accounts pencil button
        ctk.CTkButton(id_row, text="✎", width=26, height=26,
                      fg_color="transparent", text_color=C_DIM,
                      hover_color=C_CARD, font=ctk.CTkFont(size=13),
                      command=self._show_id_edit_row).pack(side="left", padx=(2, 0))

        # ── apply filters ─────────────────────────────────────────────────────
        mode_f   = self._stats_mode_var.get()
        filtered = []
        for g in games:
            rt = replay_type({"match_type":  g.get("match_type", ""),
                              "playlist_id": g.get("playlist_id", 0),
                              "replay_name": ""})
            if rt in ("Private", "Tournament"):
                continue
            if mode_f != "All" and g["mode"] != mode_f:
                continue
            filtered.append(g)

        if not filtered:
            ctk.CTkLabel(self.stats_content,
                         text=f'No {mode_f} ranked/casual games found for "{name}".',
                         text_color=C_DIM, font=ctk.CTkFont(size=13),
                         justify="center").pack(pady=20)
            return

        def _period_games(period):
            if period == "All":
                return filtered
            today = _date.today()
            delta = {"7d": 6, "30d": 29, "1y": 364}.get(period, 0)
            cutoff = (today - timedelta(days=delta)).strftime("%Y-%m-%d")
            return [g for g in filtered if g["date"] >= cutoff]

        # ── aggregate block ───────────────────────────────────────────────────
        agg = ctk.CTkFrame(self.stats_content, fg_color=C_CARD, corner_radius=6)
        agg.pack(fill="x", pady=(0, 6))
        agg.columnconfigure(tuple(range(8)), weight=1)

        _agg_val_labels = {}

        def _agg_col(frame, col, key, label, value):
            ctk.CTkLabel(frame, text=label, text_color=C_DIM,
                         font=ctk.CTkFont(size=11)).grid(
                row=0, column=col, padx=12, pady=(10, 2), sticky="ew")
            lbl = ctk.CTkLabel(frame, text=value, text_color=C_SCORE,
                               font=ctk.CTkFont(size=16, weight="bold"))
            lbl.grid(row=1, column=col, padx=12, pady=(0, 10), sticky="ew")
            _agg_val_labels[key] = lbl

        def _refresh_agg(*_):
            pg = _period_games(self._stats_period_var.get())
            pn = len(pg)
            pw = sum(1 for g in pg if g["won"] is True)
            pl = sum(1 for g in pg if g["won"] is False)
            def pavg(k): return sum(g[k] for g in pg) / pn if pn else 0
            _agg_val_labels["games"].configure(text=str(pn))
            _agg_val_labels["wl"].configure(text=f"{pw} / {pl}")
            _agg_val_labels["winpct"].configure(
                text=f"{pw/pn*100:.0f}%" if pn else "—")
            _agg_val_labels["score"].configure(
                text=f"{pavg('score'):.0f}" if pn else "—")
            _agg_val_labels["goals"].configure(
                text=f"{pavg('goals'):.2f}" if pn else "—")
            _agg_val_labels["assists"].configure(
                text=f"{pavg('assists'):.2f}" if pn else "—")
            _agg_val_labels["saves"].configure(
                text=f"{pavg('saves'):.2f}" if pn else "—")
            _agg_val_labels["shots"].configure(
                text=f"{pavg('shots'):.2f}" if pn else "—")

        pg0 = _period_games(self._stats_period_var.get())
        pn0 = len(pg0)
        pw0 = sum(1 for g in pg0 if g["won"] is True)
        pl0 = sum(1 for g in pg0 if g["won"] is False)
        def pavg0(k): return sum(g[k] for g in pg0) / pn0 if pn0 else 0

        _agg_col(agg, 0, "games",   "Games",     str(pn0))
        _agg_col(agg, 1, "wl",      "W / L",     f"{pw0} / {pl0}")
        _agg_col(agg, 2, "winpct",  "Win %",
                 f"{pw0/pn0*100:.0f}%" if pn0 else "—")
        _agg_col(agg, 3, "score",   "Avg Score",
                 f"{pavg0('score'):.0f}" if pn0 else "—")
        _agg_col(agg, 4, "goals",   "Goals/g",
                 f"{pavg0('goals'):.2f}" if pn0 else "—")
        _agg_col(agg, 5, "assists", "Assists/g",
                 f"{pavg0('assists'):.2f}" if pn0 else "—")
        _agg_col(agg, 6, "saves",   "Saves/g",
                 f"{pavg0('saves'):.2f}" if pn0 else "—")
        _agg_col(agg, 7, "shots",   "Shots/g",
                 f"{pavg0('shots'):.2f}" if pn0 else "—")

        self._stats_period_var.trace_add("write", _refresh_agg)

        # ── win/loss graph ────────────────────────────────────────────────────
        self._build_stats_graph(self.stats_content, filtered, self._stats_period_var)

        # ── per-game table ────────────────────────────────────────────────────
        ctk.CTkLabel(self.stats_content, text="Recent games",
                     text_color=C_DIM, font=ctk.CTkFont(size=12, weight="bold"),
                     anchor="w").pack(fill="x", pady=(4, 2))
        self._build_stats_table_canvas(self.stats_content, filtered)

    # ── win/loss history graph ─────────────────────────────────────────────────
    def _build_stats_graph(self, parent, games, period_var):
        C_WIN  = "#4aaa88"
        C_LOSS = "#e06060"

        frame = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=6)
        frame.pack(fill="x", pady=(0, 6))

        top = ctk.CTkFrame(frame, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(top, text="Win / Loss history",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=C_DIM).pack(side="left")
        ctk.CTkSegmentedButton(
            top, values=["7d", "30d", "1y", "All"],
            variable=period_var, font=ctk.CTkFont(size=11), height=24,
        ).pack(side="right")

        cv = tk.Canvas(frame, height=120, bg=_c(C_CARD), highlightthickness=0)
        cv.pack(fill="x", padx=8, pady=(4, 10))

        def _bucket_key(g, gran):
            d = g["date"]
            if not d:
                return None
            if gran == "day":
                return d
            if gran == "week":
                try:
                    return datetime.strptime(d, "%Y-%m-%d").date().strftime("%G-W%V")
                except ValueError:
                    return None
            if gran == "month":
                return d[:7]   # YYYY-MM
            # quarter: YYYY-Q#
            try:
                mo = int(d[5:7])
                return f"{d[:4]}-Q{(mo - 1) // 3 + 1}"
            except (ValueError, IndexError):
                return None

        def _label(b, gran):
            if gran == "day":
                return b[5:]               # MM-DD
            if gran == "week":
                try:
                    dt = datetime.strptime(b + "-1", "%G-W%V-%u").date()
                    return dt.strftime("%m/%d")
                except ValueError:
                    return b[-3:]
            if gran == "month":
                try:
                    return datetime.strptime(b, "%Y-%m").strftime("%b '%y")
                except ValueError:
                    return b[5:]
            # quarter: "YYYY-Q#" → "Q1 '26"
            return f"{b[6:]} '{b[2:4]}"

        def _make_buckets(period):
            today = _date.today()
            if period == "7d":
                days = [today - timedelta(days=i) for i in range(6, -1, -1)]
                return [d.strftime("%Y-%m-%d") for d in days], "day"
            if period == "30d":
                days = [today - timedelta(days=i) for i in range(29, -1, -1)]
                return [d.strftime("%Y-%m-%d") for d in days], "day"
            if period == "1y":
                weeks = []
                for i in range(51, -1, -1):
                    weeks.append((today - timedelta(weeks=i)).strftime("%G-W%V"))
                return weeks, "week"
            # All — adaptive granularity based on span
            dates = sorted(g["date"] for g in games if g["date"])
            if not dates:
                return [], "day"
            try:
                oldest = datetime.strptime(dates[0], "%Y-%m-%d").date()
            except ValueError:
                return [], "day"
            span_days = (today - oldest).days + 1
            if span_days <= 60:                   # ≤ 2 months  → daily
                gran = "day"
            elif span_days <= 18 * 30:            # ≤ 18 months → weekly
                gran = "week"
            elif span_days <= 10 * 365:           # ≤ 10 years  → monthly
                gran = "month"
            else:                                 # > 10 years  → quarterly
                gran = "quarter"
            if gran == "day":
                buckets = [(oldest + timedelta(days=i)).strftime("%Y-%m-%d")
                           for i in range(span_days)]
                return buckets, "day"
            if gran == "week":
                oldest_mon = oldest - timedelta(days=oldest.weekday())
                buckets, cur = [], oldest_mon
                while cur <= today:
                    buckets.append(cur.strftime("%G-W%V"))
                    cur += timedelta(weeks=1)
                return buckets, "week"
            if gran == "month":
                buckets, yr, mo = [], oldest.year, oldest.month
                while (yr, mo) <= (today.year, today.month):
                    buckets.append(f"{yr:04d}-{mo:02d}")
                    mo += 1
                    if mo > 12:
                        mo, yr = 1, yr + 1
                return buckets, "month"
            # quarterly: bucket key = "YYYY-Q#"
            buckets, yr, mo = [], oldest.year, oldest.month
            q = (mo - 1) // 3
            while (yr, q) <= (today.year, (today.month - 1) // 3):
                buckets.append(f"{yr:04d}-Q{q+1}")
                q += 1
                if q > 3:
                    q, yr = 0, yr + 1
            return buckets, "quarter"

        def draw(e=None):
            cv.delete("all")
            cw, ch = cv.winfo_width(), cv.winfo_height()
            if cw <= 1 or ch <= 1:
                return
            buckets, gran = _make_buckets(period_var.get())
            if not buckets:
                return
            wins_map  = {b: 0 for b in buckets}
            loss_map  = {b: 0 for b in buckets}
            for g in games:
                k = _bucket_key(g, gran)
                if k not in wins_map:
                    continue
                if g["won"] is True:    wins_map[k] += 1
                elif g["won"] is False: loss_map[k]  += 1

            max_total = max((wins_map[b] + loss_map[b] for b in buckets), default=1) or 1
            nb        = len(buckets)
            ML, MR, MB, MT = 4, 4, 16, 6
            plot_w = cw - ML - MR
            plot_h = ch - MT - MB
            bar_w  = min(20, max(2, plot_w // nb - 2))

            for i, b in enumerate(buckets):
                w  = wins_map[b]
                lo = loss_map[b]
                total = w + lo
                if total == 0:
                    continue
                xc   = ML + int((i + 0.5) * plot_w / nb)
                x0, x1 = xc - bar_w // 2, xc + bar_w // 2
                bar_h  = max(1, int(total / max_total * plot_h))
                ybase  = MT + plot_h
                ytop   = ybase - bar_h
                win_h  = int(bar_h * w / total) if total else 0
                loss_h = bar_h - win_h
                if loss_h > 0:
                    cv.create_rectangle(x0, ybase - loss_h, x1, ybase,
                                        fill=C_LOSS, outline="")
                if win_h > 0:
                    cv.create_rectangle(x0, ytop, x1, ybase - loss_h,
                                        fill=C_WIN, outline="")

            # x-axis baseline
            cv.create_line(ML, MT + plot_h, cw - MR, MT + plot_h,
                           fill=_c(C_DIVIDER))

            # sparse x labels
            step = max(1, nb // 7)
            for i, b in enumerate(buckets):
                if i % step == 0 or i == nb - 1:
                    xc = ML + int((i + 0.5) * plot_w / nb)
                    cv.create_text(xc, ch - 2, text=_label(b, gran),
                                   fill=_c(C_DIM), anchor="s",
                                   font=("Segoe UI", 8))

        cv.bind("<Configure>", draw)
        period_var.trace_add("write", lambda *_: draw())

    # ── virtual-scroll game table ──────────────────────────────────────────────
    def _build_stats_table_canvas(self, parent, games):
        ROW_H  = 26
        HDR_H  = 28
        PAD_L  = 10
        # (header text, fixed_px [0=flexible], anchor)
        COLS = [
            ("Date",    88, "w"),
            ("Map",      0, "w"),
            ("Mode",    52, "c"),
            ("Score",   54, "e"),
            ("G",       28, "e"),
            ("A",       28, "e"),
            ("S",       28, "e"),
            ("Sh",      30, "e"),
            ("Result",  52, "e"),
            ("↗",       34, "c"),
        ]
        FIXED_W = sum(w for _, w, _ in COLS if w) + PAD_L + 8
        MAP_MIN = 120

        def col_xs(cw):
            map_w = max(MAP_MIN, cw - FIXED_W)
            xs, x = [], PAD_L
            for _, w, _ in COLS:
                col_w = w if w else map_w
                xs.append((x, col_w))
                x += col_w
            return xs

        outer = tk.Frame(parent, bg=_c(C_BG))
        outer.pack(fill="both", expand=True)

        # Header (non-scrolling canvas)
        hdr_cv = tk.Canvas(outer, height=HDR_H, bg=_c(C_CARD), highlightthickness=0)
        hdr_cv.pack(fill="x")
        tk.Frame(outer, bg=_c(C_DIVIDER), height=1).pack(fill="x")

        # Rows canvas + scrollbar
        body = tk.Frame(outer, bg=_c(C_BG))
        body.pack(fill="both", expand=True)
        vsb = tk.Scrollbar(body, orient="vertical")
        vsb.pack(side="right", fill="y")
        rows_cv = tk.Canvas(body, bg=_c(C_BG), highlightthickness=0,
                            yscrollcommand=vsb.set)
        rows_cv.pack(side="left", fill="both", expand=True)
        vsb.config(command=rows_cv.yview)

        n       = len(games)
        total_h = n * ROW_H

        def draw_header(e=None):
            hdr_cv.delete("all")
            cw = hdr_cv.winfo_width()
            if cw <= 1:
                return
            xs  = col_xs(cw)
            ym  = HDR_H // 2
            for (x, w), (name, _, anch) in zip(xs, COLS):
                if   anch == "w": tx, ta = x + 2,     "w"
                elif anch == "e": tx, ta = x + w - 2,  "e"
                else:             tx, ta = x + w // 2, "center"
                hdr_cv.create_text(tx, ym, text=name, fill=_c(C_DIM),
                                   anchor=ta, font=("Segoe UI", 10, "bold"))

        def draw_rows(e=None):
            rows_cv.delete("row")
            cw = rows_cv.winfo_width()
            ch = rows_cv.winfo_height()
            if cw <= 1:
                return
            rows_cv.configure(scrollregion=(0, 0, cw, total_h))
            xs    = col_xs(cw)
            y_top = rows_cv.canvasy(0)
            y_bot = rows_cv.canvasy(ch)
            first = max(0, int(y_top / ROW_H) - 20)
            last  = min(n, int(y_bot / ROW_H) + 22)

            for i in range(first, last):
                g  = games[i]
                y0 = i * ROW_H
                y1 = y0 + ROW_H
                ym = y0 + ROW_H // 2
                bg = _c(C_CARD) if i % 2 else _c(C_BG)
                rows_cv.create_rectangle(0, y0, cw, y1, fill=bg, outline="",
                                         tags="row")
                if g["won"] is True:    res_txt, res_fg = "Win",  _c(C_CHECK)
                elif g["won"] is False: res_txt, res_fg = "Loss", _c(C_BORDER_FAIL)
                else:                   res_txt, res_fg = "—",    _c(C_DIM)

                vals = [g["date"], g["map"] or "—", g["mode"],
                        str(g["score"]), str(g["goals"]), str(g["assists"]),
                        str(g["saves"]),  str(g["shots"]), res_txt,
                        "↗" if g["bc_id"] else ""]

                for j, ((x, w), (_, _, anch)) in enumerate(zip(xs, COLS)):
                    txt = vals[j]
                    if not txt:
                        continue
                    if j == 8:   fg = res_fg
                    elif j == 9: fg = _c(C_BLUE)
                    else:        fg = _c(C_NAME)
                    if   anch == "w": tx, ta = x + 2,     "w"
                    elif anch == "e": tx, ta = x + w - 2,  "e"
                    else:             tx, ta = x + w // 2, "center"
                    rows_cv.create_text(tx, ym, text=txt, fill=fg, anchor=ta,
                                        font=("Segoe UI", 11), tags="row")

        def on_scroll(event):
            rows_cv.yview_scroll(-1 * (event.delta // 120), "units")
            draw_rows()

        def on_configure(e):
            draw_header()
            draw_rows(e)

        def on_click(event):
            y   = rows_cv.canvasy(event.y)
            row = int(y / ROW_H)
            if 0 <= row < n:
                cw = rows_cv.winfo_width()
                x0 = col_xs(cw)[9][0]
                if event.x >= x0 and games[row]["bc_id"]:
                    webbrowser.open(
                        f"https://ballchasing.com/replay/{games[row]['bc_id']}")

        hdr_cv.bind("<Configure>",   draw_header)
        rows_cv.bind("<Configure>",  on_configure)
        rows_cv.bind("<MouseWheel>", on_scroll)
        rows_cv.bind("<Button-1>",   on_click)
        rows_cv.bind("<Button-4>",
                     lambda e: (rows_cv.yview_scroll(-1, "units"), draw_rows()))
        rows_cv.bind("<Button-5>",
                     lambda e: (rows_cv.yview_scroll( 1, "units"), draw_rows()))

    # ── settings page ─────────────────────────────────────────────────────────

    def _build_settings_page(self):
        self.settings_page = ctk.CTkScrollableFrame(self, fg_color="transparent")
        pg = self.settings_page
        pg.grid_columnconfigure(1, weight=1)

        def _section(row, title):
            """Section header: coloured label + full-width divider line."""
            f = ctk.CTkFrame(pg, fg_color="transparent")
            f.grid(row=row, column=0, columnspan=3, sticky="ew",
                   padx=20, pady=(18, 4))
            f.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(f, text=title, text_color="#4a8cc4",
                         font=ctk.CTkFont(size=12, weight="bold")
                         ).grid(row=0, column=0, sticky="w", padx=(0, 10))
            tk.Frame(f, bg="#2a3a4a", height=1).grid(
                row=0, column=1, sticky="ew", pady=6)

        def _lbl(row, text):
            ctk.CTkLabel(pg, text=text).grid(
                row=row, column=0, sticky="w", padx=20, pady=8)

        # ── title + save ──────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(pg, fg_color="transparent")
        hdr.grid(row=0, column=0, columnspan=3, sticky="ew",
                 padx=20, pady=(18, 4))
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text="Settings",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).grid(row=0, column=0, sticky="w")
        self._save_btn = ctk.CTkButton(hdr, text="Save Settings",
                                       command=self._save_settings)
        self._save_btn.grid(row=0, column=1, sticky="e")

        # ── Connection ────────────────────────────────────────────────────────
        _section(1, "Connection")

        _lbl(2, "API Key")
        self.api_entry = ctk.CTkEntry(pg, show="•",
                                      placeholder_text="Paste your upload token")
        self.api_entry.grid(row=2, column=1, padx=8, pady=8, sticky="ew")
        self.api_entry.insert(0, self.config_data.get("api_key", ""))
        ctk.CTkButton(pg, text="Show", width=65,
                      command=self._toggle_key_visibility
                      ).grid(row=2, column=2, padx=(0, 20))

        _lbl(3, "Main Demos Folder")
        self.folder_entry = ctk.CTkEntry(pg,
                                         placeholder_text="Path to Demos folder")
        self.folder_entry.grid(row=3, column=1, padx=8, pady=8, sticky="ew")
        self.folder_entry.insert(0, self.config_data.get("demos_folder", ""))
        folder_btns = ctk.CTkFrame(pg, fg_color="transparent")
        folder_btns.grid(row=3, column=2, padx=(0, 20))
        ctk.CTkButton(folder_btns, text="Browse", width=65,
                      command=self._browse_folder).pack(side="left", padx=(0, 4))
        ctk.CTkButton(folder_btns, text="Auto", width=55,
                      command=self._auto_detect_demos_folder).pack(side="left")

        _lbl(4, "Secondary Demos Folder")
        self.sync_entry = ctk.CTkEntry(pg,
                                       placeholder_text="Optional — replays are moved from here into the Main Demos Folder")
        self.sync_entry.grid(row=4, column=1, padx=8, pady=8, sticky="ew")
        self.sync_entry.insert(0, self.config_data.get("sync_folder", ""))
        ctk.CTkButton(pg, text="Browse", width=65,
                      command=self._browse_sync_folder
                      ).grid(row=4, column=2, padx=(0, 20))

        _lbl(5, "Visibility")
        self.vis_var = ctk.StringVar(
            value=self.config_data.get("visibility", "unlisted"))
        ctk.CTkOptionMenu(pg, values=["public", "unlisted", "private"],
                          variable=self.vis_var, width=150
                          ).grid(row=5, column=1, sticky="w", padx=8, pady=8)

        # ── Behaviour ─────────────────────────────────────────────────────────
        _section(6, "Behaviour")

        _lbl(7, "Auto Upload")
        self.upload_on_detect_var = ctk.BooleanVar(
            value=self.config_data.get("upload_on_detect", True))
        ctk.CTkSwitch(pg, text="Automatically upload new replays to Ballchasing",
                      variable=self.upload_on_detect_var,
                      onvalue=True, offvalue=False,
                      progress_color=("#3B8ED0", "#1F6AA5"),
                      button_color=("#d0d0d0", "white"),
                      button_hover_color=("#bebebe", "#f0f0f0")
                      ).grid(row=7, column=1, sticky="w", padx=8, pady=8)

        _lbl(8, "Start with Windows")
        self.run_on_startup_var = ctk.BooleanVar(
            value=self._get_run_on_startup())
        ctk.CTkSwitch(pg, text="Launch automatically when Windows starts",
                      variable=self.run_on_startup_var,
                      onvalue=True, offvalue=False,
                      progress_color=("#3B8ED0", "#1F6AA5"),
                      button_color=("#d0d0d0", "white"),
                      button_hover_color=("#bebebe", "#f0f0f0")
                      ).grid(row=8, column=1, sticky="w", padx=8, pady=8)

        _lbl(9, "Desktop Shortcut")
        self.desktop_shortcut_var = ctk.BooleanVar(
            value=self.config_data.get("desktop_shortcut", False))
        ctk.CTkSwitch(pg, text="Create a desktop shortcut for quick access",
                      variable=self.desktop_shortcut_var,
                      onvalue=True, offvalue=False,
                      progress_color=("#3B8ED0", "#1F6AA5"),
                      button_color=("#d0d0d0", "white"),
                      button_hover_color=("#bebebe", "#f0f0f0")
                      ).grid(row=9, column=1, sticky="w", padx=8, pady=8)

        _lbl(10, "Launch with Rocket League")
        self.launch_with_rl_var = ctk.BooleanVar(
            value=self.config_data.get("launch_with_rl", False))
        ctk.CTkSwitch(pg, text="Start watching replays when Rocket League launches",
                      variable=self.launch_with_rl_var,
                      onvalue=True, offvalue=False,
                      progress_color=("#3B8ED0", "#1F6AA5"),
                      button_color=("#d0d0d0", "white"),
                      button_hover_color=("#bebebe", "#f0f0f0")
                      ).grid(row=10, column=1, sticky="w", padx=8, pady=8)

        _lbl(11, "Auto-fetch Stats")
        self.auto_fetch_bc_var = ctk.BooleanVar(
            value=self.config_data.get("auto_fetch_bc", True))
        ctk.CTkSwitch(pg,
                      text="Automatically fetch Ballchasing stats for new replays",
                      variable=self.auto_fetch_bc_var,
                      onvalue=True, offvalue=False,
                      progress_color=("#3B8ED0", "#1F6AA5"),
                      button_color=("#d0d0d0", "white"),
                      button_hover_color=("#bebebe", "#f0f0f0")
                      ).grid(row=11, column=1, sticky="w", padx=8, pady=8)

        _lbl(12, "Low Priority Mode")
        self.low_priority_var = ctk.BooleanVar(
            value=self.config_data.get("low_priority_mode", False))
        ctk.CTkSwitch(pg, text="Run at below-normal CPU priority (recommended for low-end PCs)",
                      variable=self.low_priority_var,
                      onvalue=True, offvalue=False,
                      progress_color=("#3B8ED0", "#1F6AA5"),
                      button_color=("#d0d0d0", "white"),
                      button_hover_color=("#bebebe", "#f0f0f0")
                      ).grid(row=12, column=1, sticky="w", padx=8, pady=8)

        _lbl(13, "Replay Folder Limit")
        limit_frame = ctk.CTkFrame(pg, fg_color="transparent")
        limit_frame.grid(row=13, column=1, sticky="w", padx=8, pady=8)
        self.replay_limit_var = tk.StringVar(
            value=str(self.config_data.get("replay_limit", 0)))
        limit_entry = ctk.CTkEntry(limit_frame, textvariable=self.replay_limit_var,
                                   width=70, justify="center")
        limit_entry.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(limit_frame,
                     text="Max replays in folder  (0 = disabled, minimum 50)",
                     text_color=C_DIM, font=ctk.CTkFont(size=11)
                     ).pack(side="left")

        # ── Appearance ────────────────────────────────────────────────────────
        _section(14, "Appearance")

        _lbl(15, "Theme")
        self.theme_var = ctk.StringVar(
            value=self.config_data.get("theme", "dark"))
        ctk.CTkSegmentedButton(pg, values=["dark", "light", "system"],
                               variable=self.theme_var,
                               command=self._on_theme_change,
                               width=200
                               ).grid(row=15, column=1, sticky="w",
                                      padx=8, pady=8)

        _lbl(16, "Colours")
        ctk.CTkButton(pg, text="Customize Colors",
                      command=self._open_color_editor
                      ).grid(row=16, column=1, sticky="w", padx=8, pady=8)

        # ── Actions ───────────────────────────────────────────────────────────
        _section(17, "Actions")

        ctk.CTkButton(pg, text="Download Replays from Ballchasing",
                      command=self._confirm_download
                      ).grid(row=18, column=0, columnspan=3,
                             padx=20, pady=(8, 2), sticky="w")
        ctk.CTkLabel(pg, text="Fetch replays from your Ballchasing account into the local cache.",
                     font=ctk.CTkFont(size=11), text_color=C_DIM, anchor="w"
                     ).grid(row=19, column=0, columnspan=3,
                            padx=20, pady=(0, 10), sticky="w")

        ctk.CTkButton(pg, text="Full Duplicate Scan",
                      command=self._run_manual_dedup
                      ).grid(row=20, column=0, columnspan=3,
                             padx=20, pady=(8, 2), sticky="w")
        ctk.CTkLabel(pg, text="MD5-hash every replay to find exact duplicates. Use when you suspect missed duplicates.",
                     font=ctk.CTkFont(size=11), text_color=C_DIM, anchor="w"
                     ).grid(row=21, column=0, columnspan=3,
                            padx=20, pady=(0, 10), sticky="w")


    # ── page switching ────────────────────────────────────────────────────────

    def _show_main(self):
        self.settings_page.pack_forget()
        self.replays_page.pack_forget()
        self.detail_page.pack_forget()
        self.main_page.pack(fill="both", expand=True)
        self.nav_btn.configure(text="⚙")
        self._current_page = "main"

    def _show_settings(self):
        self.main_page.pack_forget()
        self.replays_page.pack_forget()
        self.settings_page.pack(fill="both", expand=True)
        self.nav_btn.configure(text="←")
        self._current_page = "settings"

    def _open_color_editor(self):
        if hasattr(self, "_color_editor") and self._color_editor.winfo_exists():
            self._color_editor.focus()
            return

        COLOR_DEFS = [
            (None,            "Replay Cards"),
            ("C_BG",          "List background"),
            ("C_CARD",        "Card background"),
            ("C_BORDER",      "Card border"),
            ("C_DIVIDER",     "Dividers inside cards"),
            ("C_SCORE_COL",   "Left/right score area"),
            (None,            "Text"),
            ("C_NAME",        "Replay title"),
            ("C_DATE",        "Date & map line"),
            ("C_DIM",         "Secondary text (labels, hints, descriptions app-wide)"),
            (None,            "Stats Detail View"),
            ("C_SCORE",       "Big stat numbers (goals, assists, saves…)"),
            (None,            "Teams"),
            ("C_BLUE",        "Blue team"),
            ("C_ORANGE",      "Orange team"),
            (None,            "Upload Status"),
            ("C_BORDER_FAIL", "Failed card — border"),
            ("C_CARD_FAIL",   "Failed card — fill"),
            ("C_CHECK",       "Successful upload"),
        ]

        # mini-card layout constants — wide & short to match actual replay list proportions
        PW, PH  = 300, 100          # canvas: wide landscape card
        CX0, CY0, CX1, CY1 = 6, 10, PW-6, PH-10
        SCW = 32; BAR = 5           # score-col width, team bar width
        NH  = 26; MH = 16; PLH = 14 # name / meta / player row heights
        DIV1 = CY0 + NH
        DIV2 = CY0 + NH + MH
        MID  = (CX0 + CX1) // 2

        # map each key to the canvas regions it occupies  [(x0,y0,x1,y1), ...]
        REGIONS: dict[str, list] = {
            "C_BG":          [(0, 0, PW, PH)],
            "C_CARD":        [(CX0, CY0, CX1, CY1)],
            "C_BORDER":      [(CX0, CY0, CX1, CY1)],
            "C_SCORE_COL":   [(CX0, CY0, CX0+SCW, CY1),
                              (CX1-SCW, CY0, CX1, CY1)],
            "C_DIVIDER":     [(CX0+SCW, DIV1-1, CX1-SCW, DIV1+1),
                              (CX0+SCW, DIV2-1, CX1-SCW, DIV2+1),
                              (MID-1, DIV2, MID+1, CY1)],
            "C_NAME":        [(CX0+SCW, CY0, CX1-SCW, DIV1)],
            "C_SCORE":       [],   # stats detail view only, not on replay cards
            "C_DATE":        [(CX0+SCW, DIV1, CX1-SCW, DIV2)],
            "C_DIM":         [(CX0+SCW, DIV2, MID, DIV2+PLH),
                              (MID, DIV2, CX1-SCW, DIV2+PLH)],
            "C_BLUE":        [(CX0, CY0, CX0+SCW, CY1),
                              (CX0+SCW, DIV2, MID, CY1)],
            "C_ORANGE":      [(CX1-SCW, CY0, CX1, CY1),
                              (MID, DIV2, CX1-SCW, CY1)],
            "C_BORDER_FAIL": [(CX0, CY0, CX1, CY1)],
            "C_CARD_FAIL":   [(CX0, CY0, CX1, CY1)],
            "C_CHECK":       [(CX1-SCW-46, CY0+6, CX1-SCW-4, CY0+18)],
        }

        win = ctk.CTkToplevel(self)
        self._color_editor = win
        win.title("Color Theme Editor")
        win.resizable(True, True)
        win.minsize(800, 560)
        win.transient(self)
        win.lift()
        win.after(10, lambda: win.geometry("900x720"))

        g = globals()
        working: dict[str, list] = {name: list(g[name])
                                    for name, _ in COLOR_DEFS if name}

        # ── top header (title left, action buttons RIGHT — always visible) ─
        top = ctk.CTkFrame(win, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(16, 8))

        # action buttons on the far right — packed first so they anchor right
        ctk.CTkButton(top, text="Close", width=70,
                      fg_color="transparent", border_width=1,
                      border_color=C_BORDER, text_color=C_DIM,
                      command=win.destroy).pack(side="right", padx=(4, 0))
        ctk.CTkButton(top, text="Apply",
                      command=lambda: apply_colors()).pack(side="right", padx=(4, 0))
        ctk.CTkButton(top, text="Reset to Defaults", width=130,
                      fg_color="transparent", border_width=1,
                      border_color=C_BORDER, text_color=C_DIM,
                      command=lambda: reset_defaults()).pack(side="right", padx=(4, 0))

        ctk.CTkLabel(top, text="Color Theme Editor",
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
        ctk.CTkLabel(top, text="  ·  Hover a row to highlight it in the preview",
                     font=ctk.CTkFont(size=11), text_color=C_DIM).pack(side="left")

        # ── body: left list + right preview — fills everything below header ─
        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        body.columnconfigure(0, weight=1, minsize=420)
        body.columnconfigure(1, minsize=PW+36)
        body.rowconfigure(0, weight=1)

        # ── LEFT: colour list ─────────────────────────────────────────────
        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        col_hdr = ctk.CTkFrame(left, fg_color="transparent")
        col_hdr.pack(fill="x", pady=(0, 2))
        ctk.CTkLabel(col_hdr, text="Colour", font=ctk.CTkFont(size=11, weight="bold"),
                     width=160, anchor="w").pack(side="left")
        ctk.CTkLabel(col_hdr, text="Light", font=ctk.CTkFont(size=11, weight="bold"),
                     width=110, anchor="center").pack(side="left", padx=(0, 6))
        ctk.CTkLabel(col_hdr, text="Dark", font=ctk.CTkFont(size=11, weight="bold"),
                     width=110, anchor="center").pack(side="left")

        scroll = ctk.CTkScrollableFrame(left, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        # ── RIGHT: preview panel ──────────────────────────────────────────
        right_outer = ctk.CTkFrame(body, fg_color="transparent")
        right_outer.grid(row=0, column=1, sticky="nsew", padx=(12, 0))

        ctk.CTkLabel(right_outer, text="Preview",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     anchor="w").pack(fill="x", pady=(0, 4))

        # ── mode tab buttons (single row, 5 modes) ───────────────────────
        _preview_mode = {"v": "List"}
        _mode_btns: dict[str, ctk.CTkButton] = {}
        _MODES = ["List", "Main", "Settings", "Stats", "Detail"]
        _MODE_PH = {
            "List":    220, "Main":    200,
            "Settings":250, "Stats":   180,
            "Detail":  238,
        }

        tab_grid = ctk.CTkFrame(right_outer, fg_color="transparent")
        tab_grid.pack(fill="x", pady=(0, 6))
        for _col in range(5):
            tab_grid.columnconfigure(_col, weight=1)

        # ── scrollable canvas area (below tabs) ──────────────────────────
        canvas_scroll = ctk.CTkScrollableFrame(right_outer, fg_color="transparent",
                                               width=PW + 4)
        canvas_scroll.pack(fill="both", expand=True)

        ctk.CTkLabel(canvas_scroll, text="Light", font=ctk.CTkFont(size=10),
                     text_color=C_DIM).pack(pady=(0, 3))
        _init_ph = _MODE_PH[_preview_mode["v"]]
        pc_light = tk.Canvas(canvas_scroll, width=PW, height=_init_ph, highlightthickness=0)
        pc_light.pack(pady=(0, 10))
        ctk.CTkLabel(canvas_scroll, text="Dark", font=ctk.CTkFont(size=10),
                     text_color=C_DIM).pack(pady=(0, 3))
        pc_dark = tk.Canvas(canvas_scroll, width=PW, height=_init_ph, highlightthickness=0)
        pc_dark.pack()

        # ── coordinate constants for replay-list cards (3 cards, PH=220) ─
        _LCARD_H, _LCARD_GAP, _LCARD_PAD = 65, 5, 7
        _LCX0, _LCX1 = 6, PW - 6          # 6, 374
        _LSCW, _LBAR  = 24, 4
        _LNH,  _LMH   = 21, 13
        _LMID = (_LCX0 + _LCX1) // 2      # 190

        def _list_card_coords(i):
            cy0 = _LCARD_PAD + i * (_LCARD_H + _LCARD_GAP)
            cy1 = cy0 + _LCARD_H
            return cy0, cy1, cy0 + _LNH, cy0 + _LNH + _LMH  # cy0,cy1,d1,d2

        # ── player-stats panel constants (PH=180) ────────────────────────
        _SPX0, _SPY0, _SPX1, _SPY1 = 6, 32, PW - 6, 172
        _SP_COLS = ["Goals", "Assists", "Saves", "Score"]
        _SP_VALS = ["5",     "3",       "2",     "450"]
        _SP_CW   = (_SPX1 - _SPX0) // 4   # 92

        # ── replay-detail table constants (PH=238) ───────────────────────
        _DT_ROW_H   = 16
        _DT_TX0     = 8
        _DT_TX1     = PW - 6
        _DT_TNW     = 90          # player-name column width
        _DT_NCOLS   = 5           # Score, Goals, Assists, Saves, Shots
        _DT_TCW     = (_DT_TX1 - _DT_TX0 - _DT_TNW) // _DT_NCOLS
        _DT_BLUE_Y0  = 86
        _DT_ORA_Y0  = _DT_BLUE_Y0 + 4 * _DT_ROW_H + 4   # 154

        # ── per-mode REGIONS (hover outline only drawn if key present) ────
        def _rg(**kw): return {k: v for k, v in kw.items() if v}

        _RG_CARD = _rg(
            C_BG         = [(0, 0, PW, 100)],
            C_CARD        = [(CX0, CY0, CX1, CY1)],
            C_BORDER      = [(CX0, CY0, CX1, CY1)],
            C_SCORE_COL   = [(CX0, CY0, CX0+SCW, CY1), (CX1-SCW, CY0, CX1, CY1)],
            C_DIVIDER     = [(CX0+SCW, DIV1-1, CX1-SCW, DIV1+1),
                              (CX0+SCW, DIV2-1, CX1-SCW, DIV2+1),
                              (MID-1,   DIV2,   MID+1,   CY1)],
            C_NAME        = [(CX0+SCW, CY0, CX1-SCW, DIV1)],
            C_DATE        = [(CX0+SCW, DIV1, CX1-SCW, DIV2)],
            C_DIM         = [(CX0+SCW, DIV2, MID, CY1), (MID, DIV2, CX1-SCW, CY1)],
            C_BLUE        = [(CX0, CY0, CX0+SCW, CY1), (CX0+SCW, DIV2, MID, CY1)],
            C_ORANGE      = [(CX1-SCW, CY0, CX1, CY1), (MID, DIV2, CX1-SCW, CY1)],
            C_BORDER_FAIL = [(CX0, CY0, CX1, CY1)],
            C_CARD_FAIL   = [(CX0, CY0, CX1, CY1)],
            C_CHECK       = [(CX1-SCW-46, CY0+6, CX1-SCW-4, CY0+18)],
            C_SCORE       = [],
        )

        _rg_list_raw = {k: [] for k in _RG_CARD}
        _rg_list_raw["C_BG"] = [(0, 0, PW, _MODE_PH["List"])]
        for _li in range(3):
            _lcy0, _lcy1, _ld1, _ld2 = _list_card_coords(_li)
            _rg_list_raw["C_CARD"]     .append((_LCX0, _lcy0, _LCX1, _lcy1))
            _rg_list_raw["C_BORDER"]   .append((_LCX0, _lcy0, _LCX1, _lcy1))
            _rg_list_raw["C_SCORE_COL"]+=[(_LCX0, _lcy0, _LCX0+_LSCW, _lcy1),
                                           (_LCX1-_LSCW, _lcy0, _LCX1, _lcy1)]
            _rg_list_raw["C_DIVIDER"]  +=[(_LCX0+_LSCW, _ld1-1, _LCX1-_LSCW, _ld1+1),
                                           (_LCX0+_LSCW, _ld2-1, _LCX1-_LSCW, _ld2+1),
                                           (_LMID-1, _ld2, _LMID+1, _lcy1)]
            _rg_list_raw["C_NAME"]     .append((_LCX0+_LSCW, _lcy0, _LCX1-_LSCW, _ld1))
            _rg_list_raw["C_DATE"]     .append((_LCX0+_LSCW, _ld1, _LCX1-_LSCW, _ld2))
            _rg_list_raw["C_DIM"]     +=[(_LCX0+_LSCW, _ld2, _LMID, _lcy1),
                                          (_LMID, _ld2, _LCX1-_LSCW, _lcy1)]
            _rg_list_raw["C_BLUE"]    +=[(_LCX0, _lcy0, _LCX0+_LSCW, _lcy1),
                                          (_LCX0+_LSCW, _ld2, _LMID, _lcy1)]
            _rg_list_raw["C_ORANGE"]  +=[(_LCX1-_LSCW, _lcy0, _LCX1, _lcy1),
                                          (_LMID, _ld2, _LCX1-_LSCW, _lcy1)]
            _rg_list_raw["C_CHECK"]   .append((_LCX1-_LSCW-36, _lcy0+4, _LCX1-_LSCW-4, _lcy0+14))
            if _li == 1:   # middle card is the failed one
                _rg_list_raw["C_BORDER_FAIL"].append((_LCX0, _lcy0, _LCX1, _lcy1))
                _rg_list_raw["C_CARD_FAIL"]  .append((_LCX0, _lcy0, _LCX1, _lcy1))
        _RG_LIST = _rg(**_rg_list_raw)

        _RG_MAIN = _rg(
            C_BORDER = [(10, 60, PW-10, 82)],
            C_DIM    = [(10, 86, PW-10, 100),
                         (10, 102, PW-10, 180),
                         (PW-78, 184, PW-10, 196)],
        )
        _RG_SETTINGS = _rg(
            C_DIM = [(8,  62, 68,  76),    # API Key label
                      (8,  80, 68,  94),    # Demos Folder label
                      (8,  98, 68, 112),    # Visibility label
                      (36, 132, PW-8, 198), # toggle labels
                      (8, 212, 68, 226),    # Theme label
                      (8, 230, 68, 244)],   # Colours label
        )
        _RG_STATS = _rg(
            C_BG     = [(0, 0, PW, _MODE_PH["Stats"])],
            C_CARD   = [(_SPX0, _SPY0, _SPX1, _SPY1)],
            C_BORDER = [(_SPX0, _SPY0, _SPX1, _SPY1)],
            C_DIM    = [(_SPX0 + i*_SP_CW, _SPY0, _SPX0 + (i+1)*_SP_CW, _SPY0+22)
                         for i in range(4)],
            C_SCORE  = [(_SPX0 + i*_SP_CW, _SPY0+22, _SPX0 + (i+1)*_SP_CW, _SPY1)
                         for i in range(4)],
            C_DIVIDER= [(_SPX0 + i*_SP_CW, _SPY0+4, _SPX0 + i*_SP_CW, _SPY1-4)
                         for i in range(1, 4)],
        )
        _RG_DETAIL = _rg(
            C_DATE   = [(_DT_TX0, 22, _DT_TX1, 34)],
            C_DIM    = [(_DT_TX0,  4, _DT_TX1, 18)],
            C_BLUE   = [(6, 36, PW//2, 64),
                         (_DT_TX0, _DT_BLUE_Y0,
                          _DT_TX1, _DT_BLUE_Y0 + _DT_ROW_H)],
            C_ORANGE = [(PW//2, 36, _DT_TX1, 64),
                         (_DT_TX0, _DT_ORA_Y0,
                          _DT_TX1, _DT_ORA_Y0 + _DT_ROW_H)],
            C_SCORE  = [(_DT_TX0 + _DT_TNW,
                          _DT_BLUE_Y0 + _DT_ROW_H,
                          _DT_TX1,
                          _DT_ORA_Y0 + 4 * _DT_ROW_H)],
            C_DIVIDER= [(_DT_TX0, 84, _DT_TX1, 86)],
        )

        _MODE_REGIONS = {
            "List":     _RG_LIST,
            "Main":     _RG_MAIN,
            "Settings": _RG_SETTINGS,
            "Stats":    _RG_STATS,
            "Detail":   _RG_DETAIL,
        }

        # ── shared highlight helper ───────────────────────────────────────
        def _hl(pc, rg, key):
            if key and rg.get(key):
                for rx0, ry0, rx1, ry1 in rg[key]:
                    pc.create_rectangle(rx0, ry0, rx1, ry1,
                                         outline="#f5c518", width=2, dash=(5, 3), fill="")

        # ── draw functions (one per mode) ─────────────────────────────────
        def _draw_card(pc, idx, failed=False, highlight=None):
            pc.delete("all")
            c = lambda k: working[k][idx]
            pc.create_rectangle(0, 0, PW, 100, fill=c("C_BG"), outline="")
            cf = c("C_CARD_FAIL") if failed else c("C_CARD")
            cb = c("C_BORDER_FAIL") if failed else c("C_BORDER")
            bw = 2 if failed else 1
            pc.create_rectangle(CX0, CY0, CX1, CY1, fill=cf, outline=cb, width=bw)
            pc.create_rectangle(CX0, CY0, CX0+SCW, CY1, fill=c("C_SCORE_COL"), outline="")
            pc.create_rectangle(CX1-SCW, CY0, CX1, CY1, fill=c("C_SCORE_COL"), outline="")
            pc.create_rectangle(CX0, CY0, CX0+BAR, CY1, fill=c("C_BLUE"), outline="")
            pc.create_rectangle(CX1-BAR, CY0, CX1, CY1, fill=c("C_ORANGE"), outline="")
            scx_l = CX0 + SCW//2 + 1;  scx_r = CX1 - SCW//2 - 1
            scy   = (CY0 + CY1) // 2
            pc.create_text(scx_l, scy, text="2", fill=c("C_BLUE"),   font=("Segoe UI", 10, "bold"))
            pc.create_text(scx_r, scy, text="1", fill=c("C_ORANGE"), font=("Segoe UI", 10, "bold"))
            pc.create_line(CX0+SCW, DIV1, CX1-SCW, DIV1, fill=c("C_DIVIDER"))
            pc.create_line(CX0+SCW, DIV2, CX1-SCW, DIV2, fill=c("C_DIVIDER"))
            pc.create_line(MID, DIV2, MID, CY1, fill=c("C_DIVIDER"))
            pc.create_text(CX0+SCW+6, CY0+NH//2, anchor="w",
                           text="Online Doubles  2026-05-11", fill=c("C_NAME"),
                           font=("Segoe UI", 9, "bold"))
            if failed:
                pc.create_text(CX1-SCW-6, CY0+NH//2, anchor="e",
                               text="✗ upload failed", fill=c("C_BORDER_FAIL"),
                               font=("Segoe UI", 8))
            else:
                pc.create_text(CX1-SCW-6, CY0+NH//2, anchor="e",
                               text="✓ uploaded", fill=c("C_CHECK"),
                               font=("Segoe UI", 8))
            pc.create_text(CX0+SCW+6, DIV1+MH//2, anchor="w",
                           text="Urban Central  |  4:59  |  2026-05-11 05:03 UTC",
                           fill=c("C_DATE"), font=("Segoe UI", 8))
            for pname, col, px, py in [
                    ("Player 1", "C_BLUE",   CX0+SCW+6, DIV2+PLH//2),
                    ("Player 2", "C_ORANGE", MID+6,     DIV2+PLH//2)]:
                pc.create_text(px, py, anchor="w", text=pname, fill=c(col),
                               font=("Segoe UI", 8))
            _hl(pc, _RG_CARD, highlight)

        def _draw_replay_list(pc, idx, highlight=None):
            pc.delete("all")
            c = lambda k: working[k][idx]
            PH_L = _MODE_PH["List"]
            pc.create_rectangle(0, 0, PW, PH_L, fill=c("C_BG"), outline="")
            _cards = [
                (False, "Online Doubles  2026-05-11",   "Urban Central  |  4:59",
                 "Player 1", "Player 2", "2", "1"),
                (True,  "Competitive 3v3  2026-05-10",  "DFH Stadium  |  5:12",
                 "Player 3", "Player 4", "3", "2"),
                (False, "Duels  2026-05-09",            "Mannfield  |  3:47",
                 "Player 5", "Player 6", "1", "0"),
            ]
            for i, (failed, title, meta, p1, p2, sl, sr) in enumerate(_cards):
                cy0, cy1, d1, d2 = _list_card_coords(i)
                cf = c("C_CARD_FAIL")   if failed else c("C_CARD")
                cb = c("C_BORDER_FAIL") if failed else c("C_BORDER")
                bw = 2                  if failed else 1
                status_txt = "✗ upload failed" if failed else "✓ uploaded"
                status_col = c("C_BORDER_FAIL") if failed else c("C_CHECK")
                pc.create_rectangle(_LCX0, cy0, _LCX1, cy1,
                                     fill=cf, outline=cb, width=bw)
                if not failed:
                    pc.create_rectangle(_LCX0, cy0, _LCX0+_LSCW, cy1,
                                         fill=c("C_SCORE_COL"), outline="")
                    pc.create_rectangle(_LCX1-_LSCW, cy0, _LCX1, cy1,
                                         fill=c("C_SCORE_COL"), outline="")
                    pc.create_rectangle(_LCX0, cy0, _LCX0+_LBAR, cy1, fill=c("C_BLUE"),   outline="")
                    pc.create_rectangle(_LCX1-_LBAR, cy0, _LCX1, cy1, fill=c("C_ORANGE"), outline="")
                    scx_l = _LCX0 + _LSCW//2 + 1;  scx_r = _LCX1 - _LSCW//2 - 1
                    scy   = (cy0 + cy1) // 2
                    pc.create_text(scx_l, scy, text=sl, fill=c("C_BLUE"),   font=("Segoe UI", 9, "bold"))
                    pc.create_text(scx_r, scy, text=sr, fill=c("C_ORANGE"), font=("Segoe UI", 9, "bold"))
                    pc.create_line(_LCX0+_LSCW, d1, _LCX1-_LSCW, d1, fill=c("C_DIVIDER"))
                    pc.create_line(_LCX0+_LSCW, d2, _LCX1-_LSCW, d2, fill=c("C_DIVIDER"))
                    pc.create_line(_LMID, d2, _LMID, cy1, fill=c("C_DIVIDER"))
                    plh = (cy1 - d2) // 2
                    pc.create_text(_LCX0+_LSCW+5, d2+plh, anchor="w",
                                   text=p1, fill=c("C_BLUE"),   font=("Segoe UI", 7))
                    pc.create_text(_LMID+5,        d2+plh, anchor="w",
                                   text=p2, fill=c("C_ORANGE"), font=("Segoe UI", 7))
                pc.create_text(_LCX0+(_LSCW if not failed else 6)+5, cy0+_LNH//2, anchor="w",
                               text=title, fill=c("C_NAME"), font=("Segoe UI", 8, "bold"))
                pc.create_text(_LCX1-(_LSCW if not failed else 4)-5, cy0+_LNH//2, anchor="e",
                               text=status_txt, fill=status_col, font=("Segoe UI", 7))
                if not failed:
                    pc.create_text(_LCX0+_LSCW+5, d1+_LMH//2, anchor="w",
                                   text=meta, fill=c("C_DATE"), font=("Segoe UI", 7))
            _hl(pc, _RG_LIST, highlight)

        def _draw_main_page(pc, idx, highlight=None):
            pc.delete("all")
            c = lambda k: working[k][idx]
            PH_M  = _MODE_PH["Main"]
            bg    = "#F0F0F0" if idx == 0 else "#2B2B2B"
            txt   = "#111111" if idx == 0 else "#DEDEDE"
            lbg   = "#FAFAFA" if idx == 0 else "#1E1E1E"
            lbdr  = "#CCCCCC" if idx == 0 else "#555555"
            pc.create_rectangle(0, 0, PW, PH_M, fill=bg, outline="")
            # header
            pc.create_text(10, 14, anchor="w",
                           text="Ballchasing Auto Uploader  v1.4.55",
                           fill=txt, font=("Segoe UI", 9, "bold"))
            pc.create_text(PW-10, 14, anchor="e", text="Stopped  ●",
                           fill=c("C_DIM"), font=("Segoe UI", 8))
            # Start Watching button
            pc.create_rectangle(10, 26, PW-10, 54, fill="#3B8ED0", outline="")
            pc.create_text(PW//2, 40, text="Start Watching",
                           fill="white", font=("Segoe UI", 9, "bold"))
            # View Replays button (uses C_BORDER)
            pc.create_rectangle(10, 60, PW-10, 82,
                                 fill=bg, outline=c("C_BORDER"), width=1)
            pc.create_text(PW//2, 71, text="View Replays",
                           fill=txt, font=("Segoe UI", 8))
            # Quota row
            pc.create_text(10, 90, anchor="w",
                           text="↻  Quota: 750 / 1000 remaining",
                           fill=c("C_DIM"), font=("Segoe UI", 8))
            # Log textbox
            pc.create_rectangle(10, 100, PW-10, 178,
                                 fill=lbg, outline=lbdr, width=1)
            pc.create_text(16, 108, anchor="nw",
                           text=("[INFO] Watching: C:/Users/andre/Demo…\n"
                                 "[INFO] Uploaded: game_2026-05-11.replay\n"
                                 "[INFO] Quota: 750 remaining\n"
                                 "[INFO] Waiting for new replays…"),
                           fill=c("C_DIM"), font=("Consolas", 7))
            # Website link
            pc.create_text(PW-10, 188, anchor="e",
                           text="Website", fill=c("C_DIM"), font=("Segoe UI", 8))
            _hl(pc, _RG_MAIN, highlight)

        def _draw_settings_page(pc, idx, highlight=None):
            pc.delete("all")
            c    = lambda k: working[k][idx]
            PH_S = _MODE_PH["Settings"]
            bg   = "#F0F0F0" if idx == 0 else "#2B2B2B"
            txt  = "#111111" if idx == 0 else "#DEDEDE"
            ebg  = "#FFFFFF" if idx == 0 else "#383838"
            ebdr = "#BBBBBB" if idx == 0 else "#555555"
            sblu = "#4a8cc4"
            pc.create_rectangle(0, 0, PW, PH_S, fill=bg, outline="")

            # ── app header ─────────────────────────────────────────────────
            pc.create_text(8, 11, anchor="w",
                           text="Ballchasing Auto Uploader  v1.4.58",
                           fill=txt, font=("Segoe UI", 8, "bold"))
            pc.create_text(PW-8, 11, anchor="e", text="Stopped  ●",
                           fill=c("C_DIM"), font=("Segoe UI", 7))

            # ── Settings title + Save button ───────────────────────────────
            pc.create_text(8, 30, anchor="w", text="Settings",
                           fill=txt, font=("Segoe UI", 10, "bold"))
            pc.create_rectangle(PW-86, 21, PW-6, 39,
                                 fill="#3B8ED0", outline="")
            pc.create_text(PW-46, 30, text="Save Settings",
                           fill="white", font=("Segoe UI", 7))
            pc.create_line(8, 43, PW-8, 43, fill=ebdr)

            # ── Connection ─────────────────────────────────────────────────
            pc.create_text(8, 54, anchor="w", text="Connection",
                           fill=sblu, font=("Segoe UI", 8, "bold"))
            pc.create_line(70, 54, PW-8, 54, fill="#3a4a5a")

            # API Key
            pc.create_text(8, 69, anchor="w", text="API Key",
                           fill=c("C_DIM"), font=("Segoe UI", 7))
            pc.create_rectangle(70, 62, PW-44, 76,
                                 fill=ebg, outline=ebdr, width=1)
            pc.create_text(74, 69, anchor="w",
                           text="••••••••••••••••••••••••",
                           fill=txt, font=("Segoe UI", 7))
            pc.create_rectangle(PW-38, 62, PW-6, 76,
                                 fill="#3B8ED0", outline="")
            pc.create_text(PW-22, 69, text="Show",
                           fill="white", font=("Segoe UI", 7))

            # Demos Folder
            pc.create_text(8, 87, anchor="w", text="Demos Folder",
                           fill=c("C_DIM"), font=("Segoe UI", 7))
            pc.create_rectangle(70, 80, PW-44, 94,
                                 fill=ebg, outline=ebdr, width=1)
            pc.create_text(74, 87, anchor="w",
                           text="C:/Users/…/DemosEpic",
                           fill=txt, font=("Segoe UI", 7))
            pc.create_rectangle(PW-38, 80, PW-6, 94,
                                 fill=bg, outline=ebdr, width=1)
            pc.create_text(PW-22, 87, text="Browse",
                           fill=txt, font=("Segoe UI", 7))

            # Visibility dropdown
            pc.create_text(8, 105, anchor="w", text="Visibility",
                           fill=c("C_DIM"), font=("Segoe UI", 7))
            pc.create_rectangle(70, 98, 130, 112,
                                 fill=ebg, outline=ebdr, width=1)
            pc.create_text(74, 105, anchor="w", text="unlisted",
                           fill=txt, font=("Segoe UI", 7))
            pc.create_text(127, 105, anchor="e", text="▼",
                           fill=c("C_DIM"), font=("Segoe UI", 6))

            # ── Behaviour ──────────────────────────────────────────────────
            pc.create_text(8, 122, anchor="w", text="Behaviour",
                           fill=sblu, font=("Segoe UI", 8, "bold"))
            pc.create_line(58, 122, PW-8, 122, fill="#3a4a5a")

            for _ty, _lbl in [(138, "Auto Upload"),
                               (154, "Start with Windows"),
                               (170, "Launch with Rocket League"),
                               (186, "Auto-fetch Stats")]:
                pc.create_rectangle(8, _ty-7, 30, _ty+7,
                                     fill="#3B8ED0", outline="")
                pc.create_oval(19, _ty-6, 29, _ty+6,
                               fill="white", outline="")
                pc.create_text(36, _ty, anchor="w", text=_lbl,
                               fill=c("C_DIM"), font=("Segoe UI", 7))

            # ── Appearance ─────────────────────────────────────────────────
            pc.create_text(8, 202, anchor="w", text="Appearance",
                           fill=sblu, font=("Segoe UI", 8, "bold"))
            pc.create_line(66, 202, PW-8, 202, fill="#3a4a5a")

            # Theme segmented buttons
            pc.create_text(8, 218, anchor="w", text="Theme",
                           fill=c("C_DIM"), font=("Segoe UI", 7))
            for _ti, _tlbl in enumerate(["dark", "light", "system"]):
                _tx0 = 70 + _ti * 34
                _fc  = "#3B8ED0" if _ti == 0 else ebg
                _tc  = "white"   if _ti == 0 else txt
                pc.create_rectangle(_tx0, 211, _tx0+30, 225,
                                     fill=_fc, outline=ebdr, width=1)
                pc.create_text(_tx0+15, 218, text=_tlbl,
                               fill=_tc, font=("Segoe UI", 7))

            # Colours row
            pc.create_text(8, 236, anchor="w", text="Colours",
                           fill=c("C_DIM"), font=("Segoe UI", 7))
            pc.create_rectangle(70, 229, 170, 243,
                                 fill="#3B8ED0", outline="")
            pc.create_text(120, 236, text="Customize Colors",
                           fill="white", font=("Segoe UI", 7))

            _hl(pc, _RG_SETTINGS, highlight)

        def _draw_player_stats(pc, idx, highlight=None):
            pc.delete("all")
            c = lambda k: working[k][idx]
            PH_PS = _MODE_PH["Stats"]
            pc.create_rectangle(0, 0, PW, PH_PS, fill=c("C_BG"), outline="")
            # Title label
            pc.create_text(10, 16, anchor="w", text="Player Stats",
                           fill=c("C_DIM"), font=("Segoe UI", 9, "bold"))
            # Stats card
            pc.create_rectangle(_SPX0, _SPY0, _SPX1, _SPY1,
                                 fill=c("C_CARD"), outline=c("C_BORDER"), width=1)
            for i, (lbl, val) in enumerate(zip(_SP_COLS, _SP_VALS)):
                cx = _SPX0 + i * _SP_CW + _SP_CW // 2
                pc.create_text(cx, _SPY0 + 12, text=lbl,
                               fill=c("C_DIM"), font=("Segoe UI", 8), anchor="center")
                pc.create_text(cx, _SPY0 + 72, text=val,
                               fill=c("C_SCORE"), font=("Segoe UI", 16, "bold"),
                               anchor="center")
                if i > 0:
                    pc.create_line(_SPX0 + i*_SP_CW, _SPY0+6,
                                    _SPX0 + i*_SP_CW, _SPY1-6, fill=c("C_DIVIDER"))
            _hl(pc, _RG_STATS, highlight)

        def _draw_replay_detail(pc, idx, highlight=None):
            pc.delete("all")
            c    = lambda k: working[k][idx]
            PH_D = _MODE_PH["Detail"]
            bg   = "#F0F0F0" if idx == 0 else "#2B2B2B"
            txt  = "#111111" if idx == 0 else "#DEDEDE"
            ebg  = "#FFFFFF" if idx == 0 else "#383838"
            ebdr = "#BBBBBB" if idx == 0 else "#555555"
            pc.create_rectangle(0, 0, PW, PH_D, fill=bg, outline="")

            # ── action buttons row ─────────────────────────────────────────
            pc.create_rectangle(6, 4, 54, 18, fill=bg, outline=ebdr, width=1)
            pc.create_text(30, 11, text="← Replays",
                           fill=txt, font=("Segoe UI", 6))
            for _bi, (_blbl, _bfc, _btc, _bbd) in enumerate([
                    ("Uploaded",    ebg,       txt,      ebdr),
                    ("Ballchasing", ebg,       txt,      c("C_BLUE")),
                    ("Rename",      ebg,       txt,      ebdr),
                    ("Copy",        ebg,       txt,      ebdr)]):
                _bx = 58 + _bi * 48
                pc.create_rectangle(_bx, 4, _bx+44, 18,
                                     fill=_bfc, outline=_bbd, width=1)
                pc.create_text(_bx+22, 11, text=_blbl,
                               fill=_btc, font=("Segoe UI", 6))
            pc.create_rectangle(PW-44, 4, PW-6, 18,
                                 fill=bg, outline="#CC3333", width=1)
            pc.create_text(PW-25, 11, text="Delete",
                           fill="#CC3333", font=("Segoe UI", 6))

            # ── game info line ─────────────────────────────────────────────
            pc.create_text(8, 28, anchor="w",
                           text="2026-05-11 21:42 UTC  |  Online  |  DFH Stadium  |  4:59",
                           fill=c("C_DATE"), font=("Segoe UI", 6))

            # ── score bar ─────────────────────────────────────────────────
            _SBY0, _SBY1 = 36, 64
            _sbg = "#1a2a3a" if idx == 1 else "#d0dce8"
            pc.create_rectangle(6, _SBY0, PW-6, _SBY1, fill=_sbg, outline="")
            pc.create_text(14, (_SBY0+_SBY1)//2, anchor="w",
                           text="BLUE", fill=c("C_BLUE"),
                           font=("Segoe UI", 9, "bold"))
            pc.create_text(PW//2, (_SBY0+_SBY1)//2,
                           text="2  –  5", fill=txt,
                           font=("Segoe UI", 12, "bold"))
            pc.create_text(PW-14, (_SBY0+_SBY1)//2, anchor="e",
                           text="ORANGE  ▲", fill=c("C_ORANGE"),
                           font=("Segoe UI", 9, "bold"))

            # ── category tabs ──────────────────────────────────────────────
            _TABS = ["Overall", "Boost", "Positioning", "Movement", "Ball"]
            _TW   = (PW - 12) // len(_TABS)
            for _ti, _tlbl in enumerate(_TABS):
                _tx = 6 + _ti * _TW + _TW // 2
                _fc = c("C_BLUE") if _ti == 0 else c("C_DIM")
                pc.create_text(_tx, 74, text=_tlbl, fill=_fc,
                               font=("Segoe UI", 7))
                if _ti == 0:
                    pc.create_line(_tx-18, 81, _tx+18, 81,
                                   fill=c("C_BLUE"), width=2)
            pc.create_line(6, 84, PW-6, 84, fill=ebdr)

            # ── stats tables ───────────────────────────────────────────────
            _STAT_HDRS = ["Score", "Goals", "Assists", "Saves", "Shots"]

            def _draw_team_table(y0, team_col, players):
                # column header row (team-coloured background)
                pc.create_rectangle(_DT_TX0, y0, _DT_TX1, y0+_DT_ROW_H,
                                     fill=team_col, outline="")
                pc.create_text(_DT_TX0+4, y0+_DT_ROW_H//2, anchor="w",
                               text="Player", fill="white",
                               font=("Segoe UI", 6, "bold"))
                for _ci, _ch in enumerate(_STAT_HDRS):
                    _cx = (_DT_TX0 + _DT_TNW
                           + _ci * _DT_TCW + _DT_TCW // 2)
                    pc.create_text(_cx, y0+_DT_ROW_H//2, text=_ch,
                                   fill="white", font=("Segoe UI", 6, "bold"))
                # player rows
                for _ri, (_nm, _sc, _g, _a, _sv, _sh) in enumerate(players):
                    _ry = y0 + _DT_ROW_H + _ri * _DT_ROW_H
                    if _ri % 2 == 0:
                        pc.create_rectangle(
                            _DT_TX0, _ry, _DT_TX1, _ry+_DT_ROW_H,
                            fill=ebg if idx==0 else "#333333", outline="")
                    pc.create_line(_DT_TX0, _ry+_DT_ROW_H,
                                   _DT_TX1, _ry+_DT_ROW_H, fill=ebdr)
                    pc.create_text(_DT_TX0+4, _ry+_DT_ROW_H//2, anchor="w",
                                   text="↗", fill=c("C_DIM"),
                                   font=("Segoe UI", 6))
                    pc.create_text(_DT_TX0+14, _ry+_DT_ROW_H//2, anchor="w",
                                   text=_nm, fill=team_col,
                                   font=("Segoe UI", 7))
                    for _ci, _val in enumerate([_sc, _g, _a, _sv, _sh]):
                        _cx = (_DT_TX0 + _DT_TNW
                               + _ci * _DT_TCW + _DT_TCW // 2)
                        pc.create_text(_cx, _ry+_DT_ROW_H//2, text=_val,
                                       fill=c("C_SCORE"),
                                       font=("Segoe UI", 7, "bold"))

            _draw_team_table(_DT_BLUE_Y0, c("C_BLUE"), [
                ("Player 1", "385", "1", "0", "1", "2"),
                ("Player 2", "176", "0", "1", "0", "0"),
                ("Player 3", "455", "1", "0", "3", "2"),
            ])
            _draw_team_table(_DT_ORA_Y0, c("C_ORANGE"), [
                ("Player 4", "414", "1", "2", "0", "3"),
                ("Player 5", "537", "2", "2", "1", "4"),
                ("Player 6", "402", "2", "1", "0", "2"),
            ])

            # ── filename ───────────────────────────────────────────────────
            pc.create_text(PW//2, _DT_ORA_Y0 + 4*_DT_ROW_H + 8,
                           text="2BD51E3F…BAA3CE.replay",
                           fill=c("C_DIM"), font=("Segoe UI", 6))

            _hl(pc, _RG_DETAIL, highlight)

        # ── mode switching (resize canvases, redraw) ──────────────────────
        def _set_preview_mode(mode: str):
            _preview_mode["v"] = mode
            ph = _MODE_PH.get(mode, 100)
            pc_light.configure(height=ph)
            pc_dark.configure(height=ph)
            for m, b in _mode_btns.items():
                if m == mode:
                    b.configure(fg_color=("#3B8ED0", "#1F6AA5"),
                                text_color="white", border_width=0)
                else:
                    b.configure(fg_color="transparent", border_width=1,
                                border_color=C_BORDER,
                                text_color=("gray20", "gray80"))
            _refresh_previews()

        # create tab buttons now (after _set_preview_mode is defined)
        for _i, _lbl in enumerate(_MODES):
            _r, _c_idx = divmod(_i, 5)
            _b = ctk.CTkButton(tab_grid, text=_lbl, height=26, width=1,
                               fg_color="transparent", border_width=1,
                               border_color=C_BORDER,
                               text_color=("gray20", "gray80"),
                               font=ctk.CTkFont(size=11),
                               command=lambda m=_lbl: _set_preview_mode(m))
            _b.grid(row=_r, column=_c_idx, padx=(0, 3), pady=(0, 3), sticky="ew")
            _mode_btns[_lbl] = _b
        # ── refresh dispatcher ────────────────────────────────────────────
        def _refresh_previews(highlight: str | None = None):
            mode = _preview_mode["v"]
            rg   = _MODE_REGIONS.get(mode, {})
            # Only pass highlight if current mode's REGIONS actually contains it
            hl = highlight if (highlight and rg.get(highlight)) else None
            if mode == "List":
                _draw_replay_list(pc_light, 0, highlight=hl)
                _draw_replay_list(pc_dark,  1, highlight=hl)
            elif mode == "Main":
                _draw_main_page(pc_light, 0, highlight=hl)
                _draw_main_page(pc_dark,  1, highlight=hl)
            elif mode == "Settings":
                _draw_settings_page(pc_light, 0, highlight=hl)
                _draw_settings_page(pc_dark,  1, highlight=hl)
            elif mode == "Stats":
                _draw_player_stats(pc_light, 0, highlight=hl)
                _draw_player_stats(pc_dark,  1, highlight=hl)
            elif mode == "Detail":
                _draw_replay_detail(pc_light, 0, highlight=hl)
                _draw_replay_detail(pc_dark,  1, highlight=hl)

        # initial draw — goes through _set_preview_mode so canvases are sized correctly
        _set_preview_mode(_preview_mode["v"])

        # ── colour rows ───────────────────────────────────────────────────
        swatch_btns: dict[str, list] = {}

        def _refresh_swatch(name: str, idx: int, hex_val: str):
            btn = swatch_btns[name][idx]
            btn.configure(fg_color=hex_val, hover_color=hex_val,
                          text=hex_val, text_color=_contrasting(hex_val))

        def pick_color(name: str, idx: int):
            current = working[name][idx]
            result = colorchooser.askcolor(
                color=current,
                title=f"{'Light' if idx == 0 else 'Dark'} — {name}",
                parent=win)
            if result and result[1]:
                hex_val = result[1].upper()
                working[name][idx] = hex_val
                _refresh_swatch(name, idx, hex_val)
                _refresh_previews()

        for name, desc in COLOR_DEFS:
            if name is None:
                hdr_row = ctk.CTkFrame(scroll, fg_color="transparent")
                hdr_row.pack(fill="x", pady=(10, 2))
                ctk.CTkLabel(hdr_row, text=desc,
                             font=ctk.CTkFont(size=11, weight="bold"),
                             text_color="#4a8cc4").pack(side="left")
                ctk.CTkFrame(hdr_row, height=1, fg_color="#2a3a4a"
                             ).pack(side="left", fill="x", expand=True,
                                    padx=(8, 0), pady=6)
                continue

            row = ctk.CTkFrame(scroll, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(row, text=desc, font=ctk.CTkFont(size=12),
                         width=160, wraplength=155, anchor="w",
                         justify="left").pack(side="left")
            light_hex = working[name][0]
            dark_hex  = working[name][1]
            light_btn = ctk.CTkButton(
                row, text=light_hex, width=110, height=28,
                fg_color=light_hex, hover_color=light_hex,
                text_color=_contrasting(light_hex),
                font=ctk.CTkFont(family="Consolas", size=10),
                command=lambda n=name: pick_color(n, 0))
            light_btn.pack(side="left", padx=(0, 6))
            dark_btn = ctk.CTkButton(
                row, text=dark_hex, width=110, height=28,
                fg_color=dark_hex, hover_color=dark_hex,
                text_color=_contrasting(dark_hex),
                font=ctk.CTkFont(family="Consolas", size=10),
                command=lambda n=name: pick_color(n, 1))
            dark_btn.pack(side="left")
            swatch_btns[name] = [light_btn, dark_btn]

            # hover → highlight matching region on preview
            for widget in (row, light_btn, dark_btn):
                widget.bind("<Enter>",
                            lambda _, n=name: _refresh_previews(highlight=n))
                widget.bind("<Leave>",
                            lambda _: _refresh_previews(highlight=None))

        # ── button logic (referenced by lambdas in the header) ───────────
        def reset_defaults():
            for name, _ in COLOR_DEFS:
                if not name:
                    continue
                default = _DEFAULT_COLORS[name]
                working[name] = list(default)
                _refresh_swatch(name, 0, default[0])
                _refresh_swatch(name, 1, default[1])
            _refresh_previews()

        def apply_colors():
            g = globals()
            for name, vals in working.items():
                g[name] = (vals[0], vals[1])
            self.config_data["custom_colors"] = {k: list(v) for k, v in working.items()}
            save_config(self.config_data)
            self._rebuild_all_pages()

    def _rebuild_all_pages(self):
        """Destroy and recreate all page frames, preserving runtime state."""
        # ── snapshot ──────────────────────────────────────────────────────
        log_text    = self.log_box._textbox.get("1.0", "end-1c")
        quota_text  = self.quota_label.cget("text")
        is_watching = bool(self.observer and self.observer.is_alive())
        cur_page    = self._current_page

        # ── pack_forget THEN destroy ──────────────────────────────────────
        # CTkScrollableFrame.destroy() only destroys the inner frame and
        # leaves its _parent_frame (a separate widget on self) still packed,
        # creating a zombie layout slot.  pack_forget() first ensures that
        # orphaned _parent_frame is removed from the layout manager before
        # it becomes unreachable.
        for page in (self.main_page, self.settings_page,
                     self.replays_page, self.detail_page):
            try:
                page.pack_forget()
            except Exception:
                pass
            try:
                page.destroy()
            except Exception:
                pass

        # ── rebuild ───────────────────────────────────────────────────────
        self._build_main_page()
        self._build_settings_page()
        self._build_replays_page()
        self._build_detail_page()

        # ── restore ───────────────────────────────────────────────────────
        if log_text:
            self.log_box.configure(state="normal")
            self.log_box._textbox.insert("1.0", log_text)
            self.log_box.configure(state="disabled")
            self.log_box.see("end")

        self.quota_label.configure(text=quota_text)
        self._set_status(is_watching)

        # ── show correct page (newly built pages are not yet packed) ─────
        {"main": self._show_main, "settings": self._show_settings,
         "replays": self._show_replays
         }.get(cur_page, self._show_main)()

        # ── reapply canvas colours ────────────────────────────────────────
        self.after(50, self._apply_canvas_theme)
        if cur_page == "replays":
            self.after(100, lambda: self._apply_filters(sort=True))
        else:
            # update internal card positions in background without forcing
            # a full layout pass on the visible page
            self.after(200, lambda: self._apply_filters(sort=True))

    def _on_theme_change(self, value: str):
        """Called immediately when the user clicks a theme segment."""
        ctk.set_appearance_mode(value)
        if hasattr(self, "canvas"):
            self.after(30, self._apply_canvas_theme)
            self.after(60, lambda: self._apply_filters(sort=True))

    def _apply_canvas_theme(self):
        self.canvas.configure(bg=_c(C_BG))
        if hasattr(self, '_recent_canvas'):
            self._recent_canvas.configure(bg=_c(C_BG))
            self._update_recent_replays()
        dark = ctk.get_appearance_mode() == "Dark"
        if dark:
            self._vsb_style.configure("Vertical.TScrollbar",
                troughcolor="#1e1e1e", background="#3a3a3a",
                bordercolor="#1e1e1e", arrowcolor="#aaaaaa", relief="flat")
        else:
            self._vsb_style.configure("Vertical.TScrollbar",
                troughcolor="#d8d8d8", background="#b0b0b0",
                bordercolor="#d8d8d8", arrowcolor="#444444", relief="flat")

    def _show_replays(self):
        self.main_page.pack_forget()
        self.settings_page.pack_forget()
        self.replays_page.pack(fill="both", expand=True)
        self.nav_btn.configure(text="←")
        self._current_page = "replays"
        self._apply_canvas_theme()
        if not self._cards:
            self._load_replays()
        else:
            # update_idletasks() flushes tkinter's pending geometry work so
            # canvas.winfo_width() returns the real width (not the stale 1px
            # value from when the canvas was hidden).  Without this, cards
            # added by the filesystem watcher while on another page keep the
            # wrong grid_cols/grid_col/_max_w from the narrow-canvas run.
            self.update_idletasks()
            self._apply_filters()

    def _on_nav(self):
        if self._current_page == "main":
            self._show_settings()
        elif self._current_page == "detail":
            if getattr(self, '_detail_back', 'replays') == 'main':
                self._show_main()
            else:
                self._show_replays_from_detail()
        else:
            self._show_main()

    # ── settings actions ──────────────────────────────────────────────────────

    def _toggle_key_visibility(self):
        self.api_entry.configure(show="" if self.api_entry.cget("show") == "•" else "•")

    def _browse_folder(self):
        folder = filedialog.askdirectory(title="Select Rocket League Demos Folder")
        if folder:
            self.folder_entry.delete(0, "end")
            self.folder_entry.insert(0, folder)
            self._save_settings()

    def _browse_sync_folder(self):
        folder = filedialog.askdirectory(title="Select Secondary Demos Folder")
        if folder:
            self.sync_entry.delete(0, "end")
            self.sync_entry.insert(0, folder)
            self._save_settings()

    def _find_docs_roots(self) -> list:
        """Return all plausible Documents folder paths on this machine."""
        roots = []
        home  = Path.home()

        # 1. Registry — most reliable, handles OneDrive redirect
        for key_path, val_name in [
            (r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",      "Personal"),
            (r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders", "Personal"),
        ]:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
                    val = os.path.expandvars(winreg.QueryValueEx(k, val_name)[0])
                    p   = Path(val)
                    if p.is_dir() and p not in roots:
                        roots.append(p)
            except Exception:
                pass

        # 2. Common fallbacks
        for candidate in [
            home / "Documents",
            home / "OneDrive" / "Documents",
        ]:
            if candidate.is_dir() and candidate not in roots:
                roots.append(candidate)

        # 3. Corporate / personal OneDrive folders (OneDrive - CompanyName)
        try:
            for child in home.iterdir():
                if child.is_dir() and child.name.lower().startswith("onedrive"):
                    d = child / "Documents"
                    if d.is_dir() and d not in roots:
                        roots.append(d)
        except Exception:
            pass

        return roots

    def _auto_detect_demos_folder(self):
        try:
            self._auto_detect_demos_folder_inner()
        except Exception as e:
            messagebox.showerror("Auto-detect error", str(e))

    def _auto_detect_demos_folder_inner(self):
        import re as _re
        roots = self._find_docs_roots()
        candidates = []

        def _add(p: Path):
            if p.is_dir() and p not in [c[0] for c in candidates]:
                count = sum(1 for _ in p.glob("*.replay"))
                candidates.append((p, count))

        # ── 1. Documents folders (Epic saves replays here) ────────────────────
        for docs_root in roots:
            tadir = docs_root / "My Games" / "Rocket League" / "TAGame"
            for sub in ("DemosEpic", "Demos"):
                _add(tadir / sub)

        # ── 2. Steam library folders (VDF) ────────────────────────────────────
        steam_base = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Steam"
        vdf = steam_base / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            try:
                text = vdf.read_text(encoding="utf-8", errors="ignore")
                for match in _re.finditer(r'"path"\s+"([^"]+)"', text):
                    lib = Path(match.group(1))
                    for sub in ("DemosEpic", "Demos"):
                        _add(lib / "steamapps" / "common" / "rocketleague" / "TAGame" / sub)
            except Exception:
                pass

        # ── 3. Brute-force every drive letter A–Z ────────────────────────────
        common_subdirs = [
            "",                          # root of drive
            "Games",
            "SteamLibrary",
            "Steam",
            "Program Files",
            "Program Files (x86)",
        ]
        rl_tail_steam = Path("steamapps") / "common" / "rocketleague" / "TAGame"
        rl_tail_bare  = Path("rocketleague") / "TAGame"

        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:/")
            if not drive.exists():
                continue
            for sub_dir in common_subdirs:
                base = drive / sub_dir if sub_dir else drive
                # Steam-style layout
                for sub in ("DemosEpic", "Demos"):
                    _add(base / rl_tail_steam / sub)
                # Bare install layout  (e.g. D:\Games\rocketleague\TAGame\DemosEpic)
                for sub in ("DemosEpic", "Demos"):
                    _add(base / rl_tail_bare / sub)
                # Documents-style on non-C drives
                for user_folder in (base / "Users").glob("*") if (base / "Users").is_dir() else []:
                    docs = user_folder / "Documents"
                    tadir = docs / "My Games" / "Rocket League" / "TAGame"
                    for sub in ("DemosEpic", "Demos"):
                        _add(tadir / sub)

        if not candidates:
            messagebox.showinfo("Auto-detect",
                                "Could not find a Rocket League demos folder.\n"
                                "Please set the path manually using Browse.")
            return

        if len(candidates) == 1:
            chosen = str(candidates[0][0])
            mirror = ""
        else:
            # ── Step 1: pick main folder ──────────────────────────────────────
            root = tk.Toplevel(self)
            root.title("Select Main Demos Folder")
            root.resizable(False, False)
            root.grab_set()
            root.attributes("-topmost", True)
            root.minsize(380, 0)
            ctk.CTkLabel(root,
                         text="Multiple folders found.\nWhich one is your Main Demos Folder?",
                         font=ctk.CTkFont(size=13, weight="bold"),
                         text_color="black", justify="center"
                         ).pack(padx=24, pady=(18, 12))
            chosen_var = tk.StringVar(value=str(candidates[0][0]))
            for p, count in candidates:
                sub    = p.name
                client = "Epic Games" if sub == "DemosEpic" else "Steam"
                row = ctk.CTkFrame(root, fg_color="transparent")
                row.pack(fill="x", padx=16, pady=(6, 2))
                ctk.CTkRadioButton(
                    row,
                    text=f"{client}  —  {count} replay{'s' if count != 1 else ''}",
                    font=ctk.CTkFont(size=14, weight="bold"),
                    text_color="black",
                    variable=chosen_var, value=str(p)
                ).pack(anchor="w")
                ctk.CTkLabel(
                    row, text=str(p),
                    font=ctk.CTkFont(size=11),
                    text_color="black",
                    wraplength=360, justify="left"
                ).pack(anchor="w", padx=(28, 0))
            ctk.CTkButton(root, text="Confirm main folder",
                          command=root.destroy).pack(pady=(12, 18))
            self.wait_window(root)
            chosen = chosen_var.get()

            # ── Step 2: pick secondary folder from remaining (or browse) ──────
            remaining = [(p, c) for p, c in candidates if str(p) != chosen]
            mirror = ""
            if remaining:
                root2 = tk.Toplevel(self)
                root2.title("Select Secondary Demos Folder")
                root2.resizable(False, False)
                root2.grab_set()
                root2.attributes("-topmost", True)
                root2.minsize(380, 0)
                root2.protocol("WM_DELETE_WINDOW",
                               lambda: (mirror_var.set(""), root2.destroy()))
                ctk.CTkLabel(root2,
                             text="Select a Secondary Demos Folder\n(replays will be moved from here into the main folder)",
                             font=ctk.CTkFont(size=13, weight="bold"),
                             text_color="black", justify="center"
                             ).pack(padx=24, pady=(18, 12))
                mirror_var = tk.StringVar(value=str(remaining[0][0]))
                for p, count in remaining:
                    sub    = p.name
                    client = "Epic Games" if sub == "DemosEpic" else "Steam"
                    row = ctk.CTkFrame(root2, fg_color="transparent")
                    row.pack(fill="x", padx=16, pady=(6, 2))
                    ctk.CTkRadioButton(
                        row,
                        text=f"{client}  —  {count} replay{'s' if count != 1 else ''}",
                        font=ctk.CTkFont(size=14, weight="bold"),
                        text_color="black",
                        variable=mirror_var, value=str(p)
                    ).pack(anchor="w")
                    ctk.CTkLabel(
                        row, text=str(p),
                        font=ctk.CTkFont(size=11),
                        text_color="black",
                        wraplength=360, justify="left"
                    ).pack(anchor="w", padx=(28, 0))
                # Browse button lets user pick a completely different path
                def _browse_secondary():
                    picked = filedialog.askdirectory(
                        title="Select Secondary Demos Folder", parent=root2)
                    if picked:
                        mirror_var.set(picked)
                btn_row = ctk.CTkFrame(root2, fg_color="transparent")
                btn_row.pack(pady=(12, 18))
                ctk.CTkButton(btn_row, text="Confirm secondary folder",
                              command=root2.destroy).pack(side="left", padx=(0, 8))
                ctk.CTkButton(btn_row, text="Browse…",
                              command=_browse_secondary,
                              fg_color="transparent", border_width=1,
                              text_color="black").pack(side="left")
                ctk.CTkButton(root2, text="Skip — no secondary folder",
                              fg_color="transparent", border_width=0,
                              text_color="gray",
                              command=lambda: (mirror_var.set(""), root2.destroy())
                              ).pack(pady=(0, 10))
                self.wait_window(root2)
                mirror = mirror_var.get()

        if chosen:
            self.folder_entry.delete(0, "end")
            self.folder_entry.insert(0, chosen)
            self.config_data["demos_folder"] = chosen
            if mirror:
                self.sync_entry.delete(0, "end")
                self.sync_entry.insert(0, mirror)
                self.config_data["sync_folder"] = mirror
            save_config(self.config_data)

    def _reparse_for_ids(self):
        demos_folder = Path(self.config_data.get("demos_folder", ""))
        if not CACHE_DIR.exists():
            messagebox.showinfo("Re-parse", "No cached replays found.")
            return

        to_reparse = []
        for cf in CACHE_DIR.glob("*.json"):
            if cf.name.startswith("bc_"):
                continue
            try:
                with open(cf, encoding="utf-8") as f:
                    info = json.load(f)
            except Exception:
                continue
            already_current = info.get("_v") == CACHE_VERSION
            needs = not already_current and any(
                p.get("platform", "").lower() in ("epic", "steam")
                and not p.get("online_id", "")
                for p in info.get("players", [])
            )
            if not needs:
                continue
            replay_path = demos_folder / cf.stem  # cf.stem = "abc123.replay"
            if replay_path.exists():
                to_reparse.append((cf.stem, replay_path))

        if not to_reparse:
            messagebox.showinfo("Re-parse", "All replays already have player IDs — nothing to do.")
            return

        self._reparse_btn.configure(state="disabled", text=f"Re-parsing 0/{len(to_reparse)}…")

        def worker():
            updated, total = 0, len(to_reparse)
            for i, (name, rp) in enumerate(to_reparse):
                self.after(0, lambda i=i: self._reparse_btn.configure(
                    text=f"Re-parsing {i+1}/{total}…"))
                info = parse_card_data(rp)
                if info:
                    save_cache(rp.name, info)
                    updated += 1
            self.after(0, lambda u=updated, t=total: (
                self._reparse_btn.configure(
                    state="normal",
                    text="Re-parse replays for player IDs"),
                messagebox.showinfo(
                    "Re-parse complete",
                    f"Updated {u} of {t} replays with player IDs.")))

        threading.Thread(target=worker, daemon=True).start()

    def _save_settings(self):
        old_key    = self.config_data.get("api_key", "")
        old_folder = self.config_data.get("demos_folder", "")
        old_sync   = self.config_data.get("sync_folder", "")
        self.config_data["api_key"]          = self.api_entry.get().strip()
        self.config_data["demos_folder"]     = self.folder_entry.get().strip()
        self.config_data["sync_folder"]      = self.sync_entry.get().strip()
        self.config_data["visibility"]       = self.vis_var.get()
        self.config_data["upload_on_detect"] = self.upload_on_detect_var.get()
        self.config_data["desktop_shortcut"] = self.desktop_shortcut_var.get()
        self.config_data["launch_with_rl"]   = self.launch_with_rl_var.get()
        self.config_data["auto_fetch_bc"]     = self.auto_fetch_bc_var.get()
        self.config_data["low_priority_mode"] = self.low_priority_var.get()
        self._update_priority_for_focus()
        self.config_data["theme"]            = self.theme_var.get()
        try:
            limit = int(self.replay_limit_var.get())
            if limit != 0 and limit < 50:
                limit = 50
                self.replay_limit_var.set("50")
        except ValueError:
            limit = 0
            self.replay_limit_var.set("0")
        self.config_data["replay_limit"] = limit
        ctk.set_appearance_mode(self.theme_var.get())
        if hasattr(self, "canvas"):
            self.after(50, self._apply_canvas_theme)
            self.after(80, lambda: self._apply_filters(sort=True))
        save_config(self.config_data)
        self._set_run_on_startup(self.run_on_startup_var.get())
        self._set_launch_with_rl(self.launch_with_rl_var.get())
        # Reset button if it was showing an error (not currently watching or checking)
        current_text = self.toggle_btn.cget("text")
        if not (self.observer and self.observer.is_alive()) and current_text not in ("Stop Watching", "Checking…"):
            self._set_status(False)

        changed_watch = (
            self.config_data["api_key"] != old_key
            or self.config_data["demos_folder"] != old_folder
            or self.config_data["sync_folder"] != old_sync
        )
        if changed_watch and self.observer and self.observer.is_alive():
            self._stop_watching()
            self.after(200, self._start_watching)
        if hasattr(self, "_save_btn"):
            self._save_btn.configure(fg_color="#2d7a4f")
            self.after(1200, lambda: self._save_btn.configure(fg_color=["#3B8ED0", "#1F6AA5"]))

    # ── startup / RL watcher helpers ──────────────────────────────────────────

    _REG_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _APP_KEY  = "BallchasingUploader"

    def _launcher_cmd(self) -> str:
        # If the compiled exe exists, register that directly — no Python needed.
        exe = BASE_DIR.parent / "BallchasingUploader.exe"
        if exe.exists():
            return f'"{exe}"'
        # Fallback: launch via pythonw when running from source.
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.exists():
            pythonw = Path(sys.executable)
        launcher = BASE_DIR.parent / "launcher.py"
        return f'"{pythonw}" "{launcher}"'

    def _get_run_on_startup(self) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._REG_RUN) as k:
                winreg.QueryValueEx(k, self._APP_KEY)
                return True
        except OSError:
            return False

    def _setup_priority_focus(self):
        """After startup: bind focus events so priority drops when app is in background."""
        self._update_priority_for_focus()
        self.bind("<FocusIn>",  lambda e: self._on_app_focus(True),  add="+")
        self.bind("<FocusOut>", lambda e: self._on_app_focus(False), add="+")

    def _on_app_focus(self, focused: bool):
        """Called when the app gains or loses focus."""
        if self.config_data.get("low_priority_mode", False):
            _set_process_priority(low=not focused)

    def _update_priority_for_focus(self):
        """Apply correct priority based on current focus and setting."""
        if self.config_data.get("low_priority_mode", False):
            focused = self.focus_get() is not None
            _set_process_priority(low=not focused)
        else:
            _set_process_priority(low=False)

    def _set_run_on_startup(self, enabled: bool):
        try:
            cmd = self._launcher_cmd()
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._REG_RUN, 0,
                                winreg.KEY_SET_VALUE) as k:
                if enabled:
                    winreg.SetValueEx(k, self._APP_KEY, 0, winreg.REG_SZ, cmd)
                    self._log(f"[startup] Registered: {cmd}")
                else:
                    try:
                        winreg.DeleteValue(k, self._APP_KEY)
                        self._log("[startup] Removed from startup")
                    except OSError:
                        pass
        except Exception as e:
            self._log(f"[startup] Could not update registry: {e}", "red")

    def _sync_desktop_shortcut(self):
        """Create or remove the desktop shortcut based on the user's setting.
        Called on startup so the shortcut always matches the config, even if
        the frozen launcher created it unconditionally on a previous version."""
        want    = self.config_data.get("desktop_shortcut", False)
        desktop = Path.home() / "Desktop" / "Ballchasing Uploader.lnk"
        exe     = BASE_DIR.parent / "BallchasingUploader.exe"
        if not want:
            if desktop.exists():
                try:
                    desktop.unlink()
                except Exception:
                    pass
        else:
            if not desktop.exists() and exe.exists():
                try:
                    import subprocess
                    ps = (
                        f"$ws=New-Object -ComObject WScript.Shell; "
                        f"$s=$ws.CreateShortcut('{desktop}'); "
                        f"$s.TargetPath='{exe}'; "
                        f"$s.WorkingDirectory='{BASE_DIR.parent}'; "
                        f"$s.Description='Ballchasing Auto Uploader'; "
                        f"$s.Save()"
                    )
                    subprocess.run(
                        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                        capture_output=True, timeout=10
                    )
                except Exception:
                    pass

    def _set_launch_with_rl(self, enabled: bool):
        if enabled and not getattr(self, "_rl_poll_thread_running", False):
            self._rl_poll_thread_running = True
            t = threading.Thread(target=self._rl_poll_loop, daemon=True)
            t.start()
        elif not enabled:
            self._rl_poll_thread_running = False

    def _rl_poll_loop(self):
        was_running = False
        while getattr(self, "_rl_poll_thread_running", False):
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq RocketLeague.exe"],
                    capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                now = "RocketLeague.exe" in result.stdout
            except Exception:
                now = False
            if now and not was_running:
                if not (self.observer and self.observer.is_alive()):
                    self.after(0, self._start_watching)
            elif not now and was_running:
                if self.observer and self.observer.is_alive():
                    self.after(0, self._stop_watching)
            was_running = now
            time.sleep(5)

    # ── watcher ───────────────────────────────────────────────────────────────

    def _toggle_watching(self):
        if self.observer and self.observer.is_alive():
            self._stop_watching()
        else:
            self._start_watching()

    def _set_btn_error(self, msg: str):
        """Show an error message inside the Start Watching button. Persists until cleared."""
        self.toggle_btn.configure(text=msg,
                                  fg_color=("gray75", "gray25"),
                                  hover_color=("gray70", "gray30"),
                                  text_color="#e06060")

    def _start_watching(self):
        api_key = self.config_data.get("api_key", "").strip()
        folder  = self.config_data.get("demos_folder", "").strip()
        missing = []
        if not api_key:
            missing.append("API key")
        if not folder or not Path(folder).is_dir():
            missing.append("replay folder path")
        if missing:
            self._set_btn_error(f"⚠ Missing {' and '.join(missing)} — open Settings ⚙")
            return

        self.toggle_btn.configure(state="disabled", text="Checking…",
                                  fg_color=("gray75", "gray25"),
                                  text_color=("gray40", "gray60"))

        def ping():
            try:
                resp = requests.get("https://ballchasing.com/api/",
                                    headers={"Authorization": api_key}, timeout=8)
                if resp.status_code == 200:
                    self.after(0, self._do_start_watching, True)
                elif resp.status_code in (401, 403):
                    self.after(0, self._prompt_start_no_upload,
                               "Your API key was rejected by Ballchasing (invalid or expired).")
                else:
                    self.after(0, self._prompt_start_no_upload,
                               f"Ballchasing returned an unexpected error ({resp.status_code}).")
            except Exception:
                self.after(0, self._prompt_start_no_upload,
                           "Could not reach ballchasing.com.")

        threading.Thread(target=ping, daemon=True).start()

    def _prompt_start_no_upload(self, reason: str):
        self._set_status(False)   # restores button to normal "Start Watching" state
        answer = messagebox.askyesno(
            "Connection Issue",
            f"{reason}\n\nStart watcher without auto upload?",
            icon="warning")
        if answer:
            self._do_start_watching(upload_enabled=False)

    def _do_start_watching(self, upload_enabled: bool):
        self.toggle_btn.configure(state="normal")
        self._upload_session_enabled = upload_enabled
        folder = self.config_data.get("demos_folder", "").strip()

        def on_new_replay(filename: str):
            if self._download_active:
                return
            self.after(0, self._log, f"new replay detected: {filename}")
            path = Path(folder) / filename

            from_mirror = filename in self._mirror_no_upload
            if from_mirror:
                self._mirror_no_upload.discard(filename)

            if not from_mirror and self._upload_session_enabled and self.config_data.get("upload_on_detect", True):

                def do_upload(p=path, n=filename):
                    def on_status(name, status):
                        icons = {"uploading": "⟳", "uploaded": "✓", "duplicate": "=",
                                 "skipped": "–", "failed": "✗"}
                        tag = "green" if status in ("uploaded", "duplicate") else (
                              "red"   if status == "failed" else None)
                        self.after(0, self._log,
                                   f"{icons.get(status, '?')} {name}  [{status}]", tag)
                        if status in ("uploaded", "duplicate"):
                            def _mark(nm=name):
                                idx = self._card_index.get(nm)
                                if idx is not None:
                                    self._cards[idx]["uploaded"] = True
                                self.uploaded.add(nm)
                                self._schedule_save_uploaded()
                                self._schedule_redraw()
                            self.after(0, _mark)
                            if status == "uploaded":
                                self.after(2000, self._fetch_quota)
                                self.after(0, self._show_toast,
                                           f"✓ Uploaded: {name}")
                                self.after(3000, lambda: self._dedup(silent=True))
                        elif status == "failed":
                            def _handle_fail(_p=p, _n=n):
                                attempt = self._retry_queue.get(_n, 0) + 1
                                if attempt <= 6:
                                    self._retry_queue[_n] = attempt
                                    self._log(f"  ↺ retry {attempt}/6 in 5 min: {_n}", "red")
                                    self.after(300_000,
                                               lambda __p=_p, __n=_n: self._retry_upload(__p, __n))
                                else:
                                    self._retry_queue.pop(_n, None)
                                    self._log(f"  ✗ gave up after 6 retries: {_n}", "red")
                            self.after(0, _handle_fail)
                    upload(p, self.config_data, self.uploaded, on_status,
                           on_bc_id=lambda bc_id, fn=n: (
                               save_upload_id(fn, bc_id),
                               self.after(0, self._set_card_bc_id, fn, bc_id)))

                threading.Thread(target=do_upload, daemon=True).start()

            self.after(0, self._bg_cache_replays)
            # Add the new replay card so Recent Replays updates immediately
            self.after(500, lambda fn=filename, fo=folder: self._add_detected_card(fn, fo))
            # Clean orphans and enforce limit — no need to MD5-hash everything
            # for a single new file; just check if this file itself is a duplicate
            self.after(1000, self._cleanup_orphans)
            self.after(1500, self._enforce_replay_limit)
            self.after(2000, lambda fn=filename, fo=folder: self._check_single_dupe(fn, fo))

        handler = ReplayHandler(on_new_replay)
        self.observer = Observer()
        self.observer.schedule(handler, folder, recursive=False)

        # ── mirror folder: move new replays into the primary folder ──────────
        sync_folder = self.config_data.get("sync_folder", "").strip()
        if sync_folder and Path(sync_folder).is_dir() and sync_folder != folder:
            def on_sync_replay(filename: str, _src=sync_folder, _dst=folder):
                src_path = Path(_src) / filename
                dst_path = Path(_dst) / filename
                if dst_path.exists():
                    try:
                        src_path.unlink()
                    except Exception:
                        pass
                    return
                try:
                    # Flag before move so on_new_replay skips upload when watchdog fires
                    self._mirror_no_upload.add(filename)
                    shutil.move(str(src_path), str(dst_path))
                    self.after(0, self._log,
                               f"[sync] moved from mirror: {filename}")
                except Exception as e:
                    self._mirror_no_upload.discard(filename)
                    self.after(0, self._log,
                               f"[sync] failed to move {filename}: {e}", "red")
            self.observer.schedule(ReplayHandler(on_sync_replay),
                                   sync_folder, recursive=False)
            self._log(f"Mirroring from: {sync_folder}")

        self.observer.start()
        self._set_status(True)
        self._log(f"Watching: {folder}")
        if not upload_enabled:
            self._log("Auto upload disabled for this session.", "red")

    def _stop_watching(self):
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=3)   # don't hang UI if observer is stuck
            self.observer = None
        self._set_status(False)
        self._log("Stopped watching.")

    # ── bulk download ─────────────────────────────────────────────────────────

    def _ask_download_limit(self) -> int | None:
        """Modal dialog — returns limit (0 = all) or None if cancelled."""
        result = [None]
        dlg = ctk.CTkToplevel(self)
        dlg.title("Download Replays")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.attributes("-topmost", True)

        ctk.CTkLabel(dlg, text="How many replays to download?",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(padx=28, pady=(22, 6))
        ctk.CTkLabel(dlg, text="Most recent replays are downloaded first.",
                     font=ctk.CTkFont(size=12), text_color=C_DATE).pack(padx=28, pady=(0, 16))

        presets = ctk.CTkFrame(dlg, fg_color="transparent")
        presets.pack(padx=28, pady=(0, 12))
        for label, val in [("Last 50", 50), ("Last 100", 100), ("Last 500", 500), ("All", 0)]:
            ctk.CTkButton(presets, text=label, width=80,
                          command=lambda v=val: (result.__setitem__(0, v), dlg.destroy())
                          ).pack(side="left", padx=4)

        ctk.CTkLabel(dlg, text="Or enter a number:",
                     font=ctk.CTkFont(size=12)).pack(padx=28, pady=(0, 6))
        custom_row = ctk.CTkFrame(dlg, fg_color="transparent")
        custom_row.pack(padx=28, pady=(0, 22))
        entry = ctk.CTkEntry(custom_row, width=110, placeholder_text="e.g. 250")
        entry.pack(side="left", padx=(0, 8))

        def _ok():
            try:
                val = int(entry.get().strip())
                if val > 0:
                    result[0] = val
                    dlg.destroy()
            except ValueError:
                pass

        ctk.CTkButton(custom_row, text="OK", width=60, command=_ok).pack(side="left")
        dlg.wait_window()
        return result[0]

    def _confirm_download(self):
        api_key = self.config_data.get("api_key", "").strip()
        folder  = self.config_data.get("demos_folder", "").strip()
        if not api_key or not folder or not Path(folder).is_dir():
            messagebox.showerror("Missing Config",
                                 "A valid API key and demos folder are required.")
            return

        limit = self._ask_download_limit()
        if limit is None:
            return

        self._show_main()
        label = f"{limit:,}" if limit > 0 else "all"
        self._log(f"[download] Checking Ballchasing for {label} replays…")

        def fetch_count():
            url    = "https://ballchasing.com/api/replays"
            params = {"count": 200, "uploader": "me"}
            total  = 0
            try:
                with requests.Session() as s:
                    s.headers.update({"Authorization": api_key})
                    while True:
                        resp = s.get(url, params=params, timeout=15)
                        if resp.status_code == 429:
                            time.sleep(int(resp.headers.get("Retry-After", 10)))
                            continue
                        if resp.status_code != 200:
                            self.after(0, self._log,
                                       f"[download] Count fetch failed: {resp.status_code}", "red")
                            return
                        data   = resp.json()
                        total += len(data.get("list", []))
                        self.after(0, self._update_count_log, total)
                        # stop counting once we've confirmed enough exist
                        if limit > 0 and total >= limit:
                            total = limit
                            break
                        next_url = data.get("next")
                        if not next_url:
                            break
                        url    = next_url
                        params = {}
            except Exception as e:
                self.after(0, self._log, f"[download] Count fetch error: {e}", "red")
                return
            self.after(0, self._show_download_summary, total, limit, api_key, folder)

        threading.Thread(target=fetch_count, daemon=True).start()

    def _update_count_log(self, total: int):
        tb = self.log_box._textbox
        self.log_box.configure(state="normal")
        last_line = int(tb.index("end-1c").split(".")[0])
        last_text = tb.get(f"{last_line}.0", f"{last_line}.end")
        if "[download] Checking" in last_text or "[download] Counting" in last_text:
            tb.delete(f"{last_line}.0", f"{last_line}.end")
            tb.insert(f"{last_line}.0", f"[download] Counting… {total:,} so far")
        else:
            tb.insert("end", f"\n[download] Counting… {total:,} so far")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _show_download_summary(self, total: int, limit: int, api_key: str, folder: str):
        fewer = limit > 0 and total < limit
        if fewer:
            self._log(f"[download] Only {total:,} replays found (you requested {limit:,}).")
        else:
            self._log(f"[download] Found {total:,} replays.")

        max_bytes = total * 3 * 1024 * 1024  # 3 MB worst-case per replay
        max_gb    = max_bytes / (1024 ** 3)
        size_str  = f"~{max_gb:.1f} GB" if max_gb >= 1 else f"~{max_bytes // (1024*1024)} MB"

        # ── storage check ────────────────────────────────────────────────────
        try:
            free_bytes = shutil.disk_usage(folder).free
            free_gb    = free_bytes / (1024 ** 3)
            free_str   = f"{free_gb:.1f} GB"
        except Exception:
            free_bytes = None
            free_str   = "unknown"

        if free_bytes is not None and free_bytes < max_bytes:
            messagebox.showerror(
                "Not Enough Disk Space",
                f"Not enough free space.\n\n"
                f"Needed (worst case):  {size_str}\n"
                f"Available:            {free_str}\n\n"
                f"Free up space and try again.")
            self._log("[download] Cancelled — not enough disk space.", "red")
            return

        # ── time estimate (rough — adaptive rate will vary) ──────────────────
        secs     = total
        hours    = secs // 3600
        mins     = (secs % 3600) // 60
        time_str = f"~{hours}h {mins}m" if hours else f"~{mins}m"

        notice = f"\nNote: only {total:,} replays exist on Ballchasing.\n" if fewer else ""
        if total >= 1000:
            if not messagebox.askyesno(
                    "Ready to Download",
                    f"{notice}"
                    f"Replays to download: {total:,}\n"
                    f"Max storage needed:  {size_str}  (3 MB per replay)\n"
                    f"Free space:          {free_str}\n"
                    f"Est. time:           {time_str}\n\n"
                    f"Start download?",
                    icon="info"):
                self._log("[download] Cancelled.")
                return
        elif fewer:
            messagebox.showinfo("Note", f"Only {total:,} replays exist on Ballchasing. Starting download.")


        self._start_bulk_download(api_key, folder, total)

    def _cleanup_orphans(self, folder: str | None = None):
        """Fast background sweep: remove cache/upload entries with no matching replay.
        No MD5 hashing — safe to call at every startup."""
        if folder is None:
            folder = self.config_data.get("demos_folder", "").strip()
        if not folder or not Path(folder).is_dir():
            return

        uploaded_snapshot = set(self.uploaded)   # snapshot on main thread

        def worker():
            replay_names = {p.name for p in Path(folder).glob("*.replay") if p.is_file()}

            # orphaned cache files (skip bc_*.json — those are Ballchasing stat caches)
            if CACHE_DIR.exists():
                for cf in CACHE_DIR.glob("*.json"):
                    if cf.name.startswith("bc_"):
                        continue
                    if cf.stem not in replay_names:
                        try: cf.unlink()
                        except OSError: pass

            # orphaned upload_ids
            upload_ids = load_upload_ids()
            clean_ids  = {k: v for k, v in upload_ids.items() if k in replay_names}
            if len(clean_ids) < len(upload_ids):
                _atomic_write_json(UPLOAD_IDS_FILE, clean_ids)

            # orphaned uploaded.json entries
            orphaned_ul = uploaded_snapshot - replay_names
            if orphaned_ul:
                self.after(0, lambda: self.uploaded.difference_update(orphaned_ul))

        threading.Thread(target=worker, daemon=True).start()

    def _scan_mirror_folder(self):
        """On startup: move any .replay files in the mirror folder into the main folder."""
        sync_folder = self.config_data.get("sync_folder", "").strip()
        folder      = self.config_data.get("demos_folder", "").strip()
        if not sync_folder or not folder:
            return
        sync_path = Path(sync_folder)
        main_path = Path(folder)
        if not sync_path.is_dir() or not main_path.is_dir() or sync_path == main_path:
            return

        def worker():
            for src_path in sync_path.glob("*.replay"):
                dst_path = main_path / src_path.name
                if dst_path.exists():
                    try:
                        src_path.unlink()
                    except Exception:
                        pass
                    continue
                try:
                    # Flag before move so on_new_replay skips upload if watcher already running
                    self._mirror_no_upload.add(src_path.name)
                    shutil.move(str(src_path), str(dst_path))
                    self.after(0, self._log,
                               f"[sync] moved from mirror on startup: {src_path.name}")
                except Exception as e:
                    self._mirror_no_upload.discard(src_path.name)
                    self.after(0, self._log,
                               f"[sync] failed to move {src_path.name}: {e}", "red")

        threading.Thread(target=worker, daemon=True).start()

    def _enforce_replay_limit(self):
        """Delete oldest replays (by file date) when the folder exceeds the configured limit."""
        limit = int(self.config_data.get("replay_limit", 0))
        if limit < 50:
            return  # 0 = disabled; anything under 50 is ignored
        folder = self.config_data.get("demos_folder", "").strip()
        if not folder or not Path(folder).is_dir():
            return

        def worker():
            replays = sorted(
                Path(folder).glob("*.replay"),
                key=lambda p: p.stat().st_mtime
            )
            over = len(replays) - limit
            if over <= 0:
                return
            for p in replays[:over]:
                try:
                    p.unlink()
                    self.after(0, self._log,
                               f"[limit] deleted oldest replay: {p.name}", "red")
                    self.after(0, lambda n=p.name: self._remove_card(n))
                except OSError as e:
                    self.after(0, self._log,
                               f"[limit] could not delete {p.name}: {e}", "red")

        threading.Thread(target=worker, daemon=True).start()

    def _check_single_dupe(self, filename: str, folder: str):
        """Fast duplicate check for a single newly-detected replay.
        Only hashes the one new file and compares against existing cards —
        far cheaper than full dedup which hashes every file."""
        def worker():
            path = Path(folder) / filename
            if not path.exists():
                return
            try:
                # Hash only the new file
                m = hashlib.md5()
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(65536), b""):
                        m.update(chunk)
                new_hash = m.hexdigest()
            except OSError:
                return

            # Also get its rl_id from cache (if already parsed)
            new_rl_id = ""
            cached = load_cached(filename)
            if cached:
                new_rl_id = _norm_rl_id(cached.get("rl_id", ""))

            # Compare against every existing card
            for card in list(self._cards):
                if card["filename"] == filename:
                    continue
                other_path = card["path"]
                if not other_path.exists():
                    continue
                # Check by rl_id first (fast, no disk read)
                if new_rl_id:
                    other_info = load_cached(card["filename"]) or {}
                    if _norm_rl_id(other_info.get("rl_id", "")) == new_rl_id:
                        # Same match — delete the older file
                        older = path if path.stat().st_mtime < other_path.stat().st_mtime else other_path
                        try:
                            older.unlink()
                            self.after(0, self._log, f"[dedup] Removed duplicate: {older.name}")
                            self.after(300, self._load_replays)
                        except OSError:
                            pass
                        return
                # Fall back to MD5 only if rl_id unavailable
                try:
                    m2 = hashlib.md5()
                    with open(other_path, "rb") as fh:
                        for chunk in iter(lambda: fh.read(65536), b""):
                            m2.update(chunk)
                    if m2.hexdigest() == new_hash:
                        older = path if path.stat().st_mtime < other_path.stat().st_mtime else other_path
                        try:
                            older.unlink()
                            self.after(0, self._log, f"[dedup] Removed duplicate: {older.name}")
                            self.after(300, self._load_replays)
                        except OSError:
                            pass
                        return
                except OSError:
                    continue
        threading.Thread(target=worker, daemon=True).start()

    def _run_manual_dedup(self):
        """Triggered from Settings — runs full dedup with a time estimate logged first."""
        folder = self.config_data.get("demos_folder", "").strip()
        if not folder or not Path(folder).is_dir():
            self._log("[dedup] No demos folder configured.", "red")
            return
        try:
            count = sum(1 for _ in Path(folder).glob("*.replay"))
        except Exception:
            count = 0
        # ~50 ms per replay on a typical HDD
        secs = round(count * 0.05)
        if secs < 1:
            estimate = "< 1 second"
        elif secs < 60:
            estimate = f"~{secs} seconds"
        else:
            estimate = f"~{secs // 60}m {secs % 60}s"
        self._log(f"[dedup] Full scan of {count} replay(s) — estimated {estimate}…")
        self._show_main()   # switch back so the log is visible
        self._dedup()

    def _startup_dedup(self):
        """On startup, match replays to cache entries to find what's new.
        - Replay with no cache  → new/unknown → check individually
        - Cache with no replay  → orphan → _cleanup_orphans handles it
        Covers the swap edge case: same count but different files."""
        folder = self.config_data.get("demos_folder", "").strip()
        if not folder or not Path(folder).is_dir():
            return
        try:
            replay_files = list(Path(folder).glob("*.replay"))
        except Exception:
            return

        # Replays with no cache entry = new to the app
        new_files = [p for p in replay_files if not load_cached(p.name)]

        # Cache entries with no matching replay = deletions → orphan cleanup
        has_orphans = CACHE_DIR.exists() and any(
            cf for cf in CACHE_DIR.glob("*.json")
            if not cf.name.startswith("bc_")
            and not (Path(folder) / cf.stem).exists()
        )

        if not new_files and not has_orphans:
            return  # everything matches — nothing to do

        if new_files:
            self._log(f"[dedup] {len(new_files)} new replay(s) — checking each…")
            for i, path in enumerate(new_files):
                delay = 2000 + i * 400
                self.after(delay, lambda fn=path.name, fo=folder:
                           self._check_single_dupe(fn, fo))

        if has_orphans:
            self.after(1000, self._cleanup_orphans)

    def _dedup(self, folder: str | None = None, silent: bool = False):
        """Find and remove duplicate replays + orphaned cache/upload entries."""
        if folder is None:
            folder = self.config_data.get("demos_folder", "").strip()
        if not folder or not Path(folder).is_dir():
            if not silent:
                self._log("[dedup] No demos folder configured — skipping.")
            return

        if not silent:
            self._log("[dedup] Scanning for duplicates and orphaned entries…")

        uploaded_snapshot = set(self.uploaded)   # snapshot on main thread (thread-safe)

        def worker():
            import hashlib
            folder_path = Path(folder)
            replay_files = sorted(
                (p for p in folder_path.glob("*.replay") if p.is_file()),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
            replay_names = {p.name for p in replay_files}

            # ── 1. duplicate .replay files ────────────────────────────────────
            # group by MD5 hash (byte-identical) and by rl_id (same match, re-recorded)
            hash_groups: dict[str, list[Path]] = {}
            rl_id_groups: dict[str, list[Path]] = {}
            for path in replay_files:
                try:
                    # Chunked read — never loads more than 64 KB at once
                    # so RAM stays flat regardless of replay file size
                    m = hashlib.md5()
                    with open(path, "rb") as fh:
                        for chunk in iter(lambda: fh.read(65536), b""):
                            m.update(chunk)
                    hash_groups.setdefault(m.hexdigest(), []).append(path)
                except OSError:
                    pass
                cached = load_cached(path.name)
                rl_id = (cached or {}).get("rl_id", "")
                if rl_id:
                    rl_id_groups.setdefault(rl_id, []).append(path)

            to_delete: set[Path] = set()
            for group in hash_groups.values():
                if len(group) > 1:
                    to_delete.update(group[1:])   # keep newest (list already sorted)
            for group in rl_id_groups.values():
                survivors = [p for p in group if p not in to_delete]
                if len(survivors) > 1:
                    to_delete.update(survivors[1:])

            deleted_replays = 0
            deleted_names: set[str] = set()
            for path in to_delete:
                try:
                    path.unlink()
                    deleted_names.add(path.name)
                    deleted_replays += 1
                except OSError:
                    pass

            remaining = replay_names - deleted_names

            # ── 2. orphaned cache files ───────────────────────────────────────
            deleted_cache = 0
            if CACHE_DIR.exists():
                for cf in CACHE_DIR.glob("*.json"):
                    if cf.name.startswith("bc_"):
                        continue  # bc_*.json are Ballchasing stat caches, not replay caches
                    if cf.stem not in remaining:  # cf.stem is already "name.replay"
                        try:
                            cf.unlink()
                            deleted_cache += 1
                        except OSError:
                            pass

            # ── 3. orphaned upload_ids entries ────────────────────────────────
            upload_ids = load_upload_ids()
            clean_ids  = {k: v for k, v in upload_ids.items() if k in remaining}
            orphaned_ids = len(upload_ids) - len(clean_ids)
            if orphaned_ids:
                _atomic_write_json(UPLOAD_IDS_FILE, clean_ids)

            # ── 4. orphaned uploaded.json entries ─────────────────────────────
            orphaned_ul = len(uploaded_snapshot - remaining)
            if orphaned_ul:
                self.after(0, lambda r=remaining: self.uploaded.intersection_update(r))

            # ── log ───────────────────────────────────────────────────────────
            parts: list[str] = []
            if deleted_replays: parts.append(f"{deleted_replays} duplicate replay(s) deleted")
            if deleted_cache:   parts.append(f"{deleted_cache} orphaned cache file(s) removed")
            if orphaned_ids:    parts.append(f"{orphaned_ids} orphaned upload ID(s) cleaned")
            if orphaned_ul:     parts.append(f"{orphaned_ul} orphaned uploaded entry/entries cleaned")

            if parts:
                self.after(0, self._log, "[dedup] " + ",  ".join(parts) + ".")
            elif not silent:
                self.after(0, self._log, "[dedup] Nothing to clean up.")

            self.after(0, save_config, self.config_data)

            if deleted_replays and self._cards:
                self.after(300, self._load_replays)

        threading.Thread(target=worker, daemon=True).start()

    def _start_bulk_download(self, api_key: str, folder: str, total: int = 0):
        self._download_active  = True
        self._dl_progress_line = None
        self._dl_status_line   = None
        self._dl_error_line    = None
        self.toggle_btn.configure(text="Stop Download", fg_color="#8B4513",
                                  hover_color="#6B3410",
                                  command=self._stop_bulk_download)
        self.status_dot.configure(text_color="#e67e22")
        self.status_label.configure(text="Downloading")
        self._log("[download] Starting bulk download…")

        def worker():
            try:
                self._do_bulk_download(api_key, folder, total)
            finally:
                self._download_active = False
                self.after(0, self._finish_bulk_download)

        threading.Thread(target=worker, daemon=True).start()

    def _stop_bulk_download(self):
        self._download_active = False
        self._log("[download] Stopping…")
        self.after(500, self._bg_cache_replays)

    def _finish_bulk_download(self):
        watching = self.observer and self.observer.is_alive()
        self._set_status(watching)
        self.after(500, self._bg_cache_replays)

    @staticmethod
    def _fmt_eta(secs: float) -> str:
        if secs <= 0: return "calculating…"
        h = int(secs // 3600); m = int((secs % 3600) // 60); s = int(secs % 60)
        return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")

    def _update_dl_tracked(self, attr: str, msg: str):
        tb = self.log_box._textbox
        self.log_box.configure(state="normal")
        line_no = getattr(self, attr)
        if line_no:
            tb.delete(f"{line_no}.0", f"{line_no}.end")
            tb.insert(f"{line_no}.0", msg)
        else:
            tb.insert("end", f"\n{msg}")
            setattr(self, attr, int(tb.index("end-1c").split(".")[0]))
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _update_dl_log(self, done: int, total: int, eta: str, rate: float):
        rate_str = f"{rate:.1f}/s" if rate >= 1 else (f"1/{1/rate:.0f}s" if rate > 0 else "–")
        self._update_dl_tracked("_dl_progress_line",
                                f"[download] {done:,} / {total:,}  |  {rate_str}  |  ETA {eta}")

    def _update_dl_status(self, msg: str):
        self._update_dl_tracked("_dl_status_line", msg)

    def _update_dl_error(self, msg: str):
        self._update_dl_tracked("_dl_error_line", msg)

    def _do_bulk_download(self, api_key: str, folder: str, total_expected: int):
        dest_dir    = Path(folder)
        list_url    = "https://ballchasing.com/api/replays"
        list_params = {"count": 200, "uploader": "me",
                       "sort-by": "replay-date", "sort-dir": "desc"}
        total_dl    = 0
        total_skip  = 0
        FLOOR        = 0.5   # max delay — never slower than 2/s
        MIN_DELAY    = 0.5   # fastest target
        delay        = 1.0   # start at 1/s
        ceiling      = None  # established when first errors hit
        error_window = 0
        WINDOW_SECS  = 30
        window_start = time.time()
        start_time   = time.time()
        dl_times: deque = deque(maxlen=20)

        def _interruptible_sleep(secs):
            steps = max(1, int(secs / 0.1))
            for _ in range(steps):
                if not self._download_active: return False
                time.sleep(0.1)
            return True

        def _norm_id(s: str) -> str:
            return s.lower().replace("-", "").replace("{", "").replace("}", "")

        # Parse any uncached replays first, then build ID set from rl_id
        existing_ids: set[str] = set()
        replay_files = list(dest_dir.glob("*.replay"))
        for f in replay_files:
            cached = load_cached(f.name)
            if not cached:
                info = parse_card_data(f)
                if info:
                    save_cache(f.name, info)
                    cached = info
            if cached:
                rl_id = cached.get("rl_id", "")
                if rl_id:
                    existing_ids.add(_norm_id(rl_id))

        with requests.Session() as s:
            s.headers.update({"Authorization": api_key})
            url    = list_url
            params = list_params

            while self._download_active:
                try:
                    resp = s.get(url, params=params, timeout=30)
                except Exception as e:
                    self.after(0, self._log, f"[download] Network error: {e}", "red")
                    return

                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 10))
                    self.after(0, self._update_dl_error, f"[download] List rate limited — waiting {wait}s…")
                    if not _interruptible_sleep(wait): return
                    continue

                if resp.status_code != 200:
                    self.after(0, self._log,
                               f"[download] List fetch failed: {resp.status_code}", "red")
                    return

                data    = resp.json()
                replays = data.get("list", [])
                if not replays:
                    break

                for replay in replays:
                    if not self._download_active:
                        return

                    rid    = replay.get("id", "")
                    rl_id  = replay.get("rocket_league_id") or rid
                    orig   = rl_id + ".replay"
                    dest   = dest_dir / orig
                    if _norm_id(rl_id) in existing_ids:
                        total_skip += 1
                        continue

                    # ── download with retry ───────────────────────────────────
                    success = False
                    for attempt in range(4):
                        if not self._download_active: return
                        try:
                            dl = s.get(
                                f"https://ballchasing.com/api/replays/{rid}/file",
                                timeout=60, stream=True)
                        except Exception as e:
                            error_window += 1
                            self.after(0, self._update_dl_error, f"[download] Network error: {e}")
                            if not _interruptible_sleep(2): return
                            break

                        if dl.status_code == 429:
                            error_window += 1
                            self.after(0, self._update_dl_error, f"[download] Rate limited")
                            if not _interruptible_sleep(2): return
                            continue  # retry

                        if dl.status_code != 200:
                            error_window += 1
                            self.after(0, self._update_dl_error,
                                       f"[download] failed {rid}: {dl.status_code}")
                            if not _interruptible_sleep(2): return
                            break

                        # Write file
                        cd = dl.headers.get("Content-Disposition", "")
                        if "filename=" in cd:
                            fname = cd.split("filename=")[-1].strip().strip('"')
                            dest  = dest_dir / fname
                        with open(dest, "wb") as f:
                            for chunk in dl.iter_content(chunk_size=65536):
                                f.write(chunk)
                        # Set mtime to the replay's actual match date so that
                        # mtime-based sort matches Rocket League's replay order,
                        # regardless of download order (which is newest-first).
                        replay_date = replay.get("date", "")
                        if replay_date:
                            try:
                                dt = datetime.fromisoformat(
                                    replay_date.replace("Z", "+00:00"))
                                ts = dt.timestamp()
                                os.utime(dest, (ts, ts))
                            except Exception:
                                pass
                        existing_ids.add(_norm_id(dest.stem))
                        save_upload_id(dest.name, rid)
                        # Check this replay for duplicates while the next one downloads
                        self.after(0, lambda fn=dest.name, fo=str(dest_dir):
                                   self._check_single_dupe(fn, fo))
                        bc_cache = CACHE_DIR / f"bc_{rid}.json"
                        if not bc_cache.exists():
                            try:
                                CACHE_DIR.mkdir(exist_ok=True)
                                with open(bc_cache, "w", encoding="utf-8") as _bcf:
                                    json.dump(replay, _bcf)
                            except Exception:
                                pass
                        success = True
                        break

                    if not success:
                        continue

                    total_dl += 1

                    # ── limit reached ─────────────────────────────────────────
                    if total_dl >= total_expected:
                        self.after(0, self._update_dl_log, total_dl, total_expected, "done", total_dl / max(time.time() - start_time, 1))
                        return

                    # ── adaptive rate ─────────────────────────────────────────
                    if time.time() - window_start >= WINDOW_SECS:
                        window_start = time.time()
                        errs         = error_window
                        error_window = 0

                        if ceiling is None:
                            if errs > 0:
                                ceiling = min(FLOOR, round(delay + errs * 0.01, 3))
                                delay   = ceiling
                                self.after(0, self._update_dl_status,
                                           f"[download] ceiling set at {1/ceiling:.2f}/s")
                            else:
                                delay = max(MIN_DELAY, round(delay - 0.1, 3))
                        elif delay > ceiling + 0.05:
                            delay = max(ceiling, round(delay - 0.1, 3))
                            self.after(0, self._update_dl_status,
                                       f"[download] recovering: {1/delay:.2f}/s")
                        else:
                            if errs == 0:
                                ceiling = max(MIN_DELAY, round(ceiling - 0.01, 3))
                            else:
                                ceiling = min(FLOOR, round(ceiling + errs * 0.01, 3))
                            delay = ceiling
                            self.after(0, self._update_dl_status,
                                       f"[download] {errs} errors — ceiling {1/ceiling:.2f}/s")

                    # ── ETA (rolling rate over last 20 downloads) ─────────────
                    now = time.time()
                    dl_times.append(now)
                    elapsed = now - start_time
                    if len(dl_times) >= 5 and elapsed >= 3:
                        window_secs = dl_times[-1] - dl_times[0]
                        rate = (len(dl_times) - 1) / window_secs if window_secs > 0 else 0
                        eta  = self._fmt_eta((total_expected - total_dl) / rate) if rate > 0 else "…"
                        self.after(0, self._update_dl_log, total_dl, total_expected, eta, rate)
                    else:
                        self.after(0, self._update_dl_log, total_dl, total_expected, "calculating…", 0)

                    if not _interruptible_sleep(delay): return

                next_url = data.get("next")
                if not next_url:
                    break
                url    = next_url
                params = {}

        label = "Done" if self._download_active else "Stopped"
        self.after(0, self._log,
                   f"[download] {label} — {total_dl} downloaded, {total_skip} skipped.")
        if self._download_active:
            # Orphan cleanup only — duplicate checks ran per-file during download
            self.after(0, self._cleanup_orphans)

    def _fetch_quota(self):
        api_key = self.config_data.get("api_key", "").strip()
        if not api_key:
            self.quota_label.configure(text="Upload quota: no API key set")
            self._log("↻ quota refresh — no API key set", "red")
            return
        self._log("↻ refreshing upload quota…")
        def worker():
            try:
                resp = requests.get(
                    "https://ballchasing.com/api/",
                    headers={"Authorization": api_key},
                    timeout=10)
                if resp.status_code != 200:
                    self.after(0, lambda: self.quota_label.configure(
                        text=f"Upload quota: error {resp.status_code}"))
                    self.after(0, self._log,
                               f"↻ quota refresh — error {resp.status_code}", "red")
                    return
                data  = resp.json()
                q     = data.get("quota") or {}
                d24   = q.get("uploads_in_24h") or {}
                d7    = q.get("uploads_in_7d")  or {}
                u24, m24 = d24.get("used", "?"), d24.get("max", "?")
                u7,  m7  = d7 .get("used", "?"), d7 .get("max", "?")
                tier  = data.get("type", "")
                name  = data.get("name", "")
                if name and not self._my_name:
                    self._my_name = name
                tier_str = f"  [{tier}]" if tier else ""
                text  = (f"Upload quota:{tier_str}  "
                         f"24h: {u24}/{m24}  —  7d: {u7}/{m7}")
                self.after(0, lambda t=text: self.quota_label.configure(text=t))
                self.after(0, self._log,
                           f"↻ quota: 24h {u24}/{m24}  —  7d {u7}/{m7}")
            except Exception:
                self.after(0, lambda: self.quota_label.configure(
                    text="Upload quota: could not reach ballchasing.com"))
                self.after(0, self._log,
                           "↻ quota refresh — could not reach ballchasing.com", "red")
        threading.Thread(target=worker, daemon=True).start()

    def _schedule_save_uploaded(self):
        if self._save_uploaded_id:
            self.after_cancel(self._save_uploaded_id)
        self._save_uploaded_id = self.after(3000, self._flush_save_uploaded)

    def _flush_save_uploaded(self):
        if self._save_uploaded_id:
            self.after_cancel(self._save_uploaded_id)
            self._save_uploaded_id = None
        save_uploaded(self.uploaded)

    # ── toast notification ────────────────────────────────────────────────────

    def _show_toast(self, message: str, duration_ms: int = 5000):
        """Show a silent, auto-dismissing notification at the bottom-right of the screen."""
        try:
            toast = tk.Toplevel(self)
            toast.overrideredirect(True)
            toast.attributes("-topmost", True)
            toast.attributes("-alpha", 0.92)

            bg = "#1e1e1e" if self.config_data.get("theme", "dark") == "dark" else "#f0f0f0"
            fg = "#ffffff"  if self.config_data.get("theme", "dark") == "dark" else "#111111"

            frame = tk.Frame(toast, bg=bg, padx=14, pady=10)
            frame.pack(fill="both", expand=True)
            tk.Label(frame, text=message, bg=bg, fg=fg,
                     font=("Segoe UI", 11), wraplength=280, justify="left").pack()

            toast.update()
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            w  = toast.winfo_reqwidth()
            h  = toast.winfo_reqheight()
            toast.geometry(f"{w}x{h}+{sw - w - 24}+{sh - h - 60}")

            def _fade(alpha=0.92):
                try:
                    if alpha <= 0.0:
                        toast.destroy()
                        return
                    toast.attributes("-alpha", alpha)
                    toast.after(40, _fade, round(alpha - 0.06, 2))
                except Exception:
                    pass

            toast.after(duration_ms, _fade)
        except Exception:
            pass

    # ── retry failed uploads ──────────────────────────────────────────────────

    def _retry_upload(self, path: Path, filename: str):
        """Retry an upload that previously failed. Called on a 5-minute timer."""
        if not self._upload_session_enabled:
            return
        if not path.exists():
            self._retry_queue.pop(filename, None)
            return
        attempt = self._retry_queue.get(filename, 0)
        self._log(f"↺ retrying upload ({attempt}/6): {filename}")

        def on_status(name, status):
            icons = {"uploading": "⟳", "uploaded": "✓", "duplicate": "=",
                     "skipped": "–", "failed": "✗"}
            tag = "green" if status in ("uploaded", "duplicate") else (
                  "red"   if status == "failed" else None)
            self.after(0, self._log,
                       f"{icons.get(status, '?')} {name}  [{status}]", tag)
            if status in ("uploaded", "duplicate"):
                def _mark_success(nm=name, fn=filename, st=status):
                    self._retry_queue.pop(fn, None)
                    idx = self._card_index.get(nm)
                    if idx is not None:
                        self._cards[idx]["uploaded"] = True
                    self.uploaded.add(nm)
                    self._schedule_save_uploaded()
                    self._schedule_redraw()
                self.after(0, _mark_success)
                if status == "uploaded":
                    self.after(2000, self._fetch_quota)
                    self.after(0, self._show_toast, f"✓ Uploaded: {name}")
            elif status == "skipped":
                self.after(0, self._retry_queue.pop, filename, None)
            elif status == "failed":
                def _handle_retry_fail(fn=filename, p=path):
                    next_attempt = self._retry_queue.get(fn, 0) + 1
                    if next_attempt <= 6:
                        self._retry_queue[fn] = next_attempt
                        self._log(f"  ↺ retry {next_attempt}/6 in 5 min: {fn}", "red")
                        self.after(300_000, lambda: self._retry_upload(p, fn))
                    else:
                        self._retry_queue.pop(fn, None)
                        self._log(f"  ✗ gave up after 6 retries: {fn}", "red")
                self.after(0, _handle_retry_fail)

        threading.Thread(
            target=lambda: upload(path, self.config_data, self.uploaded, on_status,
                                  on_bc_id=lambda bc_id, fn=filename: (
                                      save_upload_id(fn, bc_id),
                                      self.after(0, self._set_card_bc_id, fn, bc_id))),
            daemon=True).start()

    # ── right-click context menu on replay cards ──────────────────────────────

    def _on_canvas_right_click(self, event):
        cy = self.canvas.canvasy(event.y)
        cw = self.canvas.winfo_width()
        for card in self._active_cards:
            if card["y"] <= cy <= card["y"] + card["height"]:
                cols = card.get("grid_cols", 1)
                if cols > 1:
                    col   = card.get("grid_col", 0)
                    gap   = _dims()["card_pad"]
                    col_w = (cw - 2 * CARD_MARGIN_X - (cols - 1) * gap) // cols
                    if not _COMPACT:
                        col_w = min(col_w, card.get("_max_w", col_w))
                    x0 = CARD_MARGIN_X + col * (col_w + gap)
                    x1 = x0 + col_w
                    if not (x0 <= event.x <= x1):
                        continue
                bc_id = load_upload_ids().get(card["filename"], "")
                menu  = tk.Menu(self, tearoff=0)
                if bc_id:
                    menu.add_command(
                        label="Open on Ballchasing ↗",
                        command=lambda bid=bc_id: webbrowser.open(
                            f"https://ballchasing.com/replay/{bid}"))
                else:
                    menu.add_command(label="Open on Ballchasing ↗  (not uploaded)",
                                     state="disabled")
                try:
                    menu.tk_popup(event.x_root, event.y_root)
                finally:
                    menu.grab_release()
                return

    # ── first-run setup guide ─────────────────────────────────────────────────

    def _bg_build_index(self):
        """Background: fetch user's BC replay list and populate upload_ids.json.
        Read-only — no file uploads. Skips if all local replays already have bc_ids."""
        api_key = self.config_data.get("api_key", "").strip()
        folder  = self.config_data.get("demos_folder", "").strip()
        if not api_key or not folder:
            return
        def _run():
            # Count how many local replays are missing a bc_id
            existing = set(load_upload_ids().keys())
            try:
                missing = sum(1 for p in Path(folder).glob("*.replay")
                              if p.name not in existing)
            except Exception:
                missing = 1  # assume there's work to do if we can't check
            if missing == 0:
                return
            saved = build_upload_id_index(
                api_key, demos_folder=folder,
                log_fn=lambda m, t=None: self.after(0, self._log, m, t))
            if saved:
                # Refresh bc_id on all in-memory cards so borders update
                self.after(0, self._refresh_card_bc_ids)
        threading.Thread(target=_run, daemon=True).start()

    def _refresh_card_bc_ids(self):
        """After index scan, update bc_id on every in-memory card and redraw."""
        ids = load_upload_ids()
        changed = False
        for card in self._cards:
            new_id = ids.get(card["filename"], "")
            if card.get("bc_id") != new_id:
                card["bc_id"] = new_id
                changed = True
        if changed:
            self._schedule_redraw()

    def _set_card_bc_id(self, filename: str, bc_id: str):
        """Update a single card's bc_id in memory and redraw so the red border clears."""
        idx = self._card_index.get(filename)
        if idx is not None and idx < len(self._cards):
            if self._cards[idx].get("bc_id") != bc_id:
                self._cards[idx]["bc_id"] = bc_id
                self._schedule_redraw()

    def _check_first_run(self):
        api_key = self.config_data.get("api_key", "").strip()
        folder  = self.config_data.get("demos_folder", "").strip()
        if api_key and folder:
            return  # already configured

        dlg = tk.Toplevel(self)
        dlg.title("Welcome to Ballchasing Uploader")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.attributes("-topmost", True)
        dlg.minsize(420, 0)

        # ── header ────────────────────────────────────────────────────────────
        ctk.CTkLabel(dlg,
                     text="👋  Let's get you set up",
                     font=ctk.CTkFont(size=16, weight="bold"),
                     text_color="black").pack(padx=24, pady=(20, 4))
        ctk.CTkLabel(dlg,
                     text="Just two things to configure and you're good to go.",
                     font=ctk.CTkFont(size=12),
                     text_color="black").pack(padx=24, pady=(0, 16))

        # ── step 1: API key ───────────────────────────────────────────────────
        s1 = ctk.CTkFrame(dlg, fg_color="transparent")
        s1.pack(fill="x", padx=20, pady=(0, 10))
        ctk.CTkLabel(s1, text="Step 1 — Ballchasing API Key",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="black").pack(anchor="w")
        ctk.CTkLabel(s1,
                     text="Get your key from ballchasing.com → Profile → API key",
                     font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w")
        api_entry = ctk.CTkEntry(s1, placeholder_text="Paste your API key here",
                                 show="•", width=380)
        api_entry.pack(fill="x", pady=(6, 0))
        if api_key:
            api_entry.insert(0, api_key)
        def _open_bc_profile():
            webbrowser.open("https://ballchasing.com/upload")
        ctk.CTkButton(s1, text="Open Ballchasing ↗", width=160, height=26,
                      fg_color="transparent", border_width=1, text_color="black",
                      command=_open_bc_profile).pack(anchor="w", pady=(6, 0))

        # ── step 2: Demos folder ──────────────────────────────────────────────
        s2 = ctk.CTkFrame(dlg, fg_color="transparent")
        s2.pack(fill="x", padx=20, pady=(0, 10))
        ctk.CTkLabel(s2, text="Step 2 — Rocket League Demos Folder",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="black").pack(anchor="w")
        ctk.CTkLabel(s2, text="Where Rocket League saves your replay files (.replay)",
                     font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w")
        folder_row = ctk.CTkFrame(s2, fg_color="transparent")
        folder_row.pack(fill="x", pady=(6, 0))
        folder_entry = ctk.CTkEntry(folder_row,
                                    placeholder_text="Path to your Demos folder",
                                    width=280)
        folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        if folder:
            folder_entry.insert(0, folder)
        def _browse():
            p = filedialog.askdirectory(title="Select Demos Folder", parent=dlg)
            if p:
                folder_entry.delete(0, "end")
                folder_entry.insert(0, p)
        def _auto():
            dlg.grab_release()
            self._auto_detect_demos_folder()
            detected = self.config_data.get("demos_folder", "").strip()
            if detected:
                folder_entry.delete(0, "end")
                folder_entry.insert(0, detected)
            dlg.grab_set()
        ctk.CTkButton(folder_row, text="Browse", width=70, command=_browse).pack(side="left", padx=(0, 4))
        ctk.CTkButton(folder_row, text="Auto", width=60, command=_auto).pack(side="left")

        # ── confirm button ────────────────────────────────────────────────────
        def _confirm():
            key = api_entry.get().strip()
            fld = folder_entry.get().strip()
            if not key:
                messagebox.showwarning("Missing API Key",
                                       "Please enter your Ballchasing API key.",
                                       parent=dlg)
                return
            if not fld:
                messagebox.showwarning("Missing Folder",
                                       "Please set your Rocket League Demos folder.",
                                       parent=dlg)
                return
            self.config_data["api_key"]      = key
            self.config_data["demos_folder"] = fld
            save_config(self.config_data)
            # update the settings fields if they exist
            try:
                self.api_entry.delete(0, "end")
                self.api_entry.insert(0, key)
                self.folder_entry.delete(0, "end")
                self.folder_entry.insert(0, fld)
            except Exception:
                pass
            dlg.destroy()
            self._show_toast("✓ Setup complete — you're ready to go!")
            self.after(100, self._load_replays)
            # Background tasks ran before setup completed — re-run them now
            self.after(1500, self._startup_dedup)
            self.after(2000, self._bg_build_index)

        ctk.CTkButton(dlg, text="Done — let's go!",
                      command=_confirm).pack(pady=(8, 20))

    def _set_status(self, watching: bool):
        if watching:
            self.status_label.configure(text="Watching")
            self.toggle_btn.configure(text="Stop Watching", fg_color="#c0392b",
                                      hover_color="#962d22", text_color="white",
                                      state="normal")
            self._start_dot_pulse()
        else:
            if self._dot_anim_id:
                self.after_cancel(self._dot_anim_id)
                self._dot_anim_id = None
            self.status_dot.configure(text_color="#555")
            self.status_label.configure(text="Stopped")
            self.toggle_btn.configure(text="Start Watching",
                                      fg_color=("#3B8ED0","#1F6AA5"),
                                      hover_color=("#36719F","#144870"),
                                      text_color="white", state="normal")

    def _start_dot_pulse(self):
        """Pulse the status dot between bright and dim green while watching."""
        _pulse_colors = ["#2ecc71", "#27ae60", "#1a9450", "#27ae60"]
        _pulse_idx    = [0]
        def _tick():
            if not self._upload_session_enabled and hasattr(self, "status_dot"):
                # still watching but upload disabled — orange, no pulse
                self.status_dot.configure(text_color="#e67e22")
                return
            color = _pulse_colors[_pulse_idx[0] % len(_pulse_colors)]
            try:
                self.status_dot.configure(text_color=color)
            except Exception:
                return
            _pulse_idx[0] += 1
            self._dot_anim_id = self.after(600, _tick)
        _tick()

    # ── log ───────────────────────────────────────────────────────────────────

    def _log(self, message: str, tag: str = None):
        self.log_box.configure(state="normal")
        if tag:
            self.log_box._textbox.insert("end", message + "\n", tag)
        else:
            self.log_box.insert("end", message + "\n")
        line_count = int(self.log_box._textbox.index("end-1c").split(".")[0])
        if line_count > 600:
            self.log_box._textbox.delete("1.0", f"{line_count - 500}.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")


    # ── update check ─────────────────────────────────────────────────────────

    def _send_ping(self):
        def worker():
            try:
                import datetime
                requests.post(
                    "http://46.101.184.78:8765/ping",
                    json={
                        "version": VERSION,
                        "time":    datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                    },
                    timeout=5)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _check_self_update(self):
        """Check if the server has a newer main.pyw and auto-update + restart.
        Backup update path — works even when the launcher's version check fails."""
        def worker():
            try:
                r = requests.get(f"{APP_SERVER}/version", timeout=8)
                if r.status_code != 200:
                    return
                server_ver = r.json().get("version", "")
                if not server_ver or server_ver == VERSION:
                    return          # already current
                client_id = self.config_data.get("_client_id", "")
                token     = self.config_data.get("_auth_token", "")
                if not client_id or not token:
                    return
                r2 = requests.post(f"{APP_SERVER}/code",
                                   json={"client_id": client_id, "token": token},
                                   timeout=30)
                if r2.status_code != 200:
                    return
                script = Path(__file__)
                tmp    = script.with_suffix(".tmp")
                tmp.write_bytes(r2.content)
                tmp.replace(script)
                cfg = {**self.config_data, "_local_version": server_ver}
                self.after(0, lambda v=server_ver, c=cfg: self._apply_self_update(v, c))
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _apply_self_update(self, new_version: str, cfg: dict):
        self.config_data = cfg
        save_config(cfg)
        self._log(f"[update] Downloaded v{new_version} — restarting in 2 s…")
        self.after(2000, self._restart_for_update)

    def _restart_for_update(self):
        exe = BASE_DIR.parent / "BallchasingUploader.exe"
        if exe.exists():
            subprocess.Popen([str(exe)], cwd=str(BASE_DIR.parent))
        else:
            subprocess.Popen([sys.executable], cwd=str(BASE_DIR.parent))
        self.destroy()

    def _check_launcher_update(self):
        """Silently download and replace launcher.py if the server has a newer version."""
        def worker():
            try:
                server_ver = self.config_data.get("_server_launcher_version", "")
                local_ver  = self.config_data.get("_launcher_version", "")
                if not server_ver or server_ver == local_ver:
                    return  # already up to date or server didn't report a version

                client_id = self.config_data.get("_client_id", "")
                token     = self.config_data.get("_auth_token", "")
                if not token or not client_id:
                    return

                r = requests.post(f"{APP_SERVER}/launcher",
                                  json={"client_id": client_id, "token": token},
                                  timeout=15)
                if r.status_code != 200:
                    return

                # atomically replace launcher.py
                launcher_path = Path(__file__).parent.parent / "launcher.py"
                tmp = launcher_path.with_suffix(".tmp")
                tmp.write_bytes(r.content)
                os.replace(tmp, launcher_path)

                # record that we now have this version
                cfg = {**self.config_data, "_launcher_version": server_ver}
                self.after(0, lambda: self._apply_launcher_update(cfg))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _apply_launcher_update(self, cfg: dict):
        self.config_data = cfg
        save_config(cfg)

    def _check_exe_update(self):
        """Download a new BallchasingUploader.exe if the server has a newer launcher version.
        Can't replace the running exe directly — writes a batch updater instead."""
        def worker():
            try:
                exe = BASE_DIR.parent / "BallchasingUploader.exe"
                if not exe.exists():
                    return  # not running as exe
                # Fetch both app version and launcher version in one call
                rv = requests.get(f"{APP_SERVER}/version", timeout=8)
                if rv.status_code != 200:
                    return
                data           = rv.json()
                server_launcher = data.get("launcher_version", "")
                app_ver         = data.get("version", VERSION)
                local_launcher  = self._exe_launcher_version
                if not server_launcher or server_launcher == local_launcher:
                    return  # exe already current
                # Download the new exe
                r = requests.get(f"{APP_SERVER}/app", timeout=60)
                if r.status_code != 200:
                    return
                new_exe = exe.with_name("BallchasingUploader.new.exe")
                new_exe.write_bytes(r.content)
                self.after(0, lambda v=app_ver: self._prompt_exe_update(v))
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _prompt_exe_update(self, new_version: str):
        """Show the persistent update button below Start Watching."""
        self._log("[update] App update available — click the update button to restart.")
        self._update_btn.configure(text="⬆  App update available — Restart & Update")
        self._update_btn.pack(fill="x", padx=20, pady=(0, 6), after=self.toggle_btn)

    def _apply_exe_update(self):
        """Replace the exe and restart.  Launch the new exe directly from Python
        (avoids the DLL-load failure that occurs when cmd.exe's 'start' is used),
        then use a tiny batch to rename it back to BallchasingUploader.exe once
        both processes have settled."""
        exe     = BASE_DIR.parent / "BallchasingUploader.exe"
        new_exe = BASE_DIR.parent / "BallchasingUploader.new.exe"
        app_dir = str(BASE_DIR.parent)

        # Rename batch: just swaps the filenames after both processes are quiet
        bat = BASE_DIR.parent / "_rename.bat"
        bat.write_text(
            "@echo off\r\n"
            "ping -n 4 127.0.0.1 > nul\r\n"           # wait ~3 s for old exe to exit
            f'if exist "{exe}" del /f /q "{exe}"\r\n'
            f'move /y "{new_exe}" "{exe}"\r\n'
            "for /d %%i in (\"%LOCALAPPDATA%\\Temp\\_MEI*\") do rd /s /q \"%%i\" 2>nul\r\n"
            f'for /d %%i in ("{app_dir}\\_MEI*") do rd /s /q \"%%i\" 2>nul\r\n'
            'del "%~f0"\r\n',
            encoding="ascii"
        )

        # Release the single-instance socket so the new exe can bind it
        try:
            _lock_sock.close()
        except Exception:
            pass

        # Launch new exe directly from Python — this works; cmd 'start' does not
        subprocess.Popen(
            [str(new_exe)],
            cwd=app_dir,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        # Run the rename batch (no exe launch inside — just renames files)
        subprocess.Popen(
            ["cmd", "/c", str(bat)],
            creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=app_dir,
        )

        self.destroy()

    def _restart(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        script  = Path(__file__).resolve()
        subprocess.Popen([str(pythonw), str(script)],
                         creationflags=subprocess.CREATE_NO_WINDOW)
        self.destroy()

    # ── integrity check ───────────────────────────────────────────────────────

    def _check_integrity(self):
        issues = []
        if not RATTLETRAP.exists():
            issues.append("rattletrap.exe not found — run start.bat to download it.")
        folder = self.config_data.get("demos_folder", "").strip()
        if folder and not Path(folder).is_dir():
            issues.append(f"Demos folder does not exist: {folder}")
        for issue in issues:
            self._log(f"[setup] {issue}", "red")

    def _on_close(self):
        self._autosave_ids_entry()
        self._flush_save_uploaded()   # don't lose pending uploaded.json write
        self._stop_watching()
        self.destroy()


def _check_dependencies() -> list[str]:
    missing = []
    for pkg in ("watchdog", "requests", "customtkinter"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    return missing

if __name__ == "__main__":
    # tell Windows this is its own app (not pythonw.exe) so taskbar shows our icon
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "BallchasingAutoUploader.1")
    except Exception:
        pass

    _missing = _check_dependencies()
    if _missing:
        _root = tk.Tk()
        _root.withdraw()
        messagebox.showerror(
            "Missing Dependencies",
            f"Required packages not installed: {', '.join(_missing)}\n\n"
            f"Run start.bat to install them.")
        _root.destroy()
        sys.exit(1)
    App().mainloop()

