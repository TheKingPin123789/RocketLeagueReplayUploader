import sys
import json
import time
import queue
import shutil
import struct
import unicodedata
import hmac
import hashlib
import base64
import winreg
import calendar as _cal
import threading
import subprocess
import webbrowser
import importlib.util as _ilu
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk
from collections import deque
from datetime import datetime, date as _date
from pathlib import Path

# bundled dependencies (installed by Setup.bat into src\lib)
_LIB_DIR = Path(__file__).parent / "lib"
if _LIB_DIR.is_dir():
    sys.path.insert(0, str(_LIB_DIR))

import customtkinter as ctk
import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler



BASE_DIR      = Path(__file__).parent
CONFIG_FILE   = BASE_DIR / "config.json"
UPLOADED_FILE = BASE_DIR / "uploaded.json"
UPLOAD_URL    = "https://ballchasing.com/api/v2/upload"
CACHE_DIR     = BASE_DIR / "cache"
RATTLETRAP    = BASE_DIR / "rattletrap.exe"
BAKKESMOD_LOG = (Path.home() / "AppData" / "Roaming" / "bakkesmod" / "bakkesmod"
                 / "data" / "ReplayLogger" / "events.log")
VERSION          = "1.0.3"
GITHUB_REPO      = "TheKingPin123789/RocketLeagueReplayUploader"
APP_SERVER       = "http://46.101.184.78:8766"
EXPIRY_CHECK_MS  = 3_600_000  # re-check every hour

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

CACHE_VERSION  = 4      # bump to invalidate all header caches
DETAIL_VERSION = 7      # bump to re-parse detailed network stats
RENDER_BUFFER    = 800   # px above/below viewport to pre-render (hides load pop-in while scrolling)
CARD_MARGIN_X    = 10    # left/right margin
SCORE_W          = 52    # left score column width
COMPACT_MIN_W    = 300   # minimum compact card width — 3-per-row only when window fits 3×this

_COMPACT = False   # toggled by the compact button

def _dims():
    if _COMPACT:
        return dict(card_pad=3, name_h=20, tag_h=15, meta_h=14, meta2_h=14, player_h=15, footer_h=2,
                    score_font=13, name_font=11, meta_font=9, tag_font=9, player_font=10)
    return     dict(card_pad=6, name_h=34, tag_h=0,  meta_h=22, meta2_h=0,  player_h=22, footer_h=6,
                    score_font=20, name_font=12, meta_font=9, tag_font=9, player_font=10)

_font_cache: dict = {}

def _measure_font(family: str, size: int, weight: str = "normal") -> tkfont.Font:
    key = (family, size, weight)
    if key not in _font_cache:
        _font_cache[key] = tkfont.Font(family=family, size=size, weight=weight)
    return _font_cache[key]

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

C_BG          = "#141414"
C_CARD        = "#1e1e1e"
C_BORDER      = "#2d2d2d"
C_BORDER_FAIL = "#7a2020"
C_CARD_FAIL   = "#1e1414"
C_DIVIDER     = "#262626"
C_BLUE        = "#64b5f6"
C_ORANGE      = "#ffb74d"
C_SCORE       = "#ffffff"
C_DIM         = "#555555"
C_DATE        = "#777777"
C_CHECK       = "#6fcf97"

RANKED_PLAYLISTS = frozenset({10, 11, 12, 13, 27, 28, 29, 30, 34})
CASUAL_PLAYLISTS = frozenset({1, 2, 3, 4, 6, 7, 31})

def replay_type(info: dict) -> str:
    """Return 'Ranked', 'Casual', 'Private', 'Tournament', or 'Online'."""
    mt  = (info.get("match_type") or "").lower()
    pid = info.get("playlist_id") or 0
    rn  = (info.get("replay_name") or "").lower()
    if mt == "private":                 return "Private"
    if mt == "tournament":              return "Tournament"
    if pid in RANKED_PLAYLISTS:         return "Ranked"
    if pid in CASUAL_PLAYLISTS:         return "Casual"
    if "ranked" in rn:                  return "Ranked"
    if "casual" in rn:                  return "Casual"
    return "Online"


# ── persistence ───────────────────────────────────────────────────────────────

def load_config() -> dict:
    defaults = {"api_key": "", "demos_folder": "", "visibility": "unlisted",
                "auto_upload": False, "upload_on_detect": True, "launch_with_rl": False}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return {**defaults, **json.load(f)}
    return defaults

def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)

def _startup_launch_cmd() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    script  = Path(__file__).resolve()
    return f'"{pythonw}" "{script}"'

def get_startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_PATH) as k:
            winreg.QueryValueEx(k, _STARTUP_REG_NAME)
            return True
    except OSError:
        return False

def set_startup_enabled(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_PATH,
                        access=winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, _STARTUP_REG_NAME, 0, winreg.REG_SZ, _startup_launch_cmd())
        else:
            try:
                winreg.DeleteValue(k, _STARTUP_REG_NAME)
            except OSError:
                pass

