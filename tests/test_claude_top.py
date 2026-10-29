"""claude-top tests.

Every test builds its own Claude config dir (state.json, transcripts, subagent metas, rate-limit
snapshot, history) under a temp HOME, never runs `claude` (CLAUDE_TOP_LIVE=0, live data is passed in) and never
asks GitHub for updates (CLAUDE_TOP_NO_UPDATE_CHECK=1; the update tests stub the request).

    python -m unittest discover -s tests
"""
import contextlib
import importlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import claude_top  # noqa: E402

H, D = 3600, 86400
MODEL = "claude-opus-5-5"


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def api_request(model=MODEL, effort="high", source="repl_main_thread"):
    return {"type": "api-request", "querySource": source, "params": {"model": model, "output_config": {"effort": effort}}}


def assistant(ctx, content=(), model=MODEL, sidechain=False, effort=None, at=None):
    usage = {"input_tokens": 10, "cache_read_input_tokens": ctx - 40, "cache_creation_input_tokens": 20, "output_tokens": 10}
    event = {"type": "assistant", "isSidechain": sidechain, "message": {"model": model, "usage": usage, "content": list(content)}}
    if effort:
        event["perTurnEffort"] = effort
    if at is not None:
        event["timestamp"] = iso(at)
    return event


def tool_use(description):
    return {"type": "tool_use", "name": "Bash", "input": {"command": "ls", "description": description}}


class Sandbox(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.config = self.home / ".claude"
        self.env = {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.config), "CLAUDE_TOP_LIVE": "0",
                    "CLAUDE_TOP_NO_UPDATE_CHECK": "1"}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("CLAUDE_TOP_RATE_LIMITS", "CLAUDE_TOP_CONTEXT_WINDOW"):
            os.environ.pop(key, None)
        self.ct = importlib.reload(claude_top)  # paths are resolved at import time
        self.now = time.time()

    # -- fixtures

    def state(self, job_id, **fields):
        fields.setdefault("updatedAt", iso(self.now))
        job = self.config / "jobs" / job_id
        job.mkdir(parents=True, exist_ok=True)
        (job / "state.json").write_text(json.dumps(fields))

    def transcript(self, sid, events, age=0.0):
        path = self.config / "projects" / "-home-dev-app" / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
        os.utime(path, (self.now - age, self.now - age))
        return path

    def subagent(self, sid, name, finished, age=0.0, model="haiku"):
        d = self.config / "projects" / "-home-dev-app" / sid / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.meta.json").write_text(json.dumps({"description": name, "model": model, "finished": finished}))
        log = d / f"{name}.jsonl"
        log.write_text("{}\n")
        os.utime(log, (self.now - age, self.now - age))

    def snapshot(self, fh=None, sd=None, ts=None):
        limits = {}
        if fh:
            limits["five_hour"] = {"used_percentage": fh[0], "resets_at": fh[1]}
        if sd:
            limits["seven_day"] = {"used_percentage": sd[0], "resets_at": sd[1]}
        self.ct.RATE_LIMITS.parent.mkdir(parents=True, exist_ok=True)
        self.ct.RATE_LIMITS.write_text(json.dumps({"ts": ts or int(self.now), "rate_limits": limits}))

    def history(self, samples):
        self.ct.HISTORY.parent.mkdir(parents=True, exist_ok=True)
        self.ct.HISTORY.write_text("".join(json.dumps({"t": t, "u": u}) + "\n" for t, u in samples))

    def windows(self):
        usage = self.ct.Usage()
        usage.refresh()
        return {w["label"]: w for w in usage.windows(self.now)}

    def rows(self, live=None):
        return {r["id"]: r for r in self.ct.load_rows(live or {})}

    def working_session(self, job_id="job1", sid="s1", **fields):
        self.state(job_id, state="working", name=job_id, sessionId=sid, createdAt=iso(self.now - 2 * H), **fields)
        self.transcript(sid, [api_request(), assistant(50_000, [tool_use("Running tests")])])
        return sid


class UsageMathTest(Sandbox):
    def test_pace_and_projection(self):
        # 5h: 50% used with 25% of the window gone -> 2x, runs out in 1h15, would be 100% at reset
        # week: 20% used halfway -> 0.4x, lands at 40%
        self.snapshot(fh=(50, self.now + 3.75 * H), sd=(20, self.now + 3.5 * D))
        w = self.windows()
        self.assertAlmostEqual(w["5h"]["pace"], 2.0, places=3)
        self.assertAlmostEqual(w["5h"]["runs_out"], self.now + 1.25 * H, delta=2)
        self.assertEqual(w["5h"]["at_reset"], 100.0)
        self.assertAlmostEqual(w["week"]["pace"], 0.4, places=3)
        self.assertIsNone(w["week"]["runs_out"])
        self.assertAlmostEqual(w["week"]["at_reset"], 40.0, places=3)
        self.assertIsNone(w["5h"]["recent_pace"])  # no history yet

    def test_exhausted_limit_runs_out_now(self):
        self.snapshot(fh=(100, self.now + 2 * H))
        w = self.windows()["5h"]
        self.assertEqual(w["runs_out"], self.now)
        self.assertEqual(w["at_reset"], 100.0)

    def test_snapshot_older_than_its_reset_rolls_the_window_forward(self):
        self.snapshot(fh=(80, self.now - 1 * H), ts=int(self.now - 2 * H))
        w = self.windows()["5h"]
        self.assertAlmostEqual(w["resets"], self.now + 4 * H, delta=1)
        self.assertAlmostEqual(w["elapsed"], 0.2, places=3)
        self.assertAlmostEqual(w["stale"], 2 * H, delta=2)

    def test_missing_or_broken_limit_shows_no_data(self):
        self.snapshot(fh=(10, self.now + H))
        self.assertIsNone(self.windows()["week"]["used"])
        self.ct.RATE_LIMITS.write_text(json.dumps({"ts": 1, "rate_limits": {"five_hour": {"used_percentage": "n/a", "resets_at": None}}}))
        self.assertIsNone(self.windows()["5h"]["used"])
        self.ct.RATE_LIMITS.write_text("{not json")
        self.assertIsNone(self.windows()["5h"]["used"])

    def test_recent_pace_from_history(self):
        self.history([(self.now - 2.5 * H, {"fh": 10, "sd": 50})])
        self.snapshot(fh=(40, self.now + 1.5 * H), sd=(54, self.now + 3 * D))  # 5h window started 3.5h ago
        w = self.windows()
        self.assertAlmostEqual(w["5h"]["recent_pace"], (30 / 2.5) / 20, places=3)  # 12%/h against 20%/h for 1x
        self.assertAlmostEqual(w["week"]["recent_pace"], (4 / 2.5) / (100 / 168), places=3)
        self.assertEqual(self.ct.pace_level(w["5h"]["recent_pace"]), "ok")

    def test_recent_pace_needs_half_the_lookback(self):
        self.history([(self.now - 1 * H, {"fh": 10, "sd": 50})])  # 3h lookback, 1h of history
        self.snapshot(fh=(40, self.now + 1.5 * H), sd=(54, self.now + 3 * D))
        self.assertIsNone(self.windows()["5h"]["recent_pace"])

    def test_recent_pace_ignores_samples_from_the_previous_window(self):
        self.history([(self.now - 4 * H, {"fh": 90}), (self.now - 1.6 * H, {"fh": 5})])
        self.snapshot(fh=(20, self.now + 3 * H))  # this window started 2h ago; the 90% sample is from the last one
        self.assertAlmostEqual(self.windows()["5h"]["recent_pace"], (15 / 1.6) / 20, places=3)

    def test_desktop_app_history_is_merged(self):
        desktop = self.home / "Library" / "Application Support" / "Claude" / "plan-usage-history.json"
        desktop.parent.mkdir(parents=True)
        desktop.write_text(json.dumps({"samples": [{"t": int((self.now - 2.5 * H) * 1000), "u": {"fh": 10, "sd": 1}}]}))
        self.snapshot(fh=(40, self.now + 1.5 * H))
        self.assertAlmostEqual(self.windows()["5h"]["recent_pace"], 0.6, places=3)

    def test_bars_mark_elapsed_time_against_usage(self):
        bar = self.ct.usage_bar(50, 0.25, 20)
        self.assertEqual(bar, "█████│████──────────")  # time marker inside the used part
        self.assertEqual(self.ct.usage_bar(20, 0.75, 20), "████───────────┼────")  # ahead of budget: marker on the line
        self.assertEqual(self.ct.pace_gauge(1.0, 8), "▰▰▰▰┃▱▱▱")
        self.assertEqual(self.ct.pace_gauge(2.0, 8), "▰▰▰▰╋▰▰▰")

    def test_statusline_text(self):
        self.snapshot(fh=(50, self.now + 3.75 * H), sd=(20, self.now + 3.5 * D))
        usage = self.ct.Usage()
        usage.refresh()
        text = self.ct.statusline_text(usage, self.now)
        self.assertRegex(text, r"^5h 50% 2\.0x → out \w{3} \d\d:\d\d · week 20% 0\.4x$")
        self.assertNotIn("\n", "".join(t for w in usage.windows(self.now) for t, _ in self.ct.usage_segments(w, self.now)))


