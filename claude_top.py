#!/usr/bin/env python3
"""claude-top: a live, top-style view of your Claude Code background sessions and plan usage.

  claude-top                  full screen, refreshes every 2s
  claude-top -i 5             refresh every 5s
  claude-top -s blocked       start filtered to sessions waiting for input
  claude-top -g billing       start with a text filter
  claude-top --once           print once and exit
  claude-top statusline       status-line command: records plan limits, prints a short usage line
  claude-top record           same, but prints nothing (pipe your own status line's input into it)

Keys: up/down j/k move, PgUp/PgDn page, g/G top/bottom, enter attach, c copy the attach command,
o sort, f state filter, / text filter, +/- refresh speed, space refresh now, q quit.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import curses
except ImportError:  # Windows: only --once works
    curses = None

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
JOBS = CLAUDE_DIR / "jobs"
PROJECTS = CLAUDE_DIR / "projects"
DATA_DIR = CLAUDE_DIR / "claude-top"
RATE_LIMITS = Path(os.environ.get("CLAUDE_TOP_RATE_LIMITS") or DATA_DIR / "rate_limits.json")
HISTORY = DATA_DIR / "usage-history.jsonl"
HISTORY_KEEP_DAYS = 8
DESKTOP_HISTORY = Path.home() / "Library" / "Application Support" / "Claude" / "plan-usage-history.json"

TAIL_BYTES = 512 * 1024
MAX_TAIL_BYTES = 2 * 1024 * 1024
DEFAULT_WINDOW = int(os.environ.get("CLAUDE_TOP_CONTEXT_WINDOW") or 1_000_000)
HEADER_ROWS = 6
FOOTER_ROWS = 2

STATES = ["all", "review", "blocked", "working", "done", "stopped"]
SORTS = ["last active", "created", "context", "agents", "name"]
STATE_LABEL = {"review": "review", "blocked": "needs-in", "working": "working", "done": "done", "stopped": "stopped"}
LIMITS = [  # rate_limits key, label, window seconds, short key
    ("five_hour", "5h", 5 * 3600, "fh"),
    ("seven_day", "week", 7 * 86400, "sd"),
]

_transcript_cache = {}
_path_cache = {}
_agents_cache = {}


# ---------------------------------------------------------------- helpers


def parse_ts(value):
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def clean(text):
    """Session text is shown verbatim: drop control characters so it can't drive the terminal."""
    return "".join(c if c.isprintable() else " " for c in str(text))


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
    if not model:
        return "-"
    return model.replace("claude-", "").replace("-", " ", 1).replace("-", ".")


def fmt_tokens(n):
    if n is None:
        return "-"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n // 1000}k" if n >= 1000 else str(n)


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


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
    Usage(3).append_history(snap)
    return snap


