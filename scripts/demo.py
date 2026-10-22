#!/usr/bin/env python3
"""Run claude-top against made-up sessions, or render that view to images.

  python3 scripts/demo.py                       # interactive demo, nothing real is read
  uv run --no-project --with pyte --with pillow python scripts/demo.py --screenshot docs/screenshot.png
  uv run --no-project --with pyte --with pillow python scripts/demo.py --gif docs/demo.gif
  uv run --no-project --with pyte --with pillow python scripts/demo.py --social docs/social-preview.png
"""
import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# name, state, minutes since last active, hours since created, model, effort, context tokens,
# subagents (running, finished), background tasks, note
SESSIONS = [
    ("api rate-limit middleware", "working", 0.1, 1.2, "claude-opus-5-5", "high", 182_000, (2, 3), 1,
     "Running the integration suite against the new limiter"),
    ("flaky checkout e2e test", "blocked", 2, 3.5, "claude-sonnet-5-5", "medium", 96_000, (0, 1), 0,
     "Fix the clock mock, or retry the test 3x in CI?"),
    ("docs site dark mode", "working", 4, 0.6, "claude-sonnet-5-5", "medium", 61_000, (1, 1), 0,
     "Checking contrast on code blocks"),
    ("billing webhook retries", "review", 25, 30, "claude-opus-5-5", "high", 298_000, (0, 4), 0,
     "PR #812 ready: idempotent retries with a dead-letter queue"),
    ("postgres 17 upgrade plan", "blocked", 38, 26, "claude-opus-5-5", "high", 415_000, (0, 6), 0,
     "Maintenance window: Sat 02:00 or Sun 02:00?"),
    ("release notes v2.4", "done", 55, 5, "claude-haiku-5-5", "low", 24_000, (0, 0), 0,
     "Drafted in CHANGELOG.md; 14 PRs grouped by area"),
    ("onboarding email copy", "blocked", 300, 50, "claude-sonnet-5-5", "medium", 88_000, (0, 0), 0,
     "Subject line A \"Welcome aboard\" or B \"You're in\"?"),
    ("search index rebuild", "done", 610, 70, "claude-opus-5-5", "high", 640_000, (0, 12), 0,
     "Rebuilt in 41 min (was 3h); benchmark in docs/"),
    ("ios crash triage", "stopped", 1500, 96, "claude-sonnet-5-5", "medium", 152_000, (0, 2), 0,
     "Stopped by user"),
    ("terraform drift report", "done", 2900, 140, "claude-opus-5-5", "medium", 205_000, (0, 3), 0,
     "3 drifted resources; fixes in PR #790"),
    ("dependency audit", "done", 4400, 200, "claude-haiku-5-5", "low", 41_000, (0, 0), 0,
     "No critical advisories; 6 minor bumps queued"),
]
HELPERS = ["run the integration tests", "review limiter edge cases", "check the docs build", "audit retries",
           "profile the queries", "draft the summary"]

