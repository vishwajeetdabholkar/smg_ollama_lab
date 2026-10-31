# 04 - Cheatsheet

## Every new terminal

```bash
cd smg_ollama_lab && source .venv/bin/activate
```

## labctl

```bash
python labctl.py status
python labctl.py up ollama-a [--env OLLAMA_NUM_PARALLEL=4 ...]
python labctl.py up adapter-a [--model qwen3:0.6b]
python labctl.py up smg [--mode http|external] [--workers a,b] [-- <smg flags>]
python labctl.py up stack --workers a,b [--env ...] [-- <smg flags>]    # ollamas + adapters + smg
python labctl.py up smg --dry-run -- --policy cache_aware                # print the command only
python labctl.py logs smg -n 50
python labctl.py down [smg|ollama-b|...|all]
python labctl.py pull <model>
```

## Raw commands (what labctl runs)

```bash
OLLAMA_HOST=127.0.0.1:11500 OLLAMA_CONTEXT_LENGTH=8192 OLLAMA_NUM_PARALLEL=4 ollama serve
python scripts/ollama_adapter.py --port 11600 --upstream http://127.0.0.1:11500 --model qwen3:0.6b
smg launch --host 127.0.0.1 --port 30100 --prometheus-port 29100 \
  --backend vllm --worker-urls http://127.0.0.1:11600 http://127.0.0.1:11601 --policy cache_aware
smg launch --host 127.0.0.1 --port 30100 --prometheus-port 29100 \
  --backend openai --worker-urls http://127.0.0.1:11500 --api-key ollama          # external mode
```

## Measuring

```bash
python bench.py [--base-url URL] --workload short|long_prompt|shared_prefix|multi_tenant \
  [--tenants 32] --concurrency 1,4,8 [--num-requests N] [--max-tokens 64] [--api-key K] --out results/x.json
python metrics.py [--save results/x.json] [--raw smg_worker_]
python chaos.py --duration 30 --kill ollama-b@8 --restore ollama-b@18 [--base-url URL] --out results/x.json
python capstone.py [--config capstone.env] [--no-restart]
python check.py <1-10|all>
```

## Gateway endpoints (:30100)

| Endpoint | What |
|---|---|
| `GET /readiness` | ready + healthy worker count |
| `GET /workers` | registry: url, runtime_type, models, health, load |
| `GET /v1/models` | self-hosted models (and aliases) |
| `POST /v1/chat/completions` | OpenAI chat |
| `POST /v1/messages` | Anthropic Messages (HTTP mode) |
| `POST /v1/responses` | OpenAI Responses |
| response header `x-smg-routed-worker-id` | which worker served it (HTTP mode) |
| request/response header `x-request-id` | trace id; yours is kept if you send one |

## Ollama endpoints (:11500)

`GET /` · `/api/version` · `/api/tags` (downloaded) · `/api/ps` (loaded: memory, context) · `/api/chat` (native) · `/v1/chat/completions` · `/v1/models`. No `/health`, no `/metrics`.

## SMG flags by topic

| Topic | Flags |
|---|---|
| workers | `--worker-urls`, `--backend {vllm,sglang,openai,...}`, `--model-alias a=b` |
| routing | `--policy {round_robin,random,cache_aware,power_of_two,least_load,prefix_hash,consistent_hashing}`, `--cache-threshold`, `--balance-abs-threshold`, `--balance-rel-threshold` |
| admission | `--max-concurrent-requests`, `--queue-size`, `--queue-timeout-secs`, `--rate-limit-tokens-per-second` |
| health | `--health-check-interval-secs`, `--health-failure-threshold`, `--health-success-threshold`, `--disable-health-check` |
| retries | `--retry-max-retries`, `--retry-initial-backoff-ms`, `--retry-max-backoff-ms`, `--disable-retries` |
| circuit breaker | `--cb-failure-threshold`, `--cb-success-threshold`, `--cb-timeout-duration-secs`, `--disable-circuit-breaker` |
| auth | `--api-key` (clients AND workers), `--tenant-api-key tenant:key` |
| observability | `--prometheus-port`, `--prometheus-duration-buckets`, `--log-level`, `--log-json`, `--request-id-headers` |

## Metrics worth knowing

`smg_http_responses_total{status_code,error_code}` · `smg_worker_requests_total{worker}` · `smg_worker_requests_active` · `smg_router_request_duration_seconds` (histogram) · `smg_worker_retries_total` · `smg_worker_cb_state` (0 closed, 1 open, 2 half-open) · `smg_worker_health` · `smg_http_rate_limit_total` · `smg_admission_queue_rejected_total` · `smg_cache_aware_policy_branch_total{branch}`

## When something is stuck

```bash
python labctl.py status                 # who is running
lsof -nP -iTCP:30100 -sTCP:LISTEN       # who holds a port
python labctl.py logs smg -n 80         # why it did not start
python labctl.py down all               # clean slate
```
