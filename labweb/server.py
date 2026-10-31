"""Interactive companion for LAB_GUIDE.md:  python labweb/server.py   (then open http://127.0.0.1:8090)

The page renders LAB_GUIDE.md and ANSWERS.md live, so editing the guide updates it.
This server only READS the lab (guide, docs, results/, run/, ports) and runs
`check.py <task>` when you press "Run check". It never starts or stops lab servers:
you still type every command yourself.

Standard library only; works with or without the lab's .venv activated.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
LAB_DIR = os.path.dirname(HERE)
sys.path.insert(0, LAB_DIR)
import lab_config as cfg  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")
VIEWABLE_EXT = (".md", ".py", ".env", ".example", ".sh", ".txt")


def lab_python() -> str:
    venv = os.path.join(LAB_DIR, ".venv", "bin", "python")
    return venv if os.path.exists(venv) else sys.executable


def read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def port_listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.25)
        return s.connect_ex((cfg.HOST, port)) == 0


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def listener(port: int) -> dict | None:
    """pid and process name listening on a port (macOS/Linux lsof)."""
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=3).stdout.split()
        if not out:
            return None
        pid = int(out[0])
        name = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=3).stdout.strip()
        return {"pid": pid, "name": os.path.basename(name)}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def lab_state(name: str) -> dict | None:
    try:
        st = json.loads(read(os.path.join(cfg.RUN_DIR, f"{name}.json")))
        os.kill(st["pid"], 0)
        return st
    except (OSError, ValueError, KeyError):
        return None


def status() -> dict:
    comps = [
        ("ollama-a", cfg.OLLAMA_PORTS["a"], cfg.OLLAMA_URL["a"] + "/"),
        ("ollama-b", cfg.OLLAMA_PORTS["b"], cfg.OLLAMA_URL["b"] + "/"),
        ("adapter-a", cfg.ADAPTER_PORTS["a"], cfg.ADAPTER_URL["a"] + "/health"),
        ("adapter-b", cfg.ADAPTER_PORTS["b"], cfg.ADAPTER_URL["b"] + "/health"),
        ("smg", cfg.SMG_PORT, cfg.SMG_URL + "/readiness"),
    ]
    out = []
    for name, port, ready_url in comps:
        st = lab_state(name)
        listening = port_listening(port)
        item = {"name": name, "port": port, "listening": listening, "labctl": bool(st),
                "ready": listening and http_ok(ready_url), "command": st["printable"] if st else None}
        if listening and not st:
            item["process"] = listener(port)
        out.append(item)
    smg_workers = None
    if port_listening(cfg.SMG_PORT):
        try:
            with urllib.request.urlopen(cfg.SMG_URL + "/workers", timeout=1.5) as r:
                smg_workers = [{"url": w.get("url"), "healthy": w.get("is_healthy"), "type": w.get("runtime_type"),
                                "models": [m.get("id", m) if isinstance(m, dict) else m for m in w.get("models", [])]}
                               for w in json.load(r).get("workers", [])]
        except Exception:
            smg_workers = None
    return {"components": out, "smg_workers": smg_workers, "metrics_listening": port_listening(cfg.SMG_METRICS_PORT),
            "lab_env": {k: v for k, v in cfg.LAB_ENV.items() if k.startswith(("OLLAMA_", "MODEL"))}}


def results() -> dict:
    files = {}
    if os.path.isdir(cfg.RESULTS_DIR):
        for fn in sorted(os.listdir(cfg.RESULTS_DIR)):
            path = os.path.join(cfg.RESULTS_DIR, fn)
            try:
                if fn.endswith(".json"):
                    files[fn] = {"mtime": os.path.getmtime(path), "data": json.loads(read(path))}
                elif fn.endswith(".jsonl"):
                    files[fn] = {"mtime": os.path.getmtime(path),
                                 "data": [json.loads(l) for l in read(path).splitlines() if l.strip()]}
            except ValueError:
                files[fn] = {"mtime": os.path.getmtime(path), "error": "not valid JSON"}
    return files


def run_check(task: int) -> dict:
    try:
        p = subprocess.run([lab_python(), "check.py", str(task)], cwd=LAB_DIR, capture_output=True, text=True,
                           timeout=120)
        text = ANSI.sub("", p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        text = f"Task {task}: not done  - check.py timed out"
    return {"task": task, "passed": f"Task {task}: done" in text, "output": text}


def safe_lab_file(rel: str) -> str | None:
    path = os.path.realpath(os.path.join(LAB_DIR, rel))
    if not path.startswith(LAB_DIR + os.sep) or ".venv" in path.split(os.sep):
        return None
    if not path.endswith(VIEWABLE_EXT) or not os.path.isfile(path):
        return None
    return path


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code: int = 200) -> None:
        self.send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self.send(200, read(os.path.join(HERE, "index.html")).encode(), "text/html; charset=utf-8")
        if u.path == "/api/guide":
            return self.json({"guide": read(os.path.join(LAB_DIR, "LAB_GUIDE.md")),
                              "answers": read(os.path.join(LAB_DIR, "ANSWERS.md")),
                              "ports": {"ollama": cfg.OLLAMA_PORTS, "adapter": cfg.ADAPTER_PORTS,
                                        "smg": cfg.SMG_PORT, "metrics": cfg.SMG_METRICS_PORT},
                              "models": {"a": cfg.MODEL, "b": cfg.MODEL_B}})
        if u.path == "/api/status":
            return self.json(status())
        if u.path == "/api/results":
            return self.json(results())
        if u.path == "/api/file":
            path = safe_lab_file(q.get("path", [""])[0])
            if not path:
                return self.json({"error": "not a viewable lab file"}, 404)
            return self.json({"path": os.path.relpath(path, LAB_DIR), "text": read(path)})
        if u.path == "/api/exists":
            names = q.get("paths", [""])[0].split(",")
            return self.json({n: bool(safe_lab_file(n)) for n in names if n})
        self.json({"error": "not found"}, 404)

    def do_POST(self):
        m = re.fullmatch(r"/api/check/(\d+)", urlparse(self.path).path)
        if m and 1 <= int(m.group(1)) <= 10:
            return self.json(run_check(int(m.group(1))))
        self.json({"error": "not found"}, 404)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}"
    print(f"SMG + Ollama lab companion: {url}   (Ctrl-C to stop)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
