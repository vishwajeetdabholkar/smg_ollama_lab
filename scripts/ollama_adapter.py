"""A ~70-line adapter that lets SMG's HTTP router manage an Ollama server.

Why it exists (you discover this yourself in Task 3):
  SMG registers a self-hosted HTTP worker only after `GET <worker>/health`
  returns 200 (see model_gateway/src/workflow/steps/util.rs, http_health_url).
  vLLM and SGLang serve /health. Ollama does not (it serves `GET /` and
  `/api/version`), so SMG refuses to register it.

What it does:
  GET /health      -> 200 if Ollama answers /api/version, else 503
  GET /v1/models   -> Ollama's model list, optionally filtered to --model, so
                      each worker advertises exactly the model you intend it to
                      serve (SMG's HTTP router takes the first listed model)
  everything else  -> passed through to Ollama byte for byte, streaming included

Usage:
  python scripts/ollama_adapter.py --port 11600 --upstream http://127.0.0.1:11500 --model qwen3:0.6b
"""

import argparse

import aiohttp
from aiohttp import web

p = argparse.ArgumentParser(description="Health + model-list adapter in front of Ollama")
p.add_argument("--host", default="127.0.0.1")
p.add_argument("--port", type=int, required=True)
p.add_argument("--upstream", required=True, help="Ollama base URL, e.g. http://127.0.0.1:11500")
p.add_argument("--model", default=None, help="only advertise this model in /v1/models")
args = p.parse_args()

HOP_BY_HOP = {"host", "content-length", "transfer-encoding", "connection", "keep-alive"}


async def health(request: web.Request) -> web.Response:
    try:
        async with request.app["session"].get(
            f"{args.upstream}/api/version", timeout=aiohttp.ClientTimeout(total=3)
        ) as r:
            up = r.status == 200
    except aiohttp.ClientError:
        up = False
    except TimeoutError:
        up = False
    return web.json_response({"status": "ok" if up else "ollama unreachable"}, status=200 if up else 503)


async def models(request: web.Request) -> web.Response:
    try:
        async with request.app["session"].get(f"{args.upstream}/v1/models") as r:
            data = await r.json()
    except (aiohttp.ClientError, TimeoutError):
        return web.json_response({"error": "ollama unreachable"}, status=503)
    if args.model:
        data["data"] = [m for m in data.get("data", []) if m.get("id") == args.model]
    return web.json_response(data)


async def passthrough(request: web.Request) -> web.StreamResponse:
    body = await request.read()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    try:
        upstream = await request.app["session"].request(
            request.method, f"{args.upstream}{request.path_qs}", data=body, headers=headers
        )
    except (aiohttp.ClientError, TimeoutError) as e:
        return web.json_response({"error": f"ollama unreachable: {e}"}, status=502)
    async with upstream:
        resp = web.StreamResponse(
            status=upstream.status,
            headers={k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP},
        )
        await resp.prepare(request)
        async for chunk in upstream.content.iter_any():
            await resp.write(chunk)
        await resp.write_eof()
        return resp


async def make_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)

    async def open_session(app):
        app["session"] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None), connector=aiohttp.TCPConnector(limit=0)
        )

    async def close_session(app):
        await app["session"].close()

    app.on_startup.append(open_session)
    app.on_cleanup.append(close_session)
    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", models)
    app.router.add_route("*", "/{tail:.*}", passthrough)
    return app


if __name__ == "__main__":
    print(f"adapter :{args.port} -> {args.upstream} (model filter: {args.model or 'none'})", flush=True)
    web.run_app(make_app(), host=args.host, port=args.port, print=None)