# keys to send, then seconds to wait and how long the GIF shows that frame (ms)
GIF_STEPS = [
    (b"", 1.5, 1600), (b"j", 0.5, 800), (b"j", 0.5, 900), (b"o", 0.6, 1300), (b"?", 0.6, 3000),
    (b"x", 0.5, 900), (b"/", 0.4, 700), (b"p", 0.4, 500), (b"r", 0.4, 800), (b"\n", 0.6, 2000),
    (b"/", 0.4, 600), (b"\n", 0.6, 3000),
]


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def build(home):
    now = time.time()
    project = home / "projects" / "-home-dev-acme-app"
    project.mkdir(parents=True)
    for i, (name, state, mins, hours, model, effort, ctx, (running, finished), bg, note) in enumerate(SESSIONS):
        sid = str(uuid.uuid4())
        job = home / "jobs" / sid[:8]
        job.mkdir(parents=True)
        last, created = now - mins * 60, now - hours * 3600
        transcript = project / f"{sid}.jsonl"
        lines = [
            {"type": "api-request", "querySource": "repl_main_thread",
             "params": {"model": model, "output_config": {"effort": effort}}},
            {"type": "assistant", "isSidechain": False, "perTurnEffort": effort,
             "message": {"model": model, "usage": {"input_tokens": 2, "cache_read_input_tokens": ctx - 1500,
                                                   "cache_creation_input_tokens": 1000, "output_tokens": 498}}},
        ]
        transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        os.utime(transcript, (last, last))
        subagents = project / sid / "subagents"
        subagents.mkdir(parents=True)
        for n in range(running + finished):
            meta = {"agentType": "general-purpose", "description": HELPERS[(i + n) % len(HELPERS)],
                    "model": "sonnet" if n % 2 else "haiku", "finished": n >= running}
            (subagents / f"agent-{n}.meta.json").write_text(json.dumps(meta))
            (subagents / f"agent-{n}.jsonl").write_text("{}\n")  # running helpers have a fresh transcript
        key = "needs" if state == "blocked" else "detail"
        kinds = ["local_agent"] * bool(running) + ["local_bash"] * bool(bg)
        (job / "state.json").write_text(json.dumps({
            "state": state, "name": name, key: note, "sessionId": sid, "resumeSessionId": sid,
            "cwd": "/home/dev/acme-app", "linkScanPath": str(transcript),
            "createdAt": iso(created), "updatedAt": iso(last),
            "inFlight": {"tasks": running + bg, "kinds": kinds},
        }))

    data = home / "claude-top"
    data.mkdir()
    five_reset, week_reset = now + 2 * 3600, now + 2.6 * 86400
    (data / "rate_limits.json").write_text(json.dumps({"ts": int(now), "rate_limits": {
        "five_hour": {"used_percentage": 30, "resets_at": int(five_reset)},
        "seven_day": {"used_percentage": 78, "resets_at": int(week_reset)},
    }}))
    history = [{"t": now - 4 * 3600, "u": {"fh": None, "sd": 74}}, {"t": now - 2.5 * 3600, "u": {"fh": 10, "sd": 75}},
               {"t": now - 3600, "u": {"fh": 22, "sd": 77}}]
    (data / "usage-history.jsonl").write_text("\n".join(json.dumps(h) for h in history) + "\n")


def capture(env, steps, cols, rows):
    """Run the demo in a pty; after each (keys, seconds, ...) step, a copy of the screen's cells."""
    import fcntl
    import pty
    import select
    import struct
    import termios

    import pyte

    class Screen(pyte.Screen):  # pyte lacks CSI S / T, which ncurses uses to scroll part of the screen
        def scroll(self, count, edge, step):
            y, margins = self.cursor.y, self.margins or pyte.screens.Margins(0, self.lines - 1)
            self.cursor.y = getattr(margins, edge)
            for _ in range(count or 1):
                step()
            self.cursor.y = y

        def scroll_up(self, count=1, *_, **__):
            self.scroll(count, "bottom", self.index)

        def scroll_down(self, count=1, *_, **__):
            self.scroll(count, "top", self.reverse_index)

    class Stream(pyte.ByteStream):
        csi = {**pyte.ByteStream.csi, "S": "scroll_up", "T": "scroll_down"}

    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.environ.update(env, TERM="xterm-256color")
        os.execvp(sys.executable, [sys.executable, str(ROOT / "claude_top.py")])
    screen = Screen(cols, rows)
    stream = Stream(screen)

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    stream.feed(os.read(fd, 65536))
                except OSError:
                    return

    frames = []
    for keys, seconds, *_ in steps:
        if keys:
            os.write(fd, keys)
        pump(seconds)
        frames.append([[screen.buffer[y][x] for x in range(cols)] for y in range(rows)])
    os.write(fd, b"q")
    pump(0.5)
    os.waitpid(pid, 0)
    return frames


FOREGROUND, BACKGROUND = "#c8d3f5", "#222436"
PALETTE = {"black": "#1b1d2b", "red": "#ff757f", "green": "#c3e88d", "yellow": "#ffc777", "brown": "#ffc777",
           "blue": "#82aaff", "magenta": "#c099ff", "cyan": "#86e1fc", "white": "#c8d3f5"}


def render(cells, size=15, pad=18):
    """A screen of pyte cells as an image, in a dark theme."""
    from PIL import Image, ImageDraw

    regular, bold = load_fonts(size)
    cw, ch = regular.getlength("M"), int(size * 1.35)
    rows, cols = len(cells), len(cells[0])
    img = Image.new("RGB", (int(cols * cw + 2 * pad), rows * ch + 2 * pad), BACKGROUND)
    draw = ImageDraw.Draw(img)
    for y, line in enumerate(cells):
        for x, c in enumerate(line):
            fg = PALETTE.get(c.fg, FOREGROUND)
            bg = PALETTE.get(c.bg, BACKGROUND)
            if c.reverse:
                fg, bg = bg, fg
            px, py = pad + x * cw, pad + y * ch
            if bg != BACKGROUND:
                draw.rectangle([px, py, px + cw + 1, py + ch], fill=bg)
            if c.data.strip():
                font = bold if c.bold and c.data.isascii() else regular  # Menlo Bold lacks some box glyphs
                draw.text((px, py + 2), c.data, font=font, fill=fg)
    return img


