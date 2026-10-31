# SMG + Ollama Lab Guide

Ten tasks. Each one follows the same loop, and the loop is the point:

1. **Predict**: write down what you expect *before* running anything (a notes file is fine).
2. **Do**: type the commands yourself. Nothing runs automatically.
3. **Observe**: compare what happened with your prediction.
4. **Explain**: answer the questions. Answers are in `ANSWERS.md`, so read them *after* you try.
5. **Check**: `python check.py <task>` confirms you saved the results, then lists what you should now be able to explain.

Each task also ends with a **challenge** that has no step-by-step instructions.

Time: about 4-6 hours in total. Tasks 1-5 teach SMG's moving parts; Tasks 6-10 teach optimization.

Keep `docs/04_cheatsheet.md` open while you work. Read `docs/01_concepts.md` before Task 1 (15 minutes).

---

## Setup (once)

```bash
cd smg_ollama_lab
bash setup.sh                 # venv + requirements + lab.env + model download + verify
source .venv/bin/activate     # in EVERY new terminal
```

**Checkpoint:** setup ends with `Environment verified.`

### Before you measure anything

- **Plug in your laptop and turn Low Power Mode off.** On battery with Low Power Mode on, an M-series Mac runs the GPU at about half speed, and every number in this lab halves with it.
- **Close heavy apps.** Browsers with many tabs and video calls compete for the same GPU.
- **Only compare runs made back to back under the same conditions.** The numbers in this guide come from an M4 Max with 36 GB; yours will differ, but the *shape* of the results should match.

### Ports used by the lab

| What | Address |
|---|---|
| Ollama A, B (lab-owned) | `127.0.0.1:11500`, `127.0.0.1:11501` |
| Adapter A, B (Task 3 on) | `127.0.0.1:11600`, `127.0.0.1:11601` |
| SMG gateway | `127.0.0.1:30100` |
| SMG metrics | `127.0.0.1:29100/metrics` |

Your normal Ollama app (port 11434) is never touched. All Ollama servers share the same downloaded models in `~/.ollama/models`.

The commands below use `qwen3:0.6b` (`MODEL`) and `llama3.2:1b` (`MODEL_B`). If you changed them in `lab.env`, substitute your names.

---

## Task 1 - Know your engine: Ollama alone

**Goal:** a baseline, and a clear picture of what Ollama does and does not offer a gateway.

**Predict:** With 1, 4 and 8 clients sending requests at the same time, how will throughput (req/s) change?

**Do.** Terminal 1 runs Ollama in the foreground so you can watch its log:

```bash
OLLAMA_HOST=127.0.0.1:11500 OLLAMA_CONTEXT_LENGTH=8192 ollama serve
```

Terminal 2: explore it.

```bash
curl -s localhost:11500/                    # "Ollama is running"
curl -s localhost:11500/api/version
curl -s localhost:11500/api/tags | python -m json.tool | head -20    # downloaded models
curl -s localhost:11500/v1/models | python -m json.tool              # same, OpenAI format
curl -si localhost:11500/health | head -1                            # remember this answer
curl -si localhost:11500/metrics | head -1                           # and this one

# Ollama's native API ...
curl -s localhost:11500/api/chat -d '{"model":"qwen3:0.6b","messages":[{"role":"user","content":"Say hi"}],"stream":false,"think":false}' | python -m json.tool
# ... and its OpenAI-compatible API (this is the one SMG speaks)
curl -s localhost:11500/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:0.6b","messages":[{"role":"user","content":"Say hi"}],"max_tokens":20}' | python -m json.tool
# same request with thinking off
curl -s localhost:11500/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:0.6b","messages":[{"role":"user","content":"Say hi"}],"max_tokens":20,"reasoning_effort":"none"}' | python -m json.tool

curl -s localhost:11500/api/ps | python -m json.tool      # loaded models: memory, context length
```

Now the baseline. `bench.py` is a load generator: N clients, each sending a request as soon as its previous one finishes.

```bash
python bench.py --base-url http://127.0.0.1:11500 --concurrency 1,4,8 --out results/t1_direct.json
```

Watch Terminal 1 while it runs.

**Observe:** Look at req/s, TTFT (time to first token) and ITL (time between tokens) as concurrency grows. Then run `ollama ps` with `OLLAMA_HOST=127.0.0.1:11500`.

