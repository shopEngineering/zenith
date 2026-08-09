#!/usr/bin/env python3
"""ZENITH/OS GPU worker node — a tiny stdlib HTTP server that runs a small set of
PREDEFINED GPU benchmarks on behalf of the ZENITH control plane. It never runs
arbitrary code: the only work it does is the matmul / bandwidth / inference burns
defined below, so a node can be exposed on the LAN without becoming a remote-exec
surface.

Contract (must match server.py `gpu_call` + the Fleet app in static/apps.js):

  GET /health                         -> {"ok": true, "host": "...", "torch": bool}
  GET /gpu                            -> {"gpu": "<name> · <util>% · <temp>°C · <power>W",
                                          "host": "..."}                (or {"error": ...})
  GET /job?type=&engine=&n=&secs=     -> per-type metrics:
      type=matmul     -> {"tflops", "matmuls", "n", "seconds", "gpu"}
      type=bandwidth  -> {"gb_per_s", "iters", "seconds", "gpu"}
      type=inference  -> {"tok_per_s", "fwd_passes", "tokens", "seconds", "config", "gpu"}
      any error       -> {"error": "..."}

  engine=venv   -> run the burn with THIS node's python (must have torch+CUDA)
  engine=docker -> run the burn inside the nvcr PyTorch container (proper GB10 kernels)

The same file is both the server and the burn runner: `--bench <type> <n> <secs>`
prints one JSON line and exits, which is what the venv subprocess and the docker
container both invoke. This keeps one code path for both engines.

Env:
  ZENITH_GPU_PORT     bind port                (default 8811)
  ZENITH_GPU_BIND     bind address             (default 0.0.0.0)
  ZENITH_GPU_IMAGE    docker image for engine=docker
                                               (default nvcr.io/nvidia/pytorch:26.05-py3)
"""
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("ZENITH_GPU_PORT", "8811"))
BIND = os.environ.get("ZENITH_GPU_BIND", "0.0.0.0")
IMAGE = os.environ.get("ZENITH_GPU_IMAGE", "nvcr.io/nvidia/pytorch:26.05-py3")
HOST = socket.gethostname()

# One GPU at a time: serialize burns so concurrent requests don't fight over the
# device (and so a burn's timing isn't polluted by another running alongside it).
_GPU_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# GPU status (nvidia-smi) — no torch needed, so /gpu works even on a bare box.
# ---------------------------------------------------------------------------
def gpu_status():
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=name,utilization.gpu,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            text=True, timeout=8, stderr=subprocess.DEVNULL).strip().splitlines()
    except Exception as e:
        return {"gpu": "no nvidia-smi (%s)" % (str(e)[:60]), "host": HOST}
    if not out:
        return {"gpu": "no GPU", "host": HOST}
    parts = [p.strip() for p in out[0].split(",")]
    name = parts[0] if parts else "GPU"
    util = parts[1] if len(parts) > 1 else "?"
    temp = parts[2] if len(parts) > 2 else "?"
    power = parts[3] if len(parts) > 3 else "?"
    # e.g. "NVIDIA GB10 · 96% · 60°C · 93W"
    try:
        power = "%d" % round(float(power))
    except Exception:
        pass
    return {"gpu": "%s · %s%% · %s°C · %sW" % (name, util, temp, power), "host": HOST}


