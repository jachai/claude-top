#!/usr/bin/env python3
"""Run claude-top against made-up sessions, or render that view to a PNG.

  python3 scripts/demo.py                       # interactive demo, nothing real is read
  uv run --with pyte --with pillow scripts/demo.py --screenshot docs/screenshot.png
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
# subagents (running, finished), note
SESSIONS = [
    ("api rate-limit middleware", "working", 0.1, 1.2, "claude-opus-5-5", "high", 182_000, (2, 3),
     "Running the integration suite against the new limiter"),
    ("flaky checkout e2e test", "blocked", 2, 3.5, "claude-sonnet-5-5", "medium", 96_000, (0, 1),
     "Retry the test 3x in CI, or fix the clock mock? (recommend: fix the mock)"),
    ("docs site dark mode", "working", 4, 0.6, "claude-sonnet-5-5", "medium", 61_000, (1, 1),
     "Checking contrast on code blocks"),
    ("postgres 17 upgrade plan", "blocked", 38, 26, "claude-opus-5-5", "high", 415_000, (0, 6),
     "Approve the maintenance window: Sat 02:00 or Sun 02:00?"),
    ("release notes v2.4", "done", 55, 5, "claude-haiku-5-5", "low", 24_000, (0, 0),
     "Release notes drafted in CHANGELOG.md; 14 PRs grouped by area"),
    ("billing webhook retries", "done", 130, 30, "claude-opus-5-5", "high", 298_000, (0, 4),
     "PR #812 open: idempotent retries with a dead-letter queue; tests green"),
    ("onboarding email copy", "blocked", 300, 50, "claude-sonnet-5-5", "medium", 88_000, (0, 0),
     "Pick a subject line: A \"Welcome aboard\" or B \"Your workspace is ready\""),
    ("search index rebuild", "done", 610, 70, "claude-opus-5-5", "high", 640_000, (0, 12),
     "Index rebuilt in 41 min (was 3h); benchmark in docs/search-bench.md"),
    ("ios crash triage", "stopped", 1500, 96, "claude-sonnet-5-5", "medium", 152_000, (0, 2),
     "Stopped by user"),
    ("terraform drift report", "done", 2900, 140, "claude-opus-5-5", "medium", 205_000, (0, 3),
     "3 drifted resources; fixes in PR #790"),
    ("dependency audit", "done", 4400, 200, "claude-haiku-5-5", "low", 41_000, (0, 0),
     "No critical advisories; 6 minor bumps queued"),
]


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def build(home):
    now = time.time()
    project = home / "projects" / "-home-dev-acme-app"
    project.mkdir(parents=True)
    for i, (name, state, mins, hours, model, effort, ctx, (running, finished), note) in enumerate(SESSIONS):
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
            meta = {"agentType": "general-purpose", "description": f"helper {n + 1}",
                    "model": "sonnet" if n % 2 else "haiku", "finished": n >= running}
            (subagents / f"agent-{n}.meta.json").write_text(json.dumps(meta))
            (subagents / f"agent-{n}.jsonl").write_text("{}\n")  # running helpers have a fresh transcript
        key = "needs" if state == "blocked" else "detail"
        (job / "state.json").write_text(json.dumps({
            "state": state, "name": name, key: note, "sessionId": sid, "resumeSessionId": sid,
            "cwd": "/home/dev/acme-app", "linkScanPath": str(transcript),
            "createdAt": iso(created), "updatedAt": iso(last),
        }))

    data = home / "claude-top"
    data.mkdir()
    five_reset, week_reset = now + 2 * 3600, now + 4.2 * 86400
    (data / "rate_limits.json").write_text(json.dumps({"ts": int(now), "rate_limits": {
        "five_hour": {"used_percentage": 38, "resets_at": int(five_reset)},
        "seven_day": {"used_percentage": 54, "resets_at": int(week_reset)},
    }}))
    history = [{"t": now - 4.5 * 3600, "u": {"fh": 0, "sd": 48}}, {"t": now - 2.5 * 3600, "u": {"fh": 6, "sd": 50}}, {"t": now - 3600, "u": {"fh": 22, "sd": 52}}]
    (data / "usage-history.jsonl").write_text("\n".join(json.dumps(h) for h in history) + "\n")


def screenshot(env, out, cols=170, rows=21):
    import fcntl
    import pty
    import select
    import struct
    import termios

    import pyte
    from PIL import Image, ImageDraw

    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(env, TERM="xterm-256color")
        os.execvp(sys.executable, [sys.executable, str(ROOT / "claude_top.py")])
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    stream.feed(os.read(fd, 65536))
                except OSError:
                    return

    pump(1.5)
    os.write(fd, b"j")  # highlight the second row so the detail line shows its subagents
    pump(1.0)
    os.write(fd, b"q")
    pump(0.5)
    os.waitpid(pid, 0)

    foreground = "#c8d3f5"
    palette = {"black": "#1b1d2b", "red": "#ff757f", "green": "#c3e88d",
               "yellow": "#ffc777", "brown": "#ffc777", "blue": "#82aaff", "magenta": "#c099ff", "cyan": "#86e1fc", "white": "#c8d3f5"}
    background = "#222436"
    size, pad = 15, 18
    regular, bold = load_fonts(size)
    cw = regular.getlength("M")
    ch = int(size * 1.35)
    img = Image.new("RGB", (int(cols * cw + 2 * pad), rows * ch + 2 * pad), background)
    draw = ImageDraw.Draw(img)
    for y in range(rows):
        for x in range(cols):
            c = screen.buffer[y][x]
            fg = palette.get(c.fg, foreground)
            bg = palette.get(c.bg, background)
            if c.reverse:
                fg, bg = bg, fg
            px, py = pad + x * cw, pad + y * ch
            if bg != background:
                draw.rectangle([px, py, px + cw + 1, py + ch], fill=bg)
            if c.data.strip():
                font = bold if c.bold and c.data.isascii() else regular  # Menlo Bold lacks some box glyphs
                draw.text((px, py + 2), c.data, font=font, fill=fg)
    img.save(out)
    print(f"wrote {out} ({img.width}x{img.height})")


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--screenshot", metavar="PNG", help="render the demo view to this file and exit")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        build(home)
        env = {"CLAUDE_CONFIG_DIR": str(home), "CLAUDE_TOP_RATE_LIMITS": str(home / "claude-top" / "rate_limits.json"),
               "CLAUDE_TOP_LIVE": "0"}  # don't mix in your real sessions from `claude agents`
        if args.screenshot:
            screenshot(env, args.screenshot)
            return
        os.environ.update(env)
        os.execvp(sys.executable, [sys.executable, str(ROOT / "claude_top.py")])


if __name__ == "__main__":
    main()