class HistoryTest(Sandbox):
    def limits(self, fh, sd):
        return {"rate_limits": {"five_hour": {"used_percentage": fh, "resets_at": self.now + H},
                                "seven_day": {"used_percentage": sd, "resets_at": self.now + D}}}

    def test_record_writes_snapshot_and_dedupes_history(self):
        self.assertIsNotNone(self.ct.record(self.limits(38, 54)))
        self.assertEqual(json.loads(self.ct.RATE_LIMITS.read_text())["rate_limits"]["five_hour"]["used_percentage"], 38)
        self.ct.record(self.limits(38, 54))  # same reading again within 15 min: no new sample
        self.ct.record(self.limits(39, 54))
        samples = [json.loads(line) for line in self.ct.HISTORY.read_text().splitlines()]
        self.assertEqual([s["u"] for s in samples], [{"fh": 38, "sd": 54}, {"fh": 39, "sd": 54}])
        self.assertTrue(all(abs(s["t"] - self.now) < 5 for s in samples))

    def test_record_ignores_input_without_limits(self):
        for bad in (None, {}, {"rate_limits": "soon"}):
            self.assertIsNone(self.ct.record(bad))
        self.assertFalse(self.ct.RATE_LIMITS.exists())
        self.assertFalse(self.ct.HISTORY.exists())

    def test_history_is_trimmed_to_recent_days_once_big(self):
        old = [(self.now - 9 * D + i, {"fh": i % 100, "sd": 1}) for i in range(7000)]  # > 256 KB, all past 8 days
        self.history(old + [(self.now - 1 * D, {"fh": 5, "sd": 1})])
        self.assertGreater(self.ct.HISTORY.stat().st_size, 256 * 1024)
        self.ct.record(self.limits(6, 1))
        samples = [json.loads(line) for line in self.ct.HISTORY.read_text().splitlines()]
        self.assertEqual([s["u"]["fh"] for s in samples], [5, 6])
        self.assertEqual([], list(self.ct.HISTORY.parent.glob("usage-history.jsonl.*")))  # no temp file left

    def test_corrupt_history_line_is_skipped(self):
        self.ct.HISTORY.parent.mkdir(parents=True)
        self.ct.HISTORY.write_text('{"t": 1, "u": {"fh": 1}}\n{broken\n\n{"t": 2, "u": {"fh": 2}}\n')
        self.assertEqual([t for t, _ in self.ct.Usage().samples], [1, 2])


class TranscriptTest(Sandbox):
    def test_tail_gives_model_effort_context_and_activity(self):
        path = self.transcript("s1", [
            api_request(effort="low", source="agent"),  # a subagent's request: not the main thread's effort
            api_request(effort="high"),
            assistant(300_000, [tool_use("Subagent step")], sidechain=True),  # subagent turn: ignored
            assistant(120_000, [{"type": "text", "text": "Now I'll run the suite."}, tool_use("Run the suite")]),
        ])
        info = self.ct.transcript_info(str(path))
        self.assertEqual((info["model"], info["effort"], info["ctx"], info["activity"]),
                         (MODEL, "high", 120_000, "Run the suite"))
        self.assertAlmostEqual(info["mtime"].timestamp(), self.now, delta=2)

    def test_activity_falls_back_to_first_text_line(self):
        path = self.transcript("s1", [api_request(), assistant(1000, [{"type": "text", "text": "  Done.\nDetails follow"}])])
        self.assertEqual(self.ct.transcript_info(str(path))["activity"], "Done.")

    def test_effort_falls_back_to_per_turn_effort(self):
        path = self.transcript("s1", [assistant(1000, effort="medium")])
        info = self.ct.transcript_info(str(path))
        self.assertEqual((info["model"], info["effort"]), (MODEL, "medium"))

    def test_tail_widens_until_the_answer_is_found(self):
        filler = [{"type": "progress", "data": "x" * 100}] * 60  # ~7 KB after the answer
        path = self.transcript("s1", [api_request(), assistant(5000, [tool_use("step")])] + filler)
        with mock.patch.object(self.ct, "TAIL_BYTES", 256):
            info = self.ct.transcript_info(str(path))
        self.assertEqual((info["ctx"], info["effort"]), (5000, "high"))

    def test_unparseable_lines_do_not_break_the_scan(self):
        path = self.transcript("s1", [api_request(), assistant(2000, [tool_use("ok")])])
        with path.open("a") as fh:
            fh.write('[1, 2]\n{"type": "assistant", "message": "not an object"}\nnot json\n')
        self.assertEqual(self.ct.transcript_info(str(path))["ctx"], 2000)

    def test_cache_refreshes_when_the_transcript_grows(self):
        path = self.transcript("s1", [api_request(), assistant(1000)])
        self.assertEqual(self.ct.transcript_info(str(path))["ctx"], 1000)
        with path.open("a") as fh:
            fh.write(json.dumps(assistant(9000, [tool_use("more")])) + "\n")
        os.utime(path, (self.now + 5, self.now + 5))
        self.assertEqual(self.ct.transcript_info(str(path))["ctx"], 9000)

    def test_short_model(self):
        self.assertEqual(self.ct.short_model("claude-opus-5-5"), "opus 5.5")
        self.assertEqual(self.ct.short_model("claude-opus-4-1-20250805"), "opus 4.1")
        self.assertEqual(self.ct.short_model(None), "-")


