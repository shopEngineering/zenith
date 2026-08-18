"""Cases — detectors over counted session facts.

Slice 1 of the Cases + Command Bar design
(nexuscore/docs/specs/2026-08-14-cases-and-command-bar-design.md).

Three rules this module exists to keep:
  1. No detector may call a model. Every predicate is arithmetic over counted
     fields, so the board stays correct when the GPU is busy.
  2. The config is re-read on EVERY pass. Session Watch shipped with four knobs
     frozen at import time and they were silently restart-only; this does not
     repeat that.
  3. Session Watch's sqlite is opened read-only. Only the watcher writes it.
"""

import copy
import json
import os
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import zenith_store as zs

HOME = Path(os.path.expanduser("~"))
CLAUDE_DIR = HOME / ".claude"
CASES_CONFIG = CLAUDE_DIR / "zenith-cases" / "config.json"
SESSION_WATCH_DB = CLAUDE_DIR / "session-watch" / "state.db"
PROJECTS_ROOT = HOME / "claudeProjects"

SAMPLE_RETENTION_HOURS = 24

DEFAULTS = {
    "enabled": True,
    "poll_seconds": 60,
    # C1: session_watch.py's own discovery window (LIVE_WINDOW_MIN,
    # session_watch.py:43,162) defaults to 60 minutes and bounds which
    # transcripts the watcher bothers to glob/stat each pass. Matching that
    # number here means the sweep never treats a session the watcher has
    # stopped even looking at as a live signal. Comfortably above every
    # detector's own threshold (waiting escalate 30m, burn window 20m,
    # stall 10m) so a genuine near-boundary case still fires first. See
    # live_sessions() for what actually freezes the `mtime` column once a
    # session goes quiet — it is a DIFFERENT mechanism than this window.
    "live_window_minutes": 60,
    # C1: a session excluded by live_window_minutes is not necessarily
    # gone — it may simply be quiet (see sweep_once(), which protects a
    # quiet session's existing cases from auto-resolve). Only once a
    # session has been quiet longer than THIS does the sweep treat it as
    # certainly over and resolve its cases with resolution='stale' (not
    # 'auto' — nothing was actually re-evaluated). Hours, not minutes, and
    # deliberately far beyond any plausible think-time: a long lunch, a
    # meeting, or an overnight break must never read as "over". Matches
    # SAMPLE_RETENTION_HOURS below — past that point the case's own
    # evidence trail has already been pruned, so there is nothing left to
    # keep re-evaluating even if the sweep wanted to.
    "session_gone_hours": 24,
    # C2: how long a dismissed case keeps its fingerprint after being
    # dismissed, so the very next sweep pass does not reopen it as a new
    # case and re-notify. Bounded, not permanent — unlike a mute (explicit
    # `until`, suppresses creation AND notification) a dismiss says "not
    # right now", so a condition still true after the cooldown resurfaces.
    "dismiss_cooldown_minutes": 60,
    "detectors": {
        "waiting_on_you": {"enabled": True, "after_minutes": 10,
                           "escalate_minutes": 30, "severity": "high"},
        "burn_no_progress": {"enabled": True, "window_minutes": 20,
                             "min_output_tokens": 50000, "severity": "high"},
        "silent_stall": {"enabled": True, "after_minutes": 10, "severity": "medium"},
        # D1: severity 'low' — DELIBERATE, and below the default notify floor
        # ('medium'), so this detector stays a visible signal on the tab but
        # never pushes to the phone. It matches 72% of live sessions and both
        # of its inputs (subagent count, models seen) are monotonic, so it can
        # never auto-resolve: a case it opens stays open until the session
        # goes stale. The PREDICATE and its thresholds are deliberately
        # unchanged — the calibration question (what fan-out is actually worth
        # attention) is still open and wants real data, not a guess. Severity
        # is the only lever moved here.
        "model_fanout": {"enabled": True, "watch_models": ["fable", "opus"],
                         "min_subagents": 4, "severity": "low"},
    },
    "projects": {},
    "mutes": [],
    "notify": {"enabled": True, "min_severity": "medium",
               "quiet_hours": ["23:00", "07:00"]},
}

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}

_last_good = None

# I4: this module used to have no logging at all while every failure path
# swallowed its exception, so a dead sweep presented as a normal board of
# quietly-ageing cases — nothing anywhere said the feature had stopped. These
# print to stdout in server.py's existing convention (server.py:358, :5298),
# which lands in whatever ZENITH's stdout is redirected to.
#
# Rate-limited per key because the sweep runs every 60s: an unrecoverable
# fault (bad detector, unreadable watcher db) would otherwise write 1440
# identical lines a day. One line per key per LOG_EVERY_SECONDS is enough to
# find it and cheap enough to leave on forever.
LOG_EVERY_SECONDS = 900
_log_last = {}


def _log(key, msg):
    now = time.time()
    if now - _log_last.get(key, 0.0) < LOG_EVERY_SECONDS:
        return False
    _log_last[key] = now
    print("ZENITH/OS: cases: " + msg, flush=True)
    return True


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    """Defaults merged with the file, re-read EVERY call. A malformed file keeps
    the last good config rather than silently failing into 'no detection'."""
    global _last_good
    try:
        raw = json.loads(Path(CASES_CONFIG).read_text())
        if not isinstance(raw, dict):
            raise ValueError("config root must be an object")
        cfg = _deep_merge(DEFAULTS, raw)
        _last_good = copy.deepcopy(cfg)
        return copy.deepcopy(cfg)
    except (OSError, ValueError):
        if _last_good is not None:
            return copy.deepcopy(_last_good)
        return copy.deepcopy(DEFAULTS)


