#!/usr/bin/env python3
"""agents-top: a live, top-style view of Claude Code and Codex sessions.

  agents-top                  full screen, refreshes every 2s
  agents-top -i 5             refresh every 5s
  agents-top -s waiting       start filtered to sessions waiting for input (or -s blocked)
  agents-top -g billing       start with a text filter
  agents-top --once           print once and exit
  agents-top init             set agents-top as your Claude Code status line (--wrap keeps an existing one)
  agents-top statusline       status-line command: records plan limits, prints a short usage line
  agents-top record           same, but prints nothing (pipe your own status line's input into it)

Keys: up/down j/k move, PgUp/PgDn page, g/G top/bottom, enter attach, c copy the attach command,
o sort, f state filter, / text filter, r show retired guides, +/- refresh speed, space refresh now, ? help,
i how to use,
sessions sit in sections (needs input, working, review, done, stopped, idle); enter or click on a section's line
(or right/left) opens and closes it, e opens every section or puts them back,
q quit, u update (shown when a newer release is out; CLAUDE_TOP_NO_UPDATE_CHECK=1 turns the daily check off).

Session titles (session-guide's markings; a title changes on that session's next prompt, but a finished one
always shows as closed here):
  🟥🟥 the active Sessions Manager (guide), always the first row
  🎟️🎟️ a retired guide (handed over; r shows or hides them)
  🟧🟧 (g) managed: still in play, it reports to the guide
  ⬜⬜ watched: the guide reads it and passes on its questions
  🟦🟦 active: running, neither managed nor watched
  ✅ finished: the guide closed it, or its ledger row is DONE
"""
import argparse
import collections
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

try:
    import curses
except ImportError:  # Windows: only --once works
    curses = None

__version__ = "0.6.3"

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
CODEX_DIR = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
JOBS = CLAUDE_DIR / "jobs"
PROJECTS = CLAUDE_DIR / "projects"
DATA_DIR = CLAUDE_DIR / "claude-top"
RATE_LIMITS = Path(os.environ.get("CLAUDE_TOP_RATE_LIMITS") or DATA_DIR / "rate_limits.json")
HISTORY = DATA_DIR / "usage-history.jsonl"
HISTORY_KEEP_DAYS = 8
UPDATE_CHECK = DATA_DIR / "update-check.json"
UPDATE_EVERY = 86400
UPDATE_URL = "https://api.github.com/repos/jachai/claude-top/releases/latest"
RELEASES_URL = "https://github.com/jachai/claude-top/releases/latest"
PACKAGE = "claude-agents-top"
GUIDES = CLAUDE_DIR / "session-guide" / "guides"  # session-guide's marker per session: a ledger path, retired or guided
GUIDE_TITLE = re.compile("^\U0001f7e5\U0001f7e5 Sessions Manager")  # session-guide's default titles, for a missing marker
RETIRED_TITLE = re.compile("^(?:\U0001f7e9\U0001f7e9|\U0001f39f\ufe0f\U0001f39f\ufe0f) Sessions Manager \\(retired\\)")
DESKTOP_HISTORY = Path.home() / "Library" / "Application Support" / "Claude" / "plan-usage-history.json"

TAIL_BYTES = 512 * 1024
MAX_TAIL_BYTES = 2 * 1024 * 1024


def guard_window():
    """agent-guard's context_ask_tokens (the limit it stops a session at), else 200k."""
    home = os.environ.get("AGENT_GUARD_HOME") or os.path.expanduser("~/.agent-guard")
    try:
        with open(os.path.join(home, "config.json")) as fh:
            value = int(json.load(fh)["context_ask_tokens"])
        if value > 0:
            return value
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 200_000


DEFAULT_WINDOW = int(os.environ.get("CLAUDE_TOP_CONTEXT_WINDOW") or guard_window())
SUBAGENT_IDLE_MINUTES = 15
IDLE_HOURS = float(os.environ.get("CLAUDE_TOP_IDLE_HOURS") or 24)

STATES = ["all", "review", "blocked", "working", "done", "stopped"]
LIVE_STATES = ("working", "blocked", "review")  # done/stopped sessions keep a stale inFlight snapshot
GROUPS = ["blocked", "working", "review", "done", "stopped", "idle"]  # one section per group, in this order
FOLDED = ("review", "done", "stopped", "idle")  # sections that start folded into their one line
GROUP_LABEL = {"blocked": "needs input", "working": "working", "review": "review", "done": "done", "stopped": "stopped"}
GUIDE_DONE = re.compile(r"^\W*(done|retired|stood down|closed)\b", re.I)  # a ledger Level that ends a worker
SORTS = ["last active", "created", "context", "agents", "name"]
STATE_LABEL = {"review": "review", "blocked": "waiting", "working": "working", "done": "done", "stopped": "stopped"}
STATE_ALIASES = {"waiting": "blocked"}  # -s takes Claude Code's word or the one the STATE column shows
LIMITS = [  # rate_limits key, label, window seconds, short key
    ("five_hour", "5h", 5 * 3600, "fh"),
    ("seven_day", "week", 7 * 86400, "sd"),
]
HEADER_ROWS = 4 + len(LIMITS)  # title, agents, one per limit, filter line, column header
FOOTER_ROWS = 2
KEYS = " enter attach/open  c copy  o sort  f state  / filter  ? help  i how-to  q quit"  # e, +/- are in ? help
LEGEND = (" \U0001f7e5 manager  \U0001f39f\ufe0f retired  \U0001f7e7 guided  \u2b1c watched  \U0001f7e6 active"
          "  \u2705 closed ")  # session-guide's title colors; shown once it has been used here
HOW_TO_USE = [
    "how to use",
    "  Each row is a Claude Code or Codex session. Press enter to open one, c to copy",
    "  the command that opens it. Sessions waiting on you come first.",
    "",
    "  Keep an eye on the state: needs input means a session is stuck until",
    "  you answer it. Open and close a section with enter, or all with e.",
    "  Narrow the list with f (state) and / (text), and sort it with o.",
    "",
    "with session-guide",
    "  /session-guide                 make this session the manager",
    "  /session-guide join            opt a session in as guided",
    "  /session-guide leave           opt it back out",
    "  /session-guide handoff         hand over to a new manager",
    "  Take over as guide. Ledger: <path>   (after /session-guide)",
    "",
    "the title colors",
    "  \U0001f7e5 manager   the one active Sessions Manager, always the first row",
    "  \U0001f39f\ufe0f retired   a manager that handed over; r shows or hides them",
    "  \U0001f7e7 guided    (g) managed: it reports to the manager, not to you",
    "  \u2b1c watched   the manager reads it and passes on its questions",
    "  \U0001f7e6 active    running, but neither managed nor watched",
    "  \u2705 closed    finished; the manager closed it",
    "",
    "  The manager interviews you about new sessions, decisions and approvals.",
    "  A title changes on that session's next prompt.",
]

_transcript_cache = {}
_path_cache = {}
_agents_cache = {}
_codex_cache = {}
_codex_names_cache = None


# ---------------------------------------------------------------- helpers


def parse_ts(value):
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def clean(text):
    """Session text is shown verbatim: drop control characters so it can't drive the terminal."""
    return "".join(c if c.isprintable() else " " for c in str(text))


def fit(text, width):
    """text cut to width at a word boundary, with … when anything was dropped."""
    if len(text) <= width:
        return text
    if width < 2:
        return text[:max(0, width)]
    cut = text[:width - 1]
    if " " in cut.strip():
        cut = cut[:cut.rstrip().rindex(" ")]
    return cut.rstrip(" ,;:·-") + "…"


