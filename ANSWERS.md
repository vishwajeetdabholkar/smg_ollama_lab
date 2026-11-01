# Answers

Read these *after* you have tried. "Reference Mac" means the M4 Max (36 GB) the lab was validated on; your numbers will differ.

---

## Task 1

1. **Requests 2-8 wait.** Ollama's default runs one sequence at a time (one slot). The others sit in Ollama's internal queue, so TTFT grows by about one request's duration per position in the queue. ITL stays flat because only one request is decoding at any moment, at full speed. Throughput is the speed of one request, however many clients you add.
2. **`/health` and `/metrics` both return 404.** A gateway needs `/health` to decide whether a worker is alive and should get traffic (registration and periodic checks). It needs `/metrics` to see an engine's queue and cache state. Without them the gateway is blind to the engine's internals, which is why SMG's own metrics become your main view.
3. **`reasoning` holds Qwen3's hidden chain of thought.** It is often hundreds of tokens, and its length varies a lot from run to run. In a benchmark that adds noise and makes "64 max tokens" mean "64 tokens of thinking, no answer". `reasoning_effort: none` makes Ollama run with thinking off.

**Challenge:** `/api/ps` shows `size_vram` and `context_length`. Raising `OLLAMA_CONTEXT_LENGTH` to 32768 roughly quadruples the KV part of the memory, because every slot reserves a full context.

## Task 2

1. **`external` means "a provider SMG does not run"**, like OpenAI or a vendor API. SMG skips the self-hosted registration steps (`detect_connection_mode`, backend and metadata discovery, so no `/health` probe). It treats the URL as an OpenAI-compatible API, chooses between external workers by load, and does not apply `--policy`.
2. **`--api-key` did two things.**
   - **Clients:** the gateway now *requires* `Authorization: Bearer <key>` from them (401 otherwise).
   - **Workers:** SMG *sends* the key to the worker, which also lets `discover_models` call the worker's `/v1/models`. Without a key it runs in "wildcard mode": it accepts any model name and forwards it, so Ollama answered the `nope:1b` request itself. With the key, SMG knows the real model list and rejects `nope:1b` itself with `model_not_found`.
3. **`/v1/models` on the gateway lists self-hosted models.** `list_models_with` in `endpoints/models.rs` answers from `self_hosted_cards`. External providers' models are deliberately not mixed into the gateway's own inventory, and a caller with a *foreign* token gets a bring-your-own-key fan-out to the providers instead.
4. **For external workers, `find_best_worker` takes `min_by_key(load)`.** That is the least in-flight requests, and ties go to the first worker. Ollama answered quickly enough that loads were often equal, so the first worker got everything. The configured policy is used by the self-hosted HTTP router, not here.
5. **External mode is right for capacity you rent, not run:** OpenAI, Anthropic, Together and other hosted APIs, or another team's endpoint. You want one gateway in front of them for auth, observability and one API surface, but you cannot (and need not) health-check or cache-route them.

**Challenge:** not with `--api-key` alone, because it applies in both directions. `--tenant-api-key tenant:key` adds client keys *on top of* `--api-key`, each mapped to a tenant identity. A client-only key without forwarding is a gateway-auth feature to look for in `middleware/auth.rs`; the point of the exercise is to read the flags' help carefully.

## Task 3

1. **The steps, in order:**
   - `classify_worker_type`
   - `discover_models` (external only)
   - `detect_connection_mode`, `detect_backend`, `discover_metadata`, `discover_dp_info`, `create_local_worker` (all self-hosted)
   - `create_external_workers` (external)
   - `register_workers`, `activate_workers`, `submit_tokenizer_job`, `update_policies`

   Task 2's external worker skipped the self-hosted steps. Task 3's worker skipped `discover_models` and `create_external_workers`. Raw Ollama failed at **`detect_connection_mode`**: `probe_http`, then `try_http_reachable`, then `GET <url>/health` = 404.
2. **A worker that cannot prove it is alive should never receive user traffic.** Probing first means a typo'd URL, or an engine still loading weights, fails registration loudly instead of failing user requests. It also tells SMG which protocol to use (HTTP/1.1 vs HTTP/2, HTTP vs gRPC).
3. **On the reference Mac, each hop (adapter, then gateway) added a few milliseconds** at concurrency 1. That is small next to a ~100 ms TTFT and a ~300 ms E2E. It matters for very short requests (embeddings, classification) or very tight SLOs. A real deployment also has no adapter: vLLM and SGLang serve `/health` themselves.
4. **SMG's HTTP router treats the first model a worker lists as the model it serves.** Ollama lists *every* downloaded model, newest first, so without the filter a worker might advertise whichever model you happened to pull last.

**`--health-check-endpoint`:** registration always probes `/health` (`http_health_url` in `util.rs` hard-codes it). Your `--health-check-endpoint` is used afterwards, by the *periodic* health checker (`http_health_check` in `worker/worker.rs`).