def save_config(patch):
    """Merge PATCH over the current config and persist — NOT a replace.

    The Settings UI only exposes a subset of keys: it has no controls for
    live_window_minutes, session_gone_hours, or the projects/mutes blocks
    Tasks 1-4 added. If a partial POST from that UI overwrote the file
    outright, every mute the user set and the liveness window that keeps
    the board from filling with zombie cases would silently vanish on the
    next Settings save. So this reads the current full config (defaults
    already filled in by load_config()), deep-merges PATCH over it with the
    same _deep_merge used to layer the file over DEFAULTS, and writes the
    result — any key PATCH does not mention survives untouched.

    Merge semantics: dicts (detectors, notify, projects, and per-project
    override blocks) merge key-by-key, recursively — posting one detector's
    tweaked after_minutes must not blank out that detector's severity, or
    any other detector's block. Scalars and LISTS both replace wholesale
    when PATCH names the key, and are left alone when it doesn't. Lists
    (mutes, watch_models, quiet_hours) deliberately do NOT merge
    element-wise or append: there is no natural key to dedup mutes by, and
    silently interleaving or duplicating entries on every save would be
    worse than requiring a full read-modify-write. The one caller that adds
    a single mute (/api/cases/act's mute branch) already does exactly
    that — load_config(), mutate the whole list, save_config() the whole
    cfg back — so replace-on-present is the only semantics a list-touching
    caller could rely on anyway.

    A non-dict PATCH (list/str/number/bool — a malformed POST body) is a
    silent no-op, not an error: matches the convention server.py's own
    POST /api/config route uses (`patch = body if isinstance(body, dict)
    else {}`), so the file stays consistent about how a malformed config
    body is handled everywhere. Without this guard, `(patch or {}).items()`
    inside _deep_merge raises AttributeError on any truthy non-dict body
    (e.g. a JSON list) — do_POST's except clause does not catch
    AttributeError, so that reached the client as a connection reset
    instead of a clean response.

    Atomic + 0600. Atomic because the sweep reads this file every pass and
    must never catch a half-written one."""
    patch = patch if isinstance(patch, dict) else {}
    merged = _deep_merge(load_config(), patch)
    path = Path(CASES_CONFIG)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with os.fdopen(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                           0o600), "w") as f:
        f.write(json.dumps(merged, indent=2) + "\n")
    os.replace(tmp, path)


# Quick-capture used to live here as a SECOND NexusMind client: its own base+token
# resolved from data/nexusmind.json, its own urllib call, and no `nexusmind_api` gate —
# while a comment two files away claimed "one door". It now lives in server.py as
# nm_capture_text(), which goes through nm_api() like every other memory route: one
# client, one token source, one switch. The /api/capture response contract
# ({"ok","key","detail"}) is unchanged, so the command bar's offline queue still works.


def detector_cfg(cfg, name, project=""):
    """Global detector block with any per-project override merged over it."""
    base = dict(cfg.get("detectors", {}).get(name, {}))
    over = (cfg.get("projects", {}).get(project or "", {}) or {}).get(name, {})
    return copy.deepcopy(_deep_merge(base, over))


def _parse_iso(s):
    """Tolerant ISO-8601 parse. This runs on Python 3.9, whose
    datetime.fromisoformat cannot handle a trailing 'Z' (that landed in 3.11)
    — and Claude transcript timestamps use exactly that form, so stripping it
    is load-bearing, not defensive decoration."""
    if not s:
        return None
    t = str(s).strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(t)
    except (ValueError, TypeError):
        return None


def is_muted(cfg, detector, address, now=None):
    now = now or datetime.now(timezone.utc)
    for m in cfg.get("mutes", []) or []:
        if m.get("detector") != detector or m.get("address") != address:
            continue
        until = _parse_iso(m.get("until"))
        if until is None or until > now:
            return True
    return False


def address(project, workstream, session):
    """project/workstream/session — the Case and Wire address."""
    return "%s/%s/%s" % (project or "", workstream or "main", session or "")


def fingerprint(detector, addr, session):
    """One live case per (detector, SESSION) — the case's stable identity.

    F1: this used to key on `addr`, and `addr` embeds `project`, which is a
    human-readable DISPLAY name. So a change to how a project RENDERS
    silently re-keyed every case built on it. Measured on this box: a
    rendering fix that turned '-home-user-zenith-src' into 'zenith-src'
    made the next sweep see the old fingerprints vanish (resolved 'stale'),
    open brand-new cases for the same sessions, and — being already past
    escalation, with a fresh empty notified_ts — push 2 real notifications
    to Luke's phone for conditions he had already been told about.

        id=23  -home-user-zenith-src  resolved stale
        id=26  zenith-src             open           <- same session_id

    `session_id` is stable for a session's whole life, so nothing about how
    we DISPLAY that session can re-key its cases again. The dedup contract
    is unchanged: at most one live case per (detector, session), which
    session_id alone already satisfies — the project/workstream in `addr`
    were never doing any of that work, they were only along for the ride.

    address() is deliberately NOT changed: mutes match on its
    project/workstream form and the UI shows it. Only identity moves.

    Existing rows are rewritten to this scheme by zenith_store.init_db()'s
    user_version<4 migration, in place — without it, shipping this function
    would cause exactly the churn and re-notification it exists to fix.

    EMPTY session_id is the one input that cannot use it, and it falls back
    to the old address-keyed form. It must not fall through to a bare
    "detector|": every session lacking an id would collapse onto that one
    string, and ux_case_live (UNIQUE on fingerprint WHERE state IN
    ('open','snoozed')) would then permit exactly ONE of them to be live at
    a time — each new one resolving the last as 'stale' in a loop, which is
    a worse version of the bug being fixed. No live case has a blank
    session_id today (verified: 0 of 22); the fallback is there so that an
    unforeseen one degrades to the previous behaviour rather than to data
    loss. A session_id can never collide with an address, since address()
    always contains two '/' and a session id never does."""
    return "%s|%s" % (detector, session or addr)


def _encode_path(p):
    return str(p).replace("/", "-")


def project_encoding_map():
    """encoded transcript-dir name -> project path, longest match wins.
    Duplicated from server.py:492 project_encoding_map() rather than
    imported — importing server here would be circular (server imports
    zenith_cases's sweep to start it)."""
    m = {}
    try:
        if PROJECTS_ROOT.exists():
            for p in PROJECTS_ROOT.iterdir():
                if p.is_dir() and not p.name.startswith("."):
                    m[_encode_path(p)] = str(p)
    except OSError:
        pass
    m[_encode_path(PROJECTS_ROOT)] = str(PROJECTS_ROOT)
    m[_encode_path(HOME)] = str(HOME)
    return m


def match_project(dirname, encmap):
    """Duplicated from server.py:504 match_project() — see project_encoding_map."""
    best, best_len = None, -1
    for enc, path in encmap.items():
        if (dirname == enc or dirname.startswith(enc + "--worktrees")) and len(enc) > best_len:
            best, best_len = path, len(enc)
    return best


