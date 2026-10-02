# Security

## What leaves your machine

beans-picker sends data over the network only when a step needs Jev: `observe` with an `instruction`,
`act` without a `candidateId`, and `extract`. `observe` without an instruction and `act` with a
`candidateId` send nothing.

Each such step makes a request to the configured Jev endpoint (`TYPESAFE_BASE_URL`, by default
`https://api.typesafe.ai`), authenticated with your `JEV_API_KEY`. The endpoint must use HTTPS,
without credentials, query or fragment in the URL; redirects are refused. The request carries:

- the `instruction` and any `text` you passed;
- the app name, the window title, the kind and title of an open dialog or menu, and the titles of
  the app's other windows;
- up to 20 lines of the window's text and up to 12 editable fields with their values;
- for each candidate action or element offered to Jev: its role, label, identifier, help text,
  current value (for a table or list, its first rows), the names of the groups it sits in, and its
  menu path.

Treat any window you point beans-picker at as data you are willing to send to the Jev endpoint.
Accessibility nodes marked as secure fields and their descendants are excluded from snapshots.
This cannot identify secrets displayed in ordinary text fields. Caller-supplied `instruction` and
`text` are still sent when Jev is used; use `candidateId` to avoid that transmission.

## What runs locally

- cua-driver (`CUA_DRIVER_BIN`, default `~/.local/bin/cua-driver`) is started as a subprocess and
  does the clicking and typing. It holds its own Accessibility and Screen Recording permissions.
- Four small helpers are compiled from the Objective-C sources shipped in the package
  (`axtext.m`, `menukeys.m`, `activate.m` and `winrec.m` in `beans_picker/native`) with `clang`, on
  first use, into `~/Library/Caches/beans-picker`. Nothing compiled is downloaded.
- The `axtext` helper is a separate background app, "beans-picker axtext", that reads text fields and
  checkboxes read-only and skips secure (password) fields. It holds its own Accessibility grant (`beans-picker grant-ax`), so your
  terminal or agent needs none. The `menukeys` helper reads an app bundle's menu shortcuts from
  its nib and needs no permission.
- The `activate` helper brings one app to the front by pid. beans-picker gives it only the app that
  was in front before the app under test took its place, never the app under test, and it needs no
  permission.
- The `winrec` helper runs only for `beans-picker record`. It writes a video of one window to the
  file you name, under the Screen Recording permission of the terminal or agent that started it. The
  command refuses a file that already exists.

## Where secrets are read from

The Jev key is read from the environment (`JEV_API_KEY`, or `TYPESAFE_API_KEY`), then from
`.env.local` and `.env` in the beans-picker checkout the server runs from (only when it is one), then
from `.env.local` and `.env` in `~/.config/beans-picker` (`$XDG_CONFIG_HOME/beans-picker`). A value that is
already set is never overridden. Keep these files out of version control; `.gitignore` excludes
`.env*` except `.env.example`.

Create the configuration directory with mode 0700 and key files with mode 0600 (see README).
An existing file keeps its old permissions when overwritten, so apply `chmod 600` explicitly.
Environment variables and these files are trusted configuration: they can select an alternate
HTTPS endpoint or a different driver executable. Do not load configuration from an untrusted checkout.

The server logs to stderr only. Tool failure logs contain the error code and exception type, not
tracebacks; helper and cleanup diagnostics also omit exception messages. Jev errors omit response bodies and exception messages, which can echo screen text or
credentials. The Jev SDK's own logging is turned off, since its request bodies carry window text.

## Temporary data and action boundaries

Screenshots and AX helper output use unpredictable, owner-only operation directories. Normal
completion, errors and cancellation clean them up. Screenshot response paths outside the owned
directory, symlinks, hardlinks and non-regular files are rejected. A process crash or forced kill can
leave private directories behind; remove stale `shots/capture-*` and `axtext/read-*` directories only
when no beans-picker process is using them. The cache is local storage, not encrypted storage.

Exact text is only paired with a matching window title and compatible fields; an absent or ambiguous
match remains unverified. The helper counts every window, including windows without field values,
and refuses supplementation for duplicate titles. Secure fields never receive exact-text
supplementation. Identical controls can still limit what accessibility metadata can distinguish.

The MCP client is trusted to authorize its own actions. `allowDestructive` is supplied by that client,
not proof of human approval. Label-based destructive detection cannot establish what an arbitrary app
will do. Return and Space require confirmation, and pop-up items are checked before they are pressed.
Indistinguishable controls are bound to their current tokens; an obsolete target is refused rather
than rebound to a different identical control. A lock-check failure also refuses the tool call.

The helpers and driver run with the user's privileges. This project does not isolate a malicious app
bundle, a modified local executable/cache, or another process already running as the same user.
Local helper/system subprocesses do not inherit `JEV_API_KEY` or `TYPESAFE_API_KEY`. This is not
a sandbox: those processes still have ordinary filesystem access, including to user-owned files.
The menu helper loads the selected application's nib; use trusted applications and keep the cache
and executables writable only by their owner. These are distinct from the temporary-file protections.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
[Report a vulnerability](https://github.com/mimo-3/beans-picker/security/advisories/new). Do not open
a public issue. Include the version (`beans-picker --version`), what you did, and what happened.
