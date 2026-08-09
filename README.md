# ZENITH/OS

A browser-based agentic operations platform — mission control for everything around your
coding-agent sessions. Real terminals, session history, background jobs, local models, and a
window manager, in one page served by a single Python file with **zero dependencies**.

The job runner is **provider-agnostic**: Claude Code, Codex, and aider are all first-class
agents (launch / cost / verify / telemetry), and the **A/B** app runs one prompt across
several of them to compare head to head.

> Everything runs on your machine. The server binds `127.0.0.1`, there is no telemetry, and
> no code or transcript leaves the box.

## Install

**macOS / Linux / WSL — one command**

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/shopEngineering/zenith/main/get.sh)
```

Detects your OS, installs missing dependencies (`python3`, `tmux`, `git`, `ffmpeg`) with your
package manager, fetches ZENITH to `~/.zenith`, and sets up auto-start so it survives a reboot.
Re-run any time to update. Use the `bash <(…)` form rather than `curl … | bash` so it can
prompt for `sudo` when a package manager needs it.

**From a checkout**

```bash
./install.sh     # one-time: a `zenith` command + auto-start (opt out: ZENITH_NO_AUTOSTART=1)
zenith           # start the server and open it
```

Then `zenith start | stop | restart | status | update | logs | autostart | autostart-off`.

**Or just run it**

```bash
python3 server.py          # stdlib only, no pip install
# → http://127.0.0.1:8777  (or ZENITH_PORT=xxxx)
```

**Windows** — run inside WSL (recommended, and how Claude Code runs best on Windows), or
natively with `pip install pywinpty` for the terminal. See [docs/WINDOWS.md](docs/WINDOWS.md).

The one thing you install yourself is your agent CLI and its login — that can't be bundled.
Get Claude Code at <https://claude.com/claude-code>.

## What's in it

**Terminals** — real PTYs in the browser (xterm.js over a stdlib WebSocket bridge). Launch a
shell or an interactive agent in any project directory, with optional tmux persistence: closing
a window detaches, and re-attaching replays scrollback. File paths and URLs in output are
clickable. Drag-select copies to the system clipboard, ⌘V pastes, and pasting an image hands
the agent a file path so it can read it. Each terminal has voice-to-text (browser recogniser or
local Whisper).

**Sessions** — every agent transcript, filtered by project, with token telemetry and a live
tail. Sessions running right now are marked as such, can be brought to the front, and can be
ended from the list. A prompt-history panel shows what you actually asked, so you never scroll
the transcript back to find it.

**Projects · Docs · Memory** — your project folders with git/spec markers, a markdown browser,
and full-text search over a memory store.

**Run** — headless coding jobs against a project (pick agent, model, mode, budget; watch live
output), scheduled recurring loops, and a multi-agent swarm.

**Lab** — A/B one prompt across N agents and models with hostile verification and an LLM judge;
dispatch GPU benchmarks to LAN nodes.

**Models · Agents** — local and remote providers (Ollama and OpenAI-compatible), chat with a
stats toggle and **constrained generation**: set rules (exact word counts, valid JSON, regex,
contains) and the reply is checked in code and retried until they hold.

**Activity** — dashboard, event timeline, cost, verification verdicts, and a risk-gate log.

**The desktop itself** — a real window manager: tiling, virtual desktops, per-window rename,
collapse-to-strip, a ⌘K command palette over apps/projects/sessions/memory, themes with a custom
accent hue, and an opt-in ambient effects layer (aurora, orbiting solar system, distant nebulae,
constellations, code rain, radar) with per-effect controls. All of it off by default.

## Configuration

ZENITH runs standalone out of the box — a fresh clone boots to a core desktop with no dead tabs
for services you don't have. Five optional integrations (memory, watchers, homelab, voice,
fleet/GPU) auto-detect at startup and appear only when found; force any of them on or off in
**Settings → Integrations**, or edit `data/config.json`. Environment variables always win over
the config file, which wins over the built-in defaults. A first-run wizard walks a fresh install
through detection.

**Security.** The server binds `127.0.0.1`. `bind` is configurable only via `data/config.json`
or `ZENITH_BIND` — deliberately not in Settings — because binding off loopback exposes an
unauthenticated server. Only do it on a network you trust. `/api/file` is restricted to your
projects and agent directories, and all rendered content is escaped client-side.

## Requirements

`python3` (3.9+), `tmux`, `git`. `ffmpeg` is optional and only used for voice. There are **no
Python dependencies** — the server is stdlib-only, and the frontend has no build step.

## Platforms

macOS and Linux run natively. Windows runs under WSL, or natively with `pywinpty` for the
terminal. Everything except the in-OS terminal is cross-platform with no extra install: the
terminal uses the stdlib pty on Unix and ConPTY on Windows.

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, ship it.