**Explain**

1. Throughput barely moves from 4 to 8 clients, but TTFT keeps growing. What is happening to requests 2-8? Hint: ITL stays flat.
2. Which two endpoints that vLLM or SGLang serve did Ollama answer with 404? Why would a gateway care about each of them?
3. What does the response's `reasoning` field contain without `reasoning_effort: none`, and why does `bench.py` always disable it?

**Challenge:** Using only `curl` and `/api/ps`, find out how much memory the loaded model takes and what context length it was loaded with. Change `OLLAMA_CONTEXT_LENGTH` to 32768, restart, and compare.

**Stop** Ollama with Ctrl-C when done. From now on, `labctl.py` starts the servers in the background. It prints every command it runs, and those are the same commands you just typed.

```bash
python check.py 1
```

---

## Task 2 - The first gateway: SMG in "external" mode

**Goal:** put SMG in front of Ollama the simplest possible way, and discover what that mode can and cannot do.

```bash
python labctl.py up ollama-a
```

**Predict:** SMG has `--backend openai` for external OpenAI-compatible servers. Will `GET /v1/models` on the gateway list Ollama's models?

**Do.** Terminal 3 runs SMG in the foreground. Read its startup log, especially the `Step started / Step succeeded` lines (the *worker registration workflow*).

```bash
smg launch --host 127.0.0.1 --port 30100 --prometheus-port 29100 \
  --backend openai --worker-urls http://127.0.0.1:11500
```

Terminal 2:

```bash
curl -s localhost:30100/readiness
curl -s localhost:30100/workers | python -m json.tool       # note runtime_type, models
curl -s localhost:30100/v1/models
curl -s localhost:30100/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:0.6b","messages":[{"role":"user","content":"Say hi"}],"max_tokens":20,"reasoning_effort":"none"}'
curl -s localhost:30100/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"nope:1b","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'      # who answered?
curl -s localhost:30100/v1/messages -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:0.6b","max_tokens":30,"messages":[{"role":"user","content":"Say hi"}]}'   # Anthropic API
```

Find the log line that explains the empty model list. Search for `wildcard`.

Now restart SMG (Ctrl-C in Terminal 3) with an API key:

```bash
smg launch --host 127.0.0.1 --port 30100 --prometheus-port 29100 \
  --backend openai --worker-urls http://127.0.0.1:11500 --api-key ollama
```

```bash
curl -si localhost:30100/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:0.6b","messages":[{"role":"user","content":"hi"}],"max_tokens":5}' | head -1   # no key
H='Authorization: Bearer ollama'
curl -s localhost:30100/workers -H "$H" | python -m json.tool | grep -A3 '"models"'
curl -s localhost:30100/v1/chat/completions -H "$H" -H 'Content-Type: application/json' \
  -d '{"model":"nope:1b","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'      # who answered now?
python bench.py --api-key ollama --concurrency 1,4,8 --out results/t2_external.json
```

Last experiment: two Ollama servers behind external mode.

```bash
python labctl.py up ollama-b
# Ctrl-C SMG, then:
smg launch --host 127.0.0.1 --port 30100 --prometheus-port 29100 --backend openai --api-key ollama \
  --worker-urls http://127.0.0.1:11500 http://127.0.0.1:11501 --policy round_robin
```

```bash
A=$(grep -c 'POST.*/v1/chat/completions' logs/ollama-a.log); B=$(grep -c 'POST.*/v1/chat/completions' logs/ollama-b.log)
python bench.py --api-key ollama --concurrency 8 --num-requests 32
echo "ollama-a +$(( $(grep -c 'POST.*/v1/chat/completions' logs/ollama-a.log) - A ))  ollama-b +$(( $(grep -c 'POST.*/v1/chat/completions' logs/ollama-b.log) - B ))"
```

**Observe:** Where did the 32 requests go?

**Explain**

1. What does `runtime_type: external` tell SMG about a worker, and what does it skip because of it?
2. `--api-key` changed two things. What are they? Hint: one concerns clients, one concerns workers.
3. With the key, `/workers` lists the models, but `/v1/models` is still empty. Read `model_gateway/src/endpoints/models.rs` (see `docs/05_reading_the_source.md`) and explain why.
4. Why did `--policy round_robin` not split the traffic? Read `model_gateway/src/routers/common/worker_selection.rs`, `find_best_worker`.
5. When is external mode the *right* choice? Think of OpenAI, Anthropic, or a vendor endpoint you do not run yourself.

