"""App and window resolution. Launching goes through cua-driver's `launch_app`, which never
activates the app."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cua_jev._aio import Sleep
from cua_jev._proc import Runner, run
from cua_jev._text import WORD_CLASS_BODY, WS_CLASS_BODY
from cua_jev.config import APP_BUNDLES
from cua_jev.driver.sentinel import PID_LINE
from cua_jev.driver.types import LaunchAppArgs, ListWindowsArgs, Window, get_int, get_str, windows_of
from cua_jev.errors import AppLaunchError

if TYPE_CHECKING:
    from cua_jev.driver.mcp import Driver

# At least three dot-separated parts of ASCII word characters and hyphens: `com.apple.TextEdit`.
_BUNDLE_ID: Final = re.compile(f"[{WORD_CLASS_BODY}-]+(?:\\.[{WORD_CLASS_BODY}-]+){{2,}}\\Z")
# A LaunchServices application serial number, as `lsappinfo find` prints it.
_ASN: Final = re.compile(f'ASN:[^{WS_CLASS_BODY}:"]+')

# A just-launched app shows its window after a moment: the window list is read again, this many
# times, this far apart.
WINDOW_CHECKS: Final = 34
WINDOW_CHECK_INTERVAL_S: Final = 0.15


@dataclass(frozen=True, slots=True, kw_only=True)
class AppTarget:
    bundle_id: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AppContext:
    pid: int
    window_id: int
    app_name: str
    launched_by_us: bool
    """True only if the app was not running before it was launched here."""


def resolve_bundle(app: str | None) -> str | None:
    """A bundle id as given, or the bundle of a known app name (`"calculator"`)."""
    if not app:
        return None
    if _BUNDLE_ID.match(app):
        return app
    return APP_BUNDLES.get(app.lower())


async def running_pid(target: AppTarget, *, runner: Runner = run) -> int | None:
    """The pid of a running app, from LaunchServices (`lsappinfo`).

    cua-driver's `list_apps` and `get_accessibility_tree` walk every app's accessibility tree, and
    one unresponsive app makes them hang; LaunchServices answers without asking any app.
    """
    query = f"bundleid={target.bundle_id}" if target.bundle_id is not None else f"name={target.name or ''}"
    try:
        found = await runner(("lsappinfo", "find", query))
        asn = _ASN.search(found.stdout)
        if asn is None:
            return None
        out = await runner(("lsappinfo", "info", "-only", "pid", asn.group(0)))
    except Exception:
        return None
    m = PID_LINE.search(out.stdout)
    return int(m.group(1)) if m else None


def pick_window(windows: Sequence[Window], title: str | None = None) -> Window | None:
    """The window matching the title hint, else the front-most on-screen layer-0 window.

    A titled window goes before an untitled one: an untitled layer-0 surface (a transient overlay
    the app left on screen) can have no accessibility window behind it at all.
    """
    usable = [w for w in windows if (w.layer if w.layer is not None else 0) == 0]
    if title:
        return next((w for w in usable if title in (w.title or "")), None)
    shown = [w for w in usable if w.is_on_screen is not False]
    shown.sort(key=lambda w: (not w.title, -(w.z_index if w.z_index is not None else -1)))
    return shown[0] if shown else None


@dataclass(frozen=True, slots=True)
class _Launched:
    pid: int
    name: str | None
    windows: list[Window]


async def ensure_app(
    driver: Driver,
    target: AppTarget,
    *,
    runner: Runner = run,
    sleep: Sleep = asyncio.sleep,
) -> AppContext:
    """The target app's pid and window, launching it in the background when needed.

    Raises AppLaunchError when the app cannot be launched or shows no window.
    """
    running = await running_pid(target, runner=runner)
    launched: _Launched | None = None
    # launch_app costs ~3 s even for a running app; skip it when the app already shows a usable window.
    if running is not None:
        windows = await _list_windows(driver, running)
        if pick_window(windows) is not None:
            launched = _Launched(running, None, windows)
    # launch_app starts the app in the background and undoes any activation the app requests itself.
    if launched is None:
        args: LaunchAppArgs = {}
        if target.bundle_id:
            args["bundle_id"] = target.bundle_id
        elif target.name is not None:
            args["name"] = target.name
        r = await driver.call("launch_app", args)
        # The launch can land while the driver's reply does not (its daemon gives up on a reply
        # after a while): what counts is whether the app is running now.
        pid = get_int(r.data, "pid") if r.ok else await running_pid(target, runner=runner)
        if pid is None:
            wanted = target.bundle_id if target.bundle_id is not None else target.name
            reason = "no pid" if r.ok else r.message
            raise AppLaunchError(f"could not launch {_shown(wanted)}: {reason}")
        launched = (
            _Launched(pid, get_str(r.data, "name"), windows_of(r.data) or []) if r.ok else _Launched(pid, None, [])
        )
    win = await _wait_for_window(driver, launched.pid, launched.windows, sleep)
    if win is None:
        raise AppLaunchError(f"no window for pid {launched.pid}")
    name = next((n for n in (launched.name, win.app_name, target.name) if n is not None), "")
    return AppContext(pid=launched.pid, window_id=win.window_id, app_name=name, launched_by_us=running is None)


def _shown(value: str | None) -> str:
    """A possibly absent name as it appears in a message: an absent one reads `undefined`."""
    return "undefined" if value is None else value


async def _list_windows(driver: Driver, pid: int) -> list[Window]:
    args: ListWindowsArgs = {"pid": pid}
    windows = windows_of(await driver.must("list_windows", args))
    if windows is None:
        raise AppLaunchError(f"list_windows returned no window list for pid {pid}")
    return windows


async def _wait_for_window(driver: Driver, pid: int, initial: list[Window], sleep: Sleep) -> Window | None:
    """Check the window list a fixed number of times, reading it again after each check."""
    windows = initial
    for _ in range(WINDOW_CHECKS):
        if (w := pick_window(windows)) is not None:
            return w
        await sleep(WINDOW_CHECK_INTERVAL_S)
        windows = await _list_windows(driver, pid)
    return None