class RowsTest(Sandbox):
    def test_last_active_is_the_last_message_not_the_file_time(self):
        # finished sessions' transcripts keep getting bookkeeping lines (no timestamp, no conversation),
        # which bumps the file's mtime; last active must stay at the last real message
        three_h = self.now - 3 * 3600
        self.state("j1", state="done", name="old", sessionId="s1", updatedAt=iso(three_h))
        self.transcript("s1", [api_request(), assistant(1000, [tool_use("final step")], at=three_h),
                               {"type": "artifact-autoreact-ledger"}], age=0)
        self.transcript("s2", [{"type": "user", "isSidechain": True, "timestamp": iso(self.now)},
                               {"type": "user", "timestamp": iso(self.now - 600)}], age=0)
        self.state("j2", state="waiting", name="replied", sessionId="s2", updatedAt=iso(self.now - 3600))
        rows = {r["id"]: r for r in self.ct.load_rows({})}
        self.assertAlmostEqual(rows["j1"]["last"].timestamp(), three_h, delta=2)
        self.assertAlmostEqual(rows["j2"]["last"].timestamp(), self.now - 600, delta=2)  # sidechain ignored

    def test_live_state_and_name_override_state_json(self):
        sid = self.working_session()
        live = {"job1": {"id": "job1", "kind": "background", "state": "blocked", "name": "renamed", "pid": 4}}
        row = self.rows(live)["job1"]
        self.assertEqual((row["state"], row["name"], row["model"], row["effort"], row["ctx"]), ("blocked", "renamed", MODEL, "high", 50_000))
        self.assertEqual(row["session"], sid)

    def test_review_means_done_with_a_pid_and_an_existing_worktree(self):
        worktree = self.home / "wt"
        worktree.mkdir()
        self.working_session(worktreePath=str(worktree), output={"result": "PR ready"})
        done = {"id": "job1", "kind": "background", "state": "done", "pid": 123}
        self.assertEqual(self.rows({"job1": done})["job1"]["state"], "review")
        self.assertEqual(self.rows({"job1": done})["job1"]["note"], "PR ready")
        self.assertEqual(self.rows({"job1": {**done, "pid": None}})["job1"]["state"], "done")
        worktree.rmdir()
        self.assertEqual(self.rows({"job1": done})["job1"]["state"], "done")

    def test_live_only_session_gets_a_row_with_its_start_time(self):
        started = int((self.now - 90) * 1000)
        live = {"x1": {"id": "x1", "kind": "interactive", "state": "working", "name": "shell", "startedAt": started, "cwd": "/tmp"}}
        row = self.rows(live)["x1"]
        self.assertEqual((row["kind"], row["state"], row["cwd"]), ("interactive", "working", "/tmp"))
        self.assertAlmostEqual(row["created"].timestamp(), self.now - 90, delta=1)

    def test_subagent_counts_as_running_only_while_unfinished_and_writing(self):
        sid = self.working_session()
        self.subagent(sid, "fresh", finished=False)
        self.subagent(sid, "finished", finished=True, model="sonnet")
        self.subagent(sid, "crashed", finished=False, age=20 * 60)  # no finished flag, but idle 20 min
        agents = self.rows()["job1"]["agents"]
        self.assertEqual((agents["total"], agents["running"], agents["active"]), (3, 1, ["fresh"]))
        self.assertEqual(agents["models"], {"haiku": 2, "sonnet": 1})
        # the same session reported done: nothing runs any more, even though nothing on disk changed
        self.state("job1", state="done", sessionId=sid)
        agents = self.rows()["job1"]["agents"]
        self.assertEqual((agents["total"], agents["running"]), (3, 0))

    def test_in_flight_counts(self):
        flight = {"tasks": 3, "kinds": ["local_bash", "artifact_watch"], "drainableMonitors": 1}
        self.working_session(inFlight=flight)
        row = self.rows()["job1"]
        self.assertEqual((row["bg"], row["bg_kinds"]), (2, ["local_bash"]))
        self.working_session(inFlight={"tasks": 2, "kinds": ["artifact_watch"], "drainableMonitors": 2})
        self.assertEqual((self.rows()["job1"]["bg"], self.rows()["job1"]["bg_kinds"]), (0, []))
        self.working_session(inFlight={"tasks": 1, "kinds": ["local_agent"]})  # helper reported before its meta exists
        self.assertEqual((self.rows()["job1"]["agents"]["running"], self.rows()["job1"]["bg"]), (1, 0))

    def test_done_session_in_flight_snapshot_is_ignored(self):
        sid = self.working_session(inFlight={"tasks": 3, "kinds": ["local_bash", "local_agent"]})
        self.subagent(sid, "fresh", finished=False)
        self.state("job1", state="done", sessionId=sid, inFlight={"tasks": 3, "kinds": ["local_bash", "local_agent"]})
        row = self.rows()["job1"]
        self.assertEqual((row["bg"], row["bg_kinds"], row["agents"]["running"]), (0, [], 0))
        self.assertEqual(self.ct.totals(self.rows().values())["background"], 0)

    def test_note_prefers_question_then_result_then_detail_then_live_activity(self):
        self.state("q", state="blocked", needs="Which DB?", detail="old", updatedAt=iso(self.now - 60))
        self.state("d", state="done", output={"result": "shipped"}, detail="old", updatedAt=iso(self.now - 60))
        self.state("w", state="working", detail="Planning", sessionId="s1", updatedAt=iso(self.now - 60))
        self.transcript("s1", [api_request(), assistant(100, [tool_use("Running tests")])])  # written after state.json
        rows = self.rows()
        self.assertEqual([rows[k]["note"] for k in "qdw"], ["Which DB?", "shipped", "Running tests"])
        self.state("w", state="working", detail="Planning", sessionId="s1", updatedAt=iso(self.now + 60))  # fresher than the transcript
        self.assertEqual(self.rows()["w"]["note"], "Planning")

    def test_session_text_cannot_drive_the_terminal(self):
        self.state("evil", state="blocked", name="a\x1b[31mred\x1b[0m\nb", needs="q\tx\x07", cwd="/d\x1b]0;t\x07")
        row = self.rows()["evil"]
        for text in (row["name"], row["note"], row["cwd"]):
            self.assertNotRegex(text, r"[\x00-\x1f\x7f]")
        self.assertEqual(row["name"], "a [31mred [0m b")

    def test_transcript_that_appears_later_is_picked_up(self):
        self.state("job1", state="working", sessionId="late")
        self.assertIsNone(self.rows()["job1"]["model"])
        self.transcript("late", [api_request(), assistant(100)])
        self.assertEqual(self.rows()["job1"]["model"], MODEL)

    def test_select_filters_and_sorts(self):
        self.state("a", state="working", name="Alpha billing", updatedAt=iso(self.now - 30))
        self.state("b", state="blocked", name="beta", cwd="/srv/billing", updatedAt=iso(self.now - 10))
        self.state("c", state="done", name="gamma", updatedAt=iso(self.now - 20))
        rows = list(self.rows().values())
        ids = lambda **kw: [r["id"] for r in self.ct.select(rows, kw.get("state"), kw.get("text"), kw.get("sort", "last active"))]
        self.assertEqual(ids(), ["b", "c", "a"])
        self.assertEqual(ids(state="blocked"), ["b"])
        self.assertEqual(ids(text="BILLING"), ["b", "a"])
        self.assertEqual(ids(sort="name"), ["a", "b", "c"])

    def guide_marker(self, sid, content):
        self.ct.GUIDES.mkdir(parents=True, exist_ok=True)
        (self.ct.GUIDES / sid).write_text(content + "\n")

    def test_session_guide_roles_from_markers_then_titles(self):
        self.state("g", state="blocked", name="renamed by hand", sessionId="sg")
        self.guide_marker("sg", "/home/me/.claude/session-guide/guide-1/ledger.md")
        self.state("old", state="done", name="whatever", sessionId="so")
        self.guide_marker("so", "retired")
        self.state("w", state="done", name="\U0001f7e5\U0001f7e5 Sessions Manager (1 of 1) \U0001f7e5\U0001f7e5", sessionId="sw")
        self.guide_marker("sw", "guided")  # the marker wins over the title
        self.state("d", state="done", name="\u2705 closed worker", sessionId="sd")
        self.guide_marker("sd", "done")  # a closed worker, not a ledger path
        self.state("t1", state="working", name="\U0001f7e5\U0001f7e5 Sessions Manager (2 of 3) \U0001f7e5\U0001f7e5")
        self.state("t2", state="done", name="\U0001f7e9\U0001f7e9 Sessions Manager (retired) \U0001f7e9\U0001f7e9")
        self.state("evil", state="done", name="x", sessionId="../../jobs/g/state.json")
        rows = self.rows()
        self.assertEqual({i: r["guide"] for i, r in rows.items()},
                         {"g": "guide", "old": "retired", "w": None, "d": "closed", "t1": "guide", "t2": "retired", "evil": None})
        self.assertEqual(rows["g"]["ledger"], "/home/me/.claude/session-guide/guide-1/ledger.md")
        self.assertIn("session guide, ledger /home/me/", self.ct.selected_detail(rows["g"]))

    def test_guide_pinned_first_and_retired_guides_hidden(self):
        self.state("a", state="working", name="alpha", updatedAt=iso(self.now - 10), sessionId="sa")
        self.state("z", state="blocked", name="zulu guide", updatedAt=iso(self.now - 500), sessionId="sz")
        self.guide_marker("sz", "/ledger.md")
        self.state("r", state="done", name="retired one", updatedAt=iso(self.now - 5), sessionId="sr")
        self.guide_marker("sr", "retired")
        rows = list(self.rows().values())
        for sort in self.ct.SORTS:
            self.assertEqual([r["id"] for r in self.ct.select(rows, None, None, sort)], ["z", "a"], sort)
        self.assertEqual([r["id"] for r in self.ct.select(rows, None, None, "last active", retired=True)], ["z", "r", "a"])
        self.assertEqual([r["id"] for r in self.ct.select(rows, "working", None, "last active")], ["a"])  # filters still apply
        self.assertEqual(self.ct.retired_count(rows), 1)

    def test_sections_fold_finished_and_idle_sessions(self):
        self.state("a", state="working", name="alpha", updatedAt=iso(self.now - 10))
        self.state("b", state="blocked", name="beta", updatedAt=iso(self.now - 20))
        self.state("c", state="done", name="gamma", updatedAt=iso(self.now - 30))
        self.state("d", state="done", name="delta", updatedAt=iso(self.now - 3 * D))
        self.state("s", state="stopped", name="sigma", updatedAt=iso(self.now - 40))
        self.state("o", state="blocked", name="omega", updatedAt=iso(self.now - 2 * D))  # waiting, but for 2 days
        self.state("g", state="done", name="guide", updatedAt=iso(self.now - 50), sessionId="sg")
        self.guide_marker("sg", "/ledger.md")
        rows, now = self.ct.select(list(self.rows().values()), None, None, "last active"), datetime.now(timezone.utc)
        ids = lambda view: [r["id"] for r in view]
        view = self.ct.layout(rows, self.ct.default_open(), now)
        self.assertEqual(ids(view), ["g", "group:blocked", "b", "group:working", "a", "group:done", "group:stopped", "group:idle"])
        self.assertEqual(ids(self.ct.layout(rows, self.ct.default_open(True), now)),
                         ["g", "group:blocked", "b", "group:working", "a", "group:done", "c", "d", "group:stopped", "s",
                          "group:idle", "o"])
        self.assertEqual(ids(self.ct.layout(rows, set(), now, idle_hours=72)),
                         ["g", "group:blocked", "group:working", "group:done", "group:stopped"])  # o isn't idle yet
        lines = {r["group"]: self.ct.group_line(r, now, 200) for r in view if r.get("group")}
        self.assertRegex(lines["done"], r"^ ▸ done\s+2 sessions · last active \d+s ago  ·  gamma, delta$")
        self.assertRegex(lines["idle"], r"^ ▸ idle 1d\+\s+1 session · last active 2d ago \(1 waiting\)  ·  omega$")
        self.assertRegex(lines["blocked"], r"^ ▾ needs input\s+1 session · last active \d+s ago$")  # open: no names
        self.assertEqual(self.ct.folded_count(view), 4)
        self.assertEqual(self.ct.selected_detail(view[-1]), " idle 1d+: o omega")

    def test_session_guide_done_workers_are_not_waiting(self):
        ledger = self.home / "ledger.md"
        ledger.write_text("\n".join([
            "# guide ledger", "## Streams", "| Stream | Where now |", "|---|---|", "| other [cccccc] | DONE |",
            "## Sessions", "| Title [id6 claude-top] | Level | Role |", "|---|---|---|",
            "| (g) finished worker [aaaaaa] | DONE, HANDED OFF 13:38 | x |",
            "| (g) live worker [bbbbbb] | MANAGED (guide-started) | x |",
            "| (g) busy again [dddddd] | DONE 12:00 | x |",
            "| 🟩🟩 old guide [eeeeee] | RETIRED 12:12 | old guide |", ""]))
        self.state("guide1", state="working", name="guide", sessionId="sg")
        self.guide_marker("sg", str(ledger))
        for job, state in (("aaaaaa11", "blocked"), ("bbbbbb11", "blocked"), ("cccccc11", "blocked"),
                           ("dddddd11", "working"), ("eeeeee11", "review")):
            self.state(job, state=state, name=job, needs="Which one?")
        self.state("ffffff11", state="blocked", name="closed by marker", needs="?", sessionId="sf")
        self.guide_marker("sf", "done")
        rows = self.rows()
        self.assertEqual({i: r["state"] for i, r in rows.items() if i != "guide1"},
                         {"aaaaaa11": "done", "bbbbbb11": "blocked", "cccccc11": "blocked", "dddddd11": "working",
                          "eeeeee11": "done", "ffffff11": "done"})
        self.assertEqual(rows["aaaaaa11"]["note"], "session guide: DONE, HANDED OFF 13:38")
        self.assertIn("done per the session guide", self.ct.selected_detail(rows["ffffff11"]))
        self.assertEqual(self.ct.ledger_done(str(self.home / "missing.md")), {})

    def test_summary_lines(self):
        sid = self.working_session()
        self.subagent(sid, "fresh", finished=False)
        self.state("q", state="blocked", needs="?")
        sessions, agents = self.ct.summary(list(self.rows().values()))
        self.assertEqual(sessions, "sessions 2: 1 work · 1 wait · 0 done · 0 stop")
        self.assertEqual(agents, "agents now 2 (1 sess + 1 sub) │ spawned 1 (1 live) │ bg 0")


