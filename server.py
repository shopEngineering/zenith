#!/usr/bin/env python3
"""ZENITH/OS backend — zero-dependency Python stdlib server.

Serves the ZENITH/OS SPA and a JSON API over Claude Code's real on-disk data:
  ~/.claude/projects/<encoded>/  session transcripts (JSONL)
  ~/claudeProjects/              projects + docs/specs
  NexusPrime/data/nexusprime.db  NexusMind memories + schedules (SQLite, read-only)
  /tmp/claude-coordination/      live session coordination state
  ~/.claude/agents, ~/.claude/plugins  agents + skills inventory
Plus a job runner that shells out to headless `claude -p`.

Run: python3 server.py   →  http://127.0.0.1:8777
"""
import atexit
import base64
import collections
import glob
import hashlib
import json
import os
import platform
import re
import importlib.util
import shlex
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import zenith_store as zs   # event-store spine (same-dir import; no packaging)
import zenith_agents as za  # agent adapter layer (P2; same boundary rules)
import zenith_cases as zc   # cases detectors + sweep loop (same boundary rules)

# Terminal/PTY is the only platform-specific layer. On Unix we use the stdlib
# pty; on Windows the real-PTY path needs pywinpty (lazy-imported when a terminal
# is spawned). Guarding these imports lets the whole server boot on Windows so all
# non-terminal features (sessions, projects, docs, memory, jobs, whisper, statusline)
# work there regardless.
IS_WINDOWS = os.name == "nt"
IS_DARWIN = platform.system() == "Darwin"
if not IS_WINDOWS:
    import fcntl
    import pty as ptymod
    import signal
    import termios

PORT = int(os.environ.get("ZENITH_PORT", "8777"))
BIND = os.environ.get("ZENITH_BIND", "127.0.0.1")   # loopback default; config-resolved at boot
ZENITH_VERSION = "v3-obs1"   # stamped into server.start events
HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
PROJECTS_ROOT = HOME / "claudeProjects"
TRANSCRIPTS_ROOT = CLAUDE_DIR / "projects"
AGENTS_DIR = CLAUDE_DIR / "agents"
USER_SKILLS_DIR = CLAUDE_DIR / "skills"
PLUGINS_DIR = CLAUDE_DIR / "plugins"
# Written by the session-watch daemon, never by ZENITH — see session_watch_list().
SESSION_WATCH_DB = CLAUDE_DIR / "session-watch" / "state.db"
# ...and the one file in that directory ZENITH DOES own. The watcher re-reads it on
# every pass, so writing it IS the apply step — see session_watch_config_save().
SESSION_WATCH_CONFIG = CLAUDE_DIR / "session-watch" / "config.json"
# NexusMind HTTP API (owner-authed) — the ONE door to memory. ZENITH runs as a host
# process and hits the container's PUBLISHED port, so its requests arrive as the
# docker-gateway IP (not loopback) — the bearer token is REQUIRED. The token is read
# from a 0600 file (ZENITH_NM_TOKEN_FILE, holding `NM_API_TOKEN=...` or the raw token)
# or the ZENITH_NM_TOKEN env. See nm_api() below; every memory/watcher route goes
# through it. ZENITH never speaks SQL to NexusMind's schema (see "NexusMind" below).
ZENITH_NM_API = os.environ.get("ZENITH_NM_API", "http://127.0.0.1:5055")
ZENITH_NM_TOKEN_FILE = os.environ.get("ZENITH_NM_TOKEN_FILE", "")
ZENITH_NM_TOKEN_ENV = os.environ.get("ZENITH_NM_TOKEN", "")
# Watchers write-through: the homelab checkout holding the canonical watcher
# YAMLs (mini/watchers/<name>.yaml). Explicit env, NOT HOME-derived — on the
# a service install may run as root (HOME=/var/root) while the checkout belongs to a
# normal user, so git runs via `sudo -u <owner>`. Absent dir (dev box) → git skipped.
ZENITH_HOMELAB_DIR = os.environ.get("ZENITH_HOMELAB_DIR", "")
ZENITH_HOMELAB_GIT_USER = os.environ.get("ZENITH_HOMELAB_GIT_USER", "")
COORD_ROOT = (Path(tempfile.gettempdir()) / "claude-coordination" if IS_WINDOWS
              else Path("/tmp/claude-coordination"))
# Path resolution. Normal (source) runs anchor everything at the repo dir next to
# this file. In a PyInstaller bundle __file__/sys._MEIPASS is READ-ONLY, so the
# bundled resources (static/, embedded statusline source) live under _MEIPASS while
# writable state (data/, the materialised statusline script) must move to a
# per-user writable dir. This split is gated strictly on the frozen flag so the
# source run below is byte-for-byte identical to before.
if getattr(sys, "frozen", False):
    ZENITH_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    ZENITH_STATE_DIR = HOME / ".zenith"          # writable per-user root
    # .resolve() so the static-serving containment check matches: on macOS _MEIPASS
    # lives under /var/folders (a symlink to /private/var), and _static() compares
    # against p.resolve(); an unresolved STATIC_DIR would fail that check → 403.
    STATIC_DIR = (ZENITH_DIR / "static").resolve()   # bundled, read-only
    DATA_DIR = ZENITH_STATE_DIR / "data"         # writable
else:
    ZENITH_DIR = Path(__file__).resolve().parent
    STATIC_DIR = ZENITH_DIR / "static"
    # System installs (e.g. the .deb) put the app under a root-owned, read-only
    # prefix like /opt/zenith. Setting ZENITH_STATE_DIR redirects all writable
    # state (data/, the materialised statusline script) to a per-user dir while
    # statics stay under ZENITH_DIR. Unset → same tree as source (byte-identical
    # to a plain `python3 server.py` run from a checkout).
    _state_override = os.environ.get("ZENITH_STATE_DIR")
    ZENITH_STATE_DIR = Path(_state_override).expanduser() if _state_override else ZENITH_DIR
    DATA_DIR = ZENITH_STATE_DIR / "data"
# ZENITH's own bin/ goes on PATH. A manifest agent whose `bin` is a shipped helper
# script (bin/pa-job.sh) then resolves by bare name wherever ZENITH is installed —
# no absolute path in a file that ships. Mutating os.environ rather than just the
# child env is deliberate: shutil.which (za.resolve_bin), subprocess and every
# spawned terminal must agree on one PATH, or the agent probe reports "missing"
# for a binary the job would in fact have found.
_ZENITH_BIN = ZENITH_DIR / "bin"
if _ZENITH_BIN.is_dir():
    os.environ["PATH"] = str(_ZENITH_BIN) + os.pathsep + os.environ.get("PATH", "")
# Release version — single source of truth (the repo-root VERSION file, bundled as a
# data file in the packaged builds). Drives the UI version display + the update check.
# (ZENITH_VERSION above is the internal build tag stamped into events; this is the
# user-facing release semver.)
try:
    ZENITH_RELEASE = (ZENITH_DIR / "VERSION").read_text(encoding="utf-8").strip()
except Exception:
    ZENITH_RELEASE = "0.0.0"
ZENITH_REPO_SLUG = os.environ.get("ZENITH_REPO_SLUG", "shopEngineering/zenith")
CLAUDE_BIN = shutil.which("claude") or (
    "/opt/homebrew/bin/claude" if IS_DARWIN else "claude")

TAILSCALE_BIN = shutil.which("tailscale") or next(
    (p for p in ("/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
                 "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
     if os.path.exists(p)), None)


def _augment_path(env):
    """Prepend Homebrew's bin on macOS so `claude` resolves under the GUI PATH."""
    if IS_DARWIN:
        env["PATH"] = "/opt/homebrew/bin:" + env.get("PATH", "")
    return env
STALE_SECS = 7200
START_TIME = time.time()

# New v2 data files (all created lazily under DATA_DIR).
LOOPS_FILE = DATA_DIR / "loops.json"
LOOP_RUNS_FILE = DATA_DIR / "loop_runs.jsonl"
DB_PATH = DATA_DIR / "zenith.db"            # event store (WAL siblings live here too)
AUTOVERIFY_FILE = DATA_DIR / "autoverify.json"   # §5.7 (used from P4)
PROVIDERS_FILE = DATA_DIR / "providers.json"
# Base for the three-way seed merge: what defaults/*.json last shipped INTO data/.
# Shared with zenith_agents.py (za.SNAPSHOT_NAME) — one file, one key per section.
SEED_SNAPSHOT_FILE = DATA_DIR / za.SNAPSHOT_NAME
AGENTS2_FILE = DATA_DIR / "agents.json"     # P2 agent manifest (routes: /api/agents2)
SWARMS_FILE = DATA_DIR / "swarms.json"
WARGAMES_FILE = DATA_DIR / "wargames.json"
SAVED_JOBS_FILE = DATA_DIR / "saved_jobs.json"
SESSION_NAMES_FILE = DATA_DIR / "session_names.json"   # {transcript_path: custom label} — user-renamed sessions
NM_CAPTURE_QUEUE = DATA_DIR / "nm_capture_queue.jsonl"
CONFIG_FILE = DATA_DIR / "config.json"      # portability/integration config (auto-seeded, gitignored)
CONFIG = {}                                 # loaded config dict; config_apply() recomputes globals from it
INTEGRATION_IDS = ("nexusmind_api", "homelab", "voice", "fleet")
RESEARCH_DIR = DATA_DIR / "research"
PROPOSALS_DIR = RESEARCH_DIR / "proposals"
WARGAMES_DIR = DATA_DIR / "wargames"

# TerminalX ports (v2.6): voice/whisper + Claude-CLI statusline.
WHISPER_TMP = DATA_DIR / "whisper_tmp"
STATUSLINE_CONFIG = DATA_DIR / "statusline_config.json"
SETTINGS_JSON = CLAUDE_DIR / "settings.json"
# Statusline script is materialised at runtime (write), so it hangs off the
# writable state dir. Its embedded source derives DATA_DIR as SCRIPT_DIR.parent/"data",
# which lines up with DATA_DIR above in both frozen and source modes.
STATUSLINE_SCRIPT = ZENITH_STATE_DIR / "scripts" / "statusline.py"

# ------------------------------------------------------------- model policy tables
# The alias table, effort tables, verify ladder and context windows used to be Python
# literals scattered through this file. They now ship as defaults/models.json (tracked,
# diffable) with data/models.json (gitignored, absent by default) merged over it — the
# same three-layer rule as everything else: data > defaults > built-in fallback. The
# fallback below is byte-identical to what shipped before, so a missing or corrupt
# defaults file changes nothing.
MODELS_FILE = DATA_DIR / "models.json"      # optional local override (import target)
MODELS_FALLBACK = {
    "aliases": {"haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-sonnet-5",
                "opus": "claude-opus-4-8", "fable": "claude-fable-5"},
    "effort_tokens": {"low": 2048, "medium": 8192, "high": 31999},
    "verify_ladder": {"haiku": "sonnet", "sonnet": "opus", "opus": "fable",
                      "fable": "fable"},
    "context_windows": {
        "claude-opus-5": 1000000, "claude-fable-5": 1000000,
        "claude-sonnet-5": 1000000, "claude-opus-4": 200000,
        "claude-sonnet-4": 200000, "claude-haiku-4": 200000,
        "claude-3": 200000, "gpt-5": 400000, "o3": 200000,
    },
    "context_window_fallback": 200000,
}


def _models_layer(section, want, base):
    """One section of the model tables: fallback, overlaid by defaults/models.json,
    overlaid by data/models.json. Entries whose value fails `want` are dropped — a
    hand-edited table must never be able to crash a boot (declared trust boundary)."""
    out = dict(base)
    for src in (za.load_defaults("models", {}), _models_local()):
        part = src.get(section) if isinstance(src, dict) else None
        if not isinstance(part, dict):
            continue
        for k, v in part.items():
            if want(v):
                out[str(k)] = v
    return out


def _models_local():
    """data/models.json → dict. Absent/corrupt → {}. Never raises (runs at import)."""
    try:
        with open(MODELS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _models_defaults():
    """The effective, validated model tables (what the module globals are built from)."""
    _pos_int = lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0  # noqa: E731
    _str = lambda v: isinstance(v, str) and bool(v.strip())                        # noqa: E731
    fb = MODELS_FALLBACK
    win_fb = fb["context_window_fallback"]
    for src in (za.load_defaults("models", {}), _models_local()):
        v = src.get("context_window_fallback") if isinstance(src, dict) else None
        if _pos_int(v):
            win_fb = v
    return {
        "aliases": _models_layer("aliases", _str, fb["aliases"]),
        "effort_tokens": _models_layer("effort_tokens", _pos_int, fb["effort_tokens"]),
        "verify_ladder": _models_layer("verify_ladder", _str, fb["verify_ladder"]),
        "context_windows": _models_layer("context_windows", _pos_int,
                                         fb["context_windows"]),
        "context_window_fallback": win_fb,
    }


_MODELS = _models_defaults()
# alias -> full model id for swarm/wargame nodes.
MODEL_MAP = _MODELS["aliases"]
EFFORT_TOKENS = _MODELS["effort_tokens"]

# Roots the /api/file endpoint may read from. Everything else is refused.
# Pasted-image drop dir — a SHORT home path (not the long repo data/ path) so the path typed into a
# Claude terminal is short: it doesn't wrap inside the TUI box (which broke the clickable link) and
# clutters the input less. In READ_ROOTS so clicking the path renders the image via /raw.
PASTED_IMG_DIR = HOME / ".zenith-img"
READ_ROOTS = [PROJECTS_ROOT, TRANSCRIPTS_ROOT, AGENTS_DIR, USER_SKILLS_DIR, DATA_DIR, PASTED_IMG_DIR]

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "dist", "build", "__pycache__",
             ".next", "target", ".claude"}

_cache_lock = threading.Lock()
_session_cache = {}   # path -> (mtime, size, summary dict)
_detail_cache = {}    # path -> (mtime, detail dict)
_projstats_cache = {} # path -> (sig, stats dict)
_skills_cache = None

JOBS = {}             # id -> job dict
_jobs_lock = threading.Lock()
_data_lock = threading.Lock()   # guards data/*.json read-modify-write


def _load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    tmp.replace(path)


def _append_jsonl(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


# ---------------------------------------------------------------- portability config
# data/config.json lifts the last personal constants + optional-integration knobs out
# of source. Three-layer resolution (env > config file > built-in default) recomputes
# the SAME module globals the ~30 call sites already read, so nothing downstream changes.
# (Capabilities/probes are Phase 2 — this phase only relocates where values come from.)

# key-path in config.json -> the existing env var that overrides it (None = config/UI only).
# Single source of truth for GET /api/config's env_overrides and config_apply's env layer.
CONFIG_ENV_MAP = {
    "server.port": "ZENITH_PORT",
    "server.bind": "ZENITH_BIND",
    "integrations.nexusmind_api.base_url": "ZENITH_NM_API",
    "integrations.nexusmind_api.token_file": "ZENITH_NM_TOKEN_FILE",
    "integrations.nexusmind_api.token": "ZENITH_NM_TOKEN",
    "integrations.homelab.dir": "ZENITH_HOMELAB_DIR",
    "integrations.homelab.git_user": "ZENITH_HOMELAB_GIT_USER",
    "integrations.voice.flowd_url": "FLOWD_URL",
    "integrations.voice.whisper_model": "WHISPER_MODEL",
}


def _cfg_get(cfg, path, default=None):
    """Nested dict lookup by dotted key-path. Absent (any missing hop) → default;
    an explicitly-present value (including "") is returned as-is."""
    cur = cfg
    for k in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _resolve(env_key, cfg_val, default):
    """env > config > default. A SET env var wins even when empty (mirrors the
    existing os.environ.get(key, default) semantics per-var). Config layer applies
    when the key is present (cfg_val is not None); otherwise the built-in default."""
    if env_key and env_key in os.environ:
        return os.environ[env_key]
    if cfg_val is not None:
        return cfg_val
    return default


def config_load():
    """Load data/config.json → dict. Missing OR corrupt → {} (all defaults). A
    corrupt (present-but-unparseable) file logs one warning and is left in place
    (never clobbered)."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            print("ZENITH/OS: config.json unreadable — using defaults, file left "
                  f"in place: {CONFIG_FILE}")
            return {}
    return {}


def config_seed():
    """Seed data/config.json on first boot if absent. Distinguishes an UPGRADE of an
    existing install (zenith.db already present → first_run_done, all-auto, no wizard —
    exactly today's behavior) from a FRESH install (no zenith.db → first_run_done False,
    triggers the wizard in a later phase). One-time legacy sniff migrates the mini's
    homelab defaults into its local (gitignored) config so the personal literals can
    leave tracked source. A present-but-corrupt file is left untouched."""
    if CONFIG_FILE.exists():
        return
    existing = DB_PATH.exists()          # the event store => an already-running install
    seed = {"version": 1, "first_run_done": existing,
            "integrations": {k: {"mode": "auto"} for k in INTEGRATION_IDS},
            "modules": {}}
    # legacy sniff: an install that predates the config file, whose homelab repo sat at
    # ~/homelab. Keyed off HOME rather than a hardcoded path so it works for whoever is
    # running it — and so nothing here needs scrubbing before the code can be published.
    if existing and (HOME / "homelab" / ".git").exists():
        seed["integrations"]["homelab"]["dir"] = str(HOME / "homelab")
        seed["integrations"]["homelab"]["git_user"] = os.environ.get("USER", "")
    try:
        with _data_lock:
            _save_json(CONFIG_FILE, seed)
    except OSError:
        pass


def config_apply(cfg):
    """Recompute the module-level integration globals from cfg using three-layer
    resolution (§2.2). Keeps the exact same global NAMES so every call site is
    unchanged; only the source of each value moves. Paths are expanduser()'d."""
    global CONFIG, ZENITH_NM_API, ZENITH_NM_TOKEN_FILE, \
        ZENITH_NM_TOKEN_ENV, ZENITH_HOMELAB_DIR, ZENITH_HOMELAB_GIT_USER, \
        FLOWD_URL, WHISPER_MODEL_NAME, PORT, BIND
    CONFIG = cfg or {}
    def g(path):
        return _cfg_get(CONFIG, path)
    ZENITH_NM_API = _resolve("ZENITH_NM_API", g("integrations.nexusmind_api.base_url"),
                             "http://127.0.0.1:5055")
    ZENITH_NM_TOKEN_FILE = os.path.expanduser(_resolve(
        "ZENITH_NM_TOKEN_FILE", g("integrations.nexusmind_api.token_file"), ""))
    ZENITH_NM_TOKEN_ENV = _resolve("ZENITH_NM_TOKEN",
                                   g("integrations.nexusmind_api.token"), "")
    ZENITH_HOMELAB_DIR = os.path.expanduser(_resolve(
        "ZENITH_HOMELAB_DIR", g("integrations.homelab.dir"), ""))
    ZENITH_HOMELAB_GIT_USER = _resolve("ZENITH_HOMELAB_GIT_USER",
                                       g("integrations.homelab.git_user"), "")
    FLOWD_URL = _resolve("FLOWD_URL", g("integrations.voice.flowd_url"),
                         "http://127.0.0.1:8787")
    WHISPER_MODEL_NAME = _resolve("WHISPER_MODEL",
                                  g("integrations.voice.whisper_model"), "tiny.en")
    PORT = int(_resolve("ZENITH_PORT", g("server.port"), 8777))
    BIND = _resolve("ZENITH_BIND", g("server.bind"), "127.0.0.1")
    # The NM base_url/token/mode may have just changed: drop the reachability and
    # corpus caches so a Settings save re-probes NOW rather than up to 30s later
    # (capabilities(refresh=True) runs right after this on the config-save path).
    with _nm_rest_lock:
        _nm_rest_cache.update(checked=0.0, ok=False, detail="not probed")
    with _nm_list_lock:
        _nm_list_cache["rows"] = None


def _config_env_overrides():
    """Key-paths whose env var is currently set (env layer active) → the Settings UI
    renders those fields read-only with a 'set by env' badge."""
    return [kp for kp, ev in CONFIG_ENV_MAP.items() if ev in os.environ]


def _deep_merge(base, patch):
    """Recursively merge patch into base (dicts merge, scalars/lists replace).
    Returns a new dict; inputs are not mutated."""
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def config_redacted(cfg):
    """GET /api/config view: the stored config with the NM-API token redacted to ""
    plus a token_set flag (the raw token never reaches the browser)."""
    view = json.loads(json.dumps(cfg or {}))     # deep copy; never mutate CONFIG
    tok = _cfg_get(cfg or {}, "integrations.nexusmind_api.token", "")
    ints = view.setdefault("integrations", {})
    if isinstance(ints, dict):
        api = ints.setdefault("nexusmind_api", {})
        if isinstance(api, dict):
            api["token"] = ""
            api["token_set"] = bool(tok)
    return view


# --- integration mode + effective state (§3.1) -----------------------------
# mode is config/UI-only (no env equivalent). `off` short-circuits every probe
# and its API chokepoint BEFORE any subprocess/socket/file-stat runs (§3.2).

def _int_mode(iid):
    """Configured mode for an integration id: auto|on|off (default auto)."""
    m = _cfg_get(CONFIG, "integrations.%s.mode" % iid, "auto")
    return m if m in ("auto", "on", "off") else "auto"


def _int_off(iid):
    """True iff the integration is explicitly disabled (mode == off)."""
    return _int_mode(iid) == "off"


def _effective_active(mode, detected):
    """§3.1: off→False; on→True; auto→(detected is True). `detected` may be None
    (probe pending) — treated as not-yet-active under auto."""
    if mode == "off":
        return False
    if mode == "on":
        return True
    return detected is True


def _slug(text, maxlen=60):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "untitled").lower()).strip("-")
    return (s or "untitled")[:maxlen]


def encode_path(p):
    return str(p).replace("/", "-")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def project_encoding_map():
    """encoded transcript-dir name -> project path, longest match wins."""
    m = {}
    if PROJECTS_ROOT.exists():
        for p in PROJECTS_ROOT.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                m[encode_path(p)] = str(p)
    m[encode_path(PROJECTS_ROOT)] = str(PROJECTS_ROOT)
    m[encode_path(HOME)] = str(HOME)
    return m


def match_project(dirname, encmap):
    best, best_len = None, -1
    for enc, path in encmap.items():
        if (dirname == enc or dirname.startswith(enc + "--worktrees")) and len(enc) > best_len:
            best, best_len = path, len(enc)
    return best


# ---------------------------------------------------------------- transcripts

def _parse_lines(raw_lines):
    out = []
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return out


def _head_objs(path, nbytes=196608):
    with open(path, "rb") as f:
        chunk = f.read(nbytes)
    lines = chunk.split(b"\n")
    if len(chunk) == nbytes:
        lines = lines[:-1]
    return _parse_lines(lines)


def _tail_objs(path, nbytes=131072):
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - nbytes))
        chunk = f.read()
    lines = chunk.split(b"\n")
    if size > nbytes:
        lines = lines[1:]
    return _parse_lines(lines)


def _count_lines(path):
    n = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                return n
            n += chunk.count(b"\n")


_real_user_text = za.real_user_text   # moved to zenith_agents (P2 A2)


def session_summary(path):
    st = path.stat()
    key = str(path)
    with _cache_lock:
        hit = _session_cache.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
    head = _head_objs(path)
    tail = _tail_objs(path)
    s = {"path": key, "id": path.stem, "size": st.st_size,
         "mtime": st.st_mtime, "title": None, "first_prompt": None,
         "last_prompt": None, "cwd": None, "git_branch": None, "model": None,
         "last_ts": None, "first_ts": None, "lines": _count_lines(path)}
    for obj in head:
        if s["first_ts"] is None and obj.get("timestamp"):
            s["first_ts"] = obj["timestamp"]
        if s["cwd"] is None and obj.get("cwd"):
            s["cwd"] = obj["cwd"]
        if s["git_branch"] is None and obj.get("gitBranch"):
            s["git_branch"] = obj["gitBranch"]
        if s["title"] is None and obj.get("type") == "ai-title":
            s["title"] = obj.get("aiTitle")
        if s["first_prompt"] is None:
            t = _real_user_text(obj)
            if t:
                s["first_prompt"] = t[:280]
    for obj in reversed(tail):
        if s["last_ts"] is None and obj.get("timestamp"):
            s["last_ts"] = obj["timestamp"]
        if s["model"] is None and obj.get("type") == "assistant":
            s["model"] = (obj.get("message") or {}).get("model")
        if s["title"] is None and obj.get("type") == "ai-title":
            s["title"] = obj.get("aiTitle")
        if s["last_prompt"] is None:
            t = _real_user_text(obj)
            if t:
                s["last_prompt"] = t[:280]
        if s["last_ts"] and s["model"] and s["title"] and s["last_prompt"]:
            break
    with _cache_lock:
        _session_cache[key] = (st.st_mtime, st.st_size, s)
    return s


# ------------------------------------------------------------ prompt history
# "what did I actually type in this session?" — the transcript already holds it,
# but session_detail() re-parses the WHOLE file and a live session's mtime moves
# on every assistant token, so it would re-parse on every popover open. Transcripts
# are append-only, so we instead remember the byte offset already scanned and parse
# only the new tail; a live session stays cheap no matter how big it gets.

PROMPTS_MAX = 300          # keep the newest N; older ones fall off the front
PROMPT_TEXT_MAX = 2000     # enough to read a real prompt back, not a whole paste
PROMPTS_CACHE_MAX = 40     # transcripts kept warm; this cache is far fatter than
                           # _session_cache, so unlike that one it evicts (browsing
                           # a few hundred sessions would otherwise just grow)

# Claude Code injects these as user-type lines. They are not things the human typed,
# and the compaction summary in particular is tens of KB — never a useful "prompt".
_PROMPT_SKIP = (
    "This session is being continued from a previous conversation",
    "Caveat: The messages below were generated by the user while running local commands",
)

_prompts_cache = {}        # path -> {"off": int, "size": int, "prompts": [...]}
_prompts_lock = threading.Lock()


def _prompt_line(obj):
    """One {ts,text[,queued]} prompt from a claude jsonl object, else None.
    Covers BOTH shapes the human actually types into: a normal user message, and
    a message sent mid-turn while the agent is still working — Claude Code records
    that as a queue-operation and never as a user line, so a naive scan loses it."""
    if obj.get("type") == "queue-operation":
        if obj.get("operation") != "enqueue":
            return None
        t = (obj.get("content") or "").strip()
        if not t or t.startswith("<") or t.startswith(_PROMPT_SKIP):
            return None
        return {"ts": obj.get("timestamp"), "text": t[:PROMPT_TEXT_MAX], "queued": True}
    t = _real_user_text(obj)
    if not t or t.startswith(_PROMPT_SKIP):
        return None
    return {"ts": obj.get("timestamp"), "text": t[:PROMPT_TEXT_MAX]}


def _scan_prompts(path):
    """Prompts for a claude transcript, scanning only bytes appended since the
    last call. Returns newest-last. Never raises on a malformed line."""
    key = str(path)
    size = path.stat().st_size
    with _prompts_lock:
        c = _prompts_cache.get(key)
        if c and c["size"] == size:
            _prompts_cache[key] = _prompts_cache.pop(key)      # LRU: mark as freshest
            return list(c["prompts"])
        # a shrunk file means it was replaced, not appended → rescan from 0
        off = c["off"] if (c and size >= c["size"]) else 0
        out = list(c["prompts"]) if off else []
    with open(path, "rb") as f:
        f.seek(off)
        data = f.read()
    end = data.rfind(b"\n") + 1          # only consume complete lines
    for raw in data[:end].split(b"\n"):
        if not raw.strip():
            continue
        try:
            p = _prompt_line(json.loads(raw))
        except Exception:                # noqa: BLE001 — a bad line is not fatal
            continue
        if p:
            if not p.get("queued"):
                # a queued message that later ran for real appears twice — drop the
                # placeholder, keeping the real line's timestamp. Bounded lookback so
                # a genuinely repeated prompt ("commit") far earlier is never eaten.
                for i in range(len(out) - 1, max(-1, len(out) - 11), -1):
                    if out[i].get("queued") and out[i]["text"] == p["text"]:
                        del out[i]
                        break
            out.append(p)
    if len(out) > PROMPTS_MAX:
        out = out[-PROMPTS_MAX:]
    with _prompts_lock:
        _prompts_cache.pop(key, None)                          # re-insert at the end
        _prompts_cache[key] = {"off": off + end, "size": size, "prompts": list(out)}
        while len(_prompts_cache) > PROMPTS_CACHE_MAX:
            _prompts_cache.pop(next(iter(_prompts_cache)))     # evict least-recent
    return out


def session_prompts(path):
    """Prompts for any agent's transcript. Claude gets the incremental scanner;
    other formats fall back to the manifest parser (already mtime-cached upstream)."""
    fmt, _aid = za.transcript_format_for_path(str(path))   # returns (format, agent_id)
    if fmt in ("claude_jsonl", "none"):
        return _scan_prompts(path)
    return list((za.parse_transcript(fmt, str(path)) or {}).get("prompts") or [])


def session_detail(path, max_lines=400000):
    """mtime-cached transcript telemetry. Since P2 the parsing core lives in
    TRANSCRIPT_PARSERS['claude_jsonl']; this wrapper keeps the cache + the
    'summary' block, so the response shape is unchanged."""
    st = path.stat()
    key = str(path)
    with _cache_lock:
        hit = _detail_cache.get(key)
        if hit and hit[0] == st.st_mtime:
            return hit[1]
    d = dict(za.parse_transcript("claude_jsonl", str(path)))
    d["summary"] = session_summary(path)
    with _cache_lock:
        _detail_cache[key] = (st.st_mtime, d)
    return d


# ------------------------------------------------------ context-window occupancy
# What `/context` shows, rebuilt from the transcript. This is NOT the token rollup
# in session_token_stats: that SUMS usage across a session and answers "what did
# this cost". A context window is not cumulative — it is whatever was sent on the
# LAST turn. So (input + cache_read + cache_creation) of the final assistant
# message IS the current occupancy, and the same figure per turn is the curve.
#
# Honesty boundary: `/context` splits its overhead into system prompt / built-in
# tools / MCP tools because the CLI assembles those and knows each size. None of
# that reaches the transcript. We therefore report what we can MEASURE from disk
# (memory files, agent definitions) separately from what is DERIVED (the rest of
# the first-turn baseline, and the conversation), and never invent the split.

# model-id prefix -> window. Ships in defaults/models.json (see MODELS_FALLBACK) and
# stays overridable via config `context.windows`, because this is the one fact here
# that goes stale; longest matching prefix wins.
CONTEXT_WINDOWS_DEFAULT = _MODELS["context_windows"]
CONTEXT_WINDOW_FALLBACK = _MODELS["context_window_fallback"]
CHARS_PER_TOKEN = 4          # the standard rough estimate; every use is labelled est.


def _context_windows():
    w = dict(CONTEXT_WINDOWS_DEFAULT)
    try:
        over = (config_load().get("context") or {}).get("windows") or {}
        for k, v in over.items():
            if isinstance(v, int) and v > 0:
                w[str(k)] = v
    except (AttributeError, TypeError):
        pass                                  # a garbage config never breaks the view
    return w


def context_window_for(model):
    if not model:
        return CONTEXT_WINDOW_FALLBACK
    w = _context_windows()
    best = ""
    for pref in w:
        if model.startswith(pref) and len(pref) > len(best):
            best = pref
    return w[best] if best else CONTEXT_WINDOW_FALLBACK


def _est_tokens_of(paths):
    """(files, chars, est_tokens) for whatever exists. Never raises."""
    files = chars = 0
    for p in paths:
        try:
            chars += len(Path(p).read_text(errors="replace"))
            files += 1
        except (OSError, ValueError):
            continue
    return files, chars, chars // CHARS_PER_TOKEN


def _context_measured(cwd):
    """The parts of the prefix ZENITH can read off disk and count itself."""
    out = {}
    f, c, t = _est_tokens_of([HOME / ".claude" / "CLAUDE.md"])
    out["memory_global"] = {"files": f, "chars": c, "tokens": t, "measured": True,
                            "label": "Global CLAUDE.md"}
    proj = []
    if cwd:
        try:
            proj = [Path(cwd) / "CLAUDE.md"]
        except (TypeError, ValueError):
            proj = []
    f, c, t = _est_tokens_of(proj)
    out["memory_project"] = {"files": f, "chars": c, "tokens": t, "measured": True,
                             "label": "Project CLAUDE.md"}
    try:
        agents = sorted((HOME / ".claude" / "agents").glob("*.md"))
    except OSError:
        agents = []
    f, c, t = _est_tokens_of(agents)
    out["agents"] = {"files": f, "chars": c, "tokens": t, "measured": True,
                     "label": "Custom agents"}
    return out


def _context_series(path, max_points=160):
    """(series, turns, model, cwd) — per-turn occupancy, newest last. Streams the
    file; skips <synthetic> turns (API-error placeholders carry no real usage)."""
    pts, model, cwd = [], None, None
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line and '"cwd"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not cwd and o.get("cwd"):
                    cwd = o.get("cwd")
                if o.get("type") != "assistant":
                    continue
                m = o.get("message") or {}
                if m.get("model") == "<synthetic>":
                    continue
                u = m.get("usage") or {}
                tot = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
                    + (u.get("cache_creation_input_tokens") or 0)
                if tot > 0:
                    pts.append(tot)
                    model = m.get("model") or model
    except OSError:
        return [], 0, None, None
    turns = len(pts)
    if turns > max_points:                     # even stride, always keeping the last
        step = turns / float(max_points)
        idx = sorted({int(i * step) for i in range(max_points)} | {turns - 1})
        pts = [pts[i] for i in idx]
    return pts, turns, model, cwd


_context_cache = {}          # path -> (mtime, size, result); the statusline hits this
                             # every turn on a file that only ever grows


def session_context(path):
    """Context-window occupancy for one transcript. mtime+size cached: the
    statusline asks on every render, and re-streaming a 1M-token transcript
    each time would put real latency in Claude's own render path."""
    p = Path(path)
    try:
        st = p.stat()
        key = str(p)
        hit = _context_cache.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
    except OSError:
        st = None
    r = _session_context_uncached(p)
    if st is not None:
        if len(_context_cache) > 400:
            _context_cache.clear()               # crude bound; this is a read cache
        _context_cache[str(p)] = (st.st_mtime, st.st_size, r)
    return r


def _session_context_uncached(path):
    p = Path(path)
    series, turns, model, cwd = _context_series(p)
    if not series:
        return {"available": False, "reason": "no usage records in this transcript",
                "path": str(p), "turns": turns}
    current, baseline = series[-1], series[0]
    window = context_window_for(model)
    measured = _context_measured(cwd)
    meas_tok = sum(v["tokens"] for v in measured.values())
    # baseline = system + tools + MCP + memory + the first user message. Subtract what
    # we measured; the remainder is harness overhead we can size but cannot itemise.
    overhead = max(0, baseline - meas_tok)
    conversation = max(0, current - baseline)
    return {
        "available": True, "path": str(p), "cwd": cwd, "model": model,
        "turns": turns, "current": current, "baseline": baseline, "window": window,
        "pct": round(current / window * 100, 1) if window else None,
        "free": max(0, window - current), "peak": max(series),
        "series": series,
        "measured": measured,
        "derived": {
            "harness": {"tokens": overhead, "measured": False,
                        "label": "Harness overhead",
                        "note": "system prompt + tool schemas + MCP — sized, not itemisable"},
            "conversation": {"tokens": conversation, "measured": False,
                             "label": "Conversation"},
        },
        "chars_per_token": CHARS_PER_TOKEN,
    }


# ------------------------------------------------------------- plan usage (5h/7d)
# The rate-limit windows are the one number here that ISN'T derivable from disk —
# it comes from Anthropic's OAuth usage endpoint. The reason this belongs in ZENITH
# rather than in the statusline script: a statusline renders inside a process tree
# descended from whatever launched Claude. Under a LaunchDaemon that tree can only
# see System.keychain, so reading the OAuth token there fails ("User interaction is
# not allowed") and every such script degrades to a frozen cache. ZENITH's own
# process already carries CLAUDE_CODE_OAUTH_TOKEN (launch-zenith.sh sources it), so
# it can do the fetch once, cache it, and hand the answer to any number of renders.

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_TTL = 120.0            # seconds; the windows move slowly
_usage_cache = {"at": 0.0, "data": None}
_usage_lock = threading.Lock()


CLAUDE_CREDENTIALS = HOME / ".claude" / ".credentials.json"


def _oauth_token():
    """Claude Code's own login credential first, the start-up env var second.

    Never the keychain. Claude Code moved credentials OUT of the keychain into
    this 0600 file, which is why every keychain-reading statusline stopped
    working — and a file is readable by a daemon where the login keychain is not,
    so this also removes the session-0 problem entirely.

    Preferring it over the env var matters for scope: `claude setup-token` mints
    an inference token WITHOUT `user:profile`, and the usage endpoint 403s on it.
    The login credential carries the full scope set.

    Re-read on every call, never cached: the access token rotates (~8h), so
    holding the string would pin an expired one."""
    try:
        d = json.loads(CLAUDE_CREDENTIALS.read_text())
        tok = ((d.get("claudeAiOauth") or {}).get("accessToken") or "").strip()
        if tok:
            return tok
    except (OSError, ValueError, TypeError, AttributeError):
        pass                       # missing/corrupt/renamed → fall through
    return (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()


def _normalize_limits(d):
    """The response's `limits` array is the general shape — the five_hour /
    seven_day / seven_day_opus fields are a fixed legacy subset, and most are
    null. Driving off `limits` means a window we've never heard of (opus, sonnet,
    cowork, a model-scoped cap) shows up on its own the moment it becomes real.

    A weekly_scoped entry carries scope.model.display_name — that is where a
    per-model cap like Fable appears. Falls back to the legacy fields if a future
    response drops the array."""
    out = []
    for lim in (d.get("limits") or []):
        if not isinstance(lim, dict):
            continue
        kind = lim.get("kind") or ""
        scope = (lim.get("scope") or {}).get("model") or {}
        name = scope.get("display_name")
        label = name or {"session": "5h", "weekly_all": "7d"}.get(kind, kind or "?")
        out.append({"key": kind + (":" + name if name else ""), "label": label,
                    "percent": lim.get("percent"), "severity": lim.get("severity"),
                    "resets_at": lim.get("resets_at"),
                    "active": bool(lim.get("is_active")), "kind": kind, "model": name})
    if not out:                       # legacy shape only
        for k, lbl in (("five_hour", "5h"), ("seven_day", "7d")):
            w = d.get(k)
            if isinstance(w, dict) and w.get("utilization") is not None:
                out.append({"key": k, "label": lbl, "percent": w.get("utilization"),
                            "severity": None, "resets_at": w.get("resets_at"),
                            "active": True, "kind": k, "model": None})
    return out


def plan_usage(force=False):
    """{ok, five_hour, seven_day, fetched_at, source|error}. NEVER raises, and
    never lets a bad response destroy a good cached one."""
    now = time.time()
    with _usage_lock:
        cached = _usage_cache["data"]
        fresh = cached and (now - _usage_cache["at"]) < USAGE_TTL
    if fresh and not force:
        return dict(cached, source="cache", age=round(now - _usage_cache["at"], 1))
    tok = _oauth_token()
    if not tok:
        stale = dict(cached, source="stale", error="no CLAUDE_CODE_OAUTH_TOKEN in "
                     "ZENITH's environment") if cached else None
        return stale or {"ok": False, "error": "no CLAUDE_CODE_OAUTH_TOKEN in ZENITH's "
                         "environment — see launch-zenith.sh / claude-auth.env"}
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + tok,
        "anthropic-beta": "oauth-2025-04-20",
        "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        body, status = e.read().decode("utf-8", "replace")[:400], e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        body, status = str(e)[:200], 0
    try:
        d = json.loads(body)
    except (ValueError, TypeError):
        d = None
    if not isinstance(d, dict) or not (d.get("five_hour") or d.get("seven_day")):
        # Same rule the shell refresher used: a shape we don't recognise must not
        # overwrite good data. Surface WHY instead of silently freezing.
        err = {"ok": False, "http": status,
               "error": "response carried neither five_hour nor seven_day",
               "body_head": body[:200]}
        with _usage_lock:
            if _usage_cache["data"]:
                return dict(_usage_cache["data"], source="stale",
                            age=round(now - _usage_cache["at"], 1), **err)
        return err
    out = {"ok": True, "five_hour": d.get("five_hour"), "seven_day": d.get("seven_day"),
           "limits": _normalize_limits(d), "spend": d.get("spend"),
           "extra_usage": d.get("extra_usage"), "fetched_at": now, "http": status}
    with _usage_lock:
        _usage_cache["data"], _usage_cache["at"] = out, now
    return dict(out, source="live", age=0.0)


def contexts_all(limit=200):
    """Every claude transcript ranked by how full its window got. Cheap-ish: one
    streaming pass per file, newest files first, hard-capped."""
    files = []
    root = HOME / ".claude" / "projects"
    try:
        files = sorted(root.glob("*/*.jsonl"), key=lambda f: -f.stat().st_mtime)[:limit]
    except OSError:
        return {"sessions": [], "scanned": 0}
    out = []
    for f in files:
        series, turns, model, cwd = _context_series(f, max_points=2)
        if not series:
            continue
        window = context_window_for(model)
        cur = series[-1]
        out.append({"id": f.stem, "path": str(f), "project": match_project(
            f.parent.name, project_encoding_map()) or f.parent.name,
            "model": model, "turns": turns, "current": cur, "window": window,
            "pct": round(cur / window * 100, 1) if window else None,
            "mtime": f.stat().st_mtime})
    out.sort(key=lambda r: -(r["pct"] or 0))
    return {"sessions": out, "scanned": len(files)}


def _snapshot_session(path, d, agent="claude"):
    """Persist a transcript telemetry dict as a snapshot (D4/P2). Both feed
    paths (sweep + on-read) come through here; INSERT OR IGNORE on
    (session,mtime,size) makes them idempotent. Never raises. Emits
    session.seen the first time a transcript is indexed."""
    try:
        st = path.stat()
        if agent == "claude":
            proj = match_project(path.parent.name, project_encoding_map()) or ""
        else:      # aider: the transcript lives IN the project dir; codex: unknown
            parent = str(path.parent)
            proj = parent if parent.startswith(str(PROJECTS_ROOT) + os.sep) else ""
        u = d.get("usage") or {}
        is_new = str(path) not in zs.snapshot_sigs()
        ins = zs.snapshot_insert(
            session=str(path), project=proj, mtime=st.st_mtime, size=st.st_size,
            tok_in=u.get("input_tokens"), tok_out=u.get("output_tokens"),
            tok_cr=u.get("cache_read_input_tokens"),
            tok_cw=u.get("cache_creation_input_tokens"),
            models=d.get("models"), tools=d.get("tools"), counts=d.get("counts"),
            prompts_n=len(d.get("prompts") or []),
            first_ts=d.get("first_ts"), last_ts=d.get("last_ts"), agent=agent)
        if ins and is_new:
            zs.emit("session.seen", project=proj, ref=str(path), outcome="ok",
                    actor="zenith", agent=agent,
                    data={"project": proj, "first_ts": d.get("first_ts"),
                          "agent": agent})
        return ins
    except Exception:
        return False


def _transcript_detail(path, fmt):
    """Telemetry for one transcript: claude keeps the cached session_detail
    (with its summary); other formats parse via the registry + a minimal
    synthesized summary block so /api/session renders the same shape."""
    if fmt == "claude_jsonl":
        return session_detail(path)
    d = dict(za.parse_transcript(fmt, str(path)))
    st = path.stat()
    d["summary"] = {"path": str(path), "id": path.stem, "size": st.st_size,
                    "mtime": st.st_mtime, "title": None, "first_prompt": None,
                    "last_prompt": None, "cwd": None, "git_branch": None,
                    "model": next(iter(d.get("models") or {}), None),
                    "last_ts": d.get("last_ts"), "first_ts": d.get("first_ts"),
                    "lines": d.get("lines_parsed")}
    return d


def _sweep_candidates(sigs):
    """(mtime, path, agent_id, format) for changed transcripts across every
    enabled agent's manifest glob (D6). Claude keeps the original one-level
    TRANSCRIPTS_ROOT walk; '{cwd}' globs resolve per project dir; absolute
    globs go through glob.glob(recursive=True), capped at 4000 paths."""
    cand = []

    def add(f, aid, fmt):
        try:
            st = f.stat()
        except OSError:
            return
        if sigs.get(str(f)) != (st.st_mtime, st.st_size):
            cand.append((st.st_mtime, str(f), aid, fmt))

    for a in za.load_agents():
        if not a.get("enabled"):
            continue
        tr = a.get("transcript") or {}
        g, fmt = tr.get("glob") or "", tr.get("format") or "none"
        if not g or fmt == "none":
            continue
        if a.get("id") == "claude":
            if TRANSCRIPTS_ROOT.exists():
                for d in TRANSCRIPTS_ROOT.iterdir():
                    if d.is_dir():
                        for f in d.glob("*.jsonl"):
                            add(f, "claude", fmt)
        elif "{cwd}" in g:
            if PROJECTS_ROOT.exists():
                for p in PROJECTS_ROOT.iterdir():
                    if p.is_dir():
                        f = Path(g.replace("{cwd}", str(p)))
                        if f.is_file():
                            add(f, a["id"], fmt)
        else:
            for fp in glob.glob(os.path.expanduser(g), recursive=True)[:4000]:
                f = Path(fp)
                if f.is_file():
                    add(f, a["id"], fmt)
    return cand


def _telemetry_sweep():
    """Daemon (§3.1/P2 D6): every 120 s, gather changed transcripts across
    every enabled agent's glob, snapshot the ≤5 most-recently-modified.
    Per-format parsers are tolerant; the sweep never dies."""
    while True:
        try:
            sigs = zs.snapshot_sigs()                 # loaded once per tick
            cand = _sweep_candidates(sigs)
            cand.sort(reverse=True)                   # newest-mtime first
            for _, fp, aid, fmt in cand[:5]:          # budget: max 5 parses/tick
                p = Path(fp)
                try:
                    _snapshot_session(p, _transcript_detail(p, fmt), agent=aid)
                except Exception:
                    pass                              # next tick retries
        except Exception:
            pass                                      # the sweep never dies
        time.sleep(120)


def _agent_sessions(limit=40):
    """Session-list entries for non-Claude transcripts (P2 A7): one row per
    file from each enabled agent's manifest glob, newest first. Fields mirror
    session_summary's shape; unknowable ones are None (the UI renders '—')."""
    rows = []
    for a in za.load_agents():
        if not a.get("enabled") or a.get("id") == "claude":
            continue
        tr = a.get("transcript") or {}
        g, fmt = tr.get("glob") or "", tr.get("format") or "none"
        if not g or fmt == "none":
            continue
        files = []
        if "{cwd}" in g:
            if PROJECTS_ROOT.exists():
                for p in PROJECTS_ROOT.iterdir():
                    if p.is_dir():
                        f = Path(g.replace("{cwd}", str(p)))
                        if f.is_file():
                            files.append((f, str(p)))
        else:
            for fp in glob.glob(os.path.expanduser(g), recursive=True)[:2000]:
                f = Path(fp)
                if f.is_file():
                    files.append((f, ""))
        for f, proj in files:
            try:
                st = f.stat()
            except OSError:
                continue
            rows.append({"path": str(f), "id": f.stem, "agent": a["id"],
                         "size": st.st_size, "mtime": st.st_mtime,
                         "title": f"{a.get('label') or a['id']} · {f.name}",
                         "first_prompt": None, "last_prompt": None,
                         "cwd": proj or None, "git_branch": None, "model": None,
                         "last_ts": None, "first_ts": None, "lines": None,
                         "project": proj,
                         "project_name": Path(proj).name if proj else a["id"]})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]


def list_sessions(project=None, limit=80):
    encmap = project_encoding_map()
    dirs = []
    if project:
        enc = encode_path(project)
        for d in TRANSCRIPTS_ROOT.iterdir():
            if d.is_dir() and (d.name == enc or d.name.startswith(enc + "--worktrees")):
                dirs.append(d)
    else:
        dirs = [d for d in TRANSCRIPTS_ROOT.iterdir() if d.is_dir()]
    files = []
    for d in dirs:
        proj = match_project(d.name, encmap)
        for f in d.glob("*.jsonl"):
            files.append((f, proj or d.name))
    files.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)
    out = []
    for f, proj in files[:limit]:
        s = dict(session_summary(f))
        s["project"] = proj
        s["project_name"] = Path(proj).name if isinstance(proj, str) and proj.startswith("/") else proj
        s["agent"] = "claude"
        out.append(s)
    extra = [r for r in _agent_sessions(limit)          # P2 A7: cross-agent rows
             if not project or r.get("project") == project]
    merged = out + extra
    merged.sort(key=lambda s: s.get("mtime") or 0, reverse=True)
    merged = merged[:limit]
    names = _load_json(SESSION_NAMES_FILE, {})           # overlay user-renamed labels
    if isinstance(names, dict) and names:
        for s in merged:
            cn = names.get(s.get("path"))
            if cn:
                s["custom_name"] = cn
    return merged


# ---------------------------------------------------------------- projects

def list_projects():
    out = []
    if not PROJECTS_ROOT.exists():
        return out
    for p in sorted(PROJECTS_ROOT.iterdir()):
        if not p.is_dir() or p.name.startswith("."):
            continue
        enc = encode_path(p)
        tdir = TRANSCRIPTS_ROOT / enc
        n_sessions = total_bytes = 0
        last_ts = 0.0
        if tdir.exists():
            for f in tdir.glob("*.jsonl"):
                try:
                    fst = f.stat()
                except OSError:
                    continue
                n_sessions += 1
                total_bytes += fst.st_size
                last_ts = max(last_ts, fst.st_mtime)
        st = p.stat()
        out.append({
            "name": p.name, "path": str(p), "mtime": st.st_mtime,
            "git": (p / ".git").exists(),
            "index": (p / "docs" / "specs" / "00-PROJECT-INDEX.md").exists(),
            "master_prompt": (p / "masterPrompt.md").exists(),
            "todo": (p / "TODO.md").exists(),
            "readme": (p / "README.md").exists(),
            "sessions": n_sessions, "total_bytes": total_bytes,
            "last_ts": last_ts or None,
        })
    out.sort(key=lambda x: (x["sessions"] > 0, x["mtime"]), reverse=True)
    return out


def _git_info(path):
    p = Path(path)
    empty = {"branch": None, "dirty": False, "ahead": 0, "behind": 0,
             "commits": 0, "last_commit_ts": None}
    if not (p / ".git").exists():
        return empty

    def g(args):
        try:
            r = subprocess.run(["git", "-C", str(p)] + args,
                               capture_output=True, text=True, timeout=5)
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""
    info = dict(empty)
    info["branch"] = g(["rev-parse", "--abbrev-ref", "HEAD"]) or None
    info["dirty"] = bool(g(["status", "--porcelain"]))
    c = g(["rev-list", "--count", "HEAD"])
    info["commits"] = int(c) if c.isdigit() else 0
    ab = g(["rev-list", "--left-right", "--count", "@{u}...HEAD"])
    if ab and ("\t" in ab or " " in ab):
        parts = ab.replace("\t", " ").split()
        if len(parts) == 2:
            info["behind"] = int(parts[0]) if parts[0].isdigit() else 0
            info["ahead"] = int(parts[1]) if parts[1].isdigit() else 0
    info["last_commit_ts"] = g(["log", "-1", "--format=%cI"]) or None
    return info


def _file_counts(root, cap=6000):
    by_ext, total = {}, 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            if fn.startswith("."):
                continue
            ext = Path(fn).suffix.lstrip(".").lower() or "(none)"
            by_ext[ext] = by_ext.get(ext, 0) + 1
            total += 1
            if total >= cap:
                return {"by_ext": by_ext, "total": total, "truncated": True}
    return {"by_ext": by_ext, "total": total, "truncated": False}


def project_stats(path):
    """Rich per-project stats. Cached by (project mtime, transcript-dir mtime);
    deep transcript scan is bounded for performance."""
    p = Path(path).resolve()
    if not (p == PROJECTS_ROOT or p.parent == PROJECTS_ROOT):
        raise PermissionError("path outside claudeProjects")
    if not p.is_dir():
        raise ValueError("no such project")
    enc = encode_path(p)
    tdirs = []
    if TRANSCRIPTS_ROOT.exists():
        for d in TRANSCRIPTS_ROOT.iterdir():
            if d.is_dir() and (d.name == enc or d.name.startswith(enc + "--worktrees")):
                tdirs.append(d)
    sig = (p.stat().st_mtime, tuple(sorted((str(d), d.stat().st_mtime) for d in tdirs)))
    with _cache_lock:
        hit = _projstats_cache.get(str(p))
        if hit and hit[0] == sig:
            return hit[1]
    tfiles = []
    for d in tdirs:
        tfiles += list(d.glob("*.jsonl"))
    tfiles.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    now = time.time()
    total_bytes = 0
    first_mt = last_mt = None
    days = {}
    for f in tfiles:
        try:
            st = f.stat()
        except OSError:
            continue
        total_bytes += st.st_size
        first_mt = st.st_mtime if first_mt is None else min(first_mt, st.st_mtime)
        last_mt = st.st_mtime if last_mt is None else max(last_mt, st.st_mtime)
        day = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
        days[day] = days.get(day, 0) + 1
    models, tools = {}, {}
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    total_lines = 0
    for f in tfiles[:25]:                         # bounded deep scan (cached)
        d = session_detail(f)
        for m, c in (d.get("models") or {}).items():
            models[m] = models.get(m, 0) + c
        for t, c in (d.get("tools") or {}).items():
            tools[t] = tools.get(t, 0) + c
        u = d.get("usage") or {}
        tokens["input"] += u.get("input_tokens", 0)
        tokens["output"] += u.get("output_tokens", 0)
        tokens["cache_read"] += u.get("cache_read_input_tokens", 0)
        tokens["cache_creation"] += u.get("cache_creation_input_tokens", 0)
        total_lines += d.get("lines_parsed", 0)
    activity_by_day = []
    for i in range(13, -1, -1):
        day = datetime.fromtimestamp(now - i * 86400).strftime("%Y-%m-%d")
        activity_by_day.append({"day": day, "count": days.get(day, 0)})
    tools_top = sorted(({"name": k, "n": v} for k, v in tools.items()),
                       key=lambda x: -x["n"])[:12]
    result = {
        "name": p.name, "path": str(p), "sessions": len(tfiles),
        "total_bytes": total_bytes, "total_lines": total_lines,
        "first_activity": datetime.fromtimestamp(first_mt).isoformat() if first_mt else None,
        "last_activity": datetime.fromtimestamp(last_mt).isoformat() if last_mt else None,
        "models": models, "tools_top": tools_top, "tokens": tokens,
        "git": _git_info(p), "files": _file_counts(p),
        "docs": {"index": (p / "docs" / "specs" / "00-PROJECT-INDEX.md").exists(),
                 "readme": (p / "README.md").exists(),
                 "master_prompt": (p / "masterPrompt.md").exists(),
                 "todo": (p / "TODO.md").exists()},
        "activity_by_day": activity_by_day,
        "deep_scanned": min(len(tfiles), 25),
    }
    with _cache_lock:
        _projstats_cache[str(p)] = (sig, result)
    return result


def docs_tree(root, max_depth=4, max_files=400):
    root = Path(root).resolve()
    if not any(root == r or r in root.parents for r in [PROJECTS_ROOT]):
        raise PermissionError("root outside claudeProjects")
    out = []

    def walk(d, depth):
        if depth > max_depth or len(out) >= max_files:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except OSError:
            return
        for e in entries:
            if len(out) >= max_files:
                return
            if e.is_dir():
                if e.name not in SKIP_DIRS and not e.name.startswith("."):
                    walk(e, depth + 1)
            elif e.suffix.lower() in (".md", ".markdown", ".txt"):
                st = e.stat()
                out.append({"path": str(e), "rel": str(e.relative_to(root)),
                            "size": st.st_size, "mtime": st.st_mtime})
    walk(root, 0)
    return out


def read_file_checked(path):
    p = Path(path).resolve()
    if not any(p == r or r in p.parents for r in READ_ROOTS):
        raise PermissionError("path outside allowed roots")
    if p.stat().st_size > 2_000_000:
        raise ValueError("file too large")
    return p.read_text(errors="replace")


# ---------------------------------------------------------------- NexusMind
# ONE DOOR: every memory read and write goes through NexusMind's HTTP API via
# nm_api() (bearer token, 5s bound, never throws). ZENITH does NOT talk to the
# `np` schema directly and must not grow a second path back to it, because a
# direct-DB reader is worse on every axis that matters:
#   * retrieval — the API's /api/search is hybrid FTS+semantic fused with RRF
#     (nDCG@10 0.333); the SQL it replaced was `title ILIKE %q% OR content ILIKE
#     %q%`, a substring scan that looked like an optimisation while being ~44%
#     worse at finding things;
#   * safety — require_principal, row-level ownership, soft-forget/retracted
#     exclusions and pin-boost scoring all live in NexusMind's application
#     layer; raw SQL sees rows the API would correctly withhold;
#   * coupling — `np.memories` is NexusPrime's private shape, and a migration
#     there would break ZENITH silently.
# It is also what makes the module work at all here: ZENITH is stdlib-only, this
# interpreter has no psycopg2 and the host has no psql, so the SQL path could
# never reach the live store and every memory route answered available:false.
#
# GET /api/memories returns the WHOLE corpus as metadata rows (key/title/tags/
# namespace/created_at/updated_at — no content) ordered by updated_at DESC in
# ~80ms, so one short-TTL cached fetch feeds browse/meta/graph/timeline and the
# related-list in detail, and every legacy response shape is preserved exactly.

NM_BACKEND = "rest"               # the `backend` value every NM payload reports
NM_LIST_TTL = 30.0                # corpus-list cache; capture invalidates it
_nm_list_cache = {"at": 0.0, "rows": None}
_nm_list_lock = threading.Lock()
_nm_rest_cache = {"checked": 0.0, "ok": False, "detail": "not probed"}
_nm_rest_lock = threading.Lock()


def _nm_err(r, fallback="nexusmind unreachable"):
    """A displayable one-line error from an nm_api() result. The bearer token is
    scrubbed unconditionally: this string reaches the browser and the logs."""
    msg = fallback
    if isinstance(r, dict):
        msg = str(r.get("error") or fallback)
        if r.get("detail"):
            msg += ": " + str(r["detail"])
    tok = _nm_api_token()
    if tok:
        msg = msg.replace(tok, "***")
    return msg[:200]


def _nm_down():
    """Why the reachability gate refused, for the routes that surface it — "HTTP
    401: authentication required" or "connection refused" beats a flat
    "unreachable" when someone is trying to fix their config. Cached, so this
    costs nothing; already token-scrubbed by _nm_rest_ok."""
    return _nm_rest_ok()[1] or "nexusmind unreachable"


def _nm_rest_ok():
    """(reachable, detail) for the NexusMind API, cached ~30s in both directions
    so a down/slow NM costs one bounded call per half-minute, never a hang."""
    with _nm_rest_lock:
        now = time.time()
        if now - _nm_rest_cache["checked"] < 30:
            return _nm_rest_cache["ok"], _nm_rest_cache["detail"]
        # 2.5s bound — shorter than the data paths' 5s, so a cold /api/capabilities
        # still fits inside CAPS_JOIN (§ capabilities) when NexusMind is down.
        r = nm_api("/api/namespaces", timeout=2.5)      # cheapest authed read
        ok = isinstance(r, list)
        detail = ("api reachable, %d namespaces" % len(r)) if ok else _nm_err(r)
        _nm_rest_cache.update(checked=now, ok=ok, detail=detail)
        return ok, detail


def _nm_list(namespace=None):
    """Every memory as a metadata row, updated_at DESC — or None when NexusMind
    is unreachable (deliberately distinct from an empty store, so callers can
    report available:false instead of "no memories")."""
    with _nm_list_lock:
        now = time.time()
        rows = _nm_list_cache["rows"]
        if rows is None or now - _nm_list_cache["at"] >= NM_LIST_TTL:
            r = nm_api("/api/memories")
            if not isinstance(r, list):
                return None
            rows = [x for x in r if isinstance(x, dict)]
            _nm_list_cache.update(at=now, rows=rows)
    return [r for r in rows if r.get("namespace") == namespace] if namespace else rows


def _nm_mem(r):
    """One API row -> the memory dict shape MemoryApp has always consumed. The
    list endpoint carries no content; the search endpoint does."""
    return {"key": r.get("key"), "title": r.get("title"),
            "content": (r.get("content") or "")[:1200],
            "tags": _parse_tags(r.get("tags")), "namespace": r.get("namespace"),
            "created_at": r.get("created_at"), "updated_at": r.get("updated_at")}


def _parse_tags(v):
    if isinstance(v, list):
        return v
    if not v:
        return []
    s = str(v).strip()
    try:
        j = json.loads(s)
        if isinstance(j, list):
            return j
    except (json.JSONDecodeError, ValueError):
        pass
    if s.startswith("{") and s.endswith("}"):        # postgres text[] literal
        return [t.strip().strip('"') for t in s[1:-1].split(",") if t.strip()]
    return [t for t in re.split(r"[,\s]+", s) if t]


def _nm_reachable():
    """True iff the memory module may talk to NexusMind: the integration is not
    switched off AND the API actually answers. `off` short-circuits before any
    socket (§3.2); an unreachable NM reports available:false, never an empty UI."""
    if _int_off("nexusmind_api"):             # off → no socket at all (§3.2)
        return False
    return _nm_rest_ok()[0]


def nm_memories(q=None, namespace=None, limit=60, tag=None):
    if not _nm_reachable():
        return {"available": False, "memories": [], "error": _nm_down()}
    limit = max(1, min(int(limit or 60), 300))
    if tag:
        # `show todos` / `list <tag>`: NM's own tag filter, which knows how tags are
        # stored per backend. Checked before `q` because the two are never combined.
        r = nm_api("/api/memories?tag=" + urllib.parse.quote(str(tag), safe=""))
        if not isinstance(r, list):
            return {"available": False, "memories": [], "error": _nm_err(r)}
        return {"available": True, "backend": NM_BACKEND,
                "memories": [_nm_mem(x) for x in r[:limit] if isinstance(x, dict)]}
    if q:
        # POST /api/search honours `limit` (GET /api/memories?q= silently caps at
        # 22) and returns content alongside the metadata.
        r = nm_api("/api/search", "POST", {"query": q, "namespace": namespace or None,
                                           "limit": min(limit, 100)})
        if not isinstance(r, dict) or not isinstance(r.get("results"), list):
            return {"available": False, "memories": [], "error": _nm_err(r)}
        rows = r["results"]
    else:
        rows = _nm_list(namespace)
        if rows is None:
            return {"available": False, "memories": [],
                    "error": _nm_err(None, "memory list unavailable")}
        rows = rows[:limit]
    return {"available": True, "backend": NM_BACKEND,
            "memories": [_nm_mem(r) for r in rows]}


def nm_meta():
    if not _nm_reachable():
        return {"available": False, "total": 0, "namespaces": [], "tags": []}
    rows = _nm_list()
    if rows is None:
        return {"available": False, "total": 0, "namespaces": [], "tags": []}
    ns_counts, tag_counts = {}, {}
    for r in rows:
        ns = r.get("namespace")
        ns_counts[ns] = ns_counts.get(ns, 0) + 1
        for t in _parse_tags(r.get("tags")):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    top_ns = sorted(ns_counts.items(), key=lambda x: -x[1])
    top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:24]
    return {"available": True, "backend": NM_BACKEND, "total": len(rows),
            "namespaces": [{"name": n, "count": c} for n, c in top_ns],
            "tags": [{"name": t, "count": c} for t, c in top_tags]}


def nm_schedules():
    """NexusMind's scheduler registry (LoopsApp's NM tab). Same door: GET
    /api/schedules returns exactly the row shape the old `SELECT * FROM
    np.schedules` produced, so the response contract is unchanged."""
    if not _nm_reachable():
        return {"available": False, "schedules": []}
    r = nm_api("/api/schedules")
    if not isinstance(r, list):
        return {"available": False, "error": _nm_err(r), "schedules": []}
    rows = sorted([s for s in r if isinstance(s, dict)],
                  key=lambda s: (not s.get("enabled"), str(s.get("name") or "")))
    return {"available": True, "backend": NM_BACKEND, "schedules": rows}


def nm_source():
    rows = _nm_list() if _nm_reachable() else None
    if rows is None:
        return {"backend": "none", "memories_total": 0}
    return {"backend": NM_BACKEND, "memories_total": len(rows)}


def nm_graph(namespace=None, cap=150):
    rows = _nm_list(namespace) if _nm_reachable() else None
    if rows is None:
        return {"backend": "none"}
    rows = rows[:max(1, min(int(cap or 150), 150))]      # already updated_at DESC
    nodes = [{"key": r.get("key"), "title": r.get("title"),
              "namespace": r.get("namespace"), "tags": _parse_tags(r.get("tags"))}
             for r in rows]
    edges = []
    for i in range(len(nodes)):
        ti = set(nodes[i]["tags"])
        if not ti:
            continue
        for j in range(i + 1, len(nodes)):
            shared = ti & set(nodes[j]["tags"])
            if shared:
                edges.append({"source": nodes[i]["key"], "target": nodes[j]["key"],
                              "weight": len(shared), "shared": sorted(shared)})
    edges.sort(key=lambda e: -e["weight"])
    return {"backend": NM_BACKEND, "nodes": nodes, "edges": edges[:800]}


def nm_timeline(limit=200):
    limit = max(1, min(int(limit or 200), 500))
    rows = _nm_list() if _nm_reachable() else None
    if rows is None:
        return {"backend": "none", "items": []}
    # the corpus arrives updated_at DESC; the timeline is keyed on created_at
    rows = sorted(rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return {"backend": NM_BACKEND,
            "items": [{"key": r.get("key"), "title": r.get("title"),
                       "namespace": r.get("namespace"),
                       "created_at": r.get("created_at")} for r in rows[:limit]]}


def file_memories():
    out = []
    if not TRANSCRIPTS_ROOT.exists():
        return out
    encmap = project_encoding_map()
    for d in TRANSCRIPTS_ROOT.iterdir():
        mem = d / "memory"
        if not mem.is_dir():
            continue
        proj = match_project(d.name, encmap) or d.name
        files = []
        for f in sorted(mem.glob("*.md")):
            st = f.stat()
            files.append({"name": f.name, "path": str(f), "size": st.st_size,
                          "mtime": st.st_mtime})
        if files:
            out.append({"project": Path(proj).name if str(proj).startswith("/") else proj,
                        "dir": str(mem), "files": files})
    out.sort(key=lambda g: -max(f["mtime"] for f in g["files"]))
    return out


# ---------------------------------------------------------------- agents/skills

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def parse_frontmatter(text):
    m = FRONTMATTER_RE.match(text)
    meta = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t", "#")):
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
    return meta


def list_agents():
    out = []
    if AGENTS_DIR.exists():
        for f in sorted(AGENTS_DIR.glob("*.md")):
            meta = parse_frontmatter(f.read_text(errors="replace"))
            out.append({"file": str(f), "name": meta.get("name", f.stem),
                        "description": meta.get("description", ""),
                        "tools": meta.get("tools", ""), "model": meta.get("model", ""),
                        "color": meta.get("color", "")})
    return out


def list_skills():
    global _skills_cache
    if _skills_cache is not None:
        return _skills_cache
    out = []
    cache = PLUGINS_DIR / "cache"
    if cache.exists():
        for skill_md in cache.glob("*/*/*/skills/*/SKILL.md"):
            try:
                meta = parse_frontmatter(skill_md.read_text(errors="replace"))
            except OSError:
                continue
            parts = skill_md.parts
            plugin = parts[parts.index("cache") + 2]
            out.append({"name": meta.get("name", skill_md.parent.name),
                        "plugin": plugin,
                        "description": (meta.get("description", "") or "")[:300]})
    out.sort(key=lambda s: (s["plugin"], s["name"]))
    _skills_cache = out
    return out


def list_user_skills():
    """Editable user skills under ~/.claude/skills/<name>/SKILL.md."""
    out = []
    if USER_SKILLS_DIR.exists():
        for d in sorted(USER_SKILLS_DIR.iterdir()):
            sk = d / "SKILL.md"
            if not sk.is_file():
                continue
            try:
                meta = parse_frontmatter(sk.read_text(errors="replace"))
            except OSError:
                continue
            out.append({"name": meta.get("name", d.name), "path": str(sk),
                        "description": (meta.get("description", "") or "")[:300]})
    return out


def _validate_frontmatter(text):
    meta = parse_frontmatter(text)
    if not meta.get("name") or not meta.get("description"):
        raise ValueError("frontmatter must include both name: and description:")
    return meta


def _backup_file(path):
    if path.exists():
        try:
            shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
        except OSError:
            pass


def skill_save(name, content):
    """Write ~/.claude/skills/<slug>/SKILL.md (backup existing, validate frontmatter)."""
    _validate_frontmatter(content)
    slug = _slug(name)
    d = USER_SKILLS_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    dest = d / "SKILL.md"
    _backup_file(dest)
    dest.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(dest), "name": slug}


def skill_delete(name):
    slug = _slug(name)
    d = (USER_SKILLS_DIR / slug).resolve()
    if d.parent != USER_SKILLS_DIR.resolve() or not d.is_dir():
        raise ValueError("no such user skill")
    shutil.rmtree(d)
    return {"ok": True, "name": slug}


def agent_save(name, content):
    """Write ~/.claude/agents/<slug>.md (backup existing, validate frontmatter)."""
    _validate_frontmatter(content)
    slug = _slug(name)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = AGENTS_DIR / (slug + ".md")
    _backup_file(dest)
    dest.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(dest), "name": slug}


def agent_delete(name):
    slug = _slug(name)
    dest = (AGENTS_DIR / (slug + ".md")).resolve()
    if dest.parent != AGENTS_DIR.resolve() or not dest.is_file():
        raise ValueError("no such agent")
    dest.unlink()
    return {"ok": True, "name": slug}


def skill_apply(src):
    """Install a proposal (data/research/proposals/*) as a user skill, agent, or loop."""
    p = Path(src).resolve()
    if PROPOSALS_DIR.resolve() not in p.parents:
        raise PermissionError("source is not a proposal file")
    if not p.is_file():
        raise ValueError("no such proposal")
    if p.suffix == ".json":                       # loop proposal
        obj = json.loads(p.read_text())
        obj.pop("id", None)
        saved = loop_upsert(obj)
        return {"applied": "loop", "id": saved["id"]}
    text = p.read_text(errors="replace")
    meta = _validate_frontmatter(text)
    name = _slug(meta["name"])
    if p.name.upper().endswith("-SKILL.MD") or "skill" in (meta.get("kind", "").lower()):
        d = USER_SKILLS_DIR / name
        d.mkdir(parents=True, exist_ok=True)
        dest = d / "SKILL.md"
        _backup_file(dest)
        shutil.copyfile(p, dest)
        return {"applied": "skill", "dest": str(dest), "name": name}
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = AGENTS_DIR / (name + ".md")
    _backup_file(dest)
    shutil.copyfile(p, dest)
    return {"applied": "agent", "dest": str(dest), "name": name}


# ---------------------------------------------------------------- coordination

def coordination_state():
    projects = []
    broadcasts = []
    if COORD_ROOT.exists():
        for d in COORD_ROOT.iterdir():
            if not d.is_dir():
                continue
            sessions = []
            for sf in (d / "sessions").glob("*.json"):
                try:
                    s = json.loads(sf.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                s["last_activity"] = sf.stat().st_mtime
                s["stale"] = (time.time() - sf.stat().st_mtime) > STALE_SECS
                sessions.append(s)
            for bf in sorted((d / "broadcasts").glob("*.json"), reverse=True)[:20]:
                try:
                    b = json.loads(bf.read_text())
                    b["project_dir"] = d.name
                    b["mtime"] = bf.stat().st_mtime
                    broadcasts.append(b)
                except (json.JSONDecodeError, OSError):
                    continue
            if sessions:
                projects.append({"dir": d.name, "sessions": sessions})
    messages = []
    if COORD_ROOT.exists():
        for d in COORD_ROOT.iterdir():
            if not d.is_dir():
                continue
            for mf in (d / "messages").glob("*.json"):
                try:
                    m = json.loads(mf.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                m["project_dir"] = d.name
                m["mtime"] = mf.stat().st_mtime
                messages.append(m)
    messages.sort(key=lambda m: -m.get("mtime", 0))
    broadcasts.sort(key=lambda b: -b.get("mtime", 0))
    return {"projects": projects, "broadcasts": broadcasts[:40],
            "messages": messages[:10]}


# ---------------------------------------------------------------- jobs

def _cfg_evt(area, action, ref="", name=""):
    """One-line config.change emitter for mutating POST routes (§3 #7)."""
    zs.emit("config.change", ref=str(ref or ""), outcome="ok", actor="user",
            data={"area": area, "action": action, "name": str(name or "")})


def _record_saved_run(saved_id, job):
    """Bump run stats on a saved job when its run finishes."""
    with _data_lock:
        jobs = _load_json(SAVED_JOBS_FILE, [])
        for sj in jobs:
            if sj.get("id") == saved_id:
                sj["run_count"] = (sj.get("run_count") or 0) + 1
                sj["last_run"] = job.get("ended") or now_iso()
                sj["last_status"] = job.get("status")
                break
        _save_json(SAVED_JOBS_FILE, jobs)


def _parse_envelope(text):
    """Thin wrapper since P2 — the tolerant Claude-envelope logic moved to
    zenith_agents.claude_envelope (COST_PARSERS['claude_json_array'] uses it).
    Kept for the self-check + any external callers. str -> dict | None."""
    return za.claude_envelope(text)


def _job_cmd(job):
    """argv for a job via its resolved adapter (za.build_argv). Still the ONE
    argv-assembly seam probe H2 asserts on — now job-dict-shaped (P2)."""
    adapter = job.get("_adapter") or za.resolve_agent(job.get("agent") or "claude")
    if adapter is None:
        raise ValueError(f"agent unavailable: {job.get('agent')!r}")
    return za.build_argv(adapter, job)


def _job_spawn_payload(job, budget, saved_id, skills, add_dir):
    return {"model": job.get("model"),
            "agent": job.get("agent") or "claude",
            "mode": job.get("mode"),
            "effort": job.get("effort"), "budget": budget,
            "label": job.get("label"), "loop_id": job.get("loop_id"),
            "saved_id": saved_id, "verify_id": job.get("verify_id"),
            "ab_id": job.get("ab_id"),
            "skills": list(skills or []),
            "prompt": (job.get("prompt") or "")[:2000],
            "add_dir": str(add_dir) if add_dir else None}


def _job_outcome(job):
    """job.end outcome column: ok | error | killed (orphaned is synthesized
    by the boot sweep, never here)."""
    if job.get("status") == "stopped":
        return "killed"
    return "ok" if job.get("rc") == 0 else "error"


def _tok_tuple(job):
    """job['usage'] -> the emit(tokens=...) 4-tuple, or None (NULL columns)."""
    u = job.get("usage") or {}
    if not u:
        return None
    return (u.get("in"), u.get("out"), u.get("cache_r"), u.get("cache_w"))


def _job_actor(job):
    if job.get("verify_id"):
        return "verify"
    if job.get("loop_id"):
        return "loop:" + str(job["loop_id"])
    if job.get("ab_id"):
        return "ab:" + str(job["ab_id"])
    return "user"


def _job_end_payload(job, env):
    """The §2.3 job.end data payload (denormalized run summary)."""
    env = env or {}
    try:
        dur = round(_iso_epoch(job.get("ended")) - _iso_epoch(job.get("started")), 1)
    except (TypeError, ValueError):
        dur = None
    return {"model": job.get("model"), "mode": job.get("mode"),
            "agent": job.get("agent") or "claude",
            "label": job.get("label"), "loop_id": job.get("loop_id"),
            "verify_id": job.get("verify_id"), "ab_id": job.get("ab_id"),
            "rc": job.get("rc"),
            "status": job.get("status"), "duration_s": dur,
            "started": job.get("started"),
            "cli_session_id": job.get("cli_session_id"),
            "subtype": env.get("subtype"),
            # num_turns is claude's field; prime-agent counts the same thing under
            # its own name, so the turns column populates for both instead of
            # going blank on the agent that does report it.
            "num_turns": env.get("num_turns") or env.get("messages"),
            "permission_denials": env.get("permission_denials"),
            "tools": env.get("tools"),      # tool executions (prime_agent_jsonl)
            "result": (job.get("result") or "")[:16000],
            "output_tail": "\n".join(job.get("output") or [])[-4000:],
            "envelope_ok": bool(env) and not job.get("_stdout_truncated"),
            "model_usage": env.get("modelUsage"),
            # provider runs only (None elsewhere): what was packed and shipped,
            # so loop history and the mission log can show it after the fact
            "kind": "provider" if job.get("agent") == "provider" else None,
            "provider": job.get("provider"), "task": job.get("task"),
            "context": job.get("context"),
            "context_label": job.get("context_label"),
            "context_chars": job.get("context_chars")}


def _job_finished(job):
    """Single completion hook (§4): adapter cost-parse → job fields → job.end
    event → loop/saved/verify hooks. Claude keeps exact P1 semantics via
    COST_PARSERS['claude_json_array'] (the moved _parse_envelope). Called from
    the reader tail AND the OSError spawn-failure path."""
    raw = job.pop("_stdout", "")
    adapter = job.pop("_adapter", None)
    lmf = job.pop("_last_msg_file", None)
    fmt = str(((adapter or {}).get("cost_format")) or "none")
    if adapter and fmt not in za.COST_PARSERS:
        job["output"].append(f"[cost parser {fmt!r} unknown — usage not booked]")
    p = za.extract_usage(adapter, raw, "\n".join(job.get("output") or [])[-20000:],
                         {"last_msg_file": lmf})
    if lmf:
        try:
            os.unlink(lmf)
        except OSError:
            pass
    env = p.get("raw") if isinstance(p, dict) else None
    if p:
        job["result"] = p.get("result") or ""
        job["output"].append("")                 # result text into the console,
        job["output"].extend(job["result"].splitlines() or ["(empty result)"])
        u = p.get("usage") or {}
        job["usage"] = {"in": u.get("in"), "out": u.get("out"),
                        "cache_r": u.get("cache_r"), "cache_w": u.get("cache_w")}
        job["cost_usd"] = p.get("cost_usd")
        job["cli_session_id"] = p.get("session_id")
    elif raw.strip():
        # Fallback (§4): killed jobs, stream interrupts, CLI drift, agents
        # whose parser found nothing — raw stdout lands in the console
        # verbatim so nothing is lost; usage stays NULL.
        job["output"].extend(raw.splitlines())
    if len(job["output"]) > 5000:
        del job["output"][:len(job["output"]) - 5000]   # same ring cap as live
    zs.emit("job.end", project=job.get("project") or "", ref=job["id"],
            outcome=_job_outcome(job), tokens=_tok_tuple(job),
            cost=job.get("cost_usd"), actor=_job_actor(job),
            agent=job.get("agent") or "claude",
            data=_job_end_payload(job, env))
    if job.get("loop_id"):
        _record_loop_run(job["loop_id"], job["id"], job)
    if job.get("saved_id"):
        _record_saved_run(job["saved_id"], job)
    if job.get("verify_id"):
        _record_verify_verdict(job)
    _ab_on_job_end(job)             # P3: A/B cohort hook (no-op without ab_id)
    _maybe_autoverify(job)          # §5.7 tail hook


def spawn_job(project, prompt, model="sonnet", mode="default", budget=None,
              effort=None, skills=None, loop_id=None, label=None, saved_id=None,
              extra_env=None, add_dir=None, verify_id=None, agent="claude",
              ab_id=None):
    job_id = uuid.uuid4().hex[:12]
    if skills:
        prompt = ("First invoke these skills via the Skill tool: "
                  + ", ".join(skills) + ".\n\n" + prompt)
    agent = str(agent or "claude")
    adapter = za.resolve_agent(agent)
    job = {"id": job_id, "project": project, "prompt": prompt, "model": model,
           "mode": mode, "status": "running", "output": [], "rc": None,
           "started": now_iso(), "ended": None, "effort": effort,
           "loop_id": loop_id, "label": label, "saved_id": saved_id,
           "verify_id": verify_id, "ab_id": ab_id, "agent": agent,
           "budget": budget, "add_dir": str(add_dir) if add_dir else None}
    job["_adapter"] = adapter          # resolved once; underscore → not public
    with _jobs_lock:
        JOBS[job_id] = job
    if adapter is None:                # unknown/disabled agent (programmatic path;
        job["status"], job["rc"] = "error", -1        # /api/jobs 400s earlier)
        job["output"].append(f"agent '{agent}' is unknown or disabled")
        job["ended"] = now_iso()
        zs.emit("job.spawn", project=project, ref=job_id, outcome="error",
                actor=_job_actor(job), agent=job["agent"],
                data=_job_spawn_payload(job, budget, saved_id, skills, add_dir))
        _job_finished(job)
        return job_id
    if za.wants_last_msg_file(adapter):     # D2: codex --output-last-message
        fd, lmf = tempfile.mkstemp(prefix="zenith-lastmsg-", suffix=".txt")
        os.close(fd)
        job["_last_msg_file"] = lmf
    cmd = _job_cmd(job)
    env = _augment_path(dict(os.environ))
    for k, v in (adapter.get("env") or {}).items():
        env.setdefault(str(k), str(v))      # manifest env; caller env wins
    if effort in EFFORT_TOKENS:
        env["MAX_THINKING_TOKENS"] = str(EFFORT_TOKENS[effort])
    if isinstance(extra_env, dict):
        for k, v in extra_env.items():
            env[str(k)] = str(v)
    try:
        # §4: stderr keeps the live console; stdout carries the machine
        # envelope (claude/codex) or the buffered human transcript (aider).
        # stdin=DEVNULL: no agent may ever block on a TTY prompt (D1).
        proc = subprocess.Popen(cmd, cwd=project, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env,
                                errors="replace", stdin=subprocess.DEVNULL)
    except OSError as e:
        job["status"], job["rc"] = "error", -1
        job["output"].append(f"failed to spawn {agent}: {e}")
        job["ended"] = now_iso()
        zs.emit("job.spawn", project=project, ref=job_id, outcome="error",
                actor=_job_actor(job), agent=job["agent"],
                data=_job_spawn_payload(job, budget, saved_id, skills, add_dir))
        _job_finished(job)
        return job_id
    job["_proc"] = proc

    def err_reader():        # live progress/warnings → console, as before
        for line in proc.stderr:
            job["output"].append(line.rstrip("\n"))
            if len(job["output"]) > 5000:
                del job["output"][:1000]

    def reader():            # stdout → private envelope buffer (cap 2 MB)
        t_err = threading.Thread(target=err_reader, daemon=True)
        t_err.start()
        head, tail, total, tsz = [], [], 0, 0
        for line in proc.stdout:
            total += len(line)
            if total <= 2_000_000:
                head.append(line)
            else:            # beyond 2 MB: keep head 200 KB + rolling tail 200 KB
                tail.append(line)
                tsz += len(line)
                while tsz > 200_000 and len(tail) > 1:
                    tsz -= len(tail[0])
                    tail.pop(0)
        proc.wait()
        t_err.join(timeout=5)
        job["rc"] = proc.returncode
        if job["status"] != "stopped":       # /api/job/stop already labeled it
            job["status"] = "done" if proc.returncode == 0 else "error"
        job["ended"] = now_iso()
        if tail:
            job["_stdout_truncated"] = True
            job["_stdout"] = ("".join(head)[:200_000]
                              + "\n…stdout truncated…\n" + "".join(tail))
        else:
            job["_stdout"] = "".join(head)
        _job_finished(job)

    threading.Thread(target=reader, daemon=True).start()
    zs.emit("job.spawn", project=project, ref=job_id, outcome="ok",
            actor=_job_actor(job), agent=job["agent"],
            data=_job_spawn_payload(job, budget, saved_id, skills, add_dir))
    return job_id


def _record_loop_run(loop_id, job_id, job):
    """Emit-only since P1: loop_runs.jsonl is retired (§2.5); the loop.run
    event is the record and /api/loops2/runs reads job.end events."""
    zs.emit("loop.run", project=job.get("project") or "", ref=str(loop_id),
            outcome="ok" if job.get("rc") == 0 else "error",
            actor="loop:" + str(loop_id),
            agent=job.get("agent") or "claude",
            data={"job_id": job_id, "name": job.get("label")})


def job_public(job, offset=0):
    return {k: v for k, v in job.items() if not k.startswith("_")} | {
        "output": job["output"][offset:], "offset": len(job["output"])}


# ---------------------------------------------------------------- verify runner (D2)
# Detection separated from repair: the verifier enumerates issues with
# evidence; it cannot fix — mode=default denies Bash/Write/Edit in headless -p.

VERIFY_MODE = "default"      # load-bearing (§5.3); probe H3 asserts on it
VERIFY_LADDER = _MODELS["verify_ladder"]   # ringer ≥ one tier above the worker
                                           # (ships in defaults/models.json)
REVIEWER_AGENT = os.environ.get("ZENITH_REVIEWER_AGENT", "claude")
# D5/OQ3: the ringer ALWAYS runs on this fixed strong agent, regardless of
# the target job's agent — a weak target-agent must never judge itself.


def _model_tier(model):
    """Normalize an alias or full model id to a MODEL_MAP tier key by
    substring ('claude-opus-4-8' → 'opus'); None when unknown."""
    m = str(model or "").lower()
    for tier in MODEL_MAP:
        if tier in m:
            return tier
    return None


VERIFY_PROMPT = """You are a hostile reviewer — a ringer brought in to find what is wrong. Your ONLY job is to
enumerate problems. You do not fix anything, you do not write files, you do not run commands,
you do not praise. Assume the work is defective until the material proves otherwise.

TARGET ({kind}): {target}
STATED INTENT (what the work was supposed to accomplish):
{intent}

MECHANICAL EVIDENCE — gathered by the OS, trusted; the agent under review did NOT produce this:
{mech}

MATERIAL UNDER REVIEW:
{material}
{extra}
Rules:
- Enumerate EVERY defensible issue. Severities: crit = wrong result / data loss / security /
  violated guardrail; major = likely bug or unmet requirement; minor = edge case, sloppiness,
  drift; info = observation worth recording.
- Every issue MUST carry evidence: a verbatim quote from the material or mechanical evidence,
  and a location (file:line, diff hunk header, or output line). No citation → do not file it.
- The worker's own claims of success are NOT evidence. Only the material and the mechanical
  evidence count.
- If the material is truncated or insufficient to judge part of the intent, file that as an
  issue: severity info, claim "insufficient evidence to verify <X>".
- You may Read files in the project for context. You cannot and must not modify anything.

Output ONLY this JSON object — no prose before or after, no code fences:
{{"issues":[{{"severity":"crit|major|minor|info","claim":"<one sentence>",
  "evidence":"<verbatim quote>","location":"<file:line | hunk | output line>"}}],
  "summary":"<2-3 sentences: overall state of the work>"}}
"""


def _cap_text(s, cap=60000):
    """Head-and-tail truncation (§5.2). -> (text, truncated?)"""
    s = s or ""
    if len(s) <= cap:
        return s, False
    half = cap // 2
    return (s[:half] + "\n\n…[TRUNCATED — head and tail shown]…\n\n" + s[-half:],
            True)


def _git(args, cwd, timeout=10):
    """subprocess git with a 10 s timeout; failure → ValueError (route → 400
    with the stderr, per §5.2)."""
    r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        raise ValueError(f"git {' '.join(args)}: {(r.stderr or '').strip()[:400]}")
    return r.stdout


def _verify_material_job(body):
    """kind=job: live console if the job is in JOBS, else the recorded
    job.end event (result + output_tail). Mech is Zenith's own record."""
    jid = str(body.get("job_id") or "")
    job = JOBS.get(jid)
    if job:
        project = job.get("project") or ""
        intent = body.get("intent") or (job.get("prompt") or "")[:2000]
        material = "\n".join(job.get("output") or [])
        try:
            dur = round(_iso_epoch(job.get("ended")) - _iso_epoch(job.get("started")), 1) \
                if job.get("ended") else None
        except (TypeError, ValueError):
            dur = None
        mech = {"rc": job.get("rc"), "status": job.get("status"),
                "duration_s": dur, "model": job.get("model"),
                "mode": job.get("mode"), "subtype": None,
                "cost_usd": job.get("cost_usd"),
                "permission_denials": None,
                "envelope_ok": job.get("cost_usd") is not None,
                "source": "live"}
    else:
        evs = zs.events_query(kind="job.end", ref=jid, limit=1)
        if not evs:
            raise KeyError("no such job (live or recorded)")
        d = evs[0].get("data") or {}
        project = evs[0].get("project") or ""
        spawn = zs.events_query(kind="job.spawn", ref=jid, limit=1)
        intent = (body.get("intent")
                  or ((spawn[0].get("data") or {}).get("prompt") if spawn else "")
                  or "")
        material = ((d.get("result") or "")
                    + "\n\n--- output tail ---\n" + (d.get("output_tail") or ""))
        mech = {"rc": d.get("rc"), "status": d.get("status"),
                "duration_s": d.get("duration_s"), "model": d.get("model"),
                "mode": d.get("mode"), "subtype": d.get("subtype"),
                "cost_usd": evs[0].get("cost_usd"),
                "permission_denials": d.get("permission_denials"),
                "envelope_ok": d.get("envelope_ok"), "source": "event"}
    material, trunc = _cap_text(material)
    if trunc:
        mech["truncated"] = True
    return project, intent, material, mech, jid


def _transcript_roots():
    """Containment roots for legitimate session transcripts, derived from each
    agent's manifest glob (the same authoritative source the sessions list
    walks): the fixed prefix before the first wildcard. '{cwd}'/'{enc_cwd}'
    globs live per-project under PROJECTS_ROOT (e.g. aider's .aider.chat.history.md)."""
    roots = {TRANSCRIPTS_ROOT.resolve(), PROJECTS_ROOT.resolve()}
    for a in za.load_agents():
        g = ((a or {}).get("transcript") or {}).get("glob") or ""
        if not g or "{cwd}" in g or "{enc_cwd}" in g:
            continue
        # fixed prefix = the glob up to its first wildcard component
        parts = []
        for seg in os.path.expanduser(g).split(os.sep):
            if any(c in seg for c in "*?["):
                break
            parts.append(seg)
        pre = os.sep.join(parts)
        if pre:
            try:
                roots.add(Path(pre).resolve())
            except (OSError, ValueError):
                pass
    return roots


def _verify_material_session(body):
    """kind=session: last 10 prompts as intent; uncommitted diff (or last
    commit when clean) in the session's project as material. Accepts any
    agent's transcript (claude/codex/aider), not just claude's."""
    spath = Path(body.get("session") or "").resolve()
    if not any(spath == r or r in spath.parents for r in _transcript_roots()):
        raise ValueError("session outside transcripts root")
    if not spath.exists():
        raise KeyError("no such transcript")
    fmt, _aid = za.transcript_format_for_path(spath)
    d = _transcript_detail(spath, fmt)
    proj = body.get("project") \
        or match_project(spath.parent.name, project_encoding_map()) or ""
    if proj:
        pp = Path(proj).resolve()
        # only shell git inside claudeProjects; an out-of-root project (e.g. a
        # malicious caller-supplied path) is ignored — judge from transcript only
        proj = str(pp) if (pp == PROJECTS_ROOT or pp.parent == PROJECTS_ROOT) else ""
    intent = body.get("intent") or "\n".join(
        p.get("text", "") for p in (d.get("prompts") or [])[-10:]) or "(no prompts found)"
    git_clean = None
    diffstat = ""
    if proj and (Path(proj) / ".git").exists():
        status = _git(["status", "--porcelain"], proj)
        if status.strip():
            git_clean = False
            material = ("UNTRACKED/CHANGED (git status --porcelain):\n" + status
                        + "\ngit diff (uncommitted):\n" + _git(["diff"], proj))
            diffstat = _git(["diff", "--stat"], proj)
        else:
            git_clean = True
            material = _git(["show", "HEAD", "--stat", "-p"], proj)
            diffstat = _git(["show", "HEAD", "--stat"], proj)
    else:
        material = "(no git project resolved for this session — judge from transcript stats)"
    mech = {"usage": d.get("usage"), "tools": d.get("tools"),
            "diffstat": diffstat[:1500], "git_clean": git_clean}
    material, trunc = _cap_text(material)
    if trunc:
        mech["truncated"] = True
    return proj, intent, material, mech, str(spath)


def _verify_material_diff(body):
    """kind=diff: `git diff <ref>` (or uncommitted worktree when ref absent)."""
    pp = Path(body.get("project") or "").resolve()
    if not (pp == PROJECTS_ROOT or pp.parent == PROJECTS_ROOT):
        raise ValueError("project outside claudeProjects")
    proj = str(pp)
    if not (Path(proj) / ".git").exists():
        raise ValueError("project is not a git repo")
    ref = body.get("ref") or ""
    if ref:
        material = _git(["diff", ref], proj)
        diffstat = _git(["diff", "--stat", ref], proj)
    else:
        material = ("git status --porcelain:\n" + _git(["status", "--porcelain"], proj)
                    + "\ngit diff (uncommitted):\n" + _git(["diff"], proj))
        diffstat = _git(["diff", "--stat"], proj)
    intent = body.get("intent") or "review this diff"
    material, trunc = _cap_text(material)
    mech = {"diffstat": diffstat[:1500], "ref": ref or "worktree"}
    if trunc:
        mech["truncated"] = True
    return proj, intent, material, mech, f"{proj}@{ref or 'worktree'}"


def _verify_material_harness(body):
    """kind=harness: the Zenith repo's own diff + invariant probe results
    (§5.6). Probes run here so their output rides in mech AND the checklist."""
    root = str(ZENITH_DIR)
    ref = body.get("ref") or "HEAD~1..HEAD"
    material = _git(["diff", ref], root)
    diffstat = _git(["diff", "--stat", ref], root)
    sha = _git(["rev-parse", "HEAD"], root).strip()
    mech = {"diffstat": diffstat[:1500], "ref": ref, "head": sha,
            "probes": _run_harness_probes()}
    material, trunc = _cap_text(material)
    if trunc:
        mech["truncated"] = True
    return root, "harness change — do the guardrails still hold", material, mech, \
        f"harness@{sha}"


def _verify_material(kind, body):
    fn = {"job": _verify_material_job, "session": _verify_material_session,
          "diff": _verify_material_diff, "harness": _verify_material_harness}.get(kind)
    if not fn:
        raise ValueError("kind must be job|session|diff|harness")
    return fn(body)


_VERIFY_PENDING = {}   # verify_id -> {kind,target_ref,project,mech,model,trigger}
                       # in-memory (guarded by _jobs_lock); a restart orphans
                       # pending verifies → _record_verify_verdict stores error.

_SEVERITIES = ("crit", "major", "minor", "info")


def _extract_json_obj(result, marker):
    """Three-stage tolerant extraction of a JSON object from LLM output:
    (1) whole text; (2) substring from the LAST occurrence of `marker` to the
    final '}'; (3) strip ``` fences and retry each fenced block.
    str -> dict|None. Shared by the ringer verdict and the A/B judge."""
    s = (result or "").strip()

    def _try(t):
        try:
            o = json.loads(t)
            return o if isinstance(o, dict) else None
        except (ValueError, TypeError):
            return None

    obj = _try(s)
    if obj is None:
        i, j = s.rfind(marker), s.rfind("}")
        if 0 <= i < j:
            obj = _try(s[i:j + 1])
    if obj is None and "```" in s:
        for part in s.replace("```json", "```").split("```"):
            obj = _try(part.strip())
            if obj is not None:
                break
    return obj


def _extract_verdict_json(result):
    """§5.5 step 2 — unchanged contract, now the marker-parameterized core."""
    return _extract_json_obj(result, '{"issues"')


def _clean_issues(raw):
    """§5.5 step 3 — validate/coerce reviewer issues: unknown severity → info,
    truncate claim 300 / evidence 1000 / location 200, drop non-dicts."""
    out = []
    for it in (raw or []):
        if not isinstance(it, dict):
            continue
        sev = it.get("severity")
        if sev not in _SEVERITIES:
            sev = "info"
        out.append({"severity": sev,
                    "claim": str(it.get("claim") or "")[:300],
                    "evidence": str(it.get("evidence") or "")[:1000],
                    "location": str(it.get("location") or "")[:200]})
    return out


def _verdict_rollup(crit, major, minor, ringer_ok, parse_ok):
    """§5.5 step 4 — computed by Zenith, NEVER taken from the reviewer:
    the reviewer enumerates, the OS judges."""
    if not ringer_ok or not parse_ok:
        return "error"
    if crit + major > 0:
        return "fail"
    if minor > 0:
        return "warn"
    return "pass"


def _record_verify_verdict(job):
    """§5.5: runs inside _job_finished (usage/cost already on the job).
    Parses the ringer's result, computes the rollup, stores the verdict row,
    emits verify.end, backfills the event id, and records the harness sha."""
    vf = job.get("verify_id")
    with _jobs_lock:
        pend = _VERIFY_PENDING.pop(vf, None)
    if pend is None:               # orphaned verify (restart mid-ringer)
        pend = {"kind": "job", "target_ref": str(job.get("id")),
                "project": job.get("project") or "",
                "mech": {"orphaned_verify": True}, "model": job.get("model"),
                "target_agent": "claude",
                "trigger": "user"}
        obj, parse_ok = None, False
    else:
        obj = _extract_verdict_json(job.get("result") or "")
        parse_ok = obj is not None
    issues = _clean_issues((obj or {}).get("issues"))
    n = {s: sum(1 for i in issues if i["severity"] == s) for s in _SEVERITIES}
    verdict = _verdict_rollup(n["crit"], n["major"], n["minor"],
                              job.get("rc") == 0, parse_ok)
    summary = str((obj or {}).get("summary") or "")[:2000]
    if verdict == "error" and not summary:
        summary = (job.get("result")
                   or "\n".join(job.get("output") or []))[:2000]
    u = job.get("usage") or {}
    vid = zs.verdict_insert(
        target_kind=pend["kind"], target_ref=pend["target_ref"],
        project=pend.get("project") or "", verify_job=job.get("id"),
        model=job.get("model") or pend.get("model") or "",
        verdict=verdict, summary=summary, issues=issues,
        mech=pend.get("mech") or {}, crit=n["crit"], major=n["major"],
        minor=n["minor"], info=n["info"], tok_in=u.get("in"),
        tok_out=u.get("out"), agent=pend.get("target_agent") or "claude",
        cost_usd=job.get("cost_usd"))
    eid = zs.emit("verify.end", project=pend.get("project") or "",
                  ref=pend["target_ref"], outcome=verdict, actor="verify",
                  agent=pend.get("target_agent") or "claude",
                  tokens=_tok_tuple(job), cost=job.get("cost_usd"),
                  data={"verdict_id": vid, "verify_id": vf, "verdict": verdict,
                        "crit": n["crit"], "major": n["major"],
                        "minor": n["minor"], "info": n["info"],
                        "ab_id": pend.get("ab_id"),
                        "trigger": pend.get("trigger", "user")})
    if eid and vid:
        zs.verdict_set_event(vid, eid)
    if pend["kind"] == "harness" and verdict in ("pass", "warn"):
        sha = str(pend["target_ref"]).split("@")[-1]
        if sha:
            zs.meta_set("harness_verified_sha", sha)
    if pend.get("ab_id"):
        _ab_verdict_arrived(pend["ab_id"], pend["target_ref"])


HARNESS_INVARIANTS = [   # §5.6: each probe is a zero-side-effect callable name
    {"id": "H1", "claim": "Launching a bypassPermissions job without a "
                          "confirmed gate is refused (428)", "probe": "_probe_h1"},
    {"id": "H2", "claim": "Budget flag still reaches the CLI: a job with "
                          "budget produces --max-budget-usd", "probe": "_probe_h2"},
    {"id": "H3", "claim": "Verify ringers run mode=default (cannot "
                          "write/execute)", "probe": "_probe_h3"},
    {"id": "H4", "claim": "Job kill works: /api/job/stop terminates and the "
                          "exit is recorded as an event", "probe": "_probe_h4"},
    {"id": "H5", "claim": "Dynamic HTML still passes through esc()",
     "probe": ""},   # reviewer checks the diff for raw innerHTML interpolation
    {"id": "H6", "claim": "Gate decisions are logged before the gated action "
                          "executes", "probe": "_probe_h6"},
    {"id": "H8", "claim": "Verify ringers run on the fixed reviewer agent "
                          "(REVIEWER_AGENT), never the target job's agent",
     "probe": "_probe_h8"},
]


def _probe_h1():
    fn = globals().get("_classify")          # P5 ships it; degrade until then
    if not fn:
        return "SKIP: _classify not shipped yet (P5)"
    g = fn("jobs.spawn", {"mode": "bypassPermissions"})
    return ("OK: level=confirm" if g.level == "confirm"
            else f"FAIL: level={g.level}")


def _probe_h2():
    cmd = _job_cmd({"prompt": "p", "model": "sonnet", "mode": "default",
                    "budget": 5, "add_dir": None, "agent": "claude"})
    return ("OK: --max-budget-usd present" if "--max-budget-usd" in cmd
            else "FAIL: flag missing from _job_cmd")


def _probe_h3():
    return ("OK: VERIFY_MODE='default'" if VERIFY_MODE == "default"
            else f"FAIL: VERIFY_MODE={VERIFY_MODE!r}")


def _probe_h4():
    killed = len(zs.events_query(kind="job.end", outcome="killed", limit=200))
    return f"job.end outcome=killed events on record (last 200 scanned): {killed}"


def _probe_h6():
    gates = zs.gates_query(limit=200)
    confirmed = sum(1 for g in gates if g.get("decision") == "confirmed")
    return (f"gates rows: {len(gates)} (confirmed: {confirmed}) — reviewer: "
            f"compare against confirmed launches in the diff window")


def _probe_h8():
    ok = REVIEWER_AGENT == "claude" or any(
        a.get("id") == REVIEWER_AGENT and a.get("enabled") and a.get("reviewer_ok")
        for a in za.load_agents())
    return (f"OK: reviewer agent={REVIEWER_AGENT!r}" if ok
            else f"FAIL: {REVIEWER_AGENT!r} is not an enabled reviewer_ok agent")


def _run_harness_probes():
    out = {}
    for inv in HARNESS_INVARIANTS:
        name = inv.get("probe")
        if not name:
            out[inv["id"]] = "no mechanical probe — reviewer checks the diff"
            continue
        try:
            out[inv["id"]] = str(globals()[name]())
        except Exception as e:
            out[inv["id"]] = f"probe error: {e}"
    return out


_drift_cache = {"t": 0.0, "head": ""}


def _harness_drift():
    """HEAD of the Zenith repo (cached 60 s) vs meta.harness_verified_sha —
    feeds the amber HARNESS DRIFT badge (§5.6). Manual verify only, no
    auto-run (cost control)."""
    now = time.time()
    if now - _drift_cache["t"] > 60:
        try:
            _drift_cache["head"] = _git(["rev-parse", "HEAD"],
                                        str(ZENITH_DIR)).strip()
        except (ValueError, OSError, subprocess.TimeoutExpired):
            _drift_cache["head"] = ""
        _drift_cache["t"] = now
    verified = zs.meta_get("harness_verified_sha", "") or ""
    head = _drift_cache["head"]
    return {"head": head, "verified": verified,
            "drift": bool(head) and head != verified}


AUTOVERIFY_DEFAULT = {"enabled": False,
                      "triggers": {"bypass_jobs": False,
                                   "acceptEdits_jobs": False,
                                   "loop_runs": False},
                      "min_cost_usd": 0.0, "daily_cap_usd": 5.0}


def _autoverify_cfg():
    cfg = dict(AUTOVERIFY_DEFAULT) | _load_json(AUTOVERIFY_FILE, {})
    cfg["triggers"] = dict(AUTOVERIFY_DEFAULT["triggers"]) | dict(cfg.get("triggers") or {})
    return cfg


def _autoverify_spend_today():
    """Sum of today's AUTO-triggered verify cost, from verify.end events
    (data.trigger=='auto'). id-desc order lets us stop at the first
    non-today row."""
    today = now_iso()[:10]
    spend = 0.0
    for e in zs.events_query(kind="verify.end", limit=200):
        if not str(e.get("ts", "")).startswith(today):
            break
        if (e.get("data") or {}).get("trigger") == "auto":
            spend += e.get("cost_usd") or 0
    return spend


def _autoverify_decision(cfg, job, spent_today):
    """Pure §5.7 predicate: 'run' or 'skip:<reason>'. Checked in _selfcheck."""
    if job.get("verify_id"):
        return "skip:ringer"                  # a ringer NEVER auto-verifies
    if not cfg.get("enabled"):
        return "skip:disabled"
    if _job_outcome(job) not in ("ok", "error"):
        return "skip:outcome"
    trig = cfg.get("triggers") or {}
    matched = ((job.get("mode") == "bypassPermissions" and trig.get("bypass_jobs"))
               or (job.get("mode") == "acceptEdits" and trig.get("acceptEdits_jobs"))
               or (job.get("loop_id") and trig.get("loop_runs")))
    if not matched:
        return "skip:no_trigger"
    if (job.get("cost_usd") or 0) < (cfg.get("min_cost_usd") or 0):
        return "skip:below_min_cost"
    if spent_today >= (cfg.get("daily_cap_usd") or 0):
        return "skip:capped"
    return "run"


def _maybe_autoverify(job):
    """§5.7 tail hook in _job_finished. Runs on the job-reader thread
    (spawn_verify is thread-safe). Never breaks the completion path."""
    try:
        cfg = _autoverify_cfg()
        if not cfg.get("enabled") or job.get("verify_id"):
            return                            # cheap outs before the spend query
        d = _autoverify_decision(cfg, job, _autoverify_spend_today())
        if d == "run":
            spawn_verify("job", {"job_id": job["id"]}, trigger="auto")
        elif d == "skip:capped":              # the skip is visible, never silent
            zs.emit("verify.skipped", project=job.get("project") or "",
                    ref=job["id"], outcome="capped", actor="zenith",
                    agent=job.get("agent") or "claude",
                    data={"reason": "daily_cap",
                          "cap": cfg.get("daily_cap_usd"), "trigger": "auto"})
    except Exception as e:
        job["output"].append(f"[autoverify error: {e}]")


def spawn_verify(kind, body, trigger="user"):
    """§5.3: gather material + mechanical evidence, pick the ladder model,
    launch the ringer as an ordinary job (one pipeline, one console, one
    accounting path). Returns (verify_id, ringer job_id)."""
    project, intent, material, mech, target_ref = _verify_material(kind, body)
    model = body.get("model") or VERIFY_LADDER.get(
        _model_tier(_target_model(kind, body)), "opus")
    verify_id = "vf_" + uuid.uuid4().hex[:8]
    # Register the pending verify BEFORE launching the ringer. If spawn_job
    # completes synchronously (disabled reviewer agent, or a Popen OSError /
    # EAGAIN fork failure — likeliest exactly when spawning N ringers for an A/B
    # cohort), it runs _job_finished inline, whose _record_verify_verdict tail
    # must find this entry (carrying ab_id) — otherwise _ab_verdict_arrived never
    # fires and the A/B cohort hangs forever in phase="verify" (the verify stage
    # has no replay sweep the way the arm/judge stages do).
    with _jobs_lock:
        _VERIFY_PENDING[verify_id] = {"kind": kind, "target_ref": target_ref,
                                      "project": project, "mech": mech,
                                      "model": model,
                                      "target_agent": _target_agent(kind, body),
                                      "ab_id": str(body.get("ab_id") or "") or None,
                                      "trigger": trigger}
    prompt = VERIFY_PROMPT.format(
        kind=kind, target=target_ref, intent=intent,
        mech=json.dumps(mech, indent=1, default=str), material=material,
        extra=_harness_checklist(mech) if kind == "harness" else "")
    job_id = spawn_job(project or str(ZENITH_DIR), prompt, model=model,
                       mode=VERIFY_MODE,          # enumerate-don't-fix, enforced
                       label=f"verify:{kind}:{target_ref[-40:]}",
                       verify_id=verify_id, agent=REVIEWER_AGENT)
    zs.emit("verify.start", project=project, ref=target_ref, outcome="ok",
            actor="zenith" if trigger == "auto" else "user",
            data={"verify_id": verify_id, "target_kind": kind, "model": model,
                  "reviewer_agent": REVIEWER_AGENT,
                  "target_agent": _target_agent(kind, body),
                  "ab_id": str(body.get("ab_id") or "") or None,
                  "verify_job": job_id, "trigger": trigger})
    return verify_id, job_id


def _target_model(kind, body):
    """The model of the work under review (job kind only) — feeds the ladder."""
    if kind != "job":
        return None
    jid = str(body.get("job_id") or "")
    job = JOBS.get(jid)
    if job:
        return job.get("model")
    evs = zs.events_query(kind="job.end", ref=jid, limit=1)
    return (evs[0].get("data") or {}).get("model") if evs else None


def _target_agent(kind, body):
    """Agent of the work under review (job kind only) — the verdict's agent
    dimension. Non-job targets (session/diff/harness) are Claude-era material."""
    if kind != "job":
        return "claude"
    jid = str(body.get("job_id") or "")
    job = JOBS.get(jid)
    if job:
        return job.get("agent") or "claude"
    evs = zs.events_query(kind="job.end", ref=jid, limit=1)
    return ((evs[0].get("data") or {}).get("agent") or "claude") if evs else "claude"


def _harness_checklist(mech):
    """The {extra} block for harness verifies (§5.4): numbered invariants +
    live probe results. Implemented fully in Task 33 (HARNESS_INVARIANTS)."""
    lines = ["", "HARNESS GUARDRAIL CHECKLIST — for EACH invariant below, either state that it still holds",
             "(citing the diff / probe evidence) or file an issue at severity crit:"]
    probes = (mech or {}).get("probes") or {}
    for i, inv in enumerate(HARNESS_INVARIANTS, 1):
        lines.append(f"{i}. [{inv['id']}] {inv['claim']} — probe: "
                     f"{probes.get(inv['id'], 'n/a')}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- loops v2
# ZENITH's own recurring-job engine (distinct from the read-only NexusMind
# schedule registry served at /api/loops).

def _iso_plus(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _iso_epoch(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return 0.0


def loops_load():
    return _load_json(LOOPS_FILE, [])


def loop_upsert(obj):
    lid = obj.get("id")
    obj.setdefault("skills", [])
    obj.setdefault("model", "sonnet")
    obj.setdefault("mode", "default")
    obj.setdefault("runner", "claude")
    obj.setdefault("agent", "claude")   # which coding agent runs the loop (claude/codex/aider)
    obj.setdefault("provider", None)
    if obj.get("context") not in CONTEXT_CHOICES:
        obj["context"] = "none"
    if obj.get("task") not in PROVIDER_TASKS:
        obj["task"] = "chat"
    obj.setdefault("context_globs", "")
    obj["enabled"] = bool(obj.get("enabled"))
    obj["interval_min"] = max(5, int(obj.get("interval_min", 60)))
    with _data_lock:
        loops = loops_load()
        if not lid:
            obj["id"] = uuid.uuid4().hex[:12]
            obj["created"] = now_iso()
            obj.setdefault("last_run", None)
            obj["next_due"] = _iso_plus(obj["interval_min"]) if obj["enabled"] else None
            loops.append(obj)
        else:
            for i, lp in enumerate(loops):
                if lp.get("id") == lid:
                    obj["created"] = lp.get("created", now_iso())
                    obj.setdefault("last_run", lp.get("last_run"))
                    if obj["enabled"] and not obj.get("next_due"):
                        obj["next_due"] = _iso_plus(obj["interval_min"])
                    if not obj["enabled"]:
                        obj["next_due"] = None
                    loops[i] = obj
                    break
            else:
                obj.setdefault("created", now_iso())
                obj["next_due"] = _iso_plus(obj["interval_min"]) if obj["enabled"] else None
                loops.append(obj)
        _save_json(LOOPS_FILE, loops)
    return obj


def loop_delete(lid):
    with _data_lock:
        loops = [lp for lp in loops_load() if lp.get("id") != lid]
        _save_json(LOOPS_FILE, loops)
    return True


def _spawn_loop_job(lp):
    if lp.get("runner") == "provider":
        # was a bespoke off-thread call that produced no job; now the same
        # first-class job the Jobs screen spawns, so a provider loop run shows
        # up in the mission log and its history points at a real job id
        return spawn_provider_job(lp["project"], lp["prompt"],
                                  lp.get("provider") or "", lp.get("model") or "",
                                  label=lp.get("name"), loop_id=lp["id"],
                                  context=lp.get("context"),
                                  context_globs=lp.get("context_globs"),
                                  task=lp.get("task"))
    return spawn_job(lp["project"], lp["prompt"], model=lp.get("model", "sonnet"),
                     mode=lp.get("mode", "default"), skills=lp.get("skills"),
                     loop_id=lp["id"], label=lp.get("name"),
                     agent=lp.get("agent", "claude"))


def loop_run_now(lid):
    with _data_lock:
        loops = loops_load()
        lp = next((x for x in loops if x.get("id") == lid), None)
        if not lp:
            raise KeyError("no such loop")
        job_id = _spawn_loop_job(lp)
        lp["last_run"] = now_iso()
        if lp.get("enabled"):
            lp["next_due"] = _iso_plus(lp["interval_min"])
        _save_json(LOOPS_FILE, loops)
    return job_id


def loop_runs(lid, limit=30):
    """HISTORY for one loop, re-backed by the event store (§2.5). Response
    shape unchanged: {loop_id, job_id, started, ended, status, tail} (+
    runner/provider/model for provider runs). loop_runs.jsonl is never read."""
    if not lid:
        return []
    limit = max(1, min(int(limit or 30), 60))
    evs = zs.events_query(kind="job.end", q=lid, limit=min(limit * 3, 200))
    out = []
    for e in evs:
        d = e.get("data") or {}
        if d.get("loop_id") != lid:
            continue                      # q= is a LIKE; enforce the exact match
        out.append({
            "loop_id": lid,
            "job_id": d["job_id"] if "job_id" in d else e.get("ref"),
            "runner": d.get("agent") or ("provider" if d.get("kind") == "provider"
                                         else "claude"),
            "provider": d.get("provider"), "model": d.get("model"),
            "task": d.get("task"), "context": d.get("context_label"),
            "context_chars": d.get("context_chars"),
            "started": d.get("started"),
            "ended": d.get("ended") or e.get("ts"),
            "status": d.get("status") or e.get("outcome"),
            "tail": d.get("output_tail") or ""})
        if len(out) >= limit:
            break
    return out


def loop_scheduler():
    """Daemon: every 30s, fire any enabled loop whose next_due has passed."""
    while True:
        try:
            now = time.time()
            with _data_lock:
                loops = loops_load()
                changed = False
                for lp in loops:
                    if not lp.get("enabled"):
                        continue
                    nd = lp.get("next_due")
                    if nd is None or now >= _iso_epoch(nd):
                        try:
                            _spawn_loop_job(lp)
                        except (OSError, KeyError):
                            pass
                        lp["last_run"] = now_iso()
                        lp["next_due"] = _iso_plus(lp["interval_min"])
                        changed = True
                if changed:
                    _save_json(LOOPS_FILE, loops)
        except Exception:
            pass
        time.sleep(30)


# ---------------------------------------------------------------- cross-agent A/B (P3)
# One prompt, N agent+model arms, same project. Cohorts are event-backed
# (ab.start/ab.judge/ab.end + ab_id inside job.* data) — no new table, the
# loop_runs reconstruction pattern. _AB_RUNS drives the live pipeline only.

_AB_RUNS = {}   # ab_id -> registry entry (see design contract); guarded by
                # _jobs_lock. In-memory: a restart orphans a live cohort (D6);
                # ab_run then reports status "orphaned" from the events.


def _ab_spawn_arm(run, spec, i):
    """Spawn one arm from a normalized spec, append it to run['arms'] under the
    lock, and return the job_id. Shared by parallel launch, sequential launch,
    and the sequential continuation. CRITICAL: spawn_* take _jobs_lock
    themselves, so this MUST be called with the lock RELEASED. The (agent →
    provider → ordinary) branching is byte-identical to the original loop."""
    ab_id = run["ab_id"]
    project, prompt, mode = run["project"], run["prompt"], run["mode"]
    agent, model = spec["agent"], spec["model"]
    provider, effort = spec["provider"], spec["effort"]
    if agent == "provider":                       # raw completion arm (no workspace)
        jid = spawn_provider_job(project, prompt, str(provider or ""), model,
                                 ab_id=ab_id, label=f"ab-arm-{i}")
    elif provider:                                # a CLI agent (aider) pointed at a
        jid = spawn_job(                          # local/remote OpenAI-compatible model
            project, prompt, model="openai/" + model, mode=mode,
            effort=effort, agent=agent, ab_id=ab_id,
            label=f"ab-arm-{i}", extra_env=_provider_openai_env(str(provider)))
    else:                                         # ordinary agent arm (unchanged)
        jid = spawn_job(project, prompt, model=model, mode=mode,
                        effort=effort, agent=agent,
                        ab_id=ab_id, label=f"ab-arm-{i}")
    with _jobs_lock:
        run["arms"].append({"agent": agent, "model": model,
                            "provider": provider, "job_id": jid,
                            "done": False, "verdict_in": False})
    return jid


def ab_launch(project, prompt, mode, arms, verify=True, judge=True,
              judge_model=None, sequential=False):
    """Spawn one ordinary job per arm, tagged with a fresh ab_id. Caller
    (the route) has already validated + gate-checked. Returns {ab_id, arms}.

    sequential=False (default): spawn ALL arms at once — byte-identical to the
    original. sequential=True: spawn only the first arm now; each subsequent
    arm is spawned by _ab_spawn_next when the prior one ends. Either way, the
    last arm ending flips phase past "arms" (verify/judge/done)."""
    ab_id = "ab_" + uuid.uuid4().hex[:8]
    # Normalize every arm to a spec up front — don't spawn yet.
    specs = [{"agent": str(a.get("agent") or "claude"),
              "model": str(a.get("model") or "sonnet"),
              "provider": a.get("provider") or None,
              "effort": a.get("effort")} for a in arms]
    run = {"ab_id": ab_id, "project": project, "prompt": prompt, "mode": mode,
           "verify": bool(verify), "judge": bool(judge),
           "judge_model": judge_model, "created": now_iso(),
           "arms": [], "judge_job": None, "judged": False, "finished": False,
           "phase": "spawning", "sequential": bool(sequential),
           "pending_specs": [], "next_i": 1}
    with _jobs_lock:
        _AB_RUNS[ab_id] = run
    if sequential:
        # Spawn only arm 1; the rest wait in pending_specs (with their 1-based
        # index) for _ab_spawn_next. next_i tracks the highest index spawned.
        run["next_i"] = 1
        run["pending_specs"] = [(i, s) for i, s in enumerate(specs[1:], 2)]
        _ab_spawn_arm(run, specs[0], 1)
    else:
        for i, spec in enumerate(specs, 1):       # spawn ALL arms now
            _ab_spawn_arm(run, spec, i)
        run["next_i"] = len(specs)
    with _jobs_lock:
        run["phase"] = "arms"
    zs.emit("ab.start", project=project, ref=ab_id, outcome="ok", actor="user",
            data={"prompt": prompt[:2000], "mode": mode,
                  "verify": run["verify"], "judge": run["judge"],
                  "judge_model": judge_model, "sequential": run["sequential"],
                  "arms": [{"agent": a["agent"], "model": a["model"],
                            "provider": a.get("provider"),
                            "job_id": a["job_id"]} for a in run["arms"]]})
    # Replay sweep: an arm that failed SYNCHRONOUSLY (unknown adapter, spawn
    # OSError) ran _job_finished before it was registered above — its hook
    # call missed the registry. Re-run it now; _ab_on_job_end is idempotent.
    # In sequential mode this also drives arm 2 to spawn if arm 1 failed sync.
    for a in list(run["arms"]):
        j = JOBS.get(a["job_id"])
        if j and j.get("status") != "running":
            _ab_on_job_end(j)
    return {"ab_id": ab_id, "arms": [a["job_id"] for a in run["arms"]]}


def _ab_on_job_end(job):
    """Tail hook in _job_finished for jobs carrying ab_id (arms + the judge).
    NEVER breaks the completion path. Ringers carry verify_id but never ab_id
    on the job dict (D8), so they don't land here."""
    ab = job.get("ab_id")
    if not ab:
        return
    try:
        fire = None
        with _jobs_lock:
            run = _AB_RUNS.get(ab)
            if run is None:
                return                      # restart-orphaned cohort
            arm = next((a for a in run["arms"]
                        if a["job_id"] == job["id"]), None)
            if arm is not None:
                arm["done"] = True          # idempotent by construction
                if run["phase"] == "arms" \
                        and all(a["done"] for a in run["arms"]):
                    # All CURRENT arms done. Sequential: if specs still pend,
                    # spawn the next one; else this was the last → arms_done.
                    # The pending_specs pop happens under the lock in
                    # _ab_spawn_next, so a duplicate hook (replay sweep) that
                    # reaches here again finds no pending spec and no-ops.
                    if run.get("pending_specs"):
                        fire = "next_arm"
                    else:
                        fire = "arms_done"
            elif run["phase"] == "judge":
                fire = "judge_done"         # cohort job that isn't an arm
        if fire == "next_arm":
            _ab_spawn_next(ab)
        elif fire == "arms_done":
            _ab_arms_done(ab)
        elif fire == "judge_done":
            _ab_judge_done(ab, job)
    except Exception as e:                  # mirror _maybe_autoverify
        job["output"].append(f"[ab hook error: {e}]")


def _ab_spawn_next(ab_id):
    """Sequential continuation: pop the next pending spec UNDER the lock (the
    single-fire guard — a duplicate hook finds pending_specs already popped and
    no-ops), then spawn it with the lock RELEASED. Mirrors how _ab_arms_done
    calls spawn_verify. The freshly-spawned arm is replay-swept for a
    synchronous failure, exactly like arm 1 in ab_launch."""
    spec = None
    idx = None
    with _jobs_lock:
        run = _AB_RUNS.get(ab_id)
        if run is None or run["phase"] != "arms":
            return
        if not run.get("pending_specs"):
            return                          # already consumed (dup hook)
        idx, spec = run["pending_specs"].pop(0)
        run["next_i"] = idx
    jid = _ab_spawn_arm(run, spec, idx)     # lock RELEASED (spawn_* take it)
    j = JOBS.get(jid)
    if j and j.get("status") != "running":
        _ab_on_job_end(j)                   # replay sweep: this arm failed sync


def _ab_arms_done(ab_id):
    """arms → verify | judge | done. The phase flip under the lock is the
    single-transition guard: duplicate calls see phase!='arms' and no-op."""
    with _jobs_lock:
        run = _AB_RUNS.get(ab_id)
        if run is None or run["phase"] != "arms":
            return
        run["phase"] = ("verify" if run["verify"]
                        else "judge" if run["judge"] else "done")
        phase = run["phase"]
        arms = list(run["arms"])
        project = run["project"]
    if phase == "verify":
        for a in arms:
            try:                      # spawn_verify takes _jobs_lock itself —
                spawn_verify("job",   # must be called with the lock RELEASED
                             {"job_id": a["job_id"], "ab_id": ab_id})
            except Exception as e:    # unverifiable arm: breadcrumb + count it
                zs.emit("ab.note", project=project, ref=ab_id,
                        outcome="error", actor="zenith",
                        data={"note": "verify launch failed for arm "
                                      f"{a['job_id']}: {e}"})
                _ab_verdict_arrived(ab_id, a["job_id"])
    elif phase == "judge":
        _ab_spawn_judge(ab_id)
    else:
        _ab_finish(ab_id)


def _ab_verdict_arrived(ab_id, arm_job_id):
    """A per-arm verdict landed (or its verify couldn't launch). When the
    LAST one arrives: verify → judge | done. Idempotent via arm.verdict_in;
    the phase flip under the lock is the single-transition guard."""
    nxt = None
    with _jobs_lock:
        run = _AB_RUNS.get(ab_id)
        if run is None or run["phase"] != "verify":
            return
        arm = next((a for a in run["arms"]
                    if a["job_id"] == str(arm_job_id)), None)
        if arm is None or arm.get("verdict_in"):
            return
        arm["verdict_in"] = True
        if all(a.get("verdict_in") for a in run["arms"]):
            run["phase"] = "judge" if run["judge"] else "done"
            nxt = run["phase"]
    if nxt == "judge":
        _ab_spawn_judge(ab_id)
    elif nxt == "done":
        _ab_finish(ab_id)


def _ab_spawn_judge(ab_id):
    """Launch the judge as an ordinary claude job: fixed agent (D5, the H8
    self-judging argument), mode=VERIFY_MODE (ranks, cannot fix), tagged with
    ab_id. judge_job is reserved under the lock BEFORE spawning — the stage
    can never double-fire — then replay-swept for synchronous spawn failure
    (same hole as arms: _job_finished runs inline before jid is recorded)."""
    with _jobs_lock:
        run = _AB_RUNS.get(ab_id)
        if run is None or run.get("judge_job") is not None:
            return
        run["judge_job"] = "pending"
        model = run.get("judge_model") or "opus"
        project, prompt = run["project"], run["prompt"]
    try:
        cohort = ab_run(ab_id)
    except KeyError:
        cohort = {"arms": []}         # ab.start unrecorded (emit spill) —
    jid = spawn_job(project,          # judge still runs, on thin evidence
                    _ab_judge_prompt(prompt, cohort["arms"]),
                    model=model, mode=VERIFY_MODE, agent="claude",
                    ab_id=ab_id, label="ab-judge")
    with _jobs_lock:
        run["judge_job"] = jid
    j = JOBS.get(jid)
    if j and j.get("status") != "running":
        _ab_on_job_end(j)             # replay sweep (judge failed to spawn)


def _ab_judge_done(ab_id, job):
    """Judge job ended: parse → ab.judge event → finish. `judged` is the
    single-fire guard; a judge that errored or emitted junk still completes
    the cohort (outcome=error, winner=None — the UI shows JUDGE ERROR)."""
    with _jobs_lock:
        run = _AB_RUNS.get(ab_id)
        if run is None or run["phase"] != "judge" or run.get("judged"):
            return
        run["judged"] = True
        n = len(run["arms"])
        project = run["project"]
    j = _parse_ab_judge(job.get("result") or "", n)
    zs.emit("ab.judge", project=project, ref=ab_id,
            outcome="ok" if (j["parse_ok"] and j["winner"]) else "error",
            actor="zenith", agent=job.get("agent") or "claude",
            tokens=_tok_tuple(job), cost=job.get("cost_usd"),
            data=j | {"judge_job": job["id"],
                      "judge_model": job.get("model")})
    _ab_finish(ab_id)


def _ab_finish(ab_id):
    """Terminal transition: emit ab.end exactly once, phase=done. The entry
    stays in _AB_RUNS (like JOBS) so status reads 'done' until restart."""
    with _jobs_lock:
        run = _AB_RUNS.get(ab_id)
        if run is None or run.get("finished"):
            return
        run["finished"] = True
        run["phase"] = "done"
        arms = [{"job_id": a["job_id"], "agent": a["agent"],
                 "model": a["model"]} for a in run["arms"]]
        project, verify, judge = run["project"], run["verify"], run["judge"]
    zs.emit("ab.end", project=project, ref=ab_id, outcome="ok", actor="zenith",
            data={"arms": arms, "verify": verify, "judge": judge})


def _ab_arm_view(meta, job, end_ev):
    """PURE per-arm cohort row from EITHER the live JOBS dict OR the recorded
    job.end event row (whichever the caller found). Cost/usage stay None when
    unknown (D9). verdict is attached by the caller."""
    base = {"agent": (meta or {}).get("agent"),
            "model": (meta or {}).get("model"),
            "job_id": str((meta or {}).get("job_id") or ""),
            "status": None, "cost_usd": None, "usage": None,
            "duration_s": None, "started": None, "result": "", "verdict": None}
    if job is not None:
        try:
            dur = (round(_iso_epoch(job.get("ended"))
                         - _iso_epoch(job.get("started")), 1)
                   if job.get("ended") else None)
        except (TypeError, ValueError):
            dur = None
        base.update(status=job.get("status"), cost_usd=job.get("cost_usd"),
                    usage=job.get("usage"), duration_s=dur,
                    started=job.get("started"),
                    result=(job.get("result") or "")[:16000])
    elif end_ev is not None:
        d = end_ev.get("data") or {}
        base.update(status=d.get("status") or end_ev.get("outcome"),
                    cost_usd=end_ev.get("cost_usd"),
                    usage={"in": end_ev.get("tok_in"),
                           "out": end_ev.get("tok_out"),
                           "cache_r": end_ev.get("tok_cr"),
                           "cache_w": end_ev.get("tok_cw")},
                    duration_s=d.get("duration_s"), started=d.get("started"),
                    result=(d.get("result") or "")[:16000])
    else:
        base["status"] = "orphaned"     # no live job, no job.end on record
    return base


def _ab_status(phase, ended):
    """Cohort status: the live registry phase wins; else a recorded ab.end
    event means done; else the server restarted mid-run — orphaned (D6)."""
    if phase:
        return phase
    return "done" if ended else "orphaned"


def ab_run(ab_id):
    """GET /api/ab/run — assemble the cohort from the ab.start event
    (authoritative arm list) + live JOBS / recorded job.end events + the
    latest per-arm verdicts + the ab.judge event. Registry-optional so it
    still answers after a restart. Raises KeyError on unknown ab_id."""
    ab_id = str(ab_id or "")
    evs = zs.events_query(kind="ab.start", ref=ab_id, limit=1)
    if not evs:
        raise KeyError("no such ab run")
    d = evs[0].get("data") or {}
    with _jobs_lock:
        run = _AB_RUNS.get(ab_id)
        phase = run["phase"] if run else None
    arms = []
    for meta in (d.get("arms") or []):
        jid = str(meta.get("job_id") or "")
        job = JOBS.get(jid)
        end = None
        if job is None:
            ee = zs.events_query(kind="job.end", ref=jid, limit=1)
            end = ee[0] if ee else None
        arm = _ab_arm_view(meta, job, end)
        v = zs.verdicts_query(target_kind="job", target_ref=jid, limit=1)
        if v:                     # populated by P3.2's per-arm verify stage
            arm["verdict"] = {k: v[0].get(k) for k in
                              ("id", "verdict", "crit", "major", "minor",
                               "info", "summary")}
        arms.append(arm)
    jev = zs.events_query(kind="ab.judge", ref=ab_id, limit=1)
    eev = zs.events_query(kind="ab.end", ref=ab_id, limit=1)
    return {"ab_id": ab_id, "project": evs[0].get("project") or "",
            "prompt": d.get("prompt") or "", "mode": d.get("mode"),
            "verify": bool(d.get("verify")),
            "judge_enabled": bool(d.get("judge")),
            "status": _ab_status(phase, bool(eev)), "arms": arms,
            "judge": (jev[0].get("data") or {}) if jev else None}


def ab_runs(limit=30):
    """GET /api/ab/runs — recent cohorts from ab.start events, id-desc."""
    out = []
    for e in zs.events_query(kind="ab.start",
                             limit=max(1, min(int(limit or 30), 100))):
        d = e.get("data") or {}
        with _jobs_lock:
            run = _AB_RUNS.get(e.get("ref"))
        out.append({"ab_id": e.get("ref"), "project": e.get("project") or "",
                    "prompt": (d.get("prompt") or "")[:120], "ts": e.get("ts"),
                    "n_arms": len(d.get("arms") or []),
                    "agents": [a.get("agent") for a in (d.get("arms") or [])],
                    "sequential": bool(d.get("sequential")),
                    "status": run["phase"] if run else None})
    return out


AB_JUDGE_PROMPT = """You are the judge of a cross-agent A/B comparison. {n} agent arms each ran the SAME prompt
on the same project. Rank them.

ORIGINAL PROMPT (what every arm was asked to do):
{prompt}

THE ARMS — for each: identity, mechanical metrics (gathered by the OS, trusted), an independent
hostile-review verdict of its work (trusted), and the arm's own result text (NOT trusted — the
arm wrote it about itself):

{arms}
Rules:
- Judge on how well each RESULT actually accomplishes the ORIGINAL PROMPT: correctness and
  completeness first, then the hostile-review verdicts, then efficiency (cost, tokens,
  duration) as the tiebreaker.
- An arm's own claims of success are NOT evidence of success.
- An arm with status=error or a fail verdict can still win only if every other arm is worse.
- Score every arm 0-10 (10 = fully accomplished the prompt, clean review, efficient).
- You may Read files in the project to check claims. You must not modify anything.

Output ONLY this JSON object — no prose before or after, no code fences:
{{"winner": <arm number 1..{n}>, "scores": [{{"arm": 1, "score": 0, "note": "<one line>"}}],
  "rationale": "<3-5 sentences: why the winner won>"}}
"""


def _parse_ab_judge(result, n_arms):
    """PURE judge-output parse (mirrors _clean_issues discipline): winner must
    be 1..n_arms, scores clamped 0..10 / one per arm / notes capped 300,
    rationale capped 2000. Never raises; parse_ok=False when nothing usable."""
    obj = _extract_json_obj(result or "", '{"winner"')
    if obj is None:
        return {"winner": None, "scores": [], "rationale": "",
                "parse_ok": False}
    try:
        w = int(obj.get("winner"))
    except (TypeError, ValueError):
        w = None
    if w is not None and not (1 <= w <= n_arms):
        w = None
    scores, seen = [], set()
    for s in (obj.get("scores") or [])[:16]:
        if not isinstance(s, dict):
            continue
        try:
            arm = int(s.get("arm"))
        except (TypeError, ValueError):
            continue
        if arm in seen or not (1 <= arm <= n_arms):
            continue
        seen.add(arm)
        try:
            sc = max(0, min(10, int(s.get("score"))))
        except (TypeError, ValueError):
            sc = None
        scores.append({"arm": arm, "score": sc,
                       "note": str(s.get("note") or "")[:300]})
    return {"winner": w, "scores": scores,
            "rationale": str(obj.get("rationale") or "")[:2000],
            "parse_ok": True}


def _ab_judge_prompt(prompt, arms):
    """PURE prompt assembly: one block per arm (identity, metrics, verdict,
    head+tail-capped result via _cap_text)."""
    blocks = []
    for i, a in enumerate(arms, 1):
        u = a.get("usage") or {}
        v = a.get("verdict")
        vline = ((f"{v.get('verdict')} (crit {v.get('crit')} / "
                  f"major {v.get('major')} / minor {v.get('minor')}) — "
                  f"{v.get('summary') or ''}")[:600] if v else "unavailable")
        result, _ = _cap_text(a.get("result") or "(no result captured)", 8000)
        blocks.append(
            f"ARM {i}: agent={a.get('agent')} model={a.get('model')}\n"
            f"metrics: cost_usd={a.get('cost_usd')} tokens_in={u.get('in')} "
            f"tokens_out={u.get('out')} duration_s={a.get('duration_s')} "
            f"status={a.get('status')}\n"
            f"hostile-review verdict: {vline}\n"
            f"RESULT:\n{result}\n")
    return AB_JUDGE_PROMPT.format(n=len(arms),
                                  prompt=_cap_text(prompt, 4000)[0],
                                  arms="\n".join(blocks))


# ---------------------------------------------------------------- model providers

# Ollama defaults num_ctx to 4096 and SILENTLY drops everything past it — packed
# context would vanish with no error — so every provider carries its own window.
DEFAULT_NUM_CTX = 8192
# A big model on a remote box is slow to first byte: weights load, then prefill
# over a large file. 180 s was sized for small local models and turns a healthy
# big-model call into a phantom failure.
DEFAULT_TIMEOUT_S = 600


def _int_or(v, fallback):
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else fallback


def _provider_num_ctx(prov):
    return _int_or((prov or {}).get("num_ctx"), DEFAULT_NUM_CTX)


def _provider_timeout(prov):
    return _int_or((prov or {}).get("timeout_s"), DEFAULT_TIMEOUT_S)


# ------------------------------------------------------------- provider defaults
# What ships is in defaults/providers.json: LOOPBACK ONLY. The previous seed named
# one particular LAN box (a GPU host, http://<host>:11434) in tracked source and
# shipped it to every install — a fact about one machine asserted on all of them.
# Real LAN/Tailscale endpoints live in gitignored data/providers.json, which always
# wins. Ollama seeds enabled:false: a box with no ollama running would otherwise
# show failing probes out of the box (gpu_nodes() ships {} for the same reason).
PROVIDERS_FALLBACK = {
    "seed": [{"id": "local-ollama", "name": "Ollama (local)", "type": "ollama",
              "base_url": "http://127.0.0.1:11434", "api_key": "", "enabled": False,
              "num_ctx": 8192, "timeout_s": 600, "detect": "ollama_tags"}],
    "kinds": [{"id": "ollama", "label": "Ollama", "detect": "ollama_tags",
               "loopback_ports": [11434]},
              {"id": "openai", "label": "OpenAI-compatible", "detect": "openai_models",
               "loopback_ports": [8000, 8080, 1234, 4000]}],
}


def _providers_defaults():
    """defaults/providers.json → {"seed": [...], "kinds": [...]}. A bare list is
    accepted as the seed (older shape). Missing/corrupt → the built-in fallback."""
    d = za.load_defaults("providers", {})
    if isinstance(d, list):
        d = {"seed": d}
    if not isinstance(d, dict):
        d = {}
    seed = [s for s in (d.get("seed") or []) if isinstance(s, dict) and s.get("id")]
    kinds = [k for k in (d.get("kinds") or []) if isinstance(k, dict) and k.get("id")]
    return {"seed": json.loads(json.dumps(seed or PROVIDERS_FALLBACK["seed"])),
            "kinds": json.loads(json.dumps(kinds or PROVIDERS_FALLBACK["kinds"]))}


def _seed_snapshot():
    """The whole snapshot doc: {section: [last-shipped seeds]}. A bare list is the
    legacy agents-only shape. Never raises."""
    snap = _load_json(SEED_SNAPSHOT_FILE, {})
    if isinstance(snap, list):
        snap = {"agents": snap}
    return snap if isinstance(snap, dict) else {}


def _seed_snapshot_put(section, seeds):
    """Record what we just seeded for `section`, preserving every other section
    (zenith_agents.py writes its own key into the same file)."""
    try:
        with _data_lock:
            snap = _seed_snapshot()
            snap[section] = seeds
            _save_json(SEED_SNAPSHOT_FILE, snap)
    except OSError:
        pass


def _prov_endpoint(p):
    """Identity of a provider as an ENDPOINT (type + normalised base_url). Two
    entries with different ids but the same endpoint are the same box, which is how
    an existing install keeps its own 'Ollama (this Mac)' instead of gaining a
    duplicate row when the shipped loopback seed arrives."""
    return (str((p or {}).get("type") or "").strip().lower(),
            str((p or {}).get("base_url") or "").strip().rstrip("/").lower())


def _providers_merge(seeds, existing, snapshot):
    """Three-way merge (§9), pure. Per field: a value the user never changed since
    we shipped it tracks the new default; a value they DID change is kept. New seed
    ids are appended — unless the user already has that endpoint under another id,
    or previously deleted it (absence is a choice)."""
    if not isinstance(existing, list):
        return json.loads(json.dumps(seeds))
    items = [p for p in existing if isinstance(p, dict)]
    ids = {p.get("id") for p in items}
    endpoints = {_prov_endpoint(p) for p in items}
    fresh = [s for s in seeds
             if s.get("id") in ids or _prov_endpoint(s) not in endpoints]
    return za.merge_by_id(fresh, items, snapshot or [])


def providers_load():
    """The provider list. First boot seeds data/providers.json from the shipped
    defaults; later boots three-way merge so a corrected default actually reaches an
    existing install (the old code wrote only when the file was absent, so it never
    did). data/ always wins on any field the user touched."""
    seeds = _providers_defaults()["seed"]
    snap = _seed_snapshot()
    if not PROVIDERS_FILE.exists():
        try:
            _save_json(PROVIDERS_FILE, seeds)
        except OSError:
            pass
        _seed_snapshot_put("providers", seeds)
        return seeds
    items = _load_json(PROVIDERS_FILE, None)
    if not isinstance(items, list):
        return []                                 # corrupt → today's behaviour ([])
    merged = _providers_merge(seeds, items, snap.get("providers"))
    if merged != items:
        try:
            with _data_lock:
                _save_json(PROVIDERS_FILE, merged)
        except OSError:
            pass
    if snap.get("providers") != seeds:            # only on an actual seed change
        _seed_snapshot_put("providers", seeds)
    return merged


def _crud_upsert(path, obj):
    with _data_lock:
        items = _load_json(path, [])
        oid = obj.get("id")
        if not oid:
            obj["id"] = uuid.uuid4().hex[:12]
            obj.setdefault("created", now_iso())
            items.append(obj)
        else:
            for i, it in enumerate(items):
                if it.get("id") == oid:
                    obj.setdefault("created", it.get("created", now_iso()))
                    items[i] = obj
                    break
            else:
                obj.setdefault("created", now_iso())
                items.append(obj)
        _save_json(path, items)
    return obj


def _crud_delete(path, oid):
    with _data_lock:
        items = [it for it in _load_json(path, []) if it.get("id") != oid]
        _save_json(path, items)
    return True


def _provider(pid):
    return next((p for p in providers_load() if p.get("id") == pid), None)


_PROV_BASE_CACHE = {}   # provider id -> (base_url, expires_epoch)


def _provider_bases(prov):
    """Candidate base_urls in preference order: the primary base_url, then any
    `fallbacks` — so one provider reaches the same box on the LAN AND over Tailscale."""
    out = []
    for b in [prov.get("base_url")] + list(prov.get("fallbacks") or []):
        b = str(b or "").strip().rstrip("/")
        if b and b not in out:
            out.append(b)
    return out


def _base_live(base, ptype="ollama", api_key=None, timeout=2.5):
    """Fast liveness probe of one endpoint (ollama /api/version, else /v1/models).
    2.5s tolerates a cold Tailscale/relay connection to a fallback base without
    prematurely giving up and using the (unreachable-from-here) primary."""
    base = (base or "").rstrip("/")
    if not base:
        return False
    url = base + ("/api/version" if ptype == "ollama" else "/v1/models")
    try:
        req = urllib.request.Request(url)
        if api_key and ptype != "ollama":
            req.add_header("Authorization", "Bearer " + api_key)
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except Exception:
        return False


def _provider_base(prov):
    """First REACHABLE candidate base_url (preferring the primary, e.g. the LAN
    address for speed), cached ~30s. If none probe live, returns the primary so the
    real request surfaces the error normally. Single-endpoint providers skip the probe."""
    cands = _provider_bases(prov)
    if len(cands) <= 1:
        return cands[0] if cands else ""
    pid = prov.get("id") or cands[0]
    now = time.time()
    hit = _PROV_BASE_CACHE.get(pid)
    if hit and hit[1] > now and hit[0] in cands:
        return hit[0]
    for b in cands:
        if _base_live(b, prov.get("type", "ollama"), prov.get("api_key")):
            _PROV_BASE_CACHE[pid] = (b, now + 30)
            return b
    _PROV_BASE_CACHE[pid] = (cands[0], now + 5)
    return cands[0]


def list_models(pid, base_url=None, ptype=None, api_key=None):
    """List a provider's models, either by saved id OR inline base_url/type (so the
    editor can preview models before the provider is saved)."""
    if base_url:
        prov = {"base_url": base_url, "type": ptype or "ollama", "api_key": api_key or ""}
    else:
        prov = _provider(pid)
        if not prov:
            return {"error": "no such provider", "models": []}
    base = _provider_base(prov)
    try:
        if prov.get("type") == "ollama":
            req = urllib.request.Request(base + "/api/tags")
            data = json.loads(urllib.request.urlopen(req, timeout=4).read())
            return {"models": [m.get("name") for m in data.get("models", [])]}
        req = urllib.request.Request(base + "/v1/models")
        if prov.get("api_key"):
            req.add_header("Authorization", "Bearer " + prov["api_key"])
        data = json.loads(urllib.request.urlopen(req, timeout=4).read())
        return {"models": [m.get("id") for m in data.get("data", [])]}
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        return {"error": str(e), "models": []}


def provider_test(base_url, ptype, api_key=None):
    """Probe a provider endpoint (6s): ollama /api/tags, openai /v1/models."""
    base = (base_url or "").rstrip("/")
    t0 = time.time()
    try:
        if ptype == "ollama":
            req = urllib.request.Request(base + "/api/tags")
            data = json.loads(urllib.request.urlopen(req, timeout=6).read())
            models = [m.get("name") for m in data.get("models", [])]
        else:
            req = urllib.request.Request(base + "/v1/models")
            if api_key:
                req.add_header("Authorization", "Bearer " + api_key)
            data = json.loads(urllib.request.urlopen(req, timeout=6).read())
            models = [m.get("id") for m in data.get("data", [])]
        return {"ok": True, "models": models,
                "latency_ms": int((time.time() - t0) * 1000)}
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        return {"ok": False, "models": [], "error": str(e),
                "latency_ms": int((time.time() - t0) * 1000)}


def _agent_probe(agent_id):
    """Resolve an agent's bin + run `--version` (10 s). Probes disabled
    entries too — the UI 'test agent' flow runs before enabling. OQ2: a
    missing aider returns the pip-install hint instead of auto-installing."""
    entry = next((a for a in za.load_agents() if a.get("id") == agent_id), None)
    if not entry:
        return {"ok": False, "error": "no such agent"}
    binp = za.resolve_bin(entry)
    installed = bool(binp) and (os.path.isfile(binp) if os.sep in str(binp)
                                else bool(shutil.which(str(binp))))
    out = {"agent": agent_id, "bin": binp, "installed": installed,
           "enabled": bool(entry.get("enabled"))}
    if not installed:
        out["ok"] = False
        out["error"] = "binary not found"
        if agent_id == "aider":
            out["hint"] = ("pip install aider-chat — or POST /api/install-deps "
                           "{\"packages\":[\"aider-chat\"]}")
        return out
    try:
        r = subprocess.run([binp, "--version"], capture_output=True, text=True,
                           timeout=10, env=_augment_path(dict(os.environ)))
        out["ok"] = r.returncode == 0
        out["version"] = (r.stdout or r.stderr or "").strip()[:200]
        if r.returncode != 0:
            out["error"] = f"exit {r.returncode}"
    except (OSError, subprocess.TimeoutExpired) as e:
        out["ok"] = False
        out["error"] = str(e)
    return out


# ------------------------------------------------------------- detection registry
# "Never claim a model exists" (design §2). Every probe below is time-bounded and
# returns an empty/False result on ANY failure, so a machine with nothing installed
# — the common case, and the one that must stay fast — gets a clean, quick answer
# instead of a wall of errors. zenith_agents.py may not do HTTP or subprocess (its
# boundary rule), so the probes live here and their RESULTS are handed to
# za.resolve_models(adapter, detected).

DETECT_HTTP_TIMEOUT = 1.5     # per endpoint; a refused loopback connect is instant
DETECT_BIN_TIMEOUT = 3.0      # per `{bin} --version`
DETECT_TTL = 30.0             # answer cache — the UI may poll this
DETECT_MAX_PROBES = 24        # hard cap on probes per sweep
DETECT_DEADLINE = 6.0         # hard wall-clock cap on one sweep


def _detect_json(url, api_key=None, timeout=DETECT_HTTP_TIMEOUT):
    """GET a JSON doc. None on any failure whatsoever — never raises, never hangs
    past `timeout`."""
    try:
        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", "Bearer " + str(api_key))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read(2_000_000).decode("utf-8", "replace"))
    except Exception:
        return None


def detect_ollama_tags(base, api_key=None, timeout=DETECT_HTTP_TIMEOUT):
    """GET {base}/api/tags -> [model name]. [] when absent/unreachable/garbage."""
    data = _detect_json(str(base or "").rstrip("/") + "/api/tags", None, timeout)
    if not isinstance(data, dict):
        return []
    return [str(m.get("name")) for m in (data.get("models") or [])
            if isinstance(m, dict) and m.get("name")]


def detect_openai_models(base, api_key=None, timeout=DETECT_HTTP_TIMEOUT):
    """GET {base}/v1/models -> [id]. One probe covers vLLM, llama.cpp server, LM
    Studio, LiteLLM and anything else OpenAI-compatible — they all converged on this
    endpoint, so ZENITH needs no per-vendor probe."""
    data = _detect_json(str(base or "").rstrip("/") + "/v1/models", api_key, timeout)
    if not isinstance(data, dict):
        return []
    return [str(m.get("id")) for m in (data.get("data") or [])
            if isinstance(m, dict) and m.get("id")]


def detect_binary_version(binary, timeout=DETECT_BIN_TIMEOUT):
    """`{bin} --version` -> (present: bool, version: str). Presence is the point;
    the version string is a bonus and is "" when the binary declines to give one."""
    b = str(binary or "")
    if not b:
        return False, ""
    path = shutil.which(b) if os.sep not in b else (
        b if os.path.isfile(b) and os.access(b, os.X_OK) else "")
    if not path:
        return False, ""
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True,
                           timeout=timeout, env=_augment_path(dict(os.environ)))
        lines = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        return True, (lines[0][:120] if lines else "")
    except Exception:
        return True, ""            # on PATH but unrunnable: still present


DETECTORS = {"ollama_tags": detect_ollama_tags,
             "openai_models": detect_openai_models,
             "binary_version": detect_binary_version}

_detect_cache = {"t": 0.0, "data": None}
_detect_lock = threading.Lock()


def _detect_candidates():
    """Endpoints worth probing: every loopback port the shipped `kinds` registry
    names (that is the whole point of shipping kinds instead of hosts), plus each
    saved+enabled provider — which is where a real LAN/Tailscale box shows up."""
    out, seen = [], set()

    def add(eid, name, ptype, base, source, origin, api_key=""):
        base = str(base or "").strip().rstrip("/")
        if not base or (ptype, base) in seen or len(out) >= DETECT_MAX_PROBES:
            return
        seen.add((ptype, base))
        out.append({"id": eid, "name": name, "type": ptype, "base_url": base,
                    "source": source, "origin": origin, "api_key": api_key})

    for kind in _providers_defaults()["kinds"]:
        kid = str(kind.get("id") or "")
        src = str(kind.get("detect") or "")
        for port in kind.get("loopback_ports") or []:
            try:
                port = int(port)
            except (TypeError, ValueError):
                continue
            add("%s:%d" % (kid, port), kind.get("label") or kid, kid,
                "http://127.0.0.1:%d" % port, src, "default")
    for p in providers_load():
        if not isinstance(p, dict) or p.get("enabled") is False:
            continue
        ptype = str(p.get("type") or "ollama")
        add(str(p.get("id") or ""), p.get("name") or p.get("id"), ptype,
            p.get("base_url"), p.get("detect")
            or ("ollama_tags" if ptype == "ollama" else "openai_models"),
            "saved", p.get("api_key") or "")
    return out


def _parallel(fns, deadline=DETECT_DEADLINE):
    """Run zero-arg callables concurrently, collect results, and give up at the
    deadline (a stuck probe must never hold an HTTP request open)."""
    results = [None] * len(fns)

    def run(i, fn):
        try:
            results[i] = fn()
        except Exception:
            results[i] = None
    threads = [threading.Thread(target=run, args=(i, f), daemon=True)
               for i, f in enumerate(fns)]
    for t in threads:
        t.start()
    end = time.time() + deadline
    for t in threads:
        t.join(max(0.0, end - time.time()))
    return results


def detect_all(refresh=False):
    """What is ACTUALLY on this machine: which agent binaries resolve, which
    inference endpoints answer, and each one's real model list. Cached ~30s.
    Returns fast and clean on a machine with nothing installed."""
    now = time.time()
    with _detect_lock:
        hit = _detect_cache.get("data")
        if hit and not refresh and (now - _detect_cache["t"]) < DETECT_TTL:
            return dict(hit, cached=True)
        t0 = time.time()
        agents = [a for a in za.load_agents() if isinstance(a, dict) and a.get("id")]
        cands = _detect_candidates()
        probes = [(lambda e=e: DETECTORS.get(e["source"], detect_openai_models)(
            e["base_url"], e.get("api_key"), DETECT_HTTP_TIMEOUT)) for e in cands]
        probes += [(lambda a=a: detect_binary_version(za.resolve_bin(a),
                                                      DETECT_BIN_TIMEOUT))
                   for a in agents]   # global read at call time: tunable at runtime
        res = _parallel(probes)
        endpoints, models = [], {k: [] for k in ("ollama_tags", "openai_models")}
        for e, got in zip(cands, res[:len(cands)]):
            found = [str(m) for m in (got or [])]
            # alive == "answered AND has something to offer": an endpoint that
            # serves no model is nothing a user can pick, so we do not claim it.
            endpoints.append({k: e[k] for k in
                              ("id", "name", "type", "base_url", "source", "origin")}
                             | {"alive": bool(found), "models": found,
                                "model_count": len(found)})
            bucket = models.setdefault(e["source"], [])
            for m in found:
                if m not in bucket:
                    bucket.append(m)
        agents_out = []
        for a, got in zip(agents, res[len(cands):]):
            present, version = got if isinstance(got, tuple) else (False, "")
            agents_out.append({"id": a["id"], "label": a.get("label") or a["id"],
                               "bin": a.get("bin") or "",
                               # path only when it resolved — resolve_bin() falls back
                               # to the bare name, which would read as "found here"
                               "path": (shutil.which(za.resolve_bin(a) or "")
                                        or za.resolve_bin(a) or "") if present else "",
                               "present": bool(present), "version": version,
                               "enabled": bool(a.get("enabled"))})
        out = {"ok": True, "checked_at": now_iso(),
               "elapsed_ms": int((time.time() - t0) * 1000), "cached": False,
               "sources": sorted(DETECTORS), "agents": agents_out,
               "endpoints": endpoints, "models": models}
        _detect_cache["data"], _detect_cache["t"] = out, time.time()
        return out


def detected_models(refresh=False):
    """{source -> [model id]} for za.resolve_models(). Cached-only by default, so
    the hot model-list routes never pay for a probe sweep."""
    with _detect_lock:
        hit = _detect_cache.get("data")
    if hit and not refresh:
        return dict(hit.get("models") or {})
    return dict(detect_all(refresh=refresh).get("models") or {})


def agent_models(adapter, refresh=False):
    """Model ids for an agent: za.resolve_models() when the shared layer provides it
    (kind=static passes through, kind=detect resolves against the probes above),
    otherwise today's static list. Never raises."""
    try:
        fn = getattr(za, "resolve_models", None)
        if callable(fn):
            return [str(m) for m in fn(adapter, detected_models(refresh)) or []]
    except Exception:
        pass
    return za.list_models(adapter)


# ------------------------------------------------------------ export / import (§5)
# One bundle in, one bundle out — this is what makes a setup portable between
# machines and reviewable in git. Secrets are redacted unless explicitly asked for,
# and a malformed bundle changes NOTHING (validate everything before writing a byte).

CONFIG_BUNDLE_VERSION = 1
_BUNDLE_SECTIONS = ("agents", "providers", "models", "config", "statusline")


def _bundle_targets():
    return {"agents": AGENTS2_FILE, "providers": PROVIDERS_FILE,
            "models": MODELS_FILE, "config": CONFIG_FILE,
            "statusline": STATUSLINE_CONFIG}


def config_export(include_secrets=False):
    """The whole configuration as one JSON doc. Redacted by default: provider api
    keys become "" with an api_key_set flag, and the config goes through the same
    config_redacted() the Settings UI already uses."""
    provs = json.loads(json.dumps(providers_load()))
    cfg = config_load()
    if not include_secrets:
        for p in provs:
            if isinstance(p, dict):
                p["api_key_set"] = bool(p.get("api_key"))
                p["api_key"] = ""
        cfg = config_redacted(cfg)
    return {"version": CONFIG_BUNDLE_VERSION, "exported_at": now_iso(),
            "zenith_release": ZENITH_RELEASE, "secrets_included": bool(include_secrets),
            "agents": za.load_agents(), "providers": provs,
            "models": _models_defaults(), "config": cfg,
            "statusline": statusline_config_load()}


def _bundle_check(bundle):
    """Empty string when the bundle is importable, else the reason it is not.
    Checked BEFORE any file is touched: an invalid bundle must leave the machine
    exactly as it was."""
    if not isinstance(bundle, dict):
        return "bundle must be a JSON object"
    ver = bundle.get("version")
    if not isinstance(ver, int) or ver < 1:
        return "missing or invalid bundle version"
    if ver > CONFIG_BUNDLE_VERSION:
        return "bundle version %s is newer than this ZENITH (%s)" % (
            ver, CONFIG_BUNDLE_VERSION)
    if not any(bundle.get(s) is not None for s in _BUNDLE_SECTIONS):
        return "bundle contains none of: " + ", ".join(_BUNDLE_SECTIONS)
    for sec in ("agents", "providers"):
        val = bundle.get(sec)
        if val is None:
            continue
        if not isinstance(val, list):
            return "%s must be a list" % sec
        for it in val:
            if not isinstance(it, dict) or not str(it.get("id") or "").strip():
                return "every %s entry needs an id" % sec[:-1]
    for sec in ("models", "config", "statusline"):
        if bundle.get(sec) is not None and not isinstance(bundle[sec], dict):
            return "%s must be an object" % sec
    return ""


def config_import(bundle):
    """Validate → back up every target it will touch → write → re-resolve → re-probe.
    Redacted secrets ("" api keys, "" NM token) keep whatever this machine already
    has, so an export/import round trip never silently wipes a credential."""
    err = _bundle_check(bundle)
    if err:
        return {"ok": False, "error": err}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    targets, applied, backups = _bundle_targets(), [], {}
    payload = {}
    if bundle.get("providers") is not None:            # keep un-exported secrets
        old = {p.get("id"): p for p in providers_load() if isinstance(p, dict)}
        provs = json.loads(json.dumps(bundle["providers"]))
        for p in provs:
            p.pop("api_key_set", None)
            if not p.get("api_key"):
                p["api_key"] = (old.get(p.get("id")) or {}).get("api_key", "")
        payload["providers"] = provs
    if bundle.get("config") is not None:
        cfg = json.loads(json.dumps(bundle["config"]))
        api = ((cfg.get("integrations") or {}).get("nexusmind_api")
               if isinstance(cfg.get("integrations"), dict) else None)
        if isinstance(api, dict):
            api.pop("token_set", None)
            if not api.get("token"):
                api["token"] = _cfg_get(config_load(),
                                        "integrations.nexusmind_api.token", "") or ""
                if not api["token"]:
                    api.pop("token", None)
        payload["config"] = cfg
    for sec in ("agents", "models", "statusline"):
        if bundle.get(sec) is not None:
            payload[sec] = json.loads(json.dumps(bundle[sec]))
    try:
        with _data_lock:
            for sec, obj in payload.items():
                path = targets[sec]
                if path.exists():
                    bak = path.with_name(path.name + ".bak-" + stamp)
                    bak.write_bytes(path.read_bytes())
                    backups[sec] = str(bak)
                _save_json(path, obj)
                applied.append(sec)
    except OSError as e:
        return {"ok": False, "error": "write failed: %s" % e,
                "applied": applied, "backups": backups}
    if "config" in applied:
        config_apply(config_load())
    global _MODELS, MODEL_MAP, EFFORT_TOKENS, VERIFY_LADDER, \
        CONTEXT_WINDOWS_DEFAULT, CONTEXT_WINDOW_FALLBACK
    _MODELS = _models_defaults()                       # re-resolve the model tables
    MODEL_MAP, EFFORT_TOKENS = _MODELS["aliases"], _MODELS["effort_tokens"]
    VERIFY_LADDER = _MODELS["verify_ladder"]
    CONTEXT_WINDOWS_DEFAULT = _MODELS["context_windows"]
    CONTEXT_WINDOW_FALLBACK = _MODELS["context_window_fallback"]
    return {"ok": True, "applied": applied, "backups": backups,
            "detect": detect_all(refresh=True),
            "capabilities": capabilities(refresh=True)}


def _chat_extract_full(ptype, data):
    """Pull the full assistant text from a non-streamed provider response."""
    if ptype == "ollama":
        return (data.get("message") or {}).get("content") or ""
    ch = (data.get("choices") or [{}])[0]
    return (ch.get("message") or {}).get("content") or ""


def provider_chat(provider_id, model, messages, options=None, fmt=None):
    """Non-streamed chat against a provider. Used by loop runners + /api/chat
    stream:false. `fmt` is a JSON schema for constrained decoding — the model
    then CANNOT emit anything but conforming JSON, which is what makes a small
    local model usable as a reviewer. Returns {ok, text, error?}."""
    prov = _provider(provider_id)
    if not prov:
        return {"ok": False, "error": "no such provider", "text": ""}
    base = _provider_base(prov)
    headers = {"Content-Type": "application/json"}
    if prov.get("type") == "ollama":
        url = base + "/api/chat"
        opts = dict(options or {})
        opts.setdefault("num_ctx", _provider_num_ctx(prov))
        payload = {"model": model, "messages": messages, "stream": False,
                   "options": opts}
        if fmt:
            payload["format"] = fmt          # ollama ≥0.5 structured outputs
    else:
        url = base + "/v1/chat/completions"
        if prov.get("api_key"):
            headers["Authorization"] = "Bearer " + prov["api_key"]
        payload = {"model": model, "messages": messages, "stream": False}
        if options and "temperature" in options:
            payload["temperature"] = options["temperature"]
        if fmt:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "review", "strict": True, "schema": fmt}}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    try:
        data = json.loads(
            urllib.request.urlopen(req, timeout=_provider_timeout(prov)).read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "error": str(e), "text": ""}
    return {"ok": True, "text": _chat_extract_full(prov.get("type"), data)}


def _provider_openai_env(provider_id):
    """extra_env that points an OpenAI-compatible CLI agent (aider → litellm) at a
    saved provider. base_url + key are resolved from data/providers.json at launch.
    (This docstring used to claim no LAN address was hardcoded anywhere; that was
    false — providers_load() seeded one specific LAN host in tracked
    source until the config-externalisation pass. It is true NOW: what ships is
    defaults/providers.json, loopback only, and every real host is machine state in
    the gitignored data/ file.)"""
    prov = _provider(provider_id) or {}
    base = (_provider_base(prov) or "").rstrip("/") + "/v1"
    key = prov.get("api_key") or "sk-noauth"      # aider/openai SDK want a non-empty key
    return {"OPENAI_API_BASE": base, "OPENAI_BASE_URL": base, "OPENAI_API_KEY": key}


# ------------------------------------------------------- provider run context
# A CLI agent runs with cwd=project and opens files itself. A provider run is one
# HTTP call to a model that has never seen the repo — so whatever it is meant to
# read has to travel WITH the prompt. `context` says what to pack.
# (The via_aider path is the other answer: aider gets real repo access. This one
# is for raw completions, where there is no agent to do the reading.)

CONTEXT_CHOICES = ("none", "diff", "diff-head", "files")
MAX_CTX_FILES = 40          # per glob pattern

PROVIDER_CONTEXT_SYS = (
    "You are reviewing material from a codebase you cannot browse. Everything you "
    "may reason about is included in this message — you have no file access, no "
    "shell, and no way to fetch more. If the material is truncated or does not "
    "contain what the task asks about, say so plainly instead of guessing. "
    "Cite file:line wherever you can."
)


def _ctx_char_budget(num_ctx):
    """Chars of material that fit in num_ctx tokens, leaving 40% of the window
    for the instructions and the model's own answer. ~3.5 chars/token."""
    return max(2000, int(_int_or(num_ctx, DEFAULT_NUM_CTX) * 0.6 * 3.5))


def _ctx_project(project):
    """Context may only be read from inside claudeProjects — same containment
    rule the verify materials use (§5.2)."""
    p = Path(project or "").resolve()
    return str(p) if (p == PROJECTS_ROOT or p.parent == PROJECTS_ROOT) else ""


def _pack_files(root, globs, cap):
    """Inline whole files by comma-separated glob, stopping at the char budget."""
    pats = [g.strip() for g in (globs or "").split(",") if g.strip()]
    if not pats:
        return "files — unavailable", "(no file globs set)", False
    chunks, seen, used, trunc = [], [], 0, False
    for pat in pats:
        if pat.startswith("/") or ".." in pat:
            continue                       # globs stay inside the project
        for f in sorted(Path(root).glob(pat))[:MAX_CTX_FILES]:
            rel = str(f.relative_to(root))
            if not f.is_file() or rel in seen or cap - used < 500:
                trunc = trunc or cap - used < 500
                continue
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            if len(text) > cap - used:
                text, _ = _cap_text(text, cap - used)
                trunc = True
            seen.append(rel)
            used += len(text)
            chunks.append(f"----- {rel} -----\n{text}")
    if not chunks:
        return "files — unavailable", "(no files matched " + ", ".join(pats) + ")", False
    return f"{len(chunks)} file(s): " + ", ".join(seen), "\n\n".join(chunks), trunc


def _pack_context(project, kind, globs, cap):
    """Build the material a provider run ships with its prompt.
    Returns (header, material, truncated); header '' means nothing was packed."""
    kind = (kind or "none").strip()
    if kind not in CONTEXT_CHOICES or kind == "none":
        return "", "", False
    proj = _ctx_project(project)
    if not proj:
        return kind + " — unavailable", "(project is outside claudeProjects)", False
    if kind == "files":
        return _pack_files(proj, globs, cap)
    if not (Path(proj) / ".git").exists():
        return kind + " — unavailable", "(project is not a git repo)", False
    try:
        if kind == "diff-head":
            header = "git show HEAD --stat -p"
            body = _git(["show", "HEAD", "--stat", "-p"], proj)
        else:
            header = "git status --porcelain + git diff (uncommitted)"
            body = ("git status --porcelain:\n" + _git(["status", "--porcelain"], proj)
                    + "\ngit diff:\n" + _git(["diff"], proj))
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        return kind + " — unavailable", f"(git failed: {str(e)[:200]})", False
    if not body.strip():
        return header, "(no changes — the working tree is clean)", False
    material, trunc = _cap_text(body, cap)
    return header, material, trunc


def _provider_messages(prompt, header, material):
    """Prompt alone when nothing was packed; otherwise a system rule + the
    material fenced off from the task text so the model can't confuse them."""
    if not header:
        return [{"role": "user", "content": prompt}]
    return [{"role": "system", "content": PROVIDER_CONTEXT_SYS},
            {"role": "user", "content": f"{prompt}\n\n--- MATERIAL ({header}) ---\n"
                                        f"{material}\n--- END MATERIAL ---"}]


# ------------------------------------------------------------- local reviewer
# Making a small local model a useful reviewer is three things, none of them the
# model: (1) constrained decoding, so output is always a parseable issue list;
# (2) ONE FILE PER CALL — small focused windows are where these models are
# strongest, and it takes the context ceiling off the review entirely; (3) a
# rigid prompt demanding a verbatim citation, enforced by _ground_issues below.

PROVIDER_TASKS = ("chat", "review")

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string",
                                 "enum": ["crit", "major", "minor", "info"]},
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["severity", "claim", "evidence", "location"],
            },
        },
    },
    "required": ["issues"],
}

REVIEW_SYS = """You are a code reviewer. You are shown ONE unit of code at a time and you
report only what is wrong with it.

Severities: crit = wrong result, data loss, security hole, or corruption;
major = a likely bug or an unmet requirement; minor = edge case or sloppiness;
info = an observation worth recording.

Hard rules:
- EVERY issue needs `evidence`: text copied VERBATIM from the material below.
  If you cannot copy the exact line you are talking about, do not report it.
- `location` is file:line, or the hunk header, when you can tell.
- Judge ONLY the material shown. Do not speculate about code you cannot see,
  and never report a missing import, helper or test as an issue — it is
  probably there, just not in this excerpt.
- Clean code is a normal outcome. Return an empty issues list rather than
  inventing something to say."""

REVIEW_USER = "UNIT UNDER REVIEW: {name}\n{focus}\n{material}"


def _chunk_name(line, marker):
    """'diff --git a/x/y.py b/x/y.py' -> 'x/y.py'; '----- a.py -----' -> 'a.py'."""
    if marker != "diff --git ":
        return line.strip().strip("- ") or "?"
    parts = line.split()
    return parts[-1][2:] if len(parts) >= 4 and parts[-1].startswith("b/") \
        else line.strip()


def _review_chunks(material, cap):
    """Split packed material into one chunk per file — on 'diff --git' for
    diffs and on the '----- path -----' banner for files mode. Anything bigger
    than the budget is head/tail capped; unrecognized material stays one chunk."""
    if not material.strip():
        return []
    marker = None
    if "\ndiff --git " in "\n" + material:
        marker = "diff --git "
    elif "\n----- " in "\n" + material:
        marker = "----- "
    if not marker:
        return [("(whole material)", _cap_text(material, cap)[0])]
    parts, cur_name, cur = [], None, []
    for line in material.splitlines(keepends=True):
        if line.startswith(marker):
            if cur_name is not None:
                parts.append((cur_name, "".join(cur)))
            cur_name, cur = _chunk_name(line, marker), [line]
        elif cur_name is not None:
            cur.append(line)
        # anything before the first marker is preamble — the `git status`
        # header, or `git show`'s commit block. Not a reviewable unit: burning
        # a model call on it produced a bogus '?' finding.
    if cur_name is not None:
        parts.append((cur_name, "".join(cur)))
    return [(n, _cap_text(t, cap)[0]) for n, t in parts]


def _norm_ws(s):
    return " ".join(str(s or "").split())


def _ground_issues(issues, material):
    """Drop findings whose `evidence` is not literally present in the material.

    The prompt demands a verbatim quote; this is what ENFORCES it. Small models
    narrate ("the LIKE operator is case-insensitive") instead of quoting, and an
    ungrounded finding is precisely the confabulation this pipeline must never
    surface. Whitespace-normalized so a quote that drops the diff's +/- column
    or reflows indentation still matches. -> (kept, dropped)"""
    hay = _norm_ws(material).lower()
    kept, dropped = [], 0
    for i in issues:
        ev = _norm_ws(i.get("evidence")).lower()
        if len(ev) >= 8 and ev in hay:
            kept.append(i)
        else:
            dropped += 1
    return kept, dropped


def _run_provider_review(job, provider_id, model, chunks, focus=""):
    """One constrained call per chunk; issues accumulate across them. A chunk
    that fails is reported in the console and skipped — a dead model mid-review
    must not silently shrink the finding list. `focus` is the job's own prompt,
    carried into every unit so preset wording still steers the review."""
    focus = (f"FOCUS REQUESTED BY THE OPERATOR: {focus.strip()}\n"
             if focus.strip() else "")
    issues, failed, ungrounded = [], 0, 0
    for i, (name, text) in enumerate(chunks, 1):
        if job.get("status") == "stopped":
            job["output"].append(f"[review] stopped after {i - 1}/{len(chunks)}")
            break
        job["output"].append(f"[review {i}/{len(chunks)}] {name} — {len(text)} chars")
        r = provider_chat(provider_id, model,
                          [{"role": "system", "content": REVIEW_SYS},
                           {"role": "user", "content": REVIEW_USER.format(
                               name=name, focus=focus, material=text)}],
                          fmt=REVIEW_SCHEMA)
        if not r.get("ok"):
            failed += 1
            job["output"].append(f"  ! {r.get('error')}")
            continue
        obj = _extract_verdict_json(r.get("text") or "")
        if obj is None:
            failed += 1
            job["output"].append("  ! unparseable response (model ignored the schema)")
            continue
        got = _clean_issues(obj.get("issues"))
        got, dropped = _ground_issues(got, text)
        ungrounded += dropped
        for g in got:
            if not g["location"] or g["location"] in (".", "?"):
                g["location"] = name
        issues.extend(got)
        job["output"].append(
            f"  → {len(got)} issue(s)"
            + (f", {dropped} dropped as unquotable" if dropped else ""))
    return issues, failed, ungrounded


def _record_provider_verdict(job, issues, chunks, failed, ungrounded=0):
    """Store a local review in the SAME verdicts table a Claude ringer writes
    to, keyed on this job — so the mission-log chip and the Obs verdict view
    light up for it exactly as they do for a frontier review."""
    n = {s: sum(1 for i in issues if i["severity"] == s) for s in _SEVERITIES}
    verdict = _verdict_rollup(n["crit"], n["major"], n["minor"],
                              job.get("rc") == 0, failed < len(chunks))
    summary = (f"{len(issues)} issue(s) over {len(chunks)} unit(s) — "
               f"{n['crit']} crit, {n['major']} major, {n['minor']} minor, "
               f"{n['info']} info."
               + (f" {failed} unit(s) FAILED to review." if failed else "")
               + (f" {ungrounded} finding(s) dropped: evidence not found in the "
                  f"material." if ungrounded else ""))
    mech = {"runner": "provider", "provider": job.get("provider"),
            "provider_name": job.get("provider_name"),
            "context": job.get("context_label"),
            "context_chars": job.get("context_chars"),
            "units": len(chunks), "units_failed": failed,
            "issues_ungrounded": ungrounded}
    vid = zs.verdict_insert(
        target_kind="job", target_ref=job["id"], project=job.get("project") or "",
        verify_job=job["id"], model=job.get("model") or "", verdict=verdict,
        summary=summary, issues=issues, mech=mech, crit=n["crit"],
        major=n["major"], minor=n["minor"], info=n["info"],
        agent="provider", cost_usd=None)
    eid = zs.emit("verify.end", project=job.get("project") or "",
                  ref=job["id"], outcome=verdict, actor="verify",
                  agent="provider",
                  data={"verdict_id": vid, "verify_id": None, "verdict": verdict,
                        "crit": n["crit"], "major": n["major"],
                        "minor": n["minor"], "info": n["info"],
                        "trigger": "provider_review"})
    if eid and vid:
        zs.verdict_set_event(vid, eid)
    return verdict, summary


def spawn_provider_job(project, prompt, provider_id, model, ab_id=None,
                       label=None, loop_id=None, saved_id=None,
                       context="none", context_globs="", task="chat"):
    """Run a raw provider completion (local LAN or remote OpenAI-compatible,
    or ollama) as a FIRST-CLASS job so the A/B pipeline (verify + judge) drives it
    exactly like an agent arm. There is no workspace or adapter, so whatever the
    model is meant to read must be PACKED and sent: `context` selects that
    material (none → the original prompt-only behaviour, byte-identical).
    `task='review'` swaps the single free-form call for one constrained call per
    file plus a stored verdict. Async (thread) like spawn_job, so
    _job_finished → _ab_on_job_end fires through the normal completion hook."""
    job_id = uuid.uuid4().hex[:12]
    prov = _provider(provider_id)
    cap = _ctx_char_budget(_provider_num_ctx(prov))
    header, material, trunc = _pack_context(project, context, context_globs, cap)
    job = {"id": job_id, "project": project, "prompt": prompt, "model": model,
           "mode": "default", "status": "running", "output": [], "rc": None,
           "started": now_iso(), "ended": None, "effort": None,
           "loop_id": loop_id, "label": label, "saved_id": saved_id,
           "verify_id": None, "ab_id": ab_id, "agent": "provider",
           "provider": provider_id, "provider_name": (prov or {}).get("name"),
           # `context` is the requested kind (round-trips through rerun/dup);
           # `context_label` describes what actually got packed
           "context": context or "none", "context_label": header or None,
           "context_chars": len(material), "context_globs": context_globs,
           "context_truncated": trunc, "task": task or "chat",
           "budget": None, "add_dir": None}
    with _jobs_lock:
        JOBS[job_id] = job
    job["output"].append(f"[provider] {(prov or {}).get('name') or provider_id}"
                         f" · {model} · num_ctx {_provider_num_ctx(prov)}"
                         f" · task {task or 'chat'}")
    if header:
        job["output"].append(f"[context] {header} — {len(material)} chars"
                             + (" (TRUNCATED to fit the window)" if trunc else ""))
    elif context and context != "none":
        job["output"].append("[context] none — the model sees only the prompt")
    zs.emit("job.spawn", project=project or "", ref=job_id, outcome="ok",
            actor=_job_actor(job), agent="provider",
            data={"kind": "provider", "model": model, "mode": None,
                  "effort": None, "budget": None, "label": label,
                  "loop_id": loop_id, "saved_id": saved_id, "verify_id": None,
                  "skills": [], "prompt": (prompt or "")[:2000],
                  "add_dir": None, "provider": provider_id, "ab_id": ab_id,
                  "task": task or "chat", "context": context or "none",
                  "context_label": header or None,
                  "context_chars": len(material), "context_truncated": trunc})

    def run_chat():
        try:
            r = provider_chat(provider_id, model,
                              _provider_messages(prompt, header, material))
        except Exception as e:                 # provider_chat is defensive, but a
            r = {"ok": False, "error": str(e)}  # bad record must not strand the arm
        if r.get("ok"):
            job["rc"] = 0
            job["result"] = (r.get("text") or "").strip()
            job["output"].append(job["result"] or "(empty completion)")
        else:
            job["rc"] = -1
            job["result"] = ""
            job["output"].append("provider error: " + str(r.get("error") or "unknown"))

    def run_review():
        # one file per call, so the review is bounded by the repo, not the window
        chunks = _review_chunks(material, cap)
        if not chunks:
            job["rc"] = -1
            job["result"] = "nothing to review — the packed context was empty"
            job["output"].append(job["result"])
            return
        job["output"].append(f"[review] {len(chunks)} unit(s), one call each")
        issues, failed, ungrounded = _run_provider_review(
            job, provider_id, model, chunks, focus=prompt)
        job["rc"] = 0 if failed < len(chunks) else -1
        verdict, summary = _record_provider_verdict(job, issues, chunks, failed,
                                                    ungrounded)
        lines = [f"## {verdict.upper()} — {summary}", ""]
        for i in sorted(issues, key=lambda x: _SEVERITIES.index(x["severity"])):
            lines.append(f"- **{i['severity']}** `{i['location']}` — {i['claim']}")
        job["result"] = "\n".join(lines)
        job["output"].append("")
        job["output"].extend(lines)

    def run():
        (run_review if task == "review" else run_chat)()
        if job["status"] != "stopped":         # /api/job/stop may have labeled it
            job["status"] = "done" if job["rc"] == 0 else "error"
        job["ended"] = now_iso()
        _job_finished(job)                     # emits job.end + fires the A/B hook

    threading.Thread(target=run, daemon=True).start()
    return job_id


# ---- constrained generation: generate -> verify -> retry ----------------------
# A "check" is a small dict {rule, target?, ...params} from a FIXED predefined set.
# No caller-supplied code ever runs (trust-boundary rule) — only these rules — so this
# is safe to expose over the API. Each check returns an error string (fed back to the
# model as targeted feedback) or None when it passes.
def _cg_slice(text, target):
    """Select the slice of `text` a check applies to. target: 'all' | 'line:N' | 'sentence:N'."""
    target = target or "all"
    if target == "all":
        return text.strip()
    kind, _, idx = str(target).partition(":")
    try:
        i = int(idx)
    except ValueError:
        return text.strip()
    body = text.strip()
    if kind == "line":
        segs = [l.strip() for l in body.splitlines() if l.strip()]
    elif kind == "sentence":
        segs = [l.strip() for l in body.splitlines() if l.strip()]   # models often 1 sentence/line
        if len(segs) < 2:
            segs = [p.strip() for p in re.split(r"(?<=[.!?])\s+", body) if p.strip()]
    else:
        return body
    return segs[i] if -len(segs) <= i < len(segs) else ""


def _cg_check_one(text, chk):
    """Run one predefined check. Returns an error string, or None if it passes."""
    if not isinstance(chk, dict):
        return None
    rule = str(chk.get("rule") or "")
    tgt = chk.get("target", "all")
    seg = _cg_slice(text, tgt)
    where = "" if tgt in (None, "all") else " (%s)" % tgt

    def cmp_num(n, label):
        if "eq" in chk and n != chk["eq"]:
            return "%s%s is %d, must be exactly %s" % (label, where, n, chk["eq"])
        if "min" in chk and n < chk["min"]:
            return "%s%s is %d, must be at least %s" % (label, where, n, chk["min"])
        if "max" in chk and n > chk["max"]:
            return "%s%s is %d, must be at most %s" % (label, where, n, chk["max"])
        return None

    if rule == "word_count":
        return cmp_num(len(seg.split()), "the word count")
    if rule == "char_count":
        return cmp_num(len(seg), "the character count")
    if rule == "line_count":
        return cmp_num(len([l for l in text.strip().splitlines() if l.strip()]), "the line count")
    if rule == "sentence_count":
        return cmp_num(len([p for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]),
                       "the sentence count")
    if rule == "json":
        try:
            obj = json.loads(seg)
        except Exception as e:
            return "the output is not valid JSON (%s)" % (str(e)[:60])
        need = chk.get("require_keys")
        if isinstance(need, list) and isinstance(obj, dict):
            missing = [k for k in need if k not in obj]
            if missing:
                return "the JSON is missing required keys: " + ", ".join(map(str, missing))
        return None
    if rule == "regex":
        try:
            ok = re.search(chk.get("pattern") or "", seg,
                           re.I if "i" in (chk.get("flags") or "") else 0) is not None
        except re.error:
            return None   # a bad caller pattern must not block generation forever
        return None if ok else "the output must match the pattern /%s/" % (chk.get("pattern") or "")
    if rule in ("contains", "not_contains"):
        val = str(chk.get("value") or "")
        hay, needle = (seg.lower(), val.lower()) if chk.get("ci") else (seg, val)
        present = needle in hay
        if rule == "contains" and not present:
            return "the output must contain %r" % val
        if rule == "not_contains" and present:
            return "the output must NOT contain %r" % val
        return None
    if rule in ("starts_with", "ends_with"):
        val = str(chk.get("value") or "")
        if chk.get("word"):                      # match the first/last WORD
            words = re.findall(r"[A-Za-z0-9']+", seg)
            got = (words[0] if rule == "starts_with" else words[-1]) if words else ""
            ok = got.lower() == val.lower()
        else:                                    # literal prefix/suffix (punctuation-tolerant)
            s = seg.strip()
            ok = (s.lower().startswith(val.lower()) if rule == "starts_with"
                  else s.lower().rstrip(".!?\"' ").endswith(val.lower()))
        verb = "start" if rule == "starts_with" else "end"
        return None if ok else "the segment%s must %s with %r" % (where, verb, val)
    return None   # unknown rule -> no-op (never blocks)


def run_checks(text, checks):
    """Run all checks; return the list of violation messages (empty list = all pass)."""
    out = []
    for chk in (checks or []):
        e = _cg_check_one(text, chk)
        if e:
            out.append(e)
    return out


def constrained_generate(provider, model, prompt, checks, max_tries=4, temperature=0.7):
    """Generate against a provider model, VERIFY with predefined checks, and RETRY with
    targeted feedback until all checks pass or max_tries is hit. RCE-safe (checks are a
    fixed set, no caller code runs). Returns {ok, text, attempts, history, error?}."""
    try:
        max_tries = max(1, min(int(max_tries or 4), 8))
    except (TypeError, ValueError):
        max_tries = 4
    base = [{"role": "user", "content": str(prompt or "")}]
    msgs, history, text = list(base), [], ""
    for attempt in range(1, max_tries + 1):
        r = provider_chat(provider, model, msgs, {"temperature": temperature})
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error", "provider error"),
                    "attempts": attempt - 1, "history": history, "text": text}
        text = r.get("text", "")
        errs = run_checks(text, checks)
        history.append({"attempt": attempt, "text": text, "errors": errs})
        if not errs:
            return {"ok": True, "text": text, "attempts": attempt, "history": history}
        msgs = base + [
            {"role": "assistant", "content": text},
            {"role": "user", "content":
                "That did not satisfy the requirements: " + "; ".join(errs)
                + ". Rewrite so ALL requirements hold; double-check each one before answering. "
                  "Reply with only the answer."}]
    return {"ok": False, "text": text, "attempts": max_tries, "history": history,
            "error": "did not converge in %d tries; remaining: %s"
                     % (max_tries, "; ".join(run_checks(text, checks)))}


def _chat_delta(ptype, line):
    """Extract just the incremental text from one streamed provider line (bytes)."""
    try:
        if ptype == "ollama":
            obj = json.loads(line)
            return (obj.get("message") or {}).get("content", "") or ""
        if line.startswith(b"data:"):
            data = line[5:].strip()
            if data == b"[DONE]":
                return ""
            obj = json.loads(data)
            ch = (obj.get("choices") or [{}])[0]
            return (ch.get("delta") or {}).get("content", "") or ""
    except (json.JSONDecodeError, KeyError, IndexError, ValueError):
        return ""
    return ""


# ---------------------------------------------------------------- research forge

def research_compose(topic, mode, target, model, effort):
    slug = _slug(topic or target)
    date = datetime.now().strftime("%Y%m%d")
    if mode == "research":
        prompt = (f"Research {topic} using WebSearch/WebFetch; write a dense cited markdown "
                  f"report to data/research/{slug}-{date}.md; end with actionable "
                  f"recommendations.")
    elif mode == "improve-skill":
        tgt = target or topic
        prompt = (f"Improve the user skill '{tgt}' (Karpathy-style iterative refinement). Steps: "
                  f"(1) READ the current definition at ~/.claude/skills/{_slug(tgt)}/SKILL.md via "
                  f"its absolute path (if missing, treat as a fresh skill for '{tgt}'). "
                  f"(2) Use WebSearch to gather current best practices for this skill's domain AND "
                  f"the Claude Code skill-authoring guide (frontmatter with a sharp `description:` "
                  f"that lists trigger phrases; a focused, imperative body). "
                  f"(3) WRITE an improved complete SKILL.md to "
                  f"data/research/proposals/{slug}-{date}-SKILL.md — valid YAML frontmatter "
                  f"(name, description) followed by the body, and begin the file with an HTML "
                  f"comment rationale header (<!-- rationale: what changed and why -->). Do NOT "
                  f"write into ~/.claude directly — the user applies it via the UI.")
    elif mode == "eval-skill":
        tgt = target or topic
        prompt = (f"Evaluate the user skill '{tgt}'. (1) READ "
                  f"~/.claude/skills/{_slug(tgt)}/SKILL.md. (2) Generate 4-6 concrete test "
                  f"scenarios that should trigger and exercise the skill, plus 1-2 near-miss "
                  f"cases that should NOT trigger it. (3) Reason through each scenario and mark "
                  f"PASS/FAIL with a one-line justification. (4) WRITE the eval report to "
                  f"data/research/{slug}-{date}-eval.md with a summary pass-rate and concrete "
                  f"fixes for any FAILs.")
    elif mode == "new-agent":
        prompt = (f"Design a new Claude Code subagent for: {topic}. Study existing definitions in "
                  f"~/.claude/agents/*.md for conventions, and WebSearch relevant best practices. "
                  f"WRITE the new agent definition to data/research/proposals/{slug}-{date}.md "
                  f"with valid YAML frontmatter (name, description, and optionally tools/model) "
                  f"followed by a focused system prompt body. Do NOT write into ~/.claude "
                  f"directly.")
    elif mode == "improve-loop":
        prompt = (f"Analyze the ZENITH loop definition '{target or topic}' and its recent run "
                  f"history (data/loop_runs.jsonl). Propose an improved loop and WRITE the "
                  f"proposed loop as JSON (fields: name, description, project, prompt, model, "
                  f"mode, interval_min, skills, enabled, runner, provider) to "
                  f"data/research/proposals/{slug}-{date}.json.")
    else:
        raise ValueError("bad research mode")
    job_id = spawn_job(str(ZENITH_DIR), prompt, model=model or "sonnet",
                       mode="acceptEdits", effort=effort, label=f"research:{mode}:{slug}")
    return job_id


def research_list():
    reports, proposals = [], []
    if RESEARCH_DIR.exists():
        for f in RESEARCH_DIR.glob("*.md"):
            st = f.stat()
            reports.append({"name": f.name, "path": str(f), "size": st.st_size,
                            "mtime": st.st_mtime})
    if PROPOSALS_DIR.exists():
        for f in sorted(PROPOSALS_DIR.iterdir()):
            if not f.is_file():
                continue
            st = f.stat()
            if f.suffix == ".json":
                kind = "loop"
            elif f.name.upper().endswith("-SKILL.MD"):
                kind = "skill"
            else:
                kind = "agent"
            proposals.append({"name": f.name, "path": str(f), "size": st.st_size,
                              "mtime": st.st_mtime, "kind": kind})
    reports.sort(key=lambda x: -x["mtime"])
    proposals.sort(key=lambda x: -x["mtime"])
    return {"reports": reports, "proposals": proposals}


def research_apply(src):
    p = Path(src).resolve()
    if PROPOSALS_DIR.resolve() not in p.parents:
        raise PermissionError("source is not a proposal file")
    if not p.is_file():
        raise ValueError("no such proposal")
    if p.suffix == ".json":                       # improve-loop proposal
        obj = json.loads(p.read_text())
        obj.pop("id", None)
        saved = loop_upsert(obj)
        return {"applied": "loop", "id": saved["id"]}
    text = p.read_text(errors="replace")          # agent proposal
    meta = parse_frontmatter(text)
    if not meta.get("name") or not meta.get("description"):
        raise ValueError("proposal missing name/description frontmatter")
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = AGENTS_DIR / (_slug(meta["name"]) + ".md")
    shutil.copyfile(p, dest)
    return {"applied": "agent", "dest": str(dest), "name": meta["name"]}


# ---------------------------------------------------------------- swarms / wargames

def _node_model_desc(m):
    """Human description of a node's model value (claude tier or provider:<id>:<model>)."""
    if isinstance(m, str) and m.startswith("provider:"):
        parts = m.split(":", 2)
        pid = parts[1] if len(parts) > 1 else "?"
        mdl = parts[2] if len(parts) > 2 else "?"
        return f"LOCAL model '{mdl}' via provider {pid} (call through ZENITH chat / curl)"
    return f"{m or 'sonnet'} (Claude tier)"


def _render_tree(nodes):
    by_parent = {}
    for n in nodes:
        by_parent.setdefault(n.get("parent"), []).append(n)

    lines = []

    def rec(pid, depth):
        for n in by_parent.get(pid, []):
            skills = n.get("skills") or []
            sk = (" · invoke skills: " + ", ".join(skills)) if skills else ""
            lines.append("  " * depth + f"- {n.get('role', 'node')} "
                         f"[{_node_model_desc(n.get('model'))}]: "
                         f"{n.get('prompt', '')}{sk}")
            rec(n.get("id"), depth + 1)

    rec(None, 0)
    return "\n".join(lines) or "(no nodes)"


def swarm_launch(sid, task, mode="default"):
    sw = next((s for s in _load_json(SWARMS_FILE, []) if s.get("id") == sid), None)
    if not sw:
        raise KeyError("no such swarm")
    nodes = sw.get("nodes", [])
    tree = _render_tree(nodes)
    root = next((n for n in nodes if not n.get("parent")), None)
    rm = (root or {}).get("model", "sonnet")
    # a provider:* root can't run as a claude orchestrator — fall back to sonnet
    root_model = ("claude-sonnet-5" if isinstance(rm, str) and rm.startswith("provider:")
                  else MODEL_MAP.get(rm, "claude-sonnet-5"))
    prompt = ("You are the root orchestrator of this org chart:\n" + tree +
              "\n\nFor each direct report: if its model is a Claude tier, dispatch a subagent "
              "via the Agent tool with that model and role prompt, and have it first invoke any "
              "listed skills via the Skill tool. If its model is a LOCAL provider model "
              "(provider:<id>:<model>), you cannot spawn a Claude subagent on it — instead call "
              "that local model yourself via ZENITH's chat endpoint "
              "(POST http://127.0.0.1:" + str(PORT) + "/api/chat with {provider,model,messages,"
              "stream:false}) or curl the provider directly, then fold its output in "
              "(best-effort). Reports with children instruct their subagent to further delegate. "
              "Task: " + (task or "") + "\n\nSynthesize a final report.")
    return spawn_job(sw["project"], prompt, model=root_model, mode=mode,
                     label=f"swarm:{sw.get('name', sid)}")


def wargame_launch(wid, mode="default"):
    wg = next((w for w in _load_json(WARGAMES_FILE, []) if w.get("id") == wid), None)
    if not wg:
        raise KeyError("no such wargame")
    slug = _slug(wg.get("name"))
    date = datetime.now().strftime("%Y%m%d")
    rounds = max(1, min(5, int(wg.get("rounds", 3))))
    prompt = (f"Run {rounds} rounds where RED (adversary — {wg.get('red_prompt', '')}) attacks "
              f"the target/plan and BLUE ({wg.get('blue_prompt', '')}) defends/fixes; after each "
              f"round JUDGE ({wg.get('judge_prompt', '')}) scores both and extracts lessons. "
              f"Scenario: {wg.get('scenario', '')}. Write an after-action report to "
              f"data/wargames/{slug}-{date}.md with concrete agent-improvement recommendations.")
    model = MODEL_MAP.get(wg.get("model", "sonnet"), wg.get("model", "claude-sonnet-5"))
    WARGAMES_DIR.mkdir(parents=True, exist_ok=True)
    return spawn_job(wg["project"], prompt, model=model, mode=mode,
                     label=f"wargame:{wg.get('name', wid)}")


def wargame_reports():
    out = []
    if WARGAMES_DIR.exists():
        for f in sorted(WARGAMES_DIR.glob("*.md")):
            st = f.stat()
            out.append({"name": f.name, "path": str(f), "size": st.st_size,
                        "mtime": st.st_mtime})
    out.sort(key=lambda x: -x["mtime"])
    return out


# ---------------------------------------------------------------- files walker

def files_walk(root, q="", exts="", hidden=False, limit=800, flat=True):
    root = Path(root).resolve()
    if not any(root == r or r in root.parents for r in READ_ROOTS):
        raise PermissionError("root outside allowed roots")
    extset = {e.strip().lstrip(".").lower() for e in (exts or "").split(",") if e.strip()}
    out = []
    if not flat:   # browse mode: immediate children only, dirs included
        try:
            entries = sorted(root.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except OSError:
            entries = []
        for p in entries:
            name = p.name
            if not hidden and name.startswith("."):
                continue
            if q and q.lower() not in name.lower():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if p.is_dir():
                if name in SKIP_DIRS and not hidden:
                    continue
                out.append({"rel": name, "ext": "", "size": None,
                            "mtime": st.st_mtime, "dir": True})
            else:
                ext = p.suffix.lstrip(".").lower()
                if extset and ext not in extset:
                    continue
                out.append({"rel": name, "ext": ext, "size": st.st_size,
                            "mtime": st.st_mtime, "dir": False})
            if len(out) >= limit:
                return {"root": str(root), "files": out, "truncated": True}
        return {"root": str(root), "files": out, "truncated": False}
    for dirpath, dirnames, filenames in os.walk(root):
        if hidden:
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        else:
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if not hidden and fn.startswith("."):
                continue
            p = Path(dirpath) / fn
            rel = str(p.relative_to(root))
            if q and q.lower() not in rel.lower():
                continue
            ext = p.suffix.lstrip(".").lower()
            if extset and ext not in extset:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"rel": rel, "ext": ext, "size": st.st_size, "mtime": st.st_mtime})
            if len(out) >= limit:
                return {"root": str(root), "files": out, "truncated": True}
    return {"root": str(root), "files": out, "truncated": False}


# ---------------------------------------------------------------- memory v2

def nm_detail(key):
    if not _nm_reachable():
        return {"available": False}
    r = nm_api("/api/memories/" + urllib.parse.quote(str(key), safe=""))
    if not isinstance(r, dict) or not r.get("key"):
        if isinstance(r, dict) and r.get("code") == 404:
            return {"available": True, "found": False}
        return {"available": False, "error": _nm_err(r)}
    tags = _parse_tags(r.get("tags"))
    d = {k: r.get(k) for k in ("key", "title", "content", "namespace",
                               "created_at", "updated_at")}
    d["tags"] = tags
    related = []
    if tags:
        tagset = set(tags)
        for o in (_nm_list() or [])[:400]:          # updated_at DESC, as before
            if o.get("key") == key:
                continue
            rt = set(_parse_tags(o.get("tags")))
            if rt & tagset:
                related.append({"key": o.get("key"), "title": o.get("title"),
                                "namespace": o.get("namespace"),
                                "updated_at": o.get("updated_at"),
                                "shared_tags": sorted(rt & tagset)})
                if len(related) >= 8:
                    break
    return {"available": True, "found": True, "memory": d, "related": related}


def nm_capture(title, content, tags, namespace):
    """Write a memory through the same one door (POST /api/memories), so the
    security gate, ownership and embedding pipeline all run. The local JSONL
    stays as an append-only audit trail of what ZENITH sent. Returns the legacy
    {"queued", "job"} shape; the route turns queued:false into a real HTTP
    error so the browser toasts the reason instead of a false success."""
    if not (content or "").strip():
        return {"queued": False, "job": None, "error": "content is empty"}
    rec = {"title": title, "content": content, "tags": tags or [],
           "namespace": namespace or "default", "queued": now_iso()}
    try:
        with _data_lock:
            _append_jsonl(NM_CAPTURE_QUEUE, rec)
    except OSError:
        pass
    if not _nm_reachable():
        return {"queued": False, "job": None, "error": _nm_down()}
    slug = re.sub(r"[^a-z0-9]+", "_", (title or content).lower()).strip("_")[:48]
    key = "zenith_%s_%s" % (time.strftime("%Y%m%d_%H%M%S"), slug or "capture")
    r = nm_api("/api/memories", "POST",
               {"key": key, "content": content, "title": title or key,
                "tags": tags or [], "namespace": namespace or "default"})
    if not isinstance(r, dict) or not r.get("key"):
        return {"queued": False, "job": None, "error": _nm_err(r, "capture failed")}
    with _nm_list_lock:                  # the new memory must show up immediately
        _nm_list_cache["rows"] = None
    return {"queued": True, "job": None, "key": r["key"]}


# ------------------------------------------------- NexusMind terminal (ask/ingest/correct)
# The NexusMind app's command terminal is a port of NexusMind's own dashboard pane. Its page
# calls its own origin directly; ZENITH must not — every one of these goes through nm_api(),
# so the bearer token stays server-side and the `nexusmind_api` switch gates the whole
# surface. Each returns the same {"available", ...} / {"available": False, "error"} shape the
# rest of the memory module uses, so one client-side helper can render every failure.

NM_LLM_TIMEOUT = 90          # ask/ingest/correct run classification + an LLM call in NM


def nm_ask(question, namespace=None):
    """POST /api/ask — NM searches its corpus and synthesises an answer."""
    question = str(question or "").strip()
    if not question:
        return {"available": True, "error": "question required", "sources": []}
    if not _nm_reachable():
        return {"available": False, "error": _nm_down(), "sources": []}
    body = {"question": question}
    if namespace:
        body["namespace"] = namespace
    r = nm_api("/api/ask", "POST", body, timeout=NM_LLM_TIMEOUT)
    if not isinstance(r, dict) or r.get("error"):
        return {"available": False, "error": _nm_err(r, "ask failed"), "sources": []}
    return {"available": True, "answer": r.get("answer") or "",
            "context": r.get("context") or "",
            "sources": r.get("sources") if isinstance(r.get("sources"), list) else []}


def nm_ingest(text, source="zenith", hints=None):
    """POST /api/ingest — the classifying write path (classify, dedup, entity
    extraction, auto-linking). Deliberately not POST /api/memories, which skips all
    four; nm_capture() is the raw-write path and stays separate."""
    text = str(text or "").strip()
    if not text:
        return {"available": True, "error": "text required"}
    if not _nm_reachable():
        return {"available": False, "error": _nm_down()}
    body = {"text": text, "source": source or "zenith"}
    if hints:
        body["hints"] = hints
    r = nm_api("/api/ingest", "POST", body, timeout=NM_LLM_TIMEOUT)
    if not isinstance(r, dict) or not r.get("key"):
        return {"available": False, "error": _nm_err(r, "ingest failed")}
    with _nm_list_lock:                  # the new memory must show up immediately
        _nm_list_cache["rows"] = None
    cls = r.get("classification") if isinstance(r.get("classification"), dict) else {}
    return {"available": True, "key": r.get("key"), "classification": cls,
            "dedup_action": r.get("dedup_action") or "new",
            "relationships_created": r.get("relationships_created") or 0}


def nm_correct(text, key=None):
    """POST /api/correct — amend the best-matching existing memory (old version kept
    by NM's temporal versioning), or store new when nothing matches."""
    text = str(text or "").strip()
    if not text:
        return {"available": True, "error": "text required"}
    if not _nm_reachable():
        return {"available": False, "error": _nm_down()}
    body = {"text": text}
    if key:
        body["key"] = key
    r = nm_api("/api/correct", "POST", body, timeout=NM_LLM_TIMEOUT)
    if not isinstance(r, dict) or r.get("error"):
        return {"available": False, "error": _nm_err(r, "correct failed")}
    with _nm_list_lock:
        _nm_list_cache["rows"] = None
    return dict(r, available=True)


def nm_stats():
    """GET /api/stats — NM's own corpus/embedding stats. NM wraps its payload as
    {status, data, meta}; older builds answered flat, so unwrap either."""
    if not _nm_reachable():
        return {"available": False, "error": _nm_down()}
    r = nm_api("/api/stats")
    if not isinstance(r, dict) or r.get("error"):
        return {"available": False, "error": _nm_err(r, "stats failed")}
    data = r.get("data") if isinstance(r.get("data"), dict) else r
    return {"available": True, "stats": data}


def nm_capture_text(text, project=""):
    """THE quick-capture path. The ⌘K command bar, the NexusMind app's terminal and
    the Cases surface all land here, so there is one NexusMind client (nm_api), one
    config and one `nexusmind_api` gate — this used to be a second, ungated client in
    zenith_cases.py with its own base+token file. Returns the {"ok","key","detail"}
    contract the command bar's localStorage queue depends on: ok:false parks the line
    locally and shows `detail`, so `detail` must always be a displayable string."""
    r = nm_ingest(text, source="zenith-cmdbar",
                  hints={"project": project} if project else None)
    if r.get("available") and r.get("key"):
        return {"ok": True, "key": r.get("key") or "",
                "detail": (r.get("classification") or {}).get("namespace") or ""}
    return {"ok": False, "detail": r.get("error") or "capture failed"}


# ---------------------------------------------------------------- stats

def stats():
    encmap = project_encoding_map()
    now = time.time()
    days, proj_stats, recent = {}, {}, []
    total_sessions = total_bytes = 0
    if TRANSCRIPTS_ROOT.exists():
        for d in TRANSCRIPTS_ROOT.iterdir():
            if not d.is_dir():
                continue
            proj = match_project(d.name, encmap) or d.name
            pname = Path(proj).name if str(proj).startswith("/") else proj
            for f in d.glob("*.jsonl"):
                try:
                    st = f.stat()
                except OSError:
                    continue
                total_sessions += 1
                total_bytes += st.st_size
                ps = proj_stats.setdefault(pname, [0, 0])
                ps[0] += 1
                ps[1] += st.st_size
                day = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
                days[day] = days.get(day, 0) + 1
                recent.append((st.st_mtime, f))
    sessions_by_day = []
    for i in range(13, -1, -1):
        day = datetime.fromtimestamp(now - i * 86400).strftime("%Y-%m-%d")
        sessions_by_day.append({"day": day, "count": days.get(day, 0)})
    top_projects = sorted(
        ({"name": k, "sessions": v[0], "bytes": v[1]} for k, v in proj_stats.items()),
        key=lambda x: -x["sessions"])[:8]
    recent.sort(key=lambda x: -x[0])
    recent_titles = []
    for _, f in recent[:5]:
        s = session_summary(f)
        recent_titles.append(s.get("title") or s.get("first_prompt") or f.stem)
    top_models_counts = {}
    for _, f in recent[:40]:                      # bounded scan (session_detail cached)
        d = session_detail(f)
        for m, c in (d.get("models") or {}).items():
            top_models_counts[m] = top_models_counts.get(m, 0) + c
    top_models = sorted(({"model": k, "count": v} for k, v in top_models_counts.items()),
                        key=lambda x: -x["count"])[:8]
    providers_detail = [{"name": pr.get("name"), "type": pr.get("type"),
                         "enabled": bool(pr.get("enabled")), "reachable": None}
                        for pr in providers_load()]
    with _jobs_lock:
        jobs = {"total": len(JOBS),
                "running": sum(1 for j in JOBS.values() if j["status"] == "running"),
                "done": sum(1 for j in JOBS.values() if j["status"] == "done"),
                "error": sum(1 for j in JOBS.values() if j["status"] == "error")}
    loops = loops_load()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    runs_today = 0
    if LOOP_RUNS_FILE.exists():
        for line in LOOP_RUNS_FILE.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (r.get("started") or "").startswith(today):
                runs_today += 1
    with _terms_lock:
        terms_live = sum(1 for t in TERMS.values() if t["status"] == "live")
    return {"sessions_by_day": sessions_by_day, "total_sessions": total_sessions,
            "total_bytes": total_bytes, "top_projects": top_projects, "jobs": jobs,
            "terms_live": terms_live,
            "loops": {"count": len(loops),
                      "enabled": sum(1 for lp in loops if lp.get("enabled")),
                      "runs_today": runs_today},
            "memory": nm_meta(), "agents": len(list_agents()),
            "skills": len(list_skills()), "providers": len(providers_load()),
            "providers_detail": providers_detail, "top_models": top_models,
            "uptime_s": int(now - START_TIME), "recent_titles": recent_titles}


# ---------------------------------------------------------------- terminals
# Real PTY sessions bridged to the browser over a minimal RFC6455 WebSocket.
# Output is buffered per terminal so a page reload can re-attach with scrollback.

TERMS = {}
_terms_lock = threading.Lock()
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TMUX_BIN = shutil.which("tmux")


class UnixPTY:
    """Real PTY via the stdlib pty module (macOS / Linux / WSL)."""
    def __init__(self, argv, cwd, env):
        pid, fd = ptymod.fork()
        if pid == 0:   # child: become the shell/claude, never return to server code
            try:
                os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(1)
        self.pid, self.fd = pid, fd

    def read(self, n):
        return os.read(self.fd, n)

    def write(self, data):
        os.write(self.fd, data)

    def set_winsize(self, rows, cols):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def terminate(self):
        try:
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass

    def reap(self):
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except OSError:
            pass


class TmuxPTY(UnixPTY):
    """A PTY attached to a persistent tmux session. The session outlives the
    attach (and the whole server), so terminals survive drops AND server restarts.
    Only an explicit kill() destroys the session; a dropped attach just detaches."""
    def __init__(self, session, cwd, argv, env, inline_env=None):
        tmux_ensure_session(session, cwd, argv, env, inline_env)
        pid, fd = ptymod.fork()
        if pid == 0:
            try:
                aenv = _augment_path(dict(os.environ))
                aenv["TERM"] = "xterm-256color"
                aenv["COLORTERM"] = "truecolor"
                os.execvpe(TMUX_BIN, [TMUX_BIN, "attach-session", "-t", session], aenv)
            finally:
                os._exit(1)
        self.pid, self.fd, self.session = pid, fd, session

    def terminate(self):   # KILL button: detach AND destroy the tmux session
        super().terminate()
        subprocess.run([TMUX_BIN, "kill-session", "-t", self.session], capture_output=True)


def tmux_ensure_session(session, cwd, argv, env, inline_env=None):
    """Create the tmux session (idempotent) with destroy-unattached off, running the
    requested command inside a persistent shell via send-keys so the session survives
    the command exiting. `inline_env` (effort + per-agent env like aider's
    OLLAMA_API_BASE) is prefixed onto the command, because a new session inherits the
    already-running tmux SERVER's env, not the env passed here. Returns True if newly
    created."""
    exists = subprocess.run([TMUX_BIN, "has-session", "-t", session],
                            capture_output=True).returncode == 0
    # Global: make tmux's mouse copy-mode selection flow to the SYSTEM clipboard via OSC 52 (the
    # frontend registers an OSC-52 handler → navigator.clipboard). Without this, `mouse on` makes a
    # drag-select land only in tmux's private buffer — the yellow highlight vanishes on release and
    # nothing reaches the browser/OS clipboard. Idempotent server-global option.
    subprocess.run([TMUX_BIN, "set-option", "-g", "set-clipboard", "on"], capture_output=True)
    # Scrub ZENITH_TERM_ID from the SERVER-global environment. A server started by an
    # early spawn captured that spawn's term id, and every session created afterwards
    # inherited it, so every statusline tagged its session against one wrong terminal.
    # The id is inlined per command now, so the global is never wanted; unsetting it
    # here heals a server that was already poisoned, without a tmux restart.
    subprocess.run([TMUX_BIN, "set-environment", "-g", "-u", "ZENITH_TERM_ID"],
                   capture_output=True)
    if not exists:
        subprocess.run([TMUX_BIN, "new-session", "-d", "-s", session, "-c", cwd],
                       env=env, capture_output=True, check=True)
        for opt, val in (("destroy-unattached", "off"), ("mouse", "on"), ("status", "off")):
            subprocess.run([TMUX_BIN, "set-option", "-t", session, opt, val], capture_output=True)
        if argv:   # launch the mode command; inline the extra env (effort + agent env)
            prefix = "".join(f"{k}={shlex.quote(str(v))} "
                             for k, v in (inline_env or {}).items())
            cmd = prefix + " ".join(shlex.quote(a) for a in argv)
            subprocess.run([TMUX_BIN, "send-keys", "-t", session, cmd, "Enter"],
                           capture_output=True)
    else:   # reconnect / server-restart: reuse, just re-assert the survival options
        for opt, val in (("destroy-unattached", "off"), ("mouse", "on"), ("status", "off")):
            subprocess.run([TMUX_BIN, "set-option", "-t", session, opt, val], capture_output=True)
    return not exists


def tmux_zenith_sessions():
    if not (TMUX_BIN and not IS_WINDOWS):
        return []
    r = subprocess.run([TMUX_BIN, "list-sessions", "-F", "#{session_name}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return [n.strip() for n in r.stdout.splitlines() if n.strip().startswith("zenith-")]


def tmux_pane_cwd(session):
    r = subprocess.run([TMUX_BIN, "display-message", "-p", "-t", session,
                        "#{pane_current_path}"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def tmux_pane_info(session):
    """(mode, session_id) for a REATTACHED tmux session. The server has no record of
    how a session it didn't spawn was started, so inspect what is running in the pane.

    NOT via #{pane_current_command}: the native claude launcher execs a binary named
    after its version, so the process NAME is literally "2.1.222" and matching on it
    reports every agent session as a plain shell. The full argv does carry the path,
    which _is_claude_cmd already knows how to read — and the SAME argv carries
    `--resume <uuid>`, which is the only way a reattached terminal can be matched
    back to its session row (otherwise it shows RESUME for a session already open)."""
    try:
        r = subprocess.run([TMUX_BIN, "display-message", "-p", "-t", session,
                            "#{pane_pid}"], capture_output=True, text=True, timeout=4)
        pid = (r.stdout or "").strip()
        if not pid.isdigit():
            return "shell", None
        pids = [pid]                      # the pane process, plus one level of children
        kids = subprocess.run(["pgrep", "-P", pid], capture_output=True,
                              text=True, timeout=4).stdout
        pids += [p for p in kids.split() if p.isdigit()]
        out = subprocess.run(["ps", "-o", "command=", "-p", ",".join(pids)],
                             capture_output=True, text=True, timeout=4).stdout
    except (OSError, subprocess.SubprocessError):
        return "shell", None
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if "prime-agent-run.sh" in line:   # the ssh whose remote command is the launcher
            return "prime-agent", None
        if _is_claude_cmd(line):
            return "claude", _session_id_from_cmd(line)
        exe = os.path.basename(line.split()[0]).lower()
        if exe in ("codex", "aider"):
            return exe, None
    return "shell", None


def tmux_pane_mode(session):
    return tmux_pane_info(session)[0]


class WinPTY:
    """Real PTY on native Windows via pywinpty (ConPTY). Lazy-imported so the
    server still runs without it — the terminal feature just reports it's missing."""
    def __init__(self, argv, cwd, env):
        try:
            from winpty import PtyProcess   # pip install pywinpty
        except ImportError as e:
            raise RuntimeError("native Windows terminals require pywinpty — run "
                               "`pip install pywinpty`, or run ZENITH inside WSL") from e
        self.proc = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(24, 80))
        self.pid = getattr(self.proc, "pid", 0)

    def read(self, n):
        data = self.proc.read(n)   # pywinpty returns str; '' on EOF
        if data == "":
            raise EOFError
        return data.encode("utf-8", "replace") if isinstance(data, str) else data

    def write(self, data):
        self.proc.write(data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else data)

    def set_winsize(self, rows, cols):
        try:
            self.proc.setwinsize(rows, cols)
        except Exception:
            pass

    def terminate(self):
        try:
            self.proc.terminate(force=True)
        except Exception:
            pass

    def reap(self):
        pass


def ws_send(conn, payload, opcode=2):
    if isinstance(payload, str):
        payload = payload.encode()
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    with conn["lock"]:
        conn["sock"].sendall(head + payload)


def ws_recv(rfile):
    hdr = rfile.read(2)
    if len(hdr) < 2:
        return None, None
    b1, b2 = hdr
    opcode = b1 & 0x0F
    ln = b2 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", rfile.read(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", rfile.read(8))[0]
    mask = rfile.read(4) if (b2 & 0x80) else b""
    data = rfile.read(ln)
    if mask:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    return opcode, data


def _session_tail_event(raw):
    """Turn one transcript jsonl line into a lightweight live-timeline event. Never raises;
    returns None for noise (queue ops, summaries, un-extractable lines)."""
    try:
        o = json.loads(raw)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(o, dict):
        return None
    typ = o.get("type") or ""
    ts = o.get("timestamp") or o.get("ts") or ""
    role, text, tool = "", "", ""
    msg = o.get("message") if isinstance(o.get("message"), dict) else None
    if msg:
        role = msg.get("role") or typ
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and b.get("text"):
                    text += b["text"]
                elif bt == "tool_use":
                    tool = b.get("name") or "tool"
                elif bt == "tool_result" and not tool:
                    tool = "result"
    elif typ in ("user", "assistant") and isinstance(o.get("content"), str):
        role, text = typ, o["content"]
    if not (text or tool or role in ("user", "assistant")):
        return None
    text = " ".join((text or "").split())
    if len(text) > 400:
        text = text[:400] + "…"
    return {"ts": ts, "role": role or typ, "tool": tool, "text": text}


def _term_reader(term):
    pty = term["pty"]
    while True:
        try:
            data = pty.read(65536)
        except (OSError, EOFError):
            data = b""
        if not data:
            break
        with term["lock"]:
            term["buf"].extend(data)
            if len(term["buf"]) > 250_000:
                del term["buf"][: len(term["buf"]) - 250_000]
            conns = list(term["conns"])
        for c in conns:
            try:
                ws_send(c, data, 2)
            except OSError:
                with term["lock"]:
                    if c in term["conns"]:
                        term["conns"].remove(c)
    term["status"] = "dead"
    pty.reap()
    try:
        life = round(time.time() - _iso_epoch(term.get("created")), 0)
    except (TypeError, ValueError):
        life = None
    zs.emit("term.close", project=term.get("cwd") or "", ref=term["id"],
            outcome="killed" if term.get("_killed") else "ok", actor="user",
            data={"project": term.get("cwd"), "lifetime_s": life})
    with term["lock"]:
        conns = list(term["conns"])
    for c in conns:
        try:
            ws_send(c, json.dumps({"t": "exit"}), 1)
        except OSError:
            pass


# --- prime-agent: a session inside the hardened container on the GPU box.
# Nothing runs locally — the terminal is an ssh whose remote command is the container
# launcher, so the whole agent lives behind the box's own sandbox (non-root, cap-drop
# ALL, no published ports, egress-locked, budgeted LiteLLM key) and dies with the ssh.
# The headless twin of this path is bin/pa-job.sh, which the agent manifest points at
# for Run/Jobs and A/B arms; both read the same endpoint config.
#
# WHERE THE ADDRESS LIVES. data/pa.json — not here. A private box's address is
# machine-identifying, publish.sh refuses one in anything that ships, and data/ is
# gitignored, so this is the only place it can sit without leaking into a commit.
# An install with no pa.json simply has no prime-agent: the mode falls back to a
# plain shell rather than ssh'ing at a placeholder someone else owns.
PA_FILE = DATA_DIR / "pa.json"
_PA_WT_RE = re.compile(r"^[A-Za-z0-9._~/-]{1,200}$")


def pa_cfg(key, default=""):
    """One prime-agent endpoint setting: env (ZENITH_PA_<KEY>) > data/pa.json >
    default. Read per call rather than cached at import, so editing pa.json takes
    effect on the next launch instead of the next restart. Never raises."""
    v = os.environ.get("ZENITH_PA_" + str(key).upper())
    if not v:
        cfg = _load_json(PA_FILE, {})
        v = cfg.get(key) if isinstance(cfg, dict) else None
    return str(v or default)


def pa_worktree(raw):
    """Validate (or default) the remote worktree for the prime-agent mode.

    The value is interpolated UNQUOTED into a remote shell command — it has to be, so
    the remote shell expands `~` — and ZENITH has no user auth beyond the network
    boundary. The charset is therefore the entire defence: no quote, space, `$`,
    backtick, `;`, `&` or `|` can survive it, so nothing can break out of the command.
    `..` and a leading `-` are refused on top of that (path escape / argv injection
    into mkdir and the launcher). Idempotent: re-validating an already-resolved path
    returns it unchanged, so callers may resolve once and pass the result down."""
    wt = (raw or "").strip()
    if not wt:
        return (pa_cfg("worktree_root", "~/scratch/pa-work").rstrip("/")
                + "/zenith-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()))
    if not _PA_WT_RE.match(wt) or ".." in wt or wt.startswith("-"):
        raise ValueError("bad worktree path — allowed: letters, digits and . _ ~ / -")
    return wt


def _mode_argv(mode, resume_id, worktree=None):
    """The command (argv list) for a launch mode, or None for a plain shell."""
    if mode == "claude":
        return [CLAUDE_BIN]
    if mode == "claude-continue":
        return [CLAUDE_BIN, "-c"]
    if mode == "claude-resume" and resume_id:
        return [CLAUDE_BIN, "--resume", str(resume_id)]
    if mode == "prime-agent":
        host, run = pa_cfg("host"), pa_cfg("run")
        if not (host and run):
            # Same honest-refusal idiom as aider-with-no-model below: an
            # unconfigured install opens a plain shell and says why, rather than
            # ssh'ing at nothing and hanging on a name that resolves elsewhere.
            print('ZENITH/OS: prime-agent is not configured — set "host" and "run" '
                  "in data/pa.json (or ZENITH_PA_HOST / ZENITH_PA_RUN). "
                  "Opening a plain shell.")
            return None
        wt = pa_worktree(worktree)
        return ["ssh", "-t", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=30", host, "--",
                f"mkdir -p {wt} && exec {run} {wt}"]
    if mode in ("codex", "aider"):
        return _agent_interactive_argv(mode)
    return None


def _agent_interactive_argv(agent_id):
    """Interactive-session argv for a non-claude agent — its own TUI/REPL (NOT the
    headless job argv). Resolves the agent's bin from the manifest; aider needs an
    explicit --model (its REPL otherwise prompts), taken from the manifest list and
    then from live detection. None if the agent is unresolvable/disabled, or has no
    model we can actually name → spawn_term falls back to a plain shell so the
    terminal still opens."""
    ad = za.resolve_agent(agent_id)
    binp = (ad or {}).get("bin_path") or (ad or {}).get("bin")
    if not binp:
        return None
    if agent_id == "aider":
        # was: fall back to the literal "ollama/llama3.2" — which asserted both that
        # ollama is installed AND that this exact tag is pulled. On a fresh machine
        # aider then hangs trying to reach a model that is not there. Detection or
        # nothing: an honest refusal beats a hung terminal.
        models = agent_models(ad) or [str(m) for m in
                                      ((ad.get("models") or {}).get("list") or [])]
        if not models:
            det = detected_models(refresh=True)
            models = ["ollama/" + m for m in det.get("ollama_tags") or []] \
                or list(det.get("openai_models") or [])
        if not models:
            print("ZENITH/OS: aider has no usable model — nothing detected and the "
                  "manifest lists none. Pull an ollama model or set one in "
                  "data/agents.json, then relaunch. Opening a plain shell.")
            return None
        return [binp, "--model", models[0]]
    return [binp]          # codex (+ any future agent): bare interactive binary


# --- workspace manifest: per-terminal state on disk so a full reboot (which kills
# tmux + every claude) can rebuild the desktop. The statusline enriches each file with
# the live claude session_id; reconstruction on boot relaunches `claude --resume <id>`.
WORKSPACE_DIR = DATA_DIR / "live"


def _workspace_persist(term):
    """Write/merge a term's reboot-recovery manifest. tmux-backed terms only."""
    if not term.get("persist"):
        return
    try:
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        p = WORKSPACE_DIR / (term["id"] + ".json")
        rec = {}
        if p.exists():
            try:
                rec = json.loads(p.read_text())
            except Exception:
                rec = {}
        mode = term.get("mode") or "shell"
        # never downgrade a known claude mode to "shell" (re-adopt reports shell)
        if not (mode == "shell" and str(rec.get("mode", "")).startswith("claude")):
            rec["mode"] = mode
        for k in ("cwd", "effort", "resume_id", "created", "worktree"):
            if term.get(k) is not None:
                rec[k] = term.get(k)
        rec["id"] = term["id"]
        p.write_text(json.dumps(rec))
    except Exception:
        pass   # manifest is best-effort; never block a terminal on it


def _workspace_forget(term_id):
    try:
        (WORKSPACE_DIR / (term_id + ".json")).unlink()
    except Exception:
        pass


def _start_term(term, rebuilt=False):
    """Register a term + start its reader thread."""
    with _terms_lock:
        TERMS[term["id"]] = term
    _workspace_persist(term)
    zs.emit("term.open", project=term.get("cwd") or "", ref=term["id"],
            outcome="ok", actor="zenith" if rebuilt else "user",
            data={"project": term.get("cwd"), "mode": term.get("mode"),
                  "rebuilt": bool(rebuilt)})
    threading.Thread(target=_term_reader, args=(term,), daemon=True).start()
    return term["id"]


def _new_term_record(term_id, pty, cwd, mode, persist, effort, resume_id=None):
    return {"id": term_id, "pty": pty, "pid": pty.pid, "cwd": cwd, "mode": mode,
            "persist": persist, "status": "live", "effort": effort,
            "resume_id": resume_id,
            # the folder identifies the window; the mode suffix only earns its space for
            # a plain shell — for an agent session the content already says which agent
            "label": (f"{Path(cwd).name} · shell" if mode == "shell"
                      else Path(cwd).name), "created": now_iso(),
            "buf": bytearray(), "conns": [], "lock": threading.Lock()}


def spawn_term(cwd, mode="shell", persist=True, resume_id=None, effort=None,
               worktree=None):
    cwd = str(Path(cwd or HOME).resolve())
    # relax to anywhere under HOME so sessions whose cwd lives elsewhere can resume.
    if not (cwd == str(HOME) or cwd.startswith(str(HOME) + os.sep)):
        raise PermissionError("cwd outside HOME")
    term_id = uuid.uuid4().hex[:10]
    # prime-agent: the LOCAL cwd is just where the ssh runs from and stays under HOME as
    # for every other mode; the work happens in `worktree` on the remote box.
    worktree = pa_worktree(worktree) if mode == "prime-agent" else None
    inner_argv = _mode_argv(mode, resume_id, worktree)

    env = _augment_path(dict(os.environ))
    env["TERM"] = "xterm-256color"
    env["ZENITH_TERM_ID"] = term_id   # lets the statusline tag its session_id to this term
    # persist the reasoning effort so the statusline's effort widget resolves.
    # inline_env carries the vars that must be prefixed onto the tmux command
    # (a new tmux session inherits the server env, not this env dict).
    inline_env = {}
    # ZENITH_TERM_ID must be INLINED, not just set in `env`. `env` belongs to the tmux
    # CLIENT we exec; the pane is created by the already-running tmux SERVER and
    # inherits the SERVER's environment. So whichever spawn happened to start that
    # server stamped its own term id into the server global, and every session created
    # afterwards inherited that same id — measured here as all 20+ live terminals
    # reporting ZENITH_TERM_ID=b352654cac. The statusline keys the live session_id file
    # by this variable, so every window wrote its ground truth into ONE other window's
    # file: that file's session id changed on every render, every other terminal was
    # left groundless, and the resolver fell back to guessing from the transcript pool.
    # This is the root cause of "this window is showing another session".
    inline_env["ZENITH_TERM_ID"] = term_id
    if effort in EFFORT_TOKENS and mode != "shell":
        env["MAX_THINKING_TOKENS"] = str(EFFORT_TOKENS[effort])
        inline_env["MAX_THINKING_TOKENS"] = str(EFFORT_TOKENS[effort])
    if mode in ("codex", "aider"):        # the agent's manifest env (e.g. aider's OLLAMA_API_BASE)
        _ad = za.resolve_agent(mode)
        for _k, _v in ((_ad or {}).get("env") or {}).items():
            env.setdefault(str(_k), str(_v))
            inline_env[str(_k)] = str(_v)

    use_tmux = bool(persist) and bool(TMUX_BIN) and not IS_WINDOWS
    if use_tmux:   # persistent: a tmux session that survives drops AND server restarts
        pty = TmuxPTY(f"zenith-{term_id}", cwd, inner_argv, env, inline_env)
    else:          # ephemeral: direct PTY (no tmux, or Windows, or persist=False)
        default_shell = "powershell.exe" if IS_WINDOWS else "/bin/zsh"
        shell = os.environ.get("SHELL") or default_shell
        argv = inner_argv or ([shell] if IS_WINDOWS else [shell, "-l"])
        pty = (WinPTY if IS_WINDOWS else UnixPTY)(argv, cwd, env)

    rec = _new_term_record(term_id, pty, cwd, mode, use_tmux, effort,
                           str(resume_id) if resume_id else None)
    if mode == "prime-agent":   # the remote worktree, not the local cwd, names this window
        rec["worktree"] = worktree
        rec["label"] = "pa · " + (worktree.rstrip("/").rsplit("/", 1)[-1] or "remote")
    return _start_term(rec)


def reattach_term(term_id):
    """Re-establish a term whose tmux session survived (e.g. after a server restart)
    but whose in-memory record was lost. Returns the term dict, or None."""
    if not (TMUX_BIN and not IS_WINDOWS):
        return None
    session = f"zenith-{term_id}"
    if subprocess.run([TMUX_BIN, "has-session", "-t", session],
                      capture_output=True).returncode != 0:
        return None
    cwd = tmux_pane_cwd(session) or str(HOME)
    pty = TmuxPTY(session, cwd, None, os.environ)   # None argv → just re-attach, no relaunch
    # ask the pane what it is running rather than assuming a shell — a reattached record
    # lands in TERMS and is served from there, so a wrong mode here is never re-derived.
    # resume_id matters just as much: without it the Sessions list cannot tell that this
    # session is already open in a window, and offers RESUME for something on screen.
    mode, rid = tmux_pane_info(session)
    _start_term(_new_term_record(term_id, pty, cwd, mode, True, None, rid))
    return TERMS.get(term_id)


def term_public(t):
    return {k: t.get(k) for k in ("id", "cwd", "mode", "persist", "status", "label",
                                  "created", "effort", "resume_id", "worktree")}


def _transcript_scan():
    """One pass over every project's transcripts -> ({session_id: path},
    {project_dir_name: [paths]}). Built once per resolution instead of stat-ing a
    candidate path per project per terminal."""
    by_id, by_dir = {}, {}
    if not TRANSCRIPTS_ROOT.exists():
        return by_id, by_dir
    for d in TRANSCRIPTS_ROOT.iterdir():
        if not d.is_dir():
            continue
        files = list(d.glob("*.jsonl"))
        by_dir[d.name] = files
        for f in files:
            by_id.setdefault(f.stem, f)      # session ids are unique; first wins
    return by_id, by_dir


_ANCESTOR_CACHE = {}


def _transcript_ancestor(path, _lines=60):
    """The session this transcript was FORKED FROM, or None if it is a root.

    Resuming (or compacting) a session does not continue its transcript: the CLI
    freezes the old file and opens a new one under a new id. The new file records
    where it came from in the snake_case `session_id` field — NOT `sessionId`,
    which always names the file's own session — so the first `session_id` that
    disagrees with the filename is the ancestor. Measured across every transcript
    on this box: 9 of 84 are forks, and in all 9 the first such value named the
    parent, including two forks off one ancestor and a two-step chain.

    Cached forever by path: a transcript's opening records never change."""
    key = str(path)
    if key in _ANCESTOR_CACHE:
        return _ANCESTOR_CACHE[key]
    anc = None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(_lines), fh):
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                sid = rec.get("session_id")
                if not sid:
                    continue
                anc = None if str(sid) == path.stem else str(sid)
                break                        # the first one that has it decides
    except OSError:
        pass
    _ANCESTOR_CACHE[key] = anc
    return anc


def _forward_fork(p, by_dir, depth=6):
    """Walk from a resume target to the transcript being written NOW.

    A --resume id names the frozen ancestor — always, not sometimes. Left alone it
    resolves to a dead file for the whole life of a resumed window, which is
    exactly the "892 min ago while I am typing in it" report. Following the fork
    edges forward lands on the live descendant instead. Newest wins where one
    ancestor forked more than once; `depth` bounds a chain and any cycle a
    malformed file could imply. Returns the input untouched when nothing forked
    off it — a window that resumed a session and has not yet written its own
    transcript must still resolve to the ancestor."""
    cur, seen = p, {p}
    for _ in range(depth):
        kids = [f for f in by_dir.get(cur.parent.name, ())
                if f not in seen and _transcript_ancestor(f) == cur.stem]
        if not kids:
            break
        kids.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        cur = kids[0]
        seen.add(cur)
    return cur


def _term_assignments(snap=None, scan=None):
    """Resolve EVERY live terminal to a transcript at once, as {term_id: Path}.

    `snap` / `scan` exist only so _selfcheck can drive this with a fixture; live
    callers pass neither and get the real terminals and the real transcript tree.

    Global on purpose, and the reason is the whole history of this function. The
    invariant is "no two terminals may ever name the same transcript", and that
    cannot be decided one request at a time: two terminals with no reported id are
    indistinguishable in isolation, so each independently picked the newest file in
    their shared project and both panes showed the same session.

    It equally cannot be decided per BRANCH. The first fix ranked the terminals
    that had no id at all and zipped them against the candidate files — but a
    terminal holding a resume id whose session another window is actually running
    breaks out of the exact-match step and lands in that same fallback, while being
    filtered OUT of the ranking for having an id. Two terminals, two different
    rival sets, both rank 0, same file again. Any scheme where one terminal's
    answer depends on a guess about which branch another terminal took will keep
    reproducing this.

    So: three passes over one deterministic order, every pass claiming from a
    shared `taken` set, and the invariant holds by construction no matter which
    pass resolves a given terminal.
      1. the id the session itself reported — statusline ground truth, then any
         recorded session_id
      2. a spawn-time --resume id
      3. whatever is left in the terminal's own project, newest transcript to most
         recently created terminal
    A terminal with nothing left to claim gets None. "No transcript yet" is a true
    statement; another session's prompts is not.
    """
    if snap is None:
        with _terms_lock:
            snap = [(tid, dict(o)) for tid, o in TERMS.items()]
    by_id, by_dir = _transcript_scan() if scan is None else scan
    # Newest terminal first. `created` is ISO-8601 so it sorts lexicographically;
    # the id is a tie-break purely so two terminals spawned in the same microsecond
    # still get a stable total order rather than one that flips between requests.
    snap.sort(key=lambda x: (x[1].get("created") or "", x[0]), reverse=True)
    live = [(tid, o) for tid, o in snap
            if o.get("cwd")
            and str(o.get("mode") or "").startswith(("claude", "codex", "aider"))]
    out, taken = {}, set()

    def claim(tid, want):
        p = by_id.get(str(want)) if want else None
        if p is None or p in taken:
            return
        out[tid] = p
        taken.add(p)

    grounded = set()
    for tid, o in live:                      # 1. what the session reported
        want = (_live_session_id(tid, o.get("cwd"), o.get("resume_id"))
                or o.get("session_id"))
        if want:
            grounded.add(tid)                # we KNOW this one; never guess for it
        claim(tid, want)
    for tid, o in live:                      # 2. spawn-time --resume
        if tid in out:
            continue
        anc = by_id.get(str(o.get("resume_id") or ""))
        if anc is None:
            continue
        # Follow the fork chain: the spawn id names the frozen ancestor, and the
        # session actually running is its newest descendant. If that descendant is
        # already taken, the window running it claimed it in pass 1 and this
        # terminal is not it — fall back to the ancestor, which at least names
        # what this window was pointed at.
        live_p = _forward_fork(anc, by_dir)
        if live_p in taken:
            live_p = anc
        if live_p not in taken:
            out[tid] = live_p
            taken.add(live_p)
    leftover = {}                            # 3. share out what remains, per project
    for tid, o in live:
        # A terminal that REPORTED its session id is excluded from the guess even when
        # the claim failed. Failure there means the transcript does not exist yet (a
        # session seconds old) or another window already holds it — in both cases we
        # know which session this is and it is not one of the leftovers. Guessing
        # anyway is how a brand-new terminal got handed a stranger's transcript:
        # observed live, a terminal reporting 1cd60a51 was served bc7fd9d1 because its
        # own file had not been written yet. "No transcript yet" is the true answer.
        if tid not in out and tid not in grounded:
            leftover.setdefault(o["cwd"], []).append(tid)
    for cwd, tids in leftover.items():
        files = []
        for f in by_dir.get(encode_path(cwd), ()):
            if f in taken:
                continue
            try:
                files.append((f.stat().st_mtime, f))
            except OSError:
                pass
        files.sort(key=lambda x: x[0], reverse=True)
        for tid, (_, f) in zip(tids, files):  # both sides already newest-first
            out[tid] = f
            taken.add(f)
    return out


def term_transcript(term_id):
    """Best-effort transcript for one live terminal: its slot in the global
    assignment. A plain shell, or a terminal with nothing left to claim, gets
    None — see _term_assignments for why this is resolved globally."""
    return _term_assignments().get(term_id)


def _live_session_id(term_id, cwd=None, rid=None):   # cwd/rid kept for callers
    """The claude session id this terminal actually reported, if any. Written to
    data/live/<term>.json by the statusline, which sees session_id on stdin and
    ZENITH_TERM_ID in its env — the only place the two are known together.

    This is GROUND TRUTH and outranks the spawn metadata: a terminal launched for
    project A can be `cd`'d and used to resume a session in project B, and only
    the statusline — which sees the live session_id — knows that happened. It is
    rewritten on every render, so it stays fresh for any active terminal."""
    try:
        rec = json.loads((DATA_DIR / "live" / (str(term_id) + ".json")).read_text())
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(rec, dict) or not rec.get("session_id"):
        return None
    return rec["session_id"]


def prompts_payload(path=None, term=None):
    """GET /api/prompts — {prompts:[{ts,text}], …} newest-LAST, for a transcript
    path or for whatever session a live terminal is driving."""
    if term:
        p = term_transcript(term)
        if not p:
            return {"prompts": [], "path": None,
                    "detail": "no transcript yet for this terminal"}
    else:
        try:
            p = Path(path or "").resolve()
        except (OSError, ValueError):
            return {"prompts": [], "path": None, "detail": "bad path"}
        roots = _transcript_roots()
        if not any(p == r or r in p.parents for r in roots) or not p.is_file():
            return {"prompts": [], "path": None, "detail": "not a session transcript"}
    try:
        pr = session_prompts(p)
    except OSError as e:
        return {"prompts": [], "path": str(p), "detail": str(e)[:120]}
    return {"prompts": pr, "path": str(p), "id": p.stem, "count": len(pr),
            "truncated": len(pr) >= PROMPTS_MAX}


# ------------------------------------------------------------- session-watch
# A separate daemon (~/.claude/session-watch) tails every live transcript and
# writes a prose read of each session — what it has been doing, what it is doing
# right now — into its OWN sqlite db every 120s. ZENITH is a READER and nothing
# else: the connection is opened mode=ro over a file: URI, so no amount of browser
# traffic can take a write lock on the daemon's WAL or corrupt it.
#
# The db is optional ON PURPOSE. A box where the watcher was never started, or is
# currently down, answers available:false — not a 500 — because the panel has to
# render either way and "no watcher" is a normal state, not an error.

def sw_connect():
    if not SESSION_WATCH_DB.exists():
        return None
    return sqlite3.connect(f"file:{SESSION_WATCH_DB}?mode=ro", uri=True, timeout=2)


def _sw_query(sql, args=()):
    """Rows as dicts, or None when the watcher db is absent/unreadable — the
    caller turns that None into available:false."""
    con = sw_connect()
    if con is None:
        return None
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _sw_age_min(ts):
    try:
        return round((time.time() - float(ts)) / 60, 1) if ts else None
    except (TypeError, ValueError):
        return None


def _sw_json(raw, default):
    try:
        v = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def _sw_project_name(project, cwd, encmap):
    """Readable name for a watcher row's mangled project dir.

    match_project is the same resolver /api/sessions uses, so ordinary projects
    display identically in both places. It misses ONLY claude's worktree dirs —
    it expects `<enc>--worktrees…` while claude writes `<enc>--claude-worktrees…`,
    because claude mangles the dot in `.claude` to a dash as well — so those fall
    back to the row's real cwd, which names the project and the worktree leaf
    without having to guess where the encoding put its separators."""
    proj = match_project(project or "", encmap)
    if proj:
        return Path(proj).name
    p = Path(cwd or "")
    try:
        rel = p.relative_to(PROJECTS_ROOT).parts
    except ValueError:
        rel = ()
    if rel:
        return rel[0] + (" · " + p.name if len(rel) > 1 else "")
    return (project or "").rsplit("-claudeProjects-", 1)[-1] or None


def session_watch_list(window=None):
    """GET /api/session-watch — every session the watcher knows, newest transcript
    write first, optionally only those touched in the last `window` minutes."""
    rows = _sw_query("SELECT session_id, project, cwd, branch, mtime, state, prompts,"
                     " errors, subagents, files_edited, tok_out, brief, now_line,"
                     " summary_at FROM sessions ORDER BY mtime DESC")
    if rows is None:
        return {"available": False, "sessions": [], "count": 0,
                "detail": "no session-watch db — the watcher is not running"}
    encmap = project_encoding_map()
    out = []
    for r in rows:
        age = _sw_age_min(r.get("mtime"))
        if window is not None and (age is None or age > window):
            continue
        r["project_name"] = _sw_project_name(r.get("project"), r.get("cwd"), encmap)
        r["age_min"] = age
        r["summary_age_min"] = _sw_age_min(r.pop("summary_at"))
        out.append(r)
    return {"available": True, "sessions": out, "count": len(out)}


SW_RECENT_N = 8          # narrative events returned with a session row
SW_RECENT_SCAN = 300     # how far back to look to find that many

# The watcher writes one event per tool call as well as one per prompt and per
# assistant say. The tool-call lines are MECHANICS — `BASH sed -n …`, `READ /path` —
# and answer "what did it type", not "what is going on". The prose lines are the
# explanation: a decision offered, a thing fixed, a conclusion reached. Any command
# meant for a HUMAN to run also lives inside prose, because a BASH event by
# definition is one the agent already ran itself.
#
# So the trail keeps SAY and USER and drops the rest. The aggregate mechanics are
# not lost — the Work section still counts every tool and names every file touched.
SW_NARRATIVE_KINDS = ("SAY", "USER")
_SW_EVENT_RE = re.compile(r"^\[(\d{1,2}:\d{2})\]\s*([A-Z][A-Z0-9_]*)\s*:?\s*(.*)$",
                          re.S)


def _sw_recent(sid):
    """The last few NARRATIVE events for one session, newest first, as
    [{seq, at, kind, text}].

    Structured rather than raw lines because the client needs the parts
    separately: `seq` addresses the event for a jump-to-transcript, `kind` picks
    the styling, and `at` is a column of its own.

    Scans SW_RECENT_SCAN rows to find SW_RECENT_N narrative ones — a busy session
    can run dozens of tool calls between two sentences, so taking the last N rows
    outright would return a trail made entirely of mechanics. Still one indexed
    read: the table is keyed (session_id, seq).

    Returns [] rather than None when the db is missing; the caller has already
    answered available:true off the sessions row, so an empty trail is the honest
    shape, not an error."""
    rows = _sw_query("SELECT seq, line FROM events WHERE session_id = ?"
                     " ORDER BY seq DESC LIMIT ?", (sid, SW_RECENT_SCAN))
    out = []
    for r in rows or []:
        m = _SW_EVENT_RE.match((r["line"] or "").strip())
        if not m or m.group(2) not in SW_NARRATIVE_KINDS:
            continue
        text = (m.group(3) or "").strip()
        if not text:
            continue
        out.append({"seq": r["seq"], "at": m.group(1), "kind": m.group(2),
                    "text": text})
        if len(out) >= SW_RECENT_N:
            break
    return out


def session_watch_session(session=None, path=None, term=None):
    """GET /api/session-watch/session — one full row, addressed three ways.

    `session` is the id (or any unique prefix of it); `path` and `term` resolve
    exactly the way /api/prompts does — term_transcript() for a live terminal,
    containment inside _transcript_roots() for a caller-supplied path. Both of
    those collapse to the same key, because the watcher's session_id IS the
    transcript filename stem."""
    sid = (session or "").strip()
    if not sid:
        if term:
            p = term_transcript(term)
            if not p:
                return {"available": False, "session_id": None,
                        "detail": "no transcript yet for this terminal"}
        else:
            try:
                p = Path(path or "").resolve()
            except (OSError, ValueError):
                return {"available": False, "session_id": None, "detail": "bad path"}
            roots = _transcript_roots()
            if not any(p == r or r in p.parents for r in roots) or not p.is_file():
                return {"available": False, "session_id": None,
                        "detail": "not a session transcript"}
        sid = p.stem
    rows = _sw_query("SELECT * FROM sessions WHERE session_id = ?", (sid,))
    if rows is None:
        return {"available": False, "session_id": sid,
                "detail": "no session-watch db — the watcher is not running"}
    # prefix form: the UI shows 8 chars of a uuid, so accept that as an address.
    # Guarded on the uuid charset because '%' and '_' are LIKE wildcards and a
    # session id can never legitimately contain either.
    if not rows and re.fullmatch(r"[0-9a-fA-F-]{4,64}", sid):
        rows = _sw_query("SELECT * FROM sessions WHERE session_id LIKE ?"
                         " ORDER BY mtime DESC", (sid + "%",))
    if not rows:
        return {"available": False, "session_id": sid,
                "detail": "the watcher has no record of this session yet"}
    r = rows[0]
    r["project_name"] = _sw_project_name(r.get("project"), r.get("cwd"),
                                         project_encoding_map())
    r["age_min"] = _sw_age_min(r.get("mtime"))
    r["summary_age_min"] = _sw_age_min(r.get("summary_at"))
    r["tools"] = _sw_json(r.get("tools_json"), [])
    r["files"] = _sw_json(r.get("files_json"), [])
    # Addressed by the RESOLVED id, not the caller's prefix: the events table keys on
    # the full session_id, so a prefix lookup would silently return nothing.
    r["recent"] = _sw_recent(r.get("session_id") or sid)
    return dict(r, available=True)


# ---- the watcher's config: read-only for the daemon, owned here ----------------
# ZENITH is the daemon's READER for state.db and its WRITER for config.json, and
# that is the whole coupling — no process management, no restart. The watcher
# re-reads this file on every pass, so a successful POST applies within one
# interval by itself.
#
# `api` is the shape of the HTTP call, not a vendor: ollama providers speak
# /api/chat, everything else is treated as OpenAI-compatible (/v1). Mirrors the
# same two-way split list_models() already makes.
SW_CONFIG_DEFAULTS = {          # == session_watch.py DEFAULTS (portable local Ollama)
    "enabled": True, "provider_id": "", "endpoint": "http://127.0.0.1:11434",
    "api": "ollama", "api_key": "", "model": "qwen3:8b", "num_ctx": 8192,
    "keep_alive": "30m", "interval": 120, "live_window": 60, "min_new": 8,
    "brief_every": 40,
}
# field -> (min, max). num_ctx is load-bearing rather than cosmetic: uncapped,
# Ollama reserves a KV cache for the model's full advertised context.
SW_CONFIG_BOUNDS = {"num_ctx": (1024, 131072), "interval": (15, 3600),
                    "live_window": (5, 1440), "min_new": (1, 500),
                    "brief_every": (1, 1000)}


def _sw_config_read():
    """The file merged over the defaults, WITH the api_key. Internal only —
    never hand this dict to a response; session_watch_config() is the public view."""
    cfg = dict(SW_CONFIG_DEFAULTS)
    on_disk = _load_json(SESSION_WATCH_CONFIG, {})
    if isinstance(on_disk, dict):
        cfg.update(on_disk)          # unknown keys (_comment) ride along untouched
    return cfg


def session_watch_config():
    """GET /api/session-watch/config — the config MINUS the secret.

    api_key never crosses to a browser; `has_key` is the only thing the UI needs
    to know about it (and the UI never offers to set one — the key comes from the
    chosen provider, server-side)."""
    cfg = _sw_config_read()
    prov = _provider(cfg.get("provider_id") or "")
    return {"enabled": bool(cfg.get("enabled")),
            "provider_id": cfg.get("provider_id") or "",
            "provider_name": (prov or {}).get("name") or "",
            "provider_missing": bool(cfg.get("provider_id")) and prov is None,
            "model": cfg.get("model") or "",
            "endpoint": cfg.get("endpoint") or "", "api": cfg.get("api") or "",
            "has_key": bool(cfg.get("api_key")),
            "keep_alive": cfg.get("keep_alive") or "30m",
            "num_ctx": cfg["num_ctx"], "interval": cfg["interval"],
            "live_window": cfg["live_window"], "min_new": cfg["min_new"],
            "brief_every": cfg["brief_every"],
            "exists": SESSION_WATCH_CONFIG.exists()}


def _sw_config_int(body, key, out):
    """One bounded whole number, or ValueError naming the field. Absent = keep."""
    if key not in body or body[key] is None or body[key] == "":
        return
    lo, hi = SW_CONFIG_BOUNDS[key]
    v = body[key]
    try:
        if isinstance(v, bool):
            raise ValueError
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{key}: must be a whole number between {lo} and {hi}")
    if not lo <= n <= hi:
        raise ValueError(f"{key}: {n} is out of range — must be {lo}–{hi}")
    out[key] = n


def session_watch_config_save(body):
    """POST /api/session-watch/config.

    endpoint/api/api_key are DERIVED from provider_id here and are NOT accepted
    from the client: taking them off the wire would let any browser point the
    watcher at an arbitrary host, or plant a key. The client picks an id out of
    data/providers.json and the server does the rest.

    Raises ValueError (→ 400, naming the field) and leaves the file untouched."""
    out = _sw_config_read()
    enabled = bool(body.get("enabled", True))
    pid = str(body.get("provider_id") or "").strip()
    model = str(body.get("model") or "").strip()
    if enabled and not pid:
        raise ValueError("provider_id: required unless summaries are off")
    if enabled and not model:
        raise ValueError("model: required unless summaries are off")
    if pid:
        prov = _provider(pid)
        if prov is None:
            raise ValueError("provider_id: no provider with id " + pid)
        # _provider_base, not base_url: a provider may list `fallbacks` so one entry
        # reaches the same box on the LAN AND over Tailscale, and the watcher stores
        # ONE endpoint — so it has to be a reachable one. Same resolver list_models
        # uses, cached ~30s; single-endpoint providers skip the probe entirely.
        out["endpoint"] = _provider_base(prov)
        out["api"] = ("ollama" if str(prov.get("type") or "").strip().lower()
                      == "ollama" else "openai")
        out["api_key"] = str(prov.get("api_key") or "")
    # provider "None": endpoint/api/api_key are left exactly as they were rather
    # than blanked, so turning summaries back on is one dropdown, not a re-setup.
    for key in SW_CONFIG_BOUNDS:
        _sw_config_int(body, key, out)
    out["enabled"] = enabled
    out["provider_id"] = pid
    out["model"] = model
    _sw_config_write(out)
    return dict(session_watch_config(), saved=True)


def _sw_config_write(cfg):
    """Atomic + 0600. Atomic because the watcher reads this file on a timer and
    must never catch a half-written one; 0600 because it carries a provider's
    api_key. The temp file is created 0600 too — a default-umask temp would be
    world-readable for the instant before the rename."""
    SESSION_WATCH_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_WATCH_CONFIG.with_name(SESSION_WATCH_CONFIG.name + ".tmp")
    with os.fdopen(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                           0o600), "w") as f:
        f.write(json.dumps(cfg, indent=2) + "\n")
    os.replace(tmp, SESSION_WATCH_CONFIG)
    os.chmod(SESSION_WATCH_CONFIG, 0o600)


def kill_term(term_id):
    _workspace_forget(term_id)   # explicit KILL = don't rebuild it on next boot
    term = TERMS.get(term_id)
    if not term:
        # not in memory but the tmux session may still exist — kill it directly
        if TMUX_BIN and not IS_WINDOWS:
            subprocess.run([TMUX_BIN, "kill-session", "-t", f"zenith-{term_id}"],
                           capture_output=True)
        zs.emit("term.close", ref=term_id, outcome="killed", actor="user",
                data={"project": None, "lifetime_s": None})
        return True
    term["_killed"] = True       # _term_reader's reap emits term.close(killed)
    term["pty"].terminate()
    term["status"] = "dead"
    with _terms_lock:
        TERMS.pop(term_id, None)
    return True


# ------------------------------------------------------------------ builders
# The third shape of prime-agent on the GPU box, after the terminal and the job: a
# FULL-DUPLEX session. An ssh child holds `--mode rpc` open on the remote container,
# ZENITH owns both of its pipes, and the browser gets a chat panel that can steer the
# agent WHILE it is running.
#
# Why neither of the other two would do. A job's stdin is DEVNULL and it answers
# exactly once, so there is nothing to steer. A terminal is a PTY carrying ANSI, so
# the only thing a panel can do with it is draw a screen — no tool call in it is
# addressable as an object. RPC mode is the one that emits structured events, and
# structure is the whole reason a Builder panel exists rather than another terminal.
#
# THE INJECTION BOUNDARY IS builder_send(). Every command reaching the child's stdin
# is built as a dict and serialised by json.dumps, so prompt text typed into a browser
# is JSON-escaped by construction and cannot introduce the newline that would make the
# child read it as a second command. Same discipline as the base64 prompt on the job
# path, expressed in the protocol's own encoding instead of around it.
BUILDERS = {}
_builders_lock = threading.Lock()
BUILDER_BUF_MAX = 1200          # projected frames kept for reconnect replay
_PA_VOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,110}$")


def pa_session_volume(worktree):
    """The docker volume holding this worktree's prime-agent session state.

    Derived from the worktree rather than random, and that IS the persistence design:
    the same worktree resumes the same session, so killing a builder and opening it
    again continues the conversation instead of meeting a stranger. Sanitised to
    docker's own name charset and prefixed, so it can neither collide with an
    unrelated volume nor start with a dash."""
    base = (worktree or "").rstrip("/").rsplit("/", 1)[-1] or "default"
    base = re.sub(r"[^A-Za-z0-9_.-]", "-", base)[:96].lstrip("-.")
    return "zenith-pa-" + (base or "default")


def _builder_argv(worktree, volume):
    """ssh argv for one RPC child. No -t ON PURPOSE: a TTY makes prime-agent start its
    interactive UI instead of speaking the protocol, which is the exact opposite of
    what the terminal mode wants from the same binary."""
    host, rpc = pa_cfg("host"), pa_cfg("rpc")
    if not (host and rpc):
        raise ValueError('prime-agent RPC is not configured — set "host" and "rpc" '
                         "in data/pa.json (or ZENITH_PA_HOST / ZENITH_PA_RPC)")
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            host, "--", f"mkdir -p {worktree} && exec {rpc} {worktree} {volume}"]


def _msg_text(content):
    """Flatten an AgentMessage content field (a string, or a list of typed blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(c.get("text") or "") for c in content
                       if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _tool_result_text(result):
    """The readable part of a tool result — its text blocks, or the object itself when
    it does not follow that shape."""
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        parts = [str(c.get("text") or "") for c in result["content"]
                 if isinstance(c, dict) and c.get("type") == "text"]
        if parts:
            return "\n".join(parts)
    return result


def _builder_snip(v, n):
    s = v if isinstance(v, str) else ("" if v is None else json.dumps(v, default=str))
    return s if len(s) <= n else s[:n] + "…"


def _builder_project(b, o):
    """One raw RPC event -> the frames the panel actually needs. Returns a list.

    This is a PROJECTION, not a proxy, and that is the point. prime-agent re-sends the
    ENTIRE accumulated message on every token, so a long answer is thousands of frames
    each carrying the whole text so far — forwarding the stream verbatim would push
    megabytes at the browser to convey a few hundred bytes of prose. Everything
    rendered here comes from the small `assistantMessageEvent` delta and the tool
    events; the fat `message` mirror is read only where it is the sole carrier of a
    fact (usage, stop reason, the user message a steer delivered)."""
    t = o.get("type")
    if t == "message_update":
        a = o.get("assistantMessageEvent") or {}
        at = a.get("type")
        if at == "text_delta":
            return [{"t": "assistant", "d": str(a.get("delta") or "")}]
        if at == "thinking_delta":
            return [{"t": "thinking", "d": str(a.get("delta") or "")}]
        if at == "text_end":
            return [{"t": "assistant_end"}]
        return []
    if t == "tool_execution_start":
        cid = str(o.get("toolCallId") or "")
        b["tools"][cid] = time.time()
        return [{"t": "tool", "id": cid, "status": "start",
                 "name": str(o.get("toolName") or "tool"),
                 "args": _builder_snip(o.get("args"), 400)}]
    if t == "tool_execution_end":
        cid = str(o.get("toolCallId") or "")
        started = b["tools"].pop(cid, None)
        # the protocol carries no duration, so time it here: start is the only other
        # place that sees this toolCallId, and the pair is what the chip needs.
        f = {"t": "tool", "id": cid, "status": "end",
             "name": str(o.get("toolName") or "tool"), "ok": not o.get("isError"),
             "out": _builder_snip(_tool_result_text(o.get("result")), 600)}
        if started:
            f["ms"] = int((time.time() - started) * 1000)
        return [f]
    if t == "agent_start":
        b["streaming"] = True
        return [{"t": "run", "phase": "start"}]
    if t == "agent_end":
        b["streaming"] = False
        return [{"t": "run", "phase": "end"}]
    if t == "message_end":
        m = o.get("message") or {}
        if m.get("role") == "assistant":
            u = m.get("usage") or {}
            b["model"] = str(m.get("model") or b.get("model") or "")
            return [{"t": "usage", "input": u.get("input"), "output": u.get("output"),
                     "total": u.get("totalTokens"), "model": b["model"],
                     "cost": (u.get("cost") or {}).get("total"),
                     "stop": m.get("stopReason")}]
        if m.get("role") == "user":
            # the transcript's own echo of what went in — including a steer the panel
            # never typed as a prompt, so the scrollback stays honest about ordering.
            return [{"t": "user", "d": _builder_snip(_msg_text(m.get("content")), 4000)}]
        return []
    if t == "session_action_update":
        acts = o.get("actions") or {}
        return [{"t": "queued", "n": acts.get("queuedCount") or 0,
                 "steering": [_builder_snip(x, 160) for x in (acts.get("steering") or [])]}]
    if t == "response":
        d = o.get("data") or {}
        if o.get("command") == "get_state" and o.get("success"):
            b["streaming"] = bool(d.get("isStreaming"))
            b["session_id"] = str(d.get("sessionId") or "")
            b["model"] = str((d.get("model") or {}).get("id") or b.get("model") or "")
            return [{"t": "state", "streaming": b["streaming"], "model": b["model"],
                     "session": b["session_id"], "messages": d.get("messageCount"),
                     "queued": (d.get("sessionActions") or {}).get("queuedCount") or 0}]
        if not o.get("success"):
            return [{"t": "error",
                     "d": "%s: %s" % (o.get("command"), o.get("error") or "failed")}]
        return []
    if t in ("compaction_start", "compaction_end", "auto_retry_start",
             "auto_retry_end", "extension_error"):
        return [{"t": "note", "d": t.replace("_", " "), "detail": _builder_snip(o, 300)}]
    return []


def _builder_emit(b, frame):
    """Buffer one projected frame for reconnect replay, then fan it out live."""
    data = json.dumps(frame, default=str).encode()
    with b["lock"]:
        buf = b["buf"]
        # coalesce consecutive text/thinking deltas IN THE BUFFER ONLY. A long answer
        # is thousands of one-token frames; replaying them individually would cost far
        # more than the prose is worth, while live connections still receive every
        # delta as it lands — which is what makes the panel stream rather than blink.
        if frame.get("t") in ("assistant", "thinking") and buf and \
                buf[-1].get("t") == frame["t"]:
            buf[-1]["d"] = (buf[-1].get("d") or "") + (frame.get("d") or "")
        else:
            buf.append(dict(frame))
            if len(buf) > BUILDER_BUF_MAX:
                del buf[: len(buf) - BUILDER_BUF_MAX]
        conns = list(b["conns"])
    for c in conns:
        try:
            ws_send(c, data, 1)
        except OSError:
            with b["lock"]:
                if c in b["conns"]:
                    b["conns"].remove(c)


def _builder_reader(b):
    r"""Parse the child's stdout as strict JSONL and broadcast the projection.

    Binary readline ON PURPOSE. The RPC framing is "split on \n, nothing else",
    because U+2028/U+2029 are legal inside JSON strings and do appear there. Binary
    readline splits on b"\n" alone; text mode would also break on a lone \r (universal
    newlines) and a Node-style line reader would also break on the Unicode separators
    — either of which corrupts a frame that merely CONTAINS one."""
    out = b["proc"].stdout
    while True:
        try:
            raw = out.readline()
        except (OSError, ValueError):
            raw = b""
        if not raw:
            break
        line = raw.rstrip(b"\r\n").decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            _builder_emit(b, {"t": "error", "d": "unparseable frame: " + line[:200]})
            continue
        if isinstance(obj, dict):
            for frame in _builder_project(b, obj):
                _builder_emit(b, frame)
    b["status"] = "dead"
    b["streaming"] = False
    _builder_emit(b, {"t": "exit"})
    try:
        life = round(time.time() - _iso_epoch(b.get("created")), 0)
    except (TypeError, ValueError):
        life = None
    zs.emit("builder.close", project=b.get("worktree") or "", ref=b["id"],
            outcome="killed" if b.get("_killed") else "ok", actor="user",
            data={"worktree": b.get("worktree"), "lifetime_s": life})


def _builder_stderr(b):
    """ssh/docker/launcher failures arrive here, not on stdout — surface them in the
    panel instead of letting a broken session look merely silent. Also keeps the pipe
    drained, so a chatty stderr can never block the child."""
    err = b["proc"].stderr
    while True:
        try:
            raw = err.readline()
        except (OSError, ValueError):
            raw = b""
        if not raw:
            break
        line = raw.rstrip(b"\r\n").decode("utf-8", "replace").strip()
        if line:
            _builder_emit(b, {"t": "error", "d": line[:400]})


def builder_send(b, cmd):
    r"""Write one JSONL command to the child's stdin.

    ensure_ascii (the json default) is load-bearing here: it escapes U+2028/U+2029 to
    \uXXXX, so the bytes on the wire are pure ASCII and cannot contain anything a
    reader might mistake for a record separator — belt and braces on top of the
    child's own compliant framing."""
    if b.get("status") != "live":
        return False
    line = (json.dumps(cmd) + "\n").encode("utf-8")
    with b["wlock"]:
        try:
            b["proc"].stdin.write(line)
            b["proc"].stdin.flush()
            return True
        except (OSError, ValueError, AttributeError):
            b["status"] = "dead"
            return False


def builder_command(b, msg):
    """One browser frame -> one RPC command."""
    text = str(msg.get("text") or "")
    t = msg.get("t")
    if t == "prompt":
        if not text.strip():
            return False
        cmd = {"type": "prompt", "message": text}
        # a prompt sent mid-run MUST declare what to do with it or the child rejects
        # it outright. Queueing behind the current run is the least surprising reading
        # of "send" — STEER is the explicit button for cutting in.
        if b.get("streaming"):
            cmd["streamingBehavior"] = "followUp"
        return builder_send(b, cmd)
    if t == "steer":
        return bool(text.strip()) and builder_send(b, {"type": "steer", "message": text})
    if t == "abort":
        return builder_send(b, {"type": "abort"})
    if t == "state":
        return builder_send(b, {"type": "get_state"})
    if t == "new":
        return builder_send(b, {"type": "new_session"})
    return False


def spawn_builder(worktree=None):
    worktree = pa_worktree(worktree)
    volume = pa_session_volume(worktree)
    if not _PA_VOL_RE.match(volume):          # unreachable by construction; the last
        raise ValueError("bad session volume name")   # gate before an unquoted argv
    argv = _builder_argv(worktree, volume)
    bid = uuid.uuid4().hex[:10]
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    b = {"id": bid, "proc": proc, "worktree": worktree, "volume": volume,
         "status": "live", "streaming": False, "created": now_iso(),
         "session_id": "", "model": "", "buf": [], "conns": [], "tools": {},
         "label": "builder · " + (worktree.rstrip("/").rsplit("/", 1)[-1] or "remote"),
         "lock": threading.Lock(), "wlock": threading.Lock()}
    with _builders_lock:
        BUILDERS[bid] = b
    zs.emit("builder.open", project=worktree, ref=bid, outcome="ok", actor="user",
            data={"worktree": worktree, "volume": volume})
    threading.Thread(target=_builder_reader, args=(b,), daemon=True).start()
    threading.Thread(target=_builder_stderr, args=(b,), daemon=True).start()
    builder_send(b, {"type": "get_state"})   # seed the panel header before any prompt
    return bid


def kill_builder(bid):
    b = BUILDERS.get(bid)
    if not b:
        return False
    b["_killed"] = True
    b["status"] = "dead"
    try:
        b["proc"].stdin.close()    # EOF ends the RPC session cleanly; terminate below
    except (OSError, ValueError, AttributeError):    # is the fallback for a wedged one
        pass
    try:
        b["proc"].terminate()
    except OSError:
        pass
    with _builders_lock:
        BUILDERS.pop(bid, None)
    return True


def builder_public(b):
    return {"id": b["id"], "worktree": b.get("worktree"), "volume": b.get("volume"),
            "label": b.get("label"), "status": b.get("status"),
            "streaming": bool(b.get("streaming")), "model": b.get("model") or "",
            "session": b.get("session_id") or "", "created": b.get("created"),
            "conns": len(b.get("conns") or [])}


# --------------------------------------------------- live claude processes
# The Sessions app lists transcripts, which are just files — but some of them have a
# claude STILL RUNNING behind them, and those accumulate: forked app sessions that
# outlive their window, a session resumed twice, a `claude /login` nobody finished.
# This maps running claude processes back onto sessions so they can be ended from
# the same place you read them.

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ACTIVE_WINDOW_S = 120        # transcript touched this recently => "working right now"


def _etime_seconds(s):
    """ps etime ([[dd-]hh:]mm:ss) -> seconds. Returns None if unparseable."""
    try:
        s = s.strip()
        days = 0
        if "-" in s:
            d, s = s.split("-", 1)
            days = int(d)
        parts = [int(x) for x in s.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    except (ValueError, IndexError):
        return None


def _is_claude_cmd(cmd):
    """True for a real claude CLI invocation — NOT for a shell that merely mentions
    claude in its arguments (tool wrappers, greps), which is why this keys off argv[0]."""
    if not cmd:
        return False
    exe = cmd.split()[0]
    return (os.path.basename(exe) == "claude" or exe.startswith("claude")
            or "/claude/versions/" in exe)


def _session_id_from_cmd(cmd):
    """--session-id wins over --resume: with --fork-session the process WRITES the
    new id while --resume names the transcript it forked from."""
    m = re.search(r"--session-id[= ]+(\S+)", cmd)
    if m and _UUID_RE.fullmatch(m.group(1)):
        return m.group(1)
    m = re.search(r"--resume[= ]+(\S+)", cmd)
    if m:
        v = m.group(1)
        stem = os.path.basename(v)[:-6] if v.endswith(".jsonl") else v
        if _UUID_RE.fullmatch(stem):
            return stem
    return None


def _classify_claude(cmd, ppid, server_pid):
    if ppid == server_pid:
        return "zenith"
    if "ClaudeCode.app" in cmd:
        return "app"
    if re.search(r"\b(daemon run|bg-pty-host|bg-spare)\b", cmd):
        return "daemon"
    return "cli"


def _term_for_session(sid):
    """The zenith terminal driving this session, if any — killing through
    kill_term() also tears down tmux + the workspace entry, which a bare
    signal would leave behind."""
    if not sid:
        return None
    with _terms_lock:
        for t in TERMS.values():
            if t.get("resume_id") and str(t["resume_id"]) == sid:
                return t["id"]
    return None


def claude_processes():
    """Live claude processes, newest-first, annotated with the session each one is
    driving. Children of another claude in the list are marked so the UI offers one
    kill per tree rather than one per pid."""
    if IS_WINDOWS:
        return []                      # ps(1) shape is Unix-only; no Windows path yet
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid=,etime=,command="],
                             capture_output=True, text=True, timeout=6).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    server_pid = os.getpid()
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, ppid_s, et, cmd = parts
        try:
            pid, ppid = int(pid_s), int(ppid_s)
        except ValueError:
            continue
        if pid == server_pid or not _is_claude_cmd(cmd):
            continue
        sid = _session_id_from_cmd(cmd)
        rows.append({"pid": pid, "ppid": ppid, "age_s": _etime_seconds(et),
                     "cmd": cmd[:400], "session": sid,
                     "origin": _classify_claude(cmd, ppid, server_pid),
                     "term_id": _term_for_session(sid)})
    known = {r["pid"] for r in rows}
    seen_sessions = collections.Counter(r["session"] for r in rows
                                        if r["session"] and r["ppid"] not in known)
    encmap = project_encoding_map()
    for r in rows:
        r["child"] = r["ppid"] in known          # killing the root takes this with it
        r["duplicate"] = bool(r["session"]) and seen_sessions[r["session"]] > 1
        r["project"] = r["active"] = r["title"] = r["path"] = None
        if not r["session"]:
            continue
        names = None
        for d in (TRANSCRIPTS_ROOT.iterdir() if TRANSCRIPTS_ROOT.exists() else ()):
            p = d / (r["session"] + ".jsonl")
            if d.is_dir() and p.is_file():
                r["path"] = str(p)
                r["project"] = match_project(d.name, encmap) or d.name
                try:                              # "working right now" is provable
                    r["active"] = (time.time() - p.stat().st_mtime) < ACTIVE_WINDOW_S
                except OSError:
                    pass
                try:
                    # name it the way the Sessions list does — a bare pid and a
                    # truncated argv tell you nothing about what a process IS
                    if names is None:
                        names = _load_json(SESSION_NAMES_FILE, {})
                    s = session_summary(p)
                    r["title"] = ((names or {}).get(str(p))
                                  or s.get("title") or s.get("first_prompt"))
                except (OSError, ValueError):
                    pass
                break
    rows.sort(key=lambda r: (r["age_s"] is None, r["age_s"] or 0))
    return rows


def kill_claude_proc(pid, force=False):
    """End one claude process. Only pids that claude_processes() currently reports
    are eligible — this is a session-manager, not a general process killer."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad pid"}
    if pid in (os.getpid(), os.getppid()):
        return {"ok": False, "error": "refusing to kill the ZENITH server"}
    match = next((p for p in claude_processes() if p["pid"] == pid), None)
    if not match:
        return {"ok": False, "error": "not a live claude process"}
    if match.get("term_id"):          # zenith-owned: tear down tmux + workspace too
        kill_term(match["term_id"])
        return {"ok": True, "pid": pid, "via": "term", "session": match.get("session")}
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return {"ok": True, "pid": pid, "already_gone": True}
    except PermissionError:
        return {"ok": False, "error": "not permitted to signal that pid"}
    zs.emit("claude.kill", project=match.get("project"), ref=str(pid), outcome="killed",
            actor="user", data={"origin": match.get("origin"),
                                "session": match.get("session"),
                                "age_s": match.get("age_s")})
    return {"ok": True, "pid": pid, "signal": "KILL" if force else "TERM",
            "session": match.get("session")}


# ---------------------------------------------------------------- platform / deps

# pip package name -> import (module) name. Only these may be installed via the API.
INSTALLABLE = {"pywinpty": "winpty", "pywhispercpp": "pywhispercpp",
               "aider-chat": "aider"}

# System (non-pip) packages ZENITH can install for you. STRICT whitelist: a request
# only ever selects a KEY here — the argv is built from these hardcoded lists, so no
# caller-supplied string ever reaches a package manager.
# `impact` is the consequence of NOT having it, in plain language. tmux especially:
# calling it "recommended" undersells "every restart destroys all your sessions".
SYS_INSTALLABLE = {
    "tmux": {
        "purpose": "persistent terminals — sessions survive a ZENITH restart",
        "impact": "without it every terminal is a child of this process, so restarting "
                  "ZENITH kills all of them (and the agent sessions inside)",
        "pkg": "tmux", "unix_only": True,
    },
    "ffmpeg": {
        "purpose": "audio conversion for local Whisper voice input",
        "impact": "without it voice-to-text falls back to browser speech recognition",
        "pkg": "ffmpeg", "unix_only": False,
    },
}

# package manager -> (argv prefix, needs_sudo). First match on PATH wins.
_PMS = [("brew", ["install"], False), ("apt-get", ["install", "-y"], True),
        ("dnf", ["install", "-y"], True), ("pacman", ["-S", "--noconfirm"], True),
        ("zypper", ["install", "-y"], True), ("apk", ["add"], True)]


def _pkg_manager():
    """(bin, argv_prefix, needs_sudo) for this box, or None. brew never needs sudo."""
    for name, args, sudo in _PMS:
        found = shutil.which(name)
        if found:
            return found, args, (sudo and os.geteuid() != 0)
    return None


def _module_installed(mod):
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _sys_dep_state(name):
    spec = SYS_INSTALLABLE[name]
    pm = _pkg_manager()
    installed = bool(TMUX_BIN) if name == "tmux" else bool(shutil.which(name))
    # A manual command to show when we can't do it ourselves (no PM, or sudo needs a password).
    manual = None
    if pm:
        manual = " ".join((["sudo"] if pm[2] else []) + [os.path.basename(pm[0])]
                          + pm[1] + [spec["pkg"]])
    return {"installed": installed, "needed": False, "system": True,
            "purpose": spec["purpose"], "impact": spec["impact"],
            "installable": bool(pm) and not (spec["unix_only"] and IS_WINDOWS),
            "manual": manual}


def platform_status():
    sys_deps = {}
    for name, spec in SYS_INSTALLABLE.items():
        if spec["unix_only"] and IS_WINDOWS:
            continue            # tmux is meaningless on native Windows (ConPTY instead)
        sys_deps[name] = _sys_dep_state(name)
    return {
        "system": platform.system(), "is_windows": IS_WINDOWS, "python": sys.executable,
        # kept as plain booleans for back-compat with older frontends
        "tmux": bool(TMUX_BIN), "ffmpeg": bool(shutil.which("ffmpeg")),
        "sys_deps": sys_deps,
        "deps": {
            "pywinpty": {"installed": _module_installed("winpty"),
                         "needed": IS_WINDOWS, "purpose": "in-OS terminal (ConPTY)"},
            "pywhispercpp": {"installed": _module_installed("pywhispercpp"),
                             "needed": False, "purpose": "local Whisper voice (else browser speech)"},
        },
    }


def install_sys_dep(name):
    """Install one whitelisted SYSTEM package. Never takes a caller string as argv."""
    global TMUX_BIN
    spec = SYS_INSTALLABLE.get(name)
    if not spec:
        return {"ok": False, "error": "not an installable system package"}
    if spec["unix_only"] and IS_WINDOWS:
        return {"ok": False, "error": f"{name} is not used on Windows"}
    pm = _pkg_manager()
    if not pm:
        return {"ok": False, "error": "no supported package manager found "
                                      "(brew/apt-get/dnf/pacman/zypper/apk)"}
    pm_bin, pm_args, needs_sudo = pm
    # -n: never prompt for a password. A web request must not hang on a TTY that
    # isn't there; if sudo isn't passwordless we say so and hand back the command.
    argv = (["sudo", "-n"] if needs_sudo else []) + [pm_bin] + pm_args + [spec["pkg"]]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=900,
                           errors="replace")
        code, out = r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        code, out = 1, f"{os.path.basename(pm_bin)} timed out after 900s"
    except OSError as e:
        code, out = 1, f"could not run {os.path.basename(pm_bin)}: {e}"
    if code != 0 and needs_sudo and "password" in out.lower():
        out += (f"\n\npasswordless sudo isn't configured — run this yourself:\n"
                f"  {_sys_dep_state(name)['manual']}")
    if name == "tmux":
        # TMUX_BIN is resolved once at import, so a fresh install would otherwise stay
        # invisible until a restart — and that restart would kill the very sessions
        # persistence is meant to protect. Re-resolve so the NEXT terminal is tmux-backed.
        TMUX_BIN = shutil.which("tmux")
    installed = _sys_dep_state(name)["installed"]
    zs.emit("deps.install", ref=name, outcome="ok" if installed else "fail", actor="user",
            data={"package": name, "kind": "system", "code": code})
    return {"ok": code == 0 and installed, "code": code, "output": out[-6000:],
            "installed": {name: installed}, "packages": [name],
            "cmd": " ".join(argv), "restart_required": False}


def install_deps(packages):
    pkgs = [p for p in (packages or []) if p in INSTALLABLE]
    if not pkgs:
        return {"ok": False, "error": "no installable package requested"}
    py = sys.executable or ("python" if IS_WINDOWS else "python3")

    def run(extra):
        cmd = [py, "-m", "pip", "install", "--user"] + extra + pkgs
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                               errors="replace")
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            return 1, "pip install timed out after 900s"
        except OSError as e:
            return 1, f"could not run pip: {e}"

    code, out = run([])
    if code != 0 and "externally-managed" in out.lower():
        code, out2 = run(["--break-system-packages"])
        out += "\n\n[retry with --break-system-packages]\n" + out2
    importlib.invalidate_caches()
    installed = {p: _module_installed(INSTALLABLE[p]) for p in pkgs}
    return {"ok": code == 0, "code": code, "output": out[-6000:],
            "installed": installed, "packages": pkgs}


# ---------------------------------------------------------------- overview

_update_cache = {"checked": 0.0, "data": None}


def _semver_tuple(v):
    out = []
    for part in str(v).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def update_check():
    """Compare the running release against the latest GitHub release (cached 1h).
    Degrades to available=False on any error — including a private-repo 404, so the
    UI banner stays dormant until the repo is public (or ZENITH_REPO_SLUG points at a
    public releases feed)."""
    now = time.time()
    c = _update_cache
    if c["data"] is not None and now - c["checked"] < 3600:
        return c["data"]
    res = {"current": ZENITH_RELEASE, "latest": None, "available": False, "url": None}
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{ZENITH_REPO_SLUG}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "zenith-os"})
        d = json.loads(urllib.request.urlopen(req, timeout=4).read())
        latest = (d.get("tag_name") or "").lstrip("v")
        res["latest"] = latest or None
        res["url"] = d.get("html_url")
        res["available"] = bool(latest) and _semver_tuple(latest) > _semver_tuple(ZENITH_RELEASE)
    except Exception as e:
        res["error"] = str(e)[:200]
    c["checked"], c["data"] = now, res
    return res


def tailscale_state():
    """Local Tailscale auth/connection state. needs_reauth=True means fleet calls
    that route over the tailnet will fail until the user re-authenticates. Hosts
    without Tailscale report installed=False (LAN-only; never nag)."""
    if not TAILSCALE_BIN:
        return {"installed": False, "needs_reauth": False}
    try:
        r = subprocess.run([TAILSCALE_BIN, "status", "--json"],
                           capture_output=True, text=True, timeout=4)
        d = json.loads(r.stdout or "{}")
    except Exception as e:
        return {"installed": True, "needs_reauth": False, "error": str(e)[:200]}
    state = d.get("BackendState")
    self_ = d.get("Self") or {}
    health = d.get("Health") or []
    online = self_.get("Online")
    needs = (state in ("NeedsLogin", "Stopped", "NoState")
             or online is False
             or any(("expir" in str(h).lower() or "log in" in str(h).lower()
                     or "login" in str(h).lower()) for h in health))
    return {"installed": True, "backend_state": state, "online": online,
            "health": health, "auth_url": d.get("AuthURL") or None,
            "needs_reauth": bool(needs)}


def tailscale_reauth():
    """Best-effort: surface a way to re-authenticate. Prefers a pending AuthURL,
    else opens the Tailscale app (macOS), else returns an instruction."""
    if not TAILSCALE_BIN:
        return {"error": "tailscale not installed"}
    st = tailscale_state()
    if st.get("auth_url"):
        return {"auth_url": st["auth_url"]}
    if IS_DARWIN:
        try:
            subprocess.run(["open", "-a", "Tailscale"], timeout=4)
            return {"opened_app": True,
                    "note": "Opened the Tailscale app — click 'Log in', then Recheck."}
        except Exception:
            pass
    return {"note": "Re-authenticate Tailscale (menubar app or `tailscale login`), then Recheck."}


def gpu_nodes():
    """GPU compute nodes the fleet can dispatch jobs to. Configure via
    data/gpu_nodes.json ({"name": "http://host:port"}, gitignored) or the
    ZENITH_GPU_NODES env (same JSON); ships EMPTY so no host is hardcoded.
    ZENITH is the control plane; these nodes just run predefined GPU jobs."""
    try:
        return json.loads((DATA_DIR / "gpu_nodes.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        return json.loads(os.environ.get("ZENITH_GPU_NODES") or "{}")
    except Exception:
        return {}


def gpu_call(node, path, timeout=15):
    """Proxy a GET to a GPU node's job endpoint (server-side, over the LAN)."""
    url = gpu_nodes().get(node)
    if not url:
        return {"error": "unknown node: " + str(node)}
    try:
        req = urllib.request.Request(url.rstrip("/") + path, headers={"User-Agent": "zenith"})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except Exception as e:
        return {"error": str(e)[:300]}


def _nm_api_token():
    """Resolve the NexusMind API bearer token. Prefers the 0600 token FILE
    (contents may be `NM_API_TOKEN=<tok>`, `<tok>`, or a KEY=VAL blob with that
    key) and falls back to the ZENITH_NM_TOKEN env. Returns "" if unavailable."""
    if ZENITH_NM_TOKEN_FILE:
        try:
            raw = Path(ZENITH_NM_TOKEN_FILE).expanduser().read_text(encoding="utf-8").strip()
            if "NM_API_TOKEN=" in raw:                # `NM_API_TOKEN=<tok>` (any line)
                for line in raw.splitlines():
                    line = line.strip()
                    if line.startswith("NM_API_TOKEN="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
            elif raw:                                 # raw token, no key
                return raw
        except Exception:
            pass
    return ZENITH_NM_TOKEN_ENV or ""


def nm_api(path, method="GET", body=None, timeout=5):
    """Proxy an authed HTTP call to the NexusMind API (server-side). ZENITH can't
    touch the NM DB directly (loopback-only in docker) so it talks to NM's HTTP
    API at ZENITH_NM_API with a REQUIRED bearer token. Never throws — returns the
    decoded JSON on success, or {"error": "..."} on any failure. Always bounded:
    the caps probe passes a shorter timeout than the data paths."""
    if _int_off("nexusmind_api"):             # off → no socket (§3.2)
        return {"error": "integration disabled"}
    token = _nm_api_token()
    if not token:
        return {"error": "no NexusMind API token (set ZENITH_NM_TOKEN_FILE or ZENITH_NM_TOKEN)"}
    url = ZENITH_NM_API.rstrip("/") + path
    headers = {"Authorization": "Bearer " + token, "User-Agent": "zenith"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:                       # surface NM's error body (e.g. 422 validator msg)
            eb = json.loads(e.read())
            detail = str(eb.get("message") or eb.get("error") or "")[:300]
        except (ValueError, OSError, AttributeError, TypeError):
            pass
        return {"error": "nm_api %s %s -> HTTP %s" % (method, path, e.code),
                "code": e.code, "detail": detail}
    except Exception as e:
        return {"error": str(e)[:300]}


# ------------------------------------------------------- watchers git write-through
# Repo-first rule: the NM DB is the live store the admin API
# already wrote; the canonical YAML must ALSO land in the homelab repo. Git
# failures are reported but never fail the request (push especially — no deploy
# key yet), and an absent checkout (dev box) skips git entirely.

_WATCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _watchers_repo_dir():
    """The homelab checkout as a Path, or None when git write-through is off
    (dir/.git missing or no git binary — e.g. a dev box)."""
    if _int_off("homelab") or not ZENITH_HOMELAB_DIR:   # off/empty — never resolve to CWD
        return None
    d = Path(ZENITH_HOMELAB_DIR)
    if d.is_dir() and (d / ".git").exists() and shutil.which("git"):
        return d
    return None


def _watchers_git(args, timeout=30):
    """git in the homelab repo. ZENITH runs as root on the mini but the checkout
    belongs to a normal user, so root wraps it in `sudo -u <owner>`; a dev run uses
    plain git. Returns (rc, combined output)."""
    cmd = ["git", "-C", ZENITH_HOMELAB_DIR] + args
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        cmd = ["sudo", "-u", ZENITH_HOMELAB_GIT_USER, "-n"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()[:400]
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)[:200]


def _yaml_has_comments(text):
    """True if the YAML carries a `#` comment — full-line or trailing.

    Quote-aware, so a `#` inside a value is not a comment: a URL fragment
    (`...?ids=bitcoin#frag`) or a message body ("BTC #1") must not trip this.
    A `#` counts only when it is outside quotes AND starts the line or follows
    whitespace, which is YAML's own rule for where a comment can begin.
    """
    for line in text.splitlines():
        quote, prev = None, ""
        for ch in line:
            if quote:
                if ch == quote and prev != "\\":
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "#" and (prev == "" or prev.isspace()):
                return True
            prev = ch
    return False


def _watchers_comment_guard(name):
    """Refusal message if watcher <name>'s YAML carries comments, else None.

    A console save round-trips the spec through NexusMind's JSONB `spec` column,
    which carries no comments, so re-rendering a hand-annotated watcher would
    silently delete its prose — and the diff would look like a harmless reformat.
    a cert watcher, for instance, documents the sandbox rationale and warns
    that `args` are untrusted console input; losing that costs real safety
    context. Refuse rather than quietly destroy it.
    """
    repo = _watchers_repo_dir()
    if repo is None:
        return None                               # dev box / repo absent — skip
    path = repo / ("mini/watchers/%s.yaml" % name)
    try:
        if not path.exists() or not _yaml_has_comments(
                path.read_text(encoding="utf-8")):
            return None
    except OSError:
        return None                               # unreadable — let the write try
    return ("refused: mini/watchers/%s.yaml carries # comments that a console "
            "save would silently delete (the spec round-trips through JSONB, "
            "which carries no comments). Edit the file in the homelab repo "
            "instead, or move its prose into a sibling .md, then save again."
            % name)


def _watchers_commit(name, yaml_text=None, delete=False):
    """Write (or `git rm`) mini/watchers/<name>.yaml, commit, best-effort push.
    Never throws; returns {"committed": bool, "pushed": bool} plus git_error /
    push_error strings when something non-fatal went wrong.

    Overwriting a comment-carrying file is refused here as well as in the save
    endpoint's pre-flight — this is the actual write site, so the guarantee holds
    for any caller, not just the one path that remembers to ask first."""
    out = {"committed": False, "pushed": False}
    repo = _watchers_repo_dir()
    if repo is None:
        return out                                # dev box / repo absent — skip
    rel = "mini/watchers/%s.yaml" % name
    path = repo / rel
    if not delete:                                # delete is explicit intent; git keeps history
        refusal = _watchers_comment_guard(name)
        if refusal:
            out["refused"] = True
            out["git_error"] = refusal
            return out
    try:
        if delete:
            if not path.exists():
                return out                        # nothing tracked — nothing to do
            rc, msg = _watchers_git(["rm", "-q", "--", rel])
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml_text or "", encoding="utf-8")
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                try:                              # keep the checkout owned by its user
                    shutil.chown(path, user=ZENITH_HOMELAB_GIT_USER)
                except (OSError, LookupError, KeyError):
                    pass                          # root-owned file still commits
            rc, msg = _watchers_git(["add", "--", rel])
        if rc != 0:
            out["git_error"] = msg
            return out
        rc, msg = _watchers_git(["status", "--porcelain", "--", rel])
        if rc == 0 and not msg:                   # identical content already committed
            out["committed"] = True
            return out
        verb = "delete" if delete else "upsert"
        rc, msg = _watchers_git(["commit", "-m",
                                 "watchers: %s %s (via ZENITH)" % (verb, name)])
        if rc != 0:
            out["git_error"] = msg
            return out
        out["committed"] = True
        rc, msg = _watchers_git(["push"], timeout=60)   # may fail: no deploy key yet
        out["pushed"] = rc == 0
        if rc != 0:
            out["push_error"] = msg               # NON-fatal by design
    except OSError as e:
        out["git_error"] = str(e)[:200]
    return out


def overview():
    n_sessions = sum(1 for d in TRANSCRIPTS_ROOT.iterdir() if d.is_dir()
                     for _ in d.glob("*.jsonl")) if TRANSCRIPTS_ROOT.exists() else 0
    coord = coordination_state()
    live = sum(1 for p in coord["projects"] for s in p["sessions"] if not s.get("stale"))
    meta = nm_meta()
    terms_live = sum(1 for t in TERMS.values() if t["status"] == "live")
    loops_enabled = sum(1 for lp in loops_load() if lp.get("enabled"))
    return {"version": ZENITH_RELEASE, "projects": len(list_projects()), "sessions": n_sessions,
            "memories": meta.get("total", 0) if meta.get("available") else None,
            "nm_available": meta.get("available", False),
            "agents": len(list_agents()), "skills": len(list_skills()),
            "live_sessions": live, "claude_bin": CLAUDE_BIN,
            "jobs_running": sum(1 for j in JOBS.values() if j["status"] == "running"),
            "terms_live": terms_live, "loops_enabled": loops_enabled, "link": "ok",
            "tmux": bool(TMUX_BIN),
            "time": now_iso()}


# ---------------------------------------------------------------- voice / whisper
# Speech-to-text, best-available: (1) flowd — the VoiceFlow daemon (parakeet
# streaming engine, ~/claudeProjects/voiceflow) when it's running on :8787 —
# far more accurate than tiny.en; (2) local pywhispercpp (lazy-imported);
# (3) frontend falls back to browser SpeechRecognition when both unavailable.

FLOWD_URL = os.environ.get("FLOWD_URL", "http://127.0.0.1:8787")
WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "tiny.en")   # config-resolved at boot
_flowd_cache = {"t": 0.0, "ok": False}


def _flowd_healthy():
    """True if the VoiceFlow daemon answers /health ok. 2s result cache so the
    mic path never adds more than one probe per burst."""
    now = time.time()
    if now - _flowd_cache["t"] < 2.0:
        return _flowd_cache["ok"]
    ok = False
    try:
        with urllib.request.urlopen(FLOWD_URL + "/health", timeout=0.8) as r:
            ok = json.load(r).get("status") == "ok"
    except Exception:                            # noqa: BLE001 — daemon down/absent
        ok = False
    _flowd_cache.update(t=now, ok=ok)
    return ok


def _flowd_transcribe(audio, filename):
    """Forward the original upload to flowd's ZENITH-compatible multipart
    endpoint. Raises on any failure — caller falls back to pywhispercpp."""
    boundary = uuid.uuid4().hex
    ctype = {"wav": "audio/wav", "webm": "audio/webm"}.get(
        (os.path.splitext(filename or "")[1] or "").lstrip(".").lower(), "application/octet-stream")
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; "
            f"filename=\"{filename or 'audio.webm'}\"\r\nContent-Type: {ctype}\r\n\r\n"
            ).encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(FLOWD_URL + "/transcribe", data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    if "text" not in out:
        raise ValueError(out.get("error") or "flowd: no text")
    return {"text": out["text"], "engine": "flowd"}


_whisper_model = None
_whisper_lock = threading.Lock()


def _load_whisper_model():
    """Import + load (and cache module-global) the whisper model. Raises on
    failure — callers catch and degrade gracefully."""
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from pywhispercpp.model import Model   # lazy: optional dependency
            _whisper_model = Model(WHISPER_MODEL_NAME)
        return _whisper_model


def whisper_health():
    if _int_off("voice"):                    # off → skip flowd + pywhispercpp; browser tier
        return {"status": "unavailable", "engine": "browser", "model": None}
    if _flowd_healthy():
        return {"status": "ok", "model": "flowd/parakeet-tdt-0.6b-v3", "engine": "flowd"}
    try:
        _load_whisper_model()
        return {"status": "ok", "model": WHISPER_MODEL_NAME}
    except Exception as e:                       # noqa: BLE001 — graceful boundary
        return {"status": "unavailable", "error": str(e)}


def _parse_multipart_audio(content_type, body):
    """Minimal multipart/form-data parser (no cgi / third-party). Returns
    (filename, bytes) for the part named 'audio'. Raises ValueError otherwise."""
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    if not m:
        raise ValueError("no multipart boundary")
    boundary = (m.group(1) or m.group(2)).strip()
    delim = b"--" + boundary.encode()
    for part in body.split(delim):
        if not part or part in (b"--\r\n", b"--", b"\r\n", b"--\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        header_blob, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = header_blob.decode("latin-1", "replace")
        if 'name="audio"' not in headers:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        fm = re.search(r'filename="([^"]*)"', headers)
        return (fm.group(1) if fm and fm.group(1) else "audio.webm"), data
    raise ValueError("no audio part in multipart body")


def whisper_transcribe(audio, filename):
    if _int_off("voice"):                    # off → browser-fallback shape, no engines
        return {"error": "voice disabled", "engine": "browser", "text": ""}
    if not audio:
        return {"error": "empty audio", "text": ""}
    if _flowd_healthy():                          # best engine first; fall through on any failure
        try:
            return _flowd_transcribe(audio, filename)
        except Exception:                        # noqa: BLE001 — degrade to pywhispercpp
            _flowd_cache.update(t=time.time(), ok=False)
    tmpfiles = []
    try:
        WHISPER_TMP.mkdir(parents=True, exist_ok=True)
        ext = (os.path.splitext(filename or "")[1] or ".webm").lower()
        src = WHISPER_TMP / (uuid.uuid4().hex + ext)
        src.write_bytes(audio)
        tmpfiles.append(src)
        target = str(src)
        if ext != ".wav" and shutil.which("ffmpeg"):
            wav = src.with_suffix(".16k.wav")
            tmpfiles.append(wav)
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(src), "-ar", "16000", "-ac", "1",
                     "-f", "wav", str(wav)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=60, check=True)
                target = str(wav)
            except (OSError, subprocess.SubprocessError):
                target = str(src)                # fall back to raw file
        model = _load_whisper_model()
        segs = model.transcribe(target)
        raw = "".join(getattr(s, "text", "") for s in segs)
        text = re.sub(r"[^\w\s'-]", "", raw).strip()
        segments = [{"text": getattr(s, "text", ""),
                     "start": getattr(s, "t0", None),
                     "end": getattr(s, "t1", None)} for s in segs]
        return {"text": text, "segments": segments}
    except Exception as e:                       # noqa: BLE001 — graceful boundary
        return {"error": str(e), "text": ""}
    finally:
        for f in tmpfiles:
            try:
                f.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- capabilities
# Effective integration state = mode (auto|on|off) resolved against a time-bounded,
# CACHED probe that REUSES the existing detection logic (§3.2). `off` short-circuits
# each probe's chokepoint above before any subprocess/socket/file-stat. Each probe
# returns (detected: bool, detail: str). GET /api/capabilities assembles per-probe
# caches; a cold cache runs the non-off probes in parallel daemon threads with a
# bounded join so a call never exceeds the slowest single probe (~3.5s) and boot
# never blocks (a warm-up thread primes them once, main()).

CAPS = {"generated": 0.0, "integrations": {}}   # capabilities-level TTL cache (§4.3)
_caps_lock = threading.Lock()
CAPS_TTL = 15.0
CAPS_JOIN = 3.5                                  # parallel cold-probe join budget


def _probe_nexusmind_api():
    """token resolvable AND the NexusMind API answers an authed read. This one
    integration now gates BOTH surfaces that use that door (Memory, Watchers),
    so it probes the API itself (/api/namespaces) rather than one blueprint —
    a missing /api/watch/* must not hide a working memory module. 2.5s bound,
    ~30s cache, token never echoed into the detail string (_nm_rest_ok)."""
    return _nm_rest_ok()


def _probe_homelab():
    d = _watchers_repo_dir()                 # filesystem, instant (dir + .git + git bin)
    return (d is not None), (("repo: " + str(d)) if d else "no repo dir")


def _probe_voice():
    if _flowd_healthy():                     # 0.8s HTTP, 2s _flowd_cache
        return True, "engine: flowd"
    if _module_installed("pywhispercpp"):
        return True, "engine: pywhispercpp"
    return False, "browser fallback only"


def _probe_fleet():
    n = len(gpu_nodes() or {})               # CONFIGURED count, not reachability (§3.2)
    return (n > 0), ("%d node%s configured" % (n, "" if n == 1 else "s"))


_PROBES = {
    "nexusmind_api": _probe_nexusmind_api,
    "homelab": _probe_homelab,
    "voice": _probe_voice,
    "fleet": _probe_fleet,
}


def _probe_one(iid):
    """Run one probe, guarding against any exception → (False, detail)."""
    try:
        return _PROBES[iid]()
    except Exception as e:                    # noqa: BLE001 — a bad probe never breaks caps
        return False, "probe error: " + str(e)[:80]


def capabilities(refresh=False):
    """Assemble the capabilities payload (§3.4). Warm calls (<TTL, not refresh)
    return cached integration states instantly. A cold/refresh call runs every
    non-off probe in parallel daemon threads with a bounded join; any probe still
    running is reported detected:null, probing:true (frontend re-polls). Never
    blocks boot — the warm-up thread primes the caches once at startup."""
    now = time.time()
    modes = {iid: _int_mode(iid) for iid in INTEGRATION_IDS}
    with _caps_lock:
        warm = ((not refresh) and CAPS["integrations"]
                and now - CAPS["generated"] < CAPS_TTL)
        cached = dict(CAPS["integrations"])

    if warm:
        ints = {}
        for iid in INTEGRATION_IDS:
            mode = modes[iid]
            c = cached.get(iid, {})
            detected = None if mode == "off" else c.get("detected")
            ints[iid] = {"mode": mode, "detected": detected,
                         "active": _effective_active(mode, detected),
                         "detail": "disabled" if mode == "off" else c.get("detail", "")}
    else:
        results = {}
        threads = []
        for iid in INTEGRATION_IDS:
            if modes[iid] == "off":
                continue                      # off short-circuits BEFORE the probe (§3.2)
            t = threading.Thread(
                target=lambda i=iid: results.__setitem__(i, _probe_one(i)),
                daemon=True)
            threads.append(t)
            t.start()
        deadline = time.time() + CAPS_JOIN
        for t in threads:
            t.join(max(0.0, deadline - time.time()))
        ints, resolved = {}, {}
        for iid in INTEGRATION_IDS:
            mode = modes[iid]
            if mode == "off":
                ints[iid] = {"mode": mode, "detected": None, "active": False,
                             "detail": "disabled"}
                continue
            if iid in results:                # probe finished within the join budget
                detected, detail = results[iid]
                ints[iid] = {"mode": mode, "detected": detected,
                             "active": _effective_active(mode, detected), "detail": detail}
                resolved[iid] = {"detected": detected, "detail": detail}
            else:                             # still probing — report pending, don't cache
                prev = cached.get(iid, {})
                ints[iid] = {"mode": mode, "detected": None,
                             "active": _effective_active(mode, None),
                             "detail": prev.get("detail", "probing"), "probing": True}
        with _caps_lock:                      # merge resolved probes; keep prior on pending
            merged = dict(CAPS["integrations"])
            merged.update(resolved)
            CAPS["integrations"] = merged
            CAPS["generated"] = time.time()

    return {"first_run": not bool(_cfg_get(CONFIG, "first_run_done", False)),
            "generated": now_iso(),
            "integrations": ints,
            "modules": _cfg_get(CONFIG, "modules", {}) or {}}


# ---------------------------------------------------------------- statusline
# Claude-CLI global statusline configurator (mirrors TerminalX). The ~20-widget
# catalog is shipped inline; enabled widgets + options live in a config json that
# scripts/statusline.py reads at render time.

# Authoritative source for scripts/statusline.py — written verbatim on install.
# ESC via chr(27) and newline via chr(10) so the embedded source stays
# backslash-free (avoids escaping surprises when this string is materialised).
STATUSLINE_SCRIPT_SRC = '''#!/usr/bin/env python3
"""ZENITH/OS statusline for Claude Code. Reads the session JSON on stdin and
prints a compact multi-line ANSI status honoring data/statusline_config.json.
Never crashes: on any error it prints a minimal fallback."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"
CONFIG = DATA_DIR / "statusline_config.json"
ESC = chr(27)
NL = chr(10)

DEF_ENABLED = ["model", "effort", "contextBar", "contextPct", "dir", "branch",
               "git", "cost", "duration", "localTime", "battery"]
DEF_VISIBLE = True
DEF_OPTIONS = {"barWidth": 12, "theme": "dark", "monthlyPlanUsd": 200,
               "showRateLimits": True}

# widget id -> (line, order), mirrored from the server catalog
LAYOUT = {
    "model": (1, 0), "effort": (1, 1), "dir": (1, 2), "git": (1, 3),
    "branch": (1, 4), "worktree": (1, 5), "agent": (1, 6),
    "contextBar": (2, 0), "contextPct": (2, 1), "usage": (2, 2), "todo": (2, 3),
    "plan5h": (2, 4), "plan7d": (2, 5), "limits": (2, 6),
    "cost": (3, 0), "duration": (3, 1), "linesChanged": (3, 2), "streak": (3, 3),
    "localTime": (4, 0), "battery": (4, 1), "wifi": (4, 2), "mem": (4, 3),
    "disk": (4, 4), "docker": (4, 5),
}


def color(code, s):
    return ESC + "[" + code + "m" + str(s) + ESC + "[0m"


def rgb(r, g, b, s):
    """24-bit colour. Every terminal ZENITH targets (and xterm.js) speaks it, and
    a gauge wants a continuous ramp rather than the 8 ANSI buckets."""
    return ESC + "[38;2;%d;%d;%dm" % (r, g, b) + str(s) + ESC + "[0m"


def ramp(p):
    """green -> amber -> red across 0..1, interpolated so a bar that is filling
    up changes colour smoothly instead of snapping between three states."""
    p = max(0.0, min(1.0, p))
    if p < 0.5:                       # 78,240,166 -> 255,180,94
        t = p / 0.5
        return (int(78 + (255 - 78) * t), int(240 + (180 - 240) * t),
                int(166 + (94 - 166) * t))
    t = (p - 0.5) / 0.5               # 255,180,94 -> 255,93,108
    return (255, int(180 + (93 - 180) * t), int(94 + (108 - 94) * t))


# eighth-width block elements: a 12-cell bar therefore resolves 96 steps, not 12,
# so small changes actually move the needle instead of rounding away.
_EIGHTHS = " ▏▎▍▌▋▊▉"
_FULL, _TRACK = "█", "░"


def bar(pct, width, dim_track=True):
    """A filled gauge. pct is 0..1."""
    pct = max(0.0, min(1.0, float(pct)))
    width = max(3, int(width))
    exact = pct * width
    full = int(exact)
    rem = int(round((exact - full) * 8))
    if rem == 8:
        full, rem = full + 1, 0
    full = min(full, width)
    head = _EIGHTHS[rem] if (rem and full < width) else ""
    filled = _FULL * full + head
    track = _TRACK * max(0, width - len(filled))
    r, g, b = ramp(pct)
    out = rgb(r, g, b, filled)
    return out + (color("38;5;238", track) if dim_track else track)


def sh(argv, timeout=1.0):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def get_cwd(data):
    ws = data.get("workspace") or {}
    return ws.get("current_dir") or data.get("cwd") or os.getcwd()


def zenith_port():
    """The port ZENITH is actually listening on, resolved by the SAME three layers
    the server uses (env > data/config.json > built-in default). This script is a
    separate process launched by Claude Code, so it re-reads the config file rather
    than importing anything; before this it honored only ZENITH_PORT, so a server
    moved via config.json server.port went unnoticed and every widget went blank."""
    port = os.environ.get("ZENITH_PORT") or ""
    if not port:
        try:
            cfg = json.loads((DATA_DIR / "config.json").read_text())
            val = (cfg.get("server") or {}).get("port")
            port = str(val) if val else ""
        except Exception:
            port = ""
    return port or "8777"


ZENITH_URL = os.environ.get("ZENITH_URL", "http://127.0.0.1:" + zenith_port())


def zenith(path, timeout=0.8):
    """GET json from the local ZENITH server. Short timeout on purpose: this runs
    in Claude's own render path, so a slow or absent server must cost ~nothing.
    Returns None on any failure — every caller degrades instead of raising."""
    try:
        import urllib.request
        with urllib.request.urlopen(ZENITH_URL + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def token_info(data):
    """Context occupancy. ZENITH reads it from the transcript exactly — the same
    number /context reports — so ask it first. The stdin guesses below are a
    fallback for when ZENITH isn't running; Claude Code does not currently put
    context tokens on stdin, so without the server this stays blank rather than
    reporting something invented."""
    tp = data.get("transcript_path") or data.get("transcript")
    if tp:
        c = zenith("/api/context?path=" + quote(str(tp), safe=""))
        if c and c.get("available") and c.get("window"):
            return c.get("current"), c.get("window")
    ctx = data.get("context")
    used = total = None
    if isinstance(ctx, dict):
        used = ctx.get("used_tokens") or ctx.get("used") or ctx.get("tokens")
        total = ctx.get("total_tokens") or ctx.get("total") or ctx.get("max_tokens")
    if total is None:
        total = 1000000 if data.get("exceeds_200k_tokens") else 200000
    return used, total


def plan_limits():
    """Normalised rate-limit windows from ZENITH: [{label,percent,kind,...}].
    ZENITH holds the credential and does the fetching; this never sees one."""
    u = zenith("/api/usage")
    if not u or not u.get("ok"):
        return []
    return [x for x in (u.get("limits") or []) if isinstance(x, dict)]


def render(wid, data, opt):
    cwd = get_cwd(data)
    if wid == "model":
        m = data.get("model") or {}
        return color("36", m.get("display_name") or m.get("id") or "?")
    if wid == "effort":
        m = data.get("model") or {}
        tk = data.get("thinking") if isinstance(data.get("thinking"), dict) else {}
        eff = (data.get("effort") or data.get("reasoning_effort")
               or m.get("effort") or tk.get("effort") or tk.get("level"))
        if not eff:
            mt = os.environ.get("MAX_THINKING_TOKENS", "")
            if mt.isdigit():
                n = int(mt)
                eff = ("low" if n <= 2048 else "medium" if n <= 8192
                       else "high" if n <= 31999 else "max")
        return color("35", "effort:" + str(eff)) if eff else ""
    if wid == "dir":
        return color("34", os.path.basename(cwd.rstrip("/")) or cwd)
    if wid == "branch":
        b = sh(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"])
        return color("35", b) if b and b != "HEAD" else ""
    if wid == "git":
        if not sh(["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"]):
            return ""
        out = sh(["git", "-C", cwd, "status", "--porcelain"])
        return color("31", "*") if out else color("32", "ok")
    if wid == "worktree":
        gd = sh(["git", "-C", cwd, "rev-parse", "--git-dir"])
        gc = sh(["git", "-C", cwd, "rev-parse", "--git-common-dir"])
        if gd and gc and os.path.abspath(gd) != os.path.abspath(gc):
            return color("33", "wt:" + (os.path.basename(cwd.rstrip("/")) or "?"))
        return ""
    if wid == "agent":
        st = (data.get("output_style") or {}).get("name")
        return color("36", st) if st else ""
    if wid in ("plan5h", "plan7d", "limits"):
        lims = plan_limits()
        if not lims:
            return ""
        if wid != "limits":
            want = "session" if wid == "plan5h" else "weekly_all"
            lims = [x for x in lims if x.get("kind") == want]
        else:
            # every window the account actually has — a model-scoped cap (Fable,
            # Opus…) appears here on its own, no new widget needed
            lims = [x for x in lims if x.get("percent") is not None]
        w = max(4, int(opt.get("planBarWidth", 6) or 6))
        parts = []
        for x in lims:
            p = float(x.get("percent") or 0) / 100.0
            r, g, b = ramp(p)
            parts.append(color("38;5;245", x.get("label") or "?") + " "
                         + bar(p, w) + " " + rgb(r, g, b, "%d%%" % round(p * 100)))
        return "  ".join(parts)
    if wid in ("contextBar", "contextPct"):
        used, total = token_info(data)
        if not used or not total:
            return ""
        pct = max(0.0, min(1.0, float(used) / float(total)))
        if wid == "contextPct":
            r, g, b = ramp(pct)
            return rgb(r, g, b, str(int(pct * 100)) + "%")
        return bar(pct, int(opt.get("barWidth", 12) or 12))
    if wid == "cost":
        c = (data.get("cost") or {}).get("total_cost_usd")
        return color("32", "$" + format(float(c), ".2f")) if c is not None else ""
    if wid == "duration":
        ms = (data.get("cost") or {}).get("total_duration_ms")
        if not ms:
            return ""
        s = int(ms) // 1000
        return color("2", str(s // 60) + "m" + str(s % 60).zfill(2) + "s")
    if wid == "linesChanged":
        c = data.get("cost") or {}
        add, rem = c.get("total_lines_added"), c.get("total_lines_removed")
        if add is None and rem is None:
            return ""
        return color("32", "+" + str(add or 0)) + "/" + color("31", "-" + str(rem or 0))
    if wid == "streak":
        if not opt.get("showRateLimits"):
            return ""
        c = (data.get("cost") or {}).get("total_cost_usd")
        plan = opt.get("monthlyPlanUsd")
        if c is None or not plan:
            return ""
        return color("2", "$" + format(float(c), ".2f") + "/$" + str(int(plan)))
    if wid == "localTime":
        return color("2", time.strftime("%H:%M"))
    if wid == "battery":
        pct = None
        if sys.platform == "darwin":
            out = sh(["pmset", "-g", "batt"])
            for tok in out.replace(";", " ").split():
                if tok.endswith("%"):
                    pct = tok
                    break
        elif sys.platform.startswith("linux"):
            try:   # /sys/class/power_supply/BAT* — no battery (desktop) → widget hides
                for base in sorted(Path("/sys/class/power_supply").glob("BAT*")):
                    cap = base / "capacity"
                    if cap.exists():
                        pct = cap.read_text().strip() + "%"
                        break
            except Exception:
                pct = None
        return color("2", "bat " + pct) if pct else ""
    if wid == "wifi":
        ssid = ""
        if sys.platform == "darwin":
            out = sh(["/usr/sbin/networksetup", "-getairportnetwork", "en0"])
            if ":" in out and "not associated" not in out.lower():
                ssid = out.split(":", 1)[1].strip()
        elif sys.platform.startswith("linux"):
            if shutil.which("iwgetid"):
                ssid = sh(["iwgetid", "-r"]).strip()
            if not ssid and shutil.which("nmcli"):
                for line in sh(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]).splitlines():
                    if line.startswith("yes:"):
                        ssid = line.split(":", 1)[1].strip()
                        break
        return color("2", ssid) if ssid else ""
    if wid == "disk":
        try:
            u = shutil.disk_usage("/")
            return color("2", "disk " + str(u.free // (1024 ** 3)) + "G")
        except Exception:
            return ""
    if wid == "docker":
        if not shutil.which("docker"):
            return ""
        out = sh(["docker", "ps", "-q"], timeout=1.5)
        n = len([x for x in out.splitlines() if x.strip()])
        return color("36", "dkr " + str(n)) if n else ""
    # usage / todo / mem: not computable from stdin -> skipped
    return ""


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    # capture this session's live claude id into the reboot-recovery manifest, keyed by
    # the zenith term id (from the env, or derived from the zenith-<id> tmux session name
    # so pre-existing sessions are retrofitted too). Best-effort, write-once per session.
    try:
        # tmux is the AUTHORITY here; the env var is only the non-tmux fallback. This
        # order used to be reversed, and the env var is precisely the thing that cannot
        # be trusted: a pane inherits ZENITH_TERM_ID from the tmux SERVER, so whichever
        # spawn started that server stamps its own id onto every window opened
        # afterwards. Measured: all 20+ live terminals reported ONE id, so every window
        # wrote its session into one other window's file and every consumer downstream
        # was left guessing. TMUX_PANE is genuinely per-pane and sessions are named
        # zenith-<term id>, so asking tmux about THIS pane is exact.
        #
        # -t $TMUX_PANE matters: a bare display-message answers for the server's notion
        # of the current client, which need not be the pane being rendered.
        #
        # And because the statusline is re-executed on every render, this RETROFITS
        # windows whose claude process still carries the poisoned value: they correct
        # themselves on their next render, with no restart.
        tid = None
        if os.environ.get("TMUX"):
            pane = os.environ.get("TMUX_PANE")
            r = subprocess.run(
                ["tmux", "display-message", "-p"]
                + (["-t", pane] if pane else []) + ["#{session_name}"],
                capture_output=True, text=True, timeout=1)
            nm = r.stdout.strip()
            if nm.startswith("zenith-"):
                tid = nm[len("zenith-"):]
        if not tid:
            tid = os.environ.get("ZENITH_TERM_ID")
        sid = data.get("session_id")
        if tid and sid:
            p = DATA_DIR / "live" / (tid + ".json")
            rec = {}
            try:
                rec = json.loads(p.read_text())
            except Exception:
                rec = {}
            if rec.get("session_id") != sid:
                rec["id"] = tid
                rec["session_id"] = sid
                cwd = data.get("cwd") or (data.get("workspace") or {}).get("current_dir")
                if cwd:
                    rec["cwd"] = cwd
                rec.setdefault("mode", "claude")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(rec))
    except Exception:
        pass
    try:
        cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    except Exception:
        cfg = {}
    # master switch: hidden means render nothing at all, while staying installed
    # in settings.json (so it comes back without a reinstall)
    if cfg.get("visible", DEF_VISIBLE) is False:
        return
    # an explicitly empty list means the user turned every data point off, which
    # must render nothing. Only a MISSING key falls back to the defaults.
    enabled = cfg["enabled"] if isinstance(cfg.get("enabled"), list) else DEF_ENABLED
    opt = dict(DEF_OPTIONS)
    opt.update(cfg.get("options") or {})
    lines = {}
    for wid in enabled:
        ln, order = LAYOUT.get(wid, (1, 99))
        try:
            piece = render(wid, data, opt)
        except Exception:
            piece = ""
        if piece:
            lines.setdefault(ln, []).append((order, piece))
    out = []
    for ln in sorted(lines):
        parts = [p for _, p in sorted(lines[ln], key=lambda x: x[0])]
        out.append("  ".join(parts))
    sys.stdout.write(NL.join(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            sys.stdout.write("zenith")
        except Exception:
            pass
'''

STATUSLINE_CATALOG = [
    {"id": "model", "label": "Model", "group": "session", "line": 1, "order": 0,
     "description": "Active model display name"},
    {"id": "effort", "label": "Effort", "group": "session", "line": 1, "order": 1,
     "description": "Reasoning effort / thinking level (when the session exposes it)"},
    {"id": "dir", "label": "Directory", "group": "session", "line": 1, "order": 2,
     "description": "Current working directory (basename)"},
    {"id": "git", "label": "Git status", "group": "git", "line": 1, "order": 2,
     "description": "Working-tree dirty/clean indicator"},
    {"id": "branch", "label": "Git branch", "group": "git", "line": 1, "order": 3,
     "description": "Current git branch"},
    {"id": "worktree", "label": "Worktree", "group": "git", "line": 1, "order": 4,
     "description": "Git worktree name when not the main tree"},
    {"id": "agent", "label": "Output style", "group": "session", "line": 1, "order": 5,
     "description": "Active output style / agent"},
    {"id": "contextBar", "label": "Context bar", "group": "context", "line": 2, "order": 0,
     "description": "Context-window usage as a bar"},
    {"id": "contextPct", "label": "Context %", "group": "context", "line": 2, "order": 1,
     "description": "Context-window used percentage"},
    {"id": "usage", "label": "Token usage", "group": "context", "line": 2, "order": 2,
     "description": "Session input/output token counts"},
    {"id": "todo", "label": "Todos", "group": "context", "line": 2, "order": 3,
     "description": "Open todo count"},
    {"id": "plan5h", "label": "Plan 5h", "group": "context", "line": 2, "order": 4,
     "description": "5-hour rate-limit window used (via ZENITH, no keychain needed)"},
    {"id": "plan7d", "label": "Plan 7d", "group": "context", "line": 2, "order": 5,
     "description": "7-day rate-limit window used (via ZENITH, no keychain needed)"},
    {"id": "limits", "label": "All plan limits", "group": "context", "line": 2, "order": 6,
     "description": "Every active rate-limit window — 5h, 7d, and any model-scoped "
                    "cap (Fable, Opus) — each with a bar"},
    {"id": "cost", "label": "Cost", "group": "cost", "line": 3, "order": 0,
     "description": "Session cost in USD"},
    {"id": "duration", "label": "Duration", "group": "cost", "line": 3, "order": 1,
     "description": "Session wall-clock duration"},
    {"id": "linesChanged", "label": "Lines changed", "group": "cost", "line": 3, "order": 2,
     "description": "Lines added / removed this session"},
    {"id": "streak", "label": "Rate limit", "group": "cost", "line": 3, "order": 3,
     "description": "Spend vs. monthly plan budget"},
    {"id": "localTime", "label": "Local time", "group": "system", "line": 4, "order": 0,
     "description": "Current local time"},
    {"id": "battery", "label": "Battery", "group": "system", "line": 4, "order": 1,
     "description": "Battery percentage"},
    {"id": "wifi", "label": "Wi-Fi", "group": "system", "line": 4, "order": 2,
     "description": "Current Wi-Fi SSID"},
    {"id": "mem", "label": "Memory", "group": "system", "line": 4, "order": 3,
     "description": "Free system memory percentage"},
    {"id": "disk", "label": "Disk", "group": "system", "line": 4, "order": 4,
     "description": "Free disk on /"},
    {"id": "docker", "label": "Docker", "group": "system", "line": 4, "order": 5,
     "description": "Running docker containers"},
]

STATUSLINE_DEFAULT = {
    # `visible` is the master switch, separate from install/uninstall: hiding
    # leaves ZENITH registered in ~/.claude/settings.json and just renders
    # nothing, so it comes back instantly without rewriting the user's settings.
    "visible": True,
    # `limits` is on by default: it is the one number a user cannot get anywhere
    # else in the terminal, and it renders empty (not broken) when ZENITH has no
    # usable credential — so a fresh install shows it the moment it can.
    "enabled": ["model", "dir", "branch", "git", "contextBar", "contextPct",
                "limits", "cost", "duration", "localTime", "battery"],
    "options": {"barWidth": 12, "planBarWidth": 6, "theme": "dark",
                "monthlyPlanUsd": 200, "showRateLimits": True},
}


def statusline_config_load():
    if not STATUSLINE_CONFIG.exists():
        cfg = json.loads(json.dumps(STATUSLINE_DEFAULT))
        try:
            _save_json(STATUSLINE_CONFIG, cfg)
        except OSError:
            pass
        return cfg
    cfg = _load_json(STATUSLINE_CONFIG, json.loads(json.dumps(STATUSLINE_DEFAULT)))
    cfg.setdefault("enabled", STATUSLINE_DEFAULT["enabled"])
    cfg.setdefault("options", dict(STATUSLINE_DEFAULT["options"]))
    cfg["visible"] = bool(cfg.get("visible", True))   # pre-existing configs → shown
    return cfg


def _settings_statusline():
    try:
        data = json.loads(SETTINGS_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    sl = data.get("statusLine")
    return sl if isinstance(sl, dict) else None


def statusline_get():
    cfg = statusline_config_load()
    sl = _settings_statusline()
    registered, cmd = "none", None
    if sl:
        cmd = sl.get("command")
        registered = "zenith" if (cmd and "zenith" in cmd) else "other"
    return {"catalog": STATUSLINE_CATALOG, "enabled": cfg.get("enabled", []),
            "options": cfg.get("options", {}), "visible": cfg.get("visible", True),
            "registered": registered, "registeredCommand": cmd,
            # when another tool owns the statusLine, drive that one instead
            "external": lk_get() if registered == "other" else {"available": False}}


def statusline_set(enabled, options, visible=None):
    cfg = statusline_config_load()
    if isinstance(enabled, list):
        valid = {w["id"] for w in STATUSLINE_CATALOG}
        cfg["enabled"] = [e for e in enabled if e in valid]
    if isinstance(options, dict):
        cfg["options"] = {**cfg.get("options", {}), **options}
    if visible is not None:
        cfg["visible"] = bool(visible)
    with _data_lock:
        _save_json(STATUSLINE_CONFIG, cfg)
    return {"ok": True, "enabled": cfg["enabled"], "options": cfg["options"],
            "visible": cfg["visible"]}


def statusline_install():
    """Register ZENITH's statusline in ~/.claude/settings.json (strict parse,
    one-time backup, stash any prior non-zenith statusLine). Returns a dict with
    a 'code' key on error so the handler can map it to an HTTP status."""
    raw = ""
    try:
        raw = SETTINGS_JSON.read_text()
    except OSError:
        raw = ""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        return {"code": 409, "error": f"settings.json is not valid JSON: {e}"}
    if not isinstance(data, dict):
        return {"code": 409, "error": "settings.json is not a JSON object"}
    # one-time backup
    bak = SETTINGS_JSON.with_name("settings.json.zenith.bak")
    if SETTINGS_JSON.exists() and not bak.exists():
        try:
            shutil.copyfile(SETTINGS_JSON, bak)
        except OSError:
            pass
    # stash a prior non-zenith statusline once, so uninstall can restore it
    prior = data.get("statusLine")
    if isinstance(prior, dict) and "zenith" not in (prior.get("command") or ""):
        cfg = statusline_config_load()
        if "prior_statusline" not in cfg:
            cfg["prior_statusline"] = prior
            with _data_lock:
                _save_json(STATUSLINE_CONFIG, cfg)
    _ensure_statusline_script()
    # use this interpreter's absolute path so the statusline runs regardless of
    # whether `python3` is on PATH (it usually isn't on native Windows)
    py = sys.executable or ("python" if IS_WINDOWS else "python3")
    command = f'"{py}" "{STATUSLINE_SCRIPT}"'
    data["statusLine"] = {"type": "command", "command": command, "padding": 0}
    _save_json(SETTINGS_JSON, data)
    return {"ok": True, "registered": "zenith", "command": command}


def statusline_uninstall():
    try:
        data = json.loads(SETTINGS_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return {"ok": True, "registered": "none"}
    if not isinstance(data, dict):
        return {"ok": True, "registered": "none"}
    cfg = statusline_config_load()
    prior = cfg.pop("prior_statusline", None)
    if prior:
        data["statusLine"] = prior
        with _data_lock:
            _save_json(STATUSLINE_CONFIG, cfg)
    else:
        data.pop("statusLine", None)
    _save_json(SETTINGS_JSON, data)
    return {"ok": True, "registered": "other" if prior else "none"}


# ------------------------------------------------ external statusline (lk)
# Another tool can own settings.json's statusLine. lk-statusline is the one we
# know how to drive: its config is four ordered widget arrays and it prints a
# line only when that line is non-empty (`if (line1) console.log(line1)`), so
# hiding is exactly "empty all four" — no blank lines left behind.
LK_DIR = HOME / ".config" / "lk-statusline"
LK_CONFIG = LK_DIR / "config.json"
LK_SCRIPT = LK_DIR / "statusline.ts"
LK_LINES = ("line1", "line2", "line3", "line4")
LK_LINE_LABELS = {"line1": "line 1", "line2": "line 2",
                  "line3": "line 3", "line4": "line 4"}


def _lk_parse(pattern, src, flags=0):
    m = re.search(pattern, src, flags)
    return m.group(1) if m else ""


def lk_introspect():
    """Read the installed script for its widget union and default layout, so the
    toggle list tracks the user's actual lk-statusline instead of a copy here
    that goes stale when they update it. -> (catalog, home_map)"""
    try:
        return _lk_parse_script(LK_SCRIPT.read_text(encoding="utf-8"))
    except OSError:
        return [], {}


def _lk_parse_script(src):
    union = _lk_parse(r"type WidgetName\s*=(.*?);", src, re.S)
    catalog = re.findall(r'"([A-Za-z0-9_]+)"', union)
    defaults = _lk_parse(r"const DEFAULT_CONFIG[^=]*=\s*\{(.*?)\n\};", src, re.S)
    home = {}
    for ln in LK_LINES:
        arr = _lk_parse(ln + r"\s*:\s*\[(.*?)\]", defaults, re.S)
        for w in re.findall(r'"([A-Za-z0-9_]+)"', arr):
            home.setdefault(w, ln)
    return catalog, home


def _lk_read():
    try:
        d = json.loads(LK_CONFIG.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _lk_write(d):
    """One-time backup before ZENITH ever writes another tool's config."""
    LK_DIR.mkdir(parents=True, exist_ok=True)
    bak = LK_CONFIG.with_name("config.json.zenith.bak")
    if LK_CONFIG.exists() and not bak.exists():
        try:
            shutil.copyfile(LK_CONFIG, bak)
        except OSError:
            pass
    _save_json(LK_CONFIG, d)


def _lk_state():
    """ZENITH-side memory: the stashed layout while hidden, and each widget's
    home line so toggling one off and back on puts it where it was."""
    cfg = statusline_config_load()
    ext = cfg.get("external") if isinstance(cfg.get("external"), dict) else {}
    ext.setdefault("hidden", False)
    ext.setdefault("stash", {})
    ext.setdefault("home", {})
    return cfg, ext


def lk_get():
    """Every catalog widget, grouped by the line it lives on (current config →
    script default → line 4), with the ones currently rendering marked on."""
    if not LK_CONFIG.exists() and not LK_SCRIPT.exists():
        return {"available": False}
    catalog, home = lk_introspect()
    cfg, ext = _lk_state()
    disk = _lk_read()
    live = {ln: [w for w in (disk.get(ln) or []) if isinstance(w, str)]
            for ln in LK_LINES}
    layout = {ln: list(ext["stash"].get(ln) or []) for ln in LK_LINES} \
        if ext["hidden"] else live
    if not catalog:                       # script unreadable → at least show what is configured
        catalog = [w for ln in LK_LINES for w in layout[ln]]
    on = [w for ln in LK_LINES for w in layout[ln]]
    where = dict(home)
    where.update(ext["home"])
    for ln in LK_LINES:
        for w in layout[ln]:
            where[w] = ln
    groups = {}
    for ln in LK_LINES:                   # enabled first, in config order
        groups[ln] = [w for w in layout[ln] if w in catalog]
    for w in catalog:
        if w not in on:
            groups.setdefault(where.get(w, "line4"), []).append(w)
    return {"available": True, "name": "lk-statusline",
            "catalog": catalog, "groups": groups, "enabled": on,
            "lineLabels": LK_LINE_LABELS, "visible": not ext["hidden"],
            "configPath": str(LK_CONFIG)}


def lk_set(lines=None, visible=None):
    """Write a layout and/or flip the master switch. Hiding stashes the layout
    ZENITH-side and empties the four arrays; showing puts them back."""
    if not LK_CONFIG.exists() and not LK_SCRIPT.exists():
        return {"available": False, "error": "lk-statusline not found"}
    catalog = set(lk_introspect()[0])
    cfg, ext = _lk_state()
    disk = _lk_read()
    if isinstance(lines, dict):
        target, seen = {}, set()
        for ln in LK_LINES:
            arr = []
            for w in (lines.get(ln) or []):
                if isinstance(w, str) and w not in seen and (not catalog or w in catalog):
                    arr.append(w)
                    seen.add(w)
                    ext["home"][w] = ln
            target[ln] = arr
    elif ext["hidden"]:
        target = {ln: list(ext["stash"].get(ln) or []) for ln in LK_LINES}
    else:
        target = {ln: [w for w in (disk.get(ln) or []) if isinstance(w, str)]
                  for ln in LK_LINES}
    if visible is not None:
        ext["hidden"] = not bool(visible)
    if ext["hidden"]:
        ext["stash"] = target
        for ln in LK_LINES:
            disk[ln] = []
    else:
        ext["stash"] = {}
        for ln in LK_LINES:
            disk[ln] = target.get(ln, [])
    _lk_write(disk)
    cfg["external"] = ext
    with _data_lock:
        _save_json(STATUSLINE_CONFIG, cfg)
    return lk_get()


def _ensure_statusline_script():
    """Write scripts/statusline.py from the embedded source if missing/stale."""
    try:
        STATUSLINE_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
        cur = STATUSLINE_SCRIPT.read_text(encoding="utf-8") if STATUSLINE_SCRIPT.exists() else ""
        if cur != STATUSLINE_SCRIPT_SRC:
            STATUSLINE_SCRIPT.write_text(STATUSLINE_SCRIPT_SRC, encoding="utf-8")
            STATUSLINE_SCRIPT.chmod(0o755)
    except OSError:
        pass


# ---------------------------------------------------------------- HTTP

MIME = {".html": "text/html", ".htm": "text/html", ".js": "application/javascript",
        ".mjs": "application/javascript", ".css": "text/css",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".webp": "image/webp", ".avif": "image/avif", ".woff2": "font/woff2",
        ".woff": "font/woff", ".ttf": "font/ttf", ".json": "application/json",
        ".txt": "text/plain", ".md": "text/plain", ".pdf": "application/pdf"}


# ------------------------------------------------- custom FX media (GIFs, images)
# Background effect layers the user supplies. These live on DISK, not in the settings
# blob: a single GIF routinely runs into megabytes and localStorage caps out around 5MB
# for EVERYTHING, so storing one there would take the whole settings object down with
# it. The browser keeps only the id + per-layer config and points an <img> at the URL.

FX_MEDIA_DIR = DATA_DIR / "fxmedia"
FX_MEDIA_EXT = {"image/gif": "gif", "image/png": "png", "image/jpeg": "jpg",
                "image/webp": "webp", "image/apng": "png", "image/svg+xml": "svg"}
FX_MEDIA_MAX = 12_000_000          # 12MB: comfortably a long GIF, not a video dump


def fx_media_list():
    if not FX_MEDIA_DIR.is_dir():
        return []
    out = []
    for p in sorted(FX_MEDIA_DIR.iterdir()):
        if p.is_file() and p.suffix.lower().lstrip(".") in set(FX_MEDIA_EXT.values()):
            try:
                out.append({"name": p.name, "url": "/fxmedia/" + p.name, "bytes": p.stat().st_size})
            except OSError:
                pass
    return out


def fx_media_save(ctype, data, orig_name=""):
    ext = FX_MEDIA_EXT.get((ctype or "").split(";")[0].strip().lower())
    if not ext:
        return {"error": "unsupported type: " + str(ctype)[:60]}
    if not data:
        return {"error": "empty upload"}
    FX_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    # the display label is user text; keep it OUT of the filename and store the
    # sanitised stem only, so nothing user-controlled reaches the filesystem
    stem = re.sub(r"[^a-zA-Z0-9_-]", "", (orig_name or "").rsplit(".", 1)[0])[:24] or "media"
    name = f"{stem}-{uuid.uuid4().hex[:6]}.{ext}"
    try:
        (FX_MEDIA_DIR / name).write_bytes(data)
    except OSError as e:
        return {"error": str(e)[:120]}
    return {"ok": True, "name": name, "url": "/fxmedia/" + name, "bytes": len(data)}


def fx_media_delete(name):
    """Delete by BASENAME only — never join a client string onto a path."""
    base = os.path.basename(str(name or ""))
    p = FX_MEDIA_DIR / base
    try:
        if base and p.is_file() and p.parent.resolve() == FX_MEDIA_DIR.resolve():
            p.unlink()
            return {"ok": True}
    except OSError as e:
        return {"ok": False, "error": str(e)[:120]}
    return {"ok": False, "error": "no such media"}


# ---------------------------------------------------------------- gate engine (E)
# op × blast × rev → level; GATE_RULES is AUTHORITATIVE per known action, the
# formula is its derivation + the fallback for unknown actions (§7.1–7.2).
# Three routine actions are pinned auto AGAINST the formula (jobs.spawn.default,
# job.stop, config.write): their ordinary job.*/config.change events already
# record them; notify is reserved for spend, recurring writers, elevated modes.

Gate = collections.namedtuple("Gate", "action op blast rev level")

FORCE_CONFIRM = {"jobs.spawn.bypass"}

GATE_RULES = {   # action -> (op, blast, rev, level)
    "jobs.spawn.default":     ("execute", "project", "reversible",   "auto"),
    "jobs.spawn.acceptEdits": ("execute", "project", "soft",         "notify"),
    "jobs.spawn.bypass":      ("execute", "system",  "irreversible", "confirm"),
    "loops.save.acceptEdits": ("write",   "project", "soft",         "notify"),
    "swarm.launch":           ("execute", "project", "soft",         "notify"),
    "wargame.launch":         ("execute", "project", "soft",         "notify"),
    "verify.run":             ("execute", "project", "reversible",   "notify"),
    "job.stop":               ("execute", "local",   "reversible",   "auto"),
    "config.write":           ("write",   "project", "soft",         "auto"),
    "scorecard.set":          ("write",   "system",  "soft",         "notify"),
    "autoverify.set":         ("write",   "system",  "soft",         "notify"),
    # watchers (NM price/stock watchers): pause/resume is routine + pinned auto
    # (like config.write); save is a repo write → notify; delete removes the
    # yaml + DB rows → irreversible → confirm (formula-consistent).
    "watchers.enable":        ("write",   "project", "reversible",   "auto"),
    "watchers.save":          ("write",   "project", "soft",         "notify"),
    "watchers.delete":        ("write",   "project", "irreversible", "confirm"),
    # fetch-now is a read-only dry-run (nothing persisted) — pinned auto like enable
    "watchers.fetchnow":      ("read",    "project", "reversible",   "auto"),
}

_ROUTE_ACTIONS = {"/api/swarms/launch": "swarm.launch",
                  "/api/wargames/launch": "wargame.launch",
                  "/api/verify": "verify.run",
                  "/api/job/stop": "job.stop",
                  "/api/scorecard": "scorecard.set",
                  "/api/autoverify": "autoverify.set",
                  "/api/watchers/enable": "watchers.enable",
                  "/api/watchers/save": "watchers.save",
                  "/api/watchers/delete": "watchers.delete",
                  "/api/watchers/fetchnow": "watchers.fetchnow"}


def _gate_level(op, blast, rev, action):
    """§7.1 collapse rules, checked in order."""
    if (rev == "irreversible" or (op == "execute" and blast == "system")
            or action in FORCE_CONFIRM):
        return "confirm"
    if op in ("write", "execute") or blast == "system":
        return "notify"
    return "auto"


def _classify(key, body=None):
    """Pure classification (probe-able, H1): route-or-action-key + salient
    body fields -> Gate. Unknown actions fall back to the formula over a
    conservative (write, project, soft)."""
    body = body or {}
    if key in ("/api/jobs", "jobs.spawn", "/api/ab/launch"):
        key = {"bypassPermissions": "jobs.spawn.bypass",
               "acceptEdits": "jobs.spawn.acceptEdits"}.get(
                   body.get("mode", "default"), "jobs.spawn.default")
    elif key == "/api/loops2/save":
        key = ("loops.save.acceptEdits" if body.get("mode") == "acceptEdits"
               else "config.write")
    else:
        key = _ROUTE_ACTIONS.get(key, key)
    rule = GATE_RULES.get(key)
    if rule:
        op, blast, rev, level = rule
    else:
        op, blast, rev = "write", "project", "soft"
        level = _gate_level(op, blast, rev, key)
    return Gate(key, op, blast, rev, level)


_GATES_PENDING = {}          # token -> {body_hash, action, route, op, blast,
_gates_lock = threading.Lock()   #        rev, project, token_hash, expires}
GATE_TTL = 120               # seconds a challenge token stays valid


def _gate_body_hash(body):
    """sha256 of the canonical body minus gate_token — binds a token to the
    exact action so it cannot be minted for X and spent on Y (§7.3)."""
    slim = {k: v for k, v in (body or {}).items() if k != "gate_token"}
    return hashlib.sha256(json.dumps(slim, sort_keys=True,
                                     separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def _gate_summary(g, body):
    if g.action.startswith("jobs.spawn"):
        if isinstance(body.get("arms"), list):
            return (f"launch A/B — mode={body.get('mode', 'default')}, "
                    f"{len(body['arms'])} arms")
        return (f"launch job — mode={body.get('mode', 'default')}, "
                f"model={body.get('model', 'sonnet')}")
    if g.action.startswith("watchers."):
        nm = body.get("name") or (body.get("spec") or {}).get("name") or "?"
        return f"{g.action.split('.', 1)[1]} watcher — {nm}"
    return g.action


def _gate_detail(g, body):
    keep = ("mode", "model", "label", "project", "kind", "id", "n", "enabled")
    return {k: body.get(k) for k in keep if k in body}


def _gate_log(g, route, project, decision, detail=None, token_hash=""):
    """gates row + gate.decision event, committed synchronously — log-then-act
    (§2.1 makes durability free). Returns the gate row id."""
    gid = zs.gate_insert(action=g.action, route=route, op=g.op, blast=g.blast,
                         rev=g.rev, level=g.level, decision=decision,
                         project=project, detail=detail or {},
                         token_hash=token_hash)
    eid = zs.emit("gate.decision", project=project, ref=str(gid),
                  outcome=decision, actor="user",
                  data={"action": g.action, "level": g.level, "route": route,
                        "op": g.op, "blast": g.blast, "rev": g.rev})
    if eid and gid:
        zs.gate_set_event(gid, eid)
    return gid


def _gate_check(handler, route, body):
    """§7.3 — the guarded route IS the check. True → proceed (auto/notify/
    valid-token confirm, all logged as required); False → a 428 challenge was
    already written to the response, caller just returns."""
    g = _classify(route, body)
    proj = str(body.get("project") or "")
    if g.level == "auto":
        return True                       # ordinary events only (§7.1)
    if g.level == "notify":
        _gate_log(g, route, proj, "auto", _gate_detail(g, body))
        return True
    # level == confirm
    now = time.time()
    tok = str(body.get("gate_token") or "")
    expired, rec = [], None
    with _gates_lock:
        for t in [t for t, r in _GATES_PENDING.items() if r["expires"] < now]:
            expired.append(_GATES_PENDING.pop(t))     # lazy reap
        if tok:
            rec = _GATES_PENDING.pop(tok, None)
    for x in expired:
        _gate_log(Gate(x["action"], x["op"], x["blast"], x["rev"], "confirm"),
                  x["route"], x["project"], "expired",
                  token_hash=x["token_hash"])
    if rec is not None and rec["expires"] >= now \
            and rec["body_hash"] == _gate_body_hash(body):
        _gate_log(g, route, proj, "confirmed", _gate_detail(g, body),
                  token_hash=rec["token_hash"])       # logged BEFORE the action
        return True
    # no token / wrong body / expired → fresh single-use challenge
    token = uuid.uuid4().hex
    th = hashlib.sha256(token.encode()).hexdigest()[:16]   # never the token
    with _gates_lock:
        _GATES_PENDING[token] = {"body_hash": _gate_body_hash(body),
                                 "action": g.action, "route": route,
                                 "op": g.op, "blast": g.blast, "rev": g.rev,
                                 "project": proj, "token_hash": th,
                                 "expires": now + GATE_TTL}
    _gate_log(g, route, proj, "prompted", _gate_detail(g, body), token_hash=th)
    handler._json({"error": "confirm_required",
                   "gate": {"token": token, "action": g.action,
                            "level": g.level, "op": g.op, "blast": g.blast,
                            "rev": g.rev, "summary": _gate_summary(g, body),
                            "detail": _gate_detail(g, body)}}, 428)
    return False


class Handler(BaseHTTPRequestHandler):
    server_version = "ZenithOS/1.0"
    protocol_version = "HTTP/1.1"   # required for WebSocket upgrade

    def log_message(self, fmt, *args):
        pass

    def _session_ws(self, q):
        """Live-tail a session transcript: push each new jsonl line as a lightweight event.
        Reuses the RFC6455 helpers; a poller thread reads appended lines while the main
        loop drains client frames (ping/close)."""
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._err("bad ws", 400)
        try:
            p = Path(q.get("path", "")).resolve()
        except (OSError, ValueError):
            return self._err("bad path", 400)
        roots = [TRANSCRIPTS_ROOT.resolve(), (Path.home() / ".codex").resolve()]
        if not any(p == r or r in p.parents for r in roots) or not p.is_file():
            return self._err("not a session transcript", 403)
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        conn = {"sock": self.connection, "lock": threading.Lock()}
        stop = threading.Event()

        def poller():
            try:
                pos = p.stat().st_size          # start at EOF → stream only new activity
            except OSError:
                pos = 0
            while not stop.is_set():
                try:
                    sz = p.stat().st_size
                    if sz < pos:                # truncated / rotated
                        pos = 0
                    if sz > pos:
                        with open(p, "rb") as f:
                            f.seek(pos)
                            chunk = f.read(sz - pos)
                            pos = f.tell()
                        for ln in chunk.split(b"\n"):
                            ln = ln.strip()
                            if not ln:
                                continue
                            ev = _session_tail_event(ln)
                            if ev:
                                try:
                                    ws_send(conn, json.dumps(ev), 1)
                                except OSError:
                                    stop.set()
                                    break
                except OSError:
                    pass
                stop.wait(1.0)

        threading.Thread(target=poller, daemon=True).start()
        try:
            while True:
                opcode, data = ws_recv(self.rfile)
                if opcode is None or opcode == 8:
                    break
                if opcode == 9:
                    ws_send(conn, data, 10)
        except OSError:
            pass
        finally:
            stop.set()

    def _term_ws(self, q):
        term_id = q.get("id", "")
        term = TERMS.get(term_id)
        if term is None or term.get("status") == "dead":
            # in-memory record gone/dead — a persistent tmux session may still be alive
            term = reattach_term(term_id)
        key = self.headers.get("Sec-WebSocket-Key")
        if term is None or not key:
            return self._err("no such terminal", 404)
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        conn = {"sock": self.connection, "lock": threading.Lock()}
        with term["lock"]:
            snapshot = bytes(term["buf"])
            term["conns"].append(conn)
        try:
            if snapshot:
                # mark the historical replay so the client won't echo query-replies (DA/DSR) it triggers
                ws_send(conn, json.dumps({"t": "replay"}), 1)
                ws_send(conn, snapshot, 2)
            if term["status"] == "dead":
                ws_send(conn, json.dumps({"t": "exit"}), 1)
            while True:
                opcode, data = ws_recv(self.rfile)
                if opcode is None or opcode == 8:
                    break
                if opcode == 9:
                    ws_send(conn, data, 10)
                    continue
                if opcode not in (1, 2):
                    continue
                try:
                    msg = json.loads(data)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                try:
                    if msg.get("t") == "in":
                        term["pty"].write(msg.get("d", "").encode())
                    elif msg.get("t") == "resize":
                        term["pty"].set_winsize(int(msg["rows"]), int(msg["cols"]))
                except (OSError, KeyError, ValueError):
                    pass
        except OSError:
            pass
        finally:
            with term["lock"]:
                if conn in term["conns"]:
                    term["conns"].remove(conn)

    def _builder_ws(self, q):
        """The Builder panel's socket. Same hand-rolled RFC6455 upgrade as the terminal
        — but the payload is JSON frames both ways rather than a byte stream, because
        what travels here are events and commands, not screen contents."""
        b = BUILDERS.get(q.get("id", ""))
        key = self.headers.get("Sec-WebSocket-Key")
        if b is None or not key:
            return self._err("no such builder", 404)
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        conn = {"sock": self.connection, "lock": threading.Lock()}
        with b["lock"]:
            replay = [dict(f) for f in b["buf"]]
            b["conns"].append(conn)
        try:
            # replay first, then the live tap — a reload rebuilds the whole scrollback
            # from the server instead of the panel starting blank mid-conversation.
            ws_send(conn, json.dumps({"t": "replay", "frames": replay,
                                      "builder": builder_public(b)}), 1)
            if b["status"] != "live":
                ws_send(conn, json.dumps({"t": "exit"}), 1)
            else:
                builder_send(b, {"type": "get_state"})   # resync isStreaming on attach
            while True:
                opcode, data = ws_recv(self.rfile)
                if opcode is None or opcode == 8:
                    break
                if opcode == 9:
                    ws_send(conn, data, 10)
                    continue
                if opcode not in (1, 2):
                    continue
                try:
                    msg = json.loads(data)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(msg, dict):
                    builder_command(b, msg)
        except OSError:
            pass
        finally:
            with b["lock"]:
                if conn in b["conns"]:
                    b["conns"].remove(conn)

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=400):
        self._json({"error": str(msg)}, code)

    def _whisper_post(self, length):
        """POST /api/whisper — hand-parsed multipart, graceful 200 on any error."""
        if length > 20_000_000:                 # ~20MB cap
            self.close_connection = True
            return self._json({"error": "upload too large", "text": ""})
        raw = self.rfile.read(length) if length > 0 else b""
        ctype = self.headers.get("Content-Type", "") or ""
        try:
            filename, audio = _parse_multipart_audio(ctype, raw)
        except ValueError as e:
            return self._json({"error": str(e), "text": ""})
        return self._json(whisper_transcribe(audio, filename))

    def _fx_media_post(self, length):
        """POST /api/fx/media — raw image/gif bytes, saved to data/fxmedia and served
        back as a URL. Deliberately not base64-in-JSON: a GIF is large enough that the
        33% base64 overhead plus a full JSON parse is worth avoiding, and the browser
        only ever needs the URL back."""
        if length <= 0:
            return self._err("empty upload", 400)
        if length > FX_MEDIA_MAX:
            self.close_connection = True
            return self._err("too large (%dMB max)" % (FX_MEDIA_MAX // 1_000_000), 413)
        res = fx_media_save(self.headers.get("Content-Type", ""), self.rfile.read(length),
                            self.headers.get("X-Media-Name", ""))
        return self._err(res["error"], 415) if res.get("error") else self._json(res)

    def _paste_image_post(self, length):
        """POST /api/term/paste-image — raw image bytes (Content-Type = image/*). Saves the image to a
        temp file and returns its absolute path, which the terminal then types in so Claude Code (running
        on this same machine) can load it. Bridges browser ⌘V-image → the CLI, which can't receive image
        bytes through the text-only PTY. Bytes never leave this Mac."""
        if length <= 0:
            return self._err("empty upload", 400)
        if length > 25_000_000:
            self.close_connection = True
            return self._err("image too large (25MB max)", 413)
        data = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type", "") or "image/png").split(";")[0].strip().lower()
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/gif": "gif",
               "image/webp": "webp", "image/bmp": "bmp"}.get(ctype)
        if not ext:
            return self._err("unsupported image type: " + ctype, 415)
        PASTED_IMG_DIR.mkdir(parents=True, exist_ok=True)
        p = PASTED_IMG_DIR / (uuid.uuid4().hex[:6] + "." + ext)   # short name → short, non-wrapping path
        try:
            p.write_bytes(data)
        except OSError as e:
            return self._err("could not save image: " + str(e), 500)
        return self._json({"path": str(p), "bytes": len(data)})

    def _chunk(self, text):
        data = text.encode() if isinstance(text, str) else text
        try:
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()
        except OSError:
            pass

    def _models_pull(self, body):
        """POST /api/models/pull — stream ollama /api/pull NDJSON as chunked text."""
        prov = _provider(body.get("provider", ""))
        if not prov or prov.get("type") != "ollama":
            return self._err("ollama provider required", 400)
        base = _provider_base(prov)
        payload = json.dumps({"name": body.get("model"), "stream": True}).encode()
        req = urllib.request.Request(base + "/api/pull", data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=180)
        except (urllib.error.URLError, OSError) as e:
            return self._err(f"provider unreachable: {e}", 502)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for line in resp:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    status = obj.get("status", "") or obj.get("error", "")
                    if obj.get("total") and obj.get("completed"):
                        status += f" {int(obj['completed'] * 100 / obj['total'])}%"
                    self._chunk(status + "\n")
                except (json.JSONDecodeError, ValueError, ZeroDivisionError):
                    self._chunk(line.decode("utf-8", "replace") + "\n")
        except OSError:
            pass
        finally:
            self._chunk("")
            try:
                resp.close()
            except OSError:
                pass

    def _chat_proxy(self, body):
        prov = _provider(body.get("provider", ""))
        if not prov:
            return self._err("no such provider", 404)
        base = _provider_base(prov)
        model = body.get("model")
        messages = body.get("messages", [])
        options = body.get("options") or {}
        if body.get("stream") is False:          # non-streamed → {text}
            r = provider_chat(body.get("provider", ""), model, messages, options)
            if not r.get("ok"):
                return self._err(r.get("error", "chat failed"), 502)
            return self._json({"text": r["text"]})
        headers = {"Content-Type": "application/json"}
        if prov.get("type") == "ollama":
            url = base + "/api/chat"
        else:
            url = base + "/v1/chat/completions"
            if prov.get("api_key"):
                headers["Authorization"] = "Bearer " + prov["api_key"]
        req_body = {"model": model, "messages": messages, "stream": True}
        if prov.get("type") == "ollama":
            opts = dict(options or {})
            opts.setdefault("num_ctx", _provider_num_ctx(prov))
            req_body["options"] = opts
        elif options and "temperature" in options:
            req_body["temperature"] = options["temperature"]
        payload = json.dumps(req_body).encode()
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            # generous timeout: a cold model can take minutes to load into memory
            # before Ollama sends anything (a 4s bound made big models 502), and
            # the bigger the box's model, the longer that first byte takes
            resp = urllib.request.urlopen(req, timeout=_provider_timeout(prov))
        except (urllib.error.URLError, OSError) as e:
            return self._err(f"provider unreachable: {e}", 502)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        ptype = prov.get("type")
        # ollama --verbose: forward the timing stats from the final done-chunk as an
        # \x1e-prefixed JSON trailer so the frontend can show them without polluting the reply.
        verbose = bool(body.get("verbose")) and ptype == "ollama"
        stats = None
        _STAT_KEYS = ("total_duration", "load_duration", "prompt_eval_count",
                      "prompt_eval_duration", "eval_count", "eval_duration", "done_reason")
        try:
            for line in resp:
                line = line.strip()
                if not line:
                    continue
                delta = _chat_delta(ptype, line)
                if delta:
                    self._chunk(delta)
                if verbose:
                    try:
                        obj = json.loads(line)
                        if obj.get("done"):
                            stats = {k: obj[k] for k in _STAT_KEYS if obj.get(k) is not None}
                    except (json.JSONDecodeError, ValueError):
                        pass
        except OSError:
            pass
        finally:
            if stats:
                self._chunk("\x1e" + json.dumps(stats))
            self._chunk("")        # terminating 0-length chunk
            try:
                resp.close()
            except OSError:
                pass

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            route = url.path
            if route == "/ws/term":
                return self._term_ws(q)
            if route == "/ws/builder":
                return self._builder_ws(q)
            if route == "/ws/session":
                return self._session_ws(q)
            if route == "/api/builder/list":
                with _builders_lock:
                    return self._json({"builders": [builder_public(x)
                                                    for x in BUILDERS.values()]})
            if route == "/api/pa":
                # Display name for the prime-agent box, so the terminal panel can label
                # its controls with the operator's own name for it. Deliberately NOT
                # derived from pa_cfg("host"): that is a user@address, which is both
                # ugly in a <label> and the kind of thing that must not be baked into
                # source. Unset -> "" and the UI falls back to a generic word.
                return self._json({"label": pa_cfg("label", "")})
            if route == "/api/term/list":
                with _terms_lock:
                    terms = [term_public(t) for t in TERMS.values()]
                    known = {t["id"] for t in terms}
                # surface persistent tmux sessions the server has no live record of
                # (e.g. reattachable after a restart) so the client can re-open them
                for session in tmux_zenith_sessions():
                    tid = session[len("zenith-"):]
                    if tid not in known:
                        cwd = tmux_pane_cwd(session)
                        mode, rid = tmux_pane_info(session)
                        base = Path(cwd).name if cwd else tid
                        terms.append({"id": tid, "cwd": cwd, "mode": mode,
                                      "persist": True, "status": "live", "effort": None,
                                      "resume_id": rid,
                                      "label": base if mode != "shell" else f"{base} · shell",
                                      "created": ""})
                terms.sort(key=lambda t: t["created"], reverse=True)
                return self._json({"terms": terms, "tmux": bool(TMUX_BIN)})
            if route == "/api/overview":
                return self._json(overview())
            if route == "/api/update":
                return self._json(update_check())
            if route == "/api/tailscale/status":
                return self._json(tailscale_state())
            if route == "/api/tailscale/reauth":
                return self._json(tailscale_reauth())
            if route in ("/api/gpu/nodes", "/api/gpu/job") and _int_off("fleet"):
                return self._json({"nodes": []} if route == "/api/gpu/nodes"
                                  else {"error": "fleet disabled"})
            if route == "/api/gpu/nodes":
                out = []
                for name, url in gpu_nodes().items():
                    s = gpu_call(name, "/gpu", timeout=6)
                    out.append({"name": name, "url": url, "up": "error" not in s,
                                "gpu": s.get("gpu"), "host": s.get("host"), "error": s.get("error")})
                return self._json({"nodes": out})
            if route == "/api/gpu/job":
                node = q.get("node", "")
                jtype = "".join(c for c in q.get("type", "matmul") if c.isalnum())[:20] or "matmul"
                engine = "docker" if q.get("engine", "venv") == "docker" else "venv"
                n = int(q.get("n", 8192))
                secs = float(q.get("secs", 8))
                path = "/job?type=%s&engine=%s&n=%d&secs=%s" % (jtype, engine, n, secs)
                return self._json(gpu_call(node, path, timeout=330))
            if route == "/api/whisper/health":
                return self._json(whisper_health())
            if route == "/api/capabilities":
                return self._json(capabilities(q.get("refresh") in ("1", "true")))
            if route == "/api/config":
                return self._json({"config": config_redacted(config_load()),
                                   "env_overrides": _config_env_overrides()})
            if route == "/api/statusline":
                return self._json(statusline_get())
            if route == "/api/projects":
                return self._json({"projects": list_projects()})
            if route == "/api/project/stats":
                return self._json(project_stats(q["path"]))
            if route == "/api/sessions":
                return self._json({"sessions": list_sessions(
                    q.get("project"), int(q.get("limit", 80)))})
            if route == "/api/prompts":
                return self._json(prompts_payload(q.get("path"), q.get("term")))
            if route == "/api/fx/media":
                return self._json({"media": fx_media_list()})
            if route == "/api/claude/procs":
                return self._json({"procs": claude_processes()})
            if route == "/api/context":
                pth = q.get("path")
                if not pth and q.get("term"):
                    # same resolution /api/prompts uses: a live terminal -> whatever
                    # transcript it is currently driving
                    tp = term_transcript(q["term"])
                    if not tp:
                        return self._json({"available": False, "term": q["term"],
                                           "reason": "no transcript yet for this terminal"})
                    pth = str(tp)
                if not pth:
                    return self._err("path or term required", 400)
                p = Path(pth).resolve()
                # same containment rule as _verify_material_session: a caller-supplied
                # path must resolve inside a known transcript root before we open it
                if not any(p == r or r in p.parents for r in _transcript_roots()):
                    return self._err("path outside transcripts root", 403)
                return self._json(session_context(p))
            if route == "/api/context/all":
                return self._json(contexts_all(int(q.get("limit", 200))))
            if route == "/api/usage":
                return self._json(plan_usage(force=q.get("refresh") == "1"))
            if route == "/api/session":
                p = Path(q["path"]).resolve()
                fmt, aid = za.transcript_format_for_path(p)
                d = _transcript_detail(p, fmt)
                d["agent"] = aid or "claude"          # surface the agent tag
                if isinstance(d.get("summary"), dict):
                    d["summary"]["agent"] = aid or "claude"
                _snapshot_session(p, d, agent=aid)
                return self._json(d)
            if route == "/api/docs":
                return self._json({"files": docs_tree(q["root"])})
            if route == "/api/file":
                return self._json({"path": q["path"],
                                   "content": read_file_checked(q["path"])})
            if route == "/api/memory":
                return self._json(nm_memories(q.get("q"), q.get("namespace"),
                                              int(q.get("limit", 60)), q.get("tag")))
            if route == "/api/memory/nmstats":
                return self._json(nm_stats())
            if route == "/api/memory/meta":
                return self._json(nm_meta())
            if route == "/api/memory/source":
                return self._json(nm_source())
            if route == "/api/memory/graph":
                return self._json(nm_graph(q.get("namespace"),
                                           int(q.get("cap", 150))))
            if route == "/api/memory/timeline":
                return self._json(nm_timeline(int(q.get("limit", 200))))
            if route == "/api/loops":
                return self._json(nm_schedules())
            if route == "/api/loops2":
                return self._json({"loops": loops_load()})
            if route == "/api/loops2/runs":
                return self._json({"runs": loop_runs(q.get("id", ""),
                                                     int(q.get("limit", 30)))})
            if route == "/api/providers":
                return self._json({"providers": providers_load()})
            if route == "/api/agents2":
                # models resolve through the detection registry (cached-only here —
                # this is a hot path, so it never pays for a probe sweep)
                out = []
                for a in za.load_agents():
                    a = dict(a)
                    if isinstance(a.get("models"), dict):
                        a["models"] = dict(a["models"], list=agent_models(a))
                    out.append(a)
                return self._json({"agents": out})
            if route == "/api/agents2/models":
                a = next((x for x in za.load_agents()
                          if x.get("id") == q.get("agent")), None)
                if not a:
                    return self._err("no such agent", 404)
                return self._json({"agent": a["id"], "models": agent_models(
                    a, refresh=q.get("refresh") in ("1", "true"))})
            if route == "/api/detect":
                return self._json(detect_all(q.get("refresh") in ("1", "true")))
            if route == "/api/config/export":
                return self._json(config_export(
                    q.get("include_secrets") in ("1", "true")))
            if route == "/api/models":
                return self._json(list_models(q.get("provider", ""),
                                              q.get("base_url"), q.get("type"),
                                              q.get("api_key")))
            if route == "/api/files":
                return self._json(files_walk(
                    q["root"], q.get("q", ""), q.get("exts", ""),
                    q.get("hidden", "0") in ("1", "true"), int(q.get("limit", 800)),
                    q.get("flat", "1") != "0"))
            if route == "/api/research/list":
                return self._json(research_list())
            if route == "/api/platform":
                return self._json(platform_status())
            if route == "/api/savedjobs":
                return self._json({"jobs": _load_json(SAVED_JOBS_FILE, [])})
            if route == "/raw":
                return self._raw_file(q.get("path", ""))
            if route == "/api/swarms":
                return self._json({"swarms": _load_json(SWARMS_FILE, [])})
            if route == "/api/wargames":
                return self._json({"wargames": _load_json(WARGAMES_FILE, [])})
            if route == "/api/wargames/reports":
                return self._json({"reports": wargame_reports()})
            if route == "/api/memory/detail":
                return self._json(nm_detail(q["key"]))
            if route == "/api/stats":
                return self._json(stats())
            if route == "/api/filemem":
                return self._json({"groups": file_memories()})
            if route == "/api/agents":
                return self._json({"agents": list_agents()})
            if route == "/api/skills":
                return self._json({"skills": list_skills()})
            if route == "/api/skills/user":
                return self._json({"skills": list_user_skills()})
            if route == "/api/skill":
                return self._json({"path": q["path"],
                                   "content": read_file_checked(q["path"])})
            if route == "/api/agent":
                return self._json({"path": q["file"],
                                   "content": read_file_checked(q["file"])})
            if route == "/api/coordination":
                return self._json(coordination_state())
            if route == "/api/events":
                return self._json({"events": zs.events_query(
                    kind=q.get("kind"), project=q.get("project"),
                    outcome=q.get("outcome"), ref=q.get("ref"), q=q.get("q"),
                    before=q.get("before"), limit=int(q.get("limit", 100)),
                    agent=q.get("agent"))})
            if route == "/api/events/stats":
                return self._json(zs.events_stats(int(q.get("days", 30)),
                                                  q.get("project")))
            if route == "/api/telemetry/sessions":
                return self._json({"sessions": zs.telemetry_sessions(
                    q.get("project"), int(q.get("days", 30)), q.get("agent"))})
            if route == "/api/telemetry/tokens":   # session token rollup (by project/agent)
                return self._json(zs.session_token_stats(int(q.get("days", 30)), q.get("project")))
            if route == "/api/autoverify":
                return self._json({"config": _autoverify_cfg(),
                                   "spent_today": _autoverify_spend_today()})
            if route == "/api/gates":
                return self._json({"gates": zs.gates_query(int(q.get("limit", 100)))})
            if route == "/api/scorecard":
                return self._json({"scores": zs.score_all()})
            if route == "/api/verdicts":
                return self._json({"verdicts": zs.verdicts_query(
                    q.get("target_kind"), q.get("target_ref"),
                    int(q.get("limit", 50))),
                    "harness": _harness_drift()})
            if route == "/api/verdict":
                v = zs.verdict_get(int(q["id"]))
                if not v:
                    return self._err("no such verdict", 404)
                return self._json(v)
            if route == "/api/ab/runs":
                return self._json({"runs": ab_runs(int(q.get("limit", 30)))})
            if route == "/api/ab/run":
                try:
                    return self._json(ab_run(q.get("ab_id", "")))
                except KeyError:
                    return self._err("no such ab run", 404)
            if route == "/api/jobs":
                with _jobs_lock:
                    jobs = [job_public(j) | {"output": []}
                            for j in JOBS.values()]
                jobs.sort(key=lambda j: j["started"], reverse=True)
                vmap = zs.verdicts_latest_for([j["id"] for j in jobs])
                for j in jobs:
                    j["verdict"] = vmap.get(j["id"])
                return self._json({"jobs": jobs})
            if route == "/api/job":
                job = JOBS.get(q.get("id", ""))
                if not job:
                    return self._err("no such job", 404)
                return self._json(job_public(job, int(q.get("offset", 0))))
            # ---- Watchers (NexusMind price/stock watchers, read-only proxy) ----
            # NM's real response envelopes ({"status":"ok","watches":[...]}, etc.) are unwrapped
            # here so the front-end always gets a plain list (or {"error":...} on failure) —
            # single normalization point, front-end never sees the NM envelope shape.
            if route == "/api/watchers":
                r = nm_api("/api/watch/list")
                if not isinstance(r, dict) or r.get("error") or r.get("status") == "error":
                    return self._json({"watchers": {"error": (r or {}).get("error", "watch list failed")}})
                return self._json({"watchers": r.get("watches", [])})
            if route == "/api/watchers/templates":
                r = nm_api("/api/watch/templates")
                if not isinstance(r, dict) or r.get("error") or r.get("status") == "error":
                    return self._json({"error": (r or {}).get("error", "templates fetch failed")})
                return self._json({"templates": r.get("templates", [])})
            if route == "/api/watchers/kinds":
                r = nm_api("/api/watch/kinds")
                if not isinstance(r, dict) or r.get("error") or r.get("status") == "error":
                    return self._json({"error": (r or {}).get("error", "kinds fetch failed")})
                return self._json({"kinds": r.get("kinds", [])})
            if route == "/api/watcher":
                name = urllib.parse.quote(q.get("name", ""), safe="")
                return self._json(nm_api("/api/watch/" + name))
            if route == "/api/watcher/signals":
                name = urllib.parse.quote(q.get("name", ""), safe="")
                limit = max(1, min(int(q.get("limit", 200)), 2000))
                r = nm_api("/api/watch/%s/signals?limit=%d" % (name, limit))
                if not isinstance(r, dict) or r.get("error") or r.get("status") == "error":
                    return self._json({"signals": {"error": (r or {}).get("error", "signals fetch failed")}})
                return self._json({"signals": r.get("signals", [])})
            if route == "/api/watcher/events":
                name = urllib.parse.quote(q.get("name", ""), safe="")
                limit = max(1, min(int(q.get("limit", 100)), 1000))
                r = nm_api("/api/watch/%s/events?limit=%d" % (name, limit))
                if not isinstance(r, dict) or r.get("error") or r.get("status") == "error":
                    return self._json({"events": {"error": (r or {}).get("error", "events fetch failed")}})
                return self._json({"events": r.get("events", [])})
            # ---- Session-watch (external watcher db, opened read-only) ----
            if route == "/api/session-watch":
                return self._json(session_watch_list(
                    float(q["window"]) if q.get("window") else None))
            if route == "/api/session-watch/session":
                if not (q.get("session") or q.get("path") or q.get("term")):
                    return self._err("session, path or term required", 400)
                return self._json(session_watch_session(
                    q.get("session"), q.get("path"), q.get("term")))
            if route == "/api/session-watch/config":
                return self._json(session_watch_config())   # api_key never leaves
            # ---- Cases (detectors over counted session facts) ----
            if route == "/api/cases":
                # N2: `state=all` is the explicit "every state" sentinel.
                # A blank `state=` cannot express it: do_GET builds `q` with
                # parse_qs's default keep_blank_values=False, so the blank
                # value drops the key entirely and q.get("state","open")
                # falls right back to "open". The command bar's `c<id> open`
                # asked for the whole table that way, got only open cases,
                # and then reported any snoozed/dismissed/resolved id as
                # "no directory recorded on that case" — a false statement
                # about a case that does have a cwd.
                #
                # Fixed here rather than by flipping keep_blank_values on
                # that shared parse: `q` feeds EVERY GET route, and turning
                # blanks into present-but-empty keys would change how each
                # one tells "absent" from "empty" (e.g. the session-watch
                # route's `session or path or term` required-arg check, which
                # a blank would start satisfying). A sentinel on the one
                # route that needs it cannot reach the others. 'all' is not a
                # real case state, so it collides with nothing.
                state = q.get("state", "open")
                return self._json({"cases": zs.cases_query(
                    state=(None if state == "all" else state),
                    detector=q.get("detector"),
                    project=q.get("project"), limit=int(q.get("limit", 100)))})
            if route == "/api/cases/config":
                return self._json(zc.load_config())
            if route.startswith("/api/"):
                return self._err("unknown endpoint", 404)
            return self._static(route)
        except PermissionError as e:
            return self._err(e, 403)
        except (KeyError, ValueError) as e:
            return self._err(e, 400)
        except (OSError, sqlite3.Error) as e:
            return self._err(e, 500)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if url.path == "/api/whisper":          # raw multipart, not JSON
                return self._whisper_post(length)
            if url.path == "/api/fx/media":           # raw image/gif bytes, not JSON
                return self._fx_media_post(length)
            if url.path == "/api/term/paste-image":   # raw image bytes, not JSON
                return self._paste_image_post(length)
            body = json.loads(self.rfile.read(length) or b"{}")
            if url.path == "/api/statusline":
                r = statusline_set(body.get("enabled"), body.get("options"),
                                   body.get("visible"))
                _cfg_evt("settings", "save", "statusline", "statusline")
                return self._json(r)
            if url.path == "/api/statusline/external":
                r = lk_set(body.get("lines"), body.get("visible"))
                if not r.get("available"):
                    return self._err(r.get("error", "no external statusline"), 404)
                _cfg_evt("settings", "save", "statusline", "lk-statusline")
                return self._json(r)
            if url.path == "/api/session-watch/config":
                # Config write on par with /api/statusline: NOT risk-gated. The
                # response is the same api_key-free view the GET returns, and the
                # event records only which provider was chosen, never its key.
                r = session_watch_config_save(body)
                _cfg_evt("settings", "save", "session-watch",
                         r.get("provider_name") or "counted stats only")
                return self._json(r)
            if url.path == "/api/config":
                # Deep-merge patch → validate → persist → re-resolve globals →
                # re-probe. Config write on par with /api/statusline: NOT risk-gated.
                # Response carries the freshly re-probed capabilities (§3.5).
                patch = body if isinstance(body, dict) else {}
                ints = patch.get("integrations")
                if ints is not None:
                    if not isinstance(ints, dict):
                        return self._err("integrations must be an object", 400)
                    for iid, ival in ints.items():
                        if iid not in INTEGRATION_IDS:
                            return self._err("unknown integration: " + str(iid), 400)
                        if isinstance(ival, dict) and "mode" in ival \
                                and ival["mode"] not in ("auto", "on", "off"):
                            return self._err("mode must be auto|on|off", 400)
                mods = patch.get("modules")
                if mods is not None:
                    if not isinstance(mods, dict):
                        return self._err("modules must be an object", 400)
                    if "settings" in mods:
                        return self._err("modules.settings is not configurable", 400)
                    if any(not isinstance(v, bool) for v in mods.values()):
                        return self._err("modules values must be booleans", 400)
                # Empty token means "keep the stored one" (§2.2) — drop it pre-merge.
                if _cfg_get(patch, "integrations.nexusmind_api.token", None) == "":
                    del patch["integrations"]["nexusmind_api"]["token"]
                with _data_lock:
                    merged = _deep_merge(config_load(), patch)
                    _save_json(CONFIG_FILE, merged)
                config_apply(merged)
                _cfg_evt("config", "save")
                return self._json({"ok": True, "capabilities": capabilities(refresh=True)})
            if url.path == "/api/config/import":
                # Validate everything first: a malformed bundle must change NOTHING.
                r = config_import(body)
                if not r.get("ok"):
                    return self._err(r.get("error", "invalid bundle"), 400)
                _cfg_evt("config", "import")
                return self._json(r)
            if url.path == "/api/statusline/install":
                r = statusline_install()
                if r.get("code") == 409:
                    return self._err(r["error"], 409)
                return self._json(r)
            if url.path == "/api/statusline/uninstall":
                return self._json(statusline_uninstall())
            if url.path == "/api/session/rename":
                p = str(body.get("path") or "").strip()
                if not p:
                    return self._err("missing path", 400)
                name = str(body.get("name") or "").strip()[:120]
                names = _load_json(SESSION_NAMES_FILE, {})
                if not isinstance(names, dict):
                    names = {}
                if name:
                    names[p] = name
                else:
                    names.pop(p, None)          # empty name clears the custom label
                _save_json(SESSION_NAMES_FILE, names)
                return self._json({"ok": True, "name": name})
            if url.path == "/api/jobs":
                project = str(Path(body["project"]).resolve())
                if not (project == str(PROJECTS_ROOT)
                        or Path(project).parent == PROJECTS_ROOT):
                    return self._err("project outside claudeProjects", 403)
                mode = body.get("mode", "default")
                if mode not in ("default", "acceptEdits", "bypassPermissions"):
                    return self._err("bad mode", 400)
                effort = body.get("effort")
                if effort is not None and effort not in EFFORT_TOKENS:
                    return self._err("bad effort", 400)
                add_dir = body.get("add_dir")
                if add_dir:
                    ad = str(Path(add_dir).resolve())
                    if not (ad == str(HOME) or ad.startswith(str(HOME) + os.sep)):
                        return self._err("add_dir outside HOME", 403)
                    add_dir = ad
                extra_env = body.get("env")
                if extra_env is not None and not isinstance(extra_env, dict):
                    return self._err("env must be an object", 400)
                # Local/provider source (mirrors the A/B provider seam): when a
                # `provider` is set, the job runs against a saved OpenAI-compatible
                # /ollama endpoint instead of Claude Code. `via_aider` false → raw
                # prompt→completion (spawn_provider_job); true → aider coding agent
                # pointed at the provider (spawn_job + _provider_openai_env). When
                # `provider` is absent the Claude path below is byte-identical.
                provider = str(body.get("provider") or "").strip()
                if provider:
                    if provider not in {p.get("id") for p in providers_load()
                                        if p.get("enabled") is not False}:
                        return self._err("unknown or disabled provider: "
                                         + provider, 400)
                    pmodel = str(body.get("model") or "").strip()
                    if not pmodel:
                        return self._err("provider job needs a model", 400)
                    pctx = str(body.get("context") or "none")
                    ptask = str(body.get("task") or "chat")
                    if pctx not in CONTEXT_CHOICES:
                        return self._err("bad context", 400)
                    if ptask not in PROVIDER_TASKS:
                        return self._err("bad task", 400)
                    if not _gate_check(self, "/api/jobs", body):
                        return
                    if body.get("via_aider"):
                        if not any(a.get("id") == "aider" and a.get("enabled")
                                   for a in za.load_agents()):
                            return self._err("aider agent is not enabled", 400)
                        job_id = spawn_job(
                            project, body["prompt"], model="openai/" + pmodel,
                            mode=mode, budget=body.get("budget"), effort=effort,
                            skills=body.get("skills"), loop_id=body.get("loop_id"),
                            label=body.get("label"), saved_id=body.get("saved_id"),
                            extra_env=_provider_openai_env(provider),
                            add_dir=add_dir, agent="aider")
                    else:
                        job_id = spawn_provider_job(
                            project, body.get("prompt") or "", provider, pmodel,
                            label=body.get("label"),
                            loop_id=body.get("loop_id"),
                            saved_id=body.get("saved_id"),
                            context=pctx, task=ptask,
                            context_globs=body.get("context_globs") or "")
                    return self._json({"id": job_id})
                agent = str(body.get("agent") or "claude")
                if not any(a.get("id") == agent and a.get("enabled")
                           for a in za.load_agents()):
                    return self._err("unknown or disabled agent: " + agent, 400)
                if not _gate_check(self, "/api/jobs", body):
                    return
                job_id = spawn_job(project, body["prompt"],
                                   body.get("model", "sonnet"), mode,
                                   body.get("budget"), effort=effort,
                                   skills=body.get("skills"),
                                   loop_id=body.get("loop_id"),
                                   label=body.get("label"),
                                   saved_id=body.get("saved_id"),
                                   extra_env=extra_env, add_dir=add_dir,
                                   agent=agent)
                return self._json({"id": job_id})
            if url.path == "/api/verify":
                if not _gate_check(self, "/api/verify", body):
                    return
                verify_id, ringer = spawn_verify(body.get("kind") or "job", body)
                with _jobs_lock:
                    vmodel = _VERIFY_PENDING.get(verify_id, {}).get("model", "")
                return self._json({"ok": True, "verify_id": verify_id,
                                   "job_id": ringer, "model": vmodel})
            if url.path == "/api/ab/launch":
                project = str(Path(body["project"]).resolve())
                if not (project == str(PROJECTS_ROOT)
                        or Path(project).parent == PROJECTS_ROOT):
                    return self._err("project outside claudeProjects", 403)
                prompt = str(body.get("prompt") or "").strip()
                if not prompt:
                    return self._err("prompt required", 400)
                mode = body.get("mode", "default")
                if mode not in ("default", "acceptEdits", "bypassPermissions"):
                    return self._err("bad mode", 400)
                arms = body.get("arms")
                if not isinstance(arms, list) or not (2 <= len(arms) <= 4):
                    return self._err(
                        "arms must be a list of 2-4 {agent,model} entries", 400)
                enabled = {a.get("id") for a in za.load_agents()
                           if a.get("enabled")}
                prov_ids = {p.get("id") for p in providers_load()
                            if p.get("enabled") is not False}
                for arm in arms:
                    if not isinstance(arm, dict):
                        return self._err("each arm must be an object", 400)
                    ag = arm.get("agent")
                    prov = arm.get("provider")
                    if ag == "provider":              # raw local/remote model arm
                        if prov not in prov_ids:
                            return self._err("unknown or disabled provider: "
                                             + str(prov), 400)
                        if not str(arm.get("model") or "").strip():
                            return self._err("provider arm needs a model", 400)
                    else:
                        if ag not in enabled:
                            return self._err("unknown or disabled agent: "
                                             + str(ag), 400)
                        if prov is not None and prov not in prov_ids:  # agent-via-provider
                            return self._err("unknown or disabled provider: "
                                             + str(prov), 400)
                    if arm.get("effort") is not None \
                            and arm.get("effort") not in EFFORT_TOKENS:
                        return self._err("bad effort", 400)
                if not _gate_check(self, "/api/ab/launch", body):
                    return
                return self._json(ab_launch(
                    project, prompt, mode, arms,
                    verify=bool(body.get("verify", True)),
                    judge=bool(body.get("judge", True)),
                    judge_model=body.get("judge_model"),
                    sequential=bool(body.get("sequential", False))))
            if url.path == "/api/autoverify":
                if not _gate_check(self, "/api/autoverify", body):
                    return
                cfg = _autoverify_cfg()
                for k in ("enabled", "min_cost_usd", "daily_cap_usd"):
                    if k in body:
                        cfg[k] = body[k]
                if isinstance(body.get("triggers"), dict):
                    cfg["triggers"] = dict(cfg["triggers"]) | body["triggers"]
                cfg["enabled"] = bool(cfg["enabled"])
                cfg["min_cost_usd"] = float(cfg["min_cost_usd"] or 0)
                cfg["daily_cap_usd"] = float(cfg["daily_cap_usd"] or 0)
                with _data_lock:
                    _save_json(AUTOVERIFY_FILE, cfg)
                _cfg_evt("autoverify", "save", "autoverify", "autoverify")
                return self._json({"config": cfg})
            if url.path == "/api/gate/deny":
                tok = str(body.get("token") or "")
                with _gates_lock:
                    rec = _GATES_PENDING.pop(tok, None)
                if rec:
                    _gate_log(Gate(rec["action"], rec["op"], rec["blast"],
                                   rec["rev"], "confirm"),
                              rec["route"], rec["project"], "denied",
                              token_hash=rec["token_hash"])
                return self._json({"ok": True})
            if url.path == "/api/scorecard":
                if not _gate_check(self, "/api/scorecard", body):
                    return
                n = int(body["n"])
                status = body["status"]
                if not (1 <= n <= 12):
                    return self._err("n must be 1..12", 400)
                if status not in ("present", "partial", "absent", "na"):
                    return self._err("bad status", 400)
                zs.score_set(n, status, body.get("note", ""),
                             body.get("evidence"))
                _cfg_evt("scorecard", "save", str(n), f"p{n}->{status}")
                return self._json({"ok": True, "scores": zs.score_all()})
            if url.path == "/api/install-deps":
                reqd = body.get("packages", []) or []
                # system packages (tmux/ffmpeg) go to the package manager, pip ones to pip.
                # One at a time for system deps — each has its own argv and its own verdict.
                syspkgs = [p for p in reqd if p in SYS_INSTALLABLE]
                if syspkgs:
                    return self._json(install_sys_dep(syspkgs[0]))
                return self._json(install_deps(reqd))
            if url.path == "/api/savedjobs/save":
                r = _crud_upsert(SAVED_JOBS_FILE, body)
                _cfg_evt("savedjobs", "save", r.get("id"), r.get("name"))
                return self._json({"job": r})
            if url.path == "/api/savedjobs/delete":
                ok = _crud_delete(SAVED_JOBS_FILE, body["id"])
                _cfg_evt("savedjobs", "delete", body["id"])
                return self._json({"ok": ok})
            if url.path == "/api/loops2/save":
                if not _gate_check(self, "/api/loops2/save", body):
                    return
                r = loop_upsert(body)
                _cfg_evt("loops", "save", r.get("id"), r.get("name"))
                return self._json({"loop": r})
            if url.path == "/api/loops2/delete":
                ok = loop_delete(body["id"])
                _cfg_evt("loops", "delete", body["id"])
                return self._json({"ok": ok})
            if url.path == "/api/loops2/run":
                return self._json({"id": loop_run_now(body["id"])})
            # ---- Watchers (NexusMind price/stock watchers) — write proxy ----
            # DB-first via NM's admin API, then git write-through of the canonical
            # YAML into the homelab repo (_watchers_commit; push best-effort).
            # NM errors come back as HTTP 200 + {"error","detail"} so the form
            # can render the validator message inline (apiSafe throws on !ok).
            if url.path == "/api/watchers/enable":
                if not _gate_check(self, "/api/watchers/enable", body):
                    return
                name = str(body.get("name") or "").strip()
                if not name:
                    return self._err("name required", 400)
                r = nm_api("/api/watch/admin/enable", "POST",
                           {"name": name, "enabled": bool(body.get("enabled"))})
                if not isinstance(r, dict) or r.get("error") \
                        or r.get("status") == "error":
                    return self._json({"error": (r or {}).get("detail")
                                       or (r or {}).get("message")
                                       or (r or {}).get("error") or "enable failed"})
                _cfg_evt("watchers", "enable", name,
                         "%s -> enabled=%s" % (name, r.get("enabled")))
                return self._json({"ok": True, "name": r.get("name", name),
                                   "enabled": r.get("enabled")})
            if url.path == "/api/watchers/save":
                if not _gate_check(self, "/api/watchers/save", body):
                    return
                spec = body.get("spec")
                if not isinstance(spec, dict):
                    return self._err("spec object required", 400)
                name = str(spec.get("name") or "").strip()
                if not _WATCH_NAME_RE.match(name):
                    return self._err(
                        "bad watcher name — letters, digits, . _ - only", 400)
                payload = {"spec": spec}
                if body.get("cooldown_min") is not None:
                    try:
                        payload["cooldown_min"] = int(body["cooldown_min"])
                    except (TypeError, ValueError):
                        return self._err("cooldown_min must be an integer", 400)
                # Pre-flight the comment guard BEFORE the DB write. This endpoint
                # is DB-first, so refusing only at the file-write site would leave
                # NM updated while the repo kept the old annotated YAML — and the
                # next repo->DB sync would silently revert the save. Refuse whole.
                refusal = _watchers_comment_guard(name)
                if refusal:
                    return self._json({"error": "refused: watcher YAML has comments",
                                       "detail": refusal, "refused": True})
                r = nm_api("/api/watch/admin/upsert", "POST", payload)
                if not isinstance(r, dict) or r.get("error") \
                        or r.get("status") == "error":
                    return self._json({"error": (r or {}).get("error")
                                       or "save failed",
                                       "detail": (r or {}).get("detail")
                                       or (r or {}).get("message") or ""})
                yaml_text = str(r.get("yaml") or "")
                git = (_watchers_commit(str(r.get("name") or name), yaml_text)
                       if yaml_text.strip()
                       else {"committed": False, "pushed": False})
                _cfg_evt("watchers", "save", name, name)
                return self._json({"ok": True,
                                   "name": r.get("name", name)} | git)
            if url.path == "/api/watchers/delete":
                if not _gate_check(self, "/api/watchers/delete", body):
                    return
                name = str(body.get("name") or "").strip()
                if not _WATCH_NAME_RE.match(name):
                    return self._err("bad watcher name", 400)
                git = _watchers_commit(name, delete=True)   # repo first, then DB
                r = nm_api("/api/watch/admin/delete", "POST", {"name": name})
                if not isinstance(r, dict) or r.get("error") \
                        or r.get("status") == "error":
                    return self._json({"error": (r or {}).get("detail")
                                       or (r or {}).get("error")
                                       or "delete failed"} | git)
                _cfg_evt("watchers", "delete", name, name)
                return self._json({"ok": True, "name": name} | git)
            if url.path == "/api/watchers/fetchnow":
                # read-only dry-run: NM validates the spec and fetches once,
                # nothing is persisted — the form's "test before save" button.
                # NM's fetch may exceed nm_api's 5s timeout only for headed/
                # sandbox kinds, which return a note immediately instead.
                if not _gate_check(self, "/api/watchers/fetchnow", body):
                    return
                spec = body.get("spec")
                if not isinstance(spec, dict):
                    return self._err("spec object required", 400)
                r = nm_api("/api/watch/admin/fetch-now", "POST", {"spec": spec})
                if not isinstance(r, dict) or r.get("error") \
                        or r.get("status") == "error":
                    return self._json({"error": (r or {}).get("error")
                                       or "fetch-now failed",
                                       "detail": (r or {}).get("detail")
                                       or (r or {}).get("message") or ""})
                return self._json({"ok": True, "reading": r.get("reading"),
                                   "rendered": r.get("rendered"),
                                   "note": r.get("note")})
            if url.path == "/api/research":
                job_id = research_compose(body.get("topic", ""),
                                          body.get("mode", "research"),
                                          body.get("target"),
                                          body.get("model", "sonnet"),
                                          body.get("effort"))
                return self._json({"id": job_id})
            if url.path == "/api/research/apply":
                r = research_apply(body["src"])
                _cfg_evt("research", "apply", body["src"])
                return self._json(r)
            if url.path == "/api/swarms/save":
                r = _crud_upsert(SWARMS_FILE, body)
                _cfg_evt("swarms", "save", r.get("id"), r.get("name"))
                return self._json({"swarm": r})
            if url.path == "/api/swarms/delete":
                ok = _crud_delete(SWARMS_FILE, body["id"])
                _cfg_evt("swarms", "delete", body["id"])
                return self._json({"ok": ok})
            if url.path == "/api/swarms/launch":
                if not _gate_check(self, "/api/swarms/launch", body):
                    return
                return self._json({"id": swarm_launch(
                    body["id"], body.get("task", ""), body.get("mode", "default"))})
            if url.path == "/api/wargames/save":
                r = _crud_upsert(WARGAMES_FILE, body)
                _cfg_evt("wargames", "save", r.get("id"), r.get("name"))
                return self._json({"wargame": r})
            if url.path == "/api/wargames/delete":
                ok = _crud_delete(WARGAMES_FILE, body["id"])
                _cfg_evt("wargames", "delete", body["id"])
                return self._json({"ok": ok})
            if url.path == "/api/wargames/launch":
                if not _gate_check(self, "/api/wargames/launch", body):
                    return
                return self._json({"id": wargame_launch(
                    body["id"], body.get("mode", "default"))})
            if url.path == "/api/project/new":
                name = str(body.get("name") or "").strip()
                if not re.match(r"^[A-Za-z0-9_][\w .-]{0,63}$", name) or "/" in name or ".." in name:
                    return self._err("invalid name — start with a letter/digit; letters, digits, space, . _ - only", 400)
                parent = Path(str(body.get("parent") or PROJECTS_ROOT)).expanduser().resolve()
                if not (parent == HOME or str(parent).startswith(str(HOME) + os.sep)):
                    return self._err("parent folder must be under your home directory", 400)
                dest = parent / name
                if dest.exists():
                    return self._err("a folder with that name already exists there", 400)
                parent.mkdir(parents=True, exist_ok=True)
                dest.mkdir()
                git_ok = False
                if body.get("git", True) and shutil.which("git"):
                    try:
                        _git(["init"], str(dest)); git_ok = True
                    except Exception:
                        pass
                _cfg_evt("project", "new", str(dest), name)
                return self._json({"path": str(dest), "name": name, "git": git_ok})
            if url.path == "/api/providers/save":
                body["num_ctx"] = _int_or(body.get("num_ctx"), DEFAULT_NUM_CTX)
                body["timeout_s"] = _int_or(body.get("timeout_s"), DEFAULT_TIMEOUT_S)
                r = _crud_upsert(PROVIDERS_FILE, body)
                _cfg_evt("providers", "save", r.get("id"), r.get("name"))
                return self._json({"provider": r})
            if url.path == "/api/providers/delete":
                ok = _crud_delete(PROVIDERS_FILE, body["id"])
                _cfg_evt("providers", "delete", body["id"])
                return self._json({"ok": ok})
            if url.path == "/api/providers/test":
                return self._json(provider_test(body.get("base_url", ""),
                                                 body.get("type", "ollama"),
                                                 body.get("api_key")))
            if url.path == "/api/agents2/save":
                if not _gate_check(self, "config.write", body):
                    return
                if not str(body.get("id") or "").strip():
                    return self._err("agent id required", 400)
                cfmt = body.get("cost_format") or "none"
                if cfmt not in za.COST_PARSERS:
                    return self._err(f"unregistered cost_format: {cfmt}", 400)
                tfmt = ((body.get("transcript") or {}).get("format")
                        if isinstance(body.get("transcript"), dict) else None) or "none"
                if tfmt not in za.TRANSCRIPT_PARSERS:
                    return self._err(f"unregistered transcript.format: {tfmt}", 400)
                if "mode_flags" in body and not isinstance(body["mode_flags"], dict):
                    return self._err("mode_flags must be an object (mode -> flag list)", 400)
                if "argv" in body and not isinstance(body["argv"], list):
                    return self._err("argv must be an array of template tokens", 400)
                r = _crud_upsert(AGENTS2_FILE, body)
                _cfg_evt("agents2", "save", r.get("id"), r.get("label"))
                return self._json({"agent": r})
            if url.path == "/api/agents2/delete":
                if str(body.get("id")) == "claude":
                    return self._err("claude is the fallback agent — disable it, don't delete", 400)
                ok = _crud_delete(AGENTS2_FILE, body["id"])
                _cfg_evt("agents2", "delete", body["id"])
                return self._json({"ok": ok})
            if url.path == "/api/agents2/probe":
                return self._json(_agent_probe(str(body.get("agent") or "")))
            if url.path == "/api/models/pull":
                return self._models_pull(body)
            if url.path == "/api/chat":
                return self._chat_proxy(body)
            if url.path == "/api/generate/constrained":
                prov = str(body.get("provider") or "")
                model = str(body.get("model") or "")
                prompt = str(body.get("prompt") or "")
                checks = body.get("checks")
                if not (prov and model and prompt):
                    return self._err("provider, model and prompt are required", 400)
                if not isinstance(checks, list):
                    return self._err("checks must be a list", 400)
                return self._json(constrained_generate(
                    prov, model, prompt, checks,
                    body.get("max_tries", 4), float(body.get("temperature", 0.7))))
            if url.path == "/api/skill/save":
                r = skill_save(body["name"], body["content"])
                _cfg_evt("skills", "save", body["name"], body["name"])
                return self._json(r)
            if url.path == "/api/skill/delete":
                r = skill_delete(body["name"])
                _cfg_evt("skills", "delete", body["name"], body["name"])
                return self._json(r)
            if url.path == "/api/skill/apply":
                r = skill_apply(body["src"])
                _cfg_evt("skills", "apply", body["src"])
                return self._json(r)
            if url.path == "/api/agent/save":
                r = agent_save(body["name"], body["content"])
                _cfg_evt("agents", "save", body["name"], body["name"])
                return self._json(r)
            if url.path == "/api/agent/delete":
                r = agent_delete(body["name"])
                _cfg_evt("agents", "delete", body["name"], body["name"])
                return self._json(r)
            # NexusMind terminal: ask / classifying ingest / correct. All proxied
            # server-side through nm_api so the token never reaches the browser and
            # the `nexusmind_api` switch gates them (same rule as /api/capture).
            if url.path == "/api/memory/ask":
                return self._json(nm_ask(body.get("question", ""),
                                         body.get("namespace")))
            if url.path == "/api/memory/ingest":
                return self._json(nm_ingest(body.get("text", ""),
                                            body.get("source") or "zenith-nm-terminal",
                                            body.get("hints")))
            if url.path == "/api/memory/correct":
                return self._json(nm_correct(body.get("text", ""), body.get("key")))
            if url.path == "/api/memory/capture":
                r = nm_capture(body.get("title", ""), body.get("content", ""),
                               body.get("tags", []), body.get("namespace", "default"))
                if not r.get("queued"):        # real error → toast, not a false "captured"
                    return self._err(r.get("error") or "capture failed", 502)
                return self._json(r)
            if url.path == "/api/term":
                try:
                    term_id = spawn_term(body.get("cwd"), body.get("mode", "shell"),
                                         body.get("persist", True),   # tmux-backed by default; survives restarts
                                         body.get("resume_id"), body.get("effort"),
                                         body.get("worktree"))   # prime-agent mode only
                except ValueError as e:
                    return self._err(str(e), 400)
                return self._json({"id": term_id, "term": term_public(TERMS[term_id])})
            if url.path == "/api/term/kill":
                return self._json({"ok": kill_term(body.get("id", ""))})
            if url.path == "/api/builder":
                try:
                    bid = spawn_builder(body.get("worktree"))
                except ValueError as e:
                    return self._err(str(e), 400)
                except OSError as e:
                    return self._err("could not start builder: " + str(e)[:160], 502)
                return self._json({"id": bid, "builder": builder_public(BUILDERS[bid])})
            if url.path == "/api/builder/kill":
                return self._json({"ok": kill_builder(body.get("id", ""))})
            if url.path == "/api/fx/media/delete":
                return self._json(fx_media_delete(body.get("name")))
            if url.path == "/api/claude/kill":
                return self._json(kill_claude_proc(body.get("pid"),
                                                   bool(body.get("force"))))
            if url.path == "/api/job/stop":
                job = JOBS.get(body.get("id", ""))
                if not job:
                    return self._err("no such job", 404)
                proc = job.get("_proc")
                if job["status"] == "running" and (proc or job.get("agent") == "provider"):
                    if proc:
                        proc.terminate()
                    else:
                        # provider job: no process to kill. The in-flight HTTP
                        # call is left to finish; run() honors 'stopped' and a
                        # multi-unit review breaks out between units.
                        job["output"].append("[stopped — provider call left in flight]")
                    job["status"] = "stopped"
                    job["ended"] = now_iso()
                return self._json({"ok": True})

            # ---- Cases: snooze/dismiss/mute are DB-only writes scoped to one
            # case (same class as the session-watch config write above) —
            # deliberately NOT added to GATE_RULES. The frontend still calls
            # them through jpost, so if a future slice does gate them the
            # confirm modal appears with no frontend change. The one
            # genuinely privileged verb Cases surfaces — kill — reaches the
            # existing job.stop rule above, untouched.
            if url.path == "/api/cases/act":
                cid = int(body.get("id") or 0)
                verb = str(body.get("verb") or "")
                c = zs.case_by_id(cid)
                if not c:
                    return self._err("no such case", 404)
                now = datetime.now(timezone.utc)
                if verb == "snooze":
                    mins = max(1, min(int(body.get("minutes") or 60), 10080))
                    zs.case_update(cid, state="snoozed",
                                   snooze_until=(now + timedelta(minutes=mins)).isoformat())
                elif verb == "dismiss":
                    zs.case_update(cid, state="dismissed", resolution="dismissed",
                                   resolved_ts=now.isoformat())
                elif verb == "mute":
                    mins = max(1, min(int(body.get("minutes") or 60), 43200))
                    cfg = zc.load_config()
                    cfg.setdefault("mutes", []).append({
                        "detector": c["detector"],
                        "address": "%s/%s" % (c.get("project", ""),
                                              c.get("workstream", "main")),
                        "until": (now + timedelta(minutes=mins)).isoformat()})
                    zc.save_config(cfg)
                    zs.case_update(cid, state="dismissed", resolution="dismissed",
                                   resolved_ts=now.isoformat())
                else:
                    return self._err("unknown verb: " + verb, 400)
                zs.emit("case.action", project=c.get("project", ""), ref=str(cid),
                        outcome=verb, actor="user", data={"detector": c["detector"]})
                return self._json({"ok": True, "case": zs.case_by_id(cid)})

            if url.path == "/api/cases/config":
                zc.save_config(body)
                _cfg_evt("settings", "save", "cases", "cases")
                return self._json({"ok": True, "config": zc.load_config()})

            if url.path == "/api/capture":
                # Quick-capture to NexusMind. Proxied server-side ON PURPOSE: the
                # NM token must never reach the browser, same rule as the
                # session-watch api_key. nm_capture_text() is the ONE client —
                # nm_api, one token source, gated by `nexusmind_api`.
                text = str(body.get("text") or "").strip()
                if not text:
                    return self._err("text required", 400)
                return self._json(nm_capture_text(text, body.get("project") or ""))

            return self._err("unknown endpoint", 404)
        except PermissionError as e:
            return self._err(e, 403)
        except RuntimeError as e:   # e.g. native Windows terminal without pywinpty
            return self._err(e, 501)
        except (KeyError, ValueError, json.JSONDecodeError, OSError) as e:
            return self._err(e, 400)

    def _raw_file(self, path):
        """Serve an allowed file with its native content-type so HTML/CSS/JS
        and images render as intended (iframe or new tab). READ_ROOTS-guarded."""
        p = Path(path).resolve()
        if not any(p == r or r in p.parents for r in READ_ROOTS):
            return self._err("path outside allowed roots", 403)
        if not p.is_file():
            return self._err("not a file", 404)
        if p.stat().st_size > 25_000_000:
            return self._err("file too large", 413)
        body = p.read_bytes()
        ctype = MIME.get(p.suffix.lower(), "application/octet-stream")
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                   "image/svg+xml"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, route):
        # user-supplied FX media lives outside STATIC_DIR; serve it by BASENAME only so
        # a crafted path can never walk out of the media directory
        if route.startswith("/fxmedia/"):
            p = FX_MEDIA_DIR / os.path.basename(route[len("/fxmedia/"):])
            if not p.is_file() or p.parent.resolve() != FX_MEDIA_DIR.resolve():
                return self._err("not found", 404)
            body = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", MIME.get(p.suffix.lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")   # immutable once written
            self.end_headers()
            self.wfile.write(body)
            return
        rel = route.lstrip("/") or "index.html"
        p = (STATIC_DIR / rel).resolve()
        if STATIC_DIR not in p.parents and p != STATIC_DIR:
            return self._err("forbidden", 403)
        if p.is_dir():
            p = p / "index.html"
        if not p.is_file():
            p = STATIC_DIR / "index.html"   # SPA fallback
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(p.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # never let the browser serve a stale app.js/apps.js/index.html from cache — a plain reload
        # must always pick up the latest UI (otherwise frontend fixes silently never appear).
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)


def _reconstruct_workspace():
    """On boot, rebuild terminals whose tmux session is gone (i.e. after a full reboot,
    which kills tmux + every claude). For each recovery manifest under data/live/, if its
    tmux session no longer exists, relaunch it — resuming the exact claude conversation by
    session_id when known — reusing the SAME term id so the client restores it in place.
    Sessions that merely survived (sleep / server restart) are left for the re-adopt path."""
    if not (TMUX_BIN and not IS_WINDOWS) or not WORKSPACE_DIR.is_dir():
        return
    rebuilt = 0
    for f in sorted(WORKSPACE_DIR.glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except Exception:
            continue
        tid = rec.get("id") or f.stem
        session = f"zenith-{tid}"
        alive = subprocess.run([TMUX_BIN, "has-session", "-t", session],
                               capture_output=True).returncode == 0
        if alive:
            continue   # survived — reattach path owns it
        cwd = rec.get("cwd") or str(HOME)
        if not Path(cwd).is_dir():
            cwd = str(HOME)
        sid, rid, mode = rec.get("session_id"), rec.get("resume_id"), rec.get("mode") or "shell"
        if mode == "prime-agent":
            # re-open the SAME remote worktree; the container itself died with the box
            argv = _mode_argv(mode, None, rec.get("worktree"))
        elif sid:
            argv = [CLAUDE_BIN, "--resume", str(sid)]        # exact conversation
        elif mode == "claude-resume" and rid:
            argv = [CLAUDE_BIN, "--resume", str(rid)]
        elif str(mode).startswith("claude"):
            argv = [CLAUDE_BIN, "-c"]                        # newest convo in cwd
        else:
            argv = None                                     # plain shell
        env = _augment_path(dict(os.environ))
        env["TERM"] = "xterm-256color"
        env["ZENITH_TERM_ID"] = tid
        eff = rec.get("effort")
        if eff in EFFORT_TOKENS and mode != "shell":
            env["MAX_THINKING_TOKENS"] = str(EFFORT_TOKENS[eff])
        try:
            pty = TmuxPTY(session, cwd, argv, env)
            _start_term(_new_term_record(tid, pty, cwd, mode, True, eff,
                                         str(rid) if rid else None), rebuilt=True)
            rebuilt += 1
        except Exception as e:
            print(f"  workspace: could not rebuild {tid}: {e}")
    if rebuilt:
        print(f"ZENITH/OS workspace: rebuilt {rebuilt} terminal(s) after reboot")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config_seed()                             # seed data/config.json if absent (checks
    config_apply(config_load())               # zenith.db BEFORE init_db creates it)
    zs.init_db(DB_PATH)                       # bootstrap + migrate + seed + import
    orphans = zs.orphan_sweep()               # close job.spawn without job.end
    zs.emit("server.start", outcome="ok", actor="zenith",
            data={"version": ZENITH_VERSION, "port": PORT, "pid": os.getpid(),
                  "orphans_closed": orphans})
    atexit.register(lambda: zs.emit(          # best-effort; SIGKILL loses it —
        "server.stop", outcome="ok", actor="zenith",   # orphan sweep compensates
        data={"uptime_s": int(time.time() - START_TIME)}))
    providers_load()   # seed data/providers.json on first run
    za.init(AGENTS2_FILE)
    za.load_agents()   # seed data/agents.json on first run (mirrors providers_load)
    _ensure_statusline_script()   # keep scripts/statusline.py in sync with the embedded source
    _reconstruct_workspace()      # rebuild the desktop's terminals if we booted after a reboot
    threading.Thread(target=loop_scheduler, daemon=True).start()
    threading.Thread(target=_telemetry_sweep, daemon=True).start()
    threading.Thread(target=zc.sweep_loop, daemon=True).start()   # cases detectors
    threading.Thread(target=lambda: capabilities(refresh=True),   # warm probes once (§3.3)
                     daemon=True).start()                          # boot itself probes nothing
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"ZENITH/OS online -> http://{BIND}:{PORT}")
    server.serve_forever()


def _selfcheck():
    """Assertion checks for server.py's pure functions — the no-framework
    check idiom. Run: python3 server.py check  →  "server self-check OK".
    Grows with each phase (envelope, rollup, ladder, autoverify, classify)."""
    # --- _job_cmd via the claude adapter (P2 A1; zero-regression parity) ---
    def _jc(prompt, model, mode, budget, add_dir):
        return _job_cmd({"prompt": prompt, "model": model, "mode": mode,
                         "budget": budget, "add_dir": add_dir, "agent": "claude"})
    cmd = _jc("hello", "sonnet", "default", None, None)
    assert cmd == [cmd[0], "-p", "hello", "--model", "sonnet",
                   "--output-format", "json"], cmd
    assert cmd[0].endswith("claude"), cmd[0]
    assert "--permission-mode" not in cmd and "--dangerously-skip-permissions" not in cmd
    cmd = _jc("p", "opus", "acceptEdits", None, None)
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    cmd = _jc("p", "sonnet", "bypassPermissions", 5, "/tmp/x")
    assert "--dangerously-skip-permissions" in cmd
    assert cmd[cmd.index("--max-budget-usd") + 1] == "5", "H2: budget reaches CLI"
    assert cmd[cmd.index("--add-dir") + 1] == "/tmp/x"
    assert _jc("p", "sonnet", "default", None, None)[-1] == "json", "trailing flags stable"
    # --- _parse_envelope (P2) ---
    env = {"type": "result", "subtype": "success", "is_error": False,
           "num_turns": 3, "duration_ms": 1200, "result": "done",
           "session_id": "s1", "total_cost_usd": 0.12,
           "usage": {"input_tokens": 10, "output_tokens": 20,
                     "cache_creation_input_tokens": 5,
                     "cache_read_input_tokens": 7}}
    assert _parse_envelope(json.dumps(env))["total_cost_usd"] == 0.12
    assert _parse_envelope("warn line\n" + json.dumps(env))["session_id"] == "s1"
    assert _parse_envelope("total garbage") is None
    assert _parse_envelope("") is None and _parse_envelope(None) is None
    assert _parse_envelope('{"a": 1}')["a"] == 1        # stage 1 takes any whole-text dict
    assert _parse_envelope('{"a": 1}\njunk') is None    # stage 2 requires type=="result"
    # array-of-messages shape (real CLI v2.1+ output): result is the LAST element
    arr = [{"type": "system", "subtype": "init"}, {"type": "assistant", "message": {}}, env]
    assert _parse_envelope(json.dumps(arr))["total_cost_usd"] == 0.12, "array envelope"
    assert _parse_envelope(json.dumps([{"type": "system"}])) is None, "array w/o result"
    # --- job completion helpers (P2) ---
    assert _job_outcome({"status": "stopped", "rc": -15}) == "killed"
    assert _job_outcome({"status": "done", "rc": 0}) == "ok"
    assert _job_outcome({"status": "error", "rc": 2}) == "error"
    assert _tok_tuple({}) is None
    assert _tok_tuple({"usage": {"in": 1, "out": 2, "cache_r": 3,
                                 "cache_w": 4}}) == (1, 2, 3, 4)
    assert _job_actor({"loop_id": "L1"}) == "loop:L1"
    assert _job_actor({"verify_id": "vf_x"}) == "verify"
    assert _job_actor({}) == "user"
    p = _job_end_payload(
        {"model": "sonnet", "mode": "default", "rc": 0, "status": "done",
         "id": "j1", "output": ["line1", "line2"],
         "started": "2026-01-01T00:00:00+00:00",
         "ended": "2026-01-01T00:00:10+00:00", "cli_session_id": "s1",
         "result": "done"},
        {"subtype": "success", "num_turns": 2, "permission_denials": [],
         "modelUsage": {"claude-sonnet-5": {}}})
    assert p["duration_s"] == 10.0 and p["subtype"] == "success", p
    assert p["envelope_ok"] is True and p["output_tail"].endswith("line2")
    assert p["model_usage"] == {"claude-sonnet-5": {}}
    assert _job_end_payload({"id": "j2", "output": []}, None)["envelope_ok"] is False
    # --- verify ladder + tiers (P4) ---
    assert _model_tier("claude-opus-4-8") == "opus"
    assert _model_tier("opus") == "opus"
    assert _model_tier("claude-sonnet-5") == "sonnet"
    assert _model_tier("weird-model") is None and _model_tier(None) is None
    assert VERIFY_LADDER == {"haiku": "sonnet", "sonnet": "opus",
                             "opus": "fable", "fable": "fable"}
    assert VERIFY_LADDER.get(_model_tier("claude-opus-4-8")) == "fable"
    assert VERIFY_LADDER.get(_model_tier("unknown"), "opus") == "opus"
    assert VERIFY_MODE == "default"                       # probe H3's constant
    p = VERIFY_PROMPT.format(kind="job", target="t", intent="i", mech="{}",
                             material="m", extra="")
    assert "hostile reviewer" in p and '{"issues"' in p and "{extra" not in p
    # --- material caps (P4) ---
    big = "x" * 70000
    capped, trunc = _cap_text(big, 60000)
    assert trunc is True and len(capped) < 60200 and "TRUNCATED" in capped
    assert capped.startswith("x" * 100) and capped.endswith("x" * 100), "head+tail kept"
    small, trunc2 = _cap_text("ok", 60000)
    assert small == "ok" and trunc2 is False
    assert _cap_text(None, 100) == ("", False)
    # --- external statusline (lk): parsed from the user's installed script ---
    cat, home = _lk_parse_script(
        'type WidgetName =\n  | "model"\n  | "dir"\n  | "battery";\n'
        'const DEFAULT_CONFIG: Config = {\n'
        '  line1: ["model", "dir"],\n  line2: [],\n  line3: [],\n'
        '  line4: ["battery"],\n  barWidth: 15,\n};\n')
    assert cat == ["model", "dir", "battery"], cat
    assert home == {"model": "line1", "dir": "line1", "battery": "line4"}, home
    assert _lk_parse_script("nothing useful here") == ([], {})
    assert LK_LINES == ("line1", "line2", "line3", "line4")
    # --- statusline visibility + per-data-point toggles ---
    assert STATUSLINE_DEFAULT["visible"] is True
    # the embedded script is the only source of truth (scripts/statusline.py is
    # a gitignored artifact rewritten from it at boot)
    assert "if cfg.get(" + chr(34) + "visible" + chr(34) in STATUSLINE_SCRIPT_SRC, \
        "master switch must be honored by the renderer, not just stored"
    assert "isinstance(cfg.get(" in STATUSLINE_SCRIPT_SRC, \
        "an explicitly empty enabled list must render nothing, not the defaults"
    assert "DEF_VISIBLE = True" in STATUSLINE_SCRIPT_SRC
    # every catalog widget lands on a real line, so the UI can group them
    assert {w["line"] for w in STATUSLINE_CATALOG} <= {1, 2, 3, 4}
    assert all(w.get("id") and w.get("label") for w in STATUSLINE_CATALOG)
    assert set(STATUSLINE_DEFAULT["enabled"]) <= {w["id"] for w in STATUSLINE_CATALOG}
    # --- provider run context: the model has no repo, so material must ship ---
    assert _provider_num_ctx({"num_ctx": 32768}) == 32768
    assert _provider_num_ctx({}) == DEFAULT_NUM_CTX, "missing → default, never 0"
    assert _provider_num_ctx({"num_ctx": "bogus"}) == DEFAULT_NUM_CTX
    assert _provider_timeout({"timeout_s": 900}) == 900
    assert _provider_timeout({}) == DEFAULT_TIMEOUT_S == 600, "big models need it"
    assert _ctx_char_budget(32768) > _ctx_char_budget(8192) > 2000
    assert _pack_context("/x", "none", "", 9999) == ("", "", False)
    assert _pack_context("/x", "bogus", "", 9999) == ("", "", False)
    h, m, _ = _pack_context("/etc", "diff", "", 9999)      # outside claudeProjects
    assert h.endswith("unavailable") and "outside" in m, "containment holds"
    assert _provider_messages("p", "", "") == [{"role": "user", "content": "p"}]
    ms = _provider_messages("review this", "git diff", "DIFF-BODY")
    assert len(ms) == 2 and ms[0]["role"] == "system"
    assert "no file access" in ms[0]["content"]
    assert "DIFF-BODY" in ms[1]["content"] and "END MATERIAL" in ms[1]["content"]
    # --- local reviewer: one unit per call lifts the context ceiling ---
    d2 = ("git status --porcelain:\n M a.py\ngit diff:\n"      # <- preamble
          "diff --git a/a.py b/a.py\n@@\n-x\n+y\n"
          "diff --git a/sub/b.js b/sub/b.js\n@@\n-p\n+q\n")
    ch = _review_chunks(d2, 60000)
    assert len(ch) == 2 and [n for n, _ in ch] == ["a.py", "sub/b.js"], ch
    assert "+y" in ch[0][1] and "git status" not in ch[0][1]
    assert "+q" in ch[1][1] and "+y" not in ch[1][1], "chunks are disjoint"
    fm = _review_chunks("----- one.py -----\nAAA\n----- two.py -----\nBBB\n", 60000)
    assert [n for n, _ in fm] == ["one.py", "two.py"], fm
    assert _review_chunks("", 100) == [] and _review_chunks("   ", 100) == []
    assert _review_chunks("no markers here", 60000)[0][0] == "(whole material)"
    assert REVIEW_SCHEMA["properties"]["issues"]["items"]["properties"][
        "severity"]["enum"] == list(_SEVERITIES)
    assert PROVIDER_TASKS == ("chat", "review")
    # evidence grounding: a finding the model cannot quote is not a finding
    mat = "def f():\n    conn = sqlite3.connect(db)\n+   return row[0]\n"
    keep, drop = _ground_issues([
        {"severity": "crit", "claim": "c", "evidence": "conn = sqlite3.connect(db)",
         "location": "a.py:2"},                          # verbatim → kept
        {"severity": "major", "claim": "c", "evidence": "conn  =  sqlite3.connect(db)",
         "location": "a.py:2"},                          # reflowed → kept
        {"severity": "major", "claim": "c", "evidence": "return row[0]",
         "location": "a.py:3"},                          # quote drops the '+' → kept
        {"severity": "major", "claim": "c",
         "evidence": "The LIKE operator is case-insensitive.", "location": "."},
        {"severity": "minor", "claim": "c", "evidence": "", "location": "."},
        {"severity": "minor", "claim": "c", "evidence": "db", "location": "."},
    ], mat)
    assert len(keep) == 3 and drop == 3, (keep, drop)
    assert _ground_issues([], mat) == ([], 0)
    assert _norm_ws("  a\n\t b  ") == "a b"
    # --- verdict extraction + rollup (P4) ---
    good = json.dumps({"issues": [{"severity": "major", "claim": "c",
                                   "evidence": "e", "location": "l"}],
                       "summary": "s"})
    assert _extract_verdict_json(good)["summary"] == "s"
    assert _extract_verdict_json("I think that\n" + good + "\ntrailing")["summary"] == "s"
    assert _extract_verdict_json("```json\n" + good + "\n```")["summary"] == "s"
    assert _extract_verdict_json("no json here at all") is None
    iss = _clean_issues([{"severity": "BOGUS", "claim": "x" * 500,
                          "evidence": "e", "location": "l"},
                         "not-a-dict",
                         {"severity": "crit", "claim": "c", "evidence": "e",
                          "location": "l"}])
    assert len(iss) == 2, "non-dict entries dropped"
    assert iss[0]["severity"] == "info" and len(iss[0]["claim"]) == 300, "coerce + truncate"
    assert iss[1]["severity"] == "crit"
    assert _verdict_rollup(0, 0, 0, True, True) == "pass"
    assert _verdict_rollup(0, 0, 2, True, True) == "warn"
    assert _verdict_rollup(0, 1, 0, True, True) == "fail"
    assert _verdict_rollup(1, 0, 0, True, True) == "fail"
    assert _verdict_rollup(0, 0, 0, False, True) == "error", "ringer rc!=0"
    assert _verdict_rollup(0, 0, 0, True, False) == "error", "parse failed"
    # --- auto-verify decision predicate (P4, §5.7) ---
    cfg = {"enabled": True, "triggers": {"bypass_jobs": True,
                                         "acceptEdits_jobs": False,
                                         "loop_runs": True},
           "min_cost_usd": 0.10, "daily_cap_usd": 5.0}
    bj = {"id": "j", "mode": "bypassPermissions", "rc": 0, "status": "done",
          "cost_usd": 0.50}
    assert _autoverify_decision(cfg, bj, 0.0) == "run"
    assert _autoverify_decision(cfg, dict(bj, verify_id="vf_x"), 0.0) \
        == "skip:ringer", "hard recursion guard"
    assert _autoverify_decision(dict(cfg, enabled=False), bj, 0.0) == "skip:disabled"
    assert _autoverify_decision(cfg, dict(bj, status="stopped"), 0.0) \
        == "skip:outcome", "killed jobs not auto-verified"
    assert _autoverify_decision(cfg, dict(bj, mode="acceptEdits"), 0.0) \
        == "skip:no_trigger"
    assert _autoverify_decision(cfg, dict(bj, mode="default", loop_id="L"), 0.0) \
        == "run", "loop_runs trigger"
    assert _autoverify_decision(cfg, dict(bj, cost_usd=0.01), 0.0) \
        == "skip:below_min_cost"
    assert _autoverify_decision(cfg, bj, 5.0) == "skip:capped"
    # --- gate classifier (P5) ---
    g = _classify("jobs.spawn", {"mode": "bypassPermissions"})
    assert g.level == "confirm" and g.action == "jobs.spawn.bypass", "H1 holds"
    assert (g.op, g.blast, g.rev) == ("execute", "system", "irreversible")
    assert _classify("/api/jobs", {"mode": "bypassPermissions"}).level == "confirm"
    assert _classify("/api/jobs", {"mode": "acceptEdits"}).level == "notify"
    assert _classify("/api/jobs", {}).level == "auto", "default spawns stay silent"
    # --- P2 A6: gates are agent-blind (normalized modes) + reviewer pin ---
    assert _classify("/api/jobs", {"mode": "bypassPermissions",
                                   "agent": "codex"}).level == "confirm", \
        "bypass on ANY agent must hit the confirm gate"
    assert _classify("/api/jobs", {"mode": "default",
                                   "agent": "aider"}).level == "auto"
    assert REVIEWER_AGENT, "reviewer agent must be set"
    assert "FAIL" not in _probe_h8(), _probe_h8()
    assert _classify("/api/loops2/save", {"mode": "acceptEdits"}).action \
        == "loops.save.acceptEdits"
    assert _classify("/api/loops2/save", {"mode": "default"}).action == "config.write"
    assert _classify("/api/loops2/save", {"mode": "default"}).level == "auto"
    assert _classify("/api/swarms/launch", {}).level == "notify"
    assert _classify("/api/wargames/launch", {}).level == "notify"
    assert _classify("/api/verify", {}).level == "notify"
    assert _classify("/api/job/stop", {}).level == "auto"
    assert _classify("/api/scorecard", {}).level == "notify"
    assert _classify("/api/autoverify", {}).level == "notify"
    assert _classify("/api/unknown/thing", {}).level == "notify", "formula fallback"
    assert _classify("/api/watchers/enable", {}).level == "auto", \
        "pause/resume stays silent (pinned like config.write)"
    assert _classify("/api/watchers/save", {}).level == "notify"
    assert _classify("/api/watchers/delete", {}).level == "confirm", \
        "watcher delete is irreversible -> modal"
    assert _classify("/api/watchers/fetchnow", {}).level == "auto", \
        "fetch-now dry-run is read-only -> silent"
    assert _WATCH_NAME_RE.match("node-price") and not _WATCH_NAME_RE.match("../x") \
        and not _WATCH_NAME_RE.match("") and not _WATCH_NAME_RE.match("a/b"), \
        "watcher names must be path-safe"
    assert _gate_level("read", "local", "reversible", "x") == "auto"
    assert _gate_level("write", "project", "soft", "x") == "notify"
    assert _gate_level("execute", "system", "soft", "x") == "confirm"
    assert _gate_level("draft", "local", "irreversible", "x") == "confirm"
    # --- A/B threading (P3.1) ---
    assert _job_actor({"ab_id": "ab_x"}) == "ab:ab_x"
    assert _job_actor({"ab_id": "ab_x", "verify_id": "vf_1"}) == "verify", \
        "ringer identity outranks cohort identity"
    assert _job_actor({"ab_id": "ab_x", "loop_id": "L1"}) == "loop:L1"
    sp = _job_spawn_payload({"model": "sonnet", "mode": "default",
                             "ab_id": "ab_9"}, None, None, [], None)
    assert sp["ab_id"] == "ab_9"
    ep = _job_end_payload({"id": "j3", "output": [], "ab_id": "ab_9"}, None)
    assert ep["ab_id"] == "ab_9"
    assert _job_end_payload({"id": "j4", "output": []}, None)["ab_id"] is None
    # --- A/B cohort assembly, pure shaping (P3.1) ---
    meta = {"agent": "codex", "model": "gpt-5.5", "job_id": "j1"}
    a = _ab_arm_view(meta, {"status": "done", "cost_usd": 0.5,
                            "usage": {"in": 1, "out": 2},
                            "started": "2026-01-01T00:00:00+00:00",
                            "ended": "2026-01-01T00:00:30+00:00",
                            "result": "r"}, None)
    assert a["agent"] == "codex" and a["model"] == "gpt-5.5"
    assert a["duration_s"] == 30.0 and a["cost_usd"] == 0.5 and a["result"] == "r"
    ev = {"outcome": "ok", "cost_usd": None, "tok_in": 5, "tok_out": 6,
          "tok_cr": None, "tok_cw": None,
          "data": {"status": "done", "duration_s": 12.5,
                   "started": "2026-01-01T00:00:00+00:00", "result": "rr"}}
    b = _ab_arm_view(meta, None, ev)
    assert b["cost_usd"] is None, "NULL cost stays None — never 0 (D9)"
    assert b["usage"]["in"] == 5 and b["duration_s"] == 12.5 and b["result"] == "rr"
    c = _ab_arm_view(meta, None, None)
    assert c["status"] == "orphaned" and c["cost_usd"] is None and c["verdict"] is None
    assert _ab_status("verify", False) == "verify"
    assert _ab_status(None, True) == "done"
    assert _ab_status(None, False) == "orphaned"
    # --- A/B launch gate (P3.1): classified as a jobs.spawn on the shared mode ---
    assert _classify("/api/ab/launch", {"mode": "bypassPermissions"}).level \
        == "confirm", "a bypass A/B must hit the confirm gate"
    assert _classify("/api/ab/launch", {"mode": "acceptEdits"}).level == "notify"
    assert _classify("/api/ab/launch", {}).level == "auto"
    gab = _classify("/api/ab/launch", {"mode": "default"})
    assert "2 arms" in _gate_summary(gab, {"mode": "default",
                                           "arms": [{}, {}]})
    # --- A/B judge parse + prompt (P3.2) ---
    gj = json.dumps({"winner": 2,
                     "scores": [{"arm": 1, "score": 6, "note": "n1"},
                                {"arm": 2, "score": 9, "note": "n2"}],
                     "rationale": "arm 2 better"})
    j = _parse_ab_judge(gj, 2)
    assert j["winner"] == 2 and j["parse_ok"] is True and len(j["scores"]) == 2
    assert _parse_ab_judge("I think\n" + gj + "\ntrailing", 2)["winner"] == 2
    assert _parse_ab_judge("```json\n" + gj + "\n```", 2)["winner"] == 2
    assert _parse_ab_judge("no json here", 2) == {"winner": None, "scores": [],
                                                  "rationale": "",
                                                  "parse_ok": False}
    assert _parse_ab_judge(json.dumps({"winner": 7}), 2)["winner"] is None, \
        "out-of-range winner rejected"
    assert _parse_ab_judge(json.dumps(
        {"winner": 1, "scores": [{"arm": 9, "score": 5}, "junk",
                                 {"arm": 1, "score": 99, "note": "x"},
                                 {"arm": 1, "score": 3}]}), 2
        )["scores"] == [{"arm": 1, "score": 10, "note": "x"}], \
        "bad arms dropped, score clamped 0..10, one score per arm"
    # the refactored extractor keeps the original verdict contract
    gv = json.dumps({"issues": [], "summary": "s2"})
    assert _extract_verdict_json("noise " + gv)["summary"] == "s2"
    jp = _ab_judge_prompt("do X", [
        {"agent": "claude", "model": "sonnet", "cost_usd": 0.1,
         "usage": {"in": 1, "out": 2}, "duration_s": 3.0, "status": "done",
         "result": "r1", "verdict": {"verdict": "pass", "crit": 0, "major": 0,
                                     "minor": 0, "summary": "fine"}},
        {"agent": "codex", "model": "gpt-5.5", "cost_usd": None, "usage": None,
         "duration_s": None, "status": "error", "result": "",
         "verdict": None}])
    assert "ARM 1" in jp and "ARM 2" in jp and '{"winner"' in jp
    assert "unavailable" in jp and "do X" in jp and "{arms" not in jp
    # --- portability config: 3-layer resolver env > config > default (Phase 1) ---
    assert _resolve(None, "cfgval", "dflt") == "cfgval", "no env key -> config layer"
    assert _resolve("ZENITH_SELFTEST_X", "cfgval", "dflt") == "cfgval", "unset env -> config"
    assert _resolve("ZENITH_SELFTEST_X", None, "dflt") == "dflt", "unset env + no config -> default"
    os.environ["ZENITH_SELFTEST_X"] = "envval"
    try:
        assert _resolve("ZENITH_SELFTEST_X", "cfgval", "dflt") == "envval", "env wins over config"
        os.environ["ZENITH_SELFTEST_X"] = ""
        assert _resolve("ZENITH_SELFTEST_X", "cfgval", "dflt") == "", \
            "set-but-empty env still wins (os.environ.get parity)"
    finally:
        del os.environ["ZENITH_SELFTEST_X"]
    assert _cfg_get({"a": {"b": "v"}}, "a.b") == "v"
    assert _cfg_get({"a": {}}, "a.b", "d") == "d", "missing hop -> default"
    assert _cfg_get({"a": {"b": ""}}, "a.b", "d") == "", "present empty string is a value"
    assert _deep_merge({"x": {"p": 1, "q": 2}}, {"x": {"q": 9}}) \
        == {"x": {"p": 1, "q": 9}}, "deep-merge recurses, does not clobber siblings"
    assert config_redacted({"integrations": {"nexusmind_api": {"token": "sekret"}}}) \
        ["integrations"]["nexusmind_api"] == {"token": "", "token_set": True}, \
        "token redacted + token_set flagged"
    # --- capabilities: effective active per mode (Phase 2) ---
    assert _effective_active("off", True) is False and _effective_active("off", None) is False, \
        "off -> inactive regardless of detected"
    assert _effective_active("on", False) is True and _effective_active("on", None) is True, \
        "on -> active regardless of detected"
    assert _effective_active("auto", True) is True, "auto active iff detected"
    assert _effective_active("auto", False) is False and _effective_active("auto", None) is False, \
        "auto inactive when undetected / still probing"
    # off short-circuits at the API chokepoints BEFORE any probe/subprocess/socket.
    _saved = CONFIG
    try:
        config_apply({"integrations": {"nexusmind_api": {"mode": "off"},
                                       "voice": {"mode": "off"}}})
        assert _int_off("nexusmind_api") and _nm_reachable() is False, \
            "nexusmind_api off -> _nm_reachable False without opening a socket"
        assert nm_api("/api/watch/kinds").get("error") == "integration disabled", \
            "nexusmind_api off -> nm_api short-circuits (no socket)"
        assert nm_memories()["available"] is False and nm_meta()["available"] is False \
            and nm_meta()["namespaces"] == [] and nm_meta()["tags"] == [] \
            and nm_source()["backend"] == "none" and "nodes" not in nm_graph() \
            and "edges" not in nm_graph() \
            and nm_timeline()["items"] == [] and nm_detail("k")["available"] is False \
            and nm_capture("t", "", [], "x")["queued"] is False, \
            "off -> every memory route degrades to the unavailable shape, no raise " \
            "(nm_graph omits nodes/edges entirely rather than asserting an empty graph)"
        # The NexusMind terminal's own routes sit behind the SAME switch — including the
        # unified quick-capture, which used to be a second ungated client in zenith_cases.
        assert nm_ask("q")["available"] is False and nm_ingest("t")["available"] is False \
            and nm_correct("t")["available"] is False and nm_stats()["available"] is False \
            and nm_memories(tag="idea")["available"] is False, \
            "off -> ask/ingest/correct/stats/tag-list degrade to unavailable, no socket"
        _cap = nm_capture_text("hello")
        assert _cap["ok"] is False and isinstance(_cap.get("detail"), str) and _cap["detail"], \
            "off -> /api/capture answers ok:false with a displayable detail (the command " \
            "bar queues on ok:false and shows detail, so it must never be empty/None)"
        assert whisper_transcribe(b"", "x.webm").get("engine") == "browser", \
            "voice off -> whisper_transcribe returns browser-fallback shape"
        assert _int_mode("fleet") == "auto", "unset mode defaults to auto"
    finally:
        config_apply(_saved)                 # restore defaults for any later checks
    # --- prompt history: incremental append-only scan ---
    import tempfile
    _line = lambda t, ts: json.dumps(                                     # noqa: E731
        {"type": "user", "timestamp": ts, "message": {"content": t}}) + "\n"
    with tempfile.TemporaryDirectory() as _td:
        _tp = Path(_td) / "t.jsonl"
        _tp.write_text(_line("first prompt", "2026-01-01T00:00:00Z")
                       + _line("<command-name>/model</command-name>", "2026-01-01T00:01:00Z")
                       + _line("This session is being continued from a previous conversation. "
                               "Summary: …", "2026-01-01T00:02:00Z"))
        _p = _scan_prompts(_tp)
        assert [x["text"] for x in _p] == ["first prompt"], \
            "synthetic/tool-shaped user lines are not prompts: %r" % (_p,)
        _off = _prompts_cache[str(_tp)]["off"]
        with open(_tp, "a") as _f:                       # append, as a live session does
            _f.write(_line("second prompt", "2026-01-01T00:03:00Z"))
        _p = _scan_prompts(_tp)
        assert [x["text"] for x in _p] == ["first prompt", "second prompt"], \
            "appended prompt not picked up: %r" % (_p,)
        assert _prompts_cache[str(_tp)]["off"] > _off, "scan offset did not advance"
        _tp.write_text(_line("replaced", "2026-01-02T00:00:00Z"))   # shrunk -> full rescan
        assert [x["text"] for x in _scan_prompts(_tp)] == ["replaced"], \
            "a shrunk (replaced) transcript must rescan from 0"
        assert prompts_payload(path=str(_tp))["detail"] == "not a session transcript", \
            "prompts_payload must reject a path outside the transcript roots"
        # mid-turn (queued) messages are prompts too, and must not double up when the
        # queued text later replays as a real user line
        _q = lambda t, ts: json.dumps(                                    # noqa: E731
            {"type": "queue-operation", "operation": "enqueue",
             "timestamp": ts, "content": t}) + "\n"
        _tq = Path(_td) / "q.jsonl"
        _tq.write_text(_q("steer mid-turn", "2026-01-01T00:00:00Z")
                       + _q("also this", "2026-01-01T00:01:00Z")
                       + _line("also this", "2026-01-01T00:02:00Z"))
        _p = _scan_prompts(_tq)
        assert [x["text"] for x in _p] == ["steer mid-turn", "also this"], \
            "queued prompt kept, replayed duplicate collapsed: %r" % (_p,)
        assert _p[0].get("queued") and not _p[1].get("queued"), \
            "the surviving replay is the real line, not the queued placeholder"
        # the cache evicts instead of growing without bound
        for _i in range(PROMPTS_CACHE_MAX + 5):
            _e = Path(_td) / ("e%d.jsonl" % _i)
            _e.write_text(_line("p%d" % _i, "2026-01-01T00:00:00Z"))
            _scan_prompts(_e)
        assert len(_prompts_cache) <= PROMPTS_CACHE_MAX, \
            "prompt cache must evict, got %d entries" % len(_prompts_cache)
    # --- live claude processes: parsing + kill guardrails ---
    assert _etime_seconds("05:25") == 325 and _etime_seconds("10:45:57") == 38757 \
        and _etime_seconds("14-03:35:22") == 1222522, "ps etime forms"
    assert _etime_seconds("garbage") is None, "unparseable etime -> None, not a crash"
    assert _is_claude_cmd("/Users/x/.local/bin/claude --resume abc"), "plain claude"
    assert _is_claude_cmd("claude /login") and _is_claude_cmd("claude bg-spare --x"), \
        "bare-name invocations"
    assert _is_claude_cmd("/Users/x/.local/share/claude/versions/2.1.220 --session-id a"), \
        "the versioned node binary is a claude process"
    assert not _is_claude_cmd("/bin/zsh -c 'ps | grep claude'"), \
        "a shell that merely MENTIONS claude must not be listed as one"
    assert not _is_claude_cmd("node /p/node_modules/.bin/vite") and not _is_claude_cmd(""), \
        "unrelated processes stay out"
    _fork = ("/x/ClaudeCode.app/Contents/MacOS/claude --session-id "
             "fce0379a-bdab-4045-bb57-e55cc967960c --fork-session --resume "
             "/h/.claude/projects/-p/cfb892cf-9e13-4306-b487-5f13cc932ab3.jsonl")
    assert _session_id_from_cmd(_fork) == "fce0379a-bdab-4045-bb57-e55cc967960c", \
        "--session-id wins: a forked session writes the NEW id, not the resumed one"
    assert _session_id_from_cmd("claude --resume ae53765f-3338-464f-8701-82ee64b58a85") \
        == "ae53765f-3338-464f-8701-82ee64b58a85", "--resume <uuid>"
    assert _session_id_from_cmd("claude --resume /a/b/8e3497c1-2920-4415-b0bb-fdb502ef6dca.jsonl") \
        == "8e3497c1-2920-4415-b0bb-fdb502ef6dca", "--resume <transcript path>"
    assert _session_id_from_cmd("claude /login") is None, "no session -> None"
    assert _classify_claude("x", 99, 99) == "zenith", "child of this server"
    assert _classify_claude("/x/ClaudeCode.app/y claude", 1, 99) == "app"
    assert _classify_claude("claude bg-spare --x", 1, 99) == "daemon"
    assert _classify_claude("/x/bin/claude --resume a", 1, 99) == "cli"
    assert kill_claude_proc(os.getpid())["error"].startswith("refusing"), \
        "must never signal the ZENITH server itself"
    assert kill_claude_proc("not-a-pid")["error"] == "bad pid"
    assert kill_claude_proc(2 ** 30)["error"] == "not a live claude process", \
        "only pids claude_processes() reports are eligible"
    # --- model tables: defaults/models.json, data/ override, garbage tolerance ---
    _shipped = za.load_defaults("models", {})
    assert isinstance(_shipped, dict) and set(_shipped) >= set(MODELS_FALLBACK), \
        "defaults/models.json must ship every section the fallback defines"
    assert MODEL_MAP == _MODELS["aliases"] and EFFORT_TOKENS == _MODELS["effort_tokens"]
    assert list(MODEL_MAP)[:4] == ["haiku", "sonnet", "opus", "fable"], \
        "_model_tier matches by substring in order — the alias order is load-bearing"
    _saved_models_file = MODELS_FILE
    with tempfile.TemporaryDirectory() as _td:
        globals()["MODELS_FILE"] = Path(_td) / "models.json"
        MODELS_FILE.write_text(json.dumps(
            {"aliases": {"sonnet": "local-sonnet", "bogus": 5},
             "context_windows": {"local-x": 4096, "bad": "nope"},
             "context_window_fallback": 12345}))
        _m = _models_defaults()
        assert _m["aliases"]["sonnet"] == "local-sonnet", "data/ wins over defaults/"
        assert "bogus" not in _m["aliases"], "a non-string alias is dropped, not fatal"
        assert _m["aliases"]["opus"] == MODELS_FALLBACK["aliases"]["opus"], \
            "an untouched key keeps the shipped value"
        assert _m["context_windows"]["local-x"] == 4096 and "bad" not in _m["context_windows"]
        assert _m["context_window_fallback"] == 12345
        MODELS_FILE.write_text("{ not json at all")
        assert _models_defaults()["aliases"] == _MODELS["aliases"], \
            "a corrupt override falls back to the shipped tables"
    globals()["MODELS_FILE"] = _saved_models_file
    assert _models_defaults()["verify_ladder"] == VERIFY_LADDER
    # --- shipped providers: loopback only, nothing claimed to be running ---
    _pd = _providers_defaults()
    for _p in _pd["seed"]:
        _h = (urlparse(str(_p.get("base_url") or "")).hostname or "").lower()
        assert _h in ("127.0.0.1", "::1", "localhost"), \
            "a shipped provider may never name a non-loopback host: %r" % (_p,)
        assert _p.get("enabled") is False, \
            "a shipped provider must not assert it is running: %r" % (_p,)
    assert {k.get("id") for k in _pd["kinds"]} >= {"ollama", "openai"}
    assert all(k.get("detect") in DETECTORS for k in _pd["kinds"]), \
        "every shipped kind names a real probe"
    # --- providers three-way merge (§9): upgrades reach existing installs ---
    _sd = [{"id": "local-ollama", "type": "ollama", "enabled": False,
            "base_url": "http://127.0.0.1:11434", "num_ctx": 8192}]
    assert _providers_merge(_sd, [], []) == _sd, "empty install takes the seed"
    _newer = [dict(_sd[0], num_ctx=16384, timeout_s=600)]
    assert _providers_merge(_newer, _sd, _sd)[0]["num_ctx"] == 16384, \
        "an untouched field tracks the shipped default (the whole point of §9)"
    _mine = _providers_merge(_newer, [dict(_sd[0], num_ctx=32768)], _sd)[0]
    assert _mine["num_ctx"] == 32768, "a customised field is never clobbered"
    assert _mine["timeout_s"] == 600, "a brand-new field is still adopted"
    assert _providers_merge(_sd, [], _sd) == [], "a deleted seed id stays deleted"
    _live = [{"id": "275dc711", "name": "Ollama (this Mac)", "type": "ollama",
              "base_url": "http://127.0.0.1:11434", "enabled": True}]
    assert _providers_merge(_sd, _live, []) == _live, \
        "same endpoint under another id -> the user's row wins, no duplicate"
    assert _providers_merge(_sd, "garbage", []) == _sd
    # --- detection registry: time-bounded, never raises, never invents a model ---
    assert set(DETECTORS) == {"ollama_tags", "openai_models", "binary_version"}
    _dead = "http://127.0.0.1:9"                    # discard port: refused instantly
    assert detect_ollama_tags(_dead, timeout=0.3) == []
    assert detect_openai_models(_dead, timeout=0.3) == []
    assert detect_ollama_tags("not a url", timeout=0.3) == [] \
        and detect_openai_models("", timeout=0.3) == []
    assert detect_ollama_tags(None, timeout=0.3) == []
    assert detect_binary_version("zenith-definitely-not-a-binary") == (False, "")
    assert detect_binary_version("")[0] is False and detect_binary_version(None)[0] is False
    _present, _ver = detect_binary_version(sys.executable)
    assert _present is True and "ython" in _ver, (_present, _ver)
    _bin_to = DETECT_BIN_TIMEOUT
    globals()["DETECT_BIN_TIMEOUT"] = 0.4    # a check must not wait on a slow CLI
    _t0 = time.time()
    _det = detect_all(refresh=True)
    assert _det["ok"] and (time.time() - _t0) < DETECT_DEADLINE + 3, \
        "a sweep is time-bounded even with nothing (or everything) installed"
    assert isinstance(_det["agents"], list) and isinstance(_det["endpoints"], list)
    assert all(set(e) >= {"base_url", "alive", "models"} for e in _det["endpoints"])
    assert all(e["alive"] == bool(e["models"]) for e in _det["endpoints"]), \
        "an endpoint is only 'alive' when it named real models"
    assert set(_det["models"]) >= {"ollama_tags", "openai_models"}
    assert all(str(_h) for _h in _det["models"])
    assert detect_all()["cached"] is True, "answers cache ~30s"
    assert detected_models() == _det["models"]
    assert isinstance(agent_models({"id": "claude", "models": {"kind": "static",
                                                              "list": ["m1"]}}), list)
    # --- export / import (§5) ---
    _b = config_export()
    assert set(_b) >= {"version", "exported_at", "agents", "providers", "models",
                       "config", "statusline"}
    assert _b["version"] == CONFIG_BUNDLE_VERSION and _b["secrets_included"] is False
    assert all(p.get("api_key") == "" and "api_key_set" in p for p in _b["providers"]), \
        "secrets are redacted unless explicitly requested"
    assert config_export(include_secrets=True)["secrets_included"] is True
    assert _bundle_check(_b) == ""
    assert _bundle_check([]) and _bundle_check({}) and _bundle_check({"version": 1})
    assert _bundle_check({"version": 99, "config": {}}).startswith("bundle version")
    assert _bundle_check({"version": 1, "agents": {}}) == "agents must be a list"
    assert _bundle_check({"version": 1, "config": []}) == "config must be an object"
    assert _bundle_check({"version": 1, "providers": [{"name": "x"}]}) \
        == "every provider entry needs an id"
    _before = PROVIDERS_FILE.read_bytes() if PROVIDERS_FILE.exists() else None
    _rej = config_import({"version": 1, "providers": [{"name": "no id"}]})
    assert _rej["ok"] is False and "id" in _rej["error"]
    assert (PROVIDERS_FILE.read_bytes() if PROVIDERS_FILE.exists() else None) \
        == _before, "a malformed bundle must not touch a single file"
    with tempfile.TemporaryDirectory() as _td:      # apply path, on a temp target
        globals()["MODELS_FILE"] = Path(_td) / "models.json"
        MODELS_FILE.write_text(json.dumps({"aliases": {"sonnet": "before-import"}}))
        _imp = config_import({"version": 1,
                              "models": {"aliases": {"sonnet": "after-import"}}})
        assert _imp["ok"] and _imp["applied"] == ["models"], _imp
        assert Path(_imp["backups"]["models"]).exists(), "target backed up first"
        assert MODEL_MAP["sonnet"] == "after-import", "tables re-resolve after import"
    globals()["MODELS_FILE"] = _saved_models_file
    globals()["DETECT_BIN_TIMEOUT"] = _bin_to
    globals()["_MODELS"] = _models_defaults()       # undo the temp-file rebinding
    globals()["MODEL_MAP"] = _MODELS["aliases"]
    globals()["EFFORT_TOKENS"] = _MODELS["effort_tokens"]
    assert MODEL_MAP["sonnet"] == MODELS_FALLBACK["aliases"]["sonnet"]
    # --- _term_assignments: no two terminals may name the same transcript -------
    # This is a regression guard for the exact shape that broke twice: several
    # claude terminals in ONE project, one of them holding a --resume id for a
    # session another window is actually running. That one falls out of the
    # exact-match step into the same fallback pool as its id-less neighbours, and
    # every previous per-terminal or per-branch scheme then handed two of them the
    # same file. Asserting the global invariant is the only thing that catches it.
    with tempfile.TemporaryDirectory() as _td:
        _cwd = "/x/proj"
        _files = []
        # dddd is a FORK of aaaa: its first session_id names its ancestor, which is
        # how a resumed session is detectable at all (see _transcript_ancestor).
        # Order matters — dddd is written BEFORE cccc so it is deliberately NOT the
        # newest file. Otherwise a terminal that failed to follow the fork would be
        # handed dddd by the recency fallback anyway, and the assertion below would
        # pass while the logic it guards was gone (verified: it did).
        # Files sit in a directory NAMED encode_path(cwd), exactly as they do under
        # TRANSCRIPTS_ROOT — _forward_fork keys by the parent directory's name, so a
        # fixture that parked them in a bare temp dir would silently never find a
        # fork and would exercise the fallback instead of the code under test.
        _dir = Path(_td) / encode_path(_cwd)
        _dir.mkdir()
        for _i, (_stem, _anc) in enumerate((("aaaa", "aaaa"), ("bbbb", "bbbb"),
                                            ("dddd", "aaaa"), ("cccc", "cccc"))):
            _f = _dir / (_stem + ".jsonl")
            _f.write_text(json.dumps({"type": "user", "session_id": _anc}) + "\n")
            os.utime(_f, (1000 + _i, 1000 + _i))          # cccc is the newest
            _files.append(_f)
        _scan = ({f.stem: f for f in _files}, {encode_path(_cwd): _files})
        _iso = "2026-01-01T00:00:0"
        _snap = [
            ("t_run", {"cwd": _cwd, "mode": "claude", "created": _iso + "0",
                       "session_id": "aaaa"}),            # really running aaaa
            ("t_fork", {"cwd": _cwd, "mode": "claude", "created": _iso + "3",
                        "resume_id": "aaaa"}),            # resumed; aaaa forked
            ("t_plain", {"cwd": _cwd, "mode": "claude", "created": _iso + "5",
                         "resume_id": "bbbb"}),           # resumed; never forked
            ("t_new", {"cwd": _cwd, "mode": "claude", "created": _iso + "2"}),
            ("t_old", {"cwd": _cwd, "mode": "claude", "created": _iso + "1"}),
            ("t_starve", {"cwd": _cwd, "mode": "claude", "created": _iso + "0"}),
            ("t_sh", {"cwd": _cwd, "mode": "shell", "created": _iso + "4"}),
            # reports a session whose transcript does not exist yet (seconds old)
            ("t_pending", {"cwd": _cwd, "mode": "claude", "created": _iso + "6",
                           "session_id": "eeee"}),
        ]
        _a = _term_assignments(_snap, _scan)
        assert len(set(_a.values())) == len(_a), ("two terminals share a "
                                                  "transcript: %r" % (_a,))
        assert "t_sh" not in _a, "a plain shell drives no transcript"
        # Asserted early and by name: knowing which session a window runs but not yet
        # having its file is NOT a licence to hand it somebody else's transcript. If
        # this regresses, t_pending silently displaces a later terminal's claim and the
        # first symptom downstream is a bare KeyError rather than the real reason.
        assert "t_pending" not in _a, ("a terminal that reported its session id must "
                                       "never be given a fallback transcript: %r" % (_a,))
        assert _a["t_run"].stem == "aaaa", "reported id outranks a resume claim"
        # The resume case that showed a 15-hour-old summary while the window was
        # busy: aaaa is the FROZEN ancestor, dddd is where the session is actually
        # being written, so the binding has to follow the fork forward.
        assert _a["t_fork"].stem == "dddd", ("a resumed window must follow its "
                                             "fork forward, not sit on the "
                                             "frozen ancestor: %r" % (_a,))
        # ...but only when a fork exists. Resumed-and-not-yet-written must still
        # resolve to the ancestor rather than to None or to somebody else's file.
        assert _a["t_plain"].stem == "bbbb", ("an unforked resume target must be "
                                              "kept: %r" % (_a,))
        assert _a["t_new"].stem == "cccc", _a
        # nothing left to hand out: None beats another session's transcript
        assert "t_old" not in _a and "t_starve" not in _a, "starved must get none"
    print("server self-check OK")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        _selfcheck()
    else:
        main()
