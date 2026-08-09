# `defaults/` — what ZENITH ships

Everything in this directory is **shipped default configuration**: tracked in git,
diffable, reviewable in a PR, and **never written at runtime**. It is the answer to
"what does a stranger get when they clone this repo?"

```
defaults/*.json      TRACKED. What ships. Never written at runtime.
      │
      ▼  seed-merge on first boot / on upgrade
data/*.json          GITIGNORED. This machine's state. User edits always win.
      │
      ▼  env override
ZENITH_* env         Highest precedence.
```

The rule that falls out of this: **`defaults/` describes the software, `data/`
describes your machine.** A LAN hostname, an API key, which models you actually
pulled, which agents you enabled — all machine state, all `data/`. If a value would
be wrong on someone else's laptop, it does not belong in this directory.

| File | Holds |
|---|---|
| `agents.json` | agent manifests: binary + probe hints, argv templates, mode flags, model sources, parser format keys |

## Editing

**Do not edit `defaults/` to change your own setup** — your edit will be an unwanted
diff in git, and the merge below can overwrite it on the next upgrade anyway. Edit
`data/agents.json` instead; that file always wins.

Edit `defaults/agents.json` when you are changing **what ships to everyone**: adding
a new agent, correcting a model id, fixing an argv template.

## The three-way merge (why your edits survive upgrades)

On boot, `load_agents()` merges three things:

| Input | What it is |
|---|---|
| `defaults/agents.json` | the new shipped values |
| `data/agents.json` | your current values |
| `data/.seed-snapshot.json` → `"agents"` | the values we shipped *last* time |

Per field, per agent:

```
your value == last shipped value  ->  take the NEW shipped value   (you never touched it)
your value != last shipped value  ->  keep YOUR value              (you customised it)
```

So a corrected model id reaches every install that never edited that field, and never
touches an install that did. New agent ids are added; an agent id you **deleted** from
`data/agents.json` stays deleted (absence is a choice, not a gap).

The old merge keyed on id membership alone — it could only ever add whole entries, so a
shipped correction reached exactly the users who did not have that agent yet. Everyone
else kept a stale copy forever with no signal it was stale.

**Cold start** (upgrading an install that has `data/agents.json` but no snapshot yet):
nothing is known to be untouched, so **every** existing field of yours wins and the
snapshot is written for next time. Your machine's behaviour does not change on the
upgrade that introduces this.

`.seed-snapshot.json` is shared with other config sections and is an object keyed by
section — `{"providers": [...], "agents": [...]}`. Each writer updates only its own key.
Deleting the snapshot is safe: the next boot rebuilds it and, until then, all your
values win.

## Model lists: `models.kind`

**Never claim a model exists.** A fresh install shows what it can *detect*; anything it
cannot verify is a visibly-distinct suggestion. `resolve_models(adapter, detected)`
returns `[{"id", "confirmed", "source"}]`, and the UI greys out `confirmed: false`.

| `kind` | Shape | Meaning |
|---|---|---|
| `static` | `{"kind": "static", "list": [...]}` | Shipped list, asserted present. For a CLI with no models endpoint (`claude`) — nothing to probe, the list *is* the fact. |
| `suggest` | `{"kind": "suggest", "list": [...]}` | Shipped list, never asserted. Shown greyed; picking one is what triggers "install/configure that?". |
| `detect` | `{"kind": "detect", "source": "ollama_tags", "fallback": []}` | Probed at runtime. If the probe finds nothing, `fallback` is offered as suggestions. |

Detection sources (`server.py` performs the I/O — `zenith_agents.py` is stdlib-only and
does no HTTP; it receives `detected` as `{source: [ids]}`):

| `source` | Probe | Covers |
|---|---|---|
| `ollama_tags` | `GET {base}/api/tags` | Ollama |
| `openai_models` | `GET {base}/v1/models` | vLLM, llama.cpp server, LM Studio, LiteLLM — anything OpenAI-compatible |

A missing `kind` is treated as `static`, so a hand-edited manifest that predates the
field keeps working.

## Adding a new agent

Append an object to `defaults/agents.json`. It will appear on every install at the next
boot without disturbing existing entries.

```jsonc
{
  "id": "myagent",                 // stable key — the merge and the UI both key on it
  "label": "My Agent",
  "enabled": false,                // ship OFF; boot probe / the user turns it on
  "bin": "myagent",                // resolved via PATH first (shutil.which)
  "bin_probe": [                   // HINTS only, used when PATH misses.
    "~/.local/bin/myagent",        //   '~' expands; '*' matches one path component
    "/opt/homebrew/bin/myagent",   //   (e.g. "~/Library/Python/*/bin/aider")
    "/usr/local/bin/myagent"
  ],
  "exec_subcommand": null,         // e.g. "exec" for codex; null for none
  "argv": ["--message", "{prompt}", "--model", "{model}", "{mode_flags}"],
  "budget_flag": null,             // appended with the value when the job sets one
  "add_dir_flag": null,
  "models": {"kind": "suggest", "list": ["some-model"]},
  "mode_flags": {                  // ZENITH's normalized modes -> this CLI's flags
    "default": [],
    "acceptEdits": ["--yes"],
    "bypassPermissions": ["--yes", "--dangerous"]
  },
  "env": {},                       // extra env for the child process
  "cost_format": "none",           // key into COST_PARSERS (code, not config)
  "transcript": {"glob": "{cwd}/.myagent.history", "format": "none", "pick": "path"},
  "auth": "external",
  "capabilities": {"json_output": false, "cost": false, "transcript": false},
  "reviewer_ok": false             // may this agent review another agent's work?
}
```

`argv` placeholders: `{prompt}`, `{model}`, `{cwd}`, `{last_msg_file}` substitute into a
single token; `{mode_flags}` splices in the list for the job's mode.

`cost_format` and `transcript.format` are **keys into parser registries in
`zenith_agents.py`** — a new value there needs a matching parser function, so it is code,
not configuration. `"none"` is always valid and yields no cost / empty telemetry.

Any `*_comment` key is free-form documentation for whoever opens the file (JSON has no
comments); nothing reads them.

After editing, run `python3 zenith_agents.py` — the self-test parses the shipped
defaults and asserts they carry no machine-specific paths or hosts.