def load_fonts(size):
    """A monospace font with box-drawing glyphs: Menlo on macOS, DejaVu Sans Mono on Linux."""
    from PIL import ImageFont

    for path, regular, bold in (
        ("/System/Library/Fonts/Menlo.ttc", 0, 1),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0, 0),
    ):
        if os.path.exists(path):
            return ImageFont.truetype(path, size, index=regular), ImageFont.truetype(path, size, index=bold)
    sys.exit("no font found: install DejaVu Sans Mono or edit load_fonts() in scripts/demo.py")


def screenshot(env, out, cols=160, rows=19):
    img = render(capture(env, [(b"", 1.5), (b"j", 0.6)], cols, rows)[-1])  # the top row keeps its colors
    img.save(out, optimize=True)
    print(f"wrote {out} ({img.width}x{img.height})")


def gif(env, out, cols=160, rows=24):
    """The scripted interaction as a looping GIF, all frames on one shared palette."""
    from PIL import Image

    frames = [render(cells) for cells in capture(env, GIF_STEPS, cols, rows)]
    sheet = Image.new("RGB", (frames[0].width, frames[0].height * len(frames)))
    for i, frame in enumerate(frames):
        sheet.paste(frame, (0, i * frame.height))
    palette = sheet.quantize(colors=128, dither=Image.Dither.NONE)
    frames = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=[ms for *_, ms in GIF_STEPS],
                   loop=0, optimize=True, disposal=1)
    print(f"wrote {out} ({frames[0].width}x{frames[0].height}, {len(frames)} frames, {os.path.getsize(out) // 1024} KB)")


def social(env, out, tagline="A live, top-style view of your Claude Code sessions and plan usage"):
    """A 1280x640 card: the name, the tagline and the top of the TUI."""
    from PIL import Image, ImageDraw, ImageFont

    card = Image.new("RGB", (1280, 640), PALETTE["black"])
    draw = ImageDraw.Draw(card)
    tui = render(capture(env, [(b"", 1.5)], 130, 18)[-1], size=14, pad=14)
    x, y = (card.width - tui.width) // 2, 230
    regular, bold = load_fonts(64)
    draw.text((x, 44), "claude-top", font=bold, fill=FOREGROUND)
    draw.text((x, 136), tagline, font=ImageFont.truetype(regular.path, 26, index=regular.index), fill=PALETTE["cyan"])
    draw.rounded_rectangle([x - 1, y - 30, x + tui.width, y + tui.height - 8], radius=10, fill=BACKGROUND, outline="#3b3f5c")
    for i, color in enumerate(("red", "yellow", "green")):
        draw.ellipse([x + 14 + i * 22, y - 21, x + 26 + i * 22, y - 9], fill=PALETTE[color])
    card.paste(tui.crop((1, 8, tui.width - 1, tui.height - 1)), (x + 1, y))
    card.save(out, optimize=True)
    print(f"wrote {out} ({card.width}x{card.height})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--screenshot", metavar="PNG", help="render the demo view to this file and exit")
    ap.add_argument("--gif", metavar="GIF", help="record a short scripted session to this file and exit")
    ap.add_argument("--social", metavar="PNG", help="render a 1280x640 social preview card and exit")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        build(home)
        env = {"CLAUDE_CONFIG_DIR": str(home), "CLAUDE_TOP_RATE_LIMITS": str(home / "claude-top" / "rate_limits.json"),
               "CLAUDE_TOP_LIVE": "0", "HOME": str(home),  # no real sessions from `claude agents` or the desktop app
               "CLAUDE_TOP_NO_UPDATE_CHECK": "1"}  # and no request to GitHub
        if args.screenshot or args.gif or args.social:
            for path, make in ((args.screenshot, screenshot), (args.gif, gif), (args.social, social)):
                if path:
                    make(env, path)
            return
        os.environ.update(env)
        os.execvp(sys.executable, [sys.executable, str(ROOT / "claude_top.py")])


if __name__ == "__main__":
    main()
