# Security

## What leaves your machine

beans-picker sends data over the network only when a step needs Jev: `observe` with an `instruction`,
`act` without a `candidateId`, and `extract`. `observe` without an instruction and `act` with a
`candidateId` send nothing.

Each such step makes a request to the configured Jev endpoint (`TYPESAFE_BASE_URL`, by default
`https://api.typesafe.ai`), authenticated with your `JEV_API_KEY`. The request carries:

- the `instruction` and any `text` you passed;
- the app name, the window title, the kind and title of an open dialog or menu, and the titles of
  the app's other windows;
- up to 20 lines of the window's text and up to 12 editable fields with their values;
- for each candidate action or element offered to Jev: its role, label, identifier, help text,
  current value (for a table or list, its first rows), the names of the groups it sits in, and its
  menu path.

Treat any window you point beans-picker at as data you are willing to send to the Jev endpoint.

## What runs locally

- cua-driver (`CUA_DRIVER_BIN`, default `~/.local/bin/cua-driver`) is started as a subprocess and
  does the clicking and typing. It holds its own Accessibility and Screen Recording permissions.
- Two small helpers are compiled from the Objective-C sources shipped in the package
  (`beans_picker/native/axtext.m`, `beans_picker/native/menukeys.m`) with `clang`, on first use, into
  `~/Library/Caches/beans-picker`. Nothing compiled is downloaded.
- The `axtext` helper is a separate background app, "beans-picker axtext", that reads text fields and
  checkboxes read-only and skips secure (password) fields. It holds its own Accessibility grant (`beans-picker grant-ax`), so your
  terminal or agent needs none. The `menukeys` helper reads an app bundle's menu shortcuts from
  its nib and needs no permission.

## Where secrets are read from

The Jev key is read from the environment (`JEV_API_KEY`, or `TYPESAFE_API_KEY`), then from
`.env.local` and `.env` in the beans-picker checkout the server runs from (only when it is one), then
from `.env.local` and `.env` in `~/.config/beans-picker` (`$XDG_CONFIG_HOME/beans-picker`). A value that is
already set is never overridden. Keep these files out of version control; `.gitignore` excludes
`.env*` except `.env.example`.

The server logs to stderr only. The Jev SDK's own logging is turned off, since its request bodies
carry window text.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
[Report a vulnerability](https://github.com/mimo-3/beans-picker/security/advisories/new). Do not open
a public issue. Include the version (`beans-picker --version`), what you did, and what happened.
