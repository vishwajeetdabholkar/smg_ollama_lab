"""Streaming load generator for OpenAI-compatible chat endpoints.

Measures, per request:
  TTFT  - time to first token (first streamed content/reasoning delta)
  ITL   - inter-token latency (gap between consecutive streamed chunks)
  E2E   - end-to-end latency
and, per run: request throughput, output-token throughput, error count.

It works the same against Ollama directly (:11500), through the adapter (:11600)
and through SMG (:30100), which is what makes the comparisons in this lab
apples-to-apples.

Workloads (all prompts are synthetic and deterministic for a given --seed):
  short          ~60-token unique questions                (decode-heavy)
  long_prompt    ~3k-token unique documents to summarize   (prefill-heavy)
  shared_prefix  one ~2k-token system prompt + short question (prefix-cache friendly)
  multi_tenant   8 different ~2k-token system prompts, random tenant per request
                 (what cache-aware routing across several workers is built for)

Standalone usage:
  python bench.py --base-url http://127.0.0.1:30100 --workload short \
                  --concurrency 1,4,8 --max-tokens 64 --out results/my_run.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import time
from dataclasses import dataclass, field

import aiohttp

import lab_config as cfg

WORDS = (
    "gateway router worker token cache prefix batch latency throughput kernel tensor "
    "memory schedule prefill decode stream queue model request policy metric engine "
    "cluster replica shard attention block page radix tree load balance health retry"
).split()


def _text(rng: random.Random, n_words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n_words))


def _system_prompt(tenant: int, n_words: int = 1500) -> str:
    rng = random.Random(10_000 + tenant)
    header = f"You are support assistant #{tenant} for product line {tenant}. Policy manual follows.\n"
    return header + _text(rng, n_words)


TENANTS = 8


def build_messages(workload: str, i: int, seed: int) -> list[dict]:
    rng = random.Random(seed * 1_000_003 + i)
    question = f"Request {i}-{rng.randint(0, 10**9)}: explain {_text(rng, 8)} in detail."
    if workload == "short":
        return [{"role": "user", "content": question + " " + _text(rng, 30)}]
    if workload == "long_prompt":
        doc = f"Document {i}-{rng.randint(0, 10**9)}.\n" + _text(rng, 2300)
        return [{"role": "user", "content": doc + "\n\nSummarize the document above."}]
    if workload == "shared_prefix":
        return [
            {"role": "system", "content": _system_prompt(0)},
            {"role": "user", "content": question},
        ]
    if workload == "multi_tenant":
        tenant = rng.randrange(TENANTS)
        return [
            {"role": "system", "content": _system_prompt(tenant)},
            {"role": "user", "content": question},
        ]
    raise ValueError(f"unknown workload {workload!r}")


@dataclass
class Result:
    ok: bool
    status: int = 0
    error: str = ""
    ttft: float | None = None
    e2e: float = 0.0
    itls: list[float] = field(default_factory=list)
    output_tokens: int = 0
    prompt_tokens: int = 0


async def one_request(
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    extra_body: dict,
    headers: dict,
) -> Result:
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
        **extra_body,
    }
    start = time.perf_counter()
    last = None
    res = Result(ok=False)
    chunks = 0
    try:
        async with session.post(url, json=body, headers=headers) as resp:
            res.status = resp.status
            if resp.status != 200:
                res.error = (await resp.text())[:300]
                res.e2e = time.perf_counter() - start
                return res
            buf = b""
            async for raw in resp.content.iter_any():
                buf += raw
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == b"[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if event.get("usage"):
                        res.output_tokens = event["usage"].get("completion_tokens", 0) or 0
                        res.prompt_tokens = event["usage"].get("prompt_tokens", 0) or 0
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if (
                            delta.get("content")
                            or delta.get("reasoning_content")
                            or delta.get("reasoning")
                            or delta.get("tool_calls")
                        ):
                            now = time.perf_counter()
                            chunks += 1
                            if res.ttft is None:
                                res.ttft = now - start
                            elif last is not None:
                                res.itls.append(now - last)
                            last = now
        res.e2e = time.perf_counter() - start
        if res.output_tokens == 0:
            res.output_tokens = chunks
        res.ok = res.ttft is not None
        if not res.ok:
            res.error = "stream ended without any token"
    except Exception as e:  # noqa: BLE001 - a benchmark must survive any single failure
        res.error = f"{type(e).__name__}: {e}"
        res.e2e = time.perf_counter() - start
    return res


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
    return values[idx]


def _ms(v: float | None) -> float | None:
    return None if v is None else round(v * 1000, 1)


async def run_load(
    base_url: str,
    model: str,
    workload: str = "short",
    concurrency: int = 8,
    num_requests: int = 64,
    max_tokens: int = 128,
    extra_body: dict | None = None,
    seed: int = 0,
    request_rate: float | None = None,
    headers: dict | None = None,
    timeout_s: float = 600,
) -> dict:
    """Run one benchmark point and return a summary dict.

    Closed loop by default: `concurrency` clients each send their next request
    as soon as the previous one finishes. With `request_rate` (req/s) requests
    are instead started on a Poisson schedule, capped at `concurrency` in flight.
    """
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    extra = cfg.BENCH_EXTRA_BODY if extra_body is None else extra_body
    hdrs = headers or {}
    results: list[Result] = []
    sem = asyncio.Semaphore(concurrency)
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    connector = aiohttp.TCPConnector(limit=0)
    rng = random.Random(seed)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:

        async def worker(i: int) -> None:
            async with sem:
                msgs = build_messages(workload, i, seed)
                results.append(
                    await one_request(session, url, model, msgs, max_tokens, extra, hdrs)
                )

        start = time.perf_counter()
        tasks = []
        for i in range(num_requests):
            tasks.append(asyncio.create_task(worker(i)))
            if request_rate:
                await asyncio.sleep(rng.expovariate(request_rate))
        await asyncio.gather(*tasks)
        duration = time.perf_counter() - start

    good = [r for r in results if r.ok]
    ttfts = [r.ttft for r in good if r.ttft is not None]
    itls = [x for r in good for x in r.itls]
    e2es = [r.e2e for r in good]
    out_tokens = sum(r.output_tokens for r in good)
    in_tokens = sum(r.prompt_tokens for r in good)
    errors: dict[str, int] = {}
    for r in results:
        if not r.ok:
            key = f"{r.status} {r.error[:80]}"
            errors[key] = errors.get(key, 0) + 1

    return {
        "base_url": base_url,
        "workload": workload,
        "concurrency": concurrency,
        "request_rate": request_rate,
        "num_requests": num_requests,
        "max_tokens": max_tokens,
        "ok": len(good),
        "failed": len(results) - len(good),
        "errors": errors,
        "duration_s": round(duration, 2),
        "req_per_s": round(len(good) / duration, 2) if duration else 0,
        "output_tok_per_s": round(out_tokens / duration, 1) if duration else 0,
        "total_tok_per_s": round((out_tokens + in_tokens) / duration, 1) if duration else 0,
        "mean_prompt_tokens": round(in_tokens / len(good)) if good else 0,
        "ttft_ms_p50": _ms(_pct(ttfts, 0.50)),
        "ttft_ms_p90": _ms(_pct(ttfts, 0.90)),
        "ttft_ms_p99": _ms(_pct(ttfts, 0.99)),
        "itl_ms_p50": _ms(_pct(itls, 0.50)),
        "itl_ms_p99": _ms(_pct(itls, 0.99)),
        "e2e_ms_p50": _ms(_pct(e2es, 0.50)),
        "e2e_ms_p99": _ms(_pct(e2es, 0.99)),
        "ttft_ms_mean": _ms(statistics.fmean(ttfts)) if ttfts else None,
    }


HEADER = (
    f"{'conc':>5} {'ok':>5} {'fail':>4} {'req/s':>7} {'out tok/s':>10} "
    f"{'TTFT p50':>9} {'TTFT p99':>9} {'ITL p50':>8} {'ITL p99':>8} {'E2E p50':>9}"
)


def fmt_row(s: dict) -> str:
    def f(v):
        return "-" if v is None else f"{v}"

    return (
        f"{s['concurrency']:>5} {s['ok']:>5} {s['failed']:>4} {s['req_per_s']:>7} "
        f"{s['output_tok_per_s']:>10} {f(s['ttft_ms_p50']):>9} {f(s['ttft_ms_p99']):>9} "
        f"{f(s['itl_ms_p50']):>8} {f(s['itl_ms_p99']):>8} {f(s['e2e_ms_p50']):>9}"
    )


async def sweep(
    base_url: str,
    model: str,
    concurrencies: list[int],
    num_requests: int | None = None,
    **kw,
) -> list[dict]:
    """Run run_load() once per concurrency level and print a table row each."""
    print(f"\nTarget {base_url}  workload={kw.get('workload', 'short')}  (latencies in ms)")
    # A few untimed requests first, so model loads (one per worker) do not pollute the first row.
    await run_load(base_url, model, concurrency=4, num_requests=4, seed=99_999,
                   **{k: v for k, v in kw.items() if k not in ("request_rate", "seed")})
    print(HEADER)
    rows = []
    for c in concurrencies:
        s = await run_load(
            base_url,
            model,
            concurrency=c,
            num_requests=num_requests or max(16, c * 4),
            **kw,
        )
        print(fmt_row(s))
        if s["errors"]:
            for err, count in list(s["errors"].items())[:3]:
                print(f"      {count}x {err}")
        rows.append(s)
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=cfg.SMG_URL)
    p.add_argument("--model", default=cfg.MODEL)
    p.add_argument("--workload", default="short", choices=["short", "long_prompt", "shared_prefix", "multi_tenant"])
    p.add_argument("--concurrency", default="1,4,8", help="comma separated list")
    p.add_argument("--num-requests", type=int, default=None, help="per point (default: max(16, 4*concurrency))")
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--request-rate", type=float, default=None, help="open-loop Poisson req/s")
    p.add_argument("--thinking", action="store_true", help="leave Qwen3 thinking on")
    p.add_argument("--api-key", default=None, help="send Authorization: Bearer <key>")
    p.add_argument("--tenants", type=int, default=8, help="multi_tenant: number of distinct system prompts")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", default=None, help="write the JSON results here")
    a = p.parse_args()
    global TENANTS
    TENANTS = a.tenants

    extra = {} if a.thinking else cfg.BENCH_EXTRA_BODY
    headers = {"Authorization": f"Bearer {a.api_key}"} if a.api_key else None
    rows = asyncio.run(
        sweep(
            a.base_url,
            a.model,
            [int(x) for x in a.concurrency.split(",")],
            workload=a.workload,
            max_tokens=a.max_tokens,
            extra_body=extra,
            seed=a.seed,
            request_rate=a.request_rate,
            num_requests=a.num_requests,
            headers=headers,
        )
    )
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as f:
            json.dump(rows, f, indent=2)
        print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