**Challenge:** Make the gateway reject clients without a key but *not* send any key to Ollama. Is that possible with `--api-key` alone? Look at `--tenant-api-key` in `smg launch --help`.

Stop SMG with Ctrl-C, then:

```bash
python check.py 2
```

---

## Task 3 - Self-hosted HTTP mode, and why `/health` matters

**Goal:** use SMG's real router (policies, retries, circuit breakers) with Ollama. First it fails, then you find out why in the source, then you fix it.

**Predict:** Will this work?

```bash
smg launch --host 127.0.0.1 --port 30100 --prometheus-port 29100 \
  --backend vllm --worker-urls http://127.0.0.1:11500 --health-check-endpoint /api/version
```

**Do:** run it in Terminal 3 and watch for `detect_connection_mode` in the log. In Terminal 2, `curl -s localhost:30100/readiness`.

Now find the cause in the source. `docs/05_reading_the_source.md` shows how to browse the SMG repo at tag `v1.13.0`. Open:

- `model_gateway/src/workflow/steps/local/detect_connection.rs`, which contains `probe_http`
- `model_gateway/src/workflow/steps/util.rs`, which contains `try_http_reachable` and `http_health_url`

What URL does SMG request, and does `--health-check-endpoint` affect it?

The fix is a small adapter. Read `scripts/ollama_adapter.py` (70 lines) first: what does it add, and what does it pass through? Then, in Terminal 4:

```bash
python scripts/ollama_adapter.py --port 11600 --upstream http://127.0.0.1:11500 --model qwen3:0.6b
```

```bash
curl -si localhost:11600/health | head -1
curl -s localhost:11600/v1/models
```

Ctrl-C SMG and start it pointing at the adapter:

```bash
smg launch --host 127.0.0.1 --port 30100 --prometheus-port 29100 \
  --backend vllm --worker-urls http://127.0.0.1:11600
```

```bash
curl -s localhost:30100/readiness
curl -s localhost:30100/workers | python -m json.tool
curl -s localhost:30100/v1/models | python -m json.tool
curl -si localhost:30100/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:0.6b","messages":[{"role":"user","content":"hi"}],"max_tokens":5,"reasoning_effort":"none"}' | grep -i '^x-'
```

Now measure the cost of each hop: Ollama direct, then the adapter, then the gateway.

```bash
python bench.py --base-url http://127.0.0.1:11500 --concurrency 1,8 --out results/t3_direct.json
python bench.py --base-url http://127.0.0.1:11600 --concurrency 1,8 --out results/t3_adapter.json
python bench.py --base-url http://127.0.0.1:30100 --concurrency 1,8 --out results/t3_gateway.json
```

**Observe:** Compare TTFT p50 at concurrency 1 for the three. How many milliseconds does each hop add?

**Explain**

1. List the registration workflow steps from the log in order. Which steps were skipped for the external worker in Task 2, and which for this one?
2. Why does SMG insist on `/health` *before* registering, instead of just trying a request?
3. The gateway added a few milliseconds. Compared to a TTFT of about 100 ms and an E2E of about 300 ms, does it matter? When would it matter?
4. Why does the adapter filter `/v1/models` to one model? Try it without `--model` and with two models downloaded.

**Challenge:** Make SMG stop routing to the adapter *without* stopping Ollama, by making the adapter's `/health` fail while chat still works. Watch `/workers`. How long does SMG take to notice with default settings? Find the flag that controls this in `smg launch --help`.

Stop the adapter and SMG (Ctrl-C). From now on, use labctl:

```bash
python labctl.py up adapter-a
python labctl.py up smg --workers a          # = the smg command above
python labctl.py status
python check.py 3
```

---

## Task 4 - One gateway, many APIs and models

**Goal:** see what the gateway gives clients that Ollama alone does not.

**Predict:** Ollama speaks OpenAI Chat Completions. Will an Anthropic Messages request work through SMG in HTTP mode, given that it failed in external mode?

**Do**

