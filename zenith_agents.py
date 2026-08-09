"""zenith_agents.py — ZENITH/OS agent adapter layer (Phase 2).

Manifest loader (shipped defaults/agents.json three-way merged into machine
state data/agents.json) + adapter resolution + PURE parser registries for
cost envelopes (COST_PARSERS) and transcripts (TRANSCRIPT_PARSERS).
Boundary rule (binding, mirrors zenith_store.py): stdlib only —
json/os/re/fnmatch/shutil. NO HTTP, NO subprocess, NO sqlite: server.py runs
the processes and the model probes and hands the results to these functions
(see resolve_models(adapter, detected)).
Self-test: `python3 zenith_agents.py` → "agents self-test OK".
"""
import fnmatch
import json
import os
import re
import shutil
import sys

AGENTS_FILE = None   # str path; set by init() from server.py (None → seeds only)

# Transcript-parser limits. Named once instead of repeated as literals in each
# of the three parsers (they must agree — session_detail's telemetry shape is
# compared across agents).
MAX_TRANSCRIPT_LINES = 400000   # hard stop on a runaway/append-only transcript
MAX_PROMPTS_KEPT = 150          # prompts retained per session in telemetry
PROMPT_EXCERPT_CHARS = 400      # per-prompt excerpt length

# ---------------------------------------------------------------- shipped defaults
# defaults/ is TRACKED and never written at runtime; data/ is gitignored machine
# state and always wins. Keeping the shipped defaults as real JSON files (rather
# than Python literals) is the whole point: they become diffable, reviewable in a
# PR, exportable, and editable without touching source.
def _defaults_dir():
    """Where defaults/ lives. In a PyInstaller bundle the read-only resources are
    unpacked under sys._MEIPASS, NOT next to this file — server.py:90-98 already
    resolves static/ that way and defaults/ must follow, or a packaged install
    silently reads nothing and falls back to the in-source tables."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "defaults")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaults")


DEFAULTS_DIR = _defaults_dir()
SNAPSHOT_NAME = ".seed-snapshot.json"


def load_defaults(name, fallback=None):
    """Read defaults/<name>.json. Never raises — a missing or corrupt defaults
    file must not stop the server booting, it just means 'no shipped default'."""
    try:
        with open(os.path.join(DEFAULTS_DIR, name + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback if fallback is not None else []


def three_way_merge(new_seed, user, old_seed):
    """Git's merge rule, applied per field.

        user value == what we last shipped  -> the user never touched it, so take
                                               the NEW shipped value (an upgrade)
        user value != what we last shipped  -> they customised it, keep theirs

    This is what the old id-only merge could not do: it only ever ADDED whole
    entries, so a corrected model id or a fixed argv never reached anyone who
    already had that agent — silently, with no signal their copy was stale.
    Recurses one level into dicts so `models`/`mode_flags` merge field-wise too.
    """
    if not isinstance(user, dict) or not isinstance(new_seed, dict):
        return user if user is not None else new_seed
    old_seed = old_seed if isinstance(old_seed, dict) else {}
    out = dict(user)
    for k, new_v in new_seed.items():
        if k not in user:                       # brand new key -> adopt it
            out[k] = new_v
            continue
        old_v = old_seed.get(k)
        if isinstance(new_v, dict) and isinstance(user.get(k), dict):
            out[k] = three_way_merge(new_v, user[k], old_v)
        elif user[k] == old_v:                  # untouched since we shipped it
            out[k] = new_v
        # else: user changed it -> keep user[k]
    return out


def merge_by_id(new_seeds, existing, snapshot, key="id"):
    """Three-way merge a list-of-dicts keyed by `key`. New ids are appended;
    ids the user deleted stay deleted (absence is a choice, not a gap)."""
    snap = {s.get(key): s for s in (snapshot or []) if isinstance(s, dict)}
    seen = {s.get(key): s for s in new_seeds if isinstance(s, dict)}
    out, have = [], set()
    for item in (existing or []):
        if not isinstance(item, dict):
            continue
        iid = item.get(key)
        have.add(iid)
        out.append(three_way_merge(seen[iid], item, snap.get(iid))
                   if iid in seen else item)
    out.extend(s for s in new_seeds
               if isinstance(s, dict) and s.get(key) not in have
               and s.get(key) not in snap)      # never resurrect a deleted id
    return out


def init(path):
    """server.py boot hook: bind the manifest to data/agents.json."""
    global AGENTS_FILE
    AGENTS_FILE = str(path) if path else None


def seed_agents():
    """The shipped manifest (defaults/agents.json), fresh objects each call.

    Agent manifests are DATA, not code: they live in a tracked JSON file so a
    corrected model id or argv is a reviewable one-line diff, and so a user can
    edit them without touching source. Missing/corrupt defaults → [] (the
    server still boots; data/agents.json just gets no new seeds)."""
    return load_defaults("agents", [])


def _snapshot_file():
    """data/.seed-snapshot.json — lives next to AGENTS_FILE."""
    return (os.path.join(os.path.dirname(AGENTS_FILE), SNAPSHOT_NAME)
            if AGENTS_FILE else None)


def read_snapshot(section):
    """The last-seeded list for `section`, or None when unknown.

    The snapshot file is an OBJECT keyed by section — {"providers": [...],
    "agents": [...]} — because agents and providers share it. A bare list is
    the legacy agents-only form and is read as such. Absent/corrupt → None,
    which the merge treats as 'nothing is known to be untouched', so the
    user's file wins field-for-field (never clobber on a cold start)."""
    path = _snapshot_file()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(snap, list):
        return snap if section == "agents" else None
    if isinstance(snap, dict):
        v = snap.get(section)
        return v if isinstance(v, list) else None
    return None


def write_snapshot(section, items):
    """Update ONE key of the shared snapshot, preserving every other section.
    Read-modify-write: blindly writing a bare list here would destroy the
    providers snapshot and silently disable that side's upgrade path."""
    path = _snapshot_file()
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        snap = None
    if isinstance(snap, list):                 # legacy agents-only form
        snap = {"agents": snap}
    elif not isinstance(snap, dict):
        snap = {}
    snap[section] = items
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2, default=str)
        os.replace(tmp, path)
    except OSError:
        pass


def _write_agents(items):
    try:
        os.makedirs(os.path.dirname(AGENTS_FILE), exist_ok=True)
        tmp = AGENTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, default=str)
        os.replace(tmp, AGENTS_FILE)
    except OSError:
        pass


