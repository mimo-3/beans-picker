# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.3.0] - Unreleased

First release on PyPI.

### Added

- The `cua-jev` command and `python -m cua_jev`: an MCP server over stdio with the tools `observe`,
  `act` and `extract`. It runs on macOS; on other systems it exits with an error.
- `cua-jev --version` and `cua-jev --help`, which work on every system.
- `CUA_JEV_LOG_LEVEL` sets the level of the server's log on stderr (default `WARNING`).
- The Jev key and other settings are read from the environment, then from `.env.local` / `.env`
  in the checkout when the package runs from a cua-jev checkout, then from `~/.config/cua-jev/`
  (`$XDG_CONFIG_HOME/cua-jev`), so an installed server finds its key without a checkout.
- Requests to cua-driver and to Jev have no time limit. At shutdown, the final `end_session` to
  cua-driver waits at most 5 seconds.

### Fixed

- A text field named only by its placeholder keeps its candidate ids once it is typed in, so text
  entered into it is checked in that field instead of ending as `mismatch` ("the target field is gone").

[0.3.0]: https://github.com/mimo-3/cua-jev/releases/tag/v0.3.0