class CliTest(Sandbox):
    def run_cli(self, *argv, stdin=None):
        return subprocess.run([sys.executable, str(ROOT / "claude_top.py"), *argv], input=stdin, capture_output=True,
                              text=True, env={**os.environ, **self.env, "COLUMNS": "200"})

    def test_once_prints_summary_usage_and_rows(self):
        self.state("w1", state="working", name="writer", needs="")
        self.state("b1", state="blocked", name="asker", needs="Which one?", updatedAt=iso(self.now - 60))
        self.snapshot(fh=(38, self.now + 2 * H), sd=(54, self.now + 4 * D))
        out = self.run_cli("--once")
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertEqual(lines[0], "sessions 2: 1 work · 1 wait · 0 done · 0 stop")
        self.assertIn("38% used", lines[2])
        self.assertIn("54% used", lines[3])
        self.assertTrue(lines[4].startswith("LAST ACTIVE") and lines[4].endswith("NOTE"))
        self.assertRegex(lines[5], r"^ ▾ needs input\s+1 session · last active \d+m ago$")
        self.assertRegex(lines[6], r"asker\s+Which one\?$")
        self.assertIn("waiting", lines[6])
        self.assertTrue(lines[7].startswith(" ▾ working "), lines[7])
        self.assertIn("working", lines[8])
        self.assertEqual(self.run_cli("--once", "-s", "blocked", "-n", "1").stdout.count("asker"), 1)
        self.assertEqual(self.run_cli("--once", "-s", "waiting").stdout.count("asker"), 1)
        self.assertNotIn("writer", self.run_cli("--once", "-s", "waiting").stdout)
        self.assertNotIn("asker", self.run_cli("--once", "-g", "writer").stdout)

    def test_long_note_is_cut_at_a_word(self):
        note = "Approve the maintenance window for Saturday night or Sunday morning, whichever has less traffic"
        self.state("b1", state="blocked", name="asker", needs=note)
        line = next(l for l in self.run_cli("--once").stdout.splitlines() if "asker" in l)
        shown = line[line.index("Approve"):]
        self.assertTrue(len(line) <= 200 and shown.endswith("…"), line)
        self.assertEqual(note[len(shown) - 1], " ")  # the cut is at a word boundary

    def test_once_without_usage_data_says_how_to_get_it(self):
        self.assertIn("claude-top init", self.run_cli("--once").stdout)

    def test_statusline_records_and_prints(self):
        status = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 38, "resets_at": self.now + 3 * H},
                                             "seven_day": {"used_percentage": 54, "resets_at": self.now + 2.8 * D}}})
        out = self.run_cli("statusline", stdin=status)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertRegex(out.stdout.strip(), r"^5h 38% 0\.9x · week 54% 0\.9x$")
        self.assertEqual(json.loads(self.ct.RATE_LIMITS.read_text())["rate_limits"]["seven_day"]["used_percentage"], 54)
        self.assertEqual(len(self.ct.HISTORY.read_text().splitlines()), 1)
        out = self.run_cli("record", stdin="not json")
        self.assertEqual((out.returncode, out.stdout), (0, ""))

    def test_once_hides_retired_guides_unless_asked(self):
        self.state("r1", state="done", name="handed over", sessionId="sr")
        self.ct.GUIDES.mkdir(parents=True)
        (self.ct.GUIDES / "sr").write_text("retired\n")
        self.assertNotIn("handed over", self.run_cli("--once").stdout)
        self.assertIn("handed over", self.run_cli("--once", "--retired").stdout)

    def test_once_folds_done_sessions_unless_expanded_or_filtered(self):
        self.state("fin1", state="done", name="finished one")
        folded = self.run_cli("--once").stdout
        self.assertRegex(folded, r"\n ▸ done\s+1 session · last active \d+s ago  ·  finished one\n")
        self.assertIn("finished one", folded)  # its name, on the section's line
        self.assertNotIn("fin1", folded)
        self.assertIn("fin1", self.run_cli("--once", "--expand").stdout)
        self.assertIn("fin1", self.run_cli("--once", "-s", "done").stdout)
        self.assertIn("fin1", self.run_cli("--once", "-g", "finished").stdout)

    def test_version(self):
        out = self.run_cli("--version")
        self.assertEqual(out.stdout.strip(), f"claude-top {self.ct.__version__}")


