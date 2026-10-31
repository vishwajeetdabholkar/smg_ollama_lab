"""Check that you finished a task:  python check.py 3   (or: python check.py all)

It looks at the result files you saved (results/...) and, for some tasks, at
what is running right now. It does NOT run anything for you.
After a pass it lists what you should now be able to explain; if you cannot,
re-read the task's "Explain" section before moving on (answers: ANSWERS.md).
"""

from __future__ import annotations

import json
import os
import sys

import requests

import lab_config as cfg

R = cfg.RESULTS_DIR


def load(name: str):
    path = os.path.join(R, name)
    if not os.path.exists(path):
        raise AssertionError(f"missing results/{name} (the task tells you which command writes it)")
    with open(path) as f:
        return json.load(f)


def rows(name: str, url_part: str) -> list[dict]:
    data = load(name)
    assert isinstance(data, list) and data, f"results/{name} is not a bench.py --out file"
    assert url_part in data[0]["base_url"], f"results/{name} was measured against {data[0]['base_url']}, expected {url_part}"
    assert all(r["ok"] > 0 for r in data), f"results/{name} has a row with zero successful requests"
    return data


def smg_models(headers=None) -> list[str]:
    r = requests.get(f"{cfg.SMG_URL}/v1/models", timeout=5, headers=headers or {})
    return [m["id"] for m in r.json().get("data", [])]


def chat(model: str, headers=None) -> requests.Response:
    return requests.post(f"{cfg.SMG_URL}/v1/chat/completions", timeout=60, headers=headers or {}, json={
        "model": model, "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 5,
        **cfg.BENCH_EXTRA_BODY})


def t1():
    rows("t1_direct.json", f":{cfg.OLLAMA_PORTS['a']}")
    return ["why throughput stops growing with concurrency on a default Ollama",
            "the two APIs Ollama serves (/api/* and /v1/*) and which one SMG uses",
            "what endpoints Ollama lacks that inference engines like vLLM have (/health, /metrics)"]


def t2():
    rows("t2_external.json", f":{cfg.SMG_PORT}")
    return ["what 'external' (runtime_type) means and what --backend openai changes",
            "why /v1/models said no_models_available, and what --api-key changed (both directions!)",
            "why --policy had no effect with two external workers"]


def t3():
    rows("t3_adapter.json", f":{cfg.ADAPTER_PORTS['a']}")
    g = rows("t3_gateway.json", f":{cfg.SMG_PORT}")
    assert g, "empty gateway results"
    return ["the registration workflow steps and which one rejected raw Ollama",
            "the file and function in SMG's source that builds the /health URL",
            "how much latency the gateway adds, measured, and why it is small"]


def t4():
    models = smg_models()
    assert cfg.MODEL in models and cfg.MODEL_B in models, (
        f"smg should serve both {cfg.MODEL} and {cfg.MODEL_B}; /v1/models shows {models}. "
        "Leave the Task 4 multi-model stack running and re-check.")
    r = chat("fast")
    assert r.status_code == 200, f"alias 'fast' did not work ({r.status_code}): start smg with --model-alias fast={cfg.MODEL}"
    worker = r.headers.get("x-smg-routed-worker-id", "")
    return [f"how SMG picked the worker for model 'fast' (it went to {worker or '?'})",
            "the difference between /v1/chat/completions, /v1/messages and /v1/responses, and who translates",
            "what x-request-id and x-smg-routed-worker-id are for"]


def t5():
    m = load("t5_metrics.json")
    assert m.get("requests_by_worker"), "t5_metrics.json has no per-worker requests; send traffic first"
    return ["which latency SMG can and cannot measure in HTTP mode (no TTFT: why?)",
            "how histogram_quantile turns buckets into a p99, and why it is an estimate",
            "how to find one request's log lines from its x-request-id"]


def t6():
    files = [f for f in os.listdir(R) if f.startswith("t6_") and f.endswith(".json")] if os.path.isdir(R) else []
    assert len(files) >= 2, f"need at least two Task 6 runs (results/t6_*.json), found {files}"
    np = cfg.LAB_ENV.get("OLLAMA_NUM_PARALLEL", "")
    assert np, "set OLLAMA_NUM_PARALLEL in lab.env to the value you chose (later tasks rely on it)"
    return [f"why you chose OLLAMA_NUM_PARALLEL={np}: throughput, latency and memory numbers",
            "what OLLAMA_KV_CACHE_TYPE=q8_0 trades, and what it needs (flash attention)",
            "why memory grows with NUM_PARALLEL x CONTEXT_LENGTH"]


def t7():
    rr, ca = load("t7_rr.json"), load("t7_ca.json")
    assert rr[0]["workload"] == ca[0]["workload"] == "multi_tenant", "both runs must use --workload multi_tenant"
    return [f"round_robin {rr[0]['req_per_s']} req/s vs cache_aware {ca[0]['req_per_s']} req/s: why",
            "why the gap is small with 8 tenants and larger with 32 (cache size arithmetic)",
            "why two Ollama servers on ONE Mac do not double compute"]


def t8():
    files = [f for f in os.listdir(R) if f.startswith("t8_")] if os.path.isdir(R) else []
    assert len(files) >= 2, f"need at least two Task 8 runs (results/t8_*.json), found {files}"
    return ["the difference between --max-concurrent-requests, --queue-size and --rate-limit-tokens-per-second",
            "why a bounded queue can improve the latency of requests you do accept",
            "how to size --max-concurrent-requests from workers x NUM_PARALLEL"]


def t9():
    g, d = load("t9_gateway.json"), load("t9_direct.json")
    assert g.get("events") and d.get("events"), "both chaos runs need a --kill event"
    return [f"failed requests: {d['failed']} without the gateway vs {g['failed']} with it, and why",
            "what the circuit breaker did after the worker came back (metrics.py)",
            "which knob shortens failure detection, and its cost"]


def t10():
    best = load("capstone_best.json")
    hist = [json.loads(l) for l in open(os.path.join(R, "capstone_history.jsonl"))]
    assert len(hist) >= 2, "run capstone.py at least twice (the starting config, then yours)"
    first = hist[0]["score_req_s"]
    assert best["score_req_s"] > first, f"best score {best['score_req_s']} does not beat your first run ({first})"
    return [f"your config went from {first} to {best['score_req_s']} req/s at SLO: which change mattered most",
            "which change you tried that did NOT help, and why"]


TASKS = {1: t1, 2: t2, 3: t3, 4: t4, 5: t5, 6: t6, 7: t7, 8: t8, 9: t9, 10: t10}


def run(n: int) -> bool:
    try:
        explain = TASKS[n]()
    except AssertionError as e:
        print(f"\033[1;31mTask {n}: not done\033[0m  - {e}")
        return False
    except requests.RequestException as e:
        print(f"\033[1;31mTask {n}: not done\033[0m  - cannot reach smg ({e.__class__.__name__}). Is it running?")
        return False
    print(f"\033[1;32mTask {n}: done\033[0m  You should now be able to explain:")
    for line in explain:
        print(f"   - {line}")
    return True


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    nums = list(TASKS) if arg == "all" else [int(arg)]
    passed = sum(run(n) for n in nums)
    if len(nums) > 1:
        print(f"\n{passed}/{len(nums)} tasks done")
