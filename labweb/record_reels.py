"""Record the guide's animated reels as GIFs (for the README). Maintainer tool; learners never need it.

  .venv/bin/python labweb/record_reels.py               # every reel -> docs/media/reel-N.gif
  .venv/bin/python labweb/record_reels.py 7 9 --fps 12  # only some
  .venv/bin/python labweb/record_reels.py --linkedin ~/Desktop/smg_reels   # 1080x1350 MP4s for social posts

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


# LinkedIn series order: the most surprising reels first. (reel number, file slug)
SERIES = [(2, "external-mode"), (3, "why-health-matters"), (7, "cache-aware-routing"), (9, "kill-a-worker"),
          (8, "admission-control"), (1, "one-slot"), (6, "slots-and-memory"), (4, "many-apis"),
          (5, "what-the-gateway-sees"), (10, "capstone-slo"), (0, "the-lab-loop")]
END_SECS = 3.0


async def capture(cdp: CDP, url: str, selector: str, fps: int, frames: str, end_card: bool, fmt: str) -> tuple[float, int]:
    await cdp.call("Page.navigate", url=url)
    for _ in range(100):
        if await cdp.js("typeof window.__advance === 'function'"):
            break
        await asyncio.sleep(0.1)
    else:
        raise RuntimeError(f"{url}: capture mode did not start")
    await asyncio.sleep(0.3)
    total = await cdp.js("window.__total")
    box = await cdp.js(f"(() => {{ const b = document.querySelector('{selector}').getBoundingClientRect();"
                       " return {x: b.x, y: b.y, width: b.width, height: b.height}; })()")
    reel_frames, end_frames = int(total * fps), int(END_SECS * fps) if end_card else 0
    for i in range(reel_frames + end_frames):
        if i >= reel_frames:
            await cdp.js(f"window.__end({min(1.0, (i - reel_frames) / (0.4 * fps)):.3f})")
        shot = await cdp.call("Page.captureScreenshot", format=fmt, quality=92, clip={**box, "scale": 1}) if fmt == "jpeg" \
            else await cdp.call("Page.captureScreenshot", format=fmt, clip={**box, "scale": 1})
        with open(os.path.join(frames, f"f{i:05d}.{'jpg' if fmt == 'jpeg' else fmt}"), "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        await cdp.js(f"window.__advance({1 / fps})")
    return total, reel_frames + end_frames


async def record(n: int, a: argparse.Namespace, session: aiohttp.ClientSession, part: int = 0, slug: str = "") -> str:
    async with session.put(f"http://127.0.0.1:{PORT}/json/new?about:blank") as r:
        target = await r.json()
    frames = tempfile.mkdtemp(prefix=f"reel{n}-")
    async with session.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0) as ws:
        cdp = CDP(ws)
        await cdp.call("Page.enable")
        if a.linkedin:
            await cdp.call("Emulation.setDeviceMetricsOverride", width=540, height=675, deviceScaleFactor=2, mobile=False)
            url = f"file://{HERE}/index.html?capture={n}&post={part}/{len(SERIES)}"
            total, count = await capture(cdp, url, "#post", a.fps, frames, True, "jpeg")
        else:
            await cdp.call("Emulation.setDeviceMetricsOverride", width=380, height=760, deviceScaleFactor=2, mobile=False)
            total, count = await capture(cdp, f"file://{HERE}/index.html?capture={n}", "#cap .reel", a.fps, frames, False, "png")
    async with session.get(f"http://127.0.0.1:{PORT}/json/close/{target['id']}"):
        pass

    if a.linkedin:
        out = os.path.join(a.linkedin, f"{part:02d}-{slug}.mp4")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(a.fps), "-i", os.path.join(frames, "f%05d.jpg"),
               "-vf", "scale=1080:1350:flags=lanczos:in_range=full:out_range=tv,format=yuv420p",
               "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-color_range", "tv", "-movflags", "+faststart", out]
    else:
        out = os.path.join(a.out, f"reel-{n}.gif")
        vf = (f"scale={a.width}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];"
              "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(a.fps), "-i",
               os.path.join(frames, "f%05d.png"), "-vf", vf, "-loop", "0", out]
    subprocess.run(cmd, check=True)
    shutil.rmtree(frames)
    print(f"reel {n:>2}: {total:4.0f}s, {count} frames -> {out} ({os.path.getsize(out) / 1e6:.1f} MB)", flush=True)
    return out


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("reels", nargs="*", type=int, default=list(range(11)))
    p.add_argument("--fps", type=int, default=None, help="default 12 for GIFs, 30 for --linkedin")
    p.add_argument("--width", type=int, default=360, help="GIF width in pixels")
    p.add_argument("--out", default=os.path.join(ROOT, "docs", "media"))
    p.add_argument("--linkedin", metavar="DIR", help="write 1080x1350 MP4s (series order, part header, end card) to DIR")
    a = p.parse_args()
    a.fps = a.fps or (30 if a.linkedin else 12)
    os.makedirs(a.linkedin or a.out, exist_ok=True)
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
            if a.linkedin:
                for part, (n, slug) in enumerate(SERIES, 1):
                    if n in a.reels:
                        await record(n, a, session, part, slug)
            else:
                for n in a.reels:
                    await record(n, a, session)
    finally:
        chrome.terminate()
        chrome.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