class Usage:
    """Plan limits plus a sample history, so we can tell the recent burn rate."""

    def __init__(self, rate_hours):
        self.rate_hours = rate_hours
        self.samples = []  # (epoch seconds, {"fh": pct, "sd": pct})
        self.snap = None
        h = load_json(DESKTOP_HISTORY) or {}  # Claude desktop app keeps one on macOS
        for s in h.get("samples") or []:
            if isinstance(s, dict) and isinstance(s.get("u"), dict) and s.get("t"):
                self.samples.append((s["t"] / 1000.0, s["u"]))
        try:
            for line in HISTORY.read_text().splitlines():
                s = json.loads(line)
                self.samples.append((s["t"], s["u"]))
        except (OSError, ValueError, KeyError):
            pass
        self.samples.sort(key=lambda s: s[0])

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
        kept = []
        for line in HISTORY.read_text().splitlines():
            try:
                if json.loads(line)["t"] >= keep_after:
                    kept.append(line)
            except (ValueError, KeyError, TypeError):
                pass
        tmp = HISTORY.with_name(HISTORY.name + ".tmp")
        tmp.write_text("\n".join(kept) + "\n")
        os.replace(tmp, HISTORY)

    def refresh(self):
        snap = load_json(RATE_LIMITS)
        if snap and isinstance(snap.get("rate_limits"), dict):
            self.snap = snap

    def windows(self, now):
        out = []
        limits = (self.snap or {}).get("rate_limits") or {}
        for key, label, window, hk in LIMITS:
            lim = limits.get(key) or {}
            used, resets = lim.get("used_percentage"), lim.get("resets_at")
            if used is None or not resets:
                out.append({"label": label, "used": None})
                continue
            used, resets = float(used), float(resets)
            while resets <= now:  # snapshot older than its reset
                resets += window
            start = resets - window
            elapsed = min(max((now - start) / window, 0.001), 1.0)
            avg_rate = used / ((now - start) / 3600) if now > start else 0.0
            recent_rate = self.recent_rate(hk, used, now, start, window)
            rate = recent_rate if recent_rate is not None else avg_rate
            one_x = 100.0 / (window / 3600)  # % per hour that lands on exactly 100% at reset
            w = {
                "label": label, "used": used, "resets": resets, "elapsed": elapsed,
                "pace": (used / 100.0) / elapsed,
                "recent_pace": recent_rate / one_x if recent_rate is not None else None,
                "stale": now - float(self.snap.get("ts") or 0),
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
        return [(f" {w['label']:<5}", "bold"), ("no data yet: set `claude-top statusline` as your status line", "dim")]
    level = pace_level(w["pace"])
    reset_local = datetime.fromtimestamp(w["resets"]).strftime("%a %H:%M")
    segs = [
        (f" {w['label']:<5}", "bold"),
        (usage_bar(w["used"], w["elapsed"], 20), level),
        (f" {w['used']:>3.0f}% used · {w['elapsed'] * 100:>2.0f}% time · reset {reset_local} ({span(w['resets'] - now)}) · ", None),
        (f"{w['pace']:.1f}x ", level),
        (pace_gauge(w["pace"], 8), level),
    ]
    if w["recent_pace"] is not None:
        segs.append((f" now {w['recent_pace']:.1f}x", pace_level(w["recent_pace"])))
    if w["runs_out"]:
        out = datetime.fromtimestamp(w["runs_out"]).strftime("%a %H:%M")
        segs.append((f" · ▶ out {out} ({span(w['resets'] - w['runs_out'])} early)", "hot"))
    else:
        segs.append((f" · ▶ ~{w['at_reset']:.0f}% at reset", "ok"))
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
            text += " ▶ out " + datetime.fromtimestamp(w["runs_out"]).strftime("%a %H:%M")
        parts.append(text)
    return " · ".join(parts)


# ---------------------------------------------------------------- sessions


def find_transcript(state):
    sid = state.get("sessionId") or ""
    path = state.get("linkScanPath")
    if path and os.path.exists(path):
        return path
    if sid not in _path_cache:
        hits = list(PROJECTS.glob(f"*/{sid}.jsonl")) if sid else []
        _path_cache[sid] = str(hits[0]) if hits else None
    return _path_cache[sid]


def complete(info):
    return info["model"] and info["effort"] and info["ctx"] is not None and info["activity"] is not None


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
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
            return block["text"].strip().splitlines()[0][:200]
    return ""


def scan_lines(lines, info):
    """Walk transcript lines newest-first until model, effort and context are known."""
    for raw in reversed(lines):
        if complete(info):
            return
        if b'"api-request"' not in raw and b'"assistant"' not in raw:
            continue
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        kind = e.get("type")
        if kind == "api-request" and not info["effort"]:
            if e.get("querySource") not in (None, "repl_main_thread"):
                continue
            params = e.get("params") or {}
            info["model"] = info["model"] or params.get("model")
            info["effort"] = (params.get("output_config") or {}).get("effort")
        elif kind == "assistant" and not e.get("isSidechain"):
            msg = e.get("message") or {}
            if info["activity"] is None:
                info["activity"] = activity_of(msg) or None
            if info["ctx"] is not None:
                continue
            usage = msg.get("usage")
            if not usage:
                continue
            info["ctx"] = sum(
                usage.get(k) or 0
                for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
            )
            info["model"] = info["model"] or msg.get("model")
            info["effort"] = info["effort"] or e.get("perTurnEffort")


def transcript_info(path):
    info = {"model": None, "effort": None, "ctx": None, "activity": None, "mtime": None}
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
    size = TAIL_BYTES
    try:
        with open(path, "rb") as fh:
            while True:  # read only the tail; widen it if the answer isn't there yet
                fh.seek(max(0, st.st_size - size))
                lines = fh.read().split(b"\n")
                if st.st_size > size:
                    lines = lines[1:]
                scan_lines(lines, info)
                if complete(info) or size >= min(st.st_size, MAX_TAIL_BYTES):
                    break
                size *= 4
    except OSError:
        return info
    _transcript_cache[path] = ((st.st_size, st.st_mtime), info)
    return info


def agents_info(transcript, live):
    """Subagents a session started: total, running now, and the model mix."""
    info = {"total": 0, "running": 0, "models": {}, "active": []}
    if not transcript:
        return info
    d = Path(transcript[: -len(".jsonl")]) / "subagents"
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
        if live and not meta.get("finished") and recently_written(meta_file.with_name(meta_file.name[: -len(".meta.json")] + ".jsonl")):
            info["running"] += 1
            info["active"].append(clean(meta.get("description") or meta.get("agentType") or meta_file.stem))
    _agents_cache[str(d)] = (mtime, info)
    return info


def recently_written(path, minutes=15):
    """A subagent's meta can miss its finished flag (crash, kill); trust a transcript that is still growing."""
    try:
        return time.time() - path.stat().st_mtime < minutes * 60
    except OSError:
        return False


def live_sessions():
    """Live state per session from `claude agents --json --all`; state.json lags it. {} if unavailable."""
    if os.environ.get("CLAUDE_TOP_LIVE") == "0" or not shutil.which("claude"):
        return {}
    try:
        out = subprocess.run(["claude", "agents", "--json", "--all"], capture_output=True, text=True, timeout=5).stdout
        return {x["id"]: x for x in json.loads(out) if isinstance(x, dict) and x.get("id")}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return {}


def load_rows():
    rows = []
    live_list = live_sessions()
    states = {p.parent.name: p for p in JOBS.glob("*/state.json")}
    for job_id in set(states) | {i for i, x in live_list.items() if x.get("kind") != "background"}:
        state_file = states.get(job_id)
        live_entry = live_list.get(job_id) or {}
        d = (load_json(state_file) if state_file else None) or {}
        if not d and not live_entry:
            continue
        if live_entry:  # the agents view's state and name are current; state.json catches up later
            state = live_entry.get("state") or d.get("state")
            worktree = d.get("worktreePath")
            if state == "done" and live_entry.get("pid") and worktree and os.path.isdir(worktree):
                state = "review"  # finished, still open, with a worktree to look at: "Ready for review"
            d = {**d, "state": state, "name": live_entry.get("name") or d.get("name"),
                 "sessionId": d.get("sessionId") or live_entry.get("sessionId"), "cwd": d.get("cwd") or live_entry.get("cwd")}
            if not d.get("createdAt") and str(live_entry.get("startedAt") or "").isdigit():
                d["createdAt"] = datetime.fromtimestamp(int(live_entry["startedAt"]) / 1000, tz=timezone.utc).isoformat()
        path = find_transcript(d)
        t = transcript_info(path)
        live = d.get("state") in ("working", "blocked", "review")  # done/stopped keep a stale inFlight snapshot
        a = agents_info(path, live)
        flight = (d.get("inFlight") or {}) if live else {}
        kinds = flight.get("kinds") or []
        if "local_agent" in kinds and not a["running"]:
            a = {**a, "running": 1}  # the session reports a helper before its meta file does
        watchers = (flight.get("drainableMonitors") or 0) if "artifact_watch" in kinds else 0
        background = max(0, (flight.get("tasks") or 0) - watchers - a["running"])
        bg_kinds = [k for k in kinds if k not in ("artifact_watch", "local_agent")]
        updated = parse_ts(d.get("updatedAt"))
        if not updated and state_file:
            try:
                updated = datetime.fromtimestamp(state_file.stat().st_mtime, tz=timezone.utc)
            except OSError:  # session removed while we were reading
                continue
        if not (updated or t["mtime"]):
            continue
        last = max(x for x in (updated, t["mtime"]) if x)
        result = (d.get("output") or {}).get("result") if isinstance(d.get("output"), dict) else None
        note = d.get("needs") or (result if d.get("state") in ("done", "review") else None) or d.get("detail") or ""
        stale_note = updated is None or (t["mtime"] and (t["mtime"] - updated).total_seconds() > 5)
        if d.get("state") == "working" and t["activity"] and stale_note:
            note = t["activity"]  # state.json still holds the note from before this turn
        rows.append({
            "id": job_id,
            "kind": live_entry.get("kind") or "background",
            "state": d.get("state") or "?",
            "name": clean(d.get("name") or d.get("intent") or job_id),
            "note": clean(note),
            "search": " ".join(str(d.get(k) or "") for k in ("name", "intent", "needs", "detail", "cwd")).lower(),
            "cwd": clean(d.get("cwd") or ""),
            "session": d.get("resumeSessionId") or d.get("sessionId"),
            "last": last,
            "created": parse_ts(d.get("createdAt")) or last,
            "model": t["model"],
            "effort": t["effort"],
            "ctx": t["ctx"],
            "agents": a,
            "bg": background if bg_kinds else 0,
            "bg_kinds": bg_kinds,
        })
    return rows


def select(rows, state, text, sort):
    out = [r for r in rows if state in (None, "all") or r["state"] == state]
    if text:
        out = [r for r in out if text.lower() in r["search"] or text.lower() in r["id"]]
    key = {
        "last active": lambda r: r["last"],
        "created": lambda r: r["created"],
        "context": lambda r: r["ctx"] or 0,
        "agents": lambda r: (r["agents"]["running"], r["agents"]["total"]),
        "name": lambda r: r["name"].lower(),
    }[sort]
    return sorted(out, key=key, reverse=sort != "name")


COLUMNS = [
    ("LAST ACTIVE", 12), ("AGO", 4), ("AGE", 4), ("STATE", 8), ("ID", 8), ("MODEL", 10), ("EFFORT", 6),
    ("RUN", 3), ("SPAWNED", 7), ("BG", 3), ("CTX", 6), ("CTX %", 10), ("NAME", 34),
]
COL_X = {h: sum(w + 2 for _, w in COLUMNS[:i]) for i, (h, _) in enumerate(COLUMNS)}


def row_cells(r, now, window):
    a = r["agents"]
    return [
        r["last"].astimezone().strftime("%b %d %H:%M"),
        ago(r["last"], now),
        ago(r["created"], now),
        STATE_LABEL.get(r["state"], r["state"]),
        r["id"],
        short_model(r["model"]),
        r["effort"] or "-",
        str(a["running"]) if a["running"] else "-",
        str(a["total"]) if a["total"] else "-",
        str(r["bg"]) if r["bg"] else "-",
        fmt_tokens(r["ctx"]),
        r["ctx"] / window if r["ctx"] is not None else None,
        r["name"],
    ]


def bar(pct, width):
    if pct is None:
        return " " * width
    filled = min(width, round(pct * width))
    return "█" * filled + "·" * (width - filled)


def plain_line(cells):
    parts = []
    for (_, w), cell in zip(COLUMNS, cells):
        if isinstance(cell, float) or cell is None:
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
    a = r["agents"]
    parts = [f" {r['id']}  {r['cwd']}  session {r['session']}"]
    if a["total"]:
        models = ", ".join(f"{m} {n}" for m, n in sorted(a["models"].items(), key=lambda kv: -kv[1]))
        parts.append(f"subagents {a['total']} ({models})")
        if a["active"]:
            parts.append("running: " + "; ".join(a["active"]))
    if r["bg_kinds"]:
        parts.append("background: " + ", ".join(k.replace("local_", "") for k in r["bg_kinds"]))
    return "   ".join(parts)


def print_once(args):
    now = datetime.now(timezone.utc)
    width = shutil.get_terminal_size((200, 20)).columns
    usage = Usage(args.rate_hours)
    usage.refresh()
    rows = load_rows()
    print("\n".join(summary(rows)))
    for w in usage.windows(time.time()):
        print("".join(t for t, _ in usage_segments(w, time.time()))[:width])
    print(plain_line([h for h, _ in COLUMNS]) + "  NOTE")
    for i, r in enumerate(select(rows, args.state, args.grep, "last active")):
        if args.n and i >= args.n:
            break
        print((plain_line(row_cells(r, now, args.window)) + "  " + r["note"])[:width])


# ---------------------------------------------------------------- full screen


class Top:
    def __init__(self, scr, args):
        self.scr = scr
        self.interval = args.interval
        self.window = args.window
        self.state = args.state or "all"
        self.text = args.grep or ""
        self.sort = "last active"
        self.usage = Usage(args.rate_hours)
        self.rows, self.view = [], []
        self.cursor = self.top = 0
        self.message = ""
        self.last_load = 0.0

    def colors(self):
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

    def body_rows(self):
        return max(1, self.scr.getmaxyx()[0] - HEADER_ROWS - FOOTER_ROWS)

    def reload(self, keep=True):
        """Refresh rows; keep the selected session under the cursor unless the view changed."""
        selected = self.view[self.cursor]["id"] if (keep and self.view) else None
        self.usage.refresh()
        self.rows = load_rows()
        self.view = select(self.rows, self.state, self.text, self.sort)
        ids = [r["id"] for r in self.view]
        if selected in ids:
            self.cursor = ids.index(selected)
        elif keep:
            self.cursor = min(self.cursor, max(0, len(ids) - 1))
        else:
            self.cursor = self.top = 0
        self.last_load = time.monotonic()

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
        head = f" claude-top {datetime.now().strftime('%H:%M:%S')} every {self.interval:g}s │ {sessions}"
        self.put(0, 0, head.ljust(w), curses.A_REVERSE | curses.A_BOLD)
        self.put(1, 0, f" {agents}".ljust(w), curses.A_REVERSE)
        for i, win in enumerate(self.usage.windows(time.time())):
            x = 0
            for text, level in usage_segments(win, time.time()):
                x = self.put(2 + i, x, text, self.c_level.get(level, 0))
        flt = f" sort: {self.sort}   state: {self.state}   filter: {self.text or '-'}   showing {len(self.view)}/{len(self.rows)}"
        self.put(HEADER_ROWS - 2, 0, flt, curses.A_DIM)
        self.put(HEADER_ROWS - 1, 0, plain_line([hd for hd, _ in COLUMNS]) + "  NOTE", self.c_head)

        body = self.body_rows()
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + body:
            self.top = self.cursor - body + 1
        for i, r in enumerate(self.view[self.top:self.top + body]):
            y = HEADER_ROWS + i
            cells = row_cells(r, now, self.window)
            sel = self.top + i == self.cursor
            self.put(y, 0, (plain_line(cells) + "  " + r["note"]).ljust(w),
                     curses.A_REVERSE if sel else 0)
            if sel:
                continue
            self.put(y, COL_X["STATE"], cells[3][:8], self.c_state.get(r["state"], 0))
            if r["agents"]["running"]:
                self.put(y, COL_X["RUN"], cells[7], self.c_state["working"])
            if r["bg"]:
                self.put(y, COL_X["BG"], cells[9], self.c_state["blocked"])
            pct = cells[11]
            if pct is not None:
                level = "ok" if pct < 0.5 else "warn" if pct < 0.8 else "hot"
                self.put(y, COL_X["CTX %"], bar(pct, 6), self.c_level[level])

        if self.view:
            self.put(h - 2, 0, selected_detail(self.view[self.cursor])[:w], curses.A_DIM)
        keys = " enter attach  c copy  o sort  f state  / filter  +/- speed  q quit"
        self.put(h - 1, 0, (self.message or keys).ljust(w), curses.A_REVERSE)
        self.scr.refresh()

    def prompt(self, label):
        h, w = self.scr.getmaxyx()
        curses.echo()
        curses.curs_set(1)
        self.put(h - 1, 0, label.ljust(w), curses.A_REVERSE)
        self.scr.timeout(-1)
        try:
            return self.scr.getstr(h - 1, len(label), 80).decode(errors="ignore").strip()
        finally:
            curses.noecho()
            curses.curs_set(0)

    def run(self):
        curses.curs_set(0)
        self.colors()
        self.reload()
        while True:
            self.draw()
            self.scr.timeout(200)
            key = self.scr.getch()
            if key != -1:
                self.message = ""
            body, last_idx = self.body_rows(), max(0, len(self.view) - 1)
            if key in (ord("q"), 27):
                return None
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
            elif key in (10, 13, curses.KEY_ENTER) and self.view:
                return self.view[self.cursor]["id"]
            elif key == ord("c") and self.view:
                cmd = f"claude attach {self.view[self.cursor]['id']}"
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
            elif key in (ord("+"), ord("=")):
                self.interval = min(60, self.interval * 2)
            elif key == ord("-"):
                self.interval = max(0.5, self.interval / 2)
            elif key == ord(" "):
                self.reload()
            if time.monotonic() - self.last_load >= self.interval:
                self.reload()


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("record", "statusline"):
        if sys.stdin.isatty():
            sys.exit(f"claude-top {sys.argv[1]} reads Claude Code's status-line JSON on stdin; see the README")
        try:
            status_input = json.load(sys.stdin)
        except ValueError:
            status_input = None
        record(status_input)
        if sys.argv[1] == "statusline":
            usage = Usage(3)
            usage.refresh()
            print(statusline_text(usage, time.time()))
        return

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--interval", type=float, default=2.0, help="refresh seconds (default 2)")
    ap.add_argument("-s", "--state", choices=STATES[1:], help="only sessions in this state (blocked = needs input)")
    ap.add_argument("-g", "--grep", help="text filter: name, ask, pending question, status or folder")
    ap.add_argument("-w", "--window", type=int, default=DEFAULT_WINDOW,
                    help=f"context window size for the CTX %% column (default {DEFAULT_WINDOW:,} tokens)")
    ap.add_argument("--rate-hours", type=float, default=3.0, help="hours of history behind the 'now' speed (default 3)")
    ap.add_argument("--once", action="store_true", help="print once and exit")
    ap.add_argument("-n", type=int, default=30, help="with --once: rows to print (0 = all)")
    args = ap.parse_args()
    if args.window <= 0:
        ap.error("--window must be a positive number of tokens")

    if args.once or not sys.stdout.isatty() or curses is None:
        try:
            print_once(args)
            sys.stdout.flush()
        except BrokenPipeError:  # e.g. piped into head
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return
    target = curses.wrapper(lambda scr: Top(scr, args).run())
    if target:
        os.execvp("claude", ["claude", "attach", target])


if __name__ == "__main__":
    main()
