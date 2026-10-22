# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/jachai/claude-top/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/jachai/claude-top/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/jachai/claude-top/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jachai/claude-top/compare/a033382...v0.2.0
[0.1.0]: https://github.com/jachai/claude-top/commit/a033382