def project_name(raw_project, cwd, encmap):
    """Readable name for a watcher row's mangled project dir (I3).

    Session Watch's `project` column is an encoded transcript-dir name like
    `-home-user-projects-NexusPrime`, not `NexusPrime`. Storing that
    raw value on a case silently breaks three things downstream: per-project
    config overrides (`projects.<name>.*`) never match, mute addresses like
    `nexuscore/main` never match, and `cases_query(project=...)` /
    the command bar's `#project` scoping (Tasks 5-8) miss entirely.

    Duplicated from server.py:5739 _sw_project_name() rather than imported
    (circular) — the two now deliberately diverge. A session whose transcript
    dir lives outside PROJECTS_ROOT entirely (e.g. ~/zenith-src) fell through
    every fallback below unchanged, so Cases showed the raw encoded dirname
    ('-home-user-zenith-src') instead of a readable name. Fixed here by
    trusting cwd's basename, but ONLY when re-encoding cwd reproduces
    raw_project exactly — i.e. cwd genuinely IS the project root, not just
    somewhere a session's cwd wandered to (a bare `Path(cwd).name` would
    misattribute a session that wandered outside its project, e.g. into a
    scratchpad, to wherever it wandered). server.py's copy has the same gap
    and is NOT fixed here — flag for a follow-up there.

    Fallback chain: encoded-dir match, else the cwd's position under
    PROJECTS_ROOT, else cwd's basename when its encoding round-trips to
    raw_project, else strip the '-claudeProjects-' prefix a raw dirname
    carries."""
    proj = match_project(raw_project or "", encmap)
    if proj:
        return Path(proj).name
    p = Path(cwd or "")
    try:
        rel = p.relative_to(PROJECTS_ROOT).parts
    except ValueError:
        rel = ()
    if rel:
        return rel[0] + (" · " + p.name if len(rel) > 1 else "")
    if cwd and _encode_path(p) == raw_project:
        return p.name
    return (raw_project or "").rsplit("-claudeProjects-", 1)[-1] or None


def _minutes_since(iso, now):
    dt = _parse_iso(iso)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 60.0


def _window(samples, minutes, now):
    """Samples inside the trailing window, oldest first."""
    cutoff = now - timedelta(minutes=float(minutes))
    out = []
    for s in samples:
        dt = _parse_iso(s.get("ts"))
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= cutoff:
            out.append(s)
    return out


def _coerce_float(val, default):
    """Safely coerce a config value to float, falling back to default."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _coerce_int(val, default):
    """Safely coerce a config value to int, falling back to default."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _coerce_dict(val, default):
    """Safely coerce a config value to a dict, falling back to default.

    A hand-edited config can put anything at a key that's supposed to hold a
    block — `"notify": true`, `"off"`, `1`, `["x"]` are all plausible next to
    sibling scalars like `"enabled": true`. Unlike float/int, `dict(val)` on a
    non-dict either raises or silently does the wrong thing (e.g. dict("ab")
    explodes, dict([("a",1)]) 'succeeds' on an unrelated shape), so this is a
    plain isinstance check rather than a coercing constructor call.

    Callers pass the corresponding DEFAULTS block as `default`, never {}:
    degrading a garbled block to per-key built-ins is how quiet hours ended
    up turning themselves OFF on a typo (MF2)."""
    return val if isinstance(val, dict) else default


def _model_names(raw):
    """Session Watch stores `models` comma-joined (session_watch.py:495).
    JSON dict/list are accepted as fallbacks; anything else yields []."""
    if isinstance(raw, dict):
        return [str(k) for k in raw.keys()]
    if isinstance(raw, list):
        return [str(m) for m in raw]
    if not isinstance(raw, str) or not raw.strip():
        return []
    s = raw.strip()
    if s[0] in "[{":
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return [str(k) for k in parsed.keys()]
            if isinstance(parsed, list):
                return [str(m) for m in parsed]
        except (ValueError, TypeError):
            pass
    return [p.strip() for p in s.split(",") if p.strip()]


def _cwd(sess):
    """I3: the session's working directory, carried on every case's evidence.

    A counted fact straight off Session Watch's `sessions.cwd` column — no
    model, no inference, consistent with the counted-never-generated rule.
    Without it the tab's OPEN TERMINAL button called launchTerm('', 'shell')
    for every case, i.e. no surface could reach the session a case is about."""
    return str(sess.get("cwd") or "")


def det_waiting_on_you(sess, samples, cfg, now, events):
    d = detector_cfg(cfg, "waiting_on_you", sess.get("project"))
    if not d.get("enabled", True) or sess.get("state") != "waiting":
        return None
    waited = _minutes_since(sess.get("last_ts"), now)
    after_minutes = _coerce_float(d.get("after_minutes"), 10)
    if waited is None or waited < after_minutes:
        return None
    escalate_minutes = _coerce_float(d.get("escalate_minutes"), 30)
    # D4: `waited_minutes` comes from last_ts — the last event actually
    # written to the transcript — and that is the clock the card shows.
    # `watcher_row_age_minutes` is the OTHER, internal clock: how long since
    # Session Watch refreshed this row. It is carried so the freshness of
    # the evidence can be audited (a very old row means these counts are
    # old), NOT as a second opinion on the wait — see live_sessions() for
    # why it understates. The UI keeps it off the card face for that exact
    # reason and exposes it only in the hover title.
    row_age = _session_age_min(sess, now)
    return {"waited_minutes": int(waited),
            "escalated": waited >= escalate_minutes,
            "watcher_row_age_minutes": None if row_age is None else int(row_age),
            "cwd": _cwd(sess),
            "prompts": sess.get("prompts"), "errors": sess.get("errors")}


def det_burn_no_progress(sess, samples, cfg, now, events):
    """Detect when a session is burning tokens with no file output.

    Verify-event suppression was deliberately omitted (see slice 2 Wire bridge):
    Production verify.* events never carry a session_id in `ref` — they use
    target_ref or job id. The check never matched in production, and the
    fabricated test contract was worse than no test. Proper session↔job
    linkage is deferred to the Wire bridge in slice 2.
    """
    d = detector_cfg(cfg, "burn_no_progress", sess.get("project"))
    if not d.get("enabled", True):
        return None
    window_minutes = _coerce_float(d.get("window_minutes"), 20)
    win = _window(samples, window_minutes, now)
    if len(win) < 2:          # never assume a zero delta from one sample
        return None
    first, last = win[0], win[-1]
    d_tok = (last.get("tok_out") or 0) - (first.get("tok_out") or 0)
    d_edit = (last.get("files_edited") or 0) - (first.get("files_edited") or 0)
    min_output_tokens = _coerce_int(d.get("min_output_tokens"), 50000)
    if d_tok < min_output_tokens or d_edit != 0:
        return None
    return {"delta_tok_out": d_tok, "delta_files_edited": d_edit,
            "cwd": _cwd(sess),
            "window_minutes": int(window_minutes), "samples": len(win)}