# ---------------------------------------------------------------------------
# The burns. Each returns the metric dict the Fleet UI renders. torch only.
# ---------------------------------------------------------------------------
def _bench(jtype, n, secs):
    import time
    try:
        import torch
    except Exception as e:
        return {"error": "torch not importable: %s" % (str(e)[:120])}
    if not torch.cuda.is_available():
        return {"error": "CUDA not available in this environment"}
    dev = torch.device("cuda")
    torch.cuda.synchronize()
    gpu = gpu_status().get("gpu", "")

    if jtype == "matmul":
        # bf16 square matmul, count how many complete in `secs`.
        a = torch.randn(n, n, device=dev, dtype=torch.bfloat16)
        b = torch.randn(n, n, device=dev, dtype=torch.bfloat16)
        torch.matmul(a, b); torch.cuda.synchronize()          # warm up
        t0 = time.perf_counter(); count = 0
        while time.perf_counter() - t0 < secs:
            torch.matmul(a, b); count += 1
        torch.cuda.synchronize()
        el = time.perf_counter() - t0
        flops = 2.0 * (n ** 3) * count
        return {"tflops": round(flops / el / 1e12, 1), "matmuls": count,
                "n": n, "seconds": round(el, 2), "gpu": gpu}

    if jtype == "bandwidth":
        # device-to-device copy of a large tensor; count read+write bytes.
        elems = max(1, (n * n))                                # ~n^2 fp32 elements
        src = torch.empty(elems, device=dev, dtype=torch.float32)
        dst = torch.empty_like(src)
        dst.copy_(src); torch.cuda.synchronize()               # warm up
        nbytes = src.element_size() * src.numel()
        t0 = time.perf_counter(); count = 0
        while time.perf_counter() - t0 < secs:
            dst.copy_(src); count += 1
        torch.cuda.synchronize()
        el = time.perf_counter() - t0
        moved = 2.0 * nbytes * count                           # read + write
        return {"gb_per_s": round(moved / el / 1e9, 1), "iters": count,
                "seconds": round(el, 2), "gpu": gpu}

    if jtype == "inference":
        # synthetic transformer forward passes (torch SDPA attention).
        layers, d, seq, batch, heads = 24, 2048, 1024, 4, 16
        import torch.nn.functional as F
        qkv = [(torch.randn(batch, heads, seq, d // heads, device=dev, dtype=torch.bfloat16),
                torch.randn(batch, heads, seq, d // heads, device=dev, dtype=torch.bfloat16),
                torch.randn(batch, heads, seq, d // heads, device=dev, dtype=torch.bfloat16))
               for _ in range(2)]
        w = torch.randn(d, d, device=dev, dtype=torch.bfloat16)

        def fwd():
            x = torch.randn(batch, seq, d, device=dev, dtype=torch.bfloat16)
            for i in range(layers):
                q, k, v = qkv[i % len(qkv)]
                a = F.scaled_dot_product_attention(q, k, v)
                a = a.transpose(1, 2).reshape(batch, seq, d)
                x = torch.matmul(a + x, w)
            return x

        fwd(); torch.cuda.synchronize()                        # warm up
        t0 = time.perf_counter(); count = 0
        while time.perf_counter() - t0 < secs:
            fwd(); count += 1
        torch.cuda.synchronize()
        el = time.perf_counter() - t0
        toks = count * batch * seq
        return {"tok_per_s": round(toks / el), "fwd_passes": count, "tokens": toks,
                "seconds": round(el, 2),
                "config": "%dL d%d seq%d b%d" % (layers, d, seq, batch), "gpu": gpu}

    return {"error": "unknown job type: %s" % jtype}


def run_job(jtype, engine, n, secs):
    """Run a burn either in-process (venv) or inside the nvcr container (docker)."""
    if engine == "docker":
        cmd = ["docker", "run", "--rm", "--gpus", "all", "--ipc=host",
               "--ulimit", "memlock=-1", "--ulimit", "stack=67108864",
               "-v", "%s:/gpu_node.py:ro" % os.path.abspath(__file__), IMAGE,
               "python", "/gpu_node.py", "--bench", jtype, str(n), str(secs)]
    else:
        cmd = [sys.executable, os.path.abspath(__file__),
               "--bench", jtype, str(n), str(secs)]
    try:
        out = subprocess.check_output(cmd, text=True, timeout=300,
                                      stderr=subprocess.STDOUT)
    except subprocess.TimeoutExpired:
        return {"error": "%s/%s timed out" % (jtype, engine)}
    except subprocess.CalledProcessError as e:
        tail = (e.output or "")[-300:]
        return {"error": "%s/%s failed: %s" % (jtype, engine, tail)}
    except FileNotFoundError as e:
        return {"error": "engine '%s' unavailable: %s" % (engine, e)}
    # the burn prints exactly one JSON line last; tolerate leading log noise.
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                continue
    return {"error": "no JSON from %s/%s burn" % (jtype, engine)}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/health":
                torch_ok = False
                try:
                    import torch  # noqa: F401
                    torch_ok = True
                except Exception:
                    pass
                return self._send({"ok": True, "host": HOST, "torch": torch_ok})
            if u.path == "/gpu":
                return self._send(gpu_status())
            if u.path == "/job":
                jtype = "".join(c for c in q.get("type", ["matmul"])[0] if c.isalnum())[:20] or "matmul"
                engine = "docker" if q.get("engine", ["venv"])[0] == "docker" else "venv"
                try:
                    n = int(q.get("n", ["8192"])[0])
                except Exception:
                    n = 8192
                try:
                    secs = float(q.get("secs", ["8"])[0])
                except Exception:
                    secs = 8.0
                n = max(256, min(n, 32768))
                secs = max(1.0, min(secs, 60.0))
                with _GPU_LOCK:
                    return self._send(run_job(jtype, engine, n, secs))
            return self._send({"error": "not found"}, 404)
        except Exception as e:
            return self._send({"error": str(e)[:300]}, 500)


def main():
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print("zenith-gpu-node on %s:%d (host %s, image %s)" % (BIND, PORT, HOST, IMAGE),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--bench":
        jt = sys.argv[2] if len(sys.argv) > 2 else "matmul"
        nn = int(sys.argv[3]) if len(sys.argv) > 3 else 8192
        ss = float(sys.argv[4]) if len(sys.argv) > 4 else 8.0
        print(json.dumps(_bench(jt, nn, ss)))
    else:
        main()
