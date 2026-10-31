# 03 - Optimization playbook

The order matters. Each step assumes the previous one is done.

## 0. Make measurements trustworthy

- Plug in the laptop and turn Low Power Mode off (`pmset -g` shows `powermode 1` when it is on). On the reference Mac, Low Power Mode on battery halved throughput.
- Warm up first: model loads and cold caches are a different experiment.
- Compare runs back to back, change one thing at a time, and repeat a surprising result before believing it.
- Measure what users feel: TTFT p50/p99 and successful req/s, not just "it ran".

## 1. Engine: get the most out of one machine (Task 6)

- Raise `OLLAMA_NUM_PARALLEL` until req/s stops improving or ITL becomes unacceptable.
- Memory is NUM_PARALLEL x CONTEXT_LENGTH x KV bytes. Lower the context, or use `OLLAMA_KV_CACHE_TYPE=q8_0`, if you need more slots.
- A gateway cannot fix an engine that runs one request at a time.

## 2. Placement: put each request where its work is cheapest (Task 7)

- Shared long prefixes (system prompts, tenants, RAG templates)? Use `cache_aware`.
- Compute the working set: distinct prefixes x prefix tokens. If it fits in one worker's cache, routing barely matters. If it only fits across workers, `cache_aware` matters a lot.
- Watch for hot spots: one popular tenant pinned to one worker. Tune `--balance-abs-threshold` / `--balance-rel-threshold` so overloaded workers spill traffic.
- No shared prefixes? `least_load` or `power_of_two` balance in-flight work better than `round_robin` when requests differ in size.

## 3. Admission: decide what to do with excess load (Task 8)

- Set `--max-concurrent-requests` to about workers x NUM_PARALLEL (x a small factor). Beyond that, extra requests only queue *inside* the engine, where nobody can see or bound them.
- A bounded `--queue-size` with `--queue-timeout-secs` absorbs bursts. `--queue-size 0` fails fast with 429, which clients can retry elsewhere.
- `--rate-limit-tokens-per-second` (requests per second, despite the name) smooths sustained overload.
- Rejecting some requests can *raise* the number that meet the SLO. Overload makes everyone slow; admission control keeps the accepted ones fast.

## 4. Resilience: fail small (Task 9)

- Health checks: `--health-check-interval-secs` and `--health-failure-threshold` trade detection speed against probe traffic and false alarms.
- Retries (`--retry-max-retries`, backoff flags) hide connection failures that happen before streaming starts.
- The circuit breaker (`--cb-failure-threshold`, `--cb-timeout-duration-secs`) stops sending traffic to a failing worker and readmits it carefully.

## 5. Verify against the SLO (Task 10)

- Load the system with an *open-loop* arrival rate (real users do not wait for each other).
- Score capacity at SLO: the highest rate where, say, 95% of requests meet the TTFT target.
- Keep a history of configs and scores (`results/capstone_history.jsonl`).

## Quick diagnosis

| Symptom | Likely cause | Look at |
|---|---|---|
| req/s flat as clients grow, TTFT climbs, ITL flat | engine runs one request at a time | `OLLAMA_NUM_PARALLEL` |
| ITL climbs with load | engine compute saturated | fewer slots, smaller model, more machines |
| TTFT high only for long prompts | prefill cost, cache misses | `cache_aware`, tenant working set |
| one worker much busier | prefix affinity hot spot | balance thresholds, `metrics.py` per worker |
| p99 explodes under bursts | unbounded queueing | `--max-concurrent-requests`, `--queue-size` |
| errors when a worker dies | no retries or slow detection | retry and health flags, circuit breaker state |