class HelpTest(unittest.TestCase):
    def test_help_lists_every_column_and_footer_key(self):
        text = "\n".join(claude_top.help_lines(80))
        for header, _ in claude_top.COLUMNS:
            self.assertIn(header, text)
        for key in ("enter", "c ", "o ", "f ", "/ ", "+  -", "space", "? ", "q  esc"):
            self.assertIn("  " + key, text)
        self.assertIn("waiting", text)
        self.assertNotIn("  u ", text)
        self.assertIn("  u            update to 0.4.0", "\n".join(claude_top.help_lines(80, "0.4.0")))

    def test_help_is_two_columns_only_when_it_fits(self):
        narrow, wide = claude_top.help_lines(80), claude_top.help_lines(200)
        self.assertLessEqual(max(map(len, narrow)) + 4, 80)
        self.assertLess(len(wide), len(narrow))
        self.assertTrue(wide[0].startswith("keys") and wide[0].endswith("columns"))
        self.assertLessEqual(max(map(len, wide)) + 4, 200)
        self.assertEqual(claude_top.help_lines(max(map(len, wide)) + 3), narrow)

    def test_box_fits_any_screen(self):
        for rows in range(0, 40, 3):
            for cols in range(0, 220, 7):
                y, x, box, offset = claude_top.help_box(claude_top.help_lines(cols), rows, cols, 999)
                self.assertTrue(y >= 0 and x >= 0 and y + len(box) <= rows)
                self.assertTrue(all(x + len(line) <= cols for line in box))
                self.assertEqual(len({len(line) for line in box}), 1 if box else 0)

    def test_box_scrolls_when_it_overflows(self):
        lines = claude_top.help_lines(80)
        y, x, box, offset = claude_top.help_box(lines, 24, 80)
        self.assertEqual((y, len(box), offset), (0, 24, 0))
        self.assertIn("any other key closes", box[-1])
        _, _, box, offset = claude_top.help_box(lines, 24, 80, 999)
        self.assertEqual(offset, len(lines) - 22)
        self.assertIn(lines[-1], box[-2])
        _, _, box, offset = claude_top.help_box(lines, 24, 80, -5)
        self.assertEqual(offset, 0)
        _, _, box, _ = claude_top.help_box(claude_top.help_lines(200), 40, 200)
        self.assertIn("─ any key closes ", box[-1])

    def test_fit_cuts_at_a_word(self):
        fit = claude_top.fit
        self.assertEqual(fit("short", 10), "short")
        self.assertEqual(fit("Running the integration suite", 20), "Running the…")
        self.assertEqual(fit("one, two three", 10), "one…")
        self.assertEqual(fit("unbreakable", 6), "unbre…")
        self.assertEqual((fit("abc", 1), fit("abc", 0), fit("abc", -3)), ("a", "", ""))

    def test_state_labels_fit_the_column(self):
        width = dict(claude_top.COLUMNS)["STATE"]
        self.assertTrue(all(len(label) <= width for label in claude_top.STATE_LABEL.values()))
        self.assertEqual(claude_top.STATE_LABEL["blocked"], "waiting")