def det_silent_stall(sess, samples, cfg, now, events):
    d = detector_cfg(cfg, "silent_stall", sess.get("project"))
    if not d.get("enabled", True) or sess.get("state") != "working":
        return None
    mins = _coerce_float(d.get("after_minutes"), 10)
    if len(samples) < 2:
        return None
    # Walk back from the newest sample while the transcript signature is
    # unchanged. The length of that trailing run is how long it has been
    # frozen — do NOT pre-filter to a window, because the sample that proves
    # the freeze started is by definition older than the threshold.
    sig = (samples[-1].get("mtime"), samples[-1].get("size"))
    if sig == (None, None):  # guard against fabricated freeze (M2)
        return None
    run = []
    for s in reversed(samples):
        if (s.get("mtime"), s.get("size")) != sig:
            break
        run.append(s)
    if len(run) < 2:            # one sample proves nothing
        return None
    span = _minutes_since(run[-1].get("ts"), now)   # run is newest-first
    if span is None or span < mins:
        return None
    return {"frozen_minutes": int(span), "mtime": sig[0], "size": sig[1],
            "cwd": _cwd(sess), "samples": len(run)}


def det_model_fanout(sess, samples, cfg, now, events):
    d = detector_cfg(cfg, "model_fanout", sess.get("project"))
    if not d.get("enabled", True):
        return None
    min_subagents = _coerce_int(d.get("min_subagents"), 4)
    if int(sess.get("subagents") or 0) < min_subagents:
        return None
    names = _model_names(sess.get("models") or "")
    # Absent/null/empty degrades to DEFAULTS, not to [] — same rule as MF2's
    # notify block. Degrading to [] fails safe (the detector goes silent) but
    # leaving one knob on a different rule than every other knob is how the
    # next reader gets it wrong.
    watch_models = d.get("watch_models") or DEFAULTS["detectors"]["model_fanout"]["watch_models"]
    if isinstance(watch_models, str):  # coerce bare string to list
        watch_models = [watch_models]
    if not isinstance(watch_models, (list, tuple)):   # dict/int/... -> DEFAULTS
        watch_models = DEFAULTS["detectors"]["model_fanout"]["watch_models"]
    watch = [str(w).lower() for w in watch_models if w]
    matched = [n for n in names if any(w in n.lower() for w in watch)]
    if not matched:
        return None
    return {"matched_models": matched, "subagents": int(sess.get("subagents") or 0),
            "cwd": _cwd(sess), "tok_out": sess.get("tok_out")}


DETECTORS = {
    "waiting_on_you": det_waiting_on_you,
    "burn_no_progress": det_burn_no_progress,
    "silent_stall": det_silent_stall,
    "model_fanout": det_model_fanout,
}

# D2: detectors exempt from the C1 liveness filter (live_window_minutes).
#
# C1 CORRECTION — read this before reasoning about either clock. `now -
# mtime` is NOT the waited time, and two earlier passes over this file got
# that backwards. See _session_age_min() and live_sessions() below for what
# `sessions.mtime` actually is; the short version is that it is the age of
# the watcher's ROW, not of the user's wait. waited_minutes is computed from
# `sessions.last_ts` (det_waiting_on_you), and that is the correct clock.
#
# The exemption is still right, but for the adjacent reason: a session that
# is waiting on the user has by definition stopped producing transcript
# content, so the watcher stops refreshing its row and `mtime` freezes. The
# liveness filter keys on exactly that frozen mtime, so applying it here
# would stop re-evaluating the very sessions this detector exists to find —
# past 60 minutes the case stopped being updated and a four-hour wait
# rendered "waited 45m" forever, because the evidence was never recomputed.
# The other three detectors keep the filter: their predicates are about a
# session still doing something, and for them a frozen row means there is
# genuinely nothing to re-evaluate.
#
# What still bounds the zombie risk for this detector is session_gone_hours
# (24h): a session quiet past that is not protected and its cases resolve as
# 'stale'. The "auto-resolve only when evaluated-and-false" invariant is
# untouched — an exempt detector is EVALUATED on a quiet session, so it may
# legitimately clear (e.g. the row's state left 'waiting').
LIVENESS_EXEMPT = frozenset(["waiting_on_you"])

# D3: `meta` key marking that the first (backfill) sweep has happened. Its
# VALUE is the ISO timestamp of that sweep, for forensics; only presence is
# ever tested. Renaming this key re-arms the backfill on an existing box.
FIRST_SWEEP_KEY = "cases_first_sweep_ts"


def read_sessions():
    """Every session row Session Watch knows about, READ-ONLY. None when the
    db is absent or unreadable — the caller degrades rather than erroring,
    because 'no watcher' is a normal state, not a fault.

    Deliberately NOT filtered to 'live' here — session_watch.py never
    deletes a row, so this can and does return sessions the watcher stopped
    tracking hours or days ago. Liveness filtering is the sweep's job
    (see live_sessions()), so this stays a plain, honest read of the table."""
    path = Path(SESSION_WATCH_DB)
    if not path.exists():
        _log("swdb", "Session Watch db not found at %s — no cases will open"
                     " until the watcher runs" % path)
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
    except sqlite3.Error as e:
        _log("swdb", "Session Watch db unreadable (%s): %s" % (path, e))
        return None
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM sessions")]
    except sqlite3.Error as e:
        _log("swdb", "Session Watch db query failed (%s): %s" % (path, e))
        return None
    finally:
        con.close()


def _session_age_min(sess, now):
    """Minutes since Session Watch last SAVED this session's row (the
    `mtime` column — epoch seconds, matching session_watch.py's own
    `state_of()` and server.py:5724 _sw_age_min()). None when mtime is
    absent or unparseable.

    C1: this is row-refresh age, NOT wait time and NOT the transcript
    file's current mtime. See live_sessions() for the full derivation and
    the measurements. Anything that wants "how long has the user been on
    the hook" must use `last_ts`, the way det_waiting_on_you does."""
    try:
        mtime = float(sess.get("mtime"))
    except (TypeError, ValueError):
        return None
    return (now.timestamp() - mtime) / 60.0