def load_uploaded() -> set:
    if UPLOADED_FILE.exists():
        with open(UPLOADED_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_uploaded(uploaded: set) -> None:
    with open(UPLOADED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(uploaded), f, indent=2)

UPLOAD_IDS_FILE = BASE_DIR / "upload_ids.json"

def load_upload_ids() -> dict:
    if UPLOAD_IDS_FILE.exists():
        try:
            with open(UPLOAD_IDS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_upload_id(filename: str, bc_id: str) -> None:
    ids = load_upload_ids()
    ids[filename] = bc_id
    with open(UPLOAD_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(ids, f, indent=2)

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
_PLATFORM_COLOR: dict[str, str] = {
    "Steam": "#ffffff",
    "Epic":  "#888888",
    "PS":    "#6b8fd0",
    "XB":    "#6aab6a",
    "SW":    "#e06060",
}

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
    with open(CACHE_DIR / (name + ".json"), "w", encoding="utf-8") as f:
        json.dump({**info, "_v": CACHE_VERSION}, f)

DETAIL_LIMIT = 20

def load_detailed(name: str) -> dict | None:
    """Return cached data if it has current-version network-frame stats, else None."""
    c = load_cached(name)
    if c and c.get("_detailed") and c.get("_dv") == DETAIL_VERSION:
        return c
    return None

def save_detailed(name: str, info: dict) -> None:
    """Merge detailed info into the existing cache file (or create it)."""
    CACHE_DIR.mkdir(exist_ok=True)
    existing = load_cached(name) or {}
    merged   = {**existing, **info, "_detailed": True, "_dv": DETAIL_VERSION, "_v": CACHE_VERSION}
    with open(CACHE_DIR / (name + ".json"), "w", encoding="utf-8") as f:
        json.dump(merged, f)

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
        proc = subprocess.run(
            [str(RATTLETRAP)],
            input=data, capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
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
            raw_plat  = str(raw_plat)
            online_id = str(entry.get("OnlineID") or entry.get("UniqueId") or "")
            players.append({
                "name":         pname,
                "team":         int(t) if t is not None else -1,
                "platform":     platform_label(raw_plat),
                "raw_platform": raw_plat,
                "online_id":    online_id,
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
        return {
            "team0":       int(props.get("Team0Score") or 0),
            "team1":       int(props.get("Team1Score") or 0),
            "date":        str(props.get("Date")       or ""),
            "replay_name": str(props.get("ReplayName") or ""),
            "map":         str(props.get("MapName")    or ""),
            "match_type":  str(props.get("MatchType")  or ""),
            "players":     players,
            "duration":    float(dur) if dur is not None else None,
            "team_size":   int(props.get("TeamSize")   or 0),
            "playlist_id": _extract_playlist_id(props, replay),
            "rl_id":       str(props.get("Id")         or ""),
        }
    except Exception:
        return None


def parse_detailed(path: Path) -> dict | None:
    """Run rrrocket --network-parse (file arg) and return full stats."""
    if not RATTLETRAP.exists():
        return None
    try:
        proc = subprocess.run(
            [str(RATTLETRAP), "--network-parse", str(path)],
            capture_output=True, timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            return None
        raw    = json.loads(proc.stdout)
        replay = raw[0] if isinstance(raw, list) else raw
        props  = replay.get("properties", {})

        demos_inf  = {}
        demos_rcvd = {}
        for demo in (props.get("Demos") or []):
            if not isinstance(demo, dict):
                continue
            att = demo.get("Attacker") or demo.get("AttackerName") or ""
            vic = demo.get("Victim")   or demo.get("VictimName")   or ""
            if att: demos_inf [att] = demos_inf .get(att, 0) + 1
            if vic: demos_rcvd[vic] = demos_rcvd.get(vic, 0) + 1

        players = []
        for entry in (props.get("PlayerStats") or []):
            if not isinstance(entry, dict):
                continue
            pname = entry.get("Name", "")
            if not pname:
                continue
            t = entry.get("Team")
            raw_plat = (entry.get("Platform") or {})
            if isinstance(raw_plat, dict):
                raw_plat = raw_plat.get("value", "")
            raw_plat  = str(raw_plat)
            online_id = str(entry.get("OnlineID") or entry.get("UniqueId") or "")
            players.append({
                "name":         pname,
                "team":         int(t) if t is not None else -1,
                "score":        int(entry.get("Score",   0) or 0),
                "goals":        int(entry.get("Goals",   0) or 0),
                "assists":      int(entry.get("Assists", 0) or 0),
                "saves":        int(entry.get("Saves",   0) or 0),
                "shots":        int(entry.get("Shots",   0) or 0),
                "demos":        demos_inf.get(pname, 0),
                "demoed":       demos_rcvd.get(pname, 0),
                "platform":     platform_label(raw_plat),
                "raw_platform": raw_plat,
                "online_id":    online_id,
            })

        goals = []
        for g in (props.get("Goals") or []):
            if not isinstance(g, dict):
                continue
            t = g.get("PlayerTeam")
            goals.append({
                "player": str(g.get("PlayerName") or ""),
                "team":   int(t) if t is not None else -1,
            })

        dur = props.get("TotalSecondsPlayed")
        wt  = props.get("WinningTeam")

        result = {
            "team0":        int(props.get("Team0Score") or 0),
            "team1":        int(props.get("Team1Score") or 0),
            "date":         str(props.get("Date")       or ""),
            "replay_name":  str(props.get("ReplayName") or ""),
            "map":          str(props.get("MapName")    or ""),
            "match_type":   str(props.get("MatchType")  or ""),
            "players":      players,
            "goals":        goals,
            "duration":     float(dur) if dur is not None else None,
            "winning_team": int(wt) if wt is not None else -1,
            "forfeit":      bool(props.get("bForfeit") or False),
            "team_size":    int(props.get("TeamSize") or 0),
            "playlist_id":  _extract_playlist_id(props, replay),
        }

        # Augment with network-frame stats (boost, positioning, movement, ball)
        if _AR_MOD is not None:
            try:
                frames  = (replay.get("network_frames") or {}).get("frames", [])
                objects = replay.get("objects", [])
                _, pri_name, car_to_pri, boost_to_car = _AR_MOD.build_actor_maps(replay)
                net_demos   = _AR_MOD.extract_demos(frames, pri_name, car_to_pri)
                boost_stats = _AR_MOD.extract_boost_stats(
                    frames, car_to_pri, pri_name, boost_to_car,
                    duration=result.get("duration") or 0,
                    objects=objects)
                p_teams = {p["name"]: p["team"] for p in result["players"]}
                pos_stats   = _AR_MOD.extract_position_stats(
                    frames, objects, car_to_pri, pri_name, p_teams)

                move_stats = _AR_MOD.extract_movement_stats(
                    frames, objects, car_to_pri, pri_name)

                # Build demo counts from network frames (both names must resolve)
                net_inf:   dict[str, int] = {}
                net_rcvd:  dict[str, int] = {}
                for d in net_demos:
                    att, vic = d["attacker"], d["victim"]
                    if att and vic:
                        net_inf [att] = net_inf .get(att, 0) + 1
                        net_rcvd[vic] = net_rcvd.get(vic, 0) + 1

                for p in result["players"]:
                    pname = p["name"]
                    # Prefer props-based counts; fall back to network-frame counts
                    if p.get("demos", 0) == 0 and p.get("demoed", 0) == 0:
                        p["demos"]  = net_inf .get(pname, 0)
                        p["demoed"] = net_rcvd.get(pname, 0)
                    bs = dict(boost_stats.get(pname, {}))
                    ms = dict(move_stats.get(pname,  {}))
                    # UI reads these four from the movement dict
                    for k in ("big_pads", "small_pads", "boost_sonic_used", "overfill"):
                        if k in bs:
                            ms[k] = bs.pop(k)
                    p["boost"]       = bs
                    p["positioning"] = pos_stats.get(pname, {})
                    p["movement"]    = ms

                result["demos_timeline"] = net_demos
                result["ball_stats"]     = _AR_MOD.extract_ball_stats(frames, objects)
            except Exception:
                pass

        return result
    except Exception:
        return None


try:
    import boxcars_py as _boxcars
    _BOXCARS = True
except ImportError:
    _BOXCARS = False

_AR_MOD   = None
_ar_path  = BASE_DIR.parent / "analyze_replay.py"
_AR_URL   = "http://46.101.184.78/analyze_replay.py"
AR_VERSION = "1.2"   # must match __version__ in analyze_replay.py

_startup_logs: list[str] = []

def _ensure_analyze_replay():
    """Download or update analyze_replay.py if missing or on a different version."""
    if _ar_path.exists():
        try:
            for line in _ar_path.read_text(encoding="utf-8").splitlines()[:10]:
                if line.startswith("__version__"):
                    if AR_VERSION in line:
                        return   # already up to date
                    _startup_logs.append(f"⬇ updating analyze_replay.py to v{AR_VERSION}…")
                    break        # wrong version — fall through to download
        except Exception:
            pass
    else:
        _startup_logs.append("⬇ downloading analyze_replay.py…")
    try:
        import urllib.request
        data = urllib.request.urlopen(_AR_URL, timeout=10).read()
        _ar_path.write_bytes(data)
        _startup_logs.append(f"✓ analyze_replay.py v{AR_VERSION} ready")
    except Exception as e:
        _startup_logs.append(f"✗ analyze_replay.py download failed: {e}")

_ensure_analyze_replay()

try:
    if _ar_path.exists():
        _ar_spec = _ilu.spec_from_file_location("analyze_replay", _ar_path)
        _AR_MOD  = _ilu.module_from_spec(_ar_spec)
        _ar_spec.loader.exec_module(_AR_MOD)
except Exception:
    pass

# ── replay parser ─────────────────────────────────────────────────────────────

def _parse_with_boxcars(path: Path) -> dict | None:
    try:
        with open(path, 'rb') as f:
            data = f.read()
        replay = _boxcars.parse_replay(data)

        # navigate to header properties — handle both dict and object layouts
        if isinstance(replay, dict):
            raw_props = replay.get('header', {}).get('properties', {})
        else:
            header = getattr(replay, 'header', replay)
            raw_props = getattr(header, 'properties', {})

        # normalise to a plain dict
        if isinstance(raw_props, dict):
            props = raw_props
        elif hasattr(raw_props, 'items'):
            props = dict(raw_props.items())
        else:
            props = {k: v for k, v in raw_props}

        players = []
        for p in props.get('PlayerStats', []):
            pd = dict(p) if not isinstance(p, dict) else p
            players.append({
                'name':    pd.get('Name', '?'),
                'team':    int(pd.get('Team', 0)),
                'score':   pd.get('Score', 0),
                'goals':   pd.get('Goals', 0),
                'assists': pd.get('Assists', 0),
                'saves':   pd.get('Saves', 0),
                'shots':   pd.get('Shots', 0),
            })

        return {
            'team0':   props.get('Team0Score', 0),
            'team1':   props.get('Team1Score', 0),
            'date':    props.get('Date', ''),
            'players': players,
        }
    except Exception:
        return None

_replay_major: int = 868   # set once per parse; safe — parser runs one-at-a-time

def _rl_read_str(data: bytes, pos: int) -> tuple:
    if pos + 4 > len(data):
        raise ValueError(f"EOF at string length pos={pos}")
    length = struct.unpack_from("<i", data, pos)[0]
    pos += 4
    if length == 0:
        return "", pos
    if length < 0:
        byte_len = (-length) * 2
        if pos + byte_len > len(data):
            raise ValueError(f"EOF reading UTF-16 string pos={pos}")
        s = data[pos:pos + byte_len].decode("utf-16-le", errors="replace").rstrip("\x00")
        return s, pos + byte_len
    if length > 65536:
        raise ValueError(f"Implausible string length={length} pos={pos-4}")
    s = data[pos:pos + length - 1].decode("latin-1", errors="replace")
    return s, pos + length

def _rl_read_prop(data: bytes, pos: int) -> tuple:
    name, pos = _rl_read_str(data, pos)
    if not name or name == "None":
        return "None", None, pos
    type_name, pos = _rl_read_str(data, pos)
    if pos + 8 > len(data):
        raise ValueError("EOF at property size block")
    value_size  = struct.unpack_from("<I", data, pos)[0]
    array_index = struct.unpack_from("<I", data, pos + 4)[0]
    pos += 8
    end = pos + value_size

    if type_name == "IntProperty":
        return name, struct.unpack_from("<i", data, pos)[0] if end - pos >= 4 else 0, end

    elif type_name in ("StrProperty", "NameProperty"):
        try:   val, _ = _rl_read_str(data, pos)
        except Exception: val = None
        return name, val, end

    elif type_name == "QWordProperty":
        return name, struct.unpack_from("<Q", data, pos)[0] if end - pos >= 8 else 0, end

    elif type_name == "BoolProperty":
        if value_size >= 1:
            return name, bool(data[pos]) if pos < len(data) else False, end
        elif _replay_major >= 868:
            return name, bool(data[pos]) if pos < len(data) else bool(array_index & 1), pos + 1
        else:
            return name, bool(struct.unpack_from("<I", data, pos)[0]) if pos + 4 <= len(data) else bool(array_index & 1), pos + 4

    elif type_name == "FloatProperty":
        return name, struct.unpack_from("<f", data, pos)[0] if end - pos >= 4 else 0.0, end

    elif type_name == "ByteProperty":
        try:
            if value_size <= 1:
                val = data[pos] if pos < len(data) else None
            else:
                enum_type, p = _rl_read_str(data, pos)
                end = p + value_size
                val = data[p] if enum_type == "None" and p < len(data) else (lambda v, _: v)(*_rl_read_str(data, p))
        except Exception:
            val = None
        return name, val, end

    elif type_name == "StructProperty":
        try:    _, spos = _rl_read_str(data, pos)
        except Exception: spos = pos
        struct_end = spos + value_size
        _none_term = b'\x05\x00\x00\x00None\x00'
        inner, p = {}, spos
        while p < struct_end:
            try:
                n2, v2, p = _rl_read_prop(data, p)
            except Exception as exc:
                idx = data.find(_none_term, p, struct_end)
                if idx != -1 and idx + len(_none_term) <= struct_end - 16:
                    p = idx + len(_none_term)
                else:
                    break
                continue
            if n2 == "None":
                break
            inner[n2] = v2
        return name, inner, struct_end

    elif type_name == "ArrayProperty":
        count = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        _none_term = b'\x05\x00\x00\x00None\x00'
        items = []
        for _ in range(count):
            item, item_start = {}, pos
            while pos < end:
                try:
                    n, v, pos = _rl_read_prop(data, pos)
                except Exception:
                    idx = data.find(_none_term, pos, end)
                    if idx == -1:
                        idx = data.find(_none_term, item_start, end)
                    pos = idx + len(_none_term) if idx != -1 else end
                    break
                if n == "None":
                    break
                if isinstance(v, dict) and v:
                    item.update(v)
                else:
                    item[n] = v
            items.append(item)
        return name, items, end

    else:
        return name, None, end


def _parse_replay_data(data: bytes) -> dict:
    if len(data) < 16:
        raise ValueError(f"File too short ({len(data)} bytes)")
    global _replay_major
    _replay_major = struct.unpack_from("<I", data, 8)[0]
    minor = struct.unpack_from("<I", data, 12)[0]
    pos = 20 if (_replay_major >= 868 and minor >= 18) else 16
    _, pos = _rl_read_str(data, pos)   # skip game_type string

    result: dict = {"score0": None, "score1": None, "date": None, "players": [],
                    "team_size": 0, "match_type": "", "playlist_id": 0,
                    "replay_name": "", "map": "", "duration": None,
                    "_num_frames": 0, "_record_fps": 0.0}
    _goal_players: dict[str, int] = {}

    while pos < len(data):
        prop_start = pos
        try:
            name, value, pos = _rl_read_prop(data, prop_start)
        except Exception:
            try:
                _, p1 = _rl_read_str(data, prop_start)
                _, p2 = _rl_read_str(data, p1)
                if p2 + 8 <= len(data):
                    pos = p2 + 8 + struct.unpack_from("<I", data, p2)[0]
                    continue
            except Exception:
                pass
            break
        if name == "None":
            break
        if   name == "Team0Score"  and value is not None: result["score0"]      = int(value)
        elif name == "Team1Score"  and value is not None: result["score1"]      = int(value)
        elif name == "Date"        and isinstance(value, str): result["date"]   = value
        elif name == "TeamSize"    and value is not None: result["team_size"]   = int(value)
        elif name == "MatchType"   and isinstance(value, str): result["match_type"]  = value
        elif name == "PlaylistId"  and value is not None: result["playlist_id"] = int(value)
        elif name == "ReplayName"         and isinstance(value, str): result["replay_name"] = value
        elif name == "MapName"            and isinstance(value, str): result["map"]      = value
        elif name == "TotalSecondsPlayed" and value is not None:      result["duration"] = float(value)
        elif name == "Goals"      and isinstance(value, list):
            for g in value:
                if isinstance(g, dict) and g.get("PlayerName") and g.get("PlayerTeam", -1) != -1:
                    _goal_players.setdefault(g["PlayerName"], int(g["PlayerTeam"]))
        elif name == "PlayerStats" and isinstance(value, list):
            for entry in value:
                if not isinstance(entry, dict): continue
                pname = entry.get("Name", "")
                if not pname: continue
                raw_plat = entry.get("Platform") or ""
                if isinstance(raw_plat, dict):
                    raw_plat = raw_plat.get("value", "")
                result["players"].append({
                    "name":     pname,
                    "team":     int(entry.get("Team", -1)),
                    "platform": platform_label(str(raw_plat)),
                })

    def _nname(n: str) -> str:
        return unicodedata.normalize("NFC", n).strip().lower()

    goal_norm = {_nname(k): (k, v) for k, v in _goal_players.items()}
    for p in result["players"]:
        if p["team"] == -1:
            match = goal_norm.get(_nname(p["name"]))
            if match:
                p["team"] = match[1]
    known_norm = {_nname(p["name"]) for p in result["players"]}
    for nk, (gname, gteam) in goal_norm.items():
        if nk not in known_norm:
            result["players"].append({"name": gname, "team": gteam})

    nf  = result.pop("_num_frames", 0)
    fps = result.pop("_record_fps", 0.0)
    if nf and fps:
        result["duration"] = nf / fps

    return result


def parse_replay_header(path: Path) -> dict | None:
    try:
        with open(path, "rb") as f:
            data = f.read(131072)   # 128 KB covers any RL replay header
        raw = _parse_replay_data(data)
        return {
            "team0":       raw["score0"] or 0,
            "team1":       raw["score1"] or 0,
            "date":        raw["date"] or "",
            "players":     [p for p in raw["players"] if p.get("name")],
            "team_size":   raw.get("team_size", 0),
            "match_type":  raw.get("match_type", ""),
            "playlist_id": raw.get("playlist_id", 0),
            "replay_name": raw.get("replay_name", ""),
            "map":         raw.get("map", ""),
            "duration":    raw.get("duration"),
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
                if resp.status_code == 201 and on_bc_id:
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
    if _COMPACT:
        blue   = len([p for p in info["players"] if p["team"] == 0])
        orange = len([p for p in info["players"] if p["team"] == 1])
        return d["name_h"] + d["tag_h"] + d["meta_h"] + d["meta2_h"] + (max(blue, 1) + max(orange, 1)) * d["player_h"] + d["footer_h"]
    rows = max(len([p for p in info["players"] if p["team"] == 0]),
               len([p for p in info["players"] if p["team"] == 1]), 1)
    return d["name_h"] + d["meta_h"] + rows * d["player_h"] + d["footer_h"]

def draw_card(canvas: tk.Canvas, y: int, w: int, entry: dict) -> None:
    d        = _dims()
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
        x0 = CARD_MARGIN_X
        x1 = w - CARD_MARGIN_X

    h  = entry["height"]
    cx = x0 + 8

    # ── card background + border ──────────────────────────────────────────────
    if entry.get("failed"):
        bg, bdr, bdr_w = C_CARD_FAIL, C_BORDER_FAIL, 2
    else:
        bg, bdr, bdr_w = C_CARD, C_BORDER, 1
    canvas.create_rectangle(x0, y, x1, y+h, fill=bg, outline=bdr, width=bdr_w)

    if not _COMPACT:
        # ── score column (normal mode only) ───────────────────────────────────
        sx = x0 + SCORE_W
        cx = sx + 8
        canvas.create_rectangle(x0, y, sx, y+h, fill="#181818", outline="")
        canvas.create_line(sx, y, sx, y+h, fill=C_DIVIDER)
        if info:
            half = y + h // 2
            canvas.create_rectangle(x0, y,    x0+4, half,  fill=C_BLUE,   outline="")
            canvas.create_rectangle(x0, half, x0+4, y+h,   fill=C_ORANGE, outline="")
            canvas.create_line(x0+4, half, sx, half, fill=C_DIVIDER)
            score_cx = x0 + 4 + (SCORE_W - 4) // 2
            canvas.create_text(score_cx, y + h // 4,     text=str(info["team0"]),
                               anchor="center", fill=C_BLUE,
                               font=("Segoe UI", d["score_font"], "bold"))
            canvas.create_text(score_cx, y + 3 * h // 4, text=str(info["team1"]),
                               anchor="center", fill=C_ORANGE,
                               font=("Segoe UI", d["score_font"], "bold"))
        else:
            canvas.create_text(x0 + SCORE_W // 2, y + h // 2, text="?",
                               anchor="center", fill=C_DIM, font=("Segoe UI", 14))
    else:
        sx = x0  # no score column in compact mode

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

    if TAG_H == 0:
        # normal mode: truncate name to leave room for tags on the right
        tag_px   = (_measure_font("Segoe UI", d["tag_font"]).measure(tags) + 14) if tags else 0
        name_max = x1 - cx - tag_px - 8
        rname    = _fit_text(rname, "Segoe UI", d["name_font"], "bold", name_max)

    canvas.create_text(cx, ny, text=rname, anchor="w",
                       fill="#e8e8e8", font=("Segoe UI", d["name_font"], "bold"))

    if TAG_H == 0:
        # normal mode: tags on right side of name row
        if tags:
            canvas.create_text(x1-6, ny, text=tags, anchor="e",
                               fill=C_DATE, font=("Segoe UI", d["tag_font"]))

    if entry.get("parsing"):
        canvas.create_text(cx, y + NAME_H + TAG_H + META_H // 2,
                           text="⟳  parsing…", anchor="w",
                           fill=C_DATE, font=("Segoe UI", d["meta_font"], "italic"))
        return
    if not info:
        return

    if TAG_H > 0:
        # compact mode: tag row below name row
        canvas.create_line(x0, y+NAME_H, x1, y+NAME_H, fill=C_DIVIDER)
        ty = y + NAME_H + TAG_H // 2
        if tags:
            tag_str = _fit_text(tags, "Segoe UI", d["tag_font"], "normal", x1 - cx - 6)
            canvas.create_text(cx, ty, text=tag_str, anchor="w",
                               fill=C_DATE, font=("Segoe UI", d["tag_font"]))

    # ── meta row(s) ──────────────────────────────────────────────────────────
    canvas.create_line(x0, y+NAME_H+TAG_H, x1, y+NAME_H+TAG_H, fill=C_DIVIDER)

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
                           fill=C_DATE, font=("Segoe UI", d["meta_font"]))

        canvas.create_line(x0, y+NAME_H+TAG_H+META_H, x1, y+NAME_H+TAG_H+META_H, fill=C_DIVIDER)
        my2 = y + NAME_H + TAG_H + META_H + META2_H // 2
        date_line = _fit_text(f"{date_str}  {time_utc}".strip(),
                              "Segoe UI", d["meta_font"], "normal", x1 - cx - 6)
        canvas.create_text(cx, my2, text=date_line, anchor="w",
                           fill=C_DATE, font=("Segoe UI", d["meta_font"]))
    else:
        # normal: single meta line
        my = y + NAME_H + META_H // 2
        meta_parts = [p for p in [map_name, dur_str, f"{date_str} {time_utc}".strip()] if p]
        meta_str   = _fit_text("  |  ".join(meta_parts), "Segoe UI", d["meta_font"], "normal", x1 - cx - 6)
        canvas.create_text(cx, my, text=meta_str, anchor="w",
                           fill=C_DATE, font=("Segoe UI", d["meta_font"]))

    # ── players ───────────────────────────────────────────────────────────────
    py_start = y + NAME_H + TAG_H + META_H + META2_H
    canvas.create_line(x0, py_start, x1, py_start, fill=C_DIVIDER)
    if not _COMPACT:
        cmid = (x0 + x1) // 2
        canvas.create_line(cmid, py_start, cmid, y+h, fill=C_DIVIDER)

    blue   = [p for p in info["players"] if p["team"] == 0]
    orange = [p for p in info["players"] if p["team"] == 1]

    if _COMPACT:
        SCORE_W_C = 22
        PLAT_W_C  = 40
        bar_w     = 3
        text_x    = x0 + bar_w + 4 + SCORE_W_C + PLAT_W_C

        actual_blue   = max(len(blue), 1)
        actual_orange = max(len(orange), 1)

        blue_start   = py_start
        blue_end     = blue_start + actual_blue * PLAYER_H
        orange_start = blue_end
        orange_end   = orange_start + actual_orange * PLAYER_H

        canvas.create_rectangle(x0, blue_start,   x0+bar_w, blue_end,   fill=C_BLUE,   outline="")
        canvas.create_rectangle(x0, orange_start, x0+bar_w, orange_end, fill=C_ORANGE, outline="")
        canvas.create_line(x0+bar_w, blue_end, x1, blue_end, fill=C_DIVIDER)

        sx_score = x0 + bar_w + 4
        sx_plat  = sx_score + SCORE_W_C

        canvas.create_text(sx_score, (blue_start   + blue_end)   // 2,
                           text=str(info["team0"]), anchor="w",
                           fill=C_BLUE,   font=("Segoe UI", d["score_font"], "bold"))
        canvas.create_text(sx_score, (orange_start + orange_end) // 2,
                           text=str(info["team1"]), anchor="w",
                           fill=C_ORANGE, font=("Segoe UI", d["score_font"], "bold"))

        pname_max   = x1 - text_x - 6
        blue_space  = (blue_end   - blue_start)   / max(len(blue),   1)
        orange_space = (orange_end - orange_start) / max(len(orange), 1)

        for i, p in enumerate(blue):
            ry   = int(blue_start + (i + 0.5) * blue_space)
            plat = p.get("platform", "")
            if plat:
                canvas.create_text(sx_plat, ry, text=plat, anchor="w",
                                   fill=_PLATFORM_COLOR.get(plat, C_DATE),
                                   font=("Segoe UI", d["meta_font"], "bold"))
            canvas.create_text(text_x, ry,
                               text=_fit_text(p["name"], "Segoe UI", d["player_font"], "normal", pname_max),
                               anchor="w", fill=C_BLUE, font=("Segoe UI", d["player_font"]))

        for i, p in enumerate(orange):
            ry   = int(orange_start + (i + 0.5) * orange_space)
            plat = p.get("platform", "")
            if plat:
                canvas.create_text(sx_plat, ry, text=plat, anchor="w",
                                   fill=_PLATFORM_COLOR.get(plat, C_DATE),
                                   font=("Segoe UI", d["meta_font"], "bold"))
            canvas.create_text(text_x, ry,
                               text=_fit_text(p["name"], "Segoe UI", d["player_font"], "normal", pname_max),
                               anchor="w", fill=C_ORANGE, font=("Segoe UI", d["player_font"]))
    else:
        # normal: platform badge + name, left/right of center
        plat_off  = 44
        cmid      = (x0 + x1) // 2
        blue_max  = cmid - cx - plat_off - 6
        blue_max0 = cmid - cx - 6          # no plat
        oran_max  = cmid - (x0 + SCORE_W + 8) - plat_off - 6
        oran_max0 = cmid - (x0 + SCORE_W + 8) - 6

        for i, p in enumerate(blue):
            ry = py_start + i * PLAYER_H + PLAYER_H // 2
            plat = p.get("platform", "")
            if plat:
                canvas.create_text(cx, ry, text=plat, anchor="w",
                                   fill=_PLATFORM_COLOR.get(plat, C_DATE),
                                   font=("Segoe UI", d["meta_font"], "bold"))
                canvas.create_text(cx + plat_off, ry,
                                   text=_fit_text(p["name"], "Segoe UI", d["player_font"], "normal", blue_max),
                                   anchor="w", fill=C_BLUE, font=("Segoe UI", d["player_font"]))
            else:
                canvas.create_text(cx, ry,
                                   text=_fit_text(p["name"], "Segoe UI", d["player_font"], "normal", blue_max0),
                                   anchor="w", fill=C_BLUE, font=("Segoe UI", d["player_font"]))

        for i, p in enumerate(orange):
            ry = py_start + i * PLAYER_H + PLAYER_H // 2
            plat = p.get("platform", "")
            if plat:
                canvas.create_text(x1-10, ry, text=plat, anchor="e",
                                   fill=_PLATFORM_COLOR.get(plat, C_DATE),
                                   font=("Segoe UI", d["meta_font"], "bold"))
                canvas.create_text(x1-plat_off-10, ry,
                                   text=_fit_text(p["name"], "Segoe UI", d["player_font"], "normal", oran_max),
                                   anchor="e", fill=C_ORANGE, font=("Segoe UI", d["player_font"]))
            else:
                canvas.create_text(x1-10, ry,
                                   text=_fit_text(p["name"], "Segoe UI", d["player_font"], "normal", oran_max0),
                                   anchor="e", fill=C_ORANGE, font=("Segoe UI", d["player_font"]))


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
            tk.Label(parent, text=h, bg=self._BG, fg=C_DIM,
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
            tk.Label(parent, text=h, bg=self._BG, fg=C_DIM,
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


# ── App ───────────────────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Ballchasing Auto Uploader")
        self.geometry("850x820")
        self.resizable(True, True)
        self.minsize(850, 500)

        self.config_data = load_config()
        self.uploaded    = load_uploaded()
        self.observer: Observer | None = None
        self._current_page = "main"

        self._cards: list[dict] = []
        self._active_cards: list[dict] = []
        self._card_index: dict[str, int] = {}
        self._total_h = _dims()["card_pad"]
        self._normal_card_w: int = 300
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
        self._filter_id   = None
        self._my_name     = ""

        self._build_header()
        self._build_main_page()
        self._build_settings_page()
        self._build_replays_page()
        self._build_detail_page()
        self._build_stats_page()
        self._show_main()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._loading = False
        self._rebuild_id            = None
        self._save_uploaded_id      = None
        self._watch_err_id          = None
        self._upload_session_enabled = True
        self._download_active        = False
        self._tier                   = "free_tester"
        self._revoked                = False
        self._dl_progress_line       = None
        self._dl_status_line         = None
        self._dl_error_line          = None
        for msg in _startup_logs:
            self._log(msg)
        self.after(200, self._fetch_quota)
        self.after(400, self._check_expiry)
        self.after(1000, self._bg_cache_replays)
        self._check_integrity()
        if self.config_data.get("launch_with_rl", False):
            self._rl_poll_thread_running = True
            threading.Thread(target=self._rl_poll_loop, daemon=True).start()

    # ── header ────────────────────────────────────────────────────────────────

    def _build_header(self):
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=20, pady=(16, 0))
        self.nav_btn = ctk.CTkButton(hdr, text="⚙", width=36, height=36,
                                     font=ctk.CTkFont(size=18),
                                     fg_color="transparent", hover_color="#2a2d2e",
                                     command=self._on_nav)
        self.nav_btn.pack(side="right")
        self.status_dot = ctk.CTkLabel(hdr, text="●", font=ctk.CTkFont(size=20),
                                       text_color="#555")
        self.status_dot.pack(side="right", padx=(0, 2))
        self.status_label = ctk.CTkLabel(hdr, text="Stopped", font=ctk.CTkFont(size=13))
        self.status_label.pack(side="right", padx=(0, 6))

    # ── main page ─────────────────────────────────────────────────────────────

    def _build_main_page(self):
        self.main_page = ctk.CTkFrame(self, fg_color="transparent")
        self.toggle_btn = ctk.CTkButton(self.main_page, text="Start Watching", height=44,
                                        font=ctk.CTkFont(size=15, weight="bold"),
                                        command=self._toggle_watching)
        self.toggle_btn.pack(fill="x", padx=20, pady=(18, 2))
        self.watch_error_label = ctk.CTkLabel(
            self.main_page, text="", text_color="#e06060",
            font=ctk.CTkFont(size=12), anchor="w")
        self.watch_error_label.pack(fill="x", padx=24, pady=(0, 6))
        ctk.CTkButton(self.main_page, text="View Replays", height=34,
                      fg_color="transparent", border_width=1,
                      border_color=("#3B8ED0", "#1F6AA5"),
                      command=self._show_replays).pack(fill="x", padx=20, pady=(0, 8))

        quota_row = ctk.CTkFrame(self.main_page, fg_color="transparent")
        quota_row.pack(fill="x", padx=20, pady=(0, 6))
        ctk.CTkButton(quota_row, text="↻", width=28, height=22,
                      fg_color="transparent", border_width=1, border_color=C_BORDER,
                      font=ctk.CTkFont(size=13),
                      command=self._fetch_quota).pack(side="left", padx=(0, 6))
        self.quota_label = ctk.CTkLabel(quota_row, text="",
                                        font=ctk.CTkFont(family="Consolas", size=12),
                                        text_color=C_DATE, anchor="w")
        self.quota_label.pack(side="left", fill="x", expand=True)


        self.log_box = ctk.CTkTextbox(self.main_page, state="disabled",
                                      font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        self.log_box._textbox.tag_config("green", foreground="#4aaa88")
        self.log_box._textbox.tag_config("red",   foreground="#e06060")

    # ── replays page ──────────────────────────────────────────────────────────

    def _build_replays_page(self):
        self.replays_page = ctk.CTkFrame(self, fg_color="transparent")

        bar = ctk.CTkFrame(self.replays_page, fg_color="transparent")
        bar.pack(fill="x", padx=20, pady=(14, 4))
        self.replays_title = ctk.CTkLabel(bar, text="Replays",
                                          font=ctk.CTkFont(size=16, weight="bold"))
        self.replays_title.pack(side="left")
        btn_col = ctk.CTkFrame(bar, fg_color="transparent")
        btn_col.pack(side="right")
        ctk.CTkButton(btn_col, text="↻  Refresh", width=90, height=28,
                      fg_color="transparent", border_width=1,
                      border_color=("#3B8ED0", "#1F6AA5"),
                      command=self._load_replays).pack()

        self.compact_btn = ctk.CTkButton(bar, text="Compact", width=75, height=28,
                      fg_color="transparent", border_width=1, border_color=C_BORDER,
                      font=ctk.CTkFont(size=12),
                      command=self._toggle_compact)
        self.compact_btn.pack(side="right", padx=(0, 8))
        ctk.CTkButton(bar, text="Player Stats", width=90, height=28,
                      fg_color="transparent", border_width=1, border_color=C_BORDER,
                      font=ctk.CTkFont(size=12),
                      command=self._show_stats).pack(side="right", padx=(0, 8))

        # ── filter bar ────────────────────────────────────────────────────────
        fbar = ctk.CTkFrame(self.replays_page, fg_color="transparent")
        fbar.pack(fill="x", padx=20, pady=(0, 6))

        _lkw = dict(text_color=C_DIM, font=ctk.CTkFont(size=12))
        _seg_kw = dict(height=26, font=ctk.CTkFont(size=12),
                       fg_color="#2a2a2a", selected_color="#3B8ED0",
                       selected_hover_color="#3480c0",
                       unselected_color="#2a2a2a", unselected_hover_color="#333333")

        # Row 1: type + mode + search
        row1 = ctk.CTkFrame(fbar, fg_color="transparent")
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

        # Row 2: date range picker button + clear
        row2 = ctk.CTkFrame(fbar, fg_color="transparent")
        row2.pack(fill="x")

        ctk.CTkLabel(row2, text="Date:", **_lkw).pack(side="left", padx=(0, 4))
        self._date_btn = ctk.CTkButton(row2, text="Any date", width=200, height=26,
                                       fg_color="transparent", border_width=1,
                                       border_color=C_BORDER, font=ctk.CTkFont(size=12),
                                       anchor="w",
                                       command=self._open_date_picker)
        self._date_btn.pack(side="left", padx=(0, 8))

        ctk.CTkButton(row2, text="✕ Clear", width=70, height=26,
                      fg_color="transparent", border_width=1, border_color=C_BORDER,
                      font=ctk.CTkFont(size=12),
                      command=self._clear_filters).pack(side="left")

        self._filt_from.trace_add("write", lambda *_: self._update_date_btn())
        self._filt_to.trace_add("write",   lambda *_: self._update_date_btn())

        # scrollable canvas
        wrap = tk.Frame(self.replays_page, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        style = ttk.Style()
        style.theme_use("clam")

        vsb = ttk.Scrollbar(wrap, orient="vertical")
        vsb.pack(side="right", fill="y")

        self.canvas = tk.Canvas(wrap, bg=C_BG, highlightthickness=0,
                                yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.config(command=self._canvas_yview)

        self.canvas.bind("<Configure>",  lambda e: self._on_canvas_resize())
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>",   lambda e: (self.canvas.yview_scroll(-1,"units"), self._schedule_redraw()))
        self.canvas.bind("<Button-5>",   lambda e: (self.canvas.yview_scroll( 1,"units"), self._schedule_redraw()))
        self.canvas.bind("<Button-1>",   self._on_canvas_click)

    def _canvas_yview(self, *args):
        self.canvas.yview(*args)
        self._schedule_redraw()

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._schedule_redraw()

    def _schedule_redraw(self):
        if self._redraw_id is not None:
            self.after_cancel(self._redraw_id)
        self._redraw_id = self.after(40, self._redraw)

    def _redraw(self):
        self._redraw_id = None
        self.canvas.delete("all")
        if not self._active_cards:
            return
        cw       = self.canvas.winfo_width()
        eff_w    = cw if _COMPACT else min(cw, CARD_MARGIN_X * 2 + self._normal_card_w)
        view_top = self.canvas.canvasy(0) - RENDER_BUFFER
        view_bot = self.canvas.canvasy(self.canvas.winfo_height()) + RENDER_BUFFER
        for card in self._active_cards:
            y0, y1 = card["y"], card["y"] + card["height"]
            if y1 >= view_top and y0 <= view_bot:
                draw_card(self.canvas, y0, eff_w, card)

    def _apply_filters(self, sort=True):
        ftype  = self._filt_type.get()
        fmode  = self._filt_mode.get()
        ffrom  = self._filt_from.get().strip()
        fto    = self._filt_to.get().strip()
        fsrch  = self._filt_search.get().strip().lower()

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
            active.append(card)

        if sort:
            def sort_key(c):
                d = (c.get("info") or {}).get("date") or ""
                return (1, d) if d else (0, c.get("mtime", 0))
            active = sorted(active, key=sort_key, reverse=True)

        self._active_cards = active

        if not _COMPACT and active:
            d         = _dims()
            nfont     = _measure_font("Segoe UI", d["name_font"], "bold")
            tfont     = _measure_font("Segoe UI", d["tag_font"])
            max_w = 250
            for c in active:
                info     = c.get("info") or {}
                rname    = (info.get("replay_name") or "") or Path(c["filename"]).stem
                ts       = info.get("team_size", 0)
                type_tag = c.get("type") or replay_type(info)
                mode_str = {1: "Duel", 2: "Doubles", 3: "Standard", 4: "Chaos"}.get(ts, f"{ts}v{ts}" if ts else "")
                tags     = "  ·  ".join(t for t in [type_tag, mode_str] if t)
                name_px  = nfont.measure(rname)
                tag_px   = (tfont.measure(tags) + 14) if tags else 0
                cw_nat   = SCORE_W + 16 + int((name_px + tag_px) * 1.12)
                max_w    = max(max_w, cw_nat)
            self._normal_card_w = max_w

        pad = _dims()["card_pad"]
        y   = pad
        if _COMPACT:
            d     = _dims()
            cw    = self.canvas.winfo_width() or 600
            avail = cw - 2 * CARD_MARGIN_X

            # minimum width needed to show the longest name without truncation
            name_font = _measure_font("Segoe UI", d["name_font"], "bold")
            max_name_px = max(
                (name_font.measure((c.get("info") or {}).get("replay_name") or Path(c["filename"]).stem)
                 for c in active),
                default=0
            )
            min_w = max(COMPACT_MIN_W, max_name_px + 8 + 6)  # cx=x0+8, right pad=6

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
            for card in self._active_cards:
                card["y"] = y
                y += card["height"] + pad
        self._total_h = y
        self.canvas.configure(scrollregion=(0, 0, 0, self._total_h))
        self._schedule_redraw()

    def _finish_loading(self):
        self._loading = False
        self._apply_filters()

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
        self._apply_filters()

    def _rebuild_positions(self):
        if self._rebuild_id:
            self.after_cancel(self._rebuild_id)
        self._rebuild_id = self.after(250, self._do_rebuild)

    def _do_rebuild(self):
        self._rebuild_id = None
        self._apply_filters()

    def _extend_positions(self, new_cards: list):
        self._apply_filters(sort=not self._loading)

    def _on_canvas_resize(self):
        if _COMPACT:
            self._apply_filters(sort=False)
        else:
            self._schedule_redraw()

    def _toggle_compact(self):
        global _COMPACT
        _COMPACT = not _COMPACT
        self.compact_btn.configure(
            text="Normal" if _COMPACT else "Compact",
            fg_color="#2a2a2a" if _COMPACT else "transparent")
        for card in self._cards:
            card["height"] = card_height(card.get("info"))
        self._apply_filters()

    # ── load + parse ──────────────────────────────────────────────────────────

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
                anchor="center", fill=C_DIM,
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

            to_parse: list[Path] = []
            batch: list[dict] = []
            for i, (path, mtime) in enumerate(paths):
                info = load_cached(path.name)
                needs_parse = info is None or "team_size" not in info or info.get("_v") != CACHE_VERSION
                if needs_parse:
                    to_parse.append(path)
                batch.append({
                    "filename": path.name,
                    "path":     path,
                    "mtime":    mtime,
                    "info":     info,
                    "type":     replay_type(info) if info else "",
                    "uploaded": path.name in self.uploaded,
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
        else:
            self._apply_filters(sort=not self._loading)
        self._schedule_redraw()

    def _start_parse_worker(self, paths: list, gen: int):
        self._parse_queue = list(paths)
        self._parse_next(gen)

    def _parse_next(self, gen: int):
        """Parse one replay with rrrocket; saves only card fields. Next starts when done."""
        if not self._parse_queue or self._parse_gen != gen:
            return
        path = self._parse_queue.pop(0)

        def do_work():
            info = parse_replay_header(path)
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
                old_h = card["height"]
                card["info"] = info
                card["type"] = replay_type(info)
                card["height"] = card_height(info)
                if card["height"] != old_h:
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
        if not RATTLETRAP.exists():
            return
        folder = self.config_data.get("demos_folder", "").strip()
        if not folder or not Path(folder).is_dir():
            return

        def worker():
            all_replays = sorted(Path(folder).glob("*.replay"),
                                 key=lambda f: f.stat().st_mtime, reverse=True)
            # Phase 1: basic card cache for all uncached replays
            to_parse = [p for p in all_replays if not load_cached(p.name)]
            if to_parse:
                self.after(0, self._log,
                           f"[cache] Parsing {len(to_parse)} uncached replay(s)…")
            for path in to_parse:
                info = parse_card_data(path)
                if info:
                    save_cache(path.name, info)
            # Phase 2: full network-parse for the 20 most recent
            to_detail = [p for p in all_replays[:DETAIL_LIMIT]
                         if not load_detailed(p.name)]
            if to_detail:
                self.after(0, self._log,
                           f"[cache] Network-parsing {len(to_detail)} replay(s) for detailed stats…")
            for path in to_detail:
                info = parse_detailed(path)
                if info:
                    save_detailed(path.name, info)
                    self.after(0, self._log, f"[cache] parsed  {path.name}")
            if to_detail:
                self.after(0, self._log, "[cache] Detailed stats ready.")
            self._enforce_detail_limit()

        threading.Thread(target=worker, daemon=True).start()

    def _enforce_detail_limit(self):
        """Remove detailed fields from the oldest cache files beyond DETAIL_LIMIT."""
        if not CACHE_DIR.exists():
            return
        detailed_files = []
        for f in CACHE_DIR.glob("*.json"):
            try:
                with open(f, encoding="utf-8") as fp:
                    if json.load(fp).get("_detailed"):
                        detailed_files.append(f)
            except Exception:
                pass
        detailed_files.sort(key=lambda f: f.stat().st_mtime)
        while len(detailed_files) > DETAIL_LIMIT:
            victim = detailed_files.pop(0)
            try:
                with open(victim, encoding="utf-8") as fp:
                    data = json.load(fp)
                data.pop("_detailed", None)
                data.pop("goals", None)
                data.pop("winning_team", None)
                data.pop("forfeit", None)
                data.pop("team_size", None)
                data.pop("demos_timeline", None)
                data.pop("ball_stats", None)
                for p in data.get("players", []):
                    for k in ("score", "goals", "assists", "saves", "shots",
                              "demos", "demoed", "boost", "positioning", "movement"):
                        p.pop(k, None)
                with open(victim, "w", encoding="utf-8") as fp:
                    json.dump(data, fp)
            except Exception:
                pass

    # ── canvas click ──────────────────────────────────────────────────────────

    def _on_canvas_click(self, event):
        cy = self.canvas.canvasy(event.y)
        cw = self.canvas.winfo_width()
        for card in self._active_cards:
            if card["y"] <= cy <= card["y"] + card["height"]:
                if _COMPACT:
                    cols  = card.get("grid_cols", 2)
                    col   = card.get("grid_col",  0)
                    gap   = _dims()["card_pad"]
                    col_w = (cw - 2 * CARD_MARGIN_X - (cols - 1) * gap) // cols
                    x0    = CARD_MARGIN_X + col * (col_w + gap)
                    x1    = x0 + col_w
                    if not (x0 <= event.x <= x1):
                        continue
                if not card.get("parsing"):
                    self._show_detail(card)
                return

    # ── detail page ───────────────────────────────────────────────────────────

    def _build_detail_page(self):
        self.detail_page  = ctk.CTkFrame(self, fg_color="transparent")
        self._current_card: dict | None = None

        header = ctk.CTkFrame(self.detail_page, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(14, 2))

        # ── row 1: back button + action buttons ───────────────────────────────
        top_row = ctk.CTkFrame(header, fg_color="transparent")
        top_row.pack(fill="x")
        ctk.CTkButton(top_row, text="← Replays", width=90, height=28,
                      fg_color="transparent", border_width=1,
                      border_color=("#3B8ED0", "#1F6AA5"),
                      command=self._show_replays_from_detail).pack(side="left")
        acts = ctk.CTkFrame(top_row, fg_color="transparent")
        acts.pack(side="right")
        _btn_kw = dict(height=28, fg_color="transparent", border_width=1,
                       border_color=C_BORDER)
        self._btn_upload = ctk.CTkButton(acts, text="Upload", width=70,
                                         command=self._detail_upload, **_btn_kw)
        self._btn_upload.pack(side="left", padx=(0, 4))
        self._btn_bc = ctk.CTkButton(acts, text="↗ Ballchasing", width=110,
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
                               text="↗ Ballchasing")

        # Serve from cache if already fully parsed
        cached = load_detailed(card["filename"])
        if cached:
            self._render_detail(cached, card)
            return

        self._render_parsing(card)

        def worker():
            card_info = parse_card_data(card["path"])
            if card_info:
                save_cache(card["filename"], card_info)
                self.after(0, lambda i=card_info: self._update_card_info(card, i))
            info = parse_detailed(card["path"])
            if info:
                save_detailed(card["filename"], info)
                self.after(0, self._enforce_detail_limit)
            self.after(0, lambda: self._render_detail(info, card))
        threading.Thread(target=worker, daemon=True).start()

    def _update_card_info(self, card: dict, info: dict):
        old_h = card["height"]
        card["info"]   = info
        card["type"]   = replay_type(info)
        card["height"] = card_height(info)
        if card["height"] != old_h:
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

    def _render_detail(self, info: dict | None, card: dict):
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
                   else "rattletrap.exe not found — run Setup.bat to download it.")
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

        # ── score banner ──────────────────────────────────────────────────────
        wt = info.get("winning_team", -1)
        banner = ctk.CTkFrame(self.detail_content, fg_color="#1e1e1e", corner_radius=6)
        banner.pack(fill="x", pady=(4, 8))
        banner.grid_columnconfigure(1, weight=1)
        replay_name = info.get("replay_name") or card.get("replay_name") or ""
        if replay_name:
            ctk.CTkLabel(banner, text=replay_name,
                         font=ctk.CTkFont(size=16, weight="bold"), text_color=C_SCORE
                         ).grid(row=0, column=0, columnspan=3, padx=20, pady=(10, 2), sticky="w")
        score_row = 1 if replay_name else 0
        ctk.CTkLabel(banner,
                     text=f"BLUE{'  ▲' if wt == 0 else ''}",
                     text_color=C_BLUE,
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).grid(row=score_row, column=0, padx=20, pady=(2 if replay_name else 10, 10), sticky="w")
        ctk.CTkLabel(banner,
                     text=f"{info.get('team0',0)}  -  {info.get('team1',0)}",
                     font=ctk.CTkFont(size=32, weight="bold")
                     ).grid(row=score_row, column=1, pady=(2 if replay_name else 10, 10))
        ctk.CTkLabel(banner,
                     text=f"ORANGE{'  ▲' if wt == 1 else ''}",
                     text_color=C_ORANGE,
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).grid(row=score_row, column=2, padx=20, pady=(2 if replay_name else 10, 10), sticky="e")

        # ── tabbed stats ──────────────────────────────────────────────────────
        tabview = ctk.CTkTabview(self.detail_content, fg_color="transparent",
                                 segmented_button_fg_color="#1e1e1e",
                                 segmented_button_selected_color="#2a2a2a",
                                 segmented_button_selected_hover_color="#333333",
                                 segmented_button_unselected_color="#1e1e1e",
                                 segmented_button_unselected_hover_color="#252525")
        tabview.pack(fill="both", expand=True, pady=(4, 0))
        for t in ("Overall", "Boost", "Positioning", "Movement", "Ball"):
            tabview.add(t)

        def _tab(name):
            return tabview.tab(name)

        # ── Overall tab ───────────────────────────────────────────────────────
        ot = _tab("Overall")
        blue   = [p for p in info["players"] if p["team"] == 0]
        orange = [p for p in info["players"] if p["team"] == 1]
        SCOLS   = ["Score", "Shots", "Goals", "Shoot%", "Assists", "Saves",
                   "Demos\nInflicted", "Demos\nTaken"]
        HDR_OFF = 2

        for team_players, color in [(blue, C_BLUE), (orange, C_ORANGE)]:
            tframe = ctk.CTkFrame(ot, fg_color="#1e1e1e", corner_radius=6)
            tframe.pack(fill="x", pady=(0, 6))
            ctk.CTkLabel(tframe, text="Player", text_color=color,
                         font=ctk.CTkFont(size=13, weight="bold"), anchor="w"
                         ).grid(row=0, column=1, padx=(4, 8), pady=(10, 4), sticky="w")
            for i, hdr in enumerate(SCOLS):
                ctk.CTkLabel(tframe, text=hdr, text_color=color,
                             font=ctk.CTkFont(size=12, weight="bold"), anchor="e",
                             justify="right"
                             ).grid(row=0, column=HDR_OFF + i,
                                    padx=(6, 6), pady=(10, 4), sticky="e")
            tk.Frame(tframe, bg=C_DIVIDER, height=1).grid(
                row=1, column=0, columnspan=HDR_OFF + len(SCOLS), sticky="ew", padx=8)
            for row, p in enumerate(team_players, 2):
                url = tracker_url(p)
                if url:
                    ctk.CTkButton(tframe, text="↗", width=26, height=22,
                                  fg_color="transparent", border_width=1,
                                  border_color=C_BORDER, font=ctk.CTkFont(size=11),
                                  command=lambda u=url: webbrowser.open(u)
                                  ).grid(row=row, column=0, padx=(10, 2), pady=3, sticky="w")
                ctk.CTkLabel(tframe, text=p["name"],
                             text_color=color, font=ctk.CTkFont(size=14), anchor="w"
                             ).grid(row=row, column=1, padx=(4, 8), pady=5, sticky="w")
                goals  = p.get("goals",  0)
                shots  = p.get("shots",  0)
                sh_pct = f"{goals/shots*100:.1f}%" if shots else "0.0%"
                stats  = [str(p.get("score", 0)), str(shots), str(goals), sh_pct,
                          str(p.get("assists", 0)), str(p.get("saves", 0)),
                          str(p.get("demos",  0)), str(p.get("demoed", 0))]
                for i, val in enumerate(stats):
                    ctk.CTkLabel(tframe, text=val,
                                 text_color=C_SCORE, font=ctk.CTkFont(size=14), anchor="e"
                                 ).grid(row=row, column=HDR_OFF + i,
                                        padx=(6, 6), pady=5, sticky="e")

        # ── Boost tab ─────────────────────────────────────────────────────────
        bt = _tab("Boost")
        boost_players = [p for p in blue + orange if p.get("boost")]
        dur = info.get("duration") or 0
        if boost_players:
            bframe = ctk.CTkFrame(bt, fg_color="#1e1e1e", corner_radius=6)
            bframe.pack(fill="x", pady=(0, 6))
            BCOLS = ["Player", "BPM", "Avg", "Time\n0 boost", "Time\n100 boost",
                     "Collected", "Big pads\ncollected", "Small pads\ncollected",
                     "Amount\nused", "Used at\nSSL", "Overfill"]
            for col, hdr in enumerate(BCOLS):
                ctk.CTkLabel(bframe, text=hdr, text_color=C_DIM,
                             font=ctk.CTkFont(size=12, weight="bold"),
                             justify="right"
                             ).grid(row=0, column=col,
                                    padx=(14 if col==0 else 6, 6), pady=(10, 4),
                                    sticky="w" if col==0 else "e")
            tk.Frame(bframe, bg=C_DIVIDER, height=1).grid(
                row=1, column=0, columnspan=len(BCOLS), sticky="ew", padx=8)
            for row, p in enumerate(boost_players, 2):
                b     = p["boost"]
                m     = p.get("movement") or {}
                color = C_BLUE if p["team"] == 0 else C_ORANGE
                t0s   = f"{b.get('time_empty_pct', 0) * dur / 100:.2f}s" if dur else "—"
                t100s = f"{b.get('time_full_pct',  0) * dur / 100:.2f}s" if dur else "—"
                vals  = [p["name"],
                         f"{b.get('bpm', 0):.0f}",
                         f"{b.get('avg_boost', 0):.1f}",
                         t0s, t100s,
                         f"{b.get('amount_collected', 0):.0f}",
                         str(m.get("big_pads",   0)),
                         str(m.get("small_pads", 0)),
                         f"{b.get('boost_used', 0):.0f}",
                         f"{m.get('boost_sonic_used', 0):.1f}",
                         f"{m.get('overfill', 0):.1f}"]
                for col, val in enumerate(vals):
                    ctk.CTkLabel(bframe, text=val,
                                 text_color=color if col == 0 else C_SCORE,
                                 font=ctk.CTkFont(size=14)
                                 ).grid(row=row, column=col,
                                        padx=(14 if col==0 else 6, 6), pady=5,
                                        sticky="w" if col==0 else "e")

        else:
            ctk.CTkLabel(bt, text="No boost data available.",
                         text_color=C_DIM, font=ctk.CTkFont(size=13)
                         ).pack(pady=20)

        # ── Positioning tab ───────────────────────────────────────────────────
        pt = _tab("Positioning")
        pos_players = [p for p in blue + orange if p.get("positioning")]
        if pos_players:
            pframe = ctk.CTkFrame(pt, fg_color="#1e1e1e", corner_radius=6)
            pframe.pack(fill="x", pady=(0, 6))
            PCOLS = ["Player", "Def. 1/3", "Neut. 1/3", "Off. 1/3",
                     "Def. 1/2", "Off. 1/2"]
            for col, hdr in enumerate(PCOLS):
                ctk.CTkLabel(pframe, text=hdr, text_color=C_DIM,
                             font=ctk.CTkFont(size=12, weight="bold")
                             ).grid(row=0, column=col,
                                    padx=(14 if col==0 else 10, 10), pady=(10, 4),
                                    sticky="w" if col==0 else "e")
            tk.Frame(pframe, bg=C_DIVIDER, height=1).grid(
                row=1, column=0, columnspan=len(PCOLS), sticky="ew", padx=8)
            for row, p in enumerate(pos_players, 2):
                pos   = p["positioning"]
                color = C_BLUE if p["team"] == 0 else C_ORANGE

                def _ps(pct):
                    s = f"{pct * dur / 100:.2f}s" if dur else ""
                    return f"{s}\n({pct:.2f}%)" if s else f"{pct:.2f}%"

                vals = [p["name"],
                        _ps(pos.get("def_pct",      0)),
                        _ps(pos.get("mid_pct",      0)),
                        _ps(pos.get("atk_pct",      0)),
                        _ps(pos.get("def_half_pct", 0)),
                        _ps(pos.get("off_half_pct", 0))]
                for col, val in enumerate(vals):
                    ctk.CTkLabel(pframe, text=val,
                                 text_color=color if col == 0 else C_SCORE,
                                 font=ctk.CTkFont(size=13), anchor="e",
                                 justify="right"
                                 ).grid(row=row, column=col,
                                        padx=(14 if col==0 else 10, 10), pady=4,
                                        sticky="w" if col==0 else "e")

        else:
            ctk.CTkLabel(pt, text="No positioning data available.",
                         text_color=C_DIM, font=ctk.CTkFont(size=13)
                         ).pack(pady=20)

        # ── Movement tab ──────────────────────────────────────────────────────
        mt = _tab("Movement")
        move_players = [p for p in blue + orange if p.get("movement")]
        if move_players:
            mframe = ctk.CTkFrame(mt, fg_color="#1e1e1e", corner_radius=6)
            mframe.pack(fill="x", pady=(0, 6))
            MCOLS = ["Player", "Avg\nSpeed%", "Tot.\nDist.", "Time slow\nspeed",
                     "Time boost\nspeed", "Time\nsupersonic", "Time\nground",
                     "Time\nlow air", "Time\nhigh air",
                     "Powerslide\ntot/avg", "PS\ncount"]
            for col, hdr in enumerate(MCOLS):
                ctk.CTkLabel(mframe, text=hdr, text_color=C_DIM,
                             font=ctk.CTkFont(size=12, weight="bold"),
                             justify="right"
                             ).grid(row=0, column=col,
                                    padx=(14 if col==0 else 6, 6), pady=(10, 4),
                                    sticky="w" if col==0 else "e")
            tk.Frame(mframe, bg=C_DIVIDER, height=1).grid(
                row=1, column=0, columnspan=len(MCOLS), sticky="ew", padx=8)
            for row, p in enumerate(move_players, 2):
                m     = p["movement"]
                color = C_BLUE if p["team"] == 0 else C_ORANGE
                st    = m.get("slide_total", 0)
                sc    = m.get("slide_count", 0)
                sa    = m.get("slide_avg", 0)
                vals  = [p["name"],
                         f"{m.get('avg_speed_pct', 0):.2f}%",
                         f"{int(m.get('total_dist', 0)):,}",
                         f"{m.get('time_slow', 0):.2f}s",
                         f"{m.get('time_boost', 0):.2f}s",
                         f"{m.get('time_supersonic', 0):.2f}s",
                         f"{m.get('time_ground', 0):.2f}s",
                         f"{m.get('time_low_air', 0):.2f}s",
                         f"{m.get('time_high_air', 0):.2f}s",
                         f"{st:.2f}s/{sa:.2f}s",
                         str(sc)]
                for col, val in enumerate(vals):
                    ctk.CTkLabel(mframe, text=val,
                                 text_color=color if col == 0 else C_SCORE,
                                 font=ctk.CTkFont(size=13)
                                 ).grid(row=row, column=col,
                                        padx=(14 if col==0 else 6, 6), pady=5,
                                        sticky="w" if col==0 else "e")
        else:
            ctk.CTkLabel(mt, text="No movement data available.",
                         text_color=C_DIM, font=ctk.CTkFont(size=13)
                         ).pack(pady=20)

        # ── Ball tab ──────────────────────────────────────────────────────────
        blt = _tab("Ball")
        ball = info.get("ball_stats")
        bsframe = ctk.CTkFrame(blt, fg_color="#1e1e1e", corner_radius=6)
        bsframe.pack(fill="x", pady=(0, 6))
        bsframe.columnconfigure(0, minsize=100)
        bsframe.columnconfigure(1, weight=1)

        if ball:
            def _ball_row(parent, row_idx, label, blue_pct, orange_pct):
                pady = (8 if row_idx == 0 else 4, 4)
                ctk.CTkLabel(parent, text=label, text_color=C_DIM,
                             font=ctk.CTkFont(size=13, weight="bold")
                             ).grid(row=row_idx, column=0, padx=(14, 8), pady=pady, sticky="w")
                canvas = tk.Canvas(parent, height=22, bg="#2a2a2a", highlightthickness=0)
                canvas.grid(row=row_idx, column=1, padx=(0, 14), pady=pady, sticky="ew")

                def _draw(e, c=canvas, bp=blue_pct, op=orange_pct):
                    w = e.width
                    h = e.height
                    if w < 2:
                        return
                    c.delete("all")
                    bw = max(1, int(w * bp / 100))
                    c.create_rectangle(0,  0, bw, h, fill=C_BLUE,   outline="")
                    c.create_rectangle(bw, 0, w,  h, fill=C_ORANGE, outline="")
                    # % labels inside each segment
                    font = ("Segoe UI", 9, "bold")
                    mid_y = h // 2
                    if bw > 30:
                        c.create_text(bw // 2, mid_y, text=f"{bp:.1f}%",
                                      fill="black", anchor="center", font=font)
                    if (w - bw) > 30:
                        c.create_text(bw + (w - bw) // 2, mid_y, text=f"{op:.1f}%",
                                      fill="black", anchor="center", font=font)

                canvas.bind("<Configure>", _draw)

            _ball_row(bsframe, 0, "Possession",
                      ball.get("blue_possession_pct", 50),
                      ball.get("orange_possession_pct", 50))
            _ball_row(bsframe, 1, "Ball Side",
                      ball.get("blue_side_pct", 50),
                      ball.get("orange_side_pct", 50))
            tk.Frame(bsframe, height=6, bg="#1e1e1e").grid(row=2, column=0, columnspan=2)
        else:
            ctk.CTkLabel(bsframe, text="Ball stats — analyzing…",
                         text_color=C_DIM, font=ctk.CTkFont(size=13)
                         ).pack(pady=10)

        # ── filename ──────────────────────────────────────────────────────────
        ctk.CTkLabel(self.detail_content, text=card["filename"],
                     text_color=C_DIM, font=ctk.CTkFont(size=11)
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
                self.after(0, self._refresh_bc_btn, bc_id)
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
            self._current_card = None
            self._show_replays_from_detail()
            self._apply_filters()
        except Exception as e:
            messagebox.showerror("Delete failed", str(e))

    # ── canvas click ──────────────────────────────────────────────────────────

    def _on_canvas_click(self, event):
        cy = self.canvas.canvasy(event.y)
        for card in self._cards:
            if card["y"] <= cy <= card["y"] + card["height"]:
                if not card.get("parsing"):
                    self._show_detail(card)
                return

    def _show_replays_from_detail(self):
        self.detail_page.pack_forget()
        self.replays_page.pack(fill="both", expand=True)
        self.nav_btn.configure(text="←")
        self._current_page = "replays"
        self._schedule_redraw()

    # ── player stats page ────────────────────────────────────────────────────

    def _build_stats_page(self):
        self.stats_page = ctk.CTkFrame(self, fg_color="transparent")

        hdr = ctk.CTkFrame(self.stats_page, fg_color="transparent")
        hdr.pack(fill="x", padx=20, pady=(14, 6))
        ctk.CTkButton(hdr, text="← Replays", width=90, height=28,
                      fg_color="transparent", border_width=1,
                      border_color=("#3B8ED0", "#1F6AA5"),
                      command=self._show_replays_from_stats).pack(side="left")
        ctk.CTkLabel(hdr, text="Player Stats",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(side="left", padx=(14, 0))

        search_row = ctk.CTkFrame(self.stats_page, fg_color="transparent")
        search_row.pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkLabel(search_row, text="Player:", text_color=C_DIM,
                     font=ctk.CTkFont(size=13)).pack(side="left", padx=(0, 6))
        self._stats_entry = ctk.CTkEntry(search_row, width=220,
                                         placeholder_text="player name",
                                         font=ctk.CTkFont(size=13))
        self._stats_entry.pack(side="left", padx=(0, 8))
        ctk.CTkButton(search_row, text="Search", width=80, height=28,
                      command=self._run_stats_search).pack(side="left")

        self.stats_content = ctk.CTkScrollableFrame(self.stats_page,
                                                    fg_color="transparent")
        self.stats_content.pack(fill="both", expand=True, padx=20, pady=(0, 16))

    def _prefill_stats_name(self, name: str):
        if not self._stats_entry.get():
            self._stats_entry.insert(0, name)

    def _show_stats(self):
        self.main_page.pack_forget()
        self.settings_page.pack_forget()
        self.replays_page.pack_forget()
        self.detail_page.pack_forget()
        self.stats_page.pack(fill="both", expand=True)
        self.nav_btn.configure(text="←")
        self._current_page = "stats"

    def _show_replays_from_stats(self):
        self.stats_page.pack_forget()
        self.replays_page.pack(fill="both", expand=True)
        self.nav_btn.configure(text="←")
        self._current_page = "replays"
        self._schedule_redraw()

    def _run_stats_search(self):
        name = self._stats_entry.get().strip()
        if not name:
            return
        for w in self.stats_content.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.stats_content, text="Scanning replays…",
                     text_color=C_DIM, font=ctk.CTkFont(size=13)).pack(pady=20)
        def worker():
            games = self._compute_player_stats(name)
            self.after(0, lambda: self._render_player_stats(name, games))
        threading.Thread(target=worker, daemon=True).start()

    def _compute_player_stats(self, name: str) -> list:
        games = []
        if not CACHE_DIR.exists():
            return games
        name_lower = name.strip().lower()
        upload_ids = load_upload_ids()
        cache_files = sorted(CACHE_DIR.glob("*.json"),
                             key=lambda f: f.stat().st_mtime, reverse=True)
        for f in cache_files:
            try:
                with open(f, encoding="utf-8") as fp:
                    info = json.load(fp)
            except Exception:
                continue
            if not info.get("_detailed"):
                continue
            players = info.get("players", [])
            p = next((p for p in players
                      if p.get("name", "").strip().lower() == name_lower), None)
            if p is None:
                continue
            filename = f.stem   # e.g. "abc123.replay"
            wt = info.get("winning_team", -1)
            won = (wt == p.get("team", -2)) if wt >= 0 else None
            ts  = info.get("team_size", 0)
            mode_str = {1: "1v1", 2: "2v2", 3: "3v3", 4: "4v4"}.get(ts, f"{ts}v{ts}" if ts else "")
            games.append({
                "date":      (info.get("date") or "")[:10],
                "map":       map_display_name(info.get("map", "")),
                "mode":      mode_str,
                "won":       won,
                "score":     p.get("score",   0),
                "goals":     p.get("goals",   0),
                "assists":   p.get("assists", 0),
                "saves":     p.get("saves",   0),
                "shots":     p.get("shots",   0),
                "demos":     p.get("demos",   0),
                "demoed":    p.get("demoed",  0),
                "bpm":       (p.get("boost") or {}).get("bpm", 0),
                "bc_id":     upload_ids.get(filename, ""),
                "filename":  filename,
            })
        return games

    def _render_player_stats(self, name: str, games: list):
        for w in self.stats_content.winfo_children():
            w.destroy()

        if not games:
            ctk.CTkLabel(self.stats_content,
                         text=f'No detailed replay data found for "{name}".\n'
                              'Only the most recent 20 replays are stored with detailed stats.',
                         text_color=C_DIM, font=ctk.CTkFont(size=13),
                         justify="center").pack(pady=30)
            return

        n       = len(games)
        wins    = sum(1 for g in games if g["won"] is True)
        losses  = sum(1 for g in games if g["won"] is False)
        tot_sh  = sum(g["shots"]   for g in games)
        tot_gl  = sum(g["goals"]   for g in games)

        def _avg(key): return sum(g[key] for g in games) / n

        # ── aggregate block ───────────────────────────────────────────────────
        agg = ctk.CTkFrame(self.stats_content, fg_color="#1e1e1e", corner_radius=6)
        agg.pack(fill="x", pady=(0, 10))
        agg.columnconfigure(tuple(range(8)), weight=1)

        def _agg_col(frame, col, label, value):
            ctk.CTkLabel(frame, text=label, text_color=C_DIM,
                         font=ctk.CTkFont(size=11)).grid(
                row=0, column=col, padx=12, pady=(10, 2), sticky="ew")
            ctk.CTkLabel(frame, text=value, text_color=C_SCORE,
                         font=ctk.CTkFont(size=16, weight="bold")).grid(
                row=1, column=col, padx=12, pady=(0, 10), sticky="ew")

        win_pct = f"{wins/n*100:.0f}%" if n else "—"
        _agg_col(agg, 0, "Games",   str(n))
        _agg_col(agg, 1, "W / L",   f"{wins} / {losses}")
        _agg_col(agg, 2, "Win %",   win_pct)
        _agg_col(agg, 3, "Avg Score", f"{_avg('score'):.0f}")
        _agg_col(agg, 4, "Goals/g",  f"{_avg('goals'):.2f}")
        _agg_col(agg, 5, "Assists/g",f"{_avg('assists'):.2f}")
        _agg_col(agg, 6, "Saves/g",  f"{_avg('saves'):.2f}")
        _agg_col(agg, 7, "Shots/g",  f"{_avg('shots'):.2f}")

        agg2 = ctk.CTkFrame(self.stats_content, fg_color="#1e1e1e", corner_radius=6)
        agg2.pack(fill="x", pady=(0, 10))
        agg2.columnconfigure(tuple(range(5)), weight=1)
        shoot_pct = f"{tot_gl/tot_sh*100:.1f}%" if tot_sh else "0.0%"
        _agg_col(agg2, 0, "Shoot %",    shoot_pct)
        _agg_col(agg2, 1, "Demos/g",    f"{_avg('demos'):.2f}")
        _agg_col(agg2, 2, "Demoed/g",   f"{_avg('demoed'):.2f}")
        bpm_vals = [g["bpm"] for g in games if g["bpm"]]
        avg_bpm = f"{sum(bpm_vals)/len(bpm_vals):.0f}" if bpm_vals else "—"
        _agg_col(agg2, 3, "Avg BPM",    avg_bpm)
        _agg_col(agg2, 4, "Replays",    f"last {n}")

        # ── per-game table ────────────────────────────────────────────────────
        ctk.CTkLabel(self.stats_content, text="Recent games",
                     text_color=C_DIM, font=ctk.CTkFont(size=12, weight="bold"),
                     anchor="w").pack(fill="x", pady=(4, 2))

        tbl = ctk.CTkFrame(self.stats_content, fg_color="#1e1e1e", corner_radius=6)
        tbl.pack(fill="x")
        TCOLS = ["Date", "Map", "Mode", "Score", "G", "A", "S", "Sh", "Result", "↗"]
        for col, hdr in enumerate(TCOLS):
            ctk.CTkLabel(tbl, text=hdr, text_color=C_DIM,
                         font=ctk.CTkFont(size=12, weight="bold"),
                         anchor="w" if col < 3 else "e"
                         ).grid(row=0, column=col,
                                padx=(12 if col == 0 else 6, 6), pady=(8, 4),
                                sticky="w" if col < 3 else "e")
        tk.Frame(tbl, bg=C_DIVIDER, height=1).grid(
            row=1, column=0, columnspan=len(TCOLS), sticky="ew", padx=8)

        for row, g in enumerate(games, 2):
            if g["won"] is True:
                result_text, result_color = "Win",  "#4aaa88"
            elif g["won"] is False:
                result_text, result_color = "Loss", "#e06060"
            else:
                result_text, result_color = "—", C_DIM
            vals = [g["date"], g["map"] or "—", g["mode"],
                    str(g["score"]), str(g["goals"]), str(g["assists"]),
                    str(g["saves"]), str(g["shots"])]
            for col, val in enumerate(vals):
                ctk.CTkLabel(tbl, text=val, text_color=C_SCORE,
                             font=ctk.CTkFont(size=13),
                             anchor="w" if col < 3 else "e"
                             ).grid(row=row, column=col,
                                    padx=(12 if col == 0 else 6, 6), pady=3,
                                    sticky="w" if col < 3 else "e")
            ctk.CTkLabel(tbl, text=result_text, text_color=result_color,
                         font=ctk.CTkFont(size=13), anchor="e"
                         ).grid(row=row, column=8, padx=(6, 6), pady=3, sticky="e")
            if g["bc_id"]:
                ctk.CTkButton(tbl, text="↗", width=28, height=22,
                              fg_color="transparent", border_width=1,
                              border_color=C_BORDER, font=ctk.CTkFont(size=11),
                              command=lambda bid=g["bc_id"]: webbrowser.open(
                                  f"https://ballchasing.com/replay/{bid}")
                              ).grid(row=row, column=9, padx=(4, 10), pady=3)
            else:
                ctk.CTkLabel(tbl, text="", width=28).grid(row=row, column=9, padx=(4, 10))

    # ── settings page ─────────────────────────────────────────────────────────

    def _build_settings_page(self):
        self.settings_page = ctk.CTkFrame(self, fg_color="transparent")
        self.settings_page.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.settings_page, text="Settings",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).grid(row=0, column=0, columnspan=3, sticky="w", padx=20, pady=(18,10))

        ctk.CTkLabel(self.settings_page, text="API Key").grid(
            row=1, column=0, sticky="w", padx=20, pady=10)
        self.api_entry = ctk.CTkEntry(self.settings_page, show="•",
                                      placeholder_text="Paste your upload token")
        self.api_entry.grid(row=1, column=1, padx=8, pady=10, sticky="ew")
        self.api_entry.insert(0, self.config_data.get("api_key", ""))
        ctk.CTkButton(self.settings_page, text="Show", width=65,
                      command=self._toggle_key_visibility
                      ).grid(row=1, column=2, padx=(0,20))

        ctk.CTkLabel(self.settings_page, text="Demos Folder").grid(
            row=2, column=0, sticky="w", padx=20, pady=10)
        self.folder_entry = ctk.CTkEntry(self.settings_page,
                                         placeholder_text="Path to Demos folder")
        self.folder_entry.grid(row=2, column=1, padx=8, pady=10, sticky="ew")
        self.folder_entry.insert(0, self.config_data.get("demos_folder", ""))
        ctk.CTkButton(self.settings_page, text="Browse", width=65,
                      command=self._browse_folder
                      ).grid(row=2, column=2, padx=(0,20))

        ctk.CTkLabel(self.settings_page, text="Visibility").grid(
            row=3, column=0, sticky="w", padx=20, pady=10)
        self.vis_var = ctk.StringVar(value=self.config_data.get("visibility", "unlisted"))
        ctk.CTkOptionMenu(self.settings_page, values=["public", "unlisted", "private"],
                          variable=self.vis_var, width=150
                          ).grid(row=3, column=1, sticky="w", padx=8, pady=10)

        ctk.CTkLabel(self.settings_page, text="Auto Upload").grid(
            row=4, column=0, sticky="w", padx=20, pady=10)
        self.upload_on_detect_var = ctk.BooleanVar(
            value=self.config_data.get("upload_on_detect", True))
        ctk.CTkSwitch(self.settings_page, text="Automatically upload new replays to Ballchasing",
                      variable=self.upload_on_detect_var, onvalue=True, offvalue=False
                      ).grid(row=4, column=1, sticky="w", padx=8, pady=10)

        ctk.CTkLabel(self.settings_page, text="Start with Windows").grid(
            row=5, column=0, sticky="w", padx=20, pady=10)
        self.run_on_startup_var = ctk.BooleanVar(
            value=self._get_run_on_startup())
        ctk.CTkSwitch(self.settings_page, text="Launch automatically when Windows starts",
                      variable=self.run_on_startup_var, onvalue=True, offvalue=False
                      ).grid(row=5, column=1, sticky="w", padx=8, pady=10)

        ctk.CTkLabel(self.settings_page, text="Launch with Rocket League").grid(
            row=6, column=0, sticky="w", padx=20, pady=10)
        self.launch_with_rl_var = ctk.BooleanVar(
            value=self.config_data.get("launch_with_rl", False))
        ctk.CTkSwitch(self.settings_page, text="Start watching replays when Rocket League starts",
                      variable=self.launch_with_rl_var, onvalue=True, offvalue=False
                      ).grid(row=6, column=1, sticky="w", padx=8, pady=10)

        ctk.CTkButton(self.settings_page, text="Save Settings",
                      command=self._save_settings
                      ).grid(row=7, column=0, columnspan=3, pady=(20, 6))

        ctk.CTkButton(self.settings_page, text="Download Replays from Ballchasing",
                      fg_color="transparent", border_width=1, border_color=C_BORDER,
                      command=self._confirm_download
                      ).grid(row=8, column=0, columnspan=3, padx=20, pady=(0, 20), sticky="ew")

    # ── page switching ────────────────────────────────────────────────────────

    def _show_main(self):
        self.settings_page.pack_forget()
        self.replays_page.pack_forget()
        self.main_page.pack(fill="both", expand=True)
        self.nav_btn.configure(text="⚙")
        self._current_page = "main"

    def _show_settings(self):
        self.main_page.pack_forget()
        self.replays_page.pack_forget()
        self.settings_page.pack(fill="both", expand=True)
        self.nav_btn.configure(text="←")
        self._current_page = "settings"

    def _show_replays(self):
        self.main_page.pack_forget()
        self.settings_page.pack_forget()
        self.replays_page.pack(fill="both", expand=True)
        self.nav_btn.configure(text="←")
        self._current_page = "replays"
        if not self._cards:
            self._load_replays()

    def _on_nav(self):
        if self._current_page == "main":
            self._show_settings()
        elif self._current_page == "detail":
            self._show_replays_from_detail()
        elif self._current_page == "stats":
            self._show_replays_from_stats()
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

    def _save_settings(self):
        old_key    = self.config_data.get("api_key", "")
        old_folder = self.config_data.get("demos_folder", "")
        self.config_data["api_key"]          = self.api_entry.get().strip()
        self.config_data["demos_folder"]     = self.folder_entry.get().strip()
        self.config_data["visibility"]       = self.vis_var.get()
        self.config_data["upload_on_detect"] = self.upload_on_detect_var.get()
        self.config_data["launch_with_rl"]   = self.launch_with_rl_var.get()
        save_config(self.config_data)
        self._set_run_on_startup(self.run_on_startup_var.get())
        self._set_launch_with_rl(self.launch_with_rl_var.get())
        changed_watch = (
            self.config_data["api_key"] != old_key
            or self.config_data["demos_folder"] != old_folder
        )
        if changed_watch and self.observer and self.observer.is_alive():
            self._stop_watching()
            self.after(200, self._start_watching)

    # ── startup / RL watcher helpers ──────────────────────────────────────────

    _REG_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _APP_KEY  = "BallchasingUploader"

    def _launcher_cmd(self) -> str:
        pythonw  = Path(sys.executable).with_name("pythonw.exe")
        launcher = BASE / "launcher.py"
        return f'"{pythonw}" "{launcher}"'

    def _get_run_on_startup(self) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._REG_RUN) as k:
                winreg.QueryValueEx(k, self._APP_KEY)
                return True
        except OSError:
            return False

    def _set_run_on_startup(self, enabled: bool):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._REG_RUN, 0,
                                winreg.KEY_SET_VALUE) as k:
                if enabled:
                    winreg.SetValueEx(k, self._APP_KEY, 0, winreg.REG_SZ, self._launcher_cmd())
                else:
                    try:
                        winreg.DeleteValue(k, self._APP_KEY)
                    except OSError:
                        pass
        except Exception as e:
            self._log(f"[startup] Could not update registry: {e}", "red")

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
            time.sleep(60)

    # ── BakkesMod event log ───────────────────────────────────────────────────

    def _poll_bakkesmod_log(self):
        try:
            if BAKKESMOD_LOG.exists():
                with open(BAKKESMOD_LOG, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._bakkesmod_log_pos)
                    new_lines = f.readlines()
                    self._bakkesmod_log_pos = f.tell()
                for line in new_lines:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split("|", 1)
                    ts    = parts[0] if len(parts) == 2 else ""
                    event = parts[1] if len(parts) == 2 else parts[0]
                    label = {
                        "plugin_loaded":   "BakkesMod plugin loaded",
                        "plugin_unloaded": "BakkesMod plugin unloaded",
                        "match_started":   "Match started",
                        "match_ended":     "Match ended — replay available to save",
                    }.get(event, event)
                    self._log(f"[bakkesmod] {ts}  {label}")
        except Exception:
            pass
        self.after(2000, self._poll_bakkesmod_log)

    def _on_startup_toggle(self):
        try:
            set_startup_enabled(self.startup_var.get())
        except Exception as e:
            messagebox.showerror("Startup", f"Could not update startup entry:\n{e}")
            self.startup_var.set(get_startup_enabled())

    # ── watcher ───────────────────────────────────────────────────────────────

    def _toggle_watching(self):
        if self.observer and self.observer.is_alive():
            self._stop_watching()
        else:
            self._start_watching()

    def _show_watch_error(self, msg: str):
        self.watch_error_label.configure(text=msg)
        if hasattr(self, "_watch_err_id") and self._watch_err_id:
            self.after_cancel(self._watch_err_id)
        self._watch_err_id = self.after(6000, lambda: self.watch_error_label.configure(text=""))

    def _start_watching(self):
        api_key = self.config_data.get("api_key", "").strip()
        folder  = self.config_data.get("demos_folder", "").strip()
        missing = []
        if not api_key:
            missing.append("API key")
        if not folder or not Path(folder).is_dir():
            missing.append("replay folder path")
        if missing:
            self._show_watch_error(f"Missing {' and '.join(missing)} — open Settings ⚙ to configure.")
            return

        self._show_watch_error("Checking connection…")
        self.toggle_btn.configure(state="disabled")

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
        self.toggle_btn.configure(state="normal")
        self.watch_error_label.configure(text="")
        answer = messagebox.askyesno(
            "Connection Issue",
            f"{reason}\n\nStart watcher without auto upload?",
            icon="warning")
        if answer:
            self._do_start_watching(upload_enabled=False)

    def _do_start_watching(self, upload_enabled: bool):
        self.toggle_btn.configure(state="normal")
        self.watch_error_label.configure(text="")
        self._upload_session_enabled = upload_enabled
        folder = self.config_data.get("demos_folder", "").strip()

        def on_new_replay(filename: str):
            if self._download_active:
                return
            self.after(0, self._log, f"new replay detected: {filename}")
            path = Path(folder) / filename

            # Background: detailed-parse the new replay, evict oldest
            def do_detail(p=path, n=filename):
                if RATTLETRAP.exists() and not load_detailed(n):
                    self.after(0, self._log, f"[parse] Network-parsing {n}…")
                    info = parse_detailed(p)
                    if info:
                        save_detailed(n, info)
                        self._enforce_detail_limit()
                        self.after(0, self._log, f"[parse] Done  {n}")
            threading.Thread(target=do_detail, daemon=True).start()

            if self._upload_session_enabled and self.config_data.get("upload_on_detect", True):

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
                    upload(p, self.config_data, self.uploaded, on_status,
                           on_bc_id=lambda bc_id, fn=n: save_upload_id(fn, bc_id))

                threading.Thread(target=do_upload, daemon=True).start()

        handler = ReplayHandler(on_new_replay)
        self.observer = Observer()
        self.observer.schedule(handler, folder, recursive=False)
        self.observer.start()
        self._set_status(True)
        self._log(f"Watching: {folder}")
        if not upload_enabled:
            self._log("Auto upload disabled for this session.", "red")

    def _stop_watching(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
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

    def _silent_dedup(self, folder: str):
        def worker():
            import hashlib as _hl
            folder_path = Path(folder)
            files = list(folder_path.glob("*.replay"))
            size_groups: dict[int, list] = {}
            for f in files:
                try:
                    size_groups.setdefault(f.stat().st_size, []).append(f)
                except OSError:
                    pass
            duplicates: list[Path] = []
            for same_size in size_groups.values():
                if len(same_size) < 2:
                    continue
                seen: dict[str, Path] = {}
                for f in same_size:
                    try:
                        h = _hl.md5(f.read_bytes()).hexdigest()
                        if h in seen:
                            duplicates.append(f)
                        else:
                            seen[h] = f
                    except OSError:
                        pass
            if duplicates:
                deleted = 0
                for f in duplicates:
                    try:
                        f.unlink()
                        deleted += 1
                    except OSError:
                        pass
                self.after(0, self._log, f"[dedup] Removed {deleted} duplicate(s).")
            else:
                self.after(0, self._log, "[dedup] No duplicates found.")
        threading.Thread(target=worker, daemon=True).start()

    def _confirm_remove_duplicates(self):
        folder = self.config_data.get("demos_folder", "").strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showerror("Missing Config", "A valid demos folder is required.")
            return
        self._show_main()
        self._log("[dedup] Scanning demos folder for duplicates…")

        def worker():
            import hashlib
            folder_path = Path(folder)
            files = list(folder_path.glob("*.replay"))
            size_groups: dict[int, list] = {}
            for f in files:
                try:
                    size_groups.setdefault(f.stat().st_size, []).append(f)
                except OSError:
                    pass

            duplicates: list[Path] = []
            for same_size in size_groups.values():
                if len(same_size) < 2:
                    continue
                seen_hashes: dict[str, Path] = {}
                for f in same_size:
                    try:
                        h = hashlib.md5(f.read_bytes()).hexdigest()
                        if h in seen_hashes:
                            duplicates.append(f)
                        else:
                            seen_hashes[h] = f
                    except OSError:
                        pass

            if not duplicates:
                self.after(0, self._log, f"[dedup] No duplicates found in {len(files):,} files.")
                return

            def confirm():
                if messagebox.askyesno(
                        "Duplicates Found",
                        f"Found {len(duplicates)} duplicate file(s) across {len(files):,} replays.\n\nDelete them? One copy of each will be kept.",
                        icon="warning"):
                    deleted = 0
                    for f in duplicates:
                        try:
                            f.unlink()
                            deleted += 1
                        except OSError:
                            pass
                    self._log(f"[dedup] Deleted {deleted} duplicate(s).")
                    if self._cards:
                        self.after(100, self._load_replays)
                else:
                    self._log("[dedup] Cancelled.")

            self.after(0, confirm)

        threading.Thread(target=worker, daemon=True).start()

    def _start_bulk_download(self, api_key: str, folder: str, total: int = 0):
        self._download_active  = True
        self._dl_progress_line = None
        self._dl_status_line   = None
        self._dl_error_line    = None
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

    def _finish_bulk_download(self):
        watching = self.observer and self.observer.is_alive()
        self._set_status(watching)

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
                        existing_ids.add(_norm_id(dest.stem))
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
                                self.after(0, self._update_dl_status,
                                           f"[download] ramping up: {1/delay:.2f}/s")
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
        if self._download_active and total_dl > 0:
            self.after(0, self._log, "[download] Running dedup to remove any duplicates…")
            self.after(0, self._silent_dedup, folder)

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
                    self.after(0, self._prefill_stats_name, name)
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

    def _set_status(self, watching: bool):
        if watching:
            self.status_dot.configure(text_color="#2ecc71")
            self.status_label.configure(text="Watching")
            self.toggle_btn.configure(text="Stop Watching", fg_color="#c0392b",
                                      hover_color="#962d22")
        else:
            self.status_dot.configure(text_color="#555")
            self.status_label.configure(text="Stopped")
            self.toggle_btn.configure(text="Start Watching",
                                      fg_color=("#3B8ED0","#1F6AA5"),
                                      hover_color=("#36719F","#144870"))

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


    # ── expiry / auth check ───────────────────────────────────────────────────

    def _check_expiry(self):
        threading.Thread(target=self._do_expiry_check, daemon=True).start()

    def _do_expiry_check(self):
        try:
            guid   = winreg.QueryValueEx(
                winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"),
                "MachineGuid")[0]
            token  = self.config_data.get("_auth_token", "")
            signed = self.config_data.get("_signed_expiry", "")

            result = self._verify_expiry(signed, guid)
            if result is None:
                self.after(0, self._handle_expired)
                return

            expiry, tier = result
            self._tier = tier

            if expiry:
                from datetime import datetime
                expiry_ts = datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S UTC").timestamp()
                if time.time() < expiry_ts:
                    self.after(EXPIRY_CHECK_MS, self._check_expiry)
                    return

            if token and guid:
                self.after(0, self._log, "↻ refreshing licence…")
                try:
                    r = requests.post(f"{APP_SERVER}/verify",
                                      json={"machine_guid": guid, "token": token},
                                      timeout=8)
                    if r.status_code == 200:
                        data     = r.json()
                        new_exp  = data.get("expiry") or ""
                        new_tier = data.get("tier", "free_tester")
                        self._tier = new_tier
                        self.config_data["_signed_expiry"] = self._sign_expiry(new_exp, new_tier, guid)
                        self.config_data.pop("_tier", None)
                        save_config(self.config_data)
                        self.after(0, self._log, f"✓ licence refreshed — tier: {new_tier}, expires: {new_exp or 'never'}")
                        if getattr(self, "_revoked", False):
                            self.after(0, self._restore_from_revoke)
                        else:
                            self.after(EXPIRY_CHECK_MS, self._check_expiry)
                    elif r.status_code == 403:
                        self.after(0, self._log, "✗ licence check failed (403 — access revoked)", "red")
                        self.after(0, self._handle_expired)
                    else:
                        self.after(0, self._log, f"✗ licence check failed (HTTP {r.status_code})")
                        self.after(EXPIRY_CHECK_MS, self._check_expiry)
                except Exception as e:
                    self.after(0, self._log, f"✗ licence check error: {e}")
                    self.after(EXPIRY_CHECK_MS, self._check_expiry)
            else:
                self.after(EXPIRY_CHECK_MS, self._check_expiry)
        except Exception:
            self.after(EXPIRY_CHECK_MS, self._check_expiry)

    def _sign_expiry(self, expiry: str, tier: str, guid: str) -> str:
        payload = f"{expiry}|{tier}"
        sig = hmac.new(guid.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return base64.b64encode(f"{payload}|{sig}".encode()).decode()

    def _verify_expiry(self, signed: str, guid: str):
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

    def _handle_expired(self):
        self._stop_watching()
        self._revoked = True
        for widget in self.winfo_children():
            widget.destroy()
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(frame, text="Access Revoked",
                     font=ctk.CTkFont(size=22, weight="bold"),
                     text_color="#e06060").pack(pady=(0, 12))
        ctk.CTkLabel(frame,
                     text="Your subscription has expired or been revoked.\nContact support to restore access.",
                     font=ctk.CTkFont(size=13),
                     text_color="#aaaaaa").pack(pady=(0, 28))
        ctk.CTkButton(frame, text="Close", command=self.destroy, width=120).pack()
        self.after(30_000, self._check_expiry)

    def _restore_from_revoke(self):
        import subprocess
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        subprocess.Popen([str(pythonw), str(Path(__file__).resolve())])
        self.destroy()

    # ── update check ─────────────────────────────────────────────────────────

    def _send_ping(self):
        def worker():
            try:
                import socket, platform, datetime
                requests.post(
                    "http://46.101.184.78:8765/ping",
                    json={
                        "user":    __import__("os").getlogin(),
                        "machine": socket.gethostname(),
                        "version": VERSION,
                        "os":      platform.platform(),
                        "time":    datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                    },
                    timeout=5)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _check_for_updates(self):
        def worker():
            try:
                resp = requests.get(
                    f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                    timeout=8)
                if resp.status_code != 200:
                    return
                data   = resp.json()
                tag    = data.get("tag_name", "")
                latest = tag.lstrip("v")
                if latest and latest != VERSION:
                    self.after(0, self._show_update_notice, latest, tag)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _show_update_notice(self, version: str, tag: str):
        if not messagebox.askyesno(
                "Update Available",
                f"Version {version} is available (you have {VERSION}).\n\n"
                f"Update now?",
                icon="info"):
            return
        threading.Thread(target=self._do_update, args=(version, tag), daemon=True).start()

    def _do_update(self, version: str, tag: str):
        self.after(0, self._log, f"[update] Downloading v{version}…")
        url = (f"https://raw.githubusercontent.com/{GITHUB_REPO}"
               f"/{tag}/src/main.pyw")
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code != 200:
                self.after(0, self._log,
                           f"[update] Download failed ({resp.status_code}) — "
                           f"please update manually.", "red")
                return
            new_code = resp.content

            # Write to a temp file first so a failed download can't corrupt the current file
            script = Path(__file__).resolve()
            tmp    = script.with_suffix(".pyw.tmp")
            tmp.write_bytes(new_code)
            tmp.replace(script)

            self.after(0, self._log, f"[update] Updated to v{version} — restarting…", "green")
            self.after(800, self._restart)
        except Exception as e:
            self.after(0, self._log, f"[update] Update failed: {e}", "red")

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
            issues.append("rattletrap.exe not found — run Setup.bat to download it.")
        folder = self.config_data.get("demos_folder", "").strip()
        if folder and not Path(folder).is_dir():
            issues.append(f"Demos folder does not exist: {folder}")
        for issue in issues:
            self._log(f"[setup] {issue}", "red")

    def _on_close(self):
        self._bg_sync_active = False
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
    _missing = _check_dependencies()
    if _missing:
        _root = tk.Tk()
        _root.withdraw()
        messagebox.showerror(
            "Missing Dependencies",
            f"Required packages not installed: {', '.join(_missing)}\n\n"
            f"Run Setup.bat to install them.")
        _root.destroy()
        sys.exit(1)
    App().mainloop()

