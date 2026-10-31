"""Check that this machine is ready for the lab (re-run any time).

  python verify_environment.py          # check only
  python verify_environment.py --pull   # also download MODEL and MODEL_B
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import os
import platform
import shutil
import subprocess
import sys

import lab_config as cfg

PROBLEMS: list[str] = []


def good(msg: str) -> None:
    print(f"  \033[1;32m✓\033[0m {msg}")


def bad(msg: str, fix: str) -> None:
    print(f"  \033[1;31m✗\033[0m {msg}\n      fix: {fix}")
    PROBLEMS.append(msg)


def info(msg: str) -> None:
    print(f"  \033[2m• {msg}\033[0m")


def ram_gib() -> float | None:
    try:
        if sys.platform == "darwin":
            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])) / 2**30
        with open("/proc/meminfo") as f:
            return int(f.readline().split()[1]) / 2**20
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pull", action="store_true", help="download MODEL and MODEL_B")
    a = p.parse_args()

    print("\nMachine")
    info(f"{platform.system()} {platform.machine()}, Python {platform.python_version()}")
    ram = ram_gib()
    if ram:
        info(f"{ram:.0f} GiB RAM" + ("  (8 GiB works; close other apps for Tasks 7-10)" if ram < 12 else ""))
    if sys.version_info < (3, 10):
        bad("Python < 3.10", "bash setup.sh (it creates a 3.12 venv with uv)")
    if sys.prefix == sys.base_prefix:
        bad("not running inside a virtualenv", "source .venv/bin/activate")

    print("\nPython packages")
    for pkg, want in (("smg", cfg.EXPECTED_VERSIONS["smg"]), ("aiohttp", None), ("requests", None), ("openai", None)):
        try:
            v = md.version(pkg)
            if want and v != want:
                bad(f"{pkg} {v} installed, lab written for {want}", f"pip install {pkg}=={want}")
            else:
                good(f"{pkg} {v}")
        except md.PackageNotFoundError:
            bad(f"{pkg} missing", "pip install -r requirements.txt")
    if not shutil.which("smg"):
        bad("`smg` command not on PATH", "source .venv/bin/activate")

    print("\nOllama")
    if not shutil.which("ollama"):
        bad("ollama not installed", "macOS: brew install ollama | Linux: curl -fsSL https://ollama.com/install.sh | sh")
    else:
        out = subprocess.run(["ollama", "--version"], capture_output=True, text=True).stdout.strip().splitlines()
        ver = out[-1].split()[-1] if out else "?"
        good(f"ollama {ver}")
        if not ver.startswith(cfg.EXPECTED_VERSIONS["ollama"]):
            info(f"lab written against Ollama {cfg.EXPECTED_VERSIONS['ollama']}.x; newer usually works")

    print("\nConfig (lab.env)")
    if not os.path.exists(os.path.join(cfg.LAB_DIR, "lab.env")):
        bad("lab.env missing", "cp lab.env.example lab.env")
    info(f"MODEL={cfg.MODEL}  MODEL_B={cfg.MODEL_B}")
    info(f"ports: ollama {cfg.OLLAMA_PORTS}  adapter {cfg.ADAPTER_PORTS}  smg {cfg.SMG_PORT} metrics {cfg.SMG_METRICS_PORT}")
    info("ollama tuning from lab.env: " + (", ".join(f"{k}={v}" for k, v in cfg.OLLAMA_TUNING.items()) or "(none)"))

    if PROBLEMS:
        print(f"\n\033[1;31m{len(PROBLEMS)} problem(s); fix them and re-run.\033[0m")
        raise SystemExit(1)

    import labctl  # noqa: E402  (needs the venv packages checked above)

    print("\nModels")
    started_here = labctl.load_state("ollama-a") is None
    if a.pull:
        subprocess.run([sys.executable, "labctl.py", "pull", cfg.MODEL, cfg.MODEL_B], cwd=cfg.LAB_DIR, check=True)
    subprocess.run([sys.executable, "labctl.py", "up", "ollama-a"], cwd=cfg.LAB_DIR, check=True)
    import requests

    have = {m["name"] for m in requests.get(cfg.OLLAMA_URL["a"] + "/api/tags", timeout=10).json().get("models", [])}
    for m in (cfg.MODEL, cfg.MODEL_B):
        full = m if ":" in m else m + ":latest"
        if full in have:
            good(f"{m} downloaded")
        else:
            bad(f"{m} not downloaded", f"python labctl.py pull {m}")
    if cfg.MODEL in have or cfg.MODEL + ":latest" in have:
        r = requests.post(cfg.OLLAMA_URL["a"] + "/v1/chat/completions", timeout=120, json={
            "model": cfg.MODEL, "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 5,
            **cfg.BENCH_EXTRA_BODY})
        if r.status_code == 200:
            good(f"chat completion from {cfg.MODEL}: {r.json()['choices'][0]['message']['content']!r}")
        else:
            bad(f"chat completion failed: {r.status_code} {r.text[:200]}", "python labctl.py logs ollama-a")
    if started_here:
        subprocess.run([sys.executable, "labctl.py", "down", "ollama-a"], cwd=cfg.LAB_DIR, check=False)

    if PROBLEMS:
        print(f"\n\033[1;31m{len(PROBLEMS)} problem(s); fix them and re-run.\033[0m")
        raise SystemExit(1)
    print("\n\033[1;32mEnvironment verified.\033[0m Start with LAB_GUIDE.md, Task 1.")


if __name__ == "__main__":
    main()
