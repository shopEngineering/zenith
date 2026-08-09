"""zenith_store.py — ZENITH/OS event store (SQLite spine).

Phase-1 observability: append-only events + satellites (verdicts, session
snapshots, primitive scores, gates). Boundary rule (binding): this module
imports ONLY sqlite3/json/os/time/threading/datetime. No HTTP, no subprocess,
no JOBS, no data/*.json. server.py imports this as `zs`.

Concurrency: WAL + short-lived per-operation connections + busy_timeout=5000.
emit() NEVER raises — on failure the record spills to events_failed.jsonl.
Self-test: `python3 zenith_store.py` → "self-test OK".
"""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta

_DB = None        # str path; set by init_db()
_FAILED = None    # <data dir>/events_failed.jsonl spill target

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id       INTEGER PRIMARY KEY,
  ts       TEXT NOT NULL,
  kind     TEXT NOT NULL,
  project  TEXT DEFAULT '',
  actor    TEXT NOT NULL DEFAULT 'user',
  ref      TEXT DEFAULT '',
  outcome  TEXT DEFAULT '',
  tok_in   INTEGER, tok_out INTEGER,
  tok_cr   INTEGER, tok_cw  INTEGER,
  cost_usd REAL,
  data     TEXT NOT NULL DEFAULT '{}',
  agent    TEXT DEFAULT 'claude'
);
CREATE INDEX IF NOT EXISTS ix_ev_kind ON events(kind, id);
CREATE INDEX IF NOT EXISTS ix_ev_proj ON events(project, id);
CREATE INDEX IF NOT EXISTS ix_ev_ref  ON events(ref);
-- ix_ev_agent is created by the v<2 migration (D3), not here: on a legacy
-- v1 DB the events table has no `agent` column yet when _SCHEMA runs, so an
-- index on events(agent,id) here would raise before the ALTER can add it.

CREATE TABLE IF NOT EXISTS verdicts(
  id          INTEGER PRIMARY KEY,
  created     TEXT NOT NULL,
  target_kind TEXT NOT NULL,
  target_ref  TEXT NOT NULL,
  project     TEXT DEFAULT '',
  verify_job  TEXT DEFAULT '',
  model       TEXT DEFAULT '',
  verdict     TEXT NOT NULL,
  summary     TEXT DEFAULT '',
  issues      TEXT NOT NULL DEFAULT '[]',
  mech        TEXT NOT NULL DEFAULT '{}',
  crit INTEGER DEFAULT 0, major INTEGER DEFAULT 0,
  minor INTEGER DEFAULT 0, info INTEGER DEFAULT 0,
  tok_in INTEGER, tok_out INTEGER, cost_usd REAL,
  event_id INTEGER,
  agent    TEXT DEFAULT 'claude'
);
CREATE INDEX IF NOT EXISTS ix_vd_target ON verdicts(target_kind, target_ref, id);