```bash
python labctl.py up smg --workers a       # if it is not running
J='Content-Type: application/json'
# 1. Anthropic Messages API -> translated by SMG to chat completions
curl -s localhost:30100/v1/messages -H "$J" -d '{"model":"qwen3:0.6b","max_tokens":60,"messages":[{"role":"user","content":"Say hi"}]}' | python -m json.tool
# 2. OpenAI Responses API
curl -s localhost:30100/v1/responses -H "$J" -d '{"model":"qwen3:0.6b","input":"Say hi","max_output_tokens":60}' | python -m json.tool
# 3. streaming, with token usage at the end
curl -sN localhost:30100/v1/chat/completions -H "$J" -d '{"model":"qwen3:0.6b","messages":[{"role":"user","content":"Count to 5"}],"max_tokens":30,"stream":true,"stream_options":{"include_usage":true},"reasoning_effort":"none"}'
# 4. your own request id, echoed back and written to the log
curl -si localhost:30100/v1/chat/completions -H "$J" -H 'x-request-id: my-trace-42' -d '{"model":"qwen3:0.6b","messages":[{"role":"user","content":"hi"}],"max_tokens":5}' | grep -i '^x-'
grep my-trace-42 logs/smg.log | head -3
```

An ordinary SDK works unchanged:

```bash
python - <<'EOF'
from openai import OpenAI
c = OpenAI(base_url="http://127.0.0.1:30100/v1", api_key="unused")
r = c.chat.completions.create(model="qwen3:0.6b", messages=[{"role": "user", "content": "One word: sky color?"}],
                              max_tokens=10, extra_body={"reasoning_effort": "none"})
print(r.choices[0].message.content, r.usage)
EOF
```

**Multi-model:** run two models, one per worker, with an alias.

```bash
python labctl.py up ollama-b
python labctl.py up adapter-b --model llama3.2:1b
python labctl.py up smg --workers a,b -- --model-alias fast=qwen3:0.6b
curl -s localhost:30100/v1/models | python -m json.tool
for m in qwen3:0.6b llama3.2:1b fast nope; do
  printf "%-12s " $m; curl -s -o /dev/null -D - localhost:30100/v1/chat/completions -H "$J" \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}" | grep -iE '^HTTP|routed' | tr -d '\r' | tr '\n' ' '; echo
done
```

**Observe:** Which worker served each model? What happened to `nope`, and who answered: SMG or Ollama?

**Explain**

1. Who translated `/v1/messages` into something Ollama understands? Why did that work in HTTP mode but not in external mode?
2. What is `x-smg-routed-worker-id` good for when you debug?
3. Why is "client picks a model name, gateway picks a worker" a better contract than clients knowing worker URLs?
4. What would `--model-alias` let you do during a model upgrade?

**Challenge:** Add gateway auth (`--api-key secret`) to the multi-model stack. Prove that clients need the key, and that `bench.py --api-key secret` works.

Leave this multi-model stack running (with the alias), then:

```bash
python check.py 4
```

---

## Task 5 - Observability: what the gateway knows

**Goal:** read SMG's metrics and logs, and learn what they can and cannot tell you.

```bash
python labctl.py up adapter-b                   # back to qwen3 on both workers (the default model)
python labctl.py up smg --workers a,b -- --policy round_robin
python bench.py --concurrency 4 --num-requests 40 --out results/t5_bench.json
python metrics.py
curl -s localhost:29100/metrics | grep -v '^#' | sed 's/{.*//; s/ .*//' | sort -u | head -80   # every metric name
```

**Predict:** SMG reports `smg_router_request_duration_seconds`. Will its p50 match the client-side E2E p50 from `bench.py`? Is there a TTFT metric?

**Do:** compare the two p50s. Then search for a TTFT metric:

```bash
python metrics.py --raw smg_router_
curl -s localhost:29100/metrics | grep -i ttft
```

Logs: restart with JSON logs and debug level, send one request, and find all its lines.

```bash
python labctl.py up smg --workers a,b -- --policy round_robin --log-json --log-level debug
curl -s localhost:30100/v1/chat/completions -H 'Content-Type: application/json' -H 'x-request-id: find-me' \
  -d '{"model":"qwen3:0.6b","messages":[{"role":"user","content":"hi"}],"max_tokens":5}' > /dev/null
grep find-me logs/smg.log | python -c "import sys,json; [print(d['level'], d['filename'].split('/')[-1], '-', d['message']) for d in map(json.loads, sys.stdin)]"
python metrics.py --save results/t5_metrics.json
```