**Challenge:** point the adapter at a wrong `--upstream` port, or kill only the adapter: `/health` fails and `/workers` shows `is_healthy: false` after `--health-failure-threshold` consecutive failures. With the default `--health-check-interval-secs 60` and threshold 3, that takes minutes. `--health-check-interval-secs 2 --health-failure-threshold 1` takes about 2 s.

## Task 4

1. **Ollama answered it, not SMG.** In self-hosted HTTP mode SMG forwards `/v1/messages` and `/v1/responses` to the worker unchanged (after validating the request and resolving `--model-alias` names), so the engine has to speak those APIs itself. Ollama does: its log shows `POST "/v1/messages"` and `POST "/v1/responses"` arriving, not `/v1/chat/completions`. The external (OpenAI-provider) router in 1.13 has no Messages implementation, so it answered "not yet implemented". SMG implements these APIs itself (chat template, tokenizing, parsing the output) only in gRPC mode, which needs a gRPC worker such as vLLM, SGLang, TensorRT-LLM or, on Apple Silicon, MLX.
2. **`x-smg-routed-worker-id`** tells you which worker served a request, without searching logs. Use it to check a policy's placement, chase a "this one worker is slow" complaint, or confirm a canary gets traffic.
3. **It decouples clients from topology.** You can add, remove or move workers, mix engines, or roll out a new model version with no client change. Clients ask for capability (a model name), and the gateway owns placement.
4. **`--model-alias fast=qwen3:0.6b`** lets clients code against a stable name (`fast`, `default`, `gpt-4o-compatible`). To upgrade, point the alias at the new model; clients do not redeploy.

**Challenge:** start SMG with `-- --api-key secret`. Requests without `Authorization: Bearer secret` get 401; `bench.py --api-key secret` works. The key is also forwarded to the adapters and Ollama, which ignore it.

## Task 5

1. **In HTTP mode SMG forwards bytes.** It does not tokenize the prompt or parse the token stream, so it does not know when "the first token" happened, or how many tokens there were. It measures request duration (`smg_router_request_duration_seconds`). In gRPC mode SMG tokenizes and detokenizes itself, so it reports TTFT and per-token metrics. This is why `bench.py` measures TTFT on the client side.
2. **A histogram stores counts per bucket, not individual values.** `histogram_quantile` assumes values are spread evenly inside a bucket and interpolates. It is badly off when the buckets are wide around the quantile you care about, e.g. a 0.5-1.0 s bucket when your p99 is 0.6 s, or when the true value is above the last finite bucket.
3. **Questions you can now answer:**
   - How many requests failed, and with which status codes or error codes?
   - How was traffic split across workers and models?
   - What are the latency percentiles?
   - How many requests were retried?
   - Is any worker unhealthy, or does it have an open circuit breaker?
   - How many requests did admission control reject?
4. **Scrape before and after, and subtract.** Or, as Prometheus does, store samples over time and use `rate()` / `increase()` over a window.

## Task 6

1. **ITL rises with more slots.** Each decode step processes more sequences and takes longer, so you get more tokens per step but not 4x. The GPU is partly saturated already, and `qwen3:0.6b` is so small that overheads (sampling, HTTP, scheduling) are a large share of the time. On the reference Mac: about 1.8x throughput at 4 slots.
2. **Each slot reserves a KV cache big enough for a full context.** That is the attention keys and values for every layer, for up to `OLLAMA_CONTEXT_LENGTH` tokens. So total KV ≈ slots × context × (per-token KV bytes). The model weights are a fixed, smaller part.
3. **q8_0 stores the KV cache in 8 bits instead of 16.** That halves KV memory at a small accuracy cost and some speed. It requires flash attention. For short lab prompts it is invisible; for long-context retrieval or very precise outputs, test quality before using it.
4. **A slot is one request's seat:** its sequence state and KV cache. `n_slots` = `OLLAMA_NUM_PARALLEL`. Busy slots are decoded together as a batch; when all slots are busy, new requests wait.

**Challenge:** throughput stops improving when the GPU is compute-bound. ITL climbs about as fast as the slot count grows, so tokens/s stays flat. On the reference Mac, 4 to 8 slots added little for short requests.

## Task 7

1. **Each tenant prompt is about 2k tokens, and the cache holds about 32k tokens per worker:**
   - About 16 tenant prompts fit in one worker's cache.
   - **8 tenants (~16k tokens)** fit in *each* worker even under round_robin, so most requests hit the cache whatever the policy. The gap is small (about 1.14x on the reference Mac).
   - **32 tenants (~64k tokens)** do not fit in one worker. round_robin sends every tenant to both workers, so caches keep evicting and prompts miss. cache_aware keeps each tenant on one worker (16 per worker, which just fits), so most requests hit. The gap was larger: about 1.32x req/s and half the TTFT p50.
2. **The two branches:**
   - **`tree_match`:** the request's prefix matched a worker's prefix tree above `--cache-threshold` (default 0.3), so it went there.
   - **`expected_wait_fallback`:** the match was too small, or the matched worker was too busy (the balance thresholds), so it went to the worker with the least expected wait.

   You see both branches counted in `smg_cache_aware_policy_branch_total`.
