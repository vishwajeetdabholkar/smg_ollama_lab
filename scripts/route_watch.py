"""See where SMG sends requests, live, in your terminal.

Two modes:

  python scripts/route_watch.py --watch
      Sends nothing. Watches SMG's metrics and draws a bar per worker, so every
      curl you send from another terminal shows up on the worker that served it.

  python scripts/route_watch.py -n 60 -c 4 --tenants 6
      Sends traffic itself and reads `x-smg-routed-worker-id` from every response.
      Each worker row shows a trail of the last requests it served, one colored
      block per tenant: with cache_aware the colors separate by worker, with
      round_robin they mix.

Useful options for traffic mode:
  --tenants N         each request starts with one of N long tenant system prompts (0 = a short "hi")
  --routing-key K     send `X-SMG-Routing-Key: K` (use "tenant" for one key per tenant)
  --target-worker I   send `X-SMG-Target-Worker: I` (worker index, consistent_hashing only)
  -H 'Name: value'    any extra header (repeatable)
  --model M           model name (try an alias, or one that does not exist)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict, deque

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lab_config as cfg  # noqa: E402
import metrics as M  # noqa: E402

COLORS = [196, 214, 46, 51, 33, 135, 205, 154, 208, 39, 171, 118]
RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
CLEAR = "\033[H\033[J"
TOPIC = ["billing disputes", "travel bookings", "medical scheduling", "insurance claims", "legal intake",
         "retail returns", "school admissions", "bank onboarding", "hotel concierge", "car rentals",
         "pharmacy refills", "utility outages"]


def fg(c: int) -> str:
    return f"\033[38;5;{c}m"


def worker_names() -> dict[str, str]:
    names = {}
    for w, url in cfg.ADAPTER_URL.items():
        names[url] = f"adapter-{w}"
    for w, url in cfg.OLLAMA_URL.items():
        names[url] = f"ollama-{w}"
    return names


def smg_command() -> str:
    try:
        with open(os.path.join(cfg.RUN_DIR, "smg.json")) as f:
            return json.load(f)["printable"]
    except (OSError, ValueError, KeyError):
        return "(smg not started by labctl; flags unknown)"


def smg_flags(cmd: str) -> str:
    parts = cmd.split()
    if "--worker-urls" in parts:
        i = parts.index("--worker-urls") + 1
        while i < len(parts) and not parts[i].startswith("--"):
            i += 1
        return " ".join(parts[i:]) or "(defaults)"
    return cmd


def tenant_prompt(t: int) -> str:
    topic = TOPIC[t % len(TOPIC)]
    rules = " ".join(f"Rule {i + 1} for tenant {t:02d}: always answer questions about {topic} politely, "
                     f"cite policy section {t}.{i + 1}, and never invent account numbers." for i in range(40))
    return f"Tenant {t:02d} handbook ({topic}). {rules}"


def bar(n: int, total: int, width: int = 28) -> str:
    filled = round(width * n / total) if total else 0
    return "█" * filled + DIM + "·" * (width - filled) + RESET


# ------------------------------------------------------------------ traffic ---

async def traffic(a: argparse.Namespace) -> None:
    names = worker_names()
    url = a.url.rstrip("/") + "/v1/chat/completions"
    counts: Counter = Counter()
    trails: dict[str, deque] = defaultdict(lambda: deque(maxlen=34))
    statuses: Counter = Counter()
    recent: deque = deque(maxlen=7)
    lat: dict[str, list] = defaultdict(list)
    sent = 0
    lock = asyncio.Lock()
    base_headers = {"Content-Type": "application/json"}
    for h in a.header:
        k, _, v = h.partition(":")
        base_headers[k.strip()] = v.strip()
    if a.api_key:
        base_headers["Authorization"] = f"Bearer {a.api_key}"
    if a.target_worker is not None:
        base_headers["X-SMG-Target-Worker"] = str(a.target_worker)
    cmd = smg_flags(smg_command())
    start = time.time()

    def draw(final: bool = False) -> None:
        total = sum(counts.values())
        out = [CLEAR if not final else "",
               f"{BOLD}SMG routing{RESET}  {a.url}   model={a.model}   tenants={a.tenants or 'none'}"
               + (f"   routing-key={a.routing_key}" if a.routing_key else ""),
               f"{DIM}smg flags: {cmd}{RESET}", ""]
        rows = sorted(counts) or []
        for w in rows:
            label = names.get(w, w)
            ms = sorted(lat[w])
            p50 = f"{ms[len(ms) // 2]:.0f} ms" if ms else "-"
            trail = "".join(f"{fg(COLORS[t % len(COLORS)])}■{RESET}" if t >= 0 else f"{DIM}■{RESET}" for t in trails[w])
            out.append(f"  {BOLD}{label:<10}{RESET} {w.replace('http://', ''):<16} {counts[w]:>4}  {bar(counts[w], total)}  p50 {p50:>7}")
            out.append(f"  {'':<27}{trail}")
        bad = {s: n for s, n in statuses.items() if s != 200}
        if bad:
            out.append("")
            out.append(f"  {BOLD}not routed / failed{RESET}  " + "  ".join(f"\033[31m{s}\033[0m ×{n}" for s, n in sorted(bad.items(), key=str)))
        out.append("")
        out.append(f"  {DIM}last requests{RESET}")
        for line in recent:
            out.append("   " + line)
        out.append("")
        out.append(f"  {sent}/{a.n} sent   {total} routed   {time.time() - start:.1f}s" + ("   done" if final else "   (Ctrl-C to stop)"))
        if a.tenants:
            out.append("  " + " ".join(f"{fg(COLORS[t % len(COLORS)])}■{RESET} t{t}" for t in range(a.tenants)))
        print("\n".join(out), flush=True)

    async def one(session: aiohttp.ClientSession, i: int) -> None:
        nonlocal sent
        t = random.randrange(a.tenants) if a.tenants else -1
        msgs = ([{"role": "system", "content": tenant_prompt(t)}] if t >= 0 else []) + \
               [{"role": "user", "content": a.prompt}]
        headers = dict(base_headers)
        if a.routing_key:
            headers["X-SMG-Routing-Key"] = f"tenant-{t}" if a.routing_key == "tenant" else a.routing_key
        body = {"model": a.model, "messages": msgs, "max_tokens": a.max_tokens, **cfg.BENCH_EXTRA_BODY}
        t0 = time.perf_counter()
        try:
            async with session.post(url, json=body, headers=headers) as r:
                await r.read()
                status, worker = r.status, r.headers.get("x-smg-routed-worker-id", "")
        except aiohttp.ClientError as e:
            status, worker = type(e).__name__, ""
        ms = (time.perf_counter() - t0) * 1000
        async with lock:
            sent += 1
            statuses[status] += 1
            tag = f"{fg(COLORS[t % len(COLORS)])}t{t}{RESET}" if t >= 0 else "  "
            if status == 200 and worker:
                counts[worker] += 1
                trails[worker].append(t)
                lat[worker].append(ms)
                recent.appendleft(f"#{i:<4} {tag:<4} → {names.get(worker, worker):<10} 200  {ms:6.0f} ms")
            else:
                recent.appendleft(f"#{i:<4} {tag:<4} → \033[31m{status}\033[0m {'by ' + names.get(worker, worker) if worker else '(answered by SMG or no worker)'}  {ms:6.0f} ms")

    queue: asyncio.Queue = asyncio.Queue()
    for i in range(1, a.n + 1):
        queue.put_nowait(i)

    async def client(session):
        while True:
            try:
                i = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await one(session, i)
            if a.delay:
                await asyncio.sleep(a.delay)

    async def painter():
        while True:
            draw()
            await asyncio.sleep(0.25)

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        p = asyncio.create_task(painter())
        try:
            await asyncio.gather(*(client(session) for _ in range(a.concurrency)))
        finally:
            p.cancel()
    draw(final=True)


# ------------------------------------------------------------------ watch ---

def watch(a: argparse.Namespace) -> None:
    names = worker_names()
    try:
        base = M.scrape()
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"cannot reach {cfg.SMG_METRICS_URL} ({e.__class__.__name__}). Is smg running?")
    b_workers = M.by_label(base, "smg_worker_requests_total", "worker")
    b_status = M.by_label(base, "smg_http_responses_total", "status_code")
    last_change: dict[str, float] = {}
    prev: dict[str, float] = {}
    start = time.time()
    try:
        while True:
            try:
                s = M.scrape()
            except Exception:  # noqa: BLE001
                print(CLEAR + "lost contact with smg metrics; retrying…", flush=True)
                time.sleep(1)
                continue
            now = time.time()
            workers = M.by_label(s, "smg_worker_requests_total", "worker")
            status = M.by_label(s, "smg_http_responses_total", "status_code")
            health = M.by_label(s, "smg_worker_health", "worker")
            cb = M.by_label(s, "smg_worker_cb_state", "worker")
            active = M.by_label(s, "smg_worker_requests_active", "worker")
            delta = {w: workers.get(w, 0) - b_workers.get(w, 0) for w in set(workers) | set(health)}
            for w, v in delta.items():
                if v != prev.get(w):
                    last_change[w] = now
            prev = delta
            total = sum(max(0, v) for v in delta.values())
            out = [CLEAR + f"{BOLD}SMG routing, live{RESET}   counting since {time.strftime('%H:%M:%S', time.localtime(start))}   (Ctrl-C to stop)",
                   f"{DIM}smg flags: {smg_flags(smg_command())}{RESET}", ""]
            for w in sorted(delta):
                fresh = now - last_change.get(w, 0) < 1.2 and delta[w] > 0
                mark = "\033[1;32m●\033[0m" if fresh else f"{DIM}●{RESET}"
                hs = "healthy" if health.get(w, 1) == 1 else "\033[31munhealthy\033[0m"
                cbs = {0: "", 1: "  \033[31mcircuit OPEN\033[0m", 2: "  \033[33mhalf-open\033[0m"}.get(int(cb.get(w, 0)), "")
                out.append(f" {mark} {BOLD}{names.get(w, w):<10}{RESET} {w.replace('http://', ''):<16} {int(delta[w]):>5}  "
                           f"{bar(int(delta[w]), int(total))}  in flight {int(active.get(w, 0))}  {hs}{cbs}")
            codes = {k: status.get(k, 0) - b_status.get(k, 0) for k in status}
            codes = {k: int(v) for k, v in codes.items() if v > 0}
            if codes:
                out.append("")
                out.append("   responses by status   " + "  ".join(
                    (f"\033[32m{k}\033[0m" if k.startswith("2") else f"\033[31m{k}\033[0m") + f" ×{v}" for k, v in sorted(codes.items())))
            out.append("")
            out.append(f"   {DIM}Send requests from another terminal; the worker that served each one lights up.{RESET}")
            print("\n".join(out), flush=True)
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--watch", action="store_true", help="only watch SMG's metrics; send nothing")
    p.add_argument("--interval", type=float, default=0.5, help="watch refresh, seconds")
    p.add_argument("--url", default=cfg.SMG_URL)
    p.add_argument("-n", type=int, default=40, help="requests to send")
    p.add_argument("-c", "--concurrency", type=int, default=4)
    p.add_argument("--model", default=cfg.MODEL)
    p.add_argument("--tenants", type=int, default=0)
    p.add_argument("--prompt", default="Say hi in three words.")
    p.add_argument("--max-tokens", type=int, default=8)
    p.add_argument("--routing-key", default=None)
    p.add_argument("--target-worker", type=int, default=None)
    p.add_argument("--api-key", default=None)
    p.add_argument("-H", "--header", action="append", default=[])
    p.add_argument("--delay", type=float, default=0.0, help="pause per client between requests, seconds")
    a = p.parse_args()
    if a.watch:
        watch(a)
    else:
        try:
            asyncio.run(traffic(a))
        except KeyboardInterrupt:
            print()


if __name__ == "__main__":
    main()
