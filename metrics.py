"""Summarize what the SMG gateway knows, from its Prometheus endpoint (:29100/metrics).

  python metrics.py                         # one summary
  python metrics.py --save results/t5_metrics.json
  python metrics.py --raw smg_worker_       # every sample whose name starts with a prefix

Counters only go up from the moment smg started. To measure one experiment,
restart smg (or note the numbers before and after).
"""

from __future__ import annotations

import argparse
import json
import os
import re

import requests

import lab_config as cfg

_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+(\S+)")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def scrape(url: str = cfg.SMG_METRICS_URL) -> list[tuple[str, dict[str, str], float]]:
    samples = []
    for line in requests.get(url, timeout=10).text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE.match(line)
        if not m:
            continue
        name, _, raw, value = m.groups()
        try:
            samples.append((name, dict(_LABEL.findall(raw or "")), float(value)))
        except ValueError:
            pass
    return samples


def total(samples, name: str, **match: str) -> float:
    return sum(v for n, lab, v in samples if n == name and all(lab.get(k) == x for k, x in match.items()))


def by_label(samples, name: str, label: str, **match: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for n, lab, v in samples:
        if n == name and all(lab.get(k) == x for k, x in match.items()):
            out[lab.get(label, "")] = out.get(lab.get(label, ""), 0.0) + v
    return out


def quantile(samples, base: str, q: float, **match: str) -> float | None:
    """Estimate a quantile from a Prometheus histogram, like PromQL's histogram_quantile()."""
    buckets: dict[float, float] = {}
    for n, lab, v in samples:
        if n == f"{base}_bucket" and all(lab.get(k) == x for k, x in match.items()) and "le" in lab:
            le = float("inf") if lab["le"] == "+Inf" else float(lab["le"])
            buckets[le] = buckets.get(le, 0.0) + v
    if not buckets or buckets[max(buckets)] <= 0:
        return None
    rank, prev_b, prev_c = q * buckets[max(buckets)], 0.0, 0.0
    for b in sorted(buckets):
        c = buckets[b]
        if c >= rank:
            if b == float("inf"):
                return prev_b
            return prev_b + (b - prev_b) * ((rank - prev_c) / (c - prev_c) if c > prev_c else 0.0)
        prev_b, prev_c = b, c
    return prev_b


def summary(samples) -> dict:
    chat = {"endpoint": "chat"}
    s = {
        "responses_by_status": by_label(samples, "smg_http_responses_total", "status_code", path="/v1/chat/completions"),
        "rejections_by_code": {k: v for k, v in by_label(samples, "smg_http_responses_total", "error_code").items() if k},
        "requests_by_worker": by_label(samples, "smg_worker_requests_total", "worker"),
        "active_by_worker": by_label(samples, "smg_worker_requests_active", "worker"),
        "requests_by_model": by_label(samples, "smg_router_requests_total", "model"),
        "chat_latency_s": {f"p{int(q * 100)}": quantile(samples, "smg_router_request_duration_seconds", q, **chat)
                           for q in (0.5, 0.9, 0.99)},
        "retries_chat": total(samples, "smg_worker_retries_total", worker_type="regular", endpoint="chat"),
        "retries_exhausted": total(samples, "smg_worker_retries_exhausted_total"),
        "worker_errors": total(samples, "smg_worker_errors_total"),
        "worker_health": by_label(samples, "smg_worker_health", "worker"),
        "circuit_breaker_state": by_label(samples, "smg_worker_cb_state", "worker"),
        "admission_inflight": total(samples, "smg_admission_inflight"),
        "rate_limit": by_label(samples, "smg_http_rate_limit_total", "result"),
        "cache_aware_branches": by_label(samples, "smg_cache_aware_policy_branch_total", "branch"),
    }
    return s


def show(s: dict) -> None:
    def kv(d: dict, fmt="{:.0f}") -> str:
        return "  ".join(f"{k}={fmt.format(v)}" for k, v in d.items()) or "-"

    print(f"gateway metrics from {cfg.SMG_METRICS_URL}")
    print(f"  chat responses by status   {kv(s['responses_by_status'])}")
    print(f"  rejections by error code   {kv(s['rejections_by_code'])}")
    print(f"  requests per model         {kv(s['requests_by_model'])}")
    print(f"  requests per worker        {kv(s['requests_by_worker'])}")
    print(f"  in flight per worker now   {kv(s['active_by_worker'])}")
    lat = {k: (v * 1000 if v is not None else float('nan')) for k, v in s["chat_latency_s"].items()}
    print(f"  chat latency (ms, server)  {kv(lat)}")
    print(f"  retries (chat) / exhausted {s['retries_chat']:.0f} / {s['retries_exhausted']:.0f}   worker errors {s['worker_errors']:.0f}")
    print(f"  worker health (1=healthy)  {kv(s['worker_health'])}")
    print(f"  circuit breaker (0=closed, 1=open, 2=half-open)  {kv(s['circuit_breaker_state'])}")
    print(f"  rate limiter               {kv(s['rate_limit'])}")
    if s["cache_aware_branches"]:
        print(f"  cache_aware decisions      {kv(s['cache_aware_branches'])}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--save", default=None, help="also write the summary as JSON")
    p.add_argument("--raw", default=None, metavar="PREFIX", help="print raw samples with this name prefix")
    a = p.parse_args()
    try:
        samples = scrape()
    except requests.RequestException as e:
        raise SystemExit(f"cannot reach {cfg.SMG_METRICS_URL} ({e}). Is smg running? python labctl.py status")
    if a.raw:
        for n, lab, v in samples:
            if n.startswith(a.raw):
                print(f"{n}{lab} {v:g}")
        return
    s = summary(samples)
    show(s)
    if a.save:
        os.makedirs(os.path.dirname(os.path.abspath(a.save)), exist_ok=True)
        with open(a.save, "w") as f:
            json.dump(s, f, indent=2)
        print(f"wrote {a.save}")


if __name__ == "__main__":
    main()