def live_sessions(sessions, cfg, now):
    """Filter to sessions the watcher still considers live (C1).

    session_watch.py NEVER deletes a session row, and `mtime` freezes long
    before the 60-minute discovery window would ever matter. The actual
    mechanism is tools/session_watch.py:784, `if row and row["offset"] ==
    st.st_size: continue` — the watcher skips reprocessing a transcript
    whose byte offset has not moved since the last pass, and `mtime` is
    only ever written inside that reprocessing step (session_watch.py:490
    save()). So `sessions.mtime` is a FROZEN SNAPSHOT of the transcript
    file's st_mtime, taken at the last pass in which the file had actually
    grown — which makes it, in practice, "when the watcher last saved this
    row". Its own `updated_at` column tracks it to within a minute.

    C1 — WHAT IT IS NOT. Two earlier passes over this file asserted that
    `now - mtime` IS the waited time. That is false, in both directions,
    and nothing should be derived from it:

      * It is not the transcript file's CURRENT mtime. Measured across all
        27 live rows: every row had `offset == st_size` (nothing appended),
        yet every real file mtime was only 4-55 minutes old while the rows
        themselves were 3 minutes to 21 hours old. Something touches these
        files without appending to them, so file mtime is not evidence of
        content activity at all.
      * It is not the wait. For fresh rows the two coincide (the saved
        st_mtime lands ~on the last event), which is why the wrong premise
        survived review twice. On older rows they diverge hard and mtime
        UNDERSTATES: measured, one session's `last_ts` put the wait at
        6041 minutes while `now - mtime` claimed 1269, and another at
        13570 vs 1272 — off by 4x and 10x. Keying the displayed wait on
        mtime would have quietly capped every long wait at the row's age.

    `waited_minutes` therefore uses `sessions.last_ts` — the timestamp of
    the last event actually written to the transcript — deliberately, and
    that is the honest number. det_waiting_on_you is where it is computed.

    What `mtime` IS good for is exactly what this function uses it for:
    deciding whether the watcher still considers a row worth refreshing.
    (The discovery window itself, LIVE_WINDOW_MIN / discover() at
    session_watch.py:43,162, is a separate, earlier gate: it just bounds
    which transcript files the watcher bothers to glob and stat each pass —
    a session can be well past having anything to reprocess long before it
    would ever fall out of that window too.)

    Without this filter, any session that ever passed through 'waiting'
    becomes a permanent waiting_on_you case, and `model_fanout` (which has
    no time predicate of its own) opens on ANY historical session that ever
    hit its subagent floor, no matter how old.

    `state == 'idle'` sessions are additionally excluded outright: no
    detector's predicate is meaningfully true for a session the watcher
    itself already considers inactive.

    A session excluded here is not necessarily GONE — see sweep_once(),
    which protects a merely-quiet session's existing cases from
    auto-resolve and only lets them resolve (as 'stale', not 'auto') once
    the session has been quiet past `session_gone_hours`."""
    window = _coerce_float(cfg.get("live_window_minutes"),
                           DEFAULTS["live_window_minutes"])
    out = []
    for s in sessions:
        if str(s.get("state") or "") == "idle":
            continue
        age = _session_age_min(s, now)
        if age is None or age > window:
            continue
        out.append(s)
    return out


def sample_once(sessions):
    """One sample row per live session. Returns the number written."""
    n = 0
    for s in sessions:
        try:
            zs.sample_insert(session=str(s.get("session_id") or ""),
                             state=str(s.get("state") or ""),
                             tok_out=s.get("tok_out"),
                             files_edited=s.get("files_edited"),
                             errors=s.get("errors"),
                             events_seen=s.get("events_seen"),
                             mtime=s.get("mtime"), size=s.get("offset"))
            n += 1
        except sqlite3.Error:
            pass
    return n


def _escalated(ev):
    """Is this case at its escalated state? (S4)

    The detector decides. waiting_on_you emits `escalated` (waited >=
    escalate_minutes). The other three have no second threshold and emit no
    such key at all — for them the key is ABSENT, which means open IS
    escalated and they notify on open exactly as they did before. That is
    deliberate: none of the three silently stops notifying. (model_fanout
    stops reaching the phone, but because D1 moved it below the notify
    severity floor — a severity decision, not an escalation one.)"""
    return bool(ev.get("escalated", True)) if isinstance(ev, dict) else True


def _notify_escalation(cfg, cid, name, project, ws, sev, ev, now, suppress=False):
    """Fire the ONE notification a case is allowed, at ESCALATION (S4).

    Luke's ruling: notify at high severity, but with the escalation delay,
    not on first fire. Before this, notify() ran only in sweep_once()'s
    case-OPEN branch — i.e. at `after_minutes` (10) — and never on a re-fire,
    so a case that only escalated later could never notify at all, and the
    escalate_minutes knob had no effect on the phone.

    `notified_ts` (a real cases column, added by the user_version<3
    migration) is the persisted marker. It has to be persisted: an in-memory
    set would re-push every open case on every ZENITH restart, which is
    exactly the nag the open-only rule existed to prevent.

    The marker records the escalation DECISION, not a delivery receipt — it
    is stamped even when should_notify() declines (quiet hours, severity
    floor) and even when the ntfy POST fails. Stamping only on a successful
    push would turn quiet hours from "suppressed" into "deferred" and
    deliver the whole night's backlog at 07:00, and would retry a dead ntfy
    every 60s forever. One escalation, one decision, no nag.

    `suppress` (D3, the first-sweep backfill) is a third way to decline, and
    it stamps the marker for the same reason quiet hours does: the decision
    "this one does not push" has been made and must not be revisited on the
    next pass 60 seconds later. Without the stamp the backfill would merely
    DELAY 19 pushes by one sweep, which is not what suppressing them means.

    Returns True only when a push was actually attempted and accepted."""
    zs.case_update(cid, notified_ts=now.isoformat())
    if suppress or not should_notify(cfg, sev, now):
        return False
    return bool(notify(cfg, {"detector": name, "project": project,
                             "workstream": ws, "severity": sev}, ev))


