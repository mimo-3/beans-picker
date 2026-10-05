# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.5.0] - 2026-10-05

### Added

- `beans-picker record --app <app> --out <file.mp4>` records one window on its own until Ctrl-C
  (macOS 15 or later), so a demo video does not show the app coming to the front or a window
  covering it.
- `BEANS_PICKER_PIXEL=off` sends no pixel clicks: every click is an accessibility press, and a click
  with modifier keys returns `failed` with the new code `pixel_disabled`.
- When the app under test comes to the front during `act`, or takes the front while it is launched,
  the app that was in front before is put back.
- `act` accepts `dragTo` with a destination `candidateId` or a `dx`/`dy` pixel offset, including
  in `then`. Drags use background delivery, check both endpoints, require confirmation for
  destructive controls under either actual endpoint for candidate and offset destinations. Verification
  requires a bounded 0.3-second quiet interval; an unsettled result remains `unverified`.
  Movement is window-relative; snapbacks do not count, while enabled-state changes do.
- The opt-in `driver` tool (`BEANS_PICKER_RAW_DRIVER=1`) forwards cua-driver calls without Jev,
  effect verification or destructive confirmation. Screen-lock, background-only and safe capture
  checks still apply. Only allowlisted app-control tools are accepted; input calls require an integer
  `pid` and an activation watch. `launch_app` is not allowlisted. `page.cdp_port` and `page.bundle_id`
  are refused with `driver_argument_disallowed`. Desktop scope, browser-preference changes through `page`, and
  file-output arguments other than redirected `screenshot_out_file` are refused. Captures use
  `shots/capture-*`. Failures are sanitized; path-key values and owned cache paths are redacted,
  preserving sibling paths that share only a directory prefix and unrelated URLs.

### Changed

- The `foreground_violation` message ends with whether the app in front before was put back.
- A window smaller than 100 × 50 points is passed over when the app has another: macOS puts such a
  control on a window while it is being recorded, and it was picked as the app's window.

## [0.4.0] - 2026-09-29

### Added

- `act` lists the controls its action brought up (a dialog's fields, a menu's items) with their ids
  in `newCandidates`, so the next step needs no `observe`. When a step changed the controls or much of
  the text, the result also carries the window's `screenText`, and `newCandidates` lists as many as a
  plain `observe` does (`newTotal` counts the rest).
- After a click, `act` takes snapshots until two in a row offer the same controls (a few at most), so a
  page that is still loading is reported as it lands.
- Unnamed square buttons in a row of three or more on a web page (a colour picker) are described by the
  colour they show, read from a capture of the window, and Jev is told that colour.
- The server instructions describe the fast loop: one `observe`, then every known step in one `act`
  with `then`, and no extra checks of a step that is `done`.

### Changed

- Text fields, search fields and text areas on a web page are typed into instead of written through
  AXValue: a page's own copy of a value (a React form's state) follows typing only. Text with a line
  break is still written, since a typed Return would submit a form or send a message.
- Failures that are not a tool's own refusal are logged at `WARNING` with only the error code and
  exception type, without response bodies, request identifiers or tracebacks. Unexpected failures
  return `an internal error occurred`.
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
  apart (icon-only buttons side by side) hold their rank among their twins and a digest of the whole
  window: their ids last while nothing on the window changes, even though Chrome renews every
  element's token on each snapshot, and all change with any change on the window.
- On a web page, buttons, links, checkboxes and similar controls are clicked at their centre when
  nothing but their own content and containers lies over it (about 0.2 s instead of about 2 s for
  an AX press).
- Short texts on a web page that no control holds and that appear once are offered as clicks, after
  every control: pages often make a label chip or a custom menu entry clickable without a role.
- `screenText` reads a web list one row per line (a heading and the short texts after it), and
  `observe` shows 60 lines instead of 30.
- Jev is told that a browser's menu-bar commands are not part of the web page it shows.
- A failed or malformed screen-lock check returns `screen_lock_unavailable` and runs nothing.

### Fixed

- Screenshots and exact-text responses are read only from a private per-operation directory.
  Symlinks, hard links, non-regular files, oversized files and paths outside that directory are
  rejected; cleanup never deletes a driver-supplied path outside the directory.
- Secure accessibility fields and their descendants are excluded before ranking and extraction.
  Exact-text values are matched only within a uniquely titled window, with compatible field
  roles, labels and counts; ambiguous or incomplete helper responses are ignored.
- PNG input, dimensions and decompressed data are bounded before use. Malformed, truncated and
  excess image data are rejected.
- Canceled native helpers are killed and reaped before their temporary files are cleaned up.
- API key setup instructions create private directories and files, including when updating an
  existing key file.

## [0.3.0] - 2026-09-27

The first release on PyPI. Versions 0.1 and 0.2 were never published.

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

### Changed

- Failures that are not a tool's own refusal have their own codes: `driver_unavailable`,
  `driver_timeout`, `driver_error` and `jev_bad_response`, besides `internal`. They are logged with
  their traceback at `WARNING`.
- A call to an unknown tool is a JSON-RPC error (-32602), not a tool result.
- `pid` and `windowId` must be at least 1.
- Unknown command-line arguments print the usage and exit with status 2 instead of starting the
  server.
- An empty `JEV_API_KEY` no longer hides `TYPESAFE_API_KEY`.
- The exact-text helper also reads switches (`AXSwitch`). Its source changed, so it is rebuilt:
  run `beans-picker grant-ax` again after upgrading.

### Fixed

- A call canceled by the client while it was still waiting for its turn no longer runs.
- A step in `then` that raises no longer discards the steps before it: it is reported as a `failed`
  step, with the completed steps and the `skipped` count.
- A Jev answer with a probability or confidence outside [0, 1], or a ranking without
  probabilities, is rejected as `jev_bad_response`.
- `~` in `CUA_DRIVER_BIN` is expanded.
- A screenshot that cua-driver saved to a path of its own is deleted on every failure path too.
- A server made by `create_server()` without a session closes the session it made when it stops.
- A text field named only by its placeholder keeps its candidate ids once it is typed in, so text
  entered into it is checked in that field instead of ending as `mismatch` ("the target field is gone").

[Unreleased]: https://github.com/mimo-3/beans-picker/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/mimo-3/beans-picker/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/mimo-3/beans-picker/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/mimo-3/beans-picker/releases/tag/v0.3.0