@unittest.skipUnless(hasattr(os, "fork") and claude_top.curses, "needs a pty and curses")
class ScreenTest(Sandbox):
    """Drive the full-screen view in a pty: it must survive the help overlay at any size."""

    @classmethod
    def setUpClass(cls):
        probe = [sys.executable, "-c", "import curses; curses.setupterm('xterm-256color')"]
        if subprocess.run(probe, capture_output=True).returncode:
            raise unittest.SkipTest("no xterm-256color terminfo")

    def run_screen(self, rows, cols, keys, env=None, argv=()):
        import fcntl
        import pty
        import select
        import struct
        import termios

        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                os.environ.update(TERM="xterm-256color", PATH=str(self.home / "no-bin"))  # never reach a real `claude`
                os.environ.update(env or {})
                os.execv(sys.executable, [sys.executable, str(ROOT / "claude_top.py"), *argv])
            finally:
                os._exit(127)
        out = bytearray()

        def pump(seconds):
            end = time.time() + seconds
            while time.time() < end:
                if select.select([fd], [], [], 0.05)[0]:
                    try:
                        out.extend(os.read(fd, 65536))
                    except OSError:
                        return

        pump(0.8)
        for key in keys:
            try:
                os.write(fd, key)
            except OSError:
                self.fail(f"claude-top exited before {key!r}: {bytes(out[-800:])!r}")
            pump(0.2)
        for _ in range(50):
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            pump(0.1)
        else:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            self.fail(f"claude-top did not quit: {bytes(out[-500:])!r}")
        os.close(fd)
        text = bytes(out).decode(errors="replace")
        self.assertNotIn("Traceback", text)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        return text

    def test_help_opens_scrolls_and_closes(self):
        self.state("b1", state="blocked", name="asker", needs="Which one?")
        text = self.run_screen(24, 80, [b"?", b"j", b"j", b"x", b"?", b"q", b"q"])
        self.assertIn("? help", text)
        self.assertIn("any other key closes", text)
        self.assertIn("this help", text)

    def test_cached_newer_release_is_offered_and_n_declines(self):
        self.ct.DATA_DIR.mkdir(parents=True)
        self.ct.UPDATE_CHECK.write_text(json.dumps({"checked": int(self.now), "latest": "9.9.9"}))
        env = {"CLAUDE_TOP_NO_UPDATE_CHECK": "0", "HTTPS_PROXY": "http://127.0.0.1:9"}  # a fresh cache: no request
        text = self.run_screen(24, 80, [b"u", b"n", b"?", b"x", b"q"], env)
        self.assertIn(f"claude-top {self.ct.__version__} ", text)
        self.assertIn("u update to 9.9.9", text)
        self.assertIn("update to 9.9.9? y/n  $ git -C ", text)
        self.assertEqual(json.loads(self.ct.UPDATE_CHECK.read_text())["checked"], int(self.now))

    def test_r_toggles_retired_guides(self):
        self.state("r1", state="done", name="handed over", sessionId="sr")
        self.ct.GUIDES.mkdir(parents=True)
        (self.ct.GUIDES / "sr").write_text("retired\n")
        text = self.run_screen(24, 160, [b"r", b"q"])
        self.assertIn("retired guides: 1 hidden (r)", text)
        self.assertIn("shown (r)", text)  # curses repaints only what changed on the line
        self.assertIn("handed over", text)

    def test_enter_and_click_open_and_close_a_section(self):
        self.state("d1", state="done", name="closedone")
        click = b"\x1b[M" + bytes([32, 32 + 6, 32 + self.ct.HEADER_ROWS + 1])  # xterm press, column 5, first body line
        release = b"\x1b[M" + bytes([35, 32 + 6, 32 + self.ct.HEADER_ROWS + 1])
        text = self.run_screen(24, 160, [b"\n", click + release, b"q"])
        self.assertIn("\x1b[?1000h", text)  # mouse reporting on
        first = text.find("▸ done")  # curses repaints only the cells that change: after that, just the arrow
        opened = text.find("▾", first)
        self.assertTrue(0 <= first < opened < text.find("▸", opened), (first, opened))
        self.assertIn("folded: 1", text)
        self.assertNotIn("\x1b[?1000h", self.run_screen(24, 160, [b"q"], argv=["--no-mouse"]))

    def test_tiny_terminal_does_not_crash(self):
        for rows, cols in ((4, 12), (1, 1)):  # no rows, so the enter that a 1x1 prompt can't take attaches nothing
            self.run_screen(rows, cols, [b"?", b"j", b"x", b"/", b"a\n", b"q"])