def sweep_once():
    """Sample, evaluate every enabled detector, reconcile case state.

    Reconciliation is level-triggered, not edge-triggered: a predicate that
    still holds updates its case, and one that no longer holds — because it
    was actually EVALUATED and came back false, not because it was skipped
    (disabled/muted/errored/not-live) — auto-resolves it with
    resolution='auto'. That is what keeps the tab from becoming a graveyard
    without also making it trigger-happy about resolving cases nobody
    actually re-checked (I1).

    'Not live' (C1) extends the same invariant one step further: a session
    the liveness filter excluded was not evaluated either, so its existing
    cases are protected exactly like a muted/disabled/crashed detector's —
    UNLESS the session has been quiet long enough (`session_gone_hours`)
    to call it certainly over, in which case its cases resolve too, but
    with the distinct resolution='stale', because nothing was actually
    re-checked and the record must not claim otherwise.

    D2 carves ONE hole in that filter: the detectors in LIVENESS_EXEMPT
    (waiting_on_you) are evaluated on quiet sessions too, because for them
    the frozen mtime the filter keys on IS the evidence. They are still
    bounded by session_gone_hours, and being evaluated they can also
    legitimately clear — the I1 invariant is unchanged, the set of
    "actually evaluated" simply got bigger for one detector."""
    out = {"opened": 0, "updated": 0, "resolved": 0, "sampled": 0,
           "notified": 0, "skipped": False}
    cfg = load_config()
    if not cfg.get("enabled", True):
        out["skipped"] = True
        return out
    sessions = read_sessions()
    if sessions is None:
        out["skipped"] = True
        return out

    # D3: the FIRST sweep that ever gets this far is a backfill of state
    # that already existed, not a burst of 19 new events. Measured on this
    # box: a first deploy opens 22 cases and 19 of them are already past
    # escalate_minutes, so every one would push immediately. Luke's call is
    # to open them all on the tab and push none.
    #
    # The marker is a `meta` row, so it survives a process restart — an
    # in-memory flag would re-arm the backfill on every ZENITH restart and
    # suppress a genuine push each time. It is deliberately NOT "is the
    # cases table empty": a user who dismisses every case would re-trigger
    # that, silently swallowing the next real escalation.
    #
    # It is stamped HERE, before any evaluation, rather than after the pass
    # completes. "Exactly once" is the requirement, and a sweep that raises
    # halfway through has still opened real cases; re-running the backfill
    # against a half-populated board would suppress pushes for the cases the
    # crashed pass never reached. Setting it first makes the suppression
    # strictly one pass wide, which is the failure direction that costs a
    # notification rather than the one that repeats silently.
    #
    # Both early returns above (disabled, watcher db unreadable) leave the
    # marker unset on purpose: neither evaluated anything, so neither was
    # the backfill.
    backfill = not zs.meta_get(FIRST_SWEEP_KEY)
    if backfill:
        zs.meta_set(FIRST_SWEEP_KEY, datetime.now(timezone.utc).isoformat())
        _log("backfill", "first cases sweep — opening pre-existing state as"
                         " cases, suppressing notifications for this pass only")

    now = datetime.now(timezone.utc)
    live = live_sessions(sessions, cfg, now)                # C1: eligible for evaluation
    out["sampled"] = sample_once(live)
    dismiss_cooldown = _coerce_float(cfg.get("dismiss_cooldown_minutes"),
                                     DEFAULTS["dismiss_cooldown_minutes"])
    gone_after_min = _coerce_float(cfg.get("session_gone_hours"),
                                   DEFAULTS["session_gone_hours"]) * 60.0
    encmap = project_encoding_map()                          # I3
    live_fps = set()      # must not resolve this pass, for any reason
    cleared_fps = set()   # positively evaluated false this pass -> may auto-resolve
    since_iso = (now - timedelta(hours=SAMPLE_RETENTION_HOURS)).isoformat()

    def evaluate(sess, names):
        """Run `names` against one session and reconcile each one's case."""
        raw_project = str(sess.get("project") or "")
        project = project_name(raw_project, sess.get("cwd"), encmap) or raw_project
        ws = str(sess.get("branch") or "main")
        sid = str(sess.get("session_id") or "")
        addr = address(project, ws, sid)
        # A normalised copy so every detector's own detector_cfg(project=...)
        # lookup sees the readable name too, not just the outer bookkeeping
        # below — otherwise per-project overrides still silently miss (I3).
        sess_n = dict(sess, project=project)
        samples = zs.samples_since(sid, since_iso)

        for name in names:
            fn = DETECTORS[name]
            fp = fingerprint(name, addr, sid)
            d = detector_cfg(cfg, name, project)

            # Every one of these three "skip" paths means the predicate was
            # NOT genuinely evaluated-and-found-false this pass, so the
            # fingerprint must stay protected from auto-resolve (I1):
            #   - disabled (config or per-project): "existing cases left
            #     untouched" per the design doc.
            #   - muted: mutes suppress notification and creation, not
            #     resolution, per the design doc.
            #   - detector raised: a transient fault is not a cleared
            #     condition.
            if not d.get("enabled", True):
                live_fps.add(fp)
                continue
            if is_muted(cfg, name, "%s/%s" % (project, ws), now) or \
               is_muted(cfg, name, addr, now):
                live_fps.add(fp)
                continue
            try:
                ev = fn(sess_n, samples, cfg, now, [])
            except Exception as e:
                live_fps.add(fp)
                # I4: a detector that raises every pass used to be completely
                # invisible — the board just quietly stopped changing.
                _log("det:" + name,
                     "detector %s raised on session %s: %r" % (name, sid[:8], e))
                continue          # one bad predicate must not stop the others

            if not ev:
                cleared_fps.add(fp)   # genuinely evaluated and cleared -> may auto-resolve
                continue

            live_fps.add(fp)
            existing = zs.case_by_fingerprint(fp, include_dismissed_minutes=dismiss_cooldown)
            if existing and existing.get("state") == "dismissed":
                continue          # C2: still cooling down — do not reopen or re-notify
            # N1: a SNOOZED case still gets UPDATED (its evidence must stay
            # current, and cases_expire_snoozes() will un-hide it later) but
            # must never reach the phone while it is hidden. Before this the
            # only state guard here was 'dismissed', so: open at 12m
            # un-escalated (no push) -> user hits SNOOZE 1H -> next sweep
            # past 30m -> the row is snoozed, notified_ts is still empty,
            # _escalated() is true -> push. The tab hid the case while the
            # phone buzzed about it, which is the exact opposite of snooze.
            # (Pre-S4 this was impossible only because notify ran on the
            # open branch alone.)
            snoozed = bool(existing and existing.get("state") == "snoozed")
            sev = str(d.get("severity", "medium"))
            if existing:
                # F1 corollary. Now that identity is pinned to the session,
                # a case SURVIVES a project rename instead of being replaced
                # by a fresh one that happened to carry the new name — so
                # the display columns have to be refreshed here, or the
                # board would show the name the case was opened under
                # forever. Trading the churn bug for a staleness bug is not
                # a fix. Only what we RENDER moves; the fingerprint does
                # not, which is the whole point.
                # `v and` — a name that failed to resolve this pass must not
                # blank out one that resolved fine when the case opened.
                drift = dict((k, v) for k, v in (("project", project),
                                                 ("workstream", ws))
                             if v and str(existing.get(k) or "") != v)
                zs.case_update(existing["id"], evidence=ev,
                               fire_count=int(existing.get("fire_count") or 1) + 1,
                               **drift)
                out["updated"] += 1
                cid = existing["id"]
                notified = str(existing.get("notified_ts") or "")
            else:
                cid = zs.case_open(detector=name, fingerprint=fp, project=project,
                                   workstream=ws, session_id=sid,
                                   severity=sev, state="open", evidence=ev)
                zs.emit("case.open", project=project, ref=str(cid),
                        outcome=sev, actor="zenith",
                        data={"detector": name, "address": addr})
                out["opened"] += 1
                notified = ""

            # S4: notify exactly once, the first time this case is seen
            # ESCALATED — which may be at open (already past
            # escalate_minutes) or on the later update that first flips it.
            # `notified_ts` on the row is what makes "first time" durable, so
            # a case that keeps being true still never pushes twice, and a
            # restart does not re-push the whole board.
            #
            # Still wrapped: a notification concern must never abort case
            # management (fix round 1 — an AttributeError here escaped
            # sweep_once() entirely and sweep_loop()'s blanket except ate the
            # rest of the pass, silently, every 60s). Unlike round 1 the
            # failure is now LOGGED instead of vanishing (I4).
            #
            # N1: `snoozed` suppresses the push WITHOUT stamping notified_ts.
            # That is the deliberate half of the decision. A snooze is
            # "not now", not "never" — DISMISS is the verb that means never,
            # and collapsing the two would leave the UI with two buttons and
            # one behaviour. So when cases_expire_snoozes() flips the row
            # back to 'open' and the condition is still escalated, the next
            # sweep notifies then, once. The no-nag guarantee is untouched:
            # notified_ts is stamped on that push, so the case still gets
            # exactly one over its whole life. (A case that already notified
            # before being snoozed has a non-empty marker and stays silent.)
            if not notified and _escalated(ev) and not snoozed:
                try:
                    if _notify_escalation(cfg, cid, name, project, ws, sev, ev, now,
                                          suppress=backfill):
                        out["notified"] += 1
                except Exception as e:
                    _log("notify", "notify path failed for case %s: %r" % (cid, e))

    # Sessions the C1 filter excluded still need their EXISTING cases
    # accounted for. A quiet session (age <= session_gone_hours) protects
    # its cases' fingerprints exactly like a mute/disabled/crash would —
    # nothing was re-evaluated, so nothing may resolve. Only a session gone
    # longer than that is left unprotected, falling through to the 'stale'
    # pass below (not the 'auto' one — no predicate was evaluated).
    #
    # D2: except for LIVENESS_EXEMPT detectors, which ARE evaluated on these
    # sessions (below) and so must NOT be blanket-protected here — that
    # protection is what froze a four-hour wait's evidence at "45m".
    live_ids = set(str(s.get("session_id") or "") for s in live)
    quiet = []
    for sess in sessions:
        sid = str(sess.get("session_id") or "")
        if sid in live_ids:
            continue
        age = _session_age_min(sess, now)
        if age is None or age > gone_after_min:
            continue                      # gone (or unreadable age) -> eligible for 'stale'
        quiet.append(sess)
        raw_project = str(sess.get("project") or "")
        project = project_name(raw_project, sess.get("cwd"), encmap) or raw_project
        ws = str(sess.get("branch") or "main")
        addr = address(project, ws, sid)
        for name in DETECTORS:
            if name not in LIVENESS_EXEMPT:
                live_fps.add(fingerprint(name, addr, sid))

    for sess in live:
        evaluate(sess, list(DETECTORS))
    for sess in quiet:                                       # D2
        evaluate(sess, [n for n in DETECTORS if n in LIVENESS_EXEMPT])

    # I2/I4: resolve + un-snooze in SQL, not by paging a cases_query() capped
    # at 500 rows — that cap would strand the oldest (most overdue) cases.
    # Two distinct passes, in order: 'auto' targets ONLY the fingerprints
    # positively evaluated-false this pass (cleared_fps); 'stale' is the
    # catch-all for everything else not protected (live_fps) — a gone
    # session, or a case whose session_id isn't in the watcher's table at
    # all — since in neither case was anything actually re-checked.
    for state in ("open", "snoozed"):
        for c in zs.cases_resolve_matching(state, cleared_fps, resolution="auto"):
            zs.emit("case.resolve", project=c.get("project", ""), ref=str(c["id"]),
                    outcome="auto", actor="zenith", data={"detector": c["detector"]})
            out["resolved"] += 1
        for c in zs.cases_resolve_cleared(state, live_fps, resolution="stale"):
            zs.emit("case.resolve", project=c.get("project", ""), ref=str(c["id"]),
                    outcome="stale", actor="zenith", data={"detector": c["detector"]})
            out["resolved"] += 1
    zs.cases_expire_snoozes(now.isoformat())               # I4: unhide expired snoozes

    try:
        zs.samples_prune(since_iso)
    except sqlite3.Error as e:
        _log("prune", "sample prune failed: %r" % (e,))
    return out