def load_agents():
    """The manifest list, three-way merged against the shipped defaults.

    First boot seeds data/agents.json from defaults/agents.json. Later boots
    merge per FIELD against data/.seed-snapshot.json: a field the user never
    touched tracks the shipped default (so a corrected model id actually
    reaches existing installs), a field they changed is kept forever, new ids
    are added, deleted ids stay deleted. The old id-only merge could only ever
    ADD whole entries, which is why a stale model list survived upgrades
    silently. Corrupt/unreadable file → seeds, never clobbered. Never raises."""
    seeds = seed_agents()
    if not AGENTS_FILE:
        return seeds
    if not os.path.exists(AGENTS_FILE):
        _write_agents(seeds)
        write_snapshot("agents", seeds)
        return seeds
    try:
        with open(AGENTS_FILE, encoding="utf-8") as f:
            items = json.load(f)
        if not isinstance(items, list):
            return seeds
    except (OSError, ValueError):
        return seeds
    snapshot = read_snapshot("agents")
    merged = merge_by_id(seeds, items, snapshot)
    if merged != items:
        _write_agents(merged)
    if snapshot != seeds:                      # record what we just seeded
        write_snapshot("agents", seeds)
    return merged


def _expand_probe(pat):
    """bin_probe hint -> concrete candidate paths. '~' expands; '*' matches one
    path component (fnmatch over the parent listing — globbing without adding
    the glob import, keeping the stdlib boundary at json/os/re/fnmatch/shutil).
    That is what lets a hint like '~/Library/Python/*/bin/aider' find a pip
    --user install without pinning anyone's Python minor version."""
    pat = os.path.expanduser(str(pat))
    if "*" not in pat:
        return [pat]
    parts = pat.split(os.sep)
    roots = [parts[0] or os.sep]
    for part in parts[1:]:
        nxt = []
        for r in roots:
            if "*" not in part:
                nxt.append(os.path.join(r, part))
                continue
            try:
                names = os.listdir(r)
            except OSError:
                continue
            nxt.extend(os.path.join(r, n) for n in sorted(names, reverse=True)
                       if fnmatch.fnmatch(n, part))
        roots = nxt[:64]                       # never fan out unboundedly
    return roots


def resolve_bin(entry):
    """PATH lookup, then bin_probe fallbacks, then the raw bin string (so a
    missing binary keeps today's Popen-OSError error path instead of a new
    failure mode). None only when the entry has no bin at all."""
    b = str(entry.get("bin") or "")
    w = shutil.which(b) if b else None
    if w:
        return w
    for hint in entry.get("bin_probe") or []:
        for cand in _expand_probe(hint):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return b or None


def resolve_agent(agent_id, agents=None):
    """Enabled manifest entry -> adapter dict (a copy) with bin_path set.
    None for unknown or disabled ids."""
    for a in (agents if agents is not None else load_agents()):
        if isinstance(a, dict) and a.get("id") == agent_id:
            if not a.get("enabled"):
                return None
            ad = dict(a)
            ad["bin_path"] = resolve_bin(a)
            return ad
    return None


def normalize_mode(adapter, mode):
    """ZENITH normalized mode (default|acceptEdits|bypassPermissions) ->
    this agent's CLI flags via manifest mode_flags. Unknown mode → default.
    NEVER raises on a garbage manifest (data/agents.json is user-editable, a
    declared trust boundary): a non-dict mode_flags or non-list flag list is
    treated as empty — same never-crash contract the parsers honor."""
    mf = adapter.get("mode_flags")
    if not isinstance(mf, dict):
        return []
    flags = mf.get(mode) if mode in mf else mf.get("default")
    if not isinstance(flags, list):
        return []
    return [str(x) for x in flags]


def _expand(tok, vals):
    for k, v in vals.items():
        tok = tok.replace("{" + k + "}", v)
    return tok


def build_argv(adapter, job):
    """Manifest argv template -> full argv. '{mode_flags}' splices the
    normalize_mode list; {prompt}/{model}/{cwd}/{last_msg_file} substitute
    into single tokens; budget/add_dir append via the manifest's
    budget_flag/add_dir_flag (claude-only today)."""
    vals = {"prompt": str(job.get("prompt") or ""),
            "model": str(job.get("model") or ""),
            "cwd": str(job.get("project") or ""),
            "last_msg_file": str(job.get("_last_msg_file") or "")}
    argv = [str(adapter.get("bin_path") or adapter.get("bin") or "")]
    if adapter.get("exec_subcommand"):
        argv.append(str(adapter["exec_subcommand"]))
    template = adapter.get("argv")
    if not isinstance(template, list):     # garbage manifest → no template tokens
        template = []
    for tok in template:
        tok = str(tok)
        if tok == "{mode_flags}":
            argv.extend(normalize_mode(adapter, str(job.get("mode") or "default")))
        else:
            argv.append(_expand(tok, vals))
    if job.get("budget") and adapter.get("budget_flag"):
        argv += [str(adapter["budget_flag"]), str(job["budget"])]
    if job.get("add_dir") and adapter.get("add_dir_flag"):
        argv += [str(adapter["add_dir_flag"]), str(job["add_dir"])]
    return argv


def wants_last_msg_file(adapter):
    """True when the argv template references {last_msg_file} — spawn_job
    then mints a tempfile into job['_last_msg_file'] (design D2)."""
    return any("{last_msg_file}" in str(t) for t in adapter.get("argv") or [])


class ModelEntry(dict):
    """One resolved model. A plain dict for the API/UI —
    {"id", "confirmed", "source"}, where the UI greys out confirmed:False —
    that ALSO stringifies to its bare id, so an id-only consumer
    (server.agent_models does `[str(m) for m in resolve_models(...)]`) keeps
    working without a change. Two contracts, one object; json.dumps still
    emits the full dict."""
    __slots__ = ()

    def __str__(self):
        return str(self.get("id") or "")


def _entry(mid, confirmed, source):
    return ModelEntry(id=str(mid), confirmed=confirmed, source=source)


def resolve_models(adapter, detected=None):
    """models manifest block -> [{"id", "confirmed", "source"}].

    PURE — no HTTP, no subprocess. The caller (server.py) runs the probes and
    hands the results in as `detected`, a {source_name: [ids]} map, today
    {"ollama_tags": [...], "openai_models": [...]}.

    kind:
      static  -> the shipped list, asserted present (a CLI with no models
                 endpoint, e.g. claude: nothing to probe, the list IS the fact)
      suggest -> the shipped list, NEVER asserted present — the UI shows these
                 greyed, and picking one is what triggers "install/configure?"
      detect  -> whatever the named source actually reported; if the probe
                 found nothing, `fallback` is offered as suggestions.
    Unknown/absent kind is treated as static so a hand-edited manifest that
    predates this field keeps working unchanged.

    confirmed=True means "we have evidence this model is there". That is the
    whole point of the split: a fresh install must not claim the owner's
    locally-pulled ollama tags exist on someone else's machine."""
    m = (adapter or {}).get("models") or {}
    if not isinstance(m, dict):
        return []
    kind = str(m.get("kind") or "static")
    listed = [str(x) for x in (m.get("list") or []) if str(x)]
    if kind == "detect":
        src = str(m.get("source") or "")
        got = (detected or {}).get(src) or []
        if got:
            return [_entry(x, True, src) for x in got if str(x)]
        return [_entry(x, False, "fallback")
                for x in (m.get("fallback") or []) if str(x)]
    if kind == "suggest":
        return [_entry(x, False, "suggest") for x in listed]
    return [_entry(x, True, "static") for x in listed]