class Reply:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class UpdateTest(Sandbox):
    def setUp(self):
        super().setUp()
        os.environ.pop("CLAUDE_TOP_NO_UPDATE_CHECK")
        self.fetch_latest = self.ct.fetch_latest
        self.fetch = mock.Mock(return_value="9.0.0")
        patcher = mock.patch.object(self.ct, "fetch_latest", self.fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cache(self):
        return json.loads(self.ct.UPDATE_CHECK.read_text())

    def test_versions_compare_as_numbers(self):
        newer = self.ct.is_newer
        self.assertTrue(newer("0.10.0", "0.9.0"))
        self.assertFalse(newer("0.9.0", "0.10.0"))
        self.assertTrue(newer("v0.3.1", "0.3.0"))
        self.assertTrue(newer("1.0", "0.9.9"))
        self.assertFalse(newer("1.0", "1.0.0"))
        self.assertFalse(newer("0.3.0", "0.3.0"))
        self.assertFalse(newer("0.3.0rc1", "0.3.0"))  # a pre-release is older than its release
        self.assertTrue(newer("0.3.0", "0.3.0rc1"))
        self.assertFalse(newer("latest", "0.3.0"))
        self.assertFalse(newer(None, "0.3.0"))

    def test_cache_is_fresh_for_a_day(self):
        due = self.ct.check_due
        self.assertTrue(due(None, self.now))
        self.assertTrue(due({"checked": "yesterday"}, self.now))
        self.assertFalse(due({"checked": self.now - 23 * H}, self.now))
        self.assertTrue(due({"checked": self.now - 25 * H}, self.now))
        self.assertTrue(due({"checked": self.now + 2 * D}, self.now))  # a clock that went backwards

    def test_checks_once_a_day_and_offers_only_newer(self):
        self.assertEqual(self.ct.update_check(self.now), "9.0.0")
        self.assertEqual(self.cache(), {"checked": int(self.now), "latest": "9.0.0"})
        self.assertEqual(self.ct.update_check(self.now + 3 * H), "9.0.0")
        self.assertEqual(self.fetch.call_count, 1)  # the second start read the cache
        self.fetch.return_value = self.ct.__version__
        self.assertIsNone(self.ct.update_check(self.now + 25 * H))
        self.assertEqual(self.fetch.call_count, 2)

    def test_failure_is_cached_as_checked(self):
        self.fetch.return_value = None
        self.assertIsNone(self.ct.update_check(self.now))
        self.assertEqual(self.cache(), {"checked": int(self.now), "latest": None})
        self.assertIsNone(self.ct.update_check(self.now + H))
        self.assertEqual(self.fetch.call_count, 1)  # no retry until tomorrow

    def test_opt_out(self):
        os.environ["CLAUDE_TOP_NO_UPDATE_CHECK"] = "1"
        self.assertIsNone(self.ct.update_check(self.now))
        self.fetch.assert_not_called()
        self.assertFalse(self.ct.UPDATE_CHECK.exists())

    def test_fetch_reads_the_tag_of_the_latest_release(self):
        seen = {}

        def opener(req, timeout):
            seen.update(url=req.full_url, agent=req.get_header("User-agent"), timeout=timeout)
            return Reply(b'{"tag_name": "v0.4.0", "name": "0.4.0"}')

        self.assertEqual(self.fetch_latest(opener), "0.4.0")
        self.assertEqual(seen["url"], "https://api.github.com/repos/jachai/claude-top/releases/latest")
        self.assertTrue(seen["agent"].startswith("claude-top/"))
        self.assertLessEqual(seen["timeout"], 5)

    def test_fetch_failures_are_silent(self):
        def raises(error):
            def opener(req, timeout):
                raise error
            return opener

        rate_limited = urllib.error.HTTPError(claude_top.UPDATE_URL, 403, "rate limit exceeded", {}, io.BytesIO())
        self.addCleanup(rate_limited.close)
        for opener in (raises(urllib.error.URLError("offline")), raises(rate_limited), raises(TimeoutError()),
                       lambda req, timeout: Reply(b"<html>"),
                       lambda req, timeout: Reply(b'{"message": "API rate limit exceeded"}'),
                       lambda req, timeout: Reply(b'["v1.0.0"]'),
                       lambda req, timeout: Reply(b'{"tag_name": "nightly"}')):
            self.assertIsNone(self.fetch_latest(opener))

    def test_once_never_checks(self):
        argv = ["claude-top", "--once"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            self.ct.main()
        self.fetch.assert_not_called()
        self.assertFalse(self.ct.UPDATE_CHECK.exists())

    def test_install_method_picks_the_command(self):
        checkout = self.home.resolve() / "src" / "claude-top"
        (checkout / ".git").mkdir(parents=True)
        (checkout / "pyproject.toml").write_text('name = "claude-agents-top"\n')
        site = "lib/python3.12/site-packages/claude_top.py"
        uv_env = self.home / ".local/share/uv/tools/claude-agents-top"
        pipx_env = self.home / ".local/share/pipx/venvs/claude-agents-top"
        other_repo = self.home / "dotfiles"
        (other_repo / ".git").mkdir(parents=True)
        cmd = self.ct.update_command
        self.assertEqual(cmd(checkout / "claude_top.py", "/usr"), ["git", "-C", str(checkout), "pull", "--ff-only"])
        self.assertEqual(cmd(uv_env / site, uv_env), ["uv", "tool", "upgrade", "claude-agents-top"])
        self.assertEqual(cmd(pipx_env / site, pipx_env), ["pipx", "upgrade", "claude-agents-top"])
        custom = self.home / "tools" / "ct"  # UV_TOOL_DIR elsewhere: uv's receipt file still says it is uv
        custom.mkdir(parents=True)
        (custom / "uv-receipt.toml").write_text("")
        self.assertEqual(cmd(custom / site, custom)[0], "uv")
        self.assertIsNone(cmd(other_repo / "claude_top.py", "/usr"))  # someone else's repo: never pull it
        self.assertIsNone(cmd(self.home / "venv" / site, self.home / "venv"))
        self.assertIn("pip install -U claude-agents-top", self.ct.manual_update("0.4.0"))

    def test_footer_and_prompt(self):
        self.assertEqual(self.ct.footer_keys(None), self.ct.KEYS)
        self.assertTrue(self.ct.footer_keys("0.4.0").startswith(" u update to 0.4.0  enter attach"))
        prompt = self.ct.update_prompt(["git", "-C", "/my dir", "pull", "--ff-only"], "0.4.0")
        self.assertEqual(prompt, " update to 0.4.0? y/n  $ git -C '/my dir' pull --ff-only")

    def test_restart_uses_the_command_it_was_run_as(self):
        exe = self.home / "bin" / "claude-top"
        exe.parent.mkdir()
        exe.write_text("#!/bin/sh\n")
        self.assertEqual(self.ct.restart_argv([str(exe), "-s", "waiting"]), [str(exe), "-s", "waiting"])
        script = os.path.abspath(self.ct.__file__)
        self.assertEqual(self.ct.restart_argv([script, "-i", "5"]), [sys.executable, script, "-i", "5"])

    def test_failed_update_waits_for_enter(self):
        out = io.StringIO()
        with mock.patch("builtins.input", return_value="") as asked, contextlib.redirect_stdout(out), \
                mock.patch.object(self.ct.os, "execv") as execv:
            self.ct.run_update([sys.executable, "-c", "raise SystemExit(3)"], "9.0.0")
            self.ct.run_update([str(self.home / "no-such-tool")], "9.0.0")
        execv.assert_not_called()
        self.assertEqual(asked.call_count, 2)
        self.assertIn("failed (exit 3)", out.getvalue())
        self.assertIn("could not run", out.getvalue())

    def test_successful_update_restarts(self):
        with mock.patch.object(self.ct.os, "execv") as execv, contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(sys, "argv", [os.path.abspath(self.ct.__file__), "-i", "5"]), \
                mock.patch("builtins.input", return_value=""):  # execv is stubbed, so run_update goes on
            self.ct.run_update([sys.executable, "-c", "pass"], "9.0.0")
        execv.assert_called_once_with(sys.executable, [sys.executable, os.path.abspath(self.ct.__file__), "-i", "5"])


class InitTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.command = f"{shlex.quote(sys.executable)} {shlex.quote(str(ROOT / 'claude_top.py'))}"
        patcher = mock.patch.object(self.ct, "own_command", lambda: self.command)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = self.config / "settings.json"

    def init(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                self.ct.init_main(list(argv))
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def read(self):
        return json.loads(self.settings.read_text())

    def backups(self):
        return sorted(self.config.glob("settings.json.*.bak"))

    def test_fresh_settings(self):
        code, out = self.init()
        self.assertEqual(code, 0)
        self.assertEqual(self.read(), {"statusLine": {"type": "command", "command": f"{self.command} statusline"}})
        self.assertEqual(self.backups(), [])  # nothing to back up
        self.assertIn("next: claude-top", out)
        self.assertIn("claude on PATH", out)

    def test_other_settings_and_formatting_are_kept(self):
        self.config.mkdir(parents=True)
        self.settings.write_text('{\n  "permissions": {\n    "allow": [\n      "Bash(ls)"\n    ]\n  },\n  "model": "opus"\n}\n')
        self.init()
        self.assertEqual(self.read(), {"permissions": {"allow": ["Bash(ls)"]}, "model": "opus",
                                       "statusLine": {"type": "command", "command": f"{self.command} statusline"}})
        self.assertTrue(self.settings.read_text().startswith('{\n  "permissions": {\n    "allow": ['))
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(json.loads(self.backups()[0].read_text()), {"permissions": {"allow": ["Bash(ls)"]}, "model": "opus"})

    def test_rerun_changes_nothing(self):
        self.init()
        before = self.settings.read_bytes()
        code, out = self.init()
        self.assertEqual((code, self.settings.read_bytes()), (0, before))
        self.assertIn("already set up", out)
        self.assertEqual(self.backups(), [])

    def test_foreign_status_line_is_never_replaced_without_wrap(self):
        self.config.mkdir(parents=True)
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "~/my-status.sh"}}, indent=2))
        before = self.settings.read_bytes()
        code, out = self.init()
        self.assertEqual((code, self.settings.read_bytes()), (1, before))
        self.assertIn("--wrap", out)
        self.assertIn('claude-top record', out)
        self.assertEqual(self.backups(), [])

    def test_wrap_runs_both_with_the_same_input(self):
        received = self.home / "received.json"
        self.config.mkdir(parents=True)
        original = {"type": "command", "command": f"cat > {shlex.quote(str(received))}; echo my-line", "padding": 0}
        self.settings.write_text(json.dumps({"statusLine": original}, indent=2))
        code, _ = self.init("--wrap")
        self.assertEqual(code, 0)
        self.assertEqual(self.read()["statusLine"], {**original, "command": str(self.ct.WRAPPER)})
        status = {"rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": self.now + H}}}
        out = subprocess.run(["/bin/sh", str(self.ct.WRAPPER)], input=json.dumps(status), capture_output=True, text=True, env=os.environ.copy())
        self.assertEqual((out.returncode, out.stdout), (0, "my-line\n"), out.stderr)
        self.assertEqual(json.loads(received.read_text()), status)
        self.assertEqual(json.loads(self.ct.RATE_LIMITS.read_text())["rate_limits"], status["rate_limits"])
        self.assertEqual(json.loads(self.ct.INIT_STATE.read_text())["previous"], original)
        # undo puts the original back and removes the wrapper
        code, out = self.init("--undo")
        self.assertEqual((code, self.read()["statusLine"]), (0, original))
        self.assertFalse(self.ct.WRAPPER.exists())
        self.assertIn("restored", out)

    def test_invalid_json_is_refused(self):
        self.config.mkdir(parents=True)
        self.settings.write_text('{"statusLine": ')
        code, _ = self.init()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.settings.read_text(), '{"statusLine": ')
        self.assertEqual(self.backups(), [])

    def test_dry_run_writes_nothing(self):
        self.config.mkdir(parents=True)
        self.settings.write_text("{}\n")
        code, out = self.init("--dry-run")
        self.assertEqual((code, self.settings.read_text()), (0, "{}\n"))
        self.assertIn("statusline", out)
        self.assertEqual(self.backups(), [])
        self.assertFalse(self.ct.INIT_STATE.exists())

    def test_undo_removes_what_init_added(self):
        self.config.mkdir(parents=True)
        self.settings.write_text(json.dumps({"model": "opus"}, indent=2))
        self.init()
        code, _ = self.init("--undo")
        self.assertEqual((code, self.read()), (0, {"model": "opus"}))
        self.assertFalse(self.ct.INIT_STATE.exists())
        code, _ = self.init("--undo")  # nothing of ours left
        self.assertEqual(code, 1 if isinstance(code, int) else code)
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
