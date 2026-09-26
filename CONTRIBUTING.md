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
  `cua_jev` logger; it goes to stderr.
- **No OS access in tests.** Subprocesses, cua-driver and the cache directory sit behind small
  seams (`runner=`, `Paths`, fakes in `tests/fakes.py`), so the suite runs anywhere. Tests that
  need macOS are marked `macos` and skipped elsewhere; the end-to-end test also needs
  `CUA_JEV_E2E=1` and a real session with cua-driver.
- **Exact text.** Lengths are counted in UTF-16 units, the way the accessibility API counts them,
  and whitespace means the fixed set in `cua_jev._text`. Use its regex classes instead of `\s`,
  `\w`, `\d` and `.`.
- Code, comments and docs are in English.

## Native helpers

`src/cua_jev/native/*.m` are compiled on the user's machine, on first use. Keep them small,
read-only and free of dependencies beyond the macOS SDK. A change to either file changes its
cache key, so the next run rebuilds it.

## Benchmark

`bench/` compares Claude Code with cua-driver alone against Claude Code with cua-jev on eight
tasks. It needs a Mac with cua-driver, the `claude` CLI and an unlocked screen.

```sh
sh bench/fixture-app/build.sh           # the fixture app, into ~/Library/Caches/cua-jev/fixture
uv run python -m bench.run --reps 3     # appends to bench/results/runs.jsonl
uv run python -m bench.summarize > bench/results/summary.md
```

Replace `bench/results/runs.jsonl` rather than appending to it when you publish new numbers, and
update the README tables from the new `summary.md`.