NTFY_URL = os.environ.get("ZENITH_NTFY_URL", "http://127.0.0.1:2586")
NTFY_TOPIC = os.environ.get("ZENITH_NTFY_TOPIC", "nexuscore-alerts")


def _hhmm(s):
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def in_quiet_hours(cfg, now):
    """Quiet hours are LOCAL wall-clock (the box's system timezone) and may
    wrap midnight. sweep_once() always passes an AWARE UTC `now`
    (datetime.now(timezone.utc)) — this box is America/Chicago, so that value
    is converted via now.astimezone() before extracting hour/minute. Without
    that conversion the default ["23:00","07:00"] (Luke's bedtime) gets
    compared against the wrong clock and the feature inverts: it suppresses
    pushes while he's awake and fires them while he's asleep. A NAIVE `now`
    (some unit tests pass one to isolate the wrap-midnight arithmetic from
    timezone concerns) is presumed by astimezone() to already BE local
    wall-clock, per stdlib semantics — it gets a local tzinfo attached with
    its hour/minute left untouched, so callers that intentionally pass a
    naive local instant are unaffected.

    MF2 — degrade in the SAFE direction. Config is a trust boundary: it is
    hand-editable and re-read every sweep pass, so a wrong type must never
    raise. But it must also not degrade to 'never quiet', which is the
    direction that COSTS something: measured at 03:00 local, a `notify` of
    `true` / `"off"` / `[]`, or a `quiet_hours` of `null` / `[]` / `["",""]`,
    each produced a HIGH-priority push to Luke's phone that the shipped
    defaults correctly suppress. A typo in the config must not be able to
    turn the bedtime window off.

    So both halves fall back to DEFAULTS["notify"], not to per-key built-ins:
    a non-dict `notify` block takes the whole default block, and a
    quiet_hours that is absent, None, not a 2-element sequence, or not
    parseable as HH:MM takes DEFAULTS["notify"]["quiet_hours"]. The way to
    say "no quiet hours" is an explicit zero-length window (["00:00",
    "00:00"]) or notify.enabled=false — both are deliberate, neither is
    something a typo produces by accident."""
    n = _coerce_dict(cfg.get("notify"), DEFAULTS["notify"])
    qh = n.get("quiet_hours")
    start = end = None
    if isinstance(qh, (list, tuple)) and len(qh) == 2:
        start, end = _hhmm(qh[0]), _hhmm(qh[1])
    if start is None or end is None:
        dqh = DEFAULTS["notify"]["quiet_hours"]
        start, end = _hhmm(dqh[0]), _hhmm(dqh[1])
    local = now.astimezone()
    cur = local.hour * 60 + local.minute
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end      # wraps midnight


