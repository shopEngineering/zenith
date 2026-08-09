# ZENITH/OS — GPU worker node

Add a GPU box to the fleet in one command. ZENITH/OS is the control plane; a
worker node just runs a small set of **predefined** GPU benchmarks (matmul /
bandwidth / inference) that the **Fleet GPU** app dispatches to it. There is no
arbitrary remote exec — the node only knows how to run the three burns in
`gpu_node.py`.

## Install (on the GPU box)

```bash
git clone https://github.com/shopEngineering/zenith        # or copy this folder over
cd zenith-os/packaging/gpu-worker
./install-gpu-worker.sh
```

It installs `gpu_node.py`, registers a systemd **user** service
(`zenith-gpu-node.service`, `Restart=always`, linger-enabled so it comes back
after a reboot without a login), starts it, and health-checks it. Re-run any time
to update. The script prints the node URL and the exact line to register it.

Knobs (all optional, via env):

| Env | Default | Meaning |
|-----|---------|---------|
| `ZENITH_GPU_PORT` | `8811` | bind port |
| `ZENITH_GPU_BIND` | `0.0.0.0` | bind address (LAN-reachable) |
| `ZENITH_GPU_PY` | autodetect | python with torch+CUDA (tries `~/vllm-env`, `~/.venv`, then `python3`) |
| `ZENITH_GPU_DIR` | `~/.zenith-gpu` | install dir |
| `ZENITH_GPU_IMAGE` | `nvcr.io/nvidia/pytorch:26.05-py3` | image for `engine=docker` jobs |

## Register it on the control plane

On the machine running ZENITH/OS, add the node to `data/gpu_nodes.json`:

```json
{ "gpu-node": "http://<node-ip>:8811" }
```

Then the **Fleet GPU** app lists it and can dispatch jobs. (`gpu_nodes.json` maps
`name → base URL`; the ZENITH server proxies `/api/gpu/*` to these over the LAN.)

## Endpoints (what the control plane calls)

| Route | Returns |
|-------|---------|
| `GET /health` | `{ok, host, torch}` |
| `GET /gpu` | `{gpu: "<name> · <util>% · <temp>°C · <power>W", host}` (via `nvidia-smi`) |
| `GET /job?type=&engine=&n=&secs=` | per-type metrics (below) |

`type` ∈ `matmul | bandwidth | inference`; `engine` ∈ `venv | docker`.

- **matmul** → `{tflops, matmuls, n, seconds, gpu}` (bf16 `n×n`)
- **bandwidth** → `{gb_per_s, iters, seconds, gpu}` (device-to-device copy)
- **inference** → `{tok_per_s, fwd_passes, tokens, seconds, config, gpu}` (synthetic 24-layer transformer, torch SDPA)

`engine=venv` runs the burn with the node's own python (fast start; kernels limited
to what that torch build has). `engine=docker` runs it inside the nvcr PyTorch
container for proper Blackwell kernels (~10–15 s container start). The node file is
its own burn runner — the docker path mounts it read-only and invokes
`python /gpu_node.py --bench …` inside the container, so both engines share one code
path.

## Manual run / smoke test

```bash
ZENITH_GPU_PORT=8811 python3 gpu_node.py            # foreground
curl -s localhost:8811/health
curl -s localhost:8811/gpu
curl -s 'localhost:8811/job?type=matmul&engine=venv&n=8192&secs=8'
python3 gpu_node.py --bench matmul 8192 8           # run one burn, print JSON, exit
```

## Reference numbers (NVIDIA GB10 / Grace-Blackwell, `n=8192`, bf16)

matmul ≈ 75 (venv) / 88 (docker) TFLOP/s · bandwidth ≈ 233 GB/s · inference ≈ 25k tok/s.
These are dense-bf16 rates, not the FP4-sparse headline number.

> Provenance: `gpu_node.py` here is the canonical, repo-tracked version of the node
> first stood up ad-hoc on gpu-node. If a live node predates this file, diff it against
> `~/.zenith-gpu/gpu_node.py` after install to confirm parity.
