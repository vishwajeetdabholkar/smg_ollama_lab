"""Run steady load and break things on a schedule; print what clients saw, second by second.

  python chaos.py --duration 30 --kill ollama-b@8 --restore ollama-b@18
  python chaos.py --duration 30 --kill adapter-b@8            # the worker's /health dies too
  python chaos.py --base-url http://127.0.0.1:11600 ...        # same load, without the gateway

--kill NAME@T stops a labctl component T seconds in; --restore NAME@T starts it
again with the settings labctl normally uses (lab.env + defaults).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import subprocess
import sys
import time

import aiohttp

import bench
import lab_config as cfg


def parse_event(s: str) -> tuple[str, float]:
    name, _, t = s.partition("@")
    return name, float(t)


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=cfg.SMG_URL)
    p.add_argument("--model", default=cfg.MODEL)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--duration", type=float, default=30)
    p.add_argument("--max-tokens", type=int, default=48)
    p.add_argument("--kill", action="append", default=[], type=parse_event, metavar="NAME@SECONDS")
    p.add_argument("--restore", action="append", default=[], type=parse_event, metavar="NAME@SECONDS")
    p.add_argument("--out", default=None, help="write the timeline as JSON")
    a = p.parse_args()

    url = f"{a.base_url}/v1/chat/completions"
    per_sec: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    errors: collections.Counter = collections.Counter()
    events = sorted([(t, "down", n) for n, t in a.kill] + [(t, "up", n) for n, t in a.restore])
    log: list[str] = []
    t0 = time.perf_counter()

    async def client(session: aiohttp.ClientSession, cid: int) -> None:
        i = 0
        while time.perf_counter() - t0 < a.duration:
            msgs = bench.build_messages("short", cid * 100_000 + i, 0)
            r = await bench.one_request(session, url, a.model, msgs, a.max_tokens, cfg.BENCH_EXTRA_BODY, {})
            sec = int(time.perf_counter() - t0)
            if r.ok:
                per_sec[sec]["ok"] += 1
            else:
                per_sec[sec]["err"] += 1
                errors[f"{r.status} {r.error[:90]}"] += 1
                await asyncio.sleep(0.1)
            i += 1

    async def chaos() -> None:
        for t, action, name in events:
            delay = t - (time.perf_counter() - t0)
            if delay > 0:
                await asyncio.sleep(delay)
            stamp = f"t={time.perf_counter() - t0:5.1f}s  labctl {action} {name}"
            print(f"\033[1;33m>>> {stamp}\033[0m", flush=True)
            log.append(stamp)
            await asyncio.to_thread(subprocess.run, [sys.executable, "labctl.py", action, name],
                                    cwd=cfg.LAB_DIR, stdout=subprocess.DEVNULL)

    async def ticker() -> None:
        shown = 0
        while shown < int(a.duration):
            await asyncio.sleep(0.25)
            now = int(time.perf_counter() - t0)
            while shown < min(now, int(a.duration)):
                c = per_sec.get(shown, collections.Counter())
                bar = "\033[32m" + "#" * min(c["ok"], 60) + "\033[31m" + "x" * min(c["err"], 30) + "\033[0m"
                print(f"  {shown:>3}s  ok={c['ok']:>3} err={c['err']:>3}  {bar}", flush=True)
                shown += 1

    print(f"load: {a.concurrency} clients -> {url} for {a.duration:.0f}s   (# = success, x = failure)")
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(limit=0)) as session:
        await asyncio.gather(*(client(session, c) for c in range(a.concurrency)), chaos(), ticker())

    total_ok = sum(c["ok"] for c in per_sec.values())
    total_err = sum(c["err"] for c in per_sec.values())
    print(f"\ntotal: {total_ok} ok, {total_err} failed")
    for e, n in errors.most_common(5):
        print(f"  {n}x {e}")
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as f:
            json.dump({"base_url": a.base_url, "ok": total_ok, "failed": total_err, "errors": dict(errors),
                       "events": log, "per_second": {k: dict(v) for k, v in sorted(per_sec.items())}}, f, indent=2)
        print(f"wrote {a.out}")


if __name__ == "__main__":
    asyncio.run(main())
