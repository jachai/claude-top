# Contributing

Issues and pull requests are welcome. claude-top is one file, `claude_top.py`, using only the standard library and
running on Python 3.9+. Please keep it that way: no runtime dependencies and no new modules. Short functions and plain
names are preferred over comments.

## Tests

The tests need [uv](https://docs.astral.sh/uv/), which fetches the Python versions for you. CI runs the oldest and
newest supported versions on Ubuntu and macOS:

```sh
uv run --no-project --python 3.9 python -m unittest discover -s tests
uv run --no-project --python 3.14 python -m unittest discover -s tests
uvx pyflakes claude_top.py scripts tests
```

Every test builds its own Claude config dir in a temp folder and sets `CLAUDE_TOP_LIVE=0`, so nothing reads your real
`~/.claude` or runs `claude`. New tests should do the same.

## Demo and images

```sh
python3 scripts/demo.py   # the full-screen view on made-up sessions
uv run --no-project --with pyte --with pillow python scripts/demo.py --screenshot docs/screenshot.png
uv run --no-project --with pyte --with pillow python scripts/demo.py --gif docs/demo.gif
uv run --no-project --with pyte --with pillow python scripts/demo.py --social docs/social-preview.png
```

The image commands run the demo in a pseudo-terminal, read the screen with pyte and draw it with Pillow. These are
development tools only, not dependencies of claude-top. Regenerate the images when the view changes, and look at them
before you commit. `docs/social-preview.png` is uploaded by hand under Settings → Social preview.

## Privacy

Never commit real session data: no transcripts, `state.json` files, session names, paths or plan usage from your own
machine. This applies to tests, issues and screenshots. Use the demo data or made-up fixtures instead.

## Releases

1. Move the `Unreleased` notes in `CHANGELOG.md` under a new version heading.
2. Bump `__version__` in `claude_top.py`.
3. Push a `vX.Y.Z` tag.

The release workflow then tests the tag, builds it, checks that the tag matches `__version__`, publishes it to PyPI and
creates a GitHub release with that version's changelog notes.
