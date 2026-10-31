"""Start, stop and inspect the lab's background servers.

You can always run the servers yourself in separate terminals instead (Tasks 1-3
show you how). labctl just saves typing once you know what the commands do: it
PRINTS every command it runs, so read them.

Components (ports come from lab.env / lab_config.py):
  ollama-a, ollama-b    `ollama serve` on 127.0.0.1:11500 / :11501 (lab-owned, separate
                        from any Ollama desktop app on :11434; models are shared on disk)
  adapter-a, adapter-b  scripts/ollama_adapter.py on :11600 / :11601 (adds /health)
  smg                   `smg launch` on :30100, metrics on :29100

Examples:
  python labctl.py up ollama-a
  python labctl.py up ollama-a --env OLLAMA_NUM_PARALLEL=4      # restarts it if the config changed
  python labctl.py up adapter-a
  python labctl.py up smg --mode http --workers a -- --policy round_robin
  python labctl.py up smg --mode external --workers a -- --api-key ollama
  python labctl.py up stack --workers a,b -- --policy cache_aware   # ollama + adapters + smg
  python labctl.py status
  python labctl.py logs smg -n 50
  python labctl.py down all
  python labctl.py pull qwen3:0.6b
  add --dry-run to `up` to only print the commands
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

import requests

import lab_config as cfg

COMPONENTS = ("ollama-a", "ollama-b", "adapter-a", "adapter-b", "smg")


def say(msg: str) -> None:
    print(f"\033[1;34m[lab]\033[0m {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"\033[1;32m[ok]\033[0m  {msg}", flush=True)


def die(msg: str) -> None:
    print(f"\033[1;31m[err]\033[0m {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


# ----------------------------------------------------------------- state ---


def _state_path(name: str) -> str:
    return os.path.join(cfg.RUN_DIR, f"{name}.json")


def load_state(name: str) -> dict | None:
    try:
        with open(_state_path(name)) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        os.kill(st["pid"], 0)
    except OSError:
        return None
    return st


def port_listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((cfg.HOST, port)) == 0


def wait_port_free(port: int, timeout: float = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_listening(port):
            return True
        time.sleep(0.3)
    return False


# ------------------------------------------------------------------ specs ---


def spec(name: str, opts: argparse.Namespace) -> dict:
    """The command, extra env, port and readiness URL for one component."""
    if name.startswith("ollama-"):
        w = name[-1]
        env = {**cfg.OLLAMA_TUNING, **dict(opts.env or []), "OLLAMA_HOST": f"{cfg.HOST}:{cfg.OLLAMA_PORTS[w]}"}
        return {
            "cmd": ["ollama", "serve"],
            "env": env,
            "port": cfg.OLLAMA_PORTS[w],
            "ready": cfg.OLLAMA_URL[w] + "/",
            "timeout": 30,
        }
    if name.startswith("adapter-"):
        w = name[-1]
        model = (getattr(opts, "model_b", None) if w == "b" else None) or opts.model or cfg.MODEL
        return {
            "cmd": [sys.executable, os.path.join("scripts", "ollama_adapter.py"), "--port", str(cfg.ADAPTER_PORTS[w]),
                    "--upstream", cfg.OLLAMA_URL[w], "--model", model],
            "env": {},
            "port": cfg.ADAPTER_PORTS[w],
            "ready": cfg.ADAPTER_URL[w] + "/health",
            "timeout": 20,
        }
    if name == "smg":
        workers = [w.strip() for w in opts.workers.split(",") if w.strip()]
        if opts.mode == "http":
            urls, backend = [cfg.ADAPTER_URL[w] for w in workers], "vllm"
        else:
            urls, backend = [cfg.OLLAMA_URL[w] for w in workers], "openai"
        cmd = ["smg", "launch", "--host", cfg.HOST, "--port", str(cfg.SMG_PORT),
               "--prometheus-port", str(cfg.SMG_METRICS_PORT),
               "--backend", backend, "--worker-urls", *urls, *opts.smg_args]
        return {"cmd": cmd, "env": {}, "port": cfg.SMG_PORT, "ready": cfg.SMG_URL + "/readiness", "timeout": 90,
                "extra_ports": [cfg.SMG_METRICS_PORT]}
    die(f"unknown component {name!r}; choose from {', '.join(COMPONENTS)}")
    raise AssertionError


def show_cmd(sp: dict) -> str:
    env = " ".join(f"{k}={v}" for k, v in sp["env"].items())
    return (env + " " if env else "") + " ".join(sp["cmd"])


# -------------------------------------------------------------- start/stop ---


def stop(name: str, quiet: bool = False) -> None:
    st = load_state(name)
    if not st:
        if os.path.exists(_state_path(name)):
            os.remove(_state_path(name))
        if not quiet:
            say(f"{name} is not running")
        return
    pid = st["pid"]
    say(f"stopping {name} (pid {pid})")
    # SIGINT first: smg drains in-flight requests on SIGINT/SIGTERM, which takes a few seconds.
    for sig, wait_s in ((signal.SIGINT, 15), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            os.kill(pid, sig)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.2)
        else:
            continue
        break
    for port in [st["port"], *st.get("extra_ports", [])]:
        if not wait_port_free(port):
            say(f"warning: port {port} still in use after stopping {name}")
    os.remove(_state_path(name))
    ok(f"{name} stopped")


def start(name: str, opts: argparse.Namespace) -> None:
    sp = spec(name, opts)
    printable = show_cmd(sp)
    if opts.dry_run:
        print(f"  {name}: {printable}")
        return
    st = load_state(name)
    if st:
        if st["printable"] == printable:
            ok(f"{name} already running with the same config (pid {st['pid']})")
            return
        say(f"{name} is running with a different config; restarting it")
        stop(name)
    for port in [sp["port"], *sp.get("extra_ports", [])]:
        if port_listening(port):
            die(f"port {port} is already in use by something labctl did not start. "
                f"Find it with:  lsof -nP -iTCP:{port} -sTCP:LISTEN")
    if not shutil.which(sp["cmd"][0]) and not os.path.isabs(sp["cmd"][0]):
        die(f"`{sp['cmd'][0]}` not found on PATH (did you `source .venv/bin/activate`? is Ollama installed?)")

    os.makedirs(cfg.RUN_DIR, exist_ok=True)
    os.makedirs(cfg.LOG_DIR, exist_ok=True)
    log_path = os.path.join(cfg.LOG_DIR, f"{name}.log")
    say(f"starting {name}")
    print(f"\033[2m  $ {printable}\033[0m")
    log = open(log_path, "w")
    proc = subprocess.Popen(
        sp["cmd"], cwd=cfg.LAB_DIR, env={**os.environ, **sp["env"]},
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
    )
    with open(_state_path(name), "w") as f:
        json.dump({"pid": proc.pid, "printable": printable, "port": sp["port"],
                   "extra_ports": sp.get("extra_ports", []), "started": time.time()}, f)

    deadline = time.time() + sp["timeout"]
    while time.time() < deadline:
        if proc.poll() is not None:
            print(open(log_path).read()[-3000:], file=sys.stderr)
            os.remove(_state_path(name))
            die(f"{name} exited during startup (log above, full log: {os.path.relpath(log_path)})")
        try:
            if requests.get(sp["ready"], timeout=2).status_code == 200:
                ok(f"{name} ready on :{sp['port']}  (log: {os.path.relpath(log_path, cfg.LAB_DIR)})")
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    die(f"{name} not ready after {sp['timeout']}s; see {os.path.relpath(log_path, cfg.LAB_DIR)} "
        f"(it is still running: `python labctl.py logs {name}` / `python labctl.py down {name}`)")


# ---------------------------------------------------------------- commands ---


def cmd_up(opts: argparse.Namespace) -> None:
    names = []
    for n in opts.names:
        if n == "stack":
            ws = [w.strip() for w in opts.workers.split(",")]
            names += [f"ollama-{w}" for w in ws] + ([f"adapter-{w}" for w in ws] if opts.mode == "http" else []) + ["smg"]
        else:
            names.append(n)
    if opts.dry_run:
        print("Commands labctl would run:")
    for n in names:
        start(n, opts)


def cmd_down(opts: argparse.Namespace) -> None:
    names = opts.names or ["all"]
    if "all" in names:
        names = ["smg", "adapter-a", "adapter-b", "ollama-a", "ollama-b"]
    for n in names:
        stop(n, quiet="all" in (opts.names or ["all"]))


def cmd_status(_opts: argparse.Namespace) -> None:
    print(f"{'component':<10} {'pid':>7} {'port':>6} {'ready':>6}  command")
    for n in COMPONENTS:
        st = load_state(n)
        if not st:
            print(f"{n:<10} {'-':>7} {'-':>6} {'-':>6}  (not running)")
            continue
        ready_url = {"smg": cfg.SMG_URL + "/readiness"}.get(n)
        if n.startswith("ollama-"):
            ready_url = cfg.OLLAMA_URL[n[-1]] + "/"
        elif n.startswith("adapter-"):
            ready_url = cfg.ADAPTER_URL[n[-1]] + "/health"
        try:
            ready = "yes" if requests.get(ready_url, timeout=2).status_code == 200 else "no"
        except requests.RequestException:
            ready = "no"
        cmd = st["printable"]
        print(f"{n:<10} {st['pid']:>7} {st['port']:>6} {ready:>6}  {cmd if len(cmd) < 150 else cmd[:147] + '...'}")
    strays = [p for p in (11434,) if port_listening(p)]
    if strays:
        print("\nnote: something (probably the Ollama desktop app) listens on :11434. That is fine; "
              "the lab uses its own Ollama servers.")


def cmd_logs(opts: argparse.Namespace) -> None:
    path = os.path.join(cfg.LOG_DIR, f"{opts.name}.log")
    if not os.path.exists(path):
        die(f"no log at {path}")
    with open(path, errors="replace") as f:
        lines = f.readlines()
    sys.stdout.writelines(lines[-opts.n:])


def cmd_pull(opts: argparse.Namespace) -> None:
    models = opts.models or [cfg.MODEL]
    start_opts = argparse.Namespace(env=[], dry_run=False)
    start("ollama-a", start_opts)
    for m in models:
        say(f"pulling {m}")
        print(f"\033[2m  $ OLLAMA_HOST={cfg.HOST}:{cfg.OLLAMA_PORTS['a']} ollama pull {m}\033[0m")
        r = subprocess.run(["ollama", "pull", m], env={**os.environ, "OLLAMA_HOST": f"{cfg.HOST}:{cfg.OLLAMA_PORTS['a']}"})
        if r.returncode != 0:
            die(f"ollama pull {m} failed")
    ok("models are stored once on disk (~/.ollama/models) and shared by every Ollama server")


def main() -> None:
    argv = sys.argv[1:]
    smg_args: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, smg_args = argv[:i], argv[i + 1:]

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="start components (idempotent)")
    up.add_argument("names", nargs="+", help=f"{', '.join(COMPONENTS)} or 'stack'")
    up.add_argument("--env", action="append", type=lambda s: tuple(s.split("=", 1)), default=[],
                    metavar="KEY=VALUE", help="extra env for ollama-* (overrides lab.env)")
    up.add_argument("--model", default=None, help="model the adapter advertises (default: MODEL)")
    up.add_argument("--model-b", default=None, help="model adapter-b advertises in `up stack` (default: MODEL)")
    up.add_argument("--mode", choices=["http", "external"], default="http",
                    help="smg: http = self-hosted workers via adapters (--backend vllm); "
                         "external = straight at Ollama as an external OpenAI provider (--backend openai)")
    up.add_argument("--workers", default="a", help="smg: comma separated subset of a,b (default: a)")
    up.add_argument("--dry-run", action="store_true")
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", help="stop components")
    down.add_argument("names", nargs="*")
    down.set_defaults(func=cmd_down)

    sub.add_parser("status", help="what is running").set_defaults(func=cmd_status)

    lg = sub.add_parser("logs", help="tail a component log")
    lg.add_argument("name")
    lg.add_argument("-n", type=int, default=40)
    lg.set_defaults(func=cmd_logs)

    pl = sub.add_parser("pull", help="download models via ollama-a")
    pl.add_argument("models", nargs="*")
    pl.set_defaults(func=cmd_pull)

    opts = p.parse_args(argv)
    opts.smg_args = smg_args
    os.chdir(cfg.LAB_DIR)
    opts.func(opts)


if __name__ == "__main__":
    main()