**Explain**

1. Why does SMG in HTTP mode have no TTFT or tokens-per-second metrics? What would it need to compute them? Hint: in gRPC mode SMG tokenizes requests itself.
2. `metrics.py` estimates p99 from histogram buckets. Why is it an estimate, and when would it be badly off?
3. Ollama has no `/metrics`. Name three questions you can now answer from SMG's metrics that you could not answer before.
4. Counters reset when SMG restarts. How do you measure one experiment without restarting? Hint: rates and differences.

**Challenge:** `--prometheus-duration-buckets` changes the histogram buckets. Pick buckets that make the p99 estimate accurate for this lab's request latencies, and verify against `bench.py`.

```bash
python labctl.py up smg --workers a,b -- --policy round_robin    # back to normal logs
python check.py 5
```

---

## Task 6 - Tune the engine first

**Goal:** a gateway cannot fix a badly configured engine. Find the right Ollama settings for this machine.

**Predict:** Ollama's default handles one request at a time (Task 1). If you allow 4 in parallel, will throughput at 8 clients go up 4x? What happens to memory?

**Do.** Use one worker, and measure through the gateway:

```bash
python labctl.py down ollama-b adapter-b
python labctl.py up smg --workers a
for np in 1 2 4 8; do
  python labctl.py up ollama-a --env OLLAMA_NUM_PARALLEL=$np
  python bench.py --concurrency 1,4,8,16 --out results/t6_np$np.json
  curl -s localhost:11500/api/ps | python -c "import sys,json; m=json.load(sys.stdin)['models'][0]; print('NUM_PARALLEL=$np  memory GB', round(m['size_vram']/1e9,2))"
done
```

Then pick your best NUM_PARALLEL (call it N) and test memory-saving options:

```bash
N=4
python labctl.py up ollama-a --env OLLAMA_NUM_PARALLEL=$N --env OLLAMA_FLASH_ATTENTION=0
python bench.py --workload long_prompt --concurrency 8 --out results/t6_fa0.json
python labctl.py up ollama-a --env OLLAMA_NUM_PARALLEL=$N --env OLLAMA_FLASH_ATTENTION=1 --env OLLAMA_KV_CACHE_TYPE=q8_0
python bench.py --workload long_prompt --concurrency 8 --out results/t6_q8.json
# check memory after each with /api/ps as above
```

On the reference Mac, NUM_PARALLEL=4 gave about 1.8x the throughput of 1, and memory went from 1.5 GB (1 slot) to 4.4 GB (4) to 8.2 GB (8). The q8_0 KV cache cut 8.2 GB to 4.7 GB, a little slower. Flash attention is already on by default; turning it off cost memory and speed.

**Observe:** Make a small table: NUM_PARALLEL, req/s at 8 clients, TTFT p50, ITL p50, memory.

**Explain**

1. Why is the gain from 1 to 4 parallel far less than 4x? Look at ITL.
2. Why does memory scale with NUM_PARALLEL x CONTEXT_LENGTH? What lives in that memory?
3. What does q8_0 trade away? Why is it fine for this lab and maybe not for long, precise tasks?
4. `logs/ollama-a.log` shows Ollama launching `llama-server` with `n_slots` and a "prompt cache". Read `docs/02_ollama_engine.md`. What is a slot?

**Now commit your choice.** Edit `lab.env` and set `OLLAMA_NUM_PARALLEL=` to your N (and `OLLAMA_KV_CACHE_TYPE` if you want it). From here on, every Ollama labctl starts uses it, including restarts during the chaos test.

```bash
python labctl.py up ollama-a     # restarts with lab.env values
python check.py 6
```

**Challenge:** Find the NUM_PARALLEL beyond which throughput stops improving *on your machine*, and explain why it stops.

---

## Task 7 - Scale out: two workers and routing policies

**Goal:** see what routing policies do, measure when cache-aware routing pays off, and work out why.

```bash
python labctl.py up stack --workers a,b -- --policy round_robin
```

