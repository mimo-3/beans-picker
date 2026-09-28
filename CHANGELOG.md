# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

To be released as 0.3.0, the first release on PyPI. Versions 0.1 and 0.2 were never published.

### Added

- The `beans-picker` command and `python -m beans_picker`: an MCP server over stdio with the tools `observe`,
  `act` and `extract`. It runs on macOS; on other systems it exits with an error.
- `beans-picker --version` and `beans-picker --help`, which work on every system.
- `BEANS_PICKER_LOG_LEVEL` sets the level of the server's log on stderr (default `WARNING`).
- The Jev key and other settings are read from the environment, then from `.env.local` / `.env`
  in the checkout when the package runs from a beans-picker checkout, then from `~/.config/beans-picker/`
  (`$XDG_CONFIG_HOME/beans-picker`), so an installed server finds its key without a checkout.
- Time limits on every request: `CUA_DRIVER_TIMEOUT` (default 120 s) for cua-driver's startup and
  each call, `JEV_CONNECT_TIMEOUT` (10 s) and `JEV_READ_TIMEOUT` (120 s) for Jev. A request that
  runs out of time fails with `driver_timeout` or `jev_unavailable` instead of blocking later calls.
  At shutdown, the final `end_session` to cua-driver waits at most 5 seconds.
- `act` lists the controls its action brought up (a dialog's fields, a menu's items) with their ids
  in `newCandidates`, so the next step needs no `observe`.

### Changed

- Failures that are not a tool's own refusal have their own codes: `driver_unavailable`,
  `driver_timeout`, `driver_error` and `jev_bad_response`, besides `internal`. They are logged at `WARNING` using only the error code and exception type, without response
  bodies, request identifiers or tracebacks. Unexpected failures return `an internal error occurred`.
- A call to an unknown tool is a JSON-RPC error (-32602), not a tool result.
- `pid` and `windowId` must be at least 1.
- Unknown command-line arguments print the usage and exit with status 2 instead of starting the
  server.
- An empty `JEV_API_KEY` no longer hides `TYPESAFE_API_KEY`.
- The exact-text helper also reads switches (`AXSwitch`). Its source changed, so it is rebuilt:
  run `beans-picker grant-ax` again after upgrading.

- Jev endpoints must use HTTPS without credentials, query strings or fragments; redirects are
  rejected. Jev errors no longer echo remote response bodies or unexpected choice values.
- Return and Space require `allowDestructive: true`, as do pop-up choices and freshly rebound
  controls whose current labels or help indicate a destructive action. Common Japanese action
  labels are also recognized. This precaution still depends on the caller granting permission.
- Candidate keys use structured encoding. Candidates with duplicate identities are scoped to the
  current snapshot and are never rebound across snapshots; observe again to obtain a current id.
- Look-alike controls and scroll areas with no named container between them (common on web pages,
  which reach cua-driver as one flat list) take the nearest heading, or else the nearest text no twin
  shares, as part of their identity, so their ids last across snapshots. Twins that neither tells
  apart stay bound to the snapshot.
- `screenText` reads a web list one row per line (a heading and the short texts after it), and
  `observe` shows 60 lines instead of 30.
- Jev is told that a browser's menu-bar commands are not part of the web page it shows.
- A failed or malformed screen-lock check returns `screen_lock_unavailable` and runs nothing.

### Fixed

- A call canceled by the client while it was still waiting for its turn no longer runs.
- A step in `then` that raises no longer discards the steps before it: it is reported as a `failed`
  step, with the completed steps and the `skipped` count.
- A Jev answer with a probability or confidence outside [0, 1], or a ranking without
  probabilities, is rejected as `jev_bad_response`.
- `~` in `CUA_DRIVER_BIN` is expanded.
- Screenshots and exact-text responses are read only from a private per-operation directory.
  Symlinks, hard links, non-regular files, oversized files and paths outside that directory are
  rejected; cleanup never deletes a driver-supplied path outside the directory.
- A server made by `create_server()` without a session closes the session it made when it stops.
- A text field named only by its placeholder keeps its candidate ids once it is typed in, so text
  entered into it is checked in that field instead of ending as `mismatch` ("the target field is gone").

- Secure accessibility fields and their descendants are excluded before ranking and extraction.
  Exact-text values are matched only within a uniquely titled window, with compatible field
  roles, labels and counts; ambiguous or incomplete helper responses are ignored.
- PNG input, dimensions and decompressed data are bounded before use. Malformed, truncated and
  excess image data are rejected.
- Canceled native helpers are killed and reaped before their temporary files are cleaned up.
- API key setup instructions create private directories and files, including when updating an
  existing key file.

[Unreleased]: https://github.com/mimo-3/beans-picker/commits/main