CREATE TABLE IF NOT EXISTS session_snapshots(
  id        INTEGER PRIMARY KEY,
  ts        TEXT NOT NULL,
  session   TEXT NOT NULL,
  project   TEXT DEFAULT '',
  mtime     REAL NOT NULL, size INTEGER NOT NULL,
  tok_in INTEGER, tok_out INTEGER, tok_cr INTEGER, tok_cw INTEGER,
  models    TEXT DEFAULT '{}',
  tools     TEXT DEFAULT '{}',
  counts    TEXT DEFAULT '{}',
  prompts_n INTEGER, first_ts TEXT, last_ts TEXT,
  agent     TEXT DEFAULT 'claude'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_snap ON session_snapshots(session, mtime, size);
CREATE INDEX IF NOT EXISTS ix_snap_proj ON session_snapshots(project, id);

CREATE TABLE IF NOT EXISTS primitive_scores(
  n INTEGER PRIMARY KEY,
  name     TEXT NOT NULL,
  status   TEXT NOT NULL,
  note     TEXT DEFAULT '',
  evidence TEXT DEFAULT '[]',
  probe    TEXT DEFAULT '',
  updated  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gates(
  id       INTEGER PRIMARY KEY,
  ts       TEXT NOT NULL,
  action   TEXT NOT NULL,
  route    TEXT NOT NULL,
  op       TEXT NOT NULL,
  blast    TEXT NOT NULL,
  rev      TEXT NOT NULL,
  level    TEXT NOT NULL,
  decision TEXT NOT NULL,
  project  TEXT DEFAULT '',
  detail   TEXT DEFAULT '{}',
  token_hash TEXT DEFAULT '',
  event_id INTEGER
);

CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

# n, name, seed status, evidence pointers, probe key (§6 of the design contract)
SCORECARD_SEED = [
    (1,  "metadata-first tool registry",                 "partial", [{"claim": "APPS registry carries id/name/icon metadata", "pointer": "static/apps.js APPS"}], ""),
    (2,  "tiered permissions",                           "partial", [{"claim": "job modes default/acceptEdits/bypass", "pointer": "server.py spawn_job mode"}], "gates_30d"),
    (3,  "crash-surviving session persistence",          "partial", [{"claim": "tmux workspace manifest rebuild", "pointer": "server.py _reconstruct_workspace"}], ""),
    (4,  "workflow != conversation state",               "partial", [{"claim": "loops/swarms configs separate from chats", "pointer": "data/loops.json"}], ""),
    (5,  "token budget + accounting",                    "present", [{"claim": "job.end carries usage columns + cost from --output-format json", "pointer": "server.py _job_finished / _job_cmd --max-budget-usd"}], "cost_jobs_30d"),
    (6,  "structured streaming events",                  "absent",  [{"claim": "stream-json deferred by design", "pointer": "design §12"}], ""),
    (7,  "separate system event log",                    "present", [{"claim": "append-only events table + /api/events", "pointer": "zenith_store.py events / server.py /api/events"}], "events_24h"),
    (8,  "two-level verification",                       "present", [{"claim": "hostile ringer verdicts + HARNESS_INVARIANTS probes", "pointer": "server.py spawn_verify / verdicts table"}], "verdicts_30d"),
    (9,  "dynamic tool-pool assembly",                   "partial", [{"claim": "skills injected per job", "pointer": "server.py spawn_job skills"}], ""),
    (10, "context compaction + persistence",             "na",      [{"claim": "delegated to Claude CLI", "pointer": "design §6"}], ""),
    (11, "permission audit trail as queryable object",   "present", [{"claim": "gate.decision + job.spawn(mode) events queryable via /api/gates + /api/events", "pointer": "server.py _gate_check / GATE_RULES"}], "permaudit_30d"),
    (12, "typed hard-scoped agent roles",                "partial", [{"claim": "agents dir + per-job mode scoping", "pointer": "server.py list_agents"}], ""),
]


def _now_iso():
    """Match server.py now_iso(): ISO-8601 UTC."""
    return datetime.now(timezone.utc).isoformat()


def _conn():
    c = sqlite3.connect(_DB, timeout=5)
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    return c


def init_db(path):
    """Bootstrap + migrate + seed + import; idempotent (PRAGMA user_version)."""
    global _DB, _FAILED
    _DB = str(path)
    _FAILED = os.path.join(os.path.dirname(_DB), "events_failed.jsonl")
    c = sqlite3.connect(_DB, timeout=5)
    try:
        c.execute("PRAGMA journal_mode=WAL")          # persistent in the file
        c.execute("PRAGMA busy_timeout=5000")
        v = c.execute("PRAGMA user_version").fetchone()[0]
        c.executescript(_SCHEMA)                       # all IF NOT EXISTS → self-heals
        if not c.execute("SELECT 1 FROM primitive_scores LIMIT 1").fetchone():
            now = _now_iso()
            c.executemany(
                "INSERT OR IGNORE INTO primitive_scores(n,name,status,note,evidence,probe,updated)"
                " VALUES(?,?,?,?,?,?,?)",
                [(n, name, status, "", json.dumps(ev), probe, now)
                 for n, name, status, ev, probe in SCORECARD_SEED])
        _import_loop_runs(c)                           # Task 8 (no-op until then)
        if v < 1:
            c.execute("PRAGMA user_version=1")
        if v < 2:      # P2: the agent dimension (D3). ALTER backfills 'claude'
            for tbl in ("events", "verdicts", "session_snapshots"):
                try:
                    c.execute(f"ALTER TABLE {tbl}"
                              f" ADD COLUMN agent TEXT DEFAULT 'claude'")
                except sqlite3.OperationalError:
                    pass       # fresh DB: _SCHEMA already created the column
            c.execute("CREATE INDEX IF NOT EXISTS ix_ev_agent ON events(agent, id)")
            c.execute("PRAGMA user_version=2")
        # future migrations: if v < 3: ...
        c.commit()
    finally:
        c.close()


def _import_loop_runs(c):
    """§2.5 one-time fold-in of data/loop_runs.jsonl (the DB's sibling file):
    each line becomes a job.end event (+ a paired loop.run). Guarded by
    meta.imported_loop_runs. The old file stays on disk, never read again.
    Uses the bootstrap connection directly (emit() would open a second
    connection against our open write txn)."""
    if c.execute("SELECT 1 FROM meta WHERE k='imported_loop_runs'").fetchone():
        return
    src = os.path.join(os.path.dirname(_DB), "loop_runs.jsonl")
    if os.path.exists(src):
        with open(src, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                lid = str(r.get("loop_id") or "")
                actor = f"loop:{lid}" if lid else "zenith"
                ended = r.get("ended") or r.get("started") or _now_iso()
                out = "ok" if r.get("status") in ("done", "ok") else (
                    "killed" if r.get("status") == "stopped" else "error")
                data = {"loop_id": lid, "job_id": r.get("job_id"),
                        "output_tail": (r.get("tail") or "")[:4000],
                        "imported": True, "started": r.get("started"),
                        "ended": r.get("ended"), "status": r.get("status"),
                        "provider": r.get("provider"), "model": r.get("model"),
                        "envelope_ok": False}
                c.execute("INSERT INTO events(ts,kind,project,actor,ref,outcome,data)"
                          " VALUES(?,?,?,?,?,?,?)",
                          (ended, "job.end", "", actor,
                           str(r.get("job_id") or ""), out,
                           json.dumps(data, default=str)))
                c.execute("INSERT INTO events(ts,kind,project,actor,ref,outcome,data)"
                          " VALUES(?,?,?,?,?,?,?)",
                          (ended, "loop.run", "", actor, lid,
                           "ok" if out == "ok" else "error",
                           json.dumps({"job_id": r.get("job_id"), "name": "",
                                       "imported": True}, default=str)))
    c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('imported_loop_runs','1')")


# ---------------------------------------------------------------- emit

def emit(kind, project="", ref="", outcome="", data=None,
         tokens=None, cost=None, actor="user", agent="claude"):
    """Append one spine event. tokens=(in,out,cache_r,cache_w). agent tags the
    runner dimension (meaningful on job./verify./session. kinds; other kinds
    carry the default). NEVER raises: on any failure the record spills
    (best-effort) to events_failed.jsonl and None is returned."""
    rec = {"ts": _now_iso(), "kind": str(kind), "project": str(project or ""),
           "actor": str(actor or "user"), "ref": str(ref or ""),
           "outcome": str(outcome or ""), "agent": str(agent or "claude")}
    try:
        rec["data"] = json.dumps(data or {}, default=str)
    except Exception:
        rec["data"] = "{}"
    ti = to = tcr = tcw = None
    if tokens:
        try:
            ti, to, tcr, tcw = (list(tokens) + [None] * 4)[:4]
        except Exception:
            pass
    try:
        c = _conn()
        try:
            cur = c.execute(
                "INSERT INTO events(ts,kind,project,actor,ref,outcome,"
                "tok_in,tok_out,tok_cr,tok_cw,cost_usd,data,agent)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec["ts"], rec["kind"], rec["project"], rec["actor"], rec["ref"],
                 rec["outcome"], ti, to, tcr, tcw, cost, rec["data"],
                 rec["agent"]))
            c.commit()
            return cur.lastrowid
        finally:
            c.close()
    except Exception:
        try:
            if _FAILED:
                with open(_FAILED, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass
        return None


# ---------------------------------------------------------------- queries

def events_query(kind=None, project=None, outcome=None, ref=None,
                 q=None, before=None, limit=100, agent=None):
    """Timeline query, id-desc keyset (before = last seen id). kind ending in
    '.' is a family prefix (kind LIKE 'job.%'); q is a LIKE over ref+data."""
    limit = max(1, min(int(limit or 100), 200))
    where, args = [], []
    if kind:
        if str(kind).endswith("."):
            where.append("kind LIKE ?"); args.append(str(kind) + "%")
        else:
            where.append("kind = ?"); args.append(str(kind))
    if project:
        where.append("project = ?"); args.append(str(project))
    if outcome:
        where.append("outcome = ?"); args.append(str(outcome))
    if agent:
        where.append("agent = ?"); args.append(str(agent))
    if ref:
        where.append("ref = ?"); args.append(str(ref))
    if q:
        where.append("(ref LIKE ? OR data LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if before:
        where.append("id < ?"); args.append(int(before))
    sql = ("SELECT * FROM events"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY id DESC LIMIT ?")
    c = _conn()
    try:
        rows = [dict(r) for r in c.execute(sql, args + [limit])]
    finally:
        c.close()
    for r in rows:
        try:
            r["data"] = json.loads(r["data"])
        except (ValueError, TypeError):
            r["data"] = {}
    return rows


def events_stats(days=30, project=None):
    """Daily cost/token/job-count series + by-kind + by-model + totals.
    NULL usage sums as 0 (absence of evidence is surfaced by the UI as '—',
    not here)."""
    days = max(1, min(int(days or 30), 365))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    pw, pa = ("AND project = ?", [str(project)]) if project else ("", [])
    c = _conn()
    try:
        day_rows = c.execute(
            f"SELECT substr(ts,1,10) d, COALESCE(SUM(cost_usd),0) cost,"
            f" COALESCE(SUM(tok_in),0) tok_in, COALESCE(SUM(tok_out),0) tok_out,"
            f" COALESCE(SUM(kind='job.end'),0) jobs"
            f" FROM events WHERE ts >= ? {pw} GROUP BY d ORDER BY d",
            [cutoff] + pa).fetchall()
        by_kind = dict(c.execute(
            f"SELECT kind, COUNT(*) FROM events WHERE ts >= ? {pw} GROUP BY kind",
            [cutoff] + pa).fetchall())
        by_model = c.execute(
            f"SELECT COALESCE(json_extract(data,'$.model'),'?') model, COUNT(*) jobs,"
            f" COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(tok_in),0) tok_in,"
            f" COALESCE(SUM(tok_out),0) tok_out"
            f" FROM events WHERE kind='job.end' AND ts >= ? {pw}"
            f" GROUP BY model ORDER BY cost DESC", [cutoff] + pa).fetchall()
        by_project = c.execute(
            f"SELECT project, COUNT(*) jobs, COALESCE(SUM(cost_usd),0) cost,"
            f" COALESCE(SUM(tok_in),0) tok_in, COALESCE(SUM(tok_out),0) tok_out"
            f" FROM events WHERE kind='job.end' AND ts >= ? {pw}"
            f" GROUP BY project ORDER BY cost DESC", [cutoff] + pa).fetchall()
        tot = c.execute(
            f"SELECT COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(tok_in),0) tok_in,"
            f" COALESCE(SUM(tok_out),0) tok_out, COALESCE(SUM(kind='job.end'),0) jobs"
            f" FROM events WHERE ts >= ? {pw}", [cutoff] + pa).fetchone()
    finally:
        c.close()
    return {"days": [dict(r) for r in day_rows], "by_kind": by_kind,
            "by_model": [dict(r) for r in by_model],
            "by_project": [dict(r) for r in by_project], "totals": dict(tot)}


# ---------------------------------------------------------------- meta + sweeps

def meta_get(k, default=None):
    c = _conn()
    try:
        r = c.execute("SELECT v FROM meta WHERE k=?", (str(k),)).fetchone()
        return r["v"] if r else default
    finally:
        c.close()


def meta_set(k, v):
    c = _conn()
    try:
        c.execute("INSERT INTO meta(k,v) VALUES(?,?)"
                  " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(k), str(v)))
        c.commit()
    finally:
        c.close()


def orphan_sweep():
    """Close job.spawn events that never got a job.end (server died mid-run):
    synthesize job.end outcome=orphaned. Returns the number closed. Run at boot
    (§3 #1), when no jobs can be running."""
    c = _conn()
    try:
        rows = c.execute(
            "SELECT ref, project, data FROM events e"
            " WHERE kind='job.spawn' AND ref != ''"
            " AND NOT EXISTS (SELECT 1 FROM events x"
            "                 WHERE x.kind='job.end' AND x.ref = e.ref)").fetchall()
    finally:
        c.close()
    n = 0
    for r in rows:
        try:
            d = json.loads(r["data"])
        except (ValueError, TypeError):
            d = {}
        eid = emit("job.end", project=r["project"], ref=r["ref"], outcome="orphaned",
                   actor="zenith", agent=d.get("agent") or "claude",
                   data={"orphaned": True, "status": "orphaned",
                         "model": d.get("model"), "mode": d.get("mode"),
                         "label": d.get("label"), "loop_id": d.get("loop_id"),
                         "agent": d.get("agent") or "claude",
                         "envelope_ok": False})
        if eid:
            n += 1
    return n


# ---------------------------------------------------------------- session snapshots

_SNAP_COLS = ("ts", "session", "project", "mtime", "size", "tok_in", "tok_out",
              "tok_cr", "tok_cw", "models", "tools", "counts", "prompts_n",
              "first_ts", "last_ts", "agent")


def snapshot_insert(**cols):
    """INSERT OR IGNORE on ux_snap(session,mtime,size) → False if duplicate."""
    cols.setdefault("ts", _now_iso())
    for k in ("models", "tools", "counts"):
        if k in cols and not isinstance(cols[k], str):
            cols[k] = json.dumps(cols[k] or {}, default=str)
    keys = [k for k in _SNAP_COLS if k in cols]
    c = _conn()
    try:
        cur = c.execute(
            f"INSERT OR IGNORE INTO session_snapshots({','.join(keys)})"
            f" VALUES({','.join('?' * len(keys))})", [cols[k] for k in keys])
        c.commit()
        return cur.rowcount == 1
    finally:
        c.close()


def snapshot_sigs():
    """{session: (mtime, size)} of the latest snapshot per session."""
    c = _conn()
    try:
        return {r["session"]: (r["mtime"], r["size"]) for r in c.execute(
            "SELECT session, mtime, size FROM session_snapshots"
            " WHERE id IN (SELECT MAX(id) FROM session_snapshots GROUP BY session)")}
    finally:
        c.close()


def telemetry_sessions(project=None, days=30, agent=None):
    """Raw snapshot series for trends; the UI diffs consecutive rows."""
    days = max(1, min(int(days or 30), 365))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    pw, pa = ("AND project = ?", [str(project)]) if project else ("", [])
    if agent:
        pw += " AND agent = ?"; pa.append(str(agent))
    c = _conn()
    try:
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM session_snapshots WHERE ts >= ? {pw}"
            f" ORDER BY session, id", [cutoff] + pa)]
    finally:
        c.close()
    for r in rows:
        for k in ("models", "tools", "counts"):
            try:
                r[k] = json.loads(r[k] or "{}")
            except (ValueError, TypeError):
                r[k] = {}
    return rows


def session_token_stats(days=30, project=None):
    """Token totals across interactive sessions — the LATEST snapshot per session (dedup),
    grouped by project + agent, with overall totals. Complements events_stats() (headless jobs)."""
    days = max(1, min(int(days or 30), 365))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    pw, pa = ("AND project = ?", [str(project)]) if project else ("", [])
    c = _conn()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT session, project, agent, tok_in, tok_out, tok_cr, tok_cw FROM session_snapshots"
            " WHERE id IN (SELECT MAX(id) FROM session_snapshots WHERE ts >= ? " + pw + " GROUP BY session)",
            [cutoff] + pa)]
    finally:
        c.close()
    TK = ("tok_in", "tok_out", "tok_cr", "tok_cw")
    totals = {"sessions": len(rows), **{k: 0 for k in TK}}
    byp, bya = {}, {}
    for r in rows:
        for k in TK:
            totals[k] += int(r[k] or 0)
        d = byp.setdefault(r["project"] or "(unknown)",
                           {"project": r["project"] or "(unknown)", "sessions": 0, **{k: 0 for k in TK}})
        a = bya.setdefault(r["agent"] or "claude",
                           {"agent": r["agent"] or "claude", "sessions": 0, **{k: 0 for k in TK}})
        for tgt in (d, a):
            tgt["sessions"] += 1
            for k in TK:
                tgt[k] += int(r[k] or 0)
    tot = lambda d: sum(d[k] for k in TK)
    return {"days": days, "totals": totals,
            "by_project": sorted(byp.values(), key=tot, reverse=True),
            "by_agent": sorted(bya.values(), key=tot, reverse=True)}


# ---------------------------------------------------------------- verdicts

_VD_COLS = ("created", "target_kind", "target_ref", "project", "verify_job",
            "model", "verdict", "summary", "issues", "mech", "crit", "major",
            "minor", "info", "tok_in", "tok_out", "cost_usd", "event_id",
            "agent")
_VD_LIST_COLS = ("id,created,target_kind,target_ref,project,verify_job,model,"
                 "verdict,summary,crit,major,minor,info,tok_in,tok_out,"
                 "cost_usd,event_id,agent")


def verdict_insert(**cols):
    cols.setdefault("created", _now_iso())
    if not isinstance(cols.get("issues"), str):
        cols["issues"] = json.dumps(cols.get("issues") or [], default=str)
    if not isinstance(cols.get("mech"), str):
        cols["mech"] = json.dumps(cols.get("mech") or {}, default=str)
    keys = [k for k in _VD_COLS if k in cols]
    c = _conn()
    try:
        cur = c.execute(f"INSERT INTO verdicts({','.join(keys)})"
                        f" VALUES({','.join('?' * len(keys))})",
                        [cols[k] for k in keys])
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def verdict_get(vid):
    """Full row, issues + mech parsed; None if absent."""
    c = _conn()
    try:
        r = c.execute("SELECT * FROM verdicts WHERE id=?", (int(vid),)).fetchone()
    finally:
        c.close()
    if not r:
        return None
    v = dict(r)
    for k, dflt in (("issues", []), ("mech", {})):
        try:
            v[k] = json.loads(v[k])
        except (ValueError, TypeError):
            v[k] = dflt
    return v


def verdicts_query(target_kind=None, target_ref=None, limit=50, agent=None):
    """Verdict list without issue/mech bodies (row chips + list views)."""
    limit = max(1, min(int(limit or 50), 200))
    where, args = [], []
    if target_kind:
        where.append("target_kind = ?"); args.append(str(target_kind))
    if target_ref:
        where.append("target_ref = ?"); args.append(str(target_ref))
    if agent:
        where.append("agent = ?"); args.append(str(agent))
    sql = (f"SELECT {_VD_LIST_COLS} FROM verdicts"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY id DESC LIMIT ?")
    c = _conn()
    try:
        return [dict(r) for r in c.execute(sql, args + [limit])]
    finally:
        c.close()


def verdicts_latest_for(refs):
    """{ref: {id,verdict,crit,major,minor}} — latest verdict per target_ref,
    for job-row chips. refs is a list of job ids."""
    refs = [str(r) for r in (refs or []) if r]
    if not refs:
        return {}
    ph = ",".join("?" * len(refs))
    c = _conn()
    try:
        rows = c.execute(
            f"SELECT target_ref, id, verdict, crit, major, minor FROM verdicts"
            f" WHERE target_ref IN ({ph})"
            f" AND id IN (SELECT MAX(id) FROM verdicts GROUP BY target_ref)",
            refs).fetchall()
    finally:
        c.close()
    return {r["target_ref"]: {"id": r["id"], "verdict": r["verdict"],
                              "crit": r["crit"], "major": r["major"],
                              "minor": r["minor"]} for r in rows}


def verdict_set_event(vid, event_id):
    """Backfill verdicts.event_id after the verify.end event is emitted."""
    c = _conn()
    try:
        c.execute("UPDATE verdicts SET event_id=? WHERE id=?", (event_id, int(vid)))
        c.commit()
    finally:
        c.close()


# ---------------------------------------------------------------- gates

_GATE_COLS = ("ts", "action", "route", "op", "blast", "rev", "level",
              "decision", "project", "detail", "token_hash", "event_id")


def gate_insert(**cols):
    cols.setdefault("ts", _now_iso())
    if not isinstance(cols.get("detail"), str):
        cols["detail"] = json.dumps(cols.get("detail") or {}, default=str)
    keys = [k for k in _GATE_COLS if k in cols]
    c = _conn()
    try:
        cur = c.execute(f"INSERT INTO gates({','.join(keys)})"
                        f" VALUES({','.join('?' * len(keys))})",
                        [cols[k] for k in keys])
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def gates_query(limit=100):
    limit = max(1, min(int(limit or 100), 500))
    c = _conn()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM gates ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        c.close()
    for r in rows:
        try:
            r["detail"] = json.loads(r["detail"])
        except (ValueError, TypeError):
            r["detail"] = {}
    return rows


def gate_set_event(gid, event_id):
    c = _conn()
    try:
        c.execute("UPDATE gates SET event_id=? WHERE id=?", (event_id, int(gid)))
        c.commit()
    finally:
        c.close()


# ---------------------------------------------------------------- scorecard

_PROBES = {
    # probe key -> (COUNT sql with one ? cutoff param, window days)
    "gates_30d":     ("SELECT COUNT(*) FROM gates WHERE ts >= ?", 30),
    "cost_jobs_30d": ("SELECT COUNT(*) FROM events WHERE kind='job.end'"
                      " AND cost_usd IS NOT NULL AND ts >= ?", 30),
    "events_24h":    ("SELECT COUNT(*) FROM events WHERE ts >= ?", 1),
    "verdicts_30d":  ("SELECT COUNT(*) FROM verdicts WHERE created >= ?", 30),
    "permaudit_30d": ("SELECT COUNT(*) FROM events WHERE"
                      " kind IN ('gate.decision','job.spawn') AND ts >= ?", 30),
}


def score_all():
    """12 rows, evidence parsed, plus probe_n = live count for the row's probe
    (None when the row has no probe). Status is judgment; counts are evidence."""
    c = _conn()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM primitive_scores ORDER BY n")]
        for r in rows:
            try:
                r["evidence"] = json.loads(r["evidence"])
            except (ValueError, TypeError):
                r["evidence"] = []
            p = _PROBES.get(r.get("probe") or "")
            if p:
                cutoff = (datetime.now(timezone.utc)
                          - timedelta(days=p[1])).isoformat()
                r["probe_n"] = c.execute(p[0], (cutoff,)).fetchone()[0]
            else:
                r["probe_n"] = None
        return rows
    finally:
        c.close()


def score_set(n, status, note, evidence):
    """Hand-flip a primitive's status. evidence=None preserves the stored
    evidence; a list replaces it."""
    sets, args = ["status=?", "note=?", "updated=?"], [str(status), str(note or ""),
                                                       _now_iso()]
    if evidence is not None:
        sets.append("evidence=?")
        args.append(evidence if isinstance(evidence, str)
                    else json.dumps(evidence, default=str))
    args.append(int(n))
    c = _conn()
    try:
        c.execute(f"UPDATE primitive_scores SET {', '.join(sets)} WHERE n=?", args)
        c.commit()
    finally:
        c.close()


# ---------------------------------------------------------------- self-test

def _selftest():
    base = os.path.join(os.environ.get("TMPDIR") or "/tmp",
                        "zs_selftest_%d" % os.getpid())
    os.makedirs(base, exist_ok=True)
    dbp = os.path.join(base, "zenith.db")
    for f in os.listdir(base):
        os.remove(os.path.join(base, f))

    with open(os.path.join(base, "loop_runs.jsonl"), "w") as f:
        f.write(json.dumps({"loop_id": "lpA", "job_id": "j_old1",
                            "started": "2026-01-01T00:00:00+00:00",
                            "ended": "2026-01-01T00:01:00+00:00",
                            "status": "done", "tail": "old tail"}) + "\n")
        f.write("NOT JSON — must be skipped\n")
        f.write(json.dumps({"loop_id": "lpA", "job_id": None, "runner": "provider",
                            "provider": "ollama", "model": "m1",
                            "started": "2026-01-02T00:00:00+00:00",
                            "ended": "2026-01-02T00:00:30+00:00",
                            "status": "error", "tail": "boom"}) + "\n")

    # -- bootstrap idempotence --
    init_db(dbp)
    init_db(dbp)
    c = sqlite3.connect(dbp)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2, "user_version must be 2"
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"events", "verdicts", "session_snapshots", "primitive_scores",
            "gates", "meta"} <= names, f"missing tables: {names}"
    assert c.execute("SELECT COUNT(*) FROM primitive_scores").fetchone()[0] == 12, "seed rows"
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal", "WAL"
    c.close()

    # -- emit + concurrency: 8 threads x 50 events --
    def worker(t):
        for i in range(50):
            emit("test.tick", project="/tmp/projA", ref=f"t{t}-{i}", outcome="ok",
                 data={"i": i}, tokens=(10, 20, 30, 40), cost=0.001, actor="user")
    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    c = sqlite3.connect(dbp)
    n = c.execute("SELECT COUNT(*) FROM events WHERE kind='test.tick'").fetchone()[0]
    assert n == 400, f"concurrency: expected 400 events, got {n}"
    row = c.execute("SELECT tok_in,tok_out,tok_cr,tok_cw,cost_usd,actor,project"
                    " FROM events WHERE kind='test.tick' LIMIT 1").fetchone()
    assert row == (10, 20, 30, 40, 0.001, "user", "/tmp/projA"), f"columns: {row}"
    c.close()
    eid = emit("test.one", ref="r1", data={"k": "v"})
    assert isinstance(eid, int), "emit returns rowid"

    # -- keyset pagination + filters --
    for i in range(25):
        emit("page.test", project="/tmp/projB", ref=f"p{i}", outcome="ok", data={"i": i})
    p1 = events_query(kind="page.test", limit=10)
    assert len(p1) == 10 and p1[0]["id"] > p1[-1]["id"], "id-desc ordering"
    p2 = events_query(kind="page.test", before=p1[-1]["id"], limit=10)
    p3 = events_query(kind="page.test", before=p2[-1]["id"], limit=10)
    ids = [e["id"] for e in p1 + p2 + p3]
    assert len(ids) == 25 and len(set(ids)) == 25, f"keyset walk: {len(ids)}/{len(set(ids))}"
    assert all(e["data"]["i"] is not None for e in p1), "data parsed to dict"
    assert len(events_query(kind="page.", limit=100)) == 25, "kind-prefix family filter"
    assert len(events_query(project="/tmp/projB", limit=100)) == 25, "project filter"
    assert len(events_query(ref="p3", limit=100)) == 1, "ref filter"
    assert len(events_query(q="p3", kind="page.test", limit=100)) >= 1, "q LIKE over ref+data"
    assert len(events_query(kind="page.test", outcome="ok", limit=5)) == 5, "outcome filter"

    # -- events_stats --
    emit("job.end", project="/tmp/projB", ref="jstat1", outcome="ok",
         tokens=(100, 200, 0, 0), cost=0.5, data={"model": "sonnet"})
    emit("job.end", project="/tmp/projB", ref="jstat2", outcome="ok",
         tokens=(50, 60, 0, 0), cost=0.25, data={"model": "opus"})
    st = events_stats(days=7)
    assert set(st) == {"days", "by_kind", "by_model", "by_project", "totals"}, f"stats keys: {set(st)}"
    assert st["totals"]["jobs"] >= 2 and st["totals"]["cost"] >= 0.75, st["totals"]
    assert st["by_kind"].get("job.end", 0) >= 2, st["by_kind"]
    models = {m["model"] for m in st["by_model"]}
    assert {"sonnet", "opus"} <= models, models
    assert st["days"] and st["days"][-1]["d"] == _now_iso()[:10], "daily bucket"
    assert events_stats(days=7, project="/tmp/nope")["totals"]["jobs"] == 0, "project scoping"
    assert any(p["project"] == "/tmp/projB" for p in st["by_project"]), st.get("by_project")

    # -- meta --
    assert meta_get("nothing", "dflt") == "dflt"
    meta_set("k1", "v1"); meta_set("k1", "v2")
    assert meta_get("k1") == "v2", "meta upsert"

    # -- orphan sweep: job.spawn without job.end -> synthetic orphaned job.end --
    emit("job.spawn", project="/tmp/projB", ref="orphan1", outcome="ok",
         data={"model": "sonnet", "mode": "default", "label": "L", "loop_id": None})
    emit("job.spawn", project="/tmp/projB", ref="paired1", outcome="ok",
         data={"model": "sonnet", "mode": "default"})
    emit("job.end", project="/tmp/projB", ref="paired1", outcome="ok", data={})
    n_orph = orphan_sweep()
    assert n_orph == 1, f"expected 1 orphan closed, got {n_orph}"
    orphs = events_query(kind="job.end", ref="orphan1")
    assert len(orphs) == 1 and orphs[0]["outcome"] == "orphaned", orphs
    assert orphs[0]["actor"] == "zenith" and orphs[0]["data"]["model"] == "sonnet"
    assert orphan_sweep() == 0, "second sweep is a no-op"

    # -- snapshot dedup on (session, mtime, size) --
    s = "/tmp/tr/x.jsonl"
    a = snapshot_insert(session=s, project="/tmp/projB", mtime=1.0, size=10,
                        tok_in=1, tok_out=2, tok_cr=3, tok_cw=4,
                        models={"m": 1}, tools={"Read": 2}, counts={"user": 1},
                        prompts_n=1, first_ts="t0", last_ts="t1")
    b = snapshot_insert(session=s, project="/tmp/projB", mtime=1.0, size=10)
    assert a is True and b is False, f"dedup: {a},{b}"
    assert snapshot_insert(session=s, project="/tmp/projB", mtime=2.0, size=20) is True
    sigs = snapshot_sigs()
    assert sigs[s] == (2.0, 20), f"latest sig wins: {sigs.get(s)}"
    series = telemetry_sessions(project="/tmp/projB", days=30)
    mine = [r for r in series if r["session"] == s]
    assert len(mine) == 2 and mine[0]["id"] < mine[1]["id"], "series ordered per session"
    assert mine[0]["models"] == {"m": 1}, "models JSON parsed"

    # -- verdicts --
    vid = verdict_insert(target_kind="job", target_ref="jstat1", project="/tmp/projB",
                         verify_job="vjob1", model="opus", verdict="warn",
                         summary="meh", issues=[{"severity": "minor", "claim": "c",
                                                 "evidence": "e", "location": "l"}],
                         mech={"rc": 0}, crit=0, major=0, minor=1, info=0,
                         tok_in=5, tok_out=6, cost_usd=0.02)
    assert isinstance(vid, int)
    v = verdict_get(vid)
    assert v["verdict"] == "warn" and v["issues"][0]["severity"] == "minor"
    assert v["mech"] == {"rc": 0}, v["mech"]
    assert verdict_get(999999) is None
    lst = verdicts_query(target_kind="job", target_ref="jstat1")
    assert len(lst) == 1 and "issues" not in lst[0], "list omits issue bodies"
    vid2 = verdict_insert(target_kind="job", target_ref="jstat1", verdict="pass",
                          summary="", issues=[], mech={})
    latest = verdicts_latest_for(["jstat1", "nope"])
    assert latest["jstat1"]["id"] == vid2 and latest["jstat1"]["verdict"] == "pass"
    assert "nope" not in latest
    verdict_set_event(vid, 12345)
    assert verdict_get(vid)["event_id"] == 12345

    # -- gates --
    gid = gate_insert(action="jobs.spawn.bypass", route="/api/jobs", op="execute",
                      blast="system", rev="irreversible", level="confirm",
                      decision="prompted", project="/tmp/projB",
                      detail={"mode": "bypassPermissions"}, token_hash="ab" * 8)
    assert isinstance(gid, int)
    gl = gates_query(limit=10)
    assert gl[0]["id"] == gid and gl[0]["detail"]["mode"] == "bypassPermissions"
    gate_set_event(gid, 54321)
    assert gates_query(limit=1)[0]["event_id"] == 54321

    # -- scorecard --
    rows = score_all()
    assert len(rows) == 12 and rows[0]["n"] == 1
    assert isinstance(rows[0]["evidence"], list), "evidence parsed"
    assert rows[6]["probe"] == "events_24h" and isinstance(rows[6]["probe_n"], int)
    assert rows[6]["probe_n"] > 0, "live probe counts today's events"
    assert rows[0]["probe_n"] is None, "no probe -> None"
    score_set(7, "present", "shipped in P1", [{"claim": "events table live",
                                               "pointer": "zenith_store.py"}])
    r7 = [r for r in score_all() if r["n"] == 7][0]
    assert r7["status"] == "present" and r7["note"] == "shipped in P1"
    score_set(7, "present", "note only", None)      # evidence=None keeps prior evidence
    r7b = [r for r in score_all() if r["n"] == 7][0]
    assert r7b["evidence"] == r7["evidence"], "None evidence preserved"

    # -- loop_runs.jsonl import: once, guarded by meta --
    assert meta_get("imported_loop_runs") == "1", "import flag set"
    imp = events_query(kind="job.end", q="lpA", limit=50)
    imp = [e for e in imp if (e["data"] or {}).get("loop_id") == "lpA"]
    assert len(imp) == 2, f"2 imported job.end rows, got {len(imp)}"
    assert all(e["data"].get("imported") for e in imp)
    assert {e["outcome"] for e in imp} == {"ok", "error"}, "status→outcome mapping"
    assert all(e["actor"] == "loop:lpA" for e in imp)
    lruns = events_query(kind="loop.run", ref="lpA", limit=50)
    assert len(lruns) == 2, "paired loop.run events"
    init_db(dbp)   # third bootstrap: import must NOT double
    imp2 = [e for e in events_query(kind="job.end", q="lpA", limit=50)
            if (e["data"] or {}).get("loop_id") == "lpA"]
    assert len(imp2) == 2, "meta flag prevents re-import"

    # -- agent dimension (P2 A3) --
    c = sqlite3.connect(dbp)
    for tbl in ("events", "verdicts", "session_snapshots"):
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({tbl})")}
        assert "agent" in cols, f"{tbl} missing agent column"
    c.close()
    emit("job.end", ref="agjob1", outcome="ok", agent="codex",
         data={"agent": "codex"})
    assert events_query(ref="agjob1")[0]["agent"] == "codex"
    assert events_query(ref="jstat1")[0]["agent"] == "claude", "default agent"
    got = events_query(kind="job.end", agent="codex", limit=200)
    assert got and all(e["agent"] == "codex" for e in got), "agent filter"
    snapshot_insert(session="/tmp/tr/cx.jsonl", project="/tmp/projB",
                    mtime=1.0, size=5, agent="codex")
    tel = telemetry_sessions(project="/tmp/projB", days=30, agent="codex")
    assert tel and all(r["agent"] == "codex" for r in tel), "snapshot agent filter"
    vidx = verdict_insert(target_kind="job", target_ref="agjob1", verdict="pass",
                          summary="", issues=[], mech={}, agent="codex")
    assert verdict_get(vidx)["agent"] == "codex"
    assert verdicts_query(agent="codex")[0]["agent"] == "codex"
    # ALTER-path migration: a pre-P2 (user_version=1) DB gains the columns
    old = os.path.join(base, "old.db")
    oc = sqlite3.connect(old)
    oc.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, ts TEXT NOT NULL,"
               " kind TEXT NOT NULL, project TEXT DEFAULT '',"
               " actor TEXT NOT NULL DEFAULT 'user', ref TEXT DEFAULT '',"
               " outcome TEXT DEFAULT '', tok_in INTEGER, tok_out INTEGER,"
               " tok_cr INTEGER, tok_cw INTEGER, cost_usd REAL,"
               " data TEXT NOT NULL DEFAULT '{}')")
    oc.execute("INSERT INTO events(ts,kind)"
               " VALUES('2026-01-01T00:00:00+00:00','job.end')")
    oc.execute("PRAGMA user_version=1")
    oc.commit(); oc.close()
    init_db(old)
    oc = sqlite3.connect(old)
    assert oc.execute("SELECT agent FROM events").fetchone()[0] == "claude", \
        "ALTER backfills 'claude' on legacy rows"
    assert oc.execute("PRAGMA user_version").fetchone()[0] == 2
    oc.close()
    init_db(dbp)   # re-bind _DB to the main self-test store

    print("self-test OK")


if __name__ == "__main__":
    _selftest()
