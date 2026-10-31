"""Shared configuration for labctl.py, bench.py, check.py and the helpers.

Every value can be overridden with an environment variable of the same name,
e.g.  MODEL=qwen3:1.7b python bench.py
Defaults come from lab.env (written by setup.sh, edit it freely).
"""

import os

LAB_DIR = os.path.dirname(os.path.abspath(__file__))


def _read_lab_env() -> dict[str, str]:
    path = os.path.join(LAB_DIR, "lab.env")
    values: dict[str, str] = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
    return values


LAB_ENV = _read_lab_env()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, LAB_ENV.get(name, default))


MODEL = _env("MODEL", "qwen3:0.6b")
MODEL_B = _env("MODEL_B", "llama3.2:1b")

HOST = "127.0.0.1"
OLLAMA_PORTS = {"a": int(_env("OLLAMA_A_PORT", "11500")), "b": int(_env("OLLAMA_B_PORT", "11501"))}
ADAPTER_PORTS = {"a": int(_env("ADAPTER_A_PORT", "11600")), "b": int(_env("ADAPTER_B_PORT", "11601"))}
SMG_PORT = int(_env("SMG_PORT", "30100"))
SMG_METRICS_PORT = int(_env("SMG_METRICS_PORT", "29100"))

OLLAMA_URL = {k: f"http://{HOST}:{p}" for k, p in OLLAMA_PORTS.items()}
ADAPTER_URL = {k: f"http://{HOST}:{p}" for k, p in ADAPTER_PORTS.items()}
SMG_URL = f"http://{HOST}:{SMG_PORT}"
SMG_METRICS_URL = f"http://{HOST}:{SMG_METRICS_PORT}/metrics"

# OLLAMA_* tuning variables from lab.env are applied to every lab Ollama server
# (OLLAMA_HOST is always set by labctl from the ports above).
OLLAMA_TUNING = {k: v for k, v in LAB_ENV.items() if k.startswith("OLLAMA_") and not k.endswith("_PORT") and v}

SCRIPTS_DIR = os.path.join(LAB_DIR, "scripts")
RESULTS_DIR = _env("RESULTS_DIR", os.path.join(LAB_DIR, "results"))
LOG_DIR = _env("LOG_DIR", os.path.join(LAB_DIR, "logs"))
RUN_DIR = _env("RUN_DIR", os.path.join(LAB_DIR, "run"))

EXPECTED_VERSIONS = {"smg": "1.13.0", "ollama": "0.32"}

# Sent with every benchmark request. Qwen3 is a "thinking" model; its hidden
# reasoning length varies wildly between runs, which makes numbers noisy.
# Ollama maps reasoning_effort=none to "think: false"; non-thinking models ignore it.
BENCH_EXTRA_BODY = {"reasoning_effort": "none"}
