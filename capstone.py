"""Task 10 capstone: run a fixed "production" workload against YOUR config and score it.

  python capstone.py                      # uses capstone.env
  python capstone.py --config my.env      # another config file
  python capstone.py --no-restart         # reuse whatever is running (faster iterations)

The workload (same every run, so scores are comparable):
  * open loop: requests arrive on a Poisson schedule at --rate req/s for --duration s,
    whether or not earlier ones have finished (like real users)
  * multi_tenant with 16 tenants: each request carries one of 16 long (~2k token)
    system prompts, plus a short question; 32 output tokens

The SLO: time to first token <= --ttft-slo-ms (default 1500 ms) for at least 95%
of requests. Rejected (429) and failed requests count as misses, so admission
control is a trade-off, not a free win.

The load is ramped (--rates 1,1.5,2,...,12 req/s, --duration s each) and stops at the
first level that misses the SLO. SCORE = the highest rate your config sustained.
That is "capacity at SLO", the number capacity planning is done with. It
depends on your hardware, so compare configs on the same machine and the same
power state (plugged in, Low Power Mode off).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shlex
import subprocess
import sys
import time

import aiohttp

import bench
import lab_config as cfg


def read_config(path: str) -> dict[str, str]:
    conf: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    return conf


def bring_up(conf: dict[str, str]) -> None:
    workers = [w.strip() for w in conf.get("WORKERS", "a").split(",") if w.strip()]
    env_args = []
    for k in ("OLLAMA_NUM_PARALLEL", "OLLAMA_FLASH_ATTENTION", "OLLAMA_KV_CACHE_TYPE", "OLLAMA_CONTEXT_LENGTH"):
        if conf.get(k):
            env_args += ["--env", f"{k}={conf[k]}"]
    run = lambda *args: subprocess.run([sys.executable, "labctl.py", *args], cwd=cfg.LAB_DIR, check=True)  # noqa: E731
    unused = [n for w in ("a", "b") if w not in workers for n in (f"ollama-{w}", f"adapter-{w}")]
    run("down", "smg", *unused)
    for w in workers:
        run("up", f"ollama-{w}", *env_args)
        run("up", f"adapter-{w}")
    run("up", "smg", "--mode", "http", "--workers", ",".join(workers), "--", *shlex.split(conf.get("SMG_FLAGS", "")))


async def run_workload(rate: float, duration: float, slo_s: float, seed: int, tenants: int) -> dict:
    bench.TENANTS = tenants
    url = f"{cfg.SMG_URL}/v1/chat/completions"
    rng = random.Random(seed)
    results: list[bench.Result] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                                     connector=aiohttp.TCPConnector(limit=0)) as s:
        # Steady state, not cold start: every tenant's system prompt is sent once (4 at a time) before timing.
        sem = asyncio.Semaphore(4)

        async def warm(t: int) -> None:
            async with sem:
                msgs = [{"role": "system", "content": bench._system_prompt(t)}, {"role": "user", "content": "hi"}]
                await bench.one_request(s, url, cfg.MODEL, msgs, 4, cfg.BENCH_EXTRA_BODY, {})

        await asyncio.gather(*(warm(t) for t in range(bench.TENANTS)))

        async def one(i: int) -> None:
            msgs = bench.build_messages("multi_tenant", i, seed)
            results.append(await bench.one_request(s, url, cfg.MODEL, msgs, 32, cfg.BENCH_EXTRA_BODY, {}))

        tasks, start, i = [], time.perf_counter(), 0
        while time.perf_counter() - start < duration:
            tasks.append(asyncio.create_task(one(i)))
            i += 1
            await asyncio.sleep(rng.expovariate(rate))
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

    good = [r for r in results if r.ok and r.ttft is not None and r.ttft <= slo_s]
    ok = [r for r in results if r.ok]
    ttfts = sorted(r.ttft for r in ok if r.ttft is not None)
    codes: dict[str, int] = {}
    for r in results:
        if not r.ok:
            codes[str(r.status)] = codes.get(str(r.status), 0) + 1

    def pct(q: float) -> float | None:
        return round(ttfts[min(len(ttfts) - 1, int(q * (len(ttfts) - 1)))] * 1000, 1) if ttfts else None

    return {
        "sent": len(results), "succeeded": len(ok), "met_slo": len(good), "failed_by_status": codes,
        "offered_rate": rate, "elapsed_s": round(elapsed, 1),
        "goodput_req_s": round(len(good) / elapsed, 2), "slo_attainment": round(len(good) / max(1, len(results)), 3),
        "ttft_ms_p50": pct(0.5), "ttft_ms_p90": pct(0.9), "ttft_ms_p99": pct(0.99),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.path.join(cfg.LAB_DIR, "capstone.env"))
    p.add_argument("--tenants", type=int, default=16, help="distinct ~2k-token system prompts")
    p.add_argument("--rates", default="1,1.5,2,2.5,3,3.5,4,5,6,7,8,10,12",
                   help="offered loads to ramp through, req/s")
    p.add_argument("--duration", type=float, default=20.0, help="seconds per load level")
    p.add_argument("--ttft-slo-ms", type=float, default=1500.0)
    p.add_argument("--target", type=float, default=0.95, help="fraction of requests that must meet the SLO")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--no-restart", action="store_true")
    a = p.parse_args()

    conf = read_config(a.config)
    print("config:", json.dumps(conf, indent=2))
    if not a.no_restart:
        bring_up(conf)
    print(f"\nworkload: multi_tenant x{a.tenants}, {a.duration:.0f}s per level; "
          f"SLO: TTFT <= {a.ttft_slo_ms:.0f} ms for >= {a.target:.0%} of requests")
    print(f"  {'rate':>5} {'sent':>5} {'ok':>5} {'in SLO':>7} {'attain':>7} {'TTFT p50':>9} {'p99':>8}  failed")
    levels, score = [], 0.0
    for rate in [float(x) for x in a.rates.split(",")]:
        res = asyncio.run(run_workload(rate, a.duration, a.ttft_slo_ms / 1000, a.seed, a.tenants))
        levels.append(res)
        passed = res["slo_attainment"] >= a.target
        mark = "\033[32mpass\033[0m" if passed else "\033[31mmiss\033[0m"
        print(f"  {rate:>5g} {res['sent']:>5} {res['succeeded']:>5} {res['met_slo']:>7} {res['slo_attainment']:>7.0%} "
              f"{res['ttft_ms_p50'] or '-':>9} {res['ttft_ms_p99'] or '-':>8}  {res['failed_by_status'] or ''}  {mark}")
        if not passed:
            break
        score = rate

    print(f"\n  \033[1mSCORE (capacity at SLO): {score:g} req/s\033[0m")

    os.makedirs(cfg.RESULTS_DIR, exist_ok=True)
    record = {"config": conf, "score_req_s": score, "levels": levels, "workload": vars(a),
              "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(os.path.join(cfg.RESULTS_DIR, "capstone_history.jsonl"), "a") as f:
        f.write(json.dumps(record) + "\n")
    best_path = os.path.join(cfg.RESULTS_DIR, "capstone_best.json")
    best = json.load(open(best_path)) if os.path.exists(best_path) else None
    if not best or score > best.get("score_req_s", -1):
        with open(best_path, "w") as f:
            json.dump(record, f, indent=2)
        print(f"  new best (previous: {best.get('score_req_s') if best else 'none'}) -> results/capstone_best.json")
    else:
        print(f"  best so far: {best['score_req_s']} req/s (results/capstone_best.json)")
    print("  every run is appended to results/capstone_history.jsonl")


if __name__ == "__main__":
    main()