def should_notify(cfg, severity, now):
    # MF2: a malformed block degrades to the whole DEFAULTS["notify"], never
    # to per-key built-ins — see in_quiet_hours().
    n = _coerce_dict(cfg.get("notify"), DEFAULTS["notify"])
    if not n.get("enabled", True):
        return False
    floor = SEVERITY_ORDER.get(str(n.get("min_severity", "medium")), 1)
    if SEVERITY_ORDER.get(str(severity), 0) < floor:
        return False
    return not in_quiet_hours(cfg, now)


def _ascii_header(s):
    """Fold a header value to plain ASCII. HTTP header values are not UTF-8.

    F2: http.client.putheader() encodes str values as LATIN-1, and ntfy
    decodes what arrives as UTF-8, so the two disagree about every byte
    above 0x7F. The title we shipped used U+00B7 as its separator and
    landed on Luke's phone as `waiting on you � nexuscore` — confirmed
    in ntfy's own message cache, where the title bytes are literally
    `waiting on you \\xef\\xbf\\xbd nexuscore` (U+FFFD, the replacement
    character). Anything OUTSIDE latin-1 is worse than mangled: putheader()
    raises UnicodeEncodeError, notify()'s blanket except swallows it, and
    the push is lost with only a log line.

    So this is not just about the separator — the PROJECT NAME goes into
    the same header, and one on this box right now is
    'floor-app · group-complete-switch'. Changing the separator alone would
    have left that case still broken.

    NFKD first so accented letters degrade to their base letter (José ->
    Jose) instead of vanishing; anything still non-ASCII is dropped, and
    whitespace runs are then collapsed so a dropped character does not
    leave a double space behind. Collapsing also removes any CR/LF, which
    http.client rejects outright as header injection.

    A notification title is a glance on a lock screen, not a place for
    typography — losing a middot is the correct trade against losing the
    push."""
    folded = unicodedata.normalize("NFKD", str(s))
    folded = folded.encode("ascii", "ignore").decode("ascii")
    return " ".join(folded.split())


def notify(cfg, case_row, evidence):
    """Best-effort ntfy push. Returns True on success. NEVER raises — a case
    that could not be pushed is still a case, and the tab is the source of
    truth.

    F2: the title is ASCII-folded because it travels in a HEADER (see
    _ascii_header). The BODY is not, and must not be: it is the request
    PAYLOAD, sent as explicit UTF-8 bytes, and ntfy stores it as UTF-8 —
    verified in the message cache, where the body's em dash is intact
    `\\xe2\\x80\\x94` while the title's separator in the same message is
    `\\xef\\xbf\\xbd`. Same message, one broken half, and the header is the
    broken one."""
    title = _ascii_header("%s - %s" % (
        str(case_row.get("detector", "")).replace("_", " "),
        case_row.get("project") or "?"))
    body = "%s/%s — %s" % (case_row.get("project") or "?",
                           case_row.get("workstream") or "main",
                           json.dumps(evidence, default=str)[:180])
    try:
        req = urllib.request.Request(
            "%s/%s" % (NTFY_URL.rstrip("/"), NTFY_TOPIC),
            data=body.encode("utf-8"), method="POST")
        req.add_header("Title", title)
        req.add_header("Priority",
                       "high" if case_row.get("severity") == "high" else "default")
        req.add_header("Tags", "warning")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception as e:
        # I4: an ntfy that has been down for a week should say so once in a
        # while rather than swallowing every push in silence.
        _log("ntfy", "push failed (%s/%s): %r" % (NTFY_URL, NTFY_TOPIC, e))
        return False


def sweep_loop():
    """Thread target. Never raises out of the loop — a detector bug must not
    take the ZENITH process down with it.

    I4: it does not swallow silently any more, either. A sweep that raises
    every pass presented as a normal board of quietly-ageing cases, with
    nothing anywhere saying the feature had died — that exact class of
    failure was the T9 Critical and was expensive to find. Rate-limited by
    _log(), so a persistent fault writes one line per 15 minutes rather than
    one per poll."""
    while True:
        try:
            sweep_once()
        except Exception as e:
            _log("sweep", "sweep pass failed: %r" % (e,))
        try:
            time.sleep(max(15, int(load_config().get("poll_seconds", 60))))
        except Exception as e:
            _log("poll", "poll_seconds unusable (%r) — sleeping 60s" % (e,))
            time.sleep(60)
