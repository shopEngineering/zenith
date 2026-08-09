#!/usr/bin/env bash
# Stand up a ZENITH/OS GPU worker node on a Linux box in one command.
#
#   ./install-gpu-worker.sh
#
# Installs gpu_node.py, registers it as a systemd USER service (Restart=always,
# linger-enabled so it survives reboot without a login), starts it, and health-checks
# it. Idempotent — re-run to update the code or refresh the unit.
#
# Env (all optional):
#   ZENITH_GPU_PORT   port to bind          (default 8811)
#   ZENITH_GPU_BIND   bind address          (default 0.0.0.0 — LAN-reachable)
#   ZENITH_GPU_PY     python with torch+CUDA (default: autodetect vllm-env, else python3)
#   ZENITH_GPU_DIR    install dir           (default ~/.zenith-gpu)
#   ZENITH_GPU_IMAGE  docker image for engine=docker jobs
#                                           (default nvcr.io/nvidia/pytorch:26.05-py3)
#
# After it prints the node URL, register the node on the ZENITH control plane by
# adding it to data/gpu_nodes.json there:  {"<name>": "http://<this-ip>:<port>"}
set -euo pipefail

PORT="${ZENITH_GPU_PORT:-8811}"
BIND="${ZENITH_GPU_BIND:-0.0.0.0}"
DIR="${ZENITH_GPU_DIR:-$HOME/.zenith-gpu}"
IMAGE="${ZENITH_GPU_IMAGE:-nvcr.io/nvidia/pytorch:26.05-py3}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gpu_node.py"
UNIT="zenith-gpu-node.service"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT"

if [ "$(id -u)" = "0" ]; then
  echo "Run this as your normal user, not root (it installs a systemd --user service)." >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd (systemctl) not found. Run the node manually instead:" >&2
  echo "  ZENITH_GPU_PORT=$PORT python3 $SRC" >&2
  exit 1
fi

# --- pick a python that has torch+CUDA ------------------------------------
pick_py() {
  if [ -n "${ZENITH_GPU_PY:-}" ]; then echo "$ZENITH_GPU_PY"; return; fi
  for c in "$HOME/vllm-env/bin/python" "$HOME/.venv/bin/python" "$(command -v python3 || true)"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    if "$c" -c 'import torch' >/dev/null 2>&1; then echo "$c"; return; fi
  done
  command -v python3 || echo /usr/bin/python3   # fall back; warn below
}
PY="$(pick_py)"

echo "==> Installing GPU worker"
echo "    python:  $PY"
echo "    dir:     $DIR"
echo "    port:    $PORT   bind: $BIND"

if ! "$PY" -c 'import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)' >/dev/null 2>&1; then
  echo "    WARNING: $PY has no torch+CUDA — venv-engine jobs will error." >&2
  echo "             Set ZENITH_GPU_PY to a torch env, or use docker-engine jobs." >&2
fi

mkdir -p "$DIR" "$UNIT_DIR"
install -m 0755 "$SRC" "$DIR/gpu_node.py"

cat > "$UNIT_PATH" <<UNIT_EOF
[Unit]
Description=ZENITH/OS GPU worker node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PY $DIR/gpu_node.py
Environment=ZENITH_GPU_PORT=$PORT
Environment=ZENITH_GPU_BIND=$BIND
Environment=ZENITH_GPU_IMAGE=$IMAGE
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
UNIT_EOF

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "$USER" >/dev/null 2>&1 \
    && echo "    linger enabled — starts at boot without login." \
    || echo "    note: could not enable linger; node starts on your next login."
fi

if command -v docker >/dev/null 2>&1 && ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  echo "    note: $USER is not in the 'docker' group — docker-engine jobs will fail" >&2
  echo "          until you add it:  sudo usermod -aG docker $USER  (then re-login)." >&2
fi

echo "==> Health check"
ok=""
for i in $(seq 1 15); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ -z "$ok" ]; then
  echo "    node did not answer on :$PORT — check: journalctl --user -u $UNIT -n 40" >&2
  exit 1
fi
curl -s "http://127.0.0.1:$PORT/gpu" | "$PY" -c 'import sys,json; d=json.load(sys.stdin); print("    gpu:", d.get("gpu"))' 2>/dev/null || true

# best-effort LAN IP for the register hint
IP="$(hostname -I 2>/dev/null | awk "{print \$1}")"; IP="${IP:-<this-host-ip>}"
echo "==> Node up: http://$IP:$PORT"
echo "    Register it on the ZENITH control plane — add to data/gpu_nodes.json:"
echo "      {\"$(hostname -s)\": \"http://$IP:$PORT\"}"
echo "    Logs:  journalctl --user -u $UNIT -f"