**Predict:** Each `multi_tenant` request carries one of several long (~2k token) system prompts. With 8 tenants and with 32 tenants, how much better will `cache_aware` be than `round_robin`?

**Do.** Restart Ollama before each policy, so every run starts with empty caches:

```bash
for T in 8 32; do for pol in round_robin cache_aware; do
  python labctl.py down all
  python labctl.py up stack --workers a,b -- --policy $pol
  python bench.py --workload multi_tenant --tenants $T --concurrency 8 --num-requests 192 --max-tokens 16 \
    --out results/t7_${pol}_t$T.json
  python metrics.py | grep -E 'per worker|cache_aware'
done; done
cp results/t7_round_robin_t32.json results/t7_rr.json; cp results/t7_cache_aware_t32.json results/t7_ca.json
```

On the reference Mac: with 8 tenants, cache_aware gave about 1.14x the req/s of round_robin; with 32 tenants, about 1.32x, and TTFT p50 halved (509 to 243 ms). Then try the other policies once each with 32 tenants: `random`, `power_of_two`, `least_load`, `prefix_hash`, `consistent_hashing`.

**Observe:** Look in `logs/ollama-a.log` for the prompt cache size limit (`grep "prompt cache" logs/ollama-a.log | head`). Note `mean_prompt_tokens` from the bench JSON.

**Explain**

1. Each worker's prompt cache holds about 32k tokens. Work out how many ~2k-token tenant prompts fit. Use that to explain why 8 tenants show a small gap and 32 a larger one.
2. What do the `cache_aware` decision branches `tree_match` and `expected_wait_fallback` mean? Read `model_gateway/src/policies/cache_aware.rs`, and also look at `--cache-threshold` and `--balance-abs-threshold`.
3. In HTTP mode SMG builds its prefix tree from request *text*, not tokens. Why does that still work? What would gRPC mode do better?
4. Both Ollama servers run on the same Mac GPU. Did two workers double the throughput of one? Why not? When *does* a second worker help on one machine?

**Challenge:** Find a workload (`--tenants`, `--concurrency`) where `cache_aware` is *worse* than `round_robin`, and explain why. Hint: all requests for one tenant pile onto one worker.

```bash
python check.py 7
```

---

## Task 8 - Admission control: protect the engine

**Goal:** decide what happens when more requests arrive than the engines can handle.

```bash
python labctl.py up stack --workers a,b -- --policy cache_aware
```

**Predict:** At 32 clients, without limits, what happens to TTFT? With `--max-concurrent-requests 8 --queue-size 0`, what do clients get?

**Do**

```bash
python bench.py --concurrency 32 --num-requests 128 --out results/t8_nolimit.json
python labctl.py up smg --workers a,b -- --policy cache_aware --max-concurrent-requests 8 --queue-size 0
python bench.py --concurrency 32 --num-requests 128 --out results/t8_noqueue.json
python metrics.py | grep -E 'status|rejections'
python labctl.py up smg --workers a,b -- --policy cache_aware --max-concurrent-requests 8 --queue-size 64 --queue-timeout-secs 5
python bench.py --concurrency 32 --num-requests 128 --out results/t8_queue.json
python labctl.py up smg --workers a,b -- --policy cache_aware --rate-limit-tokens-per-second 3
python bench.py --concurrency 8 --num-requests 40 --out results/t8_ratelimit.json
```

**Observe:** For each run, compare successes, 429s, TTFT p50/p99 *of the successful requests*, and req/s.

**Explain**

1. What is the difference between `--max-concurrent-requests`, `--queue-size` and `--rate-limit-tokens-per-second`? Hint: the rate limit queues too unless the queue is 0.
2. With limit 8 and no queue, the successful requests got *better* TTFT than with no limit. Why is failing fast sometimes kinder to clients?
3. How would you size `--max-concurrent-requests` from the number of workers and `OLLAMA_NUM_PARALLEL`? What happens if it is much bigger? Much smaller?
4. Read `model_gateway/src/middleware/concurrency.rs` and `token_bucket.rs`. Where does a queued request wait?

**Challenge:** Find the limit and queue settings that maximize successful requests per second at 32 clients while keeping TTFT p99 under 2 s.

```bash
python check.py 8
```

---

## Task 9 - Reliability: kill a worker under load