def list_models(adapter, detected=None):
    """Flat model id list (drops the confirmed/source detail of
    resolve_models). Kept for callers that just want ids."""
    return [m["id"] for m in resolve_models(adapter, detected)]


# ------------------------------------------------------------- cost parsers
# Every parser: fn(stdout, stderr, extra) -> normalized dict | None, where
# the dict is {"result": str, "usage": {"in","out","cache_r","cache_w"},
# "cost_usd": float|None, "session_id": str|None, "raw": dict|None}.
# PURE + tolerant: never raise, None on nothing-parsable.

def claude_envelope(text):
    """Tolerant parse of `claude -p --output-format json` output (the moved
    server._parse_envelope logic, byte-identical semantics). The CLI emits a
    top-level JSON ARRAY of turn messages; the result envelope is the LAST
    element with type=="result". Also handles a bare result dict (older CLI)
    and a result object on its own line. str -> dict | None; never raises."""
    s = (text or "").strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list):
        for item in reversed(obj):
            if isinstance(item, dict) and item.get("type") == "result":
                return item
        return None
    for line in reversed(s.splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    return None


def _cost_claude_json_array(stdout, stderr, extra):
    env = claude_envelope(stdout)
    if not env:
        return None
    u = env.get("usage") or {}
    return {"result": env.get("result") or "",
            "usage": {"in": u.get("input_tokens"), "out": u.get("output_tokens"),
                      "cache_r": u.get("cache_read_input_tokens"),
                      "cache_w": u.get("cache_creation_input_tokens")},
            "cost_usd": env.get("total_cost_usd"),
            "session_id": env.get("session_id"),
            "raw": env}


def _cost_none(stdout, stderr, extra):
    return None


def _find_usage(obj, depth=0):
    """First nested dict carrying an input_tokens/output_tokens pair —
    tolerant to codex event-shape drift (VERIFY AT BUILD, plan Task 11).
    Confirmed against codex-cli 0.144.0-alpha.4: the exec --json stream's
    turn.completed.usage sits at depth 1; the session-log token_count event
    nests it at payload.info.total_token_usage (depth 3)."""
    if depth > 4 or not isinstance(obj, dict):
        return None
    if "input_tokens" in obj and "output_tokens" in obj:
        return obj
    for v in obj.values():
        if isinstance(v, dict):
            r = _find_usage(v, depth + 1)
            if r:
                return r
    return None


def _find_key(obj, key, depth=0):
    """First string value for `key` anywhere in a nested dict, else None."""
    if depth > 4 or not isinstance(obj, dict):
        return None
    v = obj.get(key)
    if isinstance(v, str) and v:
        return v
    for x in obj.values():
        if isinstance(x, dict):
            r = _find_key(x, key, depth + 1)
            if r:
                return r
    return None


def _cost_codex_json(stdout, stderr, extra):
    """`codex exec --json` JSONL event stream → normalized usage. Tolerant
    scan: the LAST usage-shaped dict wins (codex reports cumulative totals);
    result text comes from --output-last-message (extra['last_msg_file']).
    ChatGPT-auth codex reports no dollar cost → cost_usd None.
    VERIFIED AT BUILD against codex-cli 0.144.0-alpha.4: usage lives on the
    turn.completed event as {input_tokens, cached_input_tokens, output_tokens,
    reasoning_output_tokens} with NO write-cache field (cache_w stays None);
    the stream carries thread_id, not session_id (so session_id → None)."""
    usage_src = None
    session_id = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        u = _find_usage(obj)
        if u:
            usage_src = u
        session_id = session_id or _find_key(obj, "session_id")
    result = None
    lmf = (extra or {}).get("last_msg_file")
    if lmf and os.path.isfile(lmf):
        try:
            with open(lmf, encoding="utf-8", errors="replace") as f:
                result = f.read().strip()
        except OSError:
            pass
    if usage_src is None and not result:
        return None
    u = usage_src or {}
    cr = u.get("cache_read_input_tokens")
    if cr is None:
        cr = u.get("cached_input_tokens")
    return {"result": result or "",
            "usage": {"in": u.get("input_tokens"), "out": u.get("output_tokens"),
                      "cache_r": cr, "cache_w": u.get("cache_creation_input_tokens")},
            "cost_usd": None, "session_id": session_id,
            "raw": {"source": "codex_json", "usage_event": usage_src}}


# aider summary-line shapes — VERIFIED AT BUILD against aider 0.86.2 on the
# key-free Ollama path (plan Task 16). REALITY: on Ollama the summary line is
#   'Tokens: 735 sent, 1 received.'  (period-terminated, NO 'Cost:' clause at
# all — Ollama is free, so aider omits cost entirely -> cost_usd None). A paid
# provider adds '. Cost: $0.0141 message, $0.0141 session.' — the regexes
# tolerate both, plus thousands-commas and 'k' suffixes.
_AIDER_TOK = re.compile(
    r"Tokens:\s*([\d.,]+k?)\s*sent(?:.*?([\d.,]+k?)\s*received)?", re.I)
_AIDER_SESSION_COST = re.compile(r"\$\s*([\d.]+)\s*session", re.I)
_AIDER_STARTED = re.compile(
    r"^# aider chat started at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_AIDER_MODEL = re.compile(r"(?:Main model|Model):\s*(\S+)")  # newer aider: '> Main model: …'


def _ktoi(s):
    """'4.2k' -> 4200, '1,100' -> 1100; None on anything unparsable."""
    s = (s or "").replace(",", "").strip().lower()
    if not s:
        return None
    try:
        return int(float(s[:-1]) * 1000) if s.endswith("k") else int(float(s))
    except ValueError:
        return None


def _cost_aider_summary(stdout, stderr, extra):
    """aider prints per-exchange lines like
    'Tokens: 4.2k sent, 356 received. Cost: $0.0141 message, $0.0141 session.'
    On the key-free Ollama path the '. Cost: …' clause is ABSENT (verified at
    build against aider 0.86.2: 'Tokens: 735 sent, 1 received.') so cost_usd
    stays None. Sum sent/received across lines; the LAST '$… session' is the
    run cost when present. The whole stdout becomes result -> the human
    transcript lands in the console after the run (buffered; D1). No summary
    lines -> None (the raw stdout then falls through _job_finished's verbatim
    dump)."""
    text = (stdout or "") + "\n" + (stderr or "")
    tin = tout = None
    for m in _AIDER_TOK.finditer(text):
        a, b = _ktoi(m.group(1)), _ktoi(m.group(2))
        if a is not None:
            tin = (tin or 0) + a
        if b is not None:
            tout = (tout or 0) + b
    cost = None
    for m in _AIDER_SESSION_COST.finditer(text):
        try:
            cost = float(m.group(1))
        except ValueError:
            pass
    if tin is None and tout is None and cost is None:
        return None
    return {"result": (stdout or "").strip(),
            "usage": {"in": tin, "out": tout, "cache_r": None, "cache_w": None},
            "cost_usd": cost, "session_id": None,
            "raw": {"source": "aider_summary", "tok_in": tin, "tok_out": tout}}


COST_PARSERS = {
    "claude_json_array": _cost_claude_json_array,
    "codex_json": _cost_codex_json,
    "aider_summary": _cost_aider_summary,
    "none": _cost_none,
}


def extract_usage(adapter, stdout, stderr, extra=None):
    """Dispatch COST_PARSERS[adapter.cost_format]; unknown/missing format →
    the 'none' parser. Never raises."""
    fmt = str(((adapter or {}).get("cost_format")) or "none")
    fn = COST_PARSERS.get(fmt) or COST_PARSERS["none"]
    try:
        return fn(stdout or "", stderr or "", extra or {})
    except Exception:
        return None


# -------------------------------------------------------- transcript parsers
# Every parser: fn(path) -> telemetry dict with the P1 session_detail core
# shape: {counts, prompts, models, usage, tools, first_ts, last_ts,
# lines_parsed}. PURE + tolerant: unreadable/malformed input → empty dict.
# (server.session_detail adds the cached 'summary' block on top.)

def empty_telemetry():
    return {"counts": {}, "prompts": [], "models": {},
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0},
            "tools": {}, "first_ts": None, "last_ts": None, "lines_parsed": 0}


def real_user_text(obj):
    """Plain human prompt text from a Claude user-type line, else None
    (moved from server._real_user_text; server aliases it)."""
    if obj.get("type") != "user":
        return None
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        t = content.strip()
        if t and not t.startswith("<"):
            return t
    return None


def _tr_claude_jsonl(path, max_lines=MAX_TRANSCRIPT_LINES):
    """The moved session_detail() parsing core — identical field semantics."""
    d = empty_telemetry()
    counts, prompts, models = d["counts"], d["prompts"], d["models"]
    usage, tools = d["usage"], d["tools"]
    first_ts = last_ts = None
    n = 0
    try:
        f = open(path, "rb")
    except OSError:
        return d
    with f:
        for raw in f:
            n += 1
            if n > max_lines:
                break
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            t = obj.get("type", "?")
            counts[t] = counts.get(t, 0) + 1
            ts = obj.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            if t == "user":
                text = real_user_text(obj)
                if text and len(prompts) < MAX_PROMPTS_KEPT:
                    prompts.append({"ts": ts, "text": text[:PROMPT_EXCERPT_CHARS]})
            elif t == "assistant":
                msg = obj.get("message") or {}
                mdl = msg.get("model")
                if mdl:
                    models[mdl] = models.get(mdl, 0) + 1
                u = msg.get("usage") or {}
                for k in usage:
                    usage[k] += u.get(k) or 0
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        tools[name] = tools.get(name, 0) + 1
    d["first_ts"], d["last_ts"] = first_ts, last_ts
    d["lines_parsed"] = min(n, max_lines)
    return d


def _tr_codex_json(path, max_lines=MAX_TRANSCRIPT_LINES):
    """Codex session log (~/.codex/sessions/**/*.jsonl): one JSON object per
    line. VERIFIED AT BUILD (plan Task 11) against codex-cli 0.144.0-alpha.4:
    every line is {timestamp, type, payload}; user prompts are
    response_item/payload.role=='user' with content[].input_text; usage sits
    on an event_msg token_count at payload.info.total_token_usage (found by the
    recursive _find_usage scan, LAST wins — codex totals are cumulative);
    the served model is on turn_context/payload.model. Tolerant: malformed
    lines skipped; unreadable file → empty telemetry."""
    d = empty_telemetry()
    usage_src = None
    n = 0
    try:
        f = open(path, "rb")
    except OSError:
        return d
    with f:
        for raw in f:
            n += 1
            if n > max_lines:
                break
            try:
                obj = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            t = str(obj.get("type") or obj.get("record_type") or "?")
            d["counts"][t] = d["counts"].get(t, 0) + 1
            ts = obj.get("timestamp") or obj.get("ts")
            if isinstance(ts, str) and ts:
                d["first_ts"] = d["first_ts"] or ts
                d["last_ts"] = ts
            u = _find_usage(obj)
            if u:
                usage_src = u
            msg = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
            if msg.get("role") == "user":
                c = msg.get("content")
                text = c if isinstance(c, str) else (
                    " ".join(str(p.get("text") or "") for p in c
                             if isinstance(p, dict)).strip()
                    if isinstance(c, list) else None)
                if text and len(d["prompts"]) < MAX_PROMPTS_KEPT:
                    d["prompts"].append({"ts": d["last_ts"], "text": text[:PROMPT_EXCERPT_CHARS]})
            mdl = msg.get("model") or obj.get("model")
            if isinstance(mdl, str) and mdl:
                d["models"][mdl] = d["models"].get(mdl, 0) + 1
    if usage_src:
        u = usage_src
        d["usage"]["input_tokens"] = int(u.get("input_tokens") or 0)
        d["usage"]["output_tokens"] = int(u.get("output_tokens") or 0)
        cr = u.get("cache_read_input_tokens")
        if cr is None:
            cr = u.get("cached_input_tokens")
        d["usage"]["cache_read_input_tokens"] = int(cr or 0)
        d["usage"]["cache_creation_input_tokens"] = \
            int(u.get("cache_creation_input_tokens") or 0)
    d["lines_parsed"] = min(n, max_lines)
    return d


def _tr_aider_md(path, max_lines=MAX_TRANSCRIPT_LINES):
    """{cwd}/.aider.chat.history.md: '#### ' lines are user prompts,
    '> Tokens: …' lines carry usage, '# aider chat started at …' marks
    session boundaries, '> Model: …' names the model. VERIFIED AT BUILD
    against aider 0.86.2 (plan Task 16): the file's meta lines are '> '-quoted
    (e.g. '> Model: ollama/llama3.2 with whole edit format',
    '> Tokens: 735 sent, 1 received.') and the prompt is a bare '#### ' line.
    Tolerant: unrecognized lines are skipped; unreadable file -> empty."""
    d = empty_telemetry()
    n = 0
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return d
    with f:
        for line in f:
            n += 1
            if n > max_lines:
                break
            line = line.rstrip("\n")
            m = _AIDER_STARTED.match(line)
            if m:
                ts = m.group(1).replace(" ", "T")
                d["first_ts"] = d["first_ts"] or ts
                d["last_ts"] = ts
                d["counts"]["chat"] = d["counts"].get("chat", 0) + 1
                continue
            if line.startswith("#### "):
                t = line[5:].strip()
                if t and len(d["prompts"]) < MAX_PROMPTS_KEPT:
                    d["prompts"].append({"ts": d["last_ts"], "text": t[:PROMPT_EXCERPT_CHARS]})
                d["counts"]["user"] = d["counts"].get("user", 0) + 1
                continue
            if line.lstrip().startswith(">"):
                m = _AIDER_MODEL.search(line)
                if m:
                    d["models"][m.group(1)] = d["models"].get(m.group(1), 0) + 1
                m = _AIDER_TOK.search(line)
                if m:
                    d["usage"]["input_tokens"] += _ktoi(m.group(1)) or 0
                    d["usage"]["output_tokens"] += _ktoi(m.group(2)) or 0
                    d["counts"]["assistant"] = d["counts"].get("assistant", 0) + 1
    d["lines_parsed"] = min(n, max_lines)
    return d


TRANSCRIPT_PARSERS = {
    "claude_jsonl": _tr_claude_jsonl,
    "codex_json": _tr_codex_json,
    "aider_md": _tr_aider_md,
    "none": lambda path: empty_telemetry(),
}


def parse_transcript(fmt, path):
    """Dispatch TRANSCRIPT_PARSERS[fmt]; unknown fmt → empty telemetry.
    Never raises."""
    fn = TRANSCRIPT_PARSERS.get(str(fmt or "none")) or TRANSCRIPT_PARSERS["none"]
    try:
        return fn(path)
    except Exception:
        return empty_telemetry()


def transcript_format_for_path(path, agents=None):
    """(format, agent_id) of the manifest transcript glob matching `path`
    (fnmatch over the expanded glob, {enc_cwd}/{cwd} wildcarded; disabled
    agents included — format detection is orthogonal to enablement).
    Fallback ('claude_jsonl', 'claude') keeps pre-P2 behavior."""
    p = os.path.abspath(os.path.expanduser(str(path)))
    for a in (agents if agents is not None else load_agents()):
        tr = (a or {}).get("transcript") or {}
        g = tr.get("glob") or ""
        if not g:
            continue
        pat = os.path.expanduser(
            g.replace("{enc_cwd}", "*").replace("{cwd}", "*"))
        if fnmatch.fnmatch(p, pat):
            return tr.get("format") or "none", a.get("id") or ""
    return "claude_jsonl", "claude"


# ---------------------------------------------------------------- self-test

def _selftest():
    import tempfile
    global AGENTS_FILE

    # -- seeds + manifest loading (no file bound: seeds only) --
    AGENTS_FILE = None
    ags = load_agents()
    assert [a["id"] for a in ags] == ["claude", "codex", "aider"], [a["id"] for a in ags]
    assert ags[0]["enabled"] is True and ags[1]["enabled"] is False and ags[2]["enabled"] is False

    # -- no machine-specific junk in what we SHIP (a stranger's clone gets this) --
    blob = json.dumps(seed_agents())
    for bad in ("/Users/", "/home/", "192.168.", "10.0.", ".local:", "tailscale"):
        assert bad not in blob, "shipped defaults leak machine state: " + bad
    for a in seed_agents():                              # hints stay relative/standard
        for hint in a.get("bin_probe") or []:
            assert hint.startswith(("~", "/opt/", "/usr/", "/Applications/")), hint

    # -- first-boot seeding, reload, seed-merge, corrupt-file tolerance --
    base = tempfile.mkdtemp(prefix="za_selftest_")
    AGENTS_FILE = os.path.join(base, "agents.json")
    snap_file = os.path.join(base, SNAPSHOT_NAME)
    load_agents()                                        # seeds the file
    assert os.path.exists(AGENTS_FILE), "first boot writes data/agents.json"
    assert os.path.exists(snap_file), "first boot writes the seed snapshot"
    with open(AGENTS_FILE) as f:
        ondisk = json.load(f)
    assert [a["id"] for a in ondisk] == ["claude", "codex", "aider"]
    # snapshot is an OBJECT keyed by section (shared with providers), and our
    # writer must never stomp a section it does not own.
    with open(snap_file) as f:
        snap = json.load(f)
    assert isinstance(snap, dict) and [a["id"] for a in snap["agents"]] == \
        ["claude", "codex", "aider"], snap
    snap["providers"] = [{"id": "ollama", "host": "127.0.0.1"}]
    with open(snap_file, "w") as f:
        json.dump(snap, f)
    write_snapshot("agents", seed_agents())
    with open(snap_file) as f:
        assert json.load(f)["providers"][0]["id"] == "ollama", "providers section preserved"

    # pre-upgrade file: user has only claude, and the snapshot only knows claude
    # (codex/aider did not exist when it was written) -> the new ids are ADDED.
    with open(AGENTS_FILE, "w") as f:
        json.dump([a for a in ondisk if a["id"] == "claude"], f)
    with open(snap_file, "w") as f:
        json.dump({"agents": [a for a in ondisk if a["id"] == "claude"]}, f)
    assert {a["id"] for a in load_agents()} == {"claude", "codex", "aider"}, "seed-merge"
    # an id the user DELETED (present in the snapshot, absent from their file)
    # stays deleted — absence is a choice, not a gap.
    with open(AGENTS_FILE, "w") as f:
        json.dump([a for a in ondisk if a["id"] == "claude"], f)
    with open(snap_file, "w") as f:
        json.dump({"agents": ondisk}, f)
    assert {a["id"] for a in load_agents()} == {"claude"}, "deleted id not resurrected"
    with open(AGENTS_FILE, "w") as f:
        f.write("NOT JSON")
    assert [a["id"] for a in load_agents()] == ["claude", "codex", "aider"], "corrupt → seeds"
    with open(AGENTS_FILE) as f:                         # corrupt file is NOT clobbered
        assert f.read() == "NOT JSON", "corrupt file left untouched"
    AGENTS_FILE = None

    # -- three-way merge upgrade path (the point of the snapshot) --
    old = [{"id": "x", "label": "X", "enabled": False, "n": 1,
            "models": {"kind": "static", "list": ["old-id"]}}]
    new = [{"id": "x", "label": "X2", "enabled": False, "n": 1,
            "models": {"kind": "static", "list": ["new-id"]}, "added": True}]
    untouched = merge_by_id(new, json.loads(json.dumps(old)), old)[0]
    assert untouched["label"] == "X2", "untouched field upgrades"
    assert untouched["models"]["list"] == ["new-id"], "untouched nested field upgrades"
    assert untouched["added"] is True, "brand-new key adopted"
    edited = json.loads(json.dumps(old))
    edited[0]["label"] = "MINE"
    edited[0]["models"]["list"] = ["mine"]
    kept = merge_by_id(new, edited, old)[0]
    assert kept["label"] == "MINE", "customised field preserved"
    assert kept["models"]["list"] == ["mine"], "customised nested field preserved"
    assert kept["added"] is True, "new key still adopted alongside a customisation"
    # no snapshot (cold start on an existing install) -> user's values all win
    cold = merge_by_id(new, json.loads(json.dumps(old)), None)[0]
    assert cold["label"] == "X" and cold["models"]["list"] == ["old-id"], cold
    assert cold["added"] is True, "cold start still adopts brand-new keys"

    # -- resolve_models: confirmed-present vs merely-suggested --
    st = resolve_models({"models": {"kind": "static", "list": ["a", "b"]}})
    assert st == [{"id": "a", "confirmed": True, "source": "static"},
                  {"id": "b", "confirmed": True, "source": "static"}], st
    sg = resolve_models({"models": {"kind": "suggest", "list": ["a"]}})
    assert sg == [{"id": "a", "confirmed": False, "source": "suggest"}], sg
    det = {"models": {"kind": "detect", "source": "ollama_tags", "fallback": ["f"]}}
    got = resolve_models(det, {"ollama_tags": ["ollama/llama3.2"],
                               "openai_models": []})
    assert got == [{"id": "ollama/llama3.2", "confirmed": True,
                    "source": "ollama_tags"}], got
    assert resolve_models(det, {"ollama_tags": []}) == \
        [{"id": "f", "confirmed": False, "source": "fallback"}], "probe empty → fallback"
    assert resolve_models(det, None) == \
        [{"id": "f", "confirmed": False, "source": "fallback"}], "no detection → fallback"
    assert resolve_models({"models": {"kind": "detect", "source": "ollama_tags"}},
                          {}) == [], "no fallback → assert nothing"
    assert resolve_models({"models": {"list": ["a"]}})[0]["confirmed"] is True, \
        "missing kind → static (pre-existing hand-edited manifests keep working)"
    assert resolve_models({}) == [] and resolve_models(None) == []
    assert resolve_models({"models": "garbage"}) == [], "garbage manifest → []"
    # dual contract: dict for the UI/JSON, bare id for id-only consumers
    # (server.agent_models does [str(m) for m in resolve_models(...)]).
    assert [str(m) for m in st] == ["a", "b"], [str(m) for m in st]
    assert json.loads(json.dumps(st))[0] == {"id": "a", "confirmed": True,
                                             "source": "static"}
    # the shipped manifest honors the rule: never claim a local model exists
    sd = {a["id"]: a for a in seed_agents()}
    assert all(m["confirmed"] for m in resolve_models(sd["claude"])), "claude list is static"
    assert not any(m["confirmed"] for m in resolve_models(sd["codex"])), "codex is suggest"
    assert resolve_models(sd["aider"], {}) == [], "aider asserts no model on a fresh box"
    assert list_models(sd["aider"], {"ollama_tags": ["ollama/x"]}) == ["ollama/x"]

    # -- bin_probe '*' expansion (portable hints, no pinned python version) --
    pd = tempfile.mkdtemp(prefix="za_probe_")
    os.makedirs(os.path.join(pd, "Python", "3.13", "bin"))
    fake = os.path.join(pd, "Python", "3.13", "bin", "aider")
    with open(fake, "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(fake, 0o755)
    assert fake in _expand_probe(os.path.join(pd, "Python", "*", "bin", "aider"))
    assert _expand_probe("/nope/*/x") == [], "unreadable dir → no candidates"
    assert _expand_probe("/plain/path") == ["/plain/path"], "no '*' → passthrough"
    assert resolve_bin({"bin": "definitely-not-a-real-binary-zx9",
                        "bin_probe": [os.path.join(pd, "Python", "*", "bin", "aider")]}) \
        == fake, "glob hint resolves"

    # -- resolve_agent --
    assert resolve_agent("nope") is None, "unknown id"
    assert resolve_agent("codex") is None, "disabled → None"
    cl = resolve_agent("claude")
    assert cl and cl["bin_path"], cl

    # -- normalize_mode --
    assert normalize_mode(cl, "default") == []
    assert normalize_mode(cl, "acceptEdits") == ["--permission-mode", "acceptEdits"]
    assert normalize_mode(cl, "bypassPermissions") == ["--dangerously-skip-permissions"]
    assert normalize_mode(cl, "garbage") == [], "unknown mode → default flags"

    # -- garbage-manifest hardening (data/agents.json is a trust boundary;
    #    argv assembly must NEVER raise, mirroring the parsers' none-fallback) --
    assert normalize_mode({"mode_flags": ["x"]}, "default") == [], "list mode_flags → []"
    assert normalize_mode({"mode_flags": "nope"}, "default") == [], "str mode_flags → []"
    assert normalize_mode({"mode_flags": {"default": "x"}}, "default") == [], "str flags → []"
    assert normalize_mode({}, "default") == [], "missing mode_flags → []"
    bad = {"bin_path": "/x/bin", "argv": {"not": "a list"}, "mode_flags": ["bad"]}
    assert build_argv(bad, {"prompt": "p", "mode": "default"}) == ["/x/bin"], "dict argv → bin only"
    bad2 = {"bin_path": "/x/bin", "argv": 42}
    assert build_argv(bad2, {"prompt": "p"}) == ["/x/bin"], "non-iterable argv → bin only"

    # -- build_argv: claude argv list-parity with the pre-P2 _job_cmd --
    argv = build_argv(cl, {"prompt": "hello", "model": "sonnet", "mode": "default",
                           "budget": None, "add_dir": None})
    assert argv == [cl["bin_path"], "-p", "hello", "--model", "sonnet",
                    "--output-format", "json"], argv
    argv = build_argv(cl, {"prompt": "p", "model": "opus", "mode": "acceptEdits"})
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    argv = build_argv(cl, {"prompt": "p", "model": "sonnet",
                           "mode": "bypassPermissions", "budget": 5,
                           "add_dir": "/tmp/x"})
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--max-budget-usd") + 1] == "5", "H2: budget reaches CLI"
    assert argv[argv.index("--add-dir") + 1] == "/tmp/x"
    assert not wants_last_msg_file(cl)

    # -- build_argv: codex template (resolved by hand — seeds ship it disabled) --
    cx = dict(seed_agents()[1], bin_path="/usr/local/bin/codex")
    argv = build_argv(cx, {"prompt": "do it", "model": "gpt-5.5",
                           "mode": "default", "project": "/tmp/proj",
                           "_last_msg_file": "/tmp/last.txt"})
    assert argv[:2] == ["/usr/local/bin/codex", "exec"], argv[:2]
    assert argv[2] == "do it" and "--json" in argv
    assert argv[argv.index("--output-last-message") + 1] == "/tmp/last.txt"
    assert argv[argv.index("-C") + 1] == "/tmp/proj"
    assert argv[argv.index("--sandbox") + 1] == "read-only", "OQ5: default is read-only"
    assert wants_last_msg_file(cx)
    assert list_models(cx) and all(isinstance(m, str) for m in list_models(cx))

    # -- claude_envelope (moved _parse_envelope logic; same fixtures) --
    envd = {"type": "result", "result": "done", "session_id": "s1",
            "total_cost_usd": 0.12,
            "usage": {"input_tokens": 10, "output_tokens": 20,
                      "cache_creation_input_tokens": 5,
                      "cache_read_input_tokens": 7}}
    assert claude_envelope(json.dumps(envd))["total_cost_usd"] == 0.12
    arr = [{"type": "system", "subtype": "init"}, {"type": "assistant"}, envd]
    assert claude_envelope(json.dumps(arr))["session_id"] == "s1", "array envelope"
    assert claude_envelope(json.dumps([{"type": "system"}])) is None
    assert claude_envelope("warn line\n" + json.dumps(envd))["session_id"] == "s1"
    assert claude_envelope("total garbage") is None
    assert claude_envelope("") is None and claude_envelope(None) is None
    assert claude_envelope('{"a": 1}')["a"] == 1
    assert claude_envelope('{"a": 1}\njunk') is None

    # -- COST_PARSERS: claude normalization + dispatch --
    p = COST_PARSERS["claude_json_array"](json.dumps(arr), "", {})
    assert p["usage"] == {"in": 10, "out": 20, "cache_r": 7, "cache_w": 5}, p
    assert p["cost_usd"] == 0.12 and p["result"] == "done" and p["session_id"] == "s1"
    assert p["raw"]["type"] == "result"
    assert COST_PARSERS["claude_json_array"]("junk", "", {}) is None
    assert COST_PARSERS["none"]("whatever", "", {}) is None
    assert extract_usage(cl, json.dumps(arr), "", {})["cost_usd"] == 0.12
    assert extract_usage({"cost_format": "unregistered"}, "x", "", {}) is None
    assert extract_usage(None, "x", "", {}) is None

    # -- TRANSCRIPT_PARSERS: claude_jsonl core (A2) --
    td = tempfile.mkdtemp(prefix="za_tr_")
    tp = os.path.join(td, "sess.jsonl")
    with open(tp, "w") as f:
        f.write(json.dumps({"type": "user", "timestamp": "2026-07-11T00:00:00Z",
                            "message": {"content": "fix the bug"}}) + "\n")
        f.write("NOT JSON — must be skipped\n")
        f.write(json.dumps({"type": "assistant",
                            "timestamp": "2026-07-11T00:01:00Z",
                            "message": {"model": "claude-sonnet-5",
                                        "usage": {"input_tokens": 100,
                                                  "output_tokens": 50,
                                                  "cache_read_input_tokens": 5,
                                                  "cache_creation_input_tokens": 2},
                                        "content": [{"type": "tool_use",
                                                     "name": "Read"},
                                                    {"type": "text",
                                                     "text": "hi"}]}}) + "\n")
    d = TRANSCRIPT_PARSERS["claude_jsonl"](tp)
    assert d["usage"]["input_tokens"] == 100 and d["usage"]["output_tokens"] == 50
    assert d["usage"]["cache_read_input_tokens"] == 5
    assert d["models"] == {"claude-sonnet-5": 1} and d["tools"] == {"Read": 1}
    assert d["prompts"] == [{"ts": "2026-07-11T00:00:00Z", "text": "fix the bug"}]
    assert d["counts"] == {"user": 1, "assistant": 1}, d["counts"]
    assert d["first_ts"] == "2026-07-11T00:00:00Z"
    assert d["last_ts"] == "2026-07-11T00:01:00Z"
    assert d["lines_parsed"] == 3
    assert TRANSCRIPT_PARSERS["claude_jsonl"](os.path.join(td, "missing.jsonl")) \
        == empty_telemetry(), "unreadable → empty"
    assert parse_transcript("not-a-format", tp) == empty_telemetry()
    assert parse_transcript("none", tp) == empty_telemetry()
    assert real_user_text({"type": "user",
                           "message": {"content": "<cmd>x</cmd>"}}) is None

    # -- codex_json cost parser (A4; shapes verified at build, parser tolerant) --
    # Fixture mirrors a REAL codex exec --json stream: turn.completed carries
    # usage {input_tokens, cached_input_tokens, output_tokens,
    # reasoning_output_tokens} and NO cache_creation_input_tokens -> cache_w
    # None (verified against codex-cli 0.144.0-alpha.4 at build).
    lines = "\n".join([
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "hi"}}),
        "not json",
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 900, "cached_input_tokens": 400,
                              "output_tokens": 120}}),
    ])
    fd, lmf = tempfile.mkstemp(); os.close(fd)
    with open(lmf, "w") as f:
        f.write("FINAL ANSWER\n")
    p = COST_PARSERS["codex_json"](lines, "", {"last_msg_file": lmf})
    assert p["usage"] == {"in": 900, "out": 120, "cache_r": 400,
                          "cache_w": None}, p
    assert p["result"] == "FINAL ANSWER" and p["cost_usd"] is None
    assert p["raw"] and p["session_id"] is None
    os.unlink(lmf)
    p2 = COST_PARSERS["codex_json"](lines, "", {"last_msg_file": None})
    assert p2 and p2["usage"]["in"] == 900 and p2["result"] == ""
    assert COST_PARSERS["codex_json"]("no json here", "", {}) is None, \
        "malformed → None (graceful)"
    legacy = json.dumps({"id": "1", "msg": {"type": "token_count",
                         "info": {"total_token_usage": {"input_tokens": 10,
                                                        "output_tokens": 2}}}})
    p3 = COST_PARSERS["codex_json"](legacy, "", {})
    assert p3["usage"]["in"] == 10 and p3["usage"]["out"] == 2, "nested legacy shape"

    # -- codex transcript parser + format routing (A4; verify at build) --
    # Fixture mirrors the REAL ~/.codex/sessions rollout shape
    # (codex-cli 0.144.0-alpha.4): {timestamp,type,payload}; user prompts as
    # response_item/payload.role=='user'/content[].input_text; usage on an
    # event_msg token_count (payload.info.total_token_usage); model on
    # turn_context/payload.model.
    cp = os.path.join(td, "rollout-1.jsonl")
    with open(cp, "w") as f:
        f.write(json.dumps({"timestamp": "2026-07-11T01:00:00Z",
                            "type": "response_item",
                            "payload": {"role": "user",
                                        "content": [{"type": "input_text",
                                                     "text": "build it"}]}}) + "\n")
        f.write("junk line\n")
        f.write(json.dumps({"timestamp": "2026-07-11T01:02:00Z",
                            "type": "event_msg",
                            "payload": {"model": "gpt-5.5",
                                        "usage": {"input_tokens": 300,
                                                  "output_tokens": 40}}}) + "\n")
    d = TRANSCRIPT_PARSERS["codex_json"](cp)
    assert d["usage"]["input_tokens"] == 300 and d["usage"]["output_tokens"] == 40
    assert d["prompts"] and d["prompts"][0]["text"] == "build it"
    assert d["models"].get("gpt-5.5") == 1
    assert d["first_ts"] == "2026-07-11T01:00:00Z"
    assert d["last_ts"] == "2026-07-11T01:02:00Z"
    assert TRANSCRIPT_PARSERS["codex_json"](os.path.join(td, "nope.jsonl")) \
        == empty_telemetry()
    fmt, aid = transcript_format_for_path(
        os.path.expanduser("~/.claude/projects/-x/abc.jsonl"))
    assert (fmt, aid) == ("claude_jsonl", "claude"), (fmt, aid)
    fmt, aid = transcript_format_for_path(
        os.path.expanduser("~/.codex/sessions/2026/07/11/rollout-1.jsonl"))
    assert (fmt, aid) == ("codex_json", "codex"), (fmt, aid)
    fmt, aid = transcript_format_for_path(
        "/Users/x/claudeProjects/foo/.aider.chat.history.md")
    assert (fmt, aid) == ("aider_md", "aider"), (fmt, aid)
    assert transcript_format_for_path("/random/file.txt")[0] == "claude_jsonl", \
        "unknown path → claude fallback (pre-P2 behavior)"
    # -- aider_summary cost parser (A5; VERIFIED AT BUILD, aider 0.86.2) --
    # First fixture: a PAID-provider shape (has the '. Cost: …' clause) so we
    # exercise cost parsing + multi-line token summation.
    out = ("Aider v0.86.1\nModel: ollama/qwen2.5-coder with diff edit format\n"
           "wrote src/x.py\n"
           "Tokens: 4.2k sent, 356 received. Cost: $0.0141 message, $0.0141 session.\n"
           "Tokens: 1,100 sent, 44 received. Cost: $0.0032 message, $0.0173 session.\n")
    p = COST_PARSERS["aider_summary"](out, "", {})
    assert p["usage"]["in"] == 5300 and p["usage"]["out"] == 400, p["usage"]
    assert p["cost_usd"] == 0.0173, "last session cost wins"
    assert p["result"].startswith("Aider"), "whole stdout becomes the console result (D1)"
    assert COST_PARSERS["aider_summary"]("no summary lines at all", "", {}) is None
    assert COST_PARSERS["aider_summary"]("", "", {}) is None
    # REALITY fixture: the EXACT summary line aider 0.86.2 printed on the
    # key-free Ollama path — period-terminated, NO 'Cost:' clause -> usage
    # books, cost_usd stays None (Ollama is free).
    real = ("Aider v0.86.2\nModel: ollama/llama3.2 with whole edit format\n"
            "OK\n\nTokens: 735 sent, 1 received.\n")
    rp = COST_PARSERS["aider_summary"](real, "", {})
    assert rp["usage"]["in"] == 735 and rp["usage"]["out"] == 1, rp["usage"]
    assert rp["cost_usd"] is None, "Ollama = no cost clause -> None (verified)"
    assert rp["result"].startswith("Aider"), "whole stdout -> console (D1)"

    # -- aider_md transcript parser (A5; VERIFIED AT BUILD, aider 0.86.2) --
    # Fixture mirrors the REAL .aider.chat.history.md: '# aider chat started
    # at …' header, '> Model: …' + '> Tokens: …' quoted meta lines, and a bare
    # '#### ' user prompt (all confirmed against aider 0.86.2 on Ollama).
    ap = os.path.join(td, ".aider.chat.history.md")
    with open(ap, "w") as f:
        f.write("# aider chat started at 2026-07-11 09:00:00\n\n")
        f.write("> Model: ollama/qwen2.5-coder with diff edit format\n\n")
        f.write("#### make the tests pass\n\n")
        f.write("I'll fix the imports.\n\n")
        f.write("> Tokens: 2.5k sent, 300 received. Cost: $0.01 message, $0.01 session.\n")
    d = TRANSCRIPT_PARSERS["aider_md"](ap)
    assert d["counts"].get("user") == 1 and d["counts"].get("chat") == 1, d["counts"]
    assert d["prompts"] == [{"ts": "2026-07-11T09:00:00",
                             "text": "make the tests pass"}]
    assert d["usage"]["input_tokens"] == 2500 and d["usage"]["output_tokens"] == 300
    assert d["models"].get("ollama/qwen2.5-coder") == 1, d["models"]
    assert d["first_ts"] == "2026-07-11T09:00:00"
    assert TRANSCRIPT_PARSERS["aider_md"](os.path.join(td, "no.md")) \
        == empty_telemetry(), "unreadable -> empty (graceful)"
    # REALITY fixture: bytes exactly as aider 0.86.2 wrote them on Ollama —
    # '> '-quoted meta, trailing markdown '  ' line breaks, period-only Tokens.
    rp2 = os.path.join(td, ".aider.real.history.md")
    with open(rp2, "w") as f:
        f.write("\n# aider chat started at 2026-07-11 10:22:59\n\n")
        f.write("> Model: ollama/llama3.2 with whole edit format  \n")
        f.write("> Git repo: .git with 1 files  \n")
        f.write("> Added hello.py to the chat.  \n\n")
        f.write("#### Reply with the single word OK. Do not edit any files.  \n\n")
        f.write("OK\n\n")
        f.write("> Tokens: 735 sent, 1 received.  \n")
    rd = _tr_aider_md(rp2)
    assert rd["counts"].get("user") == 1, rd["counts"]
    assert rd["prompts"][0]["text"] == "Reply with the single word OK. Do not edit any files."
    assert rd["usage"]["input_tokens"] == 735 and rd["usage"]["output_tokens"] == 1, rd["usage"]
    assert rd["models"].get("ollama/llama3.2") == 1, rd["models"]
    assert rd["first_ts"] == "2026-07-11T10:22:59"
    assert transcript_format_for_path(rp2)  # smoke: routing tolerates real name
    # newer aider emits '> Main model: …' (not the standalone '> Model: …');
    # capture it, but never mis-count '> Weak model:'/'> Git repo:' as the model.
    assert _AIDER_MODEL.search("> Main model: ollama/qwen3 with diff edit format").group(1) == "ollama/qwen3"
    assert _AIDER_MODEL.search("> Model: ollama/llama3.2 with whole edit format").group(1) == "ollama/llama3.2"
    assert _AIDER_MODEL.search("> Weak model: ollama/mini") is None, "weak model is not the primary"
    assert _AIDER_MODEL.search("> Git repo: .git with 1 files") is None, "no false model match"

    # --- (later phases append their asserts above this line) ---
    print("agents self-test OK")


if __name__ == "__main__":
    _selftest()
