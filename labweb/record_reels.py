"""Record the guide's animated reels as GIFs (for the README). Maintainer tool; learners never need it.

  .venv/bin/python labweb/record_reels.py               # every reel -> docs/media/reel-N.gif
  .venv/bin/python labweb/record_reels.py 7 9 --fps 12  # only some

Needs Google Chrome and ffmpeg. Opens labweb/index.html?capture=N straight from disk
(no companion server), steps the animation frame by frame and screenshots the reel.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile

import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333


class CDP:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def call(self, method: str, **params):
        self.n += 1
        await self.ws.send_str(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads((await self.ws.receive()).data)
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expr: str):
        r = await self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")


async def record(n: int, fps: int, width: int, out_dir: str, session: aiohttp.ClientSession) -> str:
    async with session.put(f"http://127.0.0.1:{PORT}/json/new?about:blank") as r:
        target = await r.json()
    async with session.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0) as ws:
        cdp = CDP(ws)
        await cdp.call("Emulation.setDeviceMetricsOverride", width=380, height=760, deviceScaleFactor=2, mobile=False)
        await cdp.call("Page.enable")
        await cdp.call("Page.navigate", url=f"file://{HERE}/index.html?capture={n}")
        for _ in range(100):
            if await cdp.js("typeof window.__advance === 'function'"):
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError(f"reel {n}: capture mode did not start")
        await asyncio.sleep(0.3)
        total = await cdp.js("window.__total")
        box = await cdp.js("(() => { const b = document.querySelector('#cap .reel').getBoundingClientRect();"
                           " return {x: b.x, y: b.y, width: b.width, height: b.height}; })()")
        frames = tempfile.mkdtemp(prefix=f"reel{n}-")
        count = int(total * fps)
        for i in range(count):
            shot = await cdp.call("Page.captureScreenshot", format="png", clip={**box, "scale": 1})
            with open(os.path.join(frames, f"f{i:05d}.png"), "wb") as f:
                f.write(base64.b64decode(shot["data"]))
            await cdp.js(f"window.__advance({1 / fps})")
    async with session.get(f"http://127.0.0.1:{PORT}/json/close/{target['id']}"):
        pass

    out = os.path.join(out_dir, f"reel-{n}.gif")
    vf = (f"scale={width}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];"
          "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i",
                    os.path.join(frames, "f%05d.png"), "-vf", vf, "-loop", "0", out], check=True)
    shutil.rmtree(frames)
    print(f"reel {n:>2}: {total:4.0f}s, {count} frames -> {os.path.relpath(out, ROOT)} ({os.path.getsize(out) / 1e6:.1f} MB)")
    return out


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("reels", nargs="*", type=int, default=list(range(11)))
    p.add_argument("--fps", type=int, default=12)
    p.add_argument("--width", type=int, default=360, help="GIF width in pixels")
    p.add_argument("--out", default=os.path.join(ROOT, "docs", "media"))
    a = p.parse_args()
    os.makedirs(a.out, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="chrome-rec-")
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
                               "--hide-scrollbars", "--allow-file-access-from-files", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with aiohttp.ClientSession() as session:
            for _ in range(100):
                try:
                    async with session.get(f"http://127.0.0.1:{PORT}/json/version"):
                        break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.1)
            for n in a.reels:
                await record(n, a.fps, a.width, a.out, session)
    finally:
        chrome.terminate()
        chrome.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
