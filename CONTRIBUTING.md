# Contributing

## Setup

You need [uv](https://docs.astral.sh/uv/). The test suite runs on macOS and Linux; using the
server needs macOS, cua-driver and a Jev key (see the README).

```sh
uv sync
```

## Checks

Run all of these before opening a pull request; CI runs the same ones.

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
```

mypy runs in strict mode over `src`, `tests` and `bench`, and coverage must stay at 90% or more.

## Ground rules

- **Behavior is the contract.** Tool names, input schemas, statuses, codes, messages, JSON key
  order and number formatting are what clients rely on. A change to any of them needs a test
  that pins the new behavior and a CHANGELOG entry.
- **stdout belongs to MCP.** The server writes nothing but protocol frames to stdout. Log with the
  `beans_picker` logger; it goes to stderr.
- **No OS access in tests.** Subprocesses, cua-driver and the cache directory sit behind small
  seams (`runner=`, `Paths`, fakes in `tests/fakes.py`), so the suite runs anywhere. Tests that
  need macOS are marked `macos` and skipped elsewhere; the end-to-end test also needs
  `BEANS_PICKER_E2E=1` and a real session with cua-driver.
- **Exact text.** Lengths are counted in UTF-16 units, the way the accessibility API counts them,
  and whitespace means the fixed set in `beans_picker._text`. Use its regex classes instead of `\s`,
  `\w`, `\d` and `.`.
- Code, comments and docs are in English.

## Native helpers

`src/beans_picker/native/*.m` are compiled on the user's machine, on first use. Keep them small
and free of dependencies beyond the macOS SDK. The ones that read an app (`axtext`, `menukeys`)
stay read-only; `activate` only brings an app to the front, and `winrec` only writes the video it
was asked for. A change to a file changes its cache key, so the next run rebuilds it.

## Benchmark

`bench/` compares cua-driver alone against beans-picker on eight tasks using Claude Code
(default), Codex CLI or OpenCode v2. It needs a Mac with cua-driver, the selected CLI and an
unlocked screen. Pass the model name accepted by that CLI:

```sh
sh bench/fixture-app/build.sh           # the fixture app, into ~/Library/Caches/beans-picker/fixture
uv run python -m bench.run --reps 3     # Claude Code, model sonnet; appends to bench/results/runs.jsonl
uv run python -m bench.run --harness codex --model gpt-6.1-sol
uv run python -m bench.run --harness opencode --model opencode/deepseek-v4.1-flash-free
uv run python -m bench.summarize > bench/results/summary.md
```

Codex uses a temporary `CODEX_HOME` with only the current home's `auth.json` copied in, read-only
sandboxing and no approval prompts. OpenCode runs a private server with temporary config and XDG
directories. Both receive only the selected MCP server and disable built-in tools where supported.
Temporary settings and copied credentials are removed when each run ends.

Summaries separate harness/model pairs and treat records without `harness` as `claude`.

Replace `bench/results/runs.jsonl` rather than appending to it when you publish new numbers, and
update the README tables from the new `summary.md`.
