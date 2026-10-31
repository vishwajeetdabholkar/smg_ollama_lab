# 02 - Ollama as an engine (read during Task 6)

## What runs when you `ollama serve`

`ollama serve` is a Go server (the `[GIN]` lines in its log). When a request needs a model, it starts a **runner** for that model. In Ollama 0.32 the runner is `llama-server` from llama.cpp. You can see the exact command line it uses:

```bash
grep "starting llama-server" logs/ollama-a.log | head -1
grep -E "n_slots|prompt cache" logs/ollama-a.log | head
```

The model stays loaded for `OLLAMA_KEEP_ALIVE` (the lab sets 30m), then it is unloaded. The first request after that pays the load time again; that is why `bench.py` sends a few untimed warm-up requests.

## Slots = `OLLAMA_NUM_PARALLEL`

`llama-server` has a fixed number of **slots**. Each slot holds one sequence (one request) and its KV cache. All busy slots are decoded together, one token each per step (batching).

- `NUM_PARALLEL=1`: one request at a time. Others wait in Ollama's queue. Throughput is flat, and TTFT grows with the queue (Task 1).
- More slots: more requests decode together. Each step takes a little longer (ITL rises), but more tokens come out per step, so total throughput rises, up to the point where the GPU is saturated.

Memory: every slot reserves KV cache for a full context of `OLLAMA_CONTEXT_LENGTH` tokens. So KV memory is roughly proportional to NUM_PARALLEL x CONTEXT_LENGTH. On the reference Mac with `qwen3:0.6b` and 8192 context it was 1.5 GB (1 slot), 4.4 GB (4) and 8.2 GB (8).

## Prompt cache

`llama-server` keeps recently used prompts' KV cache in host memory (the log says `prompt cache is enabled, size limit: 8192 MiB` and a token limit, 32768 on the reference Mac). A new request whose prompt starts with a cached prefix skips the prefill for that prefix, which means a much lower TTFT.

This is what `cache_aware` routing exploits (Task 7). It also tells you when cache-aware routing matters. If all your distinct prefixes fit in *one* worker's cache, any policy gets cache hits. If they only fit when *split* across workers, routing decides your hit rate.

## Settings worth knowing

| Variable | What it does | Lab finding (reference Mac) |
|---|---|---|
| `OLLAMA_NUM_PARALLEL` | slots per loaded model | 4 gave ~1.8x the throughput of 1 at 8 clients |
| `OLLAMA_CONTEXT_LENGTH` | context per slot | memory scales with it; 8192 is plenty for the lab |
| `OLLAMA_FLASH_ATTENTION` | faster, smaller attention | already on by default in 0.32; `=0` used more memory and was slower |
| `OLLAMA_KV_CACHE_TYPE` | `f16` (default), `q8_0`, `q4_0` | `q8_0` ~halved KV memory, slightly slower; needs flash attention |
| `OLLAMA_KEEP_ALIVE` | how long a model stays loaded | `30m` in lab.env so benchmarks do not reload |
| `OLLAMA_MAX_LOADED_MODELS` | models resident at once | matters when one server serves several models |
| `OLLAMA_HOST` | listen address | labctl sets it per server (11500, 11501) |

## Two servers on one Mac

`ollama-a` and `ollama-b` are separate processes, each with its own model copy, slots and prompt cache. But they share **one GPU**. Two workers on one machine do **not** double compute; they split it. A second worker on the same machine helps with:
- **cache capacity**: two prompt caches hold twice the prefixes, if routing keeps them apart;
- **isolation**: one can die or restart while the other serves;
- learning multi-worker behavior, which is why the lab has two.

On real deployments each worker is a separate GPU or machine, and scale-out does add compute.

## What Ollama lacks as a gateway backend

- no `/health` (SMG's self-hosted HTTP mode requires it): fixed by `scripts/ollama_adapter.py`
- no `/metrics`: SMG's metrics are your only server-side view
- no SMG gRPC servicer: no gRPC mode, so no token-level routing or TTFT metrics in SMG
- no `ignore_eos`: output lengths vary a little between runs, so expect some benchmark noise
