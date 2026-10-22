# claude-top

`top` for your Claude Code sessions. It's a live view of every background session showing when it was last active, what it's waiting on, its model, effort and context size, and the helpers it started. It also shows how fast you're using your plan's limits.

![claude-top](docs/screenshot.png)

## Install

```sh
uv tool install git+https://github.com/jachai/claude-top   # or: pipx install git+https://github.com/jachai/claude-top
```

It's a single file with no dependencies: Python 3.9+ on macOS or Linux (on Windows only `--once` works). You can also copy `claude_top.py` anywhere on your `PATH`.

## Show plan usage (optional)

Claude Code passes your plan limits to the status line command. Point it at claude-top in `~/.claude/settings.json`:

```json
{ "statusLine": { "type": "command", "command": "claude-top statusline" } }
```

That records the limits and prints a short line like `5h 38% 0.6x · week 54% 1.3x ▶ out Fri 13:37`. If you already have a status line, add this to your script instead:

```sh
echo "$input" | claude-top record
```

## Use

```sh
claude-top              # full screen, refreshes every 2s
claude-top -s blocked   # only sessions waiting for you
claude-top -g billing   # text filter
claude-top --once       # print once and exit
```

| Key | Action |
|---|---|
| `↑` `↓` / `j` `k` | move |
| `enter` | open the session (`claude attach <id>`) |
| `c` | copy the attach command |
| `o` | sort: last active, created, context, agents, name |
| `f` | filter by state |
| `/` | text filter |
| `+` `-` | refresh slower / faster |
| `q` | quit |

## Reading it

- **Header**: sessions by state (`work`ing, `wait`ing for you, `review` = finished in a worktree that's still open, `done`, `stop`ped), then what's running now: working sessions + subagents, subagents spawned in total, and background tasks.
- **Usage bars (5h, week)**: the bar fills with the % of the limit used, and `│` marks how much of the window (`time`) has passed. A bar filled past the `│` means you're ahead of budget.
- **Speed (`1.3x`)**: % used ÷ % of time elapsed. `1.0x` lands exactly on 100% at reset, and `2.0x` runs out halfway through. The gauge's middle line is 1x. **now** is the speed over the last 3 hours.
- **▶**: when you'll run out at the current speed (and how early that is), or roughly where you'll end up at reset.
- **LAST ACTIVE / AGE**: last activity vs. when the session was created. The built-in agents view only shows age.
- **RUN / SPAWNED**: subagents running now / started in total. **BG**: background shells, monitors and crons in flight.
- **CTX %**: context used, measured against a 1M window. Change it with `-w 200000` or `CLAUDE_TOP_CONTEXT_WINDOW`.

## Try it without your data

```sh
python3 scripts/demo.py   # made-up sessions in a temp dir
```

`CLAUDE_CONFIG_DIR` and `CLAUDE_TOP_RATE_LIMITS` override where it reads from.

## Notes

- Session states and names come from `claude agents --json`, the same source as the built-in view. Everything else comes from Claude Code's local files (`~/.claude/jobs`, `~/.claude/projects`). They aren't a public API, so a Claude Code update can break a column; please open an issue if it does.
- It only reads, except for its own files in `~/.claude/claude-top/`. Nothing leaves your machine.
- This is an independent project, not affiliated with Anthropic.

MIT licensed.
