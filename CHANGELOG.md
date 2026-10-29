# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.5.0] - 2026-10-29

### Added

- Sessions sit in sections, like Claude Code's agents view: **needs input** and **working** open, while **review**, **done**, **stopped** and **idle 1d+** each fold into one line that says how many sessions it holds, when the newest was last active, and their names. Enter or a mouse click on a section's line opens or closes it, `→` `←` do the same, and `e` opens every section or puts them back. The filter line counts what is folded. A state or text filter shows plain rows, with no sections. `--once` prints the same sections.
- `--expand` starts with every section open; `--idle-hours` (or `CLAUDE_TOP_IDLE_HOURS`, default 24) sets when a quiet session counts as idle; `--no-mouse` leaves the mouse to the terminal.
- session-guide workers the guide has finished with show as done instead of waiting: a `done` marker, or a DONE / RETIRED / STOOD DOWN / CLOSED Level against the session's id in the active guide's ledger (`## Sessions` table). A worker that is working stays working.

### Changed

- The footer drops `+/- speed` to fit 80 columns; `?` help still lists it.

## [0.4.1] - 2026-10-29

### Fixed

- A session that session-guide closed (its `done` marker, session-guide 0.1.3) is no longer taken for the active guide and pinned on top. Only a marker holding a ledger path counts as a guide.

## [0.4.0] - 2026-10-29

### Added

- [session-guide](https://github.com/jachai/session-guide) support. The active guide session (the 🟥🟥 Sessions Manager) is always the first row, whatever the sort, and retired guides (🟩🟩) are hidden. `r` shows or hides them, and the filter line says how many are hidden; `--retired` shows them from the start, `--once` included. Roles come from session-guide's marker files in `~/.claude/session-guide/guides/`, or from its default titles when a session has no marker. The selected guide's detail line shows its ledger.

## [0.3.0] - 2026-10-22

### Added

- The title bar shows claude-top's version.
- A daily update check in the full-screen view: a background request asks GitHub for the latest release's version and caches the answer in `~/.claude/claude-top/update-check.json`, so it never slows startup and fails silently offline. `--once`, the status line and `init` never check. `CLAUDE_TOP_NO_UPDATE_CHECK=1` turns it off.
- `u` updates claude-top when a newer release is out. It shows the command and asks first: `git pull --ff-only` for a git checkout, `uv tool upgrade` or `pipx upgrade` for those installs, and otherwise the `pip install -U` command to run yourself. After a successful update claude-top restarts with the same arguments.

## [0.2.1] - 2026-10-22

### Fixed

- LAST ACTIVE uses the time of a session's last message instead of its transcript file's modification time. Bookkeeping lines (such as artifact comment watching) keep touching finished sessions' transcripts, which made idle sessions look recently active.
- The usage lines use `→` instead of `▶`. Some terminals draw `▶` as a two-cell emoji, which garbled the header on refresh.

## [0.2.0] - 2026-10-22

### Added

- `?` opens a help overlay listing every key and column. It scrolls on small terminals, and any other key closes it.
- `-s waiting` works as an alias for `-s blocked`.
- `claude-top init` sets claude-top as Claude Code's status line so it can record your plan limits. It backs up `settings.json`, writes it atomically and can be run again safely. `--wrap` keeps an existing status line, `--dry-run` shows the change and `--undo` reverts it.
- `--version`, with the version kept in one place.
- A stdlib `unittest` suite that runs on temporary config dirs, including a pty smoke test of the full-screen view.
- CI on Ubuntu and macOS with Python 3.9 and 3.14, installed with uv.
- A release workflow that publishes tagged versions to PyPI with trusted publishing and creates a GitHub release.
- `scripts/demo.py --gif` and `--social` render the demo GIF and the social preview card. The demo data now shows every state, running subagents and a background task.
- Packaging metadata. The PyPI distribution is `claude-agents-top`, and the command stays `claude-top`.
- CONTRIBUTING.md and GitHub issue templates.

### Changed

- The STATE column shows `waiting` instead of `needs-in` for sessions waiting on you, which matches the header's `wait`.
- Notes and the detail line are cut at a word boundary with `…` instead of mid-word.
- Text typed at the `/` prompt stays on the footer bar.

### Fixed

- Finished subagents no longer show as running because of a stale cache.
- A transcript that appears after its session starts is now picked up.
- One corrupt line in the usage history no longer drops every sample after it.
- Brand-new live sessions that have no transcript yet are no longer dropped.
- Malformed local files no longer crash the view.
- The detail line is now sanitized like the rest of the session text.
- The demo and screenshots no longer read the Claude desktop app's real usage history.

## [0.1.0] - 2026-10-22

### Added

- First release: a full-screen, auto-refreshing list of Claude Code background sessions with last-active time, state, model, effort, context, subagents and background tasks.
- 5-hour and weekly plan usage bars with a speed gauge and a run-out projection.
- `claude-top statusline` and `claude-top record`, which record plan limits from Claude Code's status-line input.
- `--once` output and a demo with made-up data.

[Unreleased]: https://github.com/jachai/claude-top/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/jachai/claude-top/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/jachai/claude-top/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/jachai/claude-top/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jachai/claude-top/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/jachai/claude-top/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jachai/claude-top/compare/a033382...v0.2.0
[0.1.0]: https://github.com/jachai/claude-top/commit/a033382
