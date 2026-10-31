# 05 - Reading the source

Every behavior you observe in this lab is a few hundred lines of Rust you can read. You do not need to know Rust well; read the function names, comments and `if`s.

## Getting the code at the right version

```bash
git clone https://github.com/lightseekorg/smg.git && cd smg && git checkout v1.13.0
# or browse: https://github.com/lightseekorg/smg/tree/v1.13.0/model_gateway/src
```

Search with `git grep -n "<text>"`, or use your editor's "go to definition".

## Map: what you saw, then where it lives

| Lab | Behavior | File (under `model_gateway/src/`) | Look for |
|---|---|---|---|
| 2 | worker classified as external vs local | `workflow/steps/classify.rs` | the numbered rules in the doc comment |
| 2 | "wildcard mode", model discovery needs a key | `workflow/steps/external/discover_models.rs` | `resolve_discovery_api_key`, `DiscoverModelsStep` |
| 2 | external models missing from `/v1/models` | `endpoints/models.rs` | `list_models_with`, `self_hosted_cards` |
| 2 | `--policy` ignored for external workers | `routers/common/worker_selection.rs` | `find_best_worker` (`min_by_key(load)`) |
| 3 | raw Ollama rejected | `workflow/steps/local/detect_connection.rs` | `probe_http`, error text "health check failed for explicitly configured" |
| 3 | `/health` is hard-coded | `workflow/steps/util.rs` | `http_health_url`, `try_http_reachable` |
| 3 | the workflow steps | `workflow/mod.rs`, `workflow/data.rs` | step ids in the log |
| 3, 9 | periodic health checks after registration | `worker/worker.rs` | `http_health_check`, `health_endpoint` |
| 4 | auth, request ids | `middleware/auth.rs`, `middleware/request_id.rs` | |
| 4-9 | HTTP routing, retries | `routers/http/router.rs` | `select_worker_for_model`, retry loop |
| 7 | policies | `policies/round_robin.rs`, `cache_aware.rs`, `power_of_two.rs`, `least_load.rs`, `prefix_hash.rs`, `consistent_hashing.rs` | `select_worker` |
| 8 | concurrency limit + queue | `middleware/concurrency.rs` | permits, queue |
| 8 | rate limit | `middleware/token_bucket.rs` | refill rate, burst |
| 9 | circuit breaker | `worker/circuit_breaker.rs` | states closed / open / half-open |
| 5 | metrics | `middleware/metrics.rs` and `observability/` | metric names you scraped |

## Three reading exercises

1. **Task 3, in the code.** In `detect_connection.rs`, find where `probe_http` is called and what happens when it fails. Then in `util.rs`, explain why `--health-check-endpoint` does not change the URL probed *during registration*. Where is `--health-check-endpoint` used instead? Hint: `git grep -n health_check_endpoint`.
2. **Task 7, in the code.** In `policies/cache_aware.rs`, find where the matched-prefix ratio is compared to `cache_threshold`, and where the balance thresholds make it fall back. Map each branch to the `smg_cache_aware_policy_branch_total{branch=...}` labels you saw.
3. **Task 9, in the code.** In `worker/circuit_breaker.rs`, write down the state machine: what moves it from closed to open, from open to half-open, and from half-open back to closed. Which `--cb-*` flag feeds each transition?

## A good habit

When the lab surprises you, find the log line, then `git grep` its text. The log line's source location is printed too, e.g. `model_gateway/src/workflow/steps/external/discover_models.rs:284`, so you can open that exact line.