**Goal:** see what the gateway hides from clients when an engine dies, and what it cannot hide.

```bash
python labctl.py up stack --workers a,b -- --policy round_robin
```

**Predict:** Clients send steady traffic. At second 8 you stop `ollama-b`; at second 18 you restart it. Without a gateway (clients talk to one Ollama), what do they see? With the gateway?

**Do.** `chaos.py` runs steady load, stops and starts components on a schedule, and prints a per-second timeline.

```bash
# without the gateway: clients talk straight to ollama-b's adapter
python chaos.py --base-url http://127.0.0.1:11601 --duration 30 --kill ollama-b@8 --restore ollama-b@18 --out results/t9_direct.json
# with the gateway
python chaos.py --duration 30 --kill ollama-b@8 --restore ollama-b@18 --out results/t9_gateway.json
python metrics.py
```

Run the gateway test again with faster failure detection, and once with retries off:

```bash
python labctl.py up smg --workers a,b -- --policy round_robin --health-check-interval-secs 2 --health-failure-threshold 1
python chaos.py --duration 30 --kill ollama-b@8 --restore ollama-b@18
python labctl.py up smg --workers a,b -- --policy round_robin --disable-retries
python chaos.py --duration 30 --kill ollama-b@8 --restore ollama-b@18
```

**Observe**

- Did clients of the gateway see any errors?
- `metrics.py` shows `retries`, `worker health` and `circuit breaker` state. After ollama-b came back, how quickly did it get traffic again? Check `requests per worker` before and after a fresh bench run.

**Explain**

1. What turned a dead worker into zero client errors? Name the mechanism and the metric that proves it.
2. On the reference Mac, after 10 failures the circuit breaker *opened* for ollama-b and kept it out for a while even after it came back. Why is that a feature? Which flags tune it (`--cb-`*)?
3. Retries cannot save a request that was already streaming tokens when its worker died. Why not?
4. What did `--disable-retries` change? Why would anyone disable retries? Think of non-idempotent tools.

**Challenge:** Kill `adapter-b` instead of `ollama-b`, so the health endpoint and the data path die together. Then try a case where health says OK but requests fail: give the adapter a wrong `--upstream` and restart it by hand. Which one does SMG handle better, and why?

```bash
python check.py 9
```

---

## Task 10 - Capstone: optimize for an SLO

**Goal:** use everything to get the most capacity out of this machine for a realistic workload.

The workload (`capstone.py`): 16 tenants with ~2k-token system prompts, requests arriving at random (Poisson) at a fixed rate, 32 output tokens. The load ramps from 1 req/s upward. **Score = the highest rate at which at least 95% of requests got their first token within 1.5 s.** Rejected requests count as misses.

```bash
cat capstone.env           # a deliberately naive starting config
python capstone.py         # about 2-4 minutes; this is your baseline score
```

Now improve it. Edit `capstone.env`, run `python capstone.py`, and repeat. Change one thing at a time and write down why you expect it to help *before* you run. Every run is appended to `results/capstone_history.jsonl`.

The knobs: `WORKERS`, `OLLAMA_NUM_PARALLEL`, `OLLAMA_KV_CACHE_TYPE`, `OLLAMA_FLASH_ATTENTION`, `OLLAMA_CONTEXT_LENGTH`, and any `smg launch` flag in `SMG_FLAGS`: policy, admission control, cache-aware thresholds, health and retry settings.

Questions to guide you:

- Is the bottleneck the engine (Task 6), routing (Task 7) or overload (Task 8)? Use `metrics.py` and the TTFT columns to decide.
- Does a second worker help *for this workload* on one GPU? Think cache capacity versus compute.
- Admission control rejects requests, which count as misses. When does it still raise the score?

```bash
python check.py 10
```

Write a short summary of your best config and the two changes that mattered most. That paragraph is the real deliverable of the lab.

---

## Where to go next

- `docs/03_optimization_playbook.md`: the reasoning you just practised, as a checklist.
- `docs/05_reading_the_source.md`: follow one request through SMG's Rust code.
- Things this lab cannot show with Ollama: gRPC mode (SMG tokenizes, parses tool calls and reasoning itself, and reports TTFT), token-level cache-aware routing, and prefill/decode disaggregation. Those need vLLM or SGLang; see the `smg-vllm-lab`.