def ago(ts, now):
    secs = max(0, int((now - ts).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def span(secs):
    secs = max(0, int(secs))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    if d:
        return f"{d}d{h}h"
    return f"{h}h{rem // 60:02d}m" if h else f"{rem // 60}m"


def short_model(model):
    """claude-opus-4-1-20250805 -> opus 4.1"""
    if not model:
        return "-"
    if not str(model).startswith("claude-"):
        return str(model)
    model = re.sub(r"-\d{8}$", "", str(model))
    return model.replace("claude-", "").replace("-", " ", 1).replace("-", ".")


def fmt_tokens(n):
    if n is None:
        return "-"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n // 1000}k" if n >= 1000 else str(n)


def load_json(path):
    """The file's JSON object, or None if it is missing, unreadable or not an object."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def json_lines(text):
    """Each parseable line of a .jsonl, skipping blank and corrupt ones."""
    for line in text.splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            pass


def copy_to_clipboard(text):
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True)
                return True
            except (OSError, subprocess.CalledProcessError):
                pass
    return False


# ---------------------------------------------------------------- plan usage


def record(status_input):
    """Save the rate_limits Claude Code hands its status line, plus a history sample."""
    limits = (status_input or {}).get("rate_limits")
    if not isinstance(limits, dict):
        return None
    snap = {"ts": int(time.time()), "rate_limits": limits}
    tmp = RATE_LIMITS.with_name(f"{RATE_LIMITS.name}.{os.getpid()}")
    try:
        RATE_LIMITS.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(snap))
        os.replace(tmp, RATE_LIMITS)
    except OSError:
        return None  # never break the user's status line
    Usage().append_history(snap)
    return snap


class Usage:
    """Plan limits plus a sample history, so we can tell the recent burn rate."""

    def __init__(self, rate_hours=3.0):
        self.rate_hours = rate_hours
        self.samples = []  # (epoch seconds, {"fh": pct, "sd": pct})
        self.snap = None
        desktop = load_json(DESKTOP_HISTORY) or {}  # the Claude desktop app keeps one on macOS, in ms
        self.add_samples(desktop.get("samples") or [], scale=1 / 1000.0)
        try:
            self.add_samples(json_lines(HISTORY.read_text()))
        except OSError:
            pass
        self.samples.sort(key=lambda s: s[0])

    def add_samples(self, samples, scale=1.0):
        for s in samples:
            if isinstance(s, dict) and isinstance(s.get("u"), dict) and isinstance(s.get("t"), (int, float)):
                self.samples.append((s["t"] * scale, s["u"]))

    @staticmethod
    def percents(snap):
        return {hk: (snap["rate_limits"].get(key) or {}).get("used_percentage") for key, _, _, hk in LIMITS}

    def append_history(self, snap):
        ts, u = float(snap["ts"]), self.percents(snap)
        last = self.samples[-1] if self.samples else None
        if last and last[1] == u and ts - last[0] < 900:
            return  # nothing changed; keep the file small
        self.samples.append((ts, u))
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with HISTORY.open("a") as fh:
                fh.write(json.dumps({"t": ts, "u": u}) + "\n")
            if HISTORY.stat().st_size > 256 * 1024:
                self.trim_history(ts)
        except OSError:
            pass

    @staticmethod
    def trim_history(now):
        keep_after = now - HISTORY_KEEP_DAYS * 86400
        kept = [s for s in json_lines(HISTORY.read_text()) if isinstance(s, dict) and (s.get("t") or 0) >= keep_after]
        tmp = HISTORY.with_name(f"{HISTORY.name}.{os.getpid()}")
        tmp.write_text("".join(json.dumps(s) + "\n" for s in kept))
        os.replace(tmp, HISTORY)

    def refresh(self):
        snap = load_json(RATE_LIMITS)
        if snap and isinstance(snap.get("rate_limits"), dict):
            self.snap = snap

    def windows(self, now):
        out = []
        limits = (self.snap or {}).get("rate_limits") or {}
        for key, label, window, hk in LIMITS:
            lim = limits.get(key) if isinstance(limits.get(key), dict) else {}
            try:
                used, resets = float(lim["used_percentage"]), float(lim["resets_at"])
            except (KeyError, TypeError, ValueError):
                out.append({"label": label, "used": None})
                continue
            if resets <= now:  # snapshot older than its reset: the window has rolled over since
                resets += (math.floor((now - resets) / window) + 1) * window
            start = min(resets - window, now)
            elapsed = min(max((now - start) / window, 0.001), 1.0)
            avg_rate = used / ((now - start) / 3600) if now > start else 0.0
            recent_rate = self.recent_rate(hk, used, now, start, window)
            rate = recent_rate if recent_rate is not None else avg_rate
            one_x = 100.0 / (window / 3600)  # % per hour that lands on exactly 100% at reset
            w = {
                "label": label, "used": used, "resets": resets, "elapsed": elapsed,
                "pace": (used / 100.0) / elapsed,
                "recent_pace": recent_rate / one_x if recent_rate is not None else None,
                "stale": now - float(self.snap.get("ts") or now),
            }
            if rate > 0 and used < 100:
                out_at = now + (100.0 - used) / rate * 3600
                w["runs_out"] = out_at if out_at < resets else None
                w["at_reset"] = min(100.0, used + rate * (resets - now) / 3600)
            else:
                w["runs_out"], w["at_reset"] = (now if used >= 100 else None), used
            out.append(w)
        return out

    def recent_rate(self, hk, used, now, window_start, window):
        # Limits come in whole percents, so look back far enough for that to mean something:
        # at least 1/40 of the window (~4h for the week), and say nothing with under half of it.
        lookback = max(self.rate_hours * 3600, window / 40)
        since = max(window_start, now - lookback)
        old = [(t, u.get(hk)) for t, u in self.samples if t >= since and u.get(hk) is not None]
        if not old or now - old[0][0] < lookback / 2:
            return None
        t0, v0 = old[0]
        return max(0.0, (used - float(v0)) / ((now - t0) / 3600))


def usage_bar(used, elapsed, width):
    """Filled to the % used, with a | where the window's elapsed time is."""
    filled = min(width, round(used / 100.0 * width))
    # box-drawing glyphs: terminals draw them natively, so no font ligature turns a run into a pattern
    cells = ["█" if i < filled else "─" for i in range(width)]
    cells[min(width - 1, round(elapsed * width))] = "┼" if round(elapsed * width) >= filled else "│"
    return "".join(cells)


def pace_gauge(pace, width):
    """0x .. 2x, with the 1x line in the middle."""
    filled = min(width, round(min(pace, 2.0) / 2.0 * width))
    mid = width // 2
    cells = ["▰" if i < filled else "▱" for i in range(width)]
    cells[mid] = "┃" if filled <= mid else "╋"
    return "".join(cells)


def pace_level(pace):
    return "ok" if pace < 0.9 else "warn" if pace < 1.1 else "hot"


def usage_segments(w, now):
    """One header line per limit, as [(text, color level)]."""
    if w["used"] is None:
        return [(f" Claude {w['label']:<5}", "bold"), ("no data yet: run `agents-top init` to record your plan limits", "dim")]
    level = pace_level(w["pace"])
    reset_local = datetime.fromtimestamp(w["resets"]).strftime("%a %H:%M")
    segs = [
        (f" Claude {w['label']:<5}", "bold"),
        (usage_bar(w["used"], w["elapsed"], 20), level),
        (f" {w['used']:>3.0f}% used · {w['elapsed'] * 100:>2.0f}% time · reset {reset_local} ({span(w['resets'] - now)}) · ", None),
        (f"{w['pace']:.1f}x ", level),
        (pace_gauge(w["pace"], 8), level),
    ]
    if w["recent_pace"] is not None:
        segs.append((f" now {w['recent_pace']:.1f}x", pace_level(w["recent_pace"])))
    if w["runs_out"]:
        out = datetime.fromtimestamp(w["runs_out"]).strftime("%a %H:%M")
        segs.append((f" · → out {out} ({span(w['resets'] - w['runs_out'])} early)", "hot"))
    else:
        segs.append((f" · → ~{w['at_reset']:.0f}% at reset", "ok"))
    if w["stale"] > 3600:
        segs.append((f" (as of {span(w['stale'])} ago)", "dim"))
    return segs


def statusline_text(usage, now):
    parts = []
    for w in usage.windows(now):
        if w["used"] is None:
            continue
        text = f"{w['label']} {w['used']:.0f}% {w['pace']:.1f}x"
        if w["runs_out"]:
            text += " → out " + datetime.fromtimestamp(w["runs_out"]).strftime("%a %H:%M")
        parts.append(text)
    return " · ".join(parts)


# ---------------------------------------------------------------- sessions


def find_transcript(state):
    sid = state.get("sessionId") or ""
    path = state.get("linkScanPath")
    if path and os.path.exists(path):
        return path
    if not sid:
        return None
    if sid not in _path_cache:  # misses aren't cached: a new session's transcript appears a moment later
        hits = list(PROJECTS.glob(f"*/{sid}.jsonl"))
        if not hits:
            return None
        _path_cache[sid] = str(hits[0])
    return _path_cache[sid]


def complete(info):
    return (info["model"] and info["effort"] and info["ctx"] is not None and info["activity"] is not None
            and info["last_ts"] is not None)


def activity_of(msg):
    """What a main-thread turn is doing: its tool step's description, else its first line of text."""
    content = msg.get("content")
    if not isinstance(content, list):
        return ""
    for block in reversed(content):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_input = block.get("input") or {}
            return str(tool_input.get("description") or tool_input.get("prompt") or block.get("name") or "")[:200]
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text") or "").strip():
            return str(block["text"]).strip().splitlines()[0][:200]
    return ""


def scan_lines(lines, info):
    """Walk transcript lines newest-first until model, effort, context, activity and last message are known."""
    for raw in reversed(lines):
        if complete(info):
            return
        if b'"api-request"' not in raw and b'"assistant"' not in raw and b'"user"' not in raw:
            continue
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        kind = e.get("type")
        if kind in ("user", "assistant") and info["last_ts"] is None and not e.get("isSidechain"):
            # the file's mtime is no good: bookkeeping lines (e.g. artifact-autoreact-ledger) keep
            # touching finished sessions' transcripts, so the newest real message is the activity time
            info["last_ts"] = parse_ts(e.get("timestamp"))
        if kind == "api-request" and not info["effort"]:
            if e.get("querySource") not in (None, "repl_main_thread"):
                continue
            params = e.get("params") or {}
            info["model"] = info["model"] or params.get("model")
            info["effort"] = (params.get("output_config") or {}).get("effort")
        elif kind == "assistant" and not e.get("isSidechain"):
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            if info["activity"] is None:
                info["activity"] = activity_of(msg) or None
            if info["ctx"] is not None:
                continue
            usage = msg.get("usage")
            if not usage:
                continue
            info["ctx"] = sum(
                usage.get(k) or 0
                for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
            info["model"] = info["model"] or msg.get("model")
            info["effort"] = info["effort"] or e.get("perTurnEffort")


def transcript_info(path):
    info = {"model": None, "effort": None, "ctx": None, "activity": None, "last_ts": None, "mtime": None}
    if not path:
        return info
    try:
        st = os.stat(path)
    except OSError:
        return info
    cached = _transcript_cache.get(path)
    if cached and cached[0] == (st.st_size, st.st_mtime):
        return cached[1]
    info["mtime"] = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    size, scanned = TAIL_BYTES, 0
    try:
        with open(path, "rb") as fh:
            while True:  # read only the tail; widen it if the answer isn't there yet
                fh.seek(max(0, st.st_size - size))
                lines = fh.read().split(b"\n")
                if st.st_size > size:
                    lines = lines[1:]  # the first line is cut mid-way
                scan_lines(lines[: len(lines) - scanned], info)  # the newest lines were scanned last round
                scanned = len(lines)
                if complete(info) or size >= min(st.st_size, MAX_TAIL_BYTES):
                    break
                size *= 4
    except OSError:
        return info
    _transcript_cache[path] = ((st.st_size, st.st_mtime), info)
    return info


def agents_info(transcript, live):
    """Subagents a session started: total, running now, and the model mix.

    Only a live session can have running subagents, and a subagent counts as running only while it is
    not finished AND its transcript is still being written: a meta can miss its finished flag (crash, kill).
    """
    info = {"total": 0, "running": 0, "models": {}, "active": []}
    if not transcript:
        return info
    d = Path(transcript).with_suffix("") / "subagents"
    try:
        mtime = d.stat().st_mtime
    except OSError:
        return info
    cached = _agents_cache.get(str(d))
    if cached and cached[0] == mtime and not live:
        return cached[1]
    for meta_file in d.glob("*.meta.json"):
        meta = load_json(meta_file) or {}
        info["total"] += 1
        model = clean(meta.get("model") or "inherit")
        info["models"][model] = info["models"].get(model, 0) + 1
        if live and not meta.get("finished") and recently_written(meta_file.with_suffix("").with_suffix(".jsonl")):
            info["running"] += 1
            info["active"].append(clean(meta.get("description") or meta.get("agentType") or meta_file.stem))
    if not live:  # a live session's count depends on the clock, and its dir mtime won't change when it ends
        _agents_cache[str(d)] = (mtime, info)
    return info


def recently_written(path, minutes=SUBAGENT_IDLE_MINUTES):
    try:
        return time.time() - path.stat().st_mtime < minutes * 60
    except OSError:
        return False


def guide_role(session_ids, name):
    """(role, ledger) for a session-guide (github.com/jachai/session-guide) session; role is None for any other.

    "guide" is the active Sessions Manager, "retired" one that handed over to a new guide. The skill keeps a marker
    per session in ~/.claude/session-guide/guides/: the ledger's path for a guide, `retired`, or a worker's state
    (`guided`, `unguided`, `done` for one the guide closed: role "closed"). Only a path is a guide, so a new worker
    state never pins its session on top. A session without a marker is told by the skill's default titles instead.
    """
    for sid in session_ids:
        if not re.fullmatch(r"[0-9A-Za-z-]+", sid or ""):  # it names a file: no path tricks from state.json
            continue
        try:
            content = (GUIDES / sid).read_text().strip()
        except OSError:
            continue
        if content == "retired":
            return "retired", None
        if content == "done":
            return "closed", None
        if "/" in content or content.endswith(".md"):
            return "guide", content
        return None, None
    if RETIRED_TITLE.match(name):
        return "retired", None
    return ("guide" if GUIDE_TITLE.match(name) else None), None


_ledger_cache = {}


def ledger_done(path):
    """{id prefix: Level} for the workers a session-guide ledger lists as finished (DONE, RETIRED, ...).

    The ledger's `## Sessions` table names each worker `Title [id6]` in its first cell and its Level in the second.
    """
    try:
        st = os.stat(path)
        if st.st_size > 2 * 1024 * 1024:
            return {}
        cached = _ledger_cache.get(path)
        if cached and cached[0] == (st.st_size, st.st_mtime):
            return cached[1]
        text = Path(path).read_text(errors="replace")
    except (OSError, ValueError):
        return {}
    done, in_sessions = {}, False
    for line in text.splitlines():
        if line.startswith("## "):
            in_sessions = line[3:].strip().lower() == "sessions"
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if in_sessions and line.startswith("|") else []
        if len(cells) < 2:
            continue
        ids = re.findall(r"\[([0-9a-f]{6,36})\]", cells[0])
        if ids and GUIDE_DONE.match(cells[1]):
            done[ids[-1]] = cells[1]
    _ledger_cache[path] = ((st.st_size, st.st_mtime), done)
    return done


WORKER_MARK = re.compile("^(?:\U0001f7e7\U0001f7e7 \\(g\\)|\\(g\\)|\u2b1c\u2b1c|\U0001f7e6\U0001f7e6|\u2705)\\s*")


def closed_name(name):
    """`name` as session-guide shows a finished session: a single "\u2705 " in place of any orange, gray or blue
    worker prefix. Only the display changes; the guide reads its own markers and ledger."""
    bare = name
    while WORKER_MARK.match(bare):
        bare = WORKER_MARK.sub("", bare, count=1)
    return f"\u2705 {bare}" if bare else name


def apply_guide_done(rows):
    """A session the session guide has finished with (its `done` marker, or DONE in the active guide's ledger) shows
    as done rather than waiting on you. One that is working really is, whatever the ledger says, so it stays."""
    finished = {}
    for g in rows:
        if g["guide"] == "guide" and g["ledger"]:
            finished.update(ledger_done(os.path.expanduser(g["ledger"])))
    for r in rows:
        level = "closed by the session guide" if r["guide"] == "closed" else next(
            (lv for prefix, lv in finished.items() if r["id"].startswith(prefix) or prefix.startswith(r["id"])), None)
        if level and r["state"] in ("blocked", "review"):
            r["guide_done"] = level
            r["state"] = "done"
            r["note"] = clean(f"session guide: {level}")
        # finished = the guide closed it (marker) or its ledger row is DONE, and it is not working now. A job that
        # merely reads done, or is waiting for its guide, is not: it keeps whatever prefix its title has.
        if level and r["state"] != "working" and r["guide"] not in ("guide", "retired") and not re.match(r"\W*retired\b", level, re.I):
            r["name"] = closed_name(r["name"])
    return rows


def live_sessions():
    """Live state per session from `claude agents --json --all`; state.json lags it. {} if unavailable."""
    if os.environ.get("CLAUDE_TOP_LIVE") == "0" or not shutil.which("claude"):
        return {}
    try:
        out = subprocess.run(["claude", "agents", "--json", "--all"], capture_output=True, text=True, timeout=5).stdout
        return {x["id"]: x for x in json.loads(out) if isinstance(x, dict) and x.get("id")}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return {}


def registered_sessions():
    """Open CLI sessions omitted by some versions of `claude agents`. Never read peer-token files."""
    sessions = {}
    for path in (CLAUDE_DIR / "sessions").glob("*.json"):
        entry = load_json(path)
        if not isinstance(entry, dict):
            continue
        sid, pid = entry.get("sessionId"), entry.get("pid")
        if not isinstance(sid, str) or not sid or type(pid) is not int or pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except PermissionError:
            pass  # an existing process owned by another user
        except (OSError, OverflowError):
            continue
        times = {}
        try:
            for source, target in (("startedAt", "createdAt"), ("updatedAt", "updatedAt")):
                if isinstance(entry.get(source), (int, float)):
                    times[target] = datetime.fromtimestamp(entry[source] / 1000, tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            continue
        status = entry.get("status")
        state = {"idle": "blocked", "busy": "working", "working": "working",
                 "waiting": "blocked", "blocked": "blocked"}.get(status) if isinstance(status, str) else None
        cwd = entry.get("cwd") if isinstance(entry.get("cwd"), str) else ""
        sessions[sid] = {"id": sid, "sessionId": sid, "pid": pid, "kind": entry.get("kind") or "interactive",
                         "state": state or "?", "name": entry.get("name") or Path(cwd).name,
                         "cwd": cwd, **times}
    return sessions


def merge_live(state, live_entry):
    """state.json overlaid with the agents view, whose state and name are current; state.json catches up later.

    A session the view reports done but still holds open (a pid), with a worktree to look at, is what the
    built-in view calls "Ready for review".
    """
    if not live_entry:
        return state
    live_state = live_entry.get("state") or state.get("state")
    worktree = state.get("worktreePath")
    if live_state == "done" and live_entry.get("pid") and worktree and os.path.isdir(worktree):
        live_state = "review"
    merged = {**state, "state": live_state, "name": live_entry.get("name") or state.get("name"),
              "sessionId": state.get("sessionId") or live_entry.get("sessionId"),
              "cwd": state.get("cwd") or live_entry.get("cwd")}
    for field in ("createdAt", "updatedAt"):
        incoming, current = parse_ts(live_entry.get(field)), parse_ts(merged.get(field))
        if incoming and (not current or (field == "updatedAt" and incoming > current)):
            merged[field] = live_entry[field]
    if not merged.get("createdAt") and isinstance(live_entry.get("startedAt"), int):
        merged["createdAt"] = datetime.fromtimestamp(live_entry["startedAt"] / 1000, tz=timezone.utc).isoformat()
    return merged


def in_flight(state, agents):
    """What a live session has in flight: (agents with the local_agent hint applied, background task count, kinds).

    artifact_watch tasks are passive watchers, not work; local_agent is a subagent, which the session reports
    before the helper's meta file exists.
    """
    flight = state.get("inFlight") if isinstance(state.get("inFlight"), dict) else {}
    kinds = [str(k) for k in flight.get("kinds") or []]
    if "local_agent" in kinds and not agents["running"]:
        agents = {**agents, "running": 1}
    watchers = (flight.get("drainableMonitors") or 0) if "artifact_watch" in kinds else 0
    bg_kinds = [k for k in kinds if k not in ("artifact_watch", "local_agent")]
    background = max(0, (flight.get("tasks") or 0) - watchers - agents["running"]) if bg_kinds else 0
    return agents, background, bg_kinds


def load_rows(live_list=None):
    rows = []
    if live_list is None:
        live_list = live_sessions()
    state_files = {p.parent.name: p for p in JOBS.glob("*/state.json")}
    states = {job_id: load_json(path) or {} for job_id, path in state_files.items()}
    known = {state.get("sessionId"): job_id for job_id, state in states.items() if state.get("sessionId")}
    known.update({entry["sessionId"]: job_id for job_id, entry in live_list.items() if entry.get("sessionId")})
    live_list = dict(live_list)
    for sid, entry in registered_sessions().items():
        job_id = known.get(sid, sid)
        live_list[job_id] = {**entry, **live_list.get(job_id, {})}
    for job_id in set(state_files) | set(live_list):
        state_file = state_files.get(job_id)
        live_entry = live_list.get(job_id) or {}
        state = states.get(job_id) or {}
        if not state and not live_entry:
            continue
        state = merge_live(state, live_entry)
        path = find_transcript(state)
        t = transcript_info(path)
        live = state.get("state") in LIVE_STATES
        agents = agents_info(path, live)
        if live:
            agents, background, bg_kinds = in_flight(state, agents)
        else:
            background, bg_kinds = 0, []
        updated = parse_ts(state.get("updatedAt"))
        if not updated and state_file:
            try:
                updated = datetime.fromtimestamp(state_file.stat().st_mtime, tz=timezone.utc)
            except OSError:  # session removed while we were reading
                continue
        created = parse_ts(state.get("createdAt"))
        last = max(filter(None, (updated, t["last_ts"] or t["mtime"])), default=created)  # a brand-new session has only its start
        if not last:
            continue
        result = state["output"].get("result") if isinstance(state.get("output"), dict) else None
        name = clean(state.get("name") or state.get("intent") or job_id)
        guide, ledger = guide_role((state.get("sessionId"), state.get("resumeSessionId")), name)
        note = state.get("needs") or (result if state.get("state") in ("done", "review") else None) or state.get("detail") or ""
        newest = t["last_ts"] or t["mtime"]
        stale_note = updated is None or (newest and (newest - updated).total_seconds() > 5)
        if state.get("state") == "working" and t["activity"] and stale_note:
            note = t["activity"]  # state.json still holds the note from before this turn
        rows.append({
            "id": clean(job_id),
            "provider": "claude",
            "kind": live_entry.get("kind") or "background",
            "state": state.get("state") or "?",
            "name": name,
            "note": clean(note),
            "search": "claude " + " ".join(str(state.get(k) or "") for k in ("name", "intent", "needs", "detail", "cwd")).lower(),
            "cwd": clean(state.get("cwd") or ""),
            "session": clean(state.get("resumeSessionId") or state.get("sessionId") or ""),
            "last": last,
            "created": created or last,
            "model": t["model"],
            "effort": t["effort"],
            "ctx": t["ctx"],
            "agents": agents,
            "bg": background,
            "bg_kinds": [clean(k) for k in bg_kinds],
            "guide": guide,
            "ledger": clean(ledger or ""),
        })
    return apply_guide_done(rows) + codex_rows()


# ---------------------------------------------------------------- Codex sessions


def codex_info(path):
    try:
        st = path.stat()
        stamp = (st.st_size, st.st_mtime_ns, st.st_ino)
        cached = _codex_cache.get(path)
        if cached and cached[0] == stamp:
            return cached[1]
        with path.open("rb") as fh:
            meta = json.loads(fh.readline(MAX_TAIL_BYTES))
            if not isinstance(meta, dict) or meta.get("type") != "session_meta":
                return None
            meta = meta.get("payload")
            if not isinstance(meta, dict) or not isinstance(meta.get("id"), str) or not meta["id"]:
                return None
            meta_end = fh.tell()
            tail_start = max(0, st.st_size - MAX_TAIL_BYTES)
            fh.seek(tail_start)
            lines = fh.read().splitlines()
            if st.st_size > MAX_TAIL_BYTES:
                lines = lines[1:]
    except (OSError, ValueError):
        return None
    cwd = clean(meta.get("cwd") or "")
    source = meta.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
    info = {"id": clean(meta["id"]), "provider": "codex", "session": clean(meta["id"]),
            "kind": "subagent" if parent else "interactive", "parent": clean(parent) if parent else None,
            "cwd": cwd, "name": clean(meta.get("agent_nickname") or Path(cwd).name or meta["id"]),
            "state": None, "last": None, "created": parse_ts(meta.get("timestamp")),
            "model": None, "effort": None, "ctx": None, "context_window": meta.get("context_window"),
            "note": "", "bg": 0, "bg_kinds": [], "guide": None, "ledger": ""}
    endings = {"task_started": "working", "turn_started": "working", "task_complete": "done",
               "turn_complete": "done", "turn_aborted": "stopped", "shutdown": "stopped"}
    context_found = False
    for raw in reversed(lines):
        if info["note"] and all(info[k] is not None for k in ("state", "last", "model", "effort", "ctx", "context_window")):
            break
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
            continue
        payload, kind = event["payload"], event.get("type")
        typ = payload.get("type")
        if not isinstance(kind, str) or not isinstance(typ, (str, type(None))):
            continue
        if kind in ("event_msg", "response_item", "turn_context") and info["last"] is None:
            info["last"] = parse_ts(event.get("timestamp"))
        if kind == "turn_context" and not context_found:
            info["model"], info["effort"] = payload.get("model"), payload.get("effort")
            context_found = True
        if info["state"] is None and kind in ("turn_context", "response_item"):
            info["state"] = "done" if payload.get("phase") == "final" else "working"
        if kind == "event_msg":
            if info["state"] is None and typ in endings:
                info["state"] = endings[typ]
            usage_info = payload.get("info")
            if typ == "token_count" and isinstance(usage_info, dict) and info["ctx"] is None:
                usage = usage_info.get("last_token_usage")
                if isinstance(usage, dict):
                    tokens = [usage.get("input_tokens", 0), usage.get("output_tokens", 0)]
                    if all(isinstance(n, (int, float)) and n >= 0 for n in tokens):
                        info["ctx"] = sum(tokens)
                info["context_window"] = usage_info.get("model_context_window") or info["context_window"]
            if not info["note"]:
                info["note"] = clean(payload.get("message") if typ in ("agent_message", "agent_reasoning")
                                     else payload.get("last_agent_message") or "")[:200]
        elif kind == "response_item" and not info["note"]:
            if typ == "message" and payload.get("role") == "assistant":
                content = payload.get("content")
                if isinstance(content, list):
                    info["note"] = clean(" ".join(str(b.get("text") or "") for b in content if isinstance(b, dict)))[:200]
            elif typ in ("function_call", "custom_tool_call"):
                info["note"] = clean(payload.get("name") or "")
    if not context_found:
        reusable = cached and cached[0][2] == st.st_ino and cached[0][0] <= st.st_size
        context = cached[1] if reusable else {}
        start = max(meta_end, cached[0][0] - MAX_TAIL_BYTES) if reusable else meta_end
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                while fh.tell() < tail_start:
                    raw = fh.readline(MAX_TAIL_BYTES)
                    if b'"turn_context"' not in raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "turn_context" and isinstance(event.get("payload"), dict):
                        context = event["payload"]
        except OSError:
            pass
        info["model"], info["effort"] = context.get("model"), context.get("effort")
    for key in ("model", "effort"):
        info[key] = clean(info[key]) if info[key] else None
    info["last"] = info["last"] or info["created"] or datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    info["created"] = info["created"] or info["last"]
    info["state"] = info["state"] or "stopped"
    info["search"] = " ".join(("codex", info["name"], cwd, info["note"], info["model"] or "")).lower()
    _codex_cache[path] = (stamp, info)
    return info


def codex_names():
    global _codex_names_cache
    names = {}
    try:
        path = CODEX_DIR / "session_index.jsonl"
        st = path.stat()
        stamp = (st.st_size, st.st_mtime_ns)
        if _codex_names_cache and _codex_names_cache[0] == stamp:
            return _codex_names_cache[1]
        with path.open() as fh:
            for line in fh:
                for entry in json_lines(line):
                    if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry.get("thread_name"):
                        names[entry["id"]] = clean(entry["thread_name"])
    except (OSError, ValueError):
        _codex_names_cache = None
        return names
    _codex_names_cache = (stamp, names)
    return names


def codex_rows():
    rows, paths = {}, set()
    names = codex_names()
    for path in (CODEX_DIR / "sessions").glob("**/rollout-*.jsonl"):
        paths.add(path)
        info = codex_info(path)
        if not info:
            continue
        row = {**info, "agents": {"total": 0, "running": 0, "models": {}, "active": []}}
        if row["id"] in names:
            row["name"] = names[row["id"]]
            row["search"] += " " + row["name"].lower()
        if row["state"] == "working" and (datetime.now(timezone.utc) - row["last"]).total_seconds() > SUBAGENT_IDLE_MINUTES * 60:
            row["state"] = "stopped"  # a killed process may never write its turn's ending
        rows[row["id"]] = row
    for path in set(_codex_cache) - paths:
        del _codex_cache[path]
    for row in rows.values():
        parent, seen = rows.get(row["parent"]), {row["id"]}
        while parent and parent["parent"] in rows and parent["id"] not in seen:
            seen.add(parent["id"])
            parent = rows[parent["parent"]]
        if parent is None or parent["id"] in seen:
            continue
        agents = parent["agents"]
        agents["total"] += 1
        model = clean(row["model"] or "inherit")
        agents["models"][model] = agents["models"].get(model, 0) + 1
        if row["state"] == "working":
            agents["running"] += 1
            agents["active"].append(row["name"])
    return [r for r in rows.values() if r["parent"] not in rows or r["parent"] == r["id"]]


def row_identity(row):
    return row.get("provider"), row["id"]


def attach_command(row):
    if row.get("provider") == "codex":
        cwd = ["-C", row["cwd"]] if row.get("cwd") else []
        return ["codex", *cwd, "resume", row["session"]]
    if row.get("kind") == "interactive":
        return ["claude", "--resume", row["session"]]
    return ["claude", "attach", row["id"]]


def attach_directory(row):
    return row.get("cwd") if row.get("provider") == "claude" and row.get("kind") == "interactive" else None


def select(rows, state, text, sort, retired=False):
    """The rows to show, sorted; the active guide always comes first and retired guides only with `retired`."""
    out = [r for r in rows if (state in (None, "all") or r["state"] == state) and (retired or r["guide"] != "retired")]
    if text:
        text = text.lower()
        out = [r for r in out if text in r["search"] or text in r["id"].lower()]
    key = {
        "last active": lambda r: r["last"],
        "created": lambda r: r["created"],
        "context": lambda r: r["ctx"] or 0,
        "agents": lambda r: (r["agents"]["running"], r["agents"]["total"]),
        "name": lambda r: r["name"].lower(),
    }[sort]
    out = sorted(out, key=key, reverse=sort != "name")
    return [r for r in out if r["guide"] == "guide"] + [r for r in out if r["guide"] != "guide"]


def retired_count(rows):
    return sum(1 for r in rows if r["guide"] == "retired")


def default_open(expand=False):
    return set(GROUPS) if expand else set(GROUPS) - set(FOLDED)


def group_of(r, now, idle_hours=IDLE_HOURS):
    """The section a session sits in, or None for the session guide, which stays on top of them all.

    done and stopped sessions are finished; any other with nothing for idle_hours is idle, whatever its state says.
    """
    if r["guide"] == "guide":
        return None
    if r["state"] in ("done", "stopped"):
        return r["state"]
    if (now - r["last"]).total_seconds() > idle_hours * 3600 or r["state"] not in GROUPS:
        return "idle"
    return r["state"]


def layout(rows, expanded, now, idle_hours=IDLE_HOURS):
    """The lines to show: the session guide, then one line per section with its sessions under it when the section
    is in expanded. rows come from select(), so every section keeps their order."""
    out, groups = [], {g: [] for g in GROUPS}
    for r in rows:
        g = group_of(r, now, idle_hours)
        (groups[g] if g else out).append(r)
    for g, members in groups.items():
        if members:
            out.append({"id": f"group:{g}", "group": g, "rows": members, "open": g in expanded})
            if g in expanded:
                out.extend(members)
    return out


def group_label(group, idle_hours=IDLE_HOURS):
    if group != "idle":
        return GROUP_LABEL[group]
    return f"idle {idle_hours / 24:g}d+" if idle_hours % 24 == 0 else f"idle {idle_hours:g}h+"


def group_line(item, now, width, idle_hours=IDLE_HOURS):
    """A group's one line: open or closed, how many sessions, when the newest was active, and their names."""
    members = item["rows"]
    n = len(members)
    text = (f" {'▾' if item['open'] else '▸'} {group_label(item['group'], idle_hours):<11} {n:>3} session{'' if n == 1 else 's'}"
            f" · last active {ago(max(r['last'] for r in members), now)} ago")
    if item["group"] == "idle":
        states = collections.Counter(STATE_LABEL.get(r["state"], r["state"]) for r in members)
        text += " (" + ", ".join(f"{c} {s}" for s, c in states.most_common()) + ")"
    if item["open"]:
        return fit(text, width)
    return fit(text + "  ·  " + ", ".join(r["name"] for r in members), width)


def folded_count(view):
    return sum(len(item["rows"]) for item in view if item.get("group") and not item["open"])


COLUMNS = [
    ("LAST ACTIVE", 12), ("AGO", 4), ("AGE", 4), ("STATE", 8), ("TOOL", 6), ("ID", 8), ("MODEL", 14), ("EFFORT", 6),
    ("RUN", 3), ("SPAWNED", 7), ("BG", 3), ("CTX", 6), ("CTX %", 10), ("NAME", 34),
]
COL = {h: i for i, (h, _) in enumerate(COLUMNS)}  # column -> index into row_cells
COL_X = {h: sum(w + 2 for _, w in COLUMNS[:i]) for i, (h, _) in enumerate(COLUMNS)}  # column -> screen x


def row_cells(r, now, window):
    """One cell per column; CTX % is a fraction (or None) that plain_line draws as a bar."""
    a = r["agents"]
    context_window = r.get("context_window") if r.get("provider") == "codex" else window
    if not isinstance(context_window, (int, float)) or context_window <= 0:
        context_window = None
    return [
        r["last"].astimezone().strftime("%b %d %H:%M"),
        ago(r["last"], now),
        ago(r["created"], now),
        STATE_LABEL.get(r["state"], r["state"]),
        r.get("provider", "claude"),
        r["id"],
        short_model(r["model"]),
        r["effort"] or "-",
        str(a["running"]) if a["running"] else "-",
        str(a["total"]) if a["total"] else "-",
        str(r["bg"]) if r["bg"] else "-",
        fmt_tokens(r["ctx"]),
        r["ctx"] / context_window if r["ctx"] is not None and context_window else None,
        r["name"],
    ]


def bar(pct, width):
    if pct is None:
        return " " * width
    filled = min(width, round(pct * width))
    return "█" * filled + "·" * (width - filled)


def plain_line(cells):
    parts = []
    for i, ((_, w), cell) in enumerate(zip(COLUMNS, cells)):
        if i == COL["CTX %"] and not isinstance(cell, str):  # the header row is all text
            cell = bar(cell, w - 4) + (f"{round(cell * 100):>3}%" if cell is not None else "    ")
        parts.append(f"{str(cell)[:w]:<{w}}")
    return "  ".join(parts)


def totals(rows):
    counts = {s: sum(1 for r in rows if r["state"] == s) for s in STATES[1:]}
    live = [r for r in rows if r["state"] == "working"]
    return {
        **counts,
        "sessions": len(rows),
        "subagents_running": sum(r["agents"]["running"] for r in rows),
        "spawned": sum(r["agents"]["total"] for r in rows),
        "spawned_live": sum(r["agents"]["total"] for r in live),
        "background": sum(r["bg"] for r in rows),
    }


def summary(rows):
    """Two short header lines: sessions by state, then everything running right now."""
    t = totals(rows)
    sessions = (f"sessions {t['sessions']}: {t['working']} work · {t['blocked']} wait · "
                + (f"{t['review']} review · " if t["review"] else "")
                + f"{t['done']} done · {t['stopped']} stop")
    agents = (f"agents now {t['working'] + t['subagents_running']} "
              f"({t['working']} sess + {t['subagents_running']} sub) │ "
              f"spawned {t['spawned']} ({t['spawned_live']} live) │ bg {t['background']}")
    return sessions, agents


def selected_detail(r):
    if r.get("group"):
        return f" {group_label(r['group'])}: " + " · ".join(f"{m['id']} {m['name']}" for m in r["rows"])
    a = r["agents"]
    parts = [f" {r.get('provider', 'claude')} {r['id']}  {r['cwd']}  session {r['session']}"]
    if r["guide"] == "guide":
        parts.append("session guide" + (f", ledger {r['ledger']}" if r["ledger"] else ""))
    if r.get("guide_done"):
        parts.append("done per the session guide; Claude Code still says waiting")
    if a["total"]:
        models = ", ".join(f"{m} {n}" for m, n in sorted(a["models"].items(), key=lambda kv: -kv[1]))
        parts.append(f"subagents {a['total']} ({models})")
        if a["active"]:
            parts.append("running: " + "; ".join(a["active"]))
    if r["bg_kinds"]:
        parts.append("background: " + ", ".join(k.replace("local_", "") for k in r["bg_kinds"]))
    return "   ".join(parts)


def print_once(args):
    now, now_s = datetime.now(timezone.utc), time.time()
    width = shutil.get_terminal_size((200, 20)).columns
    usage = Usage(args.rate_hours)
    usage.refresh()
    rows = load_rows()
    print("\n".join(summary(rows)))
    for w in usage.windows(now_s):
        print("".join(text for text, _ in usage_segments(w, now_s))[:width])
    print(plain_line([h for h, _ in COLUMNS]) + "  NOTE")
    view = select(rows, args.state, args.grep, "last active", args.retired)
    if args.state in (None, "all") and not args.grep:  # a filter shows every match
        view = layout(view, default_open(args.expand), now, args.idle_hours)
    for r in view[: args.n or None]:
        if r.get("group"):
            print(group_line(r, now, width, args.idle_hours))
        else:
            print(row_line(row_cells(r, now, args.window), r["note"], width))


def row_line(cells, note, width):
    line = plain_line(cells) + "  "
    return (line + fit(note, width - len(line)))[:width]


# ---------------------------------------------------------------- help

HELP_KEYS = [
    ("↑ ↓  j k", "move"), ("PgUp PgDn", "page up / down"), ("g  G", "top / bottom"),
    ("enter", "attach; on a section, open / close"), ("→  ←", "open / close a section"),
    ("e", "open every section / put back"), ("click", "select; on a section, open / close"),
    ("c", "copy the attach command"), ("o", "next sort order"),
    ("f", "next state filter"), ("/", "text filter (empty clears it)"), ("r", "show / hide retired guides"),
    ("+  -", "refresh slower / faster"),
    ("space", "refresh now"), ("?", "this help"), ("i", "how to use, and the title colors"), ("q  esc", "quit"),
]
HELP_USAGE = [
    ("5h  week", "plan used; │ marks time elapsed"), ("1.0x", "speed: lands on 100% at reset"),
    ("now", "speed over the last few hours"), ("→", "when you run out, or % at reset"),
]
HELP_COLUMNS = [
    ("LAST ACTIVE", "when the session last did something"), ("AGO", "time since then"),
    ("AGE", "time since it started"), ("STATE", "working, waiting (on you), review, done,"),
    ("", "stopped; review = done, worktree still open"), ("TOOL", "claude or codex"),
    ("ID", "job or session id; enter attaches/resumes"),
    ("MODEL", "main thread's model"), ("EFFORT", "its reasoning effort"), ("RUN", "subagents running now"),
    ("SPAWNED", "subagents started in total"), ("BG", "background shells, monitors, crons"),
    ("CTX", "context tokens in use"), ("CTX %", "share of window: -w, or Codex's reported size"),
    ("NAME  NOTE", "name; its question, result or step"),
    ("", "the session guide is always first"),
]
HELP_GROUPS = [
    ("▾ needs input", "waiting on you; ▾ working: open"), ("▸ review", "folded: done, worktree open"),
    ("▸ done", "finished, or done per session-guide"), ("▸ stopped", "stopped sessions"),
    ("▸ idle 1d+", "anything quiet that long (--idle-hours)"),
    ("", "a state or text filter shows rows, no sections"),
]


def help_section(title, pairs):
    return [title] + [f"  {k:<11}  {v}" for k, v in pairs]


def help_keys(latest=None):
    """The keys, with u before ? and q when there is an update to offer."""
    return HELP_KEYS[:-2] + [("u", f"update to {latest}")] + HELP_KEYS[-2:] if latest else HELP_KEYS


def help_lines(cols, latest=None):
    """The help text: keys and usage beside the columns when cols allows, else one after the other."""
    left = help_section("keys", help_keys(latest)) + [""] + help_section("usage", HELP_USAGE)
    right = help_section("columns", HELP_COLUMNS) + [""] + help_section("groups", HELP_GROUPS)
    lw = max(map(len, left))
    if lw + 4 + max(map(len, right)) + 4 <= cols:
        right += [""] * (len(left) - len(right))
        return [(a.ljust(lw) + "    " + b).rstrip() for a, b in zip(left, right)]
    return left + [""] + right


def help_box(lines, rows, cols, offset=0):
    """The help overlay on a rows x cols screen: (y, x, box lines, offset), centered and scrolled to offset."""
    width, height = min(max(map(display_width, lines)) + 4, cols), min(len(lines) + 2, rows)
    inner_w, inner_h = width - 4, height - 2
    if inner_w < 1 or inner_h < 1:
        return 0, 0, [], 0
    offset = max(0, min(offset, len(lines) - inner_h))
    more = len(lines) > inner_h
    label = "↑↓ scroll · any other key closes" if more else "any key closes"
    box = [edge("┌", "help", "┐", width)]
    box += ["│ " + line[:inner_w] + " " * max(0, inner_w - display_width(line[:inner_w])) + " │"
            for line in lines[offset:offset + inner_h]]
    box.append(edge("└", label, "┘", width))
    return (rows - height) // 2, (cols - width) // 2, box, offset


def edge(left, label, right, width):
    fill = width - 2
    text = f"─ {label} " if len(label) + 3 <= fill else ""
    return left + (text + "─" * fill)[:fill] + right


# ---------------------------------------------------------------- updates


def version_key(version):
    """'v0.10.0' -> ((0, 10), True), or None if it isn't a version. A pre-release such as 0.3.0rc1 sorts before 0.3.0."""
    m = re.match(r"v?(\d+(?:\.\d+)*)(.*)$", str(version).strip())
    if not m:
        return None
    nums = [int(n) for n in m.group(1).split(".")]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    return tuple(nums), not m.group(2)


def is_newer(latest, current):
    a, b = version_key(latest), version_key(current)
    return bool(a and b and a > b)


def check_due(cache, now):
    checked = (cache or {}).get("checked")
    return not isinstance(checked, (int, float)) or abs(now - checked) >= UPDATE_EVERY


def fetch_latest(opener=None):
    """The latest release's version on GitHub, or None on any failure (offline, rate-limited, a bad reply)."""
    import urllib.request  # only the full-screen view asks, so the status line doesn't pay for the import

    req = urllib.request.Request(UPDATE_URL, headers={"User-Agent": f"agents-top/{__version__}",
                                                      "Accept": "application/vnd.github+json"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=3) as resp:
            tag = json.loads(resp.read().decode()).get("tag_name")
    except Exception:  # runs in a background thread: never let it reach the screen
        return None
    return tag.removeprefix("v") if isinstance(tag, str) and version_key(tag) else None


def update_check(now):
    """A released version newer than this one, or None. Asks GitHub at most once a day, failures included."""
    if os.environ.get("CLAUDE_TOP_NO_UPDATE_CHECK", "") not in ("", "0"):
        return None
    cache = load_json(UPDATE_CHECK) or {}
    if check_due(cache, now):
        cache = {"checked": int(now), "latest": fetch_latest() or cache.get("latest")}
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            write_json(UPDATE_CHECK, cache)
        except OSError:
            pass
    latest = cache.get("latest")
    return latest if isinstance(latest, str) and is_newer(latest, __version__) else None


def is_checkout(d):
    """d is a git clone of agents-top, not some other repo the file was copied into."""
    try:
        return (d / ".git").exists() and PACKAGE in (d / "pyproject.toml").read_text()
    except OSError:
        return False


def has_segment(path, *names):
    parts = Path(path).parts
    return any(parts[i:i + len(names)] == names for i in range(len(parts)))


def update_command(script=__file__, prefix=sys.prefix):
    """The command that updates this install, or None when we can't tell how it was installed."""
    checkout = Path(script).resolve().parent
    if is_checkout(checkout):
        return ["git", "-C", str(checkout), "pull", "--ff-only"]
    prefix = Path(prefix)
    if (prefix / "uv-receipt.toml").exists() or has_segment(prefix, "uv", "tools"):
        return ["uv", "tool", "upgrade", PACKAGE]
    if (prefix / "pipx_metadata.json").exists() or has_segment(prefix, "pipx", "venvs"):
        return ["pipx", "upgrade", PACKAGE]
    return None


def update_hint(latest):
    return f" u update to {latest} "


def display_width(text):
    """Terminal cells for text: wide emoji take two, a variation selector none."""
    return sum(0 if c == "\ufe0f" else 2 if unicodedata.east_asian_width(c) in "WF" or c == "\U0001f39f" else 1
               for c in text)


def legend_shown():
    """True once session-guide has marked a session here: the legend means nothing to anyone else."""
    try:
        return any(GUIDES.iterdir())
    except OSError:
        return False


def footer_keys(latest):
    return (update_hint(latest) if latest else "") + KEYS


def update_prompt(cmd, latest):
    return f" update to {latest}? y/n  $ {shlex.join(cmd)}"


def manual_update(latest):
    return f" {latest} is out: pip install -U {PACKAGE}, or {RELEASES_URL}"


def restart_argv(argv=None):
    """How to start again after an update: the agents-top command we ran as, else python and this file."""
    argv = sys.argv if argv is None else argv
    if os.path.basename(argv[0]) == "agents-top" and os.path.isfile(argv[0]):
        return [os.path.abspath(argv[0])] + argv[1:]
    return [sys.executable, os.path.abspath(__file__)] + argv[1:]


def run_update(cmd, latest):
    """Run cmd in the terminal and restart into the new version. Returns only after a failure, once you press Enter."""
    print(f"updating agents-top to {latest}: {shlex.join(cmd)}", flush=True)
    try:
        code = subprocess.run(cmd).returncode
    except OSError as e:
        print(f"could not run {cmd[0]}: {e}")
        code = None
    if code == 0:
        argv = restart_argv()
        try:
            os.execv(argv[0], argv)
        except OSError as e:
            sys.exit(f"updated, but could not restart ({e}): run agents-top again")
    if code:
        print(f"the update failed (exit {code}): see the output above")
    try:
        input("press Enter to go back to agents-top, or ctrl-c to quit ")
    except EOFError:
        sys.exit(1)


def attach(command, directory=None):
    """Open the session in this terminal (`claude attach <id>`, `claude --resume <id>` or `codex resume <id>`) and
    wait until you leave it. Returns the note the list shows when it comes back: empty after a clean exit, else
    what went wrong."""
    try:
        proc = subprocess.Popen(command, cwd=directory or None)
    except OSError as e:
        if directory and not os.path.isdir(directory):
            return f" could not open session directory {directory!r} ({e.strerror or e})"
        return f" could not run `{shlex.join(command)}` ({e.strerror or e}): is {command[0]} on your PATH?"
    while True:
        try:
            code = proc.wait()
            break
        except KeyboardInterrupt:  # ctrl-c is for the session; agents-top waits for it to end
            continue
    return f" `{shlex.join(command)}` exited {code}" if code else ""


# ---------------------------------------------------------------- full screen


class Top:
    def __init__(self, scr, args):
        self.scr = scr
        self.interval = args.interval
        self.window = args.window
        self.state = args.state or "all"
        self.text = args.grep or ""
        self.retired = args.retired
        self.expanded = default_open(args.expand)
        self.idle_hours = args.idle_hours
        self.mouse = not args.no_mouse
        self.matched = 0
        self.sort = "last active"
        self.usage = Usage(args.rate_hours)
        self.rows, self.view = [], []
        self.cursor = self.top = 0
        self.message = ""
        self.help = None  # scroll offset while the help overlay is open
        self.help_page = None  # the lines the overlay shows; None is the ? help
        self.last_load = 0.0
        self.latest = None  # a newer release, once the background check finds one

    def check_update(self):
        self.latest = update_check(time.time())

    def colors(self):
        yellow = green = red = cyan = 0
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for i, c in enumerate([curses.COLOR_YELLOW, curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_CYAN], 1):
                curses.init_pair(i, c, -1)
            yellow, green, red, cyan = (curses.color_pair(i) for i in range(1, 5))
        self.c_state = {"review": cyan | curses.A_BOLD, "blocked": yellow | curses.A_BOLD, "working": green | curses.A_BOLD,
                        "stopped": red, "done": curses.A_DIM}
        self.c_level = {"ok": green, "warn": yellow | curses.A_BOLD, "hot": red | curses.A_BOLD,
                        "dim": curses.A_DIM, "bold": curses.A_BOLD, None: 0}
        self.c_head = cyan | curses.A_BOLD
        self.c_group = {"blocked": yellow | curses.A_BOLD, "working": green | curses.A_BOLD, "review": cyan | curses.A_BOLD,
                        "stopped": red | curses.A_BOLD, "idle": curses.A_DIM | curses.A_BOLD}

    def body_rows(self):
        return max(1, self.scr.getmaxyx()[0] - self.header_rows - FOOTER_ROWS)

    def reload(self, keep=True):
        """Refresh rows; keep the selected session under the cursor unless the view changed."""
        self.usage.refresh()
        self.rows = load_rows()
        self.relayout(keep)
        self.last_load = time.monotonic()

    def folding(self):
        return self.state == "all" and not self.text  # a filter shows every match

    def relayout(self, keep=True, select_id=None):
        """Rebuild the lines from the loaded rows. The cursor stays on the selected line; when that session has
        just folded into a closed group, it moves to the group's line."""
        selected = select_id or (row_identity(self.view[self.cursor]) if (keep and self.view) else None)
        rows = select(self.rows, self.state, self.text, self.sort, self.retired)
        self.matched = len(rows)
        self.view = layout(rows, self.expanded, datetime.now(timezone.utc), self.idle_hours) if self.folding() else rows
        ids = [row_identity(r) for r in self.view]
        holder = next((row_identity(g) for g in self.view if g.get("group") and selected in [row_identity(m) for m in g["rows"]]), None)
        if selected in ids:
            self.cursor = ids.index(selected)
        elif holder:
            self.cursor = ids.index(holder)
        elif keep:
            self.cursor = min(self.cursor, max(0, len(ids) - 1))
        else:
            self.cursor = self.top = 0

    def group_at(self, i):
        """The group line i belongs to: its own for a group's line, else the group line above an opened session."""
        for item in reversed(self.view[: i + 1]):
            if item.get("group"):
                return item["group"]
        return None

    def set_open(self, group, is_open):
        if group:
            self.expanded = self.expanded | {group} if is_open else self.expanded - {group}
            self.relayout(select_id=(None, f"group:{group}"))

    def click(self):
        """A mouse click selects the line under it, and opens or closes a group's line; the wheel moves."""
        try:
            _, _, y, _, bstate = curses.getmouse()
        except curses.error:
            return
        if bstate & curses.BUTTON4_PRESSED:
            self.cursor = max(0, self.cursor - 3)
        elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
            self.cursor = min(max(0, len(self.view) - 1), self.cursor + 3)
        elif bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED) and self.header_rows <= y < self.header_rows + self.body_rows():
            i = self.top + y - self.header_rows
            if i < len(self.view):
                self.cursor = i
                item = self.view[i]
                if item.get("group"):
                    self.set_open(item["group"], not item["open"])

    @property
    def header_rows(self):
        return HEADER_ROWS + legend_shown()

    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y < h and x < w:
            try:
                self.scr.addnstr(y, x, text, w - x - (1 if y == h - 1 else 0), attr)
            except curses.error:
                pass
        return x + len(text)

    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        now = datetime.now(timezone.utc)
        sessions, agents = summary(self.rows)
        head = f" agents-top {__version__} {datetime.now().strftime('%H:%M:%S')} every {self.interval:g}s │ {sessions}"
        self.put(0, 0, head.ljust(w), curses.A_REVERSE | curses.A_BOLD)
        self.put(1, 0, f" {agents}".ljust(w), curses.A_REVERSE)
        legend = self.header_rows - HEADER_ROWS
        if legend:
            self.put(2, 0, LEGEND.ljust(w), curses.A_REVERSE)
        now_s = time.time()
        for i, win in enumerate(self.usage.windows(now_s)):
            x = 0
            for text, level in usage_segments(win, now_s):
                x = self.put(2 + legend + i, x, text, self.c_level.get(level, 0))
        flt = f" sort: {self.sort}   state: {STATE_LABEL.get(self.state, self.state)}   filter: {self.text or '-'}   showing {self.matched}/{len(self.rows)}"
        folded = folded_count(self.view)
        if folded:
            flt += f"   folded: {folded} (enter/click a section, e all)"
        retired = retired_count(self.rows)
        if retired:
            flt += f"   retired guides: {'shown' if self.retired else f'{retired} hidden'} (r)"
        self.put(self.header_rows - 2, 0, flt, curses.A_DIM)
        self.put(self.header_rows - 1, 0, plain_line([hd for hd, _ in COLUMNS]) + "  NOTE", self.c_head)

        body = self.body_rows()
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + body:
            self.top = self.cursor - body + 1
        for i, r in enumerate(self.view[self.top:self.top + body]):
            y = self.header_rows + i
            sel = self.top + i == self.cursor
            if r.get("group"):
                self.put(y, 0, group_line(r, now, w, self.idle_hours).ljust(w), curses.A_REVERSE if sel else curses.A_BOLD)
                if not sel:
                    self.put(y, 3, group_label(r["group"], self.idle_hours), self.c_group.get(r["group"], curses.A_BOLD))
                continue
            cells = row_cells(r, now, self.window)
            self.put(y, 0, row_line(cells, r["note"], w).ljust(w), curses.A_REVERSE if sel else 0)
            if sel:
                continue
            self.put(y, COL_X["STATE"], cells[COL["STATE"]][:8], self.c_state.get(r["state"], 0))
            if r["agents"]["running"]:
                self.put(y, COL_X["RUN"], cells[COL["RUN"]], self.c_state["working"])
            if r["bg"]:
                self.put(y, COL_X["BG"], cells[COL["BG"]], self.c_state["blocked"])
            pct = cells[COL["CTX %"]]
            if pct is not None:
                level = "ok" if pct < 0.5 else "warn" if pct < 0.8 else "hot"
                self.put(y, COL_X["CTX %"], bar(pct, 6), self.c_level[level])

        if self.view:
            self.put(h - 2, 0, fit(selected_detail(self.view[self.cursor]), w), curses.A_DIM)
        self.put(h - 1, 0, (self.message or footer_keys(self.latest)).ljust(w), curses.A_REVERSE)
        if self.latest and not self.message:
            self.put(h - 1, 0, update_hint(self.latest), curses.A_REVERSE | curses.A_BOLD)

    def draw_help(self):
        h, w = self.scr.getmaxyx()
        y, x, box, self.help = help_box(self.help_page or help_lines(w, self.latest), h, w, self.help)
        for i, line in enumerate(box):
            if i in (0, len(box) - 1):
                self.put(y + i, x, line, self.c_head)
                continue
            self.put(y + i, x, line)
            self.put(y + i, x, "│", self.c_head)
            self.put(y + i, x + display_width(line) - 1, "│", self.c_head)
            for m in re.finditer(r"(?:^│ |    )(keys|usage|columns|groups|how to use|with session-guide|the title colors)\b",
                                 line):
                self.put(y + i, x + m.start(1), m.group(1), curses.A_BOLD)

    def help_key(self, key):
        """Scroll keys move the open help; any other key closes it."""
        if key in (curses.KEY_DOWN, ord("j")):
            self.help += 1
        elif key in (curses.KEY_UP, ord("k")):
            self.help -= 1
        elif key == curses.KEY_NPAGE:
            self.help += self.body_rows()
        elif key == curses.KEY_PPAGE:
            self.help -= self.body_rows()
        elif key != curses.KEY_RESIZE:
            self.help = None

    def prompt(self, label):
        h, w = self.scr.getmaxyx()
        curses.echo()
        curses.curs_set(1)
        self.put(h - 1, 0, label.ljust(w), curses.A_REVERSE)
        self.scr.timeout(-1)
        self.scr.attron(curses.A_REVERSE)  # what you type stays on the footer bar
        try:
            return self.scr.getstr(h - 1, len(label), 80).decode(errors="ignore").strip()
        finally:
            self.scr.attroff(curses.A_REVERSE)
            curses.noecho()
            curses.curs_set(0)
            self.scr.timeout(200)

    def confirm(self, label):
        """True if the key pressed at label, on the footer line, is y."""
        h, w = self.scr.getmaxyx()
        self.put(h - 1, 0, label.ljust(w), curses.A_REVERSE)
        self.scr.timeout(-1)
        try:
            return self.scr.getch() in (ord("y"), ord("Y"))
        finally:
            self.scr.timeout(200)

    def run(self):
        curses.curs_set(0)
        self.colors()
        if self.mouse:
            curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED | curses.BUTTON4_PRESSED
                             | getattr(curses, "BUTTON5_PRESSED", 0))
            curses.mouseinterval(0)  # a press acts at once, without waiting to see if it is a click
        self.reload()
        threading.Thread(target=self.check_update, daemon=True).start()
        self.scr.timeout(200)  # redraw the clock and check the refresh interval 5x a second
        while True:
            self.draw()
            if self.help is not None:
                self.draw_help()
            self.scr.refresh()
            key = self.scr.getch()
            if key != -1:
                self.message = ""
            body, last_idx = self.body_rows(), max(0, len(self.view) - 1)
            if key != -1 and self.help is not None:
                self.help_key(key)
            elif key in (ord("q"), 27):
                return "quit", None
            elif key in (curses.KEY_DOWN, ord("j")):
                self.cursor = min(last_idx, self.cursor + 1)
            elif key in (curses.KEY_UP, ord("k")):
                self.cursor = max(0, self.cursor - 1)
            elif key == curses.KEY_NPAGE:
                self.cursor = min(last_idx, self.cursor + body)
            elif key == curses.KEY_PPAGE:
                self.cursor = max(0, self.cursor - body)
            elif key == ord("g"):
                self.cursor = 0
            elif key == ord("G"):
                self.cursor = last_idx
            elif key == curses.KEY_MOUSE:
                self.click()
            elif key in (10, 13, curses.KEY_ENTER) and self.view:
                item = self.view[self.cursor]
                if not item.get("group"):
                    return "attach", (attach_command(item), attach_directory(item))
                self.set_open(item["group"], not item["open"])
            elif key in (curses.KEY_RIGHT, ord("l")) and self.view:
                self.set_open(self.view[self.cursor].get("group"), True)
            elif key in (curses.KEY_LEFT, ord("h")) and self.view:
                self.set_open(self.group_at(self.cursor), False)
            elif key == ord("e"):
                shown = {item["group"] for item in self.view if item.get("group")}
                self.expanded = default_open() if shown and shown <= self.expanded else set(GROUPS)
                self.relayout()
            elif key == ord("c") and self.view and self.view[self.cursor].get("group"):
                self.message = " a section line: enter or click opens it"
            elif key == ord("c") and self.view:
                row = self.view[self.cursor]
                cmd = shlex.join(attach_command(row))
                if attach_directory(row):
                    cmd = f"cd {shlex.quote(attach_directory(row))} && {cmd}"
                self.message = f" copied: {cmd}" if copy_to_clipboard(cmd) else f" {cmd}"
            elif key == ord("o"):
                self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)]
                self.reload(keep=False)
            elif key == ord("f"):
                self.state = STATES[(STATES.index(self.state) + 1) % len(STATES)]
                self.reload(keep=False)
            elif key == ord("/"):
                self.text = self.prompt(" filter: ")
                self.reload(keep=False)
            elif key == ord("r"):
                self.retired = not self.retired
                self.reload()
            elif key in (ord("+"), ord("=")):
                self.interval = min(60, self.interval * 2)
            elif key == ord("-"):
                self.interval = max(0.5, self.interval / 2)
            elif key == ord(" "):
                self.reload()
            elif key == ord("?"):
                self.help_page = None
                self.help = 0
            elif key == ord("i"):
                self.help_page = HOW_TO_USE
                self.help = 0
            elif key == ord("u") and self.latest:
                cmd = update_command()
                if not cmd:
                    self.message = manual_update(self.latest)
                elif self.confirm(update_prompt(cmd, self.latest)):
                    return "update", (cmd, self.latest)
            if time.monotonic() - self.last_load >= self.interval:
                self.reload()


# ---------------------------------------------------------------- init


SETTINGS = CLAUDE_DIR / "settings.json"
WRAPPER = DATA_DIR / "statusline.sh"
INIT_STATE = DATA_DIR / "init.json"  # the statusLine we installed and the one it replaced, for --undo


def own_command():
    """How the status line should call us: an absolute path, since it runs outside your login shell."""
    exe = sys.argv[0] if os.path.basename(sys.argv[0]) == "agents-top" else shutil.which("agents-top")
    if exe and os.path.isfile(exe):
        return shlex.quote(os.path.abspath(exe))
    return f"{shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))}"


def is_ours(status_line):
    command = str((status_line or {}).get("command") or "")
    return command == str(WRAPPER) or bool(re.search(r"(?:agents-top|claude[-_]top(?:\.py)?)'? statusline$", command))


def write_json(path, data):
    tmp = path.with_name(f"{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def write_settings(settings, dry_run):
    """Replace settings.json atomically, keeping a timestamped copy of the old one next to it."""
    backup = SETTINGS.with_name(f"{SETTINGS.name}.{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak")
    if dry_run:
        print(f"dry run: would back up {SETTINGS} to {backup.name} and write:")
        print(json.dumps({"statusLine": settings.get("statusLine")}, indent=2))
        return
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, backup)
        print(f"backed up {SETTINGS} to {backup.name}")
    write_json(SETTINGS, settings)
    print(f"wrote {SETTINGS}")


def wrapper_script(original_command):
    return (
        "#!/bin/sh\n"
        "# Written by `agents-top init --wrap`: records your plan limits for agents-top, then runs the status line\n"
        "# you had before with the same input. `agents-top init --undo` puts the original back.\n"
        'input=$(cat)\n'
        f"printf '%s' \"$input\" | {own_command()} record\n"
        f"printf '%s' \"$input\" | {original_command}\n"
    )


def checklist():
    encoding = (sys.stdout.encoding or "").lower()
    print("\ncheck:")
    print(f"  python {sys.version_info.major}.{sys.version_info.minor}: " + ("ok" if sys.version_info >= (3, 9) else "needs 3.9+"))
    print("  claude on PATH: " + ("ok" if shutil.which("claude") else "not found: live states (working/review) need `claude agents`"))
    print("  UTF-8 terminal: " + ("ok" if "utf" in encoding else f"stdout is {encoding or 'unknown'}: the bars need a UTF-8 locale"))
    print("  the usage bars fill in after Claude Code next refreshes its status line (send it a message)")
    print("\nnext: agents-top")


def init_main(argv):
    ap = argparse.ArgumentParser(prog="agents-top init", description="Set agents-top as your Claude Code status line.")
    ap.add_argument("--wrap", action="store_true", help="keep an existing status line: run it after recording the limits")
    ap.add_argument("--dry-run", action="store_true", help="show the change without writing anything")
    ap.add_argument("--undo", action="store_true", help="put back the status line you had before init")
    args = ap.parse_args(argv)

    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text())
        except ValueError as e:
            sys.exit(f"{SETTINGS} is not valid JSON ({e}); fix it first, nothing was changed")
        if not isinstance(settings, dict):
            sys.exit(f"{SETTINGS} is not a JSON object; nothing was changed")
    else:
        settings = {}
    current = settings.get("statusLine")
    if current is not None and not isinstance(current, dict):
        sys.exit(f"settings.json has an unexpected statusLine ({json.dumps(current)}); nothing was changed")
    previous = (load_json(INIT_STATE) or {}).get("previous") if is_ours(current) else current

    if args.undo:
        if not is_ours(current):
            sys.exit("the status line in settings.json isn't agents-top's; nothing to undo")
        if previous:
            settings["statusLine"] = previous
        else:
            settings.pop("statusLine", None)
        write_settings(settings, args.dry_run)
        if not args.dry_run:
            for p in (INIT_STATE, WRAPPER):
                if p.exists():
                    p.unlink()
            print(f"restored {json.dumps(previous)}" if previous else "removed agents-top's status line")
        return

    status_line = {"type": "command", "command": f"{own_command()} statusline"}
    if current and not is_ours(current):
        if not args.wrap or current.get("type") != "command" or not current.get("command"):
            print(f"settings.json already has a status line: {json.dumps(current)}")
            print("agents-top won't replace it. Either:")
            print("  agents-top init --wrap     record the limits, then run your status line unchanged, or")
            print('  add `echo "$input" | agents-top record` to your own status-line script.')
            sys.exit(1)
        status_line = {**current, "command": str(WRAPPER)}
        script = wrapper_script(current["command"])
        if args.dry_run:
            print(f"dry run: would write {WRAPPER}:\n{script}")
        else:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            WRAPPER.write_text(script)
            WRAPPER.chmod(0o755)
            print(f"wrote {WRAPPER}")
    elif current == status_line or (is_ours(current) and current.get("command") == str(WRAPPER) and WRAPPER.exists()):
        print(f"already set up: {json.dumps(current)}")
        checklist()
        return

    settings["statusLine"] = status_line
    write_settings(settings, args.dry_run)
    if not args.dry_run:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        write_json(INIT_STATE, {"previous": previous, "statusLine": status_line})
    print(f"status line: {status_line['command']}")
    checklist()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        return init_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("record", "statusline"):
        if sys.stdin.isatty():
            sys.exit(f"agents-top {sys.argv[1]} reads Claude Code's status-line JSON on stdin; see the README")
        try:
            status_input = json.load(sys.stdin)
        except ValueError:
            status_input = None
        record(status_input)
        if sys.argv[1] == "statusline":
            usage = Usage()
            usage.refresh()
            print(statusline_text(usage, time.time()))
        return

    ap = argparse.ArgumentParser(prog="agents-top", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"agents-top {__version__}")
    ap.add_argument("-i", "--interval", type=float, default=2.0, help="refresh seconds (default 2)")
    ap.add_argument("-s", "--state", choices=STATES[1:] + list(STATE_ALIASES),
                    help="only sessions in this state (blocked or waiting = needs your input)")
    ap.add_argument("-g", "--grep", help="text filter: name, provider, ask, pending question, status or folder")
    ap.add_argument("-w", "--window", type=int, default=DEFAULT_WINDOW,
                    help=f"Claude context window size for CTX %% (default {DEFAULT_WINDOW:,} tokens; Codex uses its log)")
    ap.add_argument("--rate-hours", type=float, default=3.0, help="hours of history behind the 'now' speed (default 3)")
    ap.add_argument("--retired", action="store_true", help="show retired session guides (hidden by default; r toggles)")
    ap.add_argument("--expand", action="store_true", help="start with every section open (review, done, stopped, idle fold)")
    ap.add_argument("--idle-hours", type=float, default=IDLE_HOURS,
                    help=f"fold a session quiet this many hours into the idle group (default {IDLE_HOURS:g})")
    ap.add_argument("--no-mouse", action="store_true", help="leave the mouse to the terminal (text selection)")
    ap.add_argument("--once", action="store_true", help="print once and exit")
    ap.add_argument("-n", type=int, default=30, help="with --once: rows to print (0 = all)")
    args = ap.parse_args()
    if args.window <= 0:
        ap.error("--window must be a positive number of tokens")
    if args.idle_hours <= 0:
        ap.error("--idle-hours must be a positive number of hours")
    args.state = STATE_ALIASES.get(args.state, args.state)

    if args.once or not sys.stdout.isatty() or curses is None:
        try:
            print_once(args)
            sys.stdout.flush()
        except BrokenPipeError:  # e.g. piped into head
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return
    top = None

    def screen(scr):
        nonlocal top  # one Top for the whole run, so the list comes back as you left it
        if top is None:
            top = Top(scr, args)
        top.scr = scr
        return top.run()

    while True:
        try:
            action, value = curses.wrapper(screen)
            if action == "attach":
                top.message = attach(*value)
            elif action == "update":
                run_update(*value)  # returns only if the update failed and you went back
            else:
                return
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
