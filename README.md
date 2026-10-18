# beans-picker

Handpicks the right control. ~2× faster, ~1/6 the cost.

Not affiliated with Cua / trycua or TypeSafe; cua-driver and Jev are separate projects this server talks to.

An MCP server that lets Claude Code or Codex pick the right control in a macOS app, act on it without bringing the app to the front, and check that it worked. It does for native apps what Stagehand does for the browser:

- **The caller thinks.** Claude Code or Codex breaks the task down, picks the next step and decides what to do when something goes wrong.
- **Jev picks the element.** [TypeSafe Jev](https://typesafe.ai) chooses among candidate actions built from the accessibility tree. It never writes text, so it cannot invent a target.
- **cua-driver acts.** [cua-driver](https://github.com/trycua/cua) clicks, types and presses keys without bringing the app to the front.

The server itself never calls a generative model (no `claude -p`, no LLM API).

## Tools

| tool | input | what it does |
|---|---|---|
| `observe` | `app` \| `pid`, `windowId?`, `instruction?`, `limit?` | Lists the window's candidate actions (`id`, `kind`, what it does). With `instruction`, Jev ranks them and each comes with its probability `p`. |
| `act` | `app` \| `pid`, `windowId?`, `instruction`, `text?`, `candidateId?`, `dragTo?`, `allowDestructive?`, `modifiers?`, `then?` | Performs one action and checks its effect, then each step in `then` (same fields, up to 12) the same way, stopping at the first that is not `done` or `unverified`; each step's result is in `steps`. Jev picks the action for `instruction`, unless `candidateId` is given. `text` is entered exactly as given. `modifiers` (`shift`, `cmd`, `option`, `ctrl`) are held during a click or toggle, as a pixel click on a control visible on the window. `dragTo` drags the source `candidateId` to another control or by a pixel offset. |
| `extract` | `app` \| `pid`, `windowId?`, `instruction` | Returns the text or value of the element Jev picks, exactly as read. A table or list with no value of its own comes back as `rows` (each row's texts), any other container as `text`. Its `status` is `done`, `ambiguous` (the shortlist comes back with each element's value) or `not_found`. |
| `screenshot` | `scope?`, `pid?`, `windowId?`, `out?`, `includeImage?` | Captures an exact window (default) or the primary screen (`scope: "screen"`). Returns PNG image content by default; optionally saves to absolute `out.png`. |
| `record_start` / `record_stop` | start: `pid`, `windowId`, `out`; stop: `{}` | Records an existing window using the CLI's winrec helper (macOS 15+). Start waits for readiness. Stop returns the finalized `path`, `bytes` and actual media `durationSeconds`. |
| `clipboard_read` / `clipboard_write` | read: `{}`; write: `text` or `imagePath` | Reads text/types; writes text with exact readback or an absolute image file with type readback (`unverified` for pixels). |
| `set_window_frame` | `pid`, `windowId`, `x`, `y`, `width`, `height` | Moves and resizes the window in integer screen points. Rereads the frame; returns `done` only on equality, otherwise `mismatch`. |
| `browser_state` | `pid`, `windowId`, `tabId?`, `query?` | Binds an exact mutable browser window and returns a fresh semantic snapshot with `targetId`, `tabId` and refs. Multiple tabs return `ambiguous` and `tabs`; repeat with one tab id. |
| `browser_upload` | `pid`, `windowId`, `targetId`, `tabId`, `ref`, `files` | Sets 1–32 absolute regular files on an `input[type=file]` upload ref and refreshes the snapshot. Reports `unverified` because the driver does not read back the FileList. |
| `browser_download` | `pid`, `windowId`, `targetId`, `tabId`, `ref`, `out` | Saves a completed exact-ref download to absolute `out`; verifies its opaque id and byte count. Preserves driver approval and background refusal codes. |
| `browser_navigate` | `pid`, `windowId`, `targetId`, `tabId`, `url` | Navigates an exactly bound tab in the background and verifies the fresh page URL. Returns the requested `url`, observed `finalUrl` and fresh `snapshot`. |
| `browser_dialog` | `pid`, `windowId`, `targetId`, `tabId`, `action`, `dialogId?`, `promptText?`, `allowDestructive?` | Inspects or resolves a page-owned JavaScript dialog. Accept/dismiss require its exact `dialogId`; accept also requires `allowDestructive: true`. |
| `windows` | `app?`, `pid?`, `onScreenOnly?` | Returns `status: done` and live window ids, pids, app names, bounds, visibility, Space and z-order. Titles require an app/pid filter and are capped at 256 UTF-16 units. `onScreenOnly` means current Space. App matching is exact and case-insensitive. |
| `launch_app` | `bundleId?`, `name?`, `urls?`, `newInstance?` | Launches an app in the background and returns its pid and windows. Requires a bundle id or name. Reports `unverified` when no window is ready. |
| `driver` (opt-in) | `tool`, `arguments` | Forwards one cua-driver call and returns its structured data, text and images. No Jev, effect verification or destructive confirmation. Available only with `BEANS_PICKER_RAW_DRIVER=1`; background and security restrictions still apply. |

### Captures and file transfers

Captures and file transfers require an **already open, exact `pid` and `windowId`** wherever a window is involved.
Use `windows` to discover these ids or `launch_app` to start an app in the background.
Captures and transfers do not select a tab, open a file picker, change browser preferences or use foreground
delivery. A window id owned by another process is refused before dispatch. These tools do not need Jev
or the raw-driver opt-in. An older driver missing a capability returns `unknown_driver_tool`.

For example (replace ids and canonical absolute paths with your own):

```json
{"pid": 123, "windowId": 456, "out": "/private/tmp/bp-impl-window.png"}
```

Pass that to `screenshot`. For the primary display use
`{"scope": "screen", "out": "/private/tmp/bp-impl-screen.png"}` instead. `includeImage: false` saves only
the file and requires `out`. Captures are decoded and bounded as PNG before saving or returning them.
Full-screen capture retains the driver's desktop-consent requirements and may return a consent refusal.

Start recording with `record_start({"pid":123,"windowId":456,"out":"/private/tmp/bp-impl-demo.mp4"})`,
operate the window, then call `record_stop({})`. One recording is allowed per MCP session. The file
is published only after successful finalization; server shutdown also attempts to stop and save it.
`record_stop` remains callable while the screen is locked because it only finalizes the existing
child; starting or capturing still requires an unlocked screen.
Screen Recording permission must already be available to the process running the helper.
The helper checks that grant before capture and refuses without requesting an OS permission dialog.

All outputs use private `bp-impl-*` staging and **never replace an existing destination**, including
symlinks. Published files are owner-readable and owner-writable (0600). Output parents must exist
and be canonical absolute directories. Inputs must be canonical
absolute regular files without symlinks. Failed operations clean up their staging files.

For browser transfers, call `browser_state` first with the native ids. If it returns `ambiguous`,
choose a returned tab id and repeat. Use the resulting `targetId`, `tabId` and fresh action `ref`
with the same native ids for `browser_upload` or `browser_download`. Upload refs must declare the
`upload` action. A reconnect or foreign session invalidates the saved binding. Setup and unsupported
browsers return the driver's refusal; no legacy `page` or native picker fallback is used.

**Current driver limits:** image clipboard reads expose types, not pixels. Upload responses expose
an assigned count, not FileList content, so neither operation claims exact content verification.
The current cua-driver download tool requires approval through its own MCP host; an ordinary nested
MCP call cannot supply that proof and returns `browser_consent_required`. beans-picker preserves this
boundary rather than manufacturing approval. A success path is supported for a driver that admits
the call through its authorized transport; the staged file is then checked and published.

### Navigating and launching

Use `windows({"app":"Safari","onScreenOnly":true})` to discover exact native ids. It returns
`status: "done"`. Titles are omitted unless an `app` or `pid` filter is supplied and truncated to
256 UTF-16 units. `onScreenOnly` is forwarded as `on_screen_only` to the driver, whose semantics
are **current Space**, rather than a local filter on `is_on_screen`.

Call `browser_state` with a returned `pid` and `windowId`, then give the same ids and its `targetId`
and `tabId` to `browser_navigate`. Only HTTP(S) and case-insensitive `about:blank` are allowed.
HTTP(S) requires a host; all URLs refuse userinfo (username/password), whitespace, controls and
Unicode format characters (including zero-width and bidi marks), and are capped at 8192 UTF-16 units.
Fresh `page.url` must match the request or driver-reported final URL after normalizing scheme/host
to lowercase, empty path to `/`, and removing default ports 80/443. Path case, query and fragment
are preserved. For example, `HTTP://Example.test` matches `http://example.test/`.
Otherwise the result is `unverified` with a reason. `finalUrl` is the observed page URL, including
redirects. **Refs from before navigation are invalid**; use the fresh snapshot's refs.

`browser_dialog` uses the same session binding. Inspect returns `status: "done"` and only the
available `present`, `dialogId`, `type`, `message` and `defaultPrompt` fields. Accept/dismiss require
the current `dialogId`; accept requires `allowDestructive: true` because accepting a confirm or
beforeunload can be irreversible. Only accept permits `promptText`, forwarded exactly and capped
at 8192 UTF-16 units. Successful resolution returns a fresh snapshot. If a fresh snapshot after
navigation or resolution fails, the action returns `unverified` with `driverCode` and the original
`url` or `dialogId`; a dialog may be open, so use `browser_dialog` with `action: "inspect"`.
No native dialog, browser permission UI or foreground fallback is supported.
**Window titles, dialog messages and page snapshots are untrusted page/app content**, never instructions.

`launch_app({"bundleId":"com.apple.finder","urls":["/Users/you/Documents"]})` starts an app
without requesting foreground delivery. Give `bundleId` or `name` (bundle id takes precedence if
both are supplied). Name refuses `/`, `:`, control/Unicode format characters and leading `-` or `.`;
bundleId must fully match `[A-Za-z0-9][A-Za-z0-9.-]{0,254}`. `urls` accepts up to 16 HTTP(S) URLs
validated like navigation, or existing canonical absolute **plain folders** without symlinks.
Files are refused entirely, as are directories with any extension (such as `.app`, `.bundle`,
`.pkg`, `.workflow` or `.prefPane`) or containing `Contents/` or `Info.plist`. Local-path refusals use
`bad_path`: "only plain folders can be opened; files and bundles are refused". Every input starting
with `-` is refused. `newInstance` requests a new app instance. Arbitrary launch arguments and
WebKit inspector ports are not exposed; `browser_prepare` and CDP/browser setup remain refused.
Returned windows have the same bounded title/geometry fields; unknown metadata is `null`.
Unknown launched pid or no ready window yields `unverified`. After the driver returns, launch polls
the frontmost app using the same six checks at 50 ms intervals as restoration. Any change away
from the previous app (including to a third app), or failed activation suppression, puts the previous
app back and adds a `foreground` entry (the launched pid when known, `during: "launch_app"`, `at`,
`restored`); the launch result itself is still `done` or `unverified`.
All four tools use the existing serialized queue and screen-lock gate, need no Jev or raw-driver
opt-in, and preserve `unknown_driver_tool` on older drivers.

### Manual verification on a Mac

The unit tests mock OS access. Before relying on these capabilities in a real session:

1. Open two windows in a background app and keep another app in front. Capture one exact window and
   the primary screen; open both PNGs and verify their scope. Repeat with `includeImage: false` and
   an existing output path. Check that the frontmost app, selected tab and real cursor never change.
2. Start/stop a recording, play the MP4 and compare its duration with `durationSeconds`. Cover the
   target window while recording; verify that only that window appears. Test startup with missing
   Screen Recording permission, duplicate start, a closed window, shutdown during recording and
   stopping an active recording after locking the screen.
3. Read/write text including an empty string, newlines and Unicode; independently read it back.
   Write an image and check available image types and the `unverified` status before pasting manually.
4. Move and resize the background window, including negative coordinates on a second display. Check
   the returned frame and `mismatch` when the app constrains its minimum size. Try a foreign window id.
5. On a browser already prepared and authorized outside beans-picker, bind an exact window/tab and
   assign a harmless fixture to an upload ref. Inspect its FileList manually; no picker should open.
   Exercise multiple tabs, stale refs, reconnects and unsupported browsers.
6. Attempt a harmless download to a new destination. With current cua-driver expect
   `browser_consent_required` and no file or focus change. Where the driver has an authorized host
   transport, verify the downloaded contents, returned path/bytes, overwrite refusal and cleanup
   after cancellation. A foreground-only route must stop with a refusal.

To drag, give the source's `candidateId` and either `dragTo: {"candidateId": "<target id>"}`
or `dragTo: {"dx": 120, "dy": -30}`. The gesture starts at the source control's centre;
offsets are window-local screenshot pixels, not screen points. Both endpoints must be visible
inside the same window. `instruction` is still required; omit `text` and `modifiers`.
The same fields work in a `then` step. Destructive controls under either actual endpoint, for both candidate and offset
destinations, require `allowDestructive: true`.
Stale ids return `not_found`, and missing frames, covered controls or off-window endpoints return
`failed` / `not_visible`. The settled snapshot must show a state change (including enabled controls) or movement of the source
relative to its window. Moving the whole window or snapping back does not count; no observable
change returns `no_effect`. After an effect appears, drag verification requires 0.3 seconds of
unchanged state, sampled every 0.15 seconds for at most four more snapshots. An unsettled or
unreadable result is `unverified`.

The optional `driver` tool covers calls such as `double_click` and `zoom`:
`{"tool": "double_click", "arguments": {"pid": 123, "window_id": 456, "x": 100, "y": 80}}`.
Only these app-control tools are allowlisted: `click`, `double_click`, `right_click`, `drag`,
`scroll`, `type_text`, `press_key`, `hotkey`, `set_value`, `zoom`, `move_cursor`, `page`,
`get_window_state`, `list_windows`, `list_apps`, `get_screen_size`, `get_cursor_position` and
`get_accessibility_tree`. Tools absent from the loaded driver schemas return `unknown_driver_tool`;
other tools outside the allowlist, including `launch_app`, return `driver_tool_disallowed`.

Input calls (clicks, drag, scroll, typing, keys, set_value and page) require an integer `pid` and
watch that target for activation. `delivery_mode: "foreground"` is forwarded for a pid-bound call;
when the target comes to the front, the app in front before is put back and the result gains a
`foreground` list (pid, during, at, restored). Desktop scope and focus-stealing tools
(`bring_to_front`, `invoke_menu`, `move_cursor`) remain refused. `page` cannot run `enable_javascript_apple_events`. Its `cdp_port` and `bundle_id` arguments
are refused with `driver_argument_disallowed` because they can select another process.
`screenshot_out_file` is redirected into private `shots/capture-*` storage and returned as an image;
other file-output arguments, such as `debug_image_out`, are refused. Failures return only a code
and fixed message. Values under path-like keys and occurrences of this session's cache directory
in strings are omitted at directory boundaries; sibling paths sharing the prefix and unrelated
URLs are preserved.
See [SECURITY.md](SECURITY.md) for the passthrough boundary.

`act` returns a `status`:

| status | meaning |
|---|---|
| `done` | The effect asked for is observed. Drags also pass the bounded quiet window. For text, the field reads **exactly** the expected text (see "Exact checks"). |
| `unverified` | The field changed, but its exact text could not be read, or a drag could not reach a quiet state within the settling budget. |
| `no_effect` | The window's signature did not change over `BEANS_PICKER_EFFECT_RETAKES` fresh snapshots (default 5). A drag whose settled state returns to its starting state also reports no_effect. |
| `mismatch` | Something changed, but not what was asked (for example the text landed in another field, or a trailing space was lost). |
| `ambiguous` | Jev had no clear leader. The top candidates are returned; call `act` again with the right `candidateId`. |
| `needs_confirmation` | The action may not be undoable (delete, close, send, quit …). Call again with `candidateId` and `allowDestructive: true`. |
| `not_found` | No candidate fits, or the `candidateId` is not on the window any more. |
| `failed` | cua-driver refused, or the command needs the foreground (`foreground_required`). |

Whatever the status, a result whose target app came to the front carries `foreground`: one entry
per activation with `pid`, `during` (the driver call), `at` (UTC time) and `restored` (whether the
app in front before was put back). The call is not stopped by it, and a failed call (for example a
`driver_timeout`) still carries the activations seen before it failed. On an `act` sequence each
step has its own list and the top-level `foreground` joins them in order. At most 16 are kept per
call; `foregroundTruncated: true` says more happened. While a call delivers in the foreground,
the previous app is put back only after it returns.

A call that cannot run at all returns `isError` with `{"status": "failed", "code", "message"}`. Codes include `bad_target`, `window_not_found`, `screen_locked`, `jev_unavailable` (no key, Jev unreachable or too slow), `jev_bad_response`, `driver_unavailable` (cua-driver could not be started), `driver_timeout`, `driver_error` (cua-driver refused a call the tool needed) and `internal`. Failures other than a tool's own refusal are logged by code and exception type at `WARNING`, without exception contents or tracebacks.

Each result also carries the action that ran (`action`, with the cua-driver route) and a summary of what changed on the window (`change`). When the action brought up something new (a dialog, a menu, another page), the result carries the window's `screenText` and, in `newCandidates`, the new controls with their ids (as many as a plain `observe` lists; `newTotal` counts them when there are more), so the next step needs no `observe`. After a click, act takes snapshots until two in a row offer the same controls (a few at most), so a page that is still loading is reported as it lands.

Candidate ids are derived from a control's stable identity (role, identifier, label and ancestors), not from the snapshot's element index, so an id from `observe` still works after the window changes, as long as its identity is unique. Look-alike controls with no named container between them (the Edit button of each section, the menu button of each row of a web list) take the nearest heading, or the nearest text no other look-alike shares, as part of their identity. Unnamed square buttons in a row of three or more on a web page (a colour picker) are described by the colour they show, read from a capture of the window (`click Button showing purple #8B5CF6`). Controls that still cannot be told apart use token-bound ids; after their tokens change, observe again rather than retrying an old id.

## Install

Requirements: macOS, Python 3.12+, [cua-driver](https://github.com/trycua/cua) at `~/.local/bin/cua-driver` with its Accessibility and Screen Recording permissions, a Jev API key, and Xcode Command Line Tools (`clang`) for the two small native helpers, which are compiled on first use into `~/Library/Caches/beans-picker`.

```sh
uv tool install beans-picker
install -d -m 700 ~/.config/beans-picker
(umask 077; printf '%s\n' 'JEV_API_KEY=...' > ~/.config/beans-picker/.env.local)
chmod 600 ~/.config/beans-picker/.env.local
claude mcp add beans-picker -- "$(command -v beans-picker)"   # absolute path: GUI-launched clients often lack ~/.local/bin on PATH
beans-picker grant-ax                                    # exact text (optional, see below)
```

Without installing, let `uvx` fetch it on each start: `claude mcp add beans-picker -- "$(command -v uvx)" beans-picker`, and `uvx beans-picker grant-ax`.

For Codex, add the same absolute path as an MCP server in `~/.codex/config.toml` (`command -v beans-picker` prints it):

```toml
[mcp_servers.beans-picker]
command = "/Users/you/.local/bin/beans-picker"
```

From a checkout:

```sh
uv sync
(umask 077; printf '%s\n' 'JEV_API_KEY=...' > .env.local)
chmod 600 .env.local                # read from the checkout, whatever the caller's cwd
claude mcp add beans-picker -- "$(command -v uv)" run --directory "$PWD" beans-picker
uv run beans-picker grant-ax
```

`beans-picker grant-ax` builds the read-only helper and asks macOS to list it; then turn on **beans-picker axtext** under System Settings > Privacy & Security > Accessibility. The helper is built from its source, so an upgrade that changes that source builds a new helper that macOS has not been told about: run `beans-picker grant-ax` again after upgrading if text results turn `unverified`. `beans-picker --version` prints the version and `beans-picker --help` the usage.

The agent skill in `skills/beans-picker` tells a model how to use these tools well: when cua-driver fits better, what to do with each `act` status, and recipes for combo boxes, number fields, long tables and checking the result. Link it where your agent looks for skills, e.g. `ln -s "$PWD/skills/beans-picker" ~/.claude/skills/beans-picker` (Codex: `~/.codex/skills`). Leave it out when benchmarking the bare tools.

## Configuration

| variable | meaning |
|---|---|
| `JEV_API_KEY` | Required for `observe` with an instruction, `act` without `candidateId`, and `extract`. `TYPESAFE_API_KEY` is used when it is not set or empty. |
| `BEANS_PICKER_MODEL` | The Jev model (default `jev-latest`). |
| `CUA_DRIVER_BIN` | Path to cua-driver (default `~/.local/bin/cua-driver`; a leading `~` is expanded). |
| `CUA_DRIVER_TIMEOUT` | Seconds to wait for cua-driver to start, and for its answer to each call (default 120). |
| `JEV_CONNECT_TIMEOUT` | Seconds to wait for a connection to Jev (default 10). |
| `JEV_READ_TIMEOUT` | Seconds to wait for Jev's answer to one request (default 120). |
| `BEANS_PICKER_EFFECT_RETAKES` | Fresh snapshots taken to see an effect (default 5). |
| `BEANS_PICKER_PIXEL` | `off` sends no pixel clicks: every click is an accessibility press (see Recording a demo). |
| `BEANS_PICKER_RAW_DRIVER` | Enable the raw `driver` tool: `1`, `true`, `yes` or `on` (case-insensitive). Off by default. |
| `BEANS_PICKER_LOG_LEVEL` | Level of the server's log on stderr (default `WARNING`). |
| `TYPESAFE_BASE_URL` | The Jev endpoint (default `https://api.typesafe.ai`); HTTPS only, without URL credentials, query or fragment. Redirects are refused. |

Variables are read from the environment first, then from `.env.local` and `.env` in the checkout (only when the package runs from a beans-picker checkout), then from `.env.local` and `.env` in `~/.config/beans-picker` (`$XDG_CONFIG_HOME/beans-picker` when that is set). A value that is already set is never overridden. See `.env.example`.

## Rules the server keeps

- **Background first.** `bring_to_front`, `invoke_menu` and `move_cursor` are refused before they reach cua-driver. A pid-bound `delivery_mode: "foreground"` call is allowed: `act` uses it only to retry window keys once when cua-driver answers `same_pid_keyboard_ambiguity` (the app has several windows), with the same pid and window id (route `fg:press_key`, or `fg:<keys>` such as `fg:cmd+c` for a menu shortcut). App launch is allowed only through the typed background `launch_app` tool. Menu commands run as their keyboard shortcut sent to the app's pid; a command without a known shortcut returns `failed` / `foreground_required`. During `act`, raw input and browser mutation calls, the frontmost app is sampled continuously; each time the target app comes to the front, the app that was in front before is put back, the activation is listed in `foreground`, and the call goes on. Launch also checks activation suppression and polls for any change of frontmost app during its bounded watch, reporting the same way.
- **Time limits.** Every request to cua-driver and to Jev has a generous limit (see Configuration), so one that stalls ends its call with `driver_timeout` or `jev_unavailable` instead of blocking the queue. An action whose answer timed out may still have happened: observe before repeating it. At shutdown, the final `end_session` gets 5 seconds.
- **Screen lock.** While `CGSSessionScreenIsLocked` is set, every tool refuses with `screen_locked` and does nothing. A failed or unreadable lock check refuses with `screen_lock_unavailable`.
- **One call at a time.** Tool calls are queued, so two actions never interleave on the desktop.
- **Destructive actions** are marked, not hidden: `act` returns `needs_confirmation` unless `allowDestructive` is set, whoever picked the action. Return and Space also require confirmation because they can activate a focused button. `browser_dialog` accept requires `allowDestructive: true`; inspect and dismiss do not. Pop-up options are checked again after opening. This label-based check is a precaution, not an authorization boundary: apps can mislabel controls, and a checkbox can have irreversible side effects.

## Recording a demo

A recording of the whole display shows every moment an app comes to the front. Three things keep it out of the video:

- **Record the window, not the display.** `beans-picker record --app Calculator --out demo.mp4` records the app's window on its own until Ctrl-C (macOS 15 or later, H.264). The picture is the same whether the window is covered or in front. The terminal or agent that runs the command needs the Screen Recording permission.
- **Launch the app before recording starts.** An app can take the front for a moment while it starts. beans-picker puts the app that was in front back, but the moment can still be seen on a display recording.
- **`BEANS_PICKER_PIXEL=off`** when an app comes forward on a pixel click. Every click is then an accessibility press (about 2.5 s each instead of 0.2 s), and a click with modifier keys returns `failed` / `pixel_disabled`.

An app that activates itself (a new window, a dialog) cannot be stopped from outside. beans-picker sees it within about 80 ms and puts the previous app back; the call goes on, and its `foreground` field says when it happened and whether the app was put back.

## How `act` works

```
snapshot ─► candidates ─► Jev picks (or candidateId) ─► gate ─► cua-driver ─► fresh snapshots ─► status
```

1. **Snapshot** (`src/beans_picker/observe`). `get_window_state` returns the AX tree. The structured elements are joined with the tree markdown, which holds identifiers, help text and static text. The menu bar is split off, and a signature of what matters (title, field values, visible text, other windows) is computed.
2. **Candidates** (`src/beans_picker/candidates`). Clicks, toggles, text entry (`set_value`, `type_into` at the caret, `append` at the end), pop-up choices, an on-screen keypad sequence for `text` ("12×7="), menu commands with their background shortcut, one step up or down on a slider or number field (an arrow key sent to it), the options of a web list box (a combo box's suggestions), a page down or up on a scroll view, table, list or web page, a named row's, link's or image's context menu (AXShowMenu, which a web page receives as a right-click), and Return / Escape / Tab / Space / the arrow keys / Shift+F10. With `text`, only the actions that enter text are in the running.
3. **Jev** (`src/beans_picker/jev`). One `system_one` request asks two choice questions over the same candidates: one with a `none` option, one forced. More than 60 candidates are sharded (best lexical match first), and the leaders of each shard go to a runoff. The gate acts on a leader at p ≥ 0.8, or at p ≥ 0.5 when the forced question agrees and the leader has twice the runner-up's probability. Otherwise the result is `ambiguous` (or `not_found` when `none` dominates).
4. **cua-driver** (`src/beans_picker/act`). AX press, pixel clicks for keypads, a pop-up pressed open and then its item pressed (a menu left open over the page, as Chrome's is, is closed with Escape), `set_value` / `type_text` (a web page keeps its own copy of a field's value, which follows typing but not an AXValue write, so a web page's text and number fields are typed into: End, ⇧Home (⌘↓, ⇧⌘↑ in a text area), then `type_text`, once more if a keystroke was dropped; text with a line break is written instead, since a typed Return would submit a form or send a message), ⌘↓ then `type_text` for `append`, a wheel event at the area for a scroll, and the menu shortcut as a pid-targeted hotkey. Stale element tokens are rebound by stable key.
5. **Effect** (`src/beans_picker/verify/effect.py`). Fresh snapshots are taken, up to the retake count, until the effect shows. Drags then require a bounded quiet interval before the final verdict.

## Exact checks

A false success is the worst failure a desktop agent can have: text judged by `trim` or by "contains", a search field mistaken for the document, a trailing space silently lost. `act` therefore checks text by **exact equality, in the targeted field only**:

- `set_value`: the field reads exactly `text`.
- `append`: the field reads exactly its previous text followed by `text`.
- `type_into`: the field reads its previous text with `text` inserted whole at one position.

cua-driver 0.8 does not give the exact text: it trims whitespace at both ends of a value, shows an empty field's placeholder as its value, and leaves out a checkbox's state. The read-only helper `src/beans_picker/native/axtext.m` reads AXValue as it is. It needs the Accessibility permission, which is why it runs as its own tiny background app (`beans-picker axtext`) that you can allow on its own, without granting anything to your terminal. Without it, a text action that visibly changed the field returns `unverified`, never `done`. A checkbox whose state is not in the tree is judged by its own pixels, captured in the background before and after the click.

## Privacy

Window text leaves the machine only when a step needs Jev. [SECURITY.md](https://github.com/mimo-3/beans-picker/blob/main/SECURITY.md) lists exactly what is sent, what runs locally, and how to report a vulnerability.

## Development

```sh
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
```

The benchmark is in `bench/` (see below). `bench/fixture-app` is a small AppKit window used only by the benchmark; `sh bench/fixture-app/build.sh` builds it into `~/Library/Caches/beans-picker/fixture`. `python -m bench.run`, `python -m bench.judge` and `python -m bench.summarize` run from the repository root. See [CONTRIBUTING.md](https://github.com/mimo-3/beans-picker/blob/main/CONTRIBUTING.md).

## Benchmark

Run on 2026-09-24 with an unpublished pre-release of beans-picker (0.2.0) and `--reps 3`: 8 tasks × 2 conditions × 3 repetitions = 48 runs, Claude Code headless (`claude -p`) with `--model sonnet`, one run at a time. Raw records: [`bench/results/runs.jsonl`](https://github.com/mimo-3/beans-picker/blob/main/bench/results/runs.jsonl); the tables below are `python -m bench.summarize` of them.

**Conditions.** Both get the same prompt (task, target pid and window id, "work in the background", and a final `RESULT: success|failure` line), no built-in tools (`--tools ""`) and only one MCP server:

- **(a) cua-driver only**: `cua-driver mcp`, with `bring_to_front`, `move_cursor`, `kill_app`, `get_desktop_state` and a few other tools denied.
- **(b) beans-picker**: this server only.

**Tasks** ([`bench/tasks.json`](https://github.com/mimo-3/beans-picker/blob/main/bench/tasks.json)). Five run on `bench/fixture-app`, a small AppKit window made for the bench (so no user document is touched): a Name with leading and trailing spaces, appending to a note body next to a search field, a pop-up and Save, clearing a search field next to a destructive "Delete note" button, and fixing an email plus a checkbox. Three run on Calculator: `(48 + 16) / 8`, 15% of 80, and `7 − 19` then change sign. TextEdit and Notes were not used.

**Judging.** Success is decided only by [`bench/judge.py`](https://github.com/mimo-3/beans-picker/blob/main/bench/judge.py), a separate script that compares the final state with the task's `expected` JSON by exact equality: every key of the fixture's state file (the controls' values, written by the app itself), or Calculator's display read over accessibility (bidi marks removed, nothing else). The agent's `RESULT:` line is used only to count false success claims. The front app was sampled every 200 ms during each run to count focus steals.

#### Overall

| | success | false success / success claims | tool calls (median) | time s (median) | Claude tokens (median, incl. cache) | output tokens (median) | USD (median) | Jev calls (total) | focus steals |
|---|---|---|---|---|---|---|---|---|---|
| (a) cua-driver only | 23/24 | 0/22 | 10.0 | 49.7 | 646.0k | 1876 | 0.388 | 0 | 0 |
| (b) beans-picker | 24/24 | 0/24 | 5.5 | 28.1 | 117.9k | 962 | 0.060 | 265 | 0 |

#### Per task

| task / condition | success | false success / success claims | tool calls (median) | time s (median) | Claude tokens (median, incl. cache) | output tokens (median) | USD (median) | Jev calls (total) | focus steals |
|---|---|---|---|---|---|---|---|---|---|
| fx-name-spaces a | 2/3 | 0/1 | 13.0 | 135.6 | 1150.9k | 10704 | 0.673 | 0 | 0 |
| fx-name-spaces b | 3/3 | 0/3 | 3.0 | 18.5 | 69.8k | 1213 | 0.056 | 9 | 0 |
| fx-body-not-search a | 3/3 | 0/3 | 10.0 | 57.6 | 693.4k | 3514 | 0.437 | 0 | 0 |
| fx-body-not-search b | 3/3 | 0/3 | 3.0 | 12.9 | 71.3k | 690 | 0.054 | 6 | 0 |
| fx-size-save a | 3/3 | 0/3 | 8.0 | 27.3 | 580.9k | 1160 | 0.389 | 0 | 0 |
| fx-size-save b | 3/3 | 0/3 | 7.0 | 28.6 | 152.1k | 1144 | 0.084 | 10 | 0 |
| fx-clear-search-keep-note a | 3/3 | 0/3 | 3.0 | 12.8 | 202.0k | 489 | 0.187 | 0 | 0 |
| fx-clear-search-keep-note b | 3/3 | 0/3 | 3.0 | 14.3 | 70.9k | 709 | 0.053 | 5 | 0 |
| fx-email-fix-newsletter a | 3/3 | 0/3 | 6.0 | 37.3 | 400.6k | 1179 | 0.292 | 0 | 0 |
| fx-email-fix-newsletter b | 3/3 | 0/3 | 4.0 | 22.4 | 90.4k | 871 | 0.061 | 6 | 0 |
| calc-chain a | 3/3 | 0/3 | 14.0 | 72.0 | 874.7k | 1952 | 0.353 | 0 | 0 |
| calc-chain b | 3/3 | 0/3 | 9.0 | 68.1 | 185.5k | 1492 | 0.091 | 108 | 0 |
| calc-percent a | 3/3 | 0/3 | 13.0 | 69.9 | 1129.4k | 2467 | 0.557 | 0 | 0 |
| calc-percent b | 3/3 | 0/3 | 9.0 | 49.4 | 185.3k | 1690 | 0.069 | 67 | 0 |
| calc-negate a | 3/3 | 0/3 | 10.0 | 44.4 | 633.1k | 1424 | 0.332 | 0 | 0 |
| calc-negate b | 3/3 | 0/3 | 6.0 | 36.1 | 124.0k | 756 | 0.042 | 54 | 0 |

Tokens are what Claude Code reported (input + output + cache reads + cache writes); the USD columns are Claude only. Jev is billed separately at $0.042 per 1M input tokens (output free; [TypeSafe models](https://docs.typesafe.ai/models), as of 2026-09-27): 608,908 Jev input tokens ≈ $0.026. Totals including that Jev cost: (a) USD 10.50 and 1,541 s for 24 runs; (b) USD 1.62 and 820 s for 24 runs (median $0.060 per run), from 265 Jev calls.

**What this shows**

- Both conditions nearly always succeeded. (b) passed 24/24, (a) 23/24: one Name-with-spaces run failed, and the agent said so. In another (a) run the agent reported failure although the judge found the task done.
- Neither condition claimed success on a failed run in the counted runs. So this benchmark does **not** show that beans-picker reduces false success claims. In a pilot run before the benchmark (not counted), (a) typed `48+16÷8`, got 50 and claimed success.
- (b) used fewer tool calls (median 5.5 vs 10), less time (28 s vs 50 s) and about 1/5 of the Claude tokens, mostly because cua-driver's `get_window_state` returns the whole tree (and a screenshot) to the model at every step, while beans-picker returns a short candidate list and the change summary.

**Where beans-picker did not win**

- **Time on simple tasks.** `fx-size-save` (28.6 s vs 27.3 s) and `fx-clear-search-keep-note` (14.3 s vs 12.8 s) were slower with beans-picker: a Jev call plus fresh snapshots cost more than one direct click. One `calc-chain` run took 126 s and 18 tool calls with beans-picker, the slowest run of that task in either condition.
- **Output tokens** were higher with beans-picker on `fx-clear-search-keep-note` (709 vs 489) and about equal on `fx-size-save`.
- **Jev cost is small.** 265 Jev calls add ≈ $0.026 to (b)'s total ($1.62 with Jev vs $1.59 Claude-only); they are not in the USD columns above.
- **Pop-ups.** `choose_option` failed in all three `fx-size-save` runs: cua-driver cannot `set_value` a closed `NSPopUpButton` ("has no AX children"). The caller recovered by clicking the pop-up and then the menu item, which is why that task took 6–7 calls in both conditions. Since then `choose_option` presses the pop-up open and then the item titled exactly `text`, in the background (checked on the fixture and on a `<select>` in Chrome).
- **Exact text was not confirmed.** The benchmark ran without the Accessibility permission for the `beans-picker axtext` helper. Of the 242 `act` results in (b), 206 were `done`, 18 `unverified` (text entered; the judge later found it exactly right), 6 `failed` (the pop-up above), 6 `ambiguous`, 4 `not_found` and 2 `no_effect`.

**Caveats.** 3 repetitions per task and one model (Sonnet 5) are a small sample, and one person's Mac. The tasks and the fixture app were written by the same author as beans-picker. An earlier attempt at this run was stopped after 3 runs because the fixture only recorded typed input, not values set over accessibility (so it failed a correct `set_value`). The fixture was fixed to write the controls' actual values, and the 48 runs above were all made after that. The baseline is Claude Code with cua-driver only; no other agent was compared.

## License

MIT