3. **Ollama's prompt cache works on prefixes,** and the same text prefix produces the same token prefix, so a text-based tree predicts cache hits well. In gRPC mode SMG tokenizes and can match on exact tokens, or use the engine's real cache events (`--kv-index`), which is more precise. That matters with templates and special tokens.
4. **No.** Both servers share one GPU, so two workers split the compute instead of doubling it. A second worker on one machine helps with *cache capacity* (two caches), isolation and restarts, not raw compute. With separate GPUs or machines, it would add compute.

**Challenge:** one or a few very hot tenants (`--tenants 1` or `2`) at high concurrency. cache_aware pins them to one worker while the other idles, until the balance thresholds spill traffic. round_robin spreads the load and wins.

## Task 8

1. **The three flags:**
   - **`--max-concurrent-requests`:** how many requests may be in flight inside the gateway at once.
   - **`--queue-size` / `--queue-timeout-secs`:** how many more may *wait* for a slot, and for how long. A full queue answers 429 `admission_queue_full`; waiting longer than the timeout answers 503 `admission_queue_timeout`.
   - **`--rate-limit-tokens-per-second`:** a token bucket that limits how fast requests are *admitted* (one token per request, bursting up to `--max-concurrent-requests`). Requests over the rate wait in the same queue; with `--queue-size 0` they get 429 immediately. On the reference Mac, 2/s with burst 4 admitted about 13 of 68 requests in 5 s.
2. **Without a limit, all 32 requests pile into Ollama's internal queue,** and *everyone* waits behind everyone. With limit 8 and no queue, the 8 admitted requests run at near-unloaded speed. The rest get an immediate 429, which a client or load balancer can retry elsewhere or later. Fast failure is information; a 10-second wait is not.
3. **Start at workers × NUM_PARALLEL,** e.g. 2 × 4 = 8, maybe ×1.5 to keep the engines busy between requests.
   - **Much bigger:** the gateway admits work the engines cannot run, so it queues invisibly inside Ollama and the gateway's limits do nothing.
   - **Much smaller:** engines sit idle while the gateway rejects requests.
4. **In `concurrency.rs`, a request first takes a token from the token bucket** (`token_bucket.rs`). If none is available and the queue has room, it is placed in a bounded queue and waits there, holding no worker resources, until a permit frees or the queue timeout expires.

## Task 9

1. **Retries.** Requests sent to the dead worker failed at connect time, *before* any bytes were streamed, and SMG retried them on the healthy worker. `smg_worker_retries_total{endpoint="chat"}` went up while client errors stayed at 0. Health checks and the circuit breaker then stopped new traffic to it. On the reference Mac: 616 failures without the gateway, 0 with it (default retries), and 9 with `--disable-retries`.
2. **A worker that just failed 10 times in a row is likely to fail again,** or to be fragile while it warms up. The breaker *opens* (state 1): no traffic. After `--cb-timeout-duration-secs` it goes *half-open* (2) and lets a few requests through. After `--cb-success-threshold` successes it *closes* (0). This avoids hammering a recovering engine, at the price of slower readmission. Tune with `--cb-failure-threshold`, `--cb-success-threshold`, `--cb-timeout-duration-secs` and `--cb-window-duration-secs`.
3. **Once tokens have been sent to the client, SMG cannot "unsend" them.** Replaying on another worker would produce a different continuation and a corrupted response. The client sees a broken stream (in chaos.py: `ClientPayloadError ... not completed`). Only the client can safely retry the whole request.
4. **With `--disable-retries`, each request that hit the dead worker became a client error** (9 on the reference Mac) until health checks and the circuit breaker removed it. You disable retries when requests are not safe to repeat: tool calls with side effects, very expensive generations, or when a client already retries and double retries would amplify load during an outage.

**Challenge:** killing the adapter takes `/health` down together with the data path, so health checks and connection errors agree and SMG reacts quickly. A worker whose `/health` says OK but whose requests fail is the harder case. Health checks keep readmitting it, and only the per-request signals (retries, circuit breaker) protect clients. That is why real systems need both.

## Task 10

There is no single answer. Things that usually matter, in order, on one Mac:
- **`OLLAMA_NUM_PARALLEL` above 1** is the biggest jump. The naive config queues every request inside Ollama.
- **Two workers with `cache_aware`** when the tenants' prompts do not fit in one worker's prompt cache. On one GPU, more workers do not add compute.
- **Admission control** (`--max-concurrent-requests` ≈ total slots, with a small queue) raises the score only when the load would otherwise push *every* request past the SLO.
- **q8_0 KV cache** helps only if memory limits the slots you can afford.

On the reference Mac (on battery, Low Power Mode on) the starting config passed 2 req/s with 96% of requests within the SLO, and failed at 3 req/s with 23%. A config with 2 workers, 8 slots each and `cache_aware` reached 100% at 2 req/s and 94% at 3 req/s. Plugged in, both are higher.
