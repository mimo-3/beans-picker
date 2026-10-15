"""Background-only public capabilities, with exact targets and explicit effect readback."""

from __future__ import annotations

import asyncio
import base64
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp.types import CallToolResult, TextContent

from beans_picker import _json
from beans_picker._json import JsonObject, JsonValue
from beans_picker._text import utf16_len, utf16_units
from beans_picker.driver.mcp import Driver
from beans_picker.driver.sentinel import RESTORE_CHECK_INTERVAL_S, RESTORE_CHECKS
from beans_picker.driver.types import Window, get_int, get_obj, get_str, windows_of
from beans_picker.errors import ToolError
from beans_picker.tools.artifacts import input_path, output_path, plain_folder, publish
from beans_picker.tools.capability_args import browser_url, normalized_url
from beans_picker.tools.driver import _capture_image
from beans_picker.tools.session import Session, Target


async def _call(driver: Driver, tool: str, args: JsonObject) -> JsonObject:
    schemas = getattr(driver, "tool_schemas", None)
    if not isinstance(schemas, Mapping) or tool not in schemas:
        raise ToolError("unknown_driver_tool", "the tool is not in cua-driver's loaded schemas")
    result = await driver.call(tool, args)
    if not result.ok:
        raise ToolError(result.code, result.message)
    if result.data.get("effect") in {"refused", "failed"}:
        raise ToolError("foreground_required", "the driver could not perform the operation in the background")
    return result.data


def _text(args: JsonObject, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ToolError("bad_arguments", f"give a non-empty {key}")
    return value


async def _window(driver: Driver, target: Target) -> Window:
    data = await _call(driver, "list_windows", {"pid": target.pid})
    windows = windows_of(data) or []
    window = next((w for w in windows if w.pid == target.pid and w.window_id == target.window_id), None)
    if window is None:
        raise ToolError("window_not_found", "the exact pid and windowId are not in the live window list")
    return window


async def _target(driver: Driver, args: JsonObject) -> Target:
    pid, window_id = args.get("pid"), args.get("windowId")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or isinstance(window_id, bool)
        or not isinstance(window_id, int)
    ):
        raise ToolError("bad_target", "give both pid and windowId of an already open window")
    target = Target(pid, window_id)
    await _window(driver, target)
    return target


async def capability_tool(session: Session, name: str, args: JsonObject) -> object:
    if name == "record_stop":
        return await session.recorder.stop()
    driver = await session.driver()
    if name == "windows":
        return await _windows(driver, args)
    if name == "launch_app":
        return await _launch(session, driver, args)
    if name == "clipboard_read":
        data = await _call(driver, "clipboard_read", {"include_text": True})
        return {**data, "status": "done"}
    if name == "clipboard_write":
        return await _clipboard(driver, args)
    if name == "screenshot":
        return await _screenshot(session, driver, args)
    if name == "browser_navigate":
        browser_url(args.get("url"))  # refuse a bad URL before any driver call
    target = await _target(driver, args)
    if name == "record_start":
        return await session.recorder.start(target.pid, target.window_id, Path(_text(args, "out")))
    if name == "set_window_frame":
        return await _frame(driver, target, args)
    if name == "browser_state":
        return await _browser_state(session, driver, target, args)
    if name in {"browser_navigate", "browser_dialog"}:
        return await _browser_action(session, driver, target, name, args)
    return await _transfer(session, driver, target, name, args)


async def _clipboard(driver: Driver, args: JsonObject) -> JsonObject:
    if ("text" in args) == ("imagePath" in args):
        raise ToolError("bad_arguments", "give exactly one of text or imagePath")
    if "text" in args:
        payload = {"text": args["text"]}
    else:
        path = await asyncio.to_thread(input_path, _text(args, "imagePath"))
        payload = {"image_path": str(path)}
    await _call(driver, "clipboard_write", payload)
    data = await _call(driver, "clipboard_read", {"include_text": True})
    if "text" in args:
        return {**data, "status": "done" if data.get("text") == args["text"] else "mismatch"}
    # A type list proves that some image is present, not that the supplied pixels were written.
    return {**data, "status": "unverified", "reason": "image pixels cannot be read back by cua-driver"}


async def _screenshot(session: Session, driver: Driver, args: JsonObject) -> CallToolResult:
    scope = args.get("scope", "window")
    if scope == "screen" and ("pid" in args or "windowId" in args):
        raise ToolError("bad_target", "a screen capture cannot also name a window target")
    if args.get("includeImage", True) is False and "out" not in args:
        raise ToolError("bad_arguments", "give out or request an image")
    out = output_path(_text(args, "out"), suffix=".png") if "out" in args else None
    if out is None:
        session.paths.shots.mkdir(parents=True, exist_ok=True)
    directory = out.parent if out is not None else session.paths.shots
    with TemporaryDirectory(prefix="bp-impl-capture-", dir=directory) as stage:
        staged = Path(stage) / "bp-impl-capture.png"
        payload: JsonObject = {"screenshot_out_file": str(staged)}
        if scope == "window":
            target = await _target(driver, args)
            payload.update(pid=target.pid, window_id=target.window_id, max_elements=1, include_screenshot=True)
            tool = "get_window_state"
        else:
            tool = "get_desktop_state"
        # Preserve the full ToolResult for the existing bounded PNG reader.
        schemas = getattr(driver, "tool_schemas", {})
        if tool not in schemas:
            raise ToolError("unknown_driver_tool", "the tool is not in cua-driver's loaded schemas")
        result = await driver.call(tool, payload)
        if not result.ok:
            raise ToolError(result.code, result.message)
        img = await asyncio.to_thread(_capture_image, result, staged)
        data: JsonObject = {"status": "done", "scope": scope}
        if out is not None:
            # _capture_image allows a same-directory added suffix. Publish exactly those verified bytes.
            verified = Path(stage) / "bp-impl-verified.png"
            verified.write_bytes(base64.b64decode(img.data))
            data["bytes"] = await asyncio.to_thread(publish, verified, out)
            data["path"] = str(out)
        return CallToolResult(
            content=[
                TextContent(type="text", text=_json.dumps(data)),
                *([img] if args.get("includeImage", True) else []),
            ],
            structured_content=data,
        )


async def _frame(driver: Driver, target: Target, args: JsonObject) -> JsonObject:
    requested: JsonObject = {key: args[key] for key in ("x", "y", "width", "height")}
    await driver.sentinel.watch(target.pid)
    try:
        await _call(driver, "set_window_frame", {"pid": target.pid, "window_id": target.window_id, **requested})
        window = await _window(driver, target)
        if window.bounds is None:
            raise ToolError("unverified_frame", "the window's frame could not be read back")
        b = window.bounds
        frame: JsonObject = {"x": b.x, "y": b.y, "width": b.width, "height": b.height}
        return {
            "status": "done" if frame == requested else "mismatch",
            "pid": target.pid,
            "windowId": target.window_id,
            "frame": frame,
        }
    finally:
        driver.sentinel.stop()


async def _browser_state(session: Session, driver: Driver, target: Target, args: JsonObject) -> JsonObject:
    chosen = args.get("tabId")
    generation = getattr(driver, "generation", 0)
    # Preserve the exact capabilities returned with an ambiguous tab list. A second bind may mint
    # new ids; selecting the first bind's tab by ordinal or title would silently change authority.
    cached = next(
        (
            (target_id, tab_id)
            for (target_id, tab_id), saved in session.browser_bindings.items()
            if tab_id == chosen and saved == (target, generation)
        ),
        None,
    )
    if cached is not None:
        target_id, tab_id = cached
        snapshot = await _browser_snapshot(driver, target_id, tab_id, args)
        return {
            "status": "done",
            "pid": target.pid,
            "windowId": target.window_id,
            "targetId": target_id,
            "tabId": tab_id,
            "snapshot": snapshot,
        }
    bound = await _call(driver, "get_browser_state", {"pid": target.pid, "window_id": target.window_id})
    if bound.get("binding_quality") != "exact" or bound.get("mutation_allowed") is not True:
        raise ToolError("browser_binding_required", "the driver did not prove an exact mutable browser binding")
    target_id = _text(bound, "target_id")
    tabs = bound.get("tabs")
    ids = [_text(tab, "tab_id") for tab in tabs if isinstance(tab, dict)] if isinstance(tabs, list) else []
    generation = getattr(driver, "generation", 0)
    for tab_id in ids:
        session.browser_bindings[(target_id, tab_id)] = (target, generation)
    if chosen is None:
        if len(ids) != 1:
            return {
                "status": "ambiguous",
                "targetId": target_id,
                "tabs": bound.get("tabs"),
                "message": "repeat browser_state with one exact tabId from this tab list",
            }
        chosen = ids[0]
    if not isinstance(chosen, str) or chosen not in ids:
        raise ToolError("browser_tab_required", "the requested tabId is not in this exact window")
    snapshot = await _browser_snapshot(driver, target_id, chosen, args)
    session.browser_bindings[(target_id, chosen)] = (target, generation)
    return {
        "status": "done",
        "pid": target.pid,
        "windowId": target.window_id,
        "targetId": target_id,
        "tabId": chosen,
        "snapshot": snapshot,
    }


async def _browser_snapshot(driver: Driver, target_id: str, tab_id: str, args: JsonObject) -> JsonObject:
    payload: JsonObject = {"target_id": target_id, "tab_id": tab_id, "snapshot_format": "semantic_v2"}
    if "query" in args:
        payload["query"] = args["query"]
    return await _call(driver, "get_browser_state", payload)


async def _transfer(session: Session, driver: Driver, target: Target, name: str, args: JsonObject) -> JsonObject:
    target_id, tab_id = _binding(session, driver, target, args)
    ref = _text(args, "ref")
    payload: JsonObject = {"target_id": target_id, "tab_id": tab_id, "ref": ref}
    await driver.sentinel.watch(target.pid)
    try:
        if name == "browser_upload":
            files = args.get("files")
            if not isinstance(files, list) or not 1 <= len(files) <= 32 or not all(isinstance(f, str) for f in files):
                raise ToolError("bad_arguments", "give 1 to 32 absolute regular files")
            checked: list[JsonValue] = []
            for file in files:
                if isinstance(file, str):
                    checked.append(str(await asyncio.to_thread(input_path, file)))
            data = await _call(driver, "browser_set_input_files", {**payload, "files": checked})
            snapshot = await _browser_snapshot(driver, target_id, tab_id, {})
            return {
                "status": "unverified",
                "fileCount": data.get("file_count"),
                "snapshot": snapshot,
                "reason": "assignment dispatched; the driver does not provide FileList readback",
            }
        out = output_path(_text(args, "out"))
        with TemporaryDirectory(prefix="bp-impl-download-", dir=out.parent) as stage:
            data = await _call(driver, "browser_download", {**payload, "destination_root": stage})
            download_id = data.get("download_id")
            size = data.get("bytes")
            if (
                data.get("status") != "completed"
                or not isinstance(download_id, str)
                or download_id in {"", ".", ".."}
                or Path(download_id).name != download_id
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                raise ToolError("unverified_output", "the driver did not identify a completed download and byte count")
            await asyncio.to_thread(publish, Path(stage) / download_id, out, expected_bytes=size)
            return {"status": "done", "path": str(out), "bytes": size}
    finally:
        driver.sentinel.stop()


def _binding(session: Session, driver: Driver, target: Target, args: JsonObject) -> tuple[str, str]:
    target_id, tab_id = (_text(args, key) for key in ("targetId", "tabId"))
    binding = session.browser_bindings.get((target_id, tab_id))
    if binding != (target, getattr(driver, "generation", 0)):
        raise ToolError("browser_binding_required", "use browser_state in this session for the same pid and windowId")
    return target_id, tab_id


async def _browser_action(session: Session, driver: Driver, target: Target, name: str, args: JsonObject) -> JsonObject:
    target_id, tab_id = _binding(session, driver, target, args)
    payload: JsonObject = {"target_id": target_id, "tab_id": tab_id}
    if name == "browser_navigate":
        payload["url"] = browser_url(args.get("url"))
    else:
        action = args.get("action")
        if action not in {"inspect", "accept", "dismiss"}:
            raise ToolError("bad_arguments", "give inspect, accept or dismiss as action")
        payload["action"] = action
        if action != "inspect":
            payload["dialog_id"] = _text(args, "dialogId")
        if "promptText" in args:
            if action != "accept" or not isinstance(args["promptText"], str) or utf16_len(args["promptText"]) > 8192:
                raise ToolError("bad_arguments", "promptText is allowed only with accept and at most 8192 UTF-16 units")
            payload["prompt_text"] = args["promptText"]
        if action == "accept" and args.get("allowDestructive") is not True:
            return {"status": "needs_confirmation", "message": "repeat with allowDestructive to accept the dialog"}
    await driver.sentinel.watch(target.pid)
    try:
        data = await _call(driver, name, payload)
        if name == "browser_dialog":
            result: JsonObject = {"status": "done"}
            for native, public in (
                ("present", "present"),
                ("dialog_id", "dialogId"),
                ("kind", "type"),
                ("message", "message"),
                ("default_prompt", "defaultPrompt"),
            ):
                if native in data:
                    result[public] = data[native]
            if args["action"] == "inspect":
                return result
            # Current cua-driver acknowledges resolution with status=ok and the exact id/action.
            resolved = data.get("resolved") is True or (
                "resolved" not in data
                and (
                    data.get("status") == "resolved"
                    or (
                        data.get("status") == "ok"
                        and data.get("dialog_id") == payload["dialog_id"]
                        and data.get("action") == payload["action"]
                    )
                )
            )
            result["status"] = "done" if resolved else "unverified"
            result["dialogId"] = payload["dialog_id"]
            if not resolved:
                result["reason"] = "the driver did not report the exact dialog resolved"
            else:
                await _action_snapshot(driver, target_id, tab_id, result)
            return result
        result = {"status": "unverified", "url": payload["url"]}
        snapshot = await _action_snapshot(driver, target_id, tab_id, result)
        if snapshot is None:
            return result
        page = get_obj(snapshot, "page") or {}
        current_url = get_str(page, "url")
        final_url = get_str(data, "final_url") or get_str(data, "url")
        observed = normalized_url(current_url) if current_url is not None else None
        verified = observed is not None and observed in {
            normalized_url(_text(payload, "url")),
            normalized_url(final_url) if final_url is not None else None,
        }
        result["status"] = "done" if verified else "unverified"
        if current_url is not None:
            result["finalUrl"] = current_url
        if not verified:
            result["reason"] = "the fresh page URL does not match the requested or driver-reported final URL"
        result["snapshot"] = snapshot
        return result
    finally:
        driver.sentinel.stop()


async def _action_snapshot(driver: Driver, target_id: str, tab_id: str, result: JsonObject) -> JsonObject | None:
    try:
        snapshot = await _browser_snapshot(driver, target_id, tab_id, {})
    except ToolError as err:
        result.update(
            status="unverified",
            reason="the fresh snapshot failed; a dialog may be open; use browser_dialog with action=inspect",
            driverCode=err.code,
        )
        return None
    result["snapshot"] = snapshot
    return snapshot


def _window_json(window: Window, *, include_title: bool = True) -> JsonObject:
    b = window.bounds
    result: JsonObject = {
        "pid": window.pid,
        "windowId": window.window_id,
        "app": window.app_name,
        "bounds": {"x": b.x, "y": b.y, "width": b.width, "height": b.height} if b is not None else None,
        "onScreen": window.is_on_screen,
        "onCurrentSpace": window.on_current_space,
        "zIndex": window.z_index,
    }
    if include_title:
        # Avoid leaving half of an astral character at the UTF-16 boundary.
        result["title"] = (
            utf16_units(window.title)[:512].decode("utf-16-le", "ignore") if window.title is not None else None
        )
    return result


async def _windows(driver: Driver, args: JsonObject) -> JsonObject:
    payload = {"pid": args["pid"]} if "pid" in args else {}
    if "onScreenOnly" in args:
        payload["on_screen_only"] = args["onScreenOnly"]
    data = await _call(driver, "list_windows", payload)
    app = _text(args, "app").casefold() if "app" in args else None
    return {
        "status": "done",
        "windows": [
            _window_json(w, include_title="app" in args or "pid" in args)
            for w in windows_of(data) or []
            if ("pid" not in args or w.pid == args["pid"])
            and (app is None or (w.app_name is not None and w.app_name.casefold() == app))
        ],
    }


async def _launch(session: Session, driver: Driver, args: JsonObject) -> JsonObject:
    if "bundleId" not in args and "name" not in args:
        raise ToolError("bad_arguments", "give bundleId or name")
    payload: JsonObject = {}
    for public, native in (("bundleId", "bundle_id"), ("name", "name")):
        if public in args:
            value = _text(args, public)
            if (public == "bundleId" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,254}", value) is None) or (
                public == "name"
                and (
                    value.startswith(("-", "."))
                    or any(ch in "/:" or unicodedata.category(ch) in {"Cc", "Cf"} for ch in value)
                )
            ):
                raise ToolError("bad_arguments", f"invalid {public}")
            payload[native] = value
    if "newInstance" in args:
        payload["creates_new_application_instance"] = args["newInstance"]
    if "urls" in args:
        urls = args["urls"]
        if not isinstance(urls, list) or len(urls) > 16 or not all(isinstance(u, str) for u in urls):
            raise ToolError("bad_arguments", "give at most 16 URLs or canonical absolute paths")
        checked: list[JsonValue] = []
        for url in urls:
            if isinstance(url, str) and url.startswith("-"):
                raise ToolError("bad_arguments", "launch inputs must not start with '-'")
            if isinstance(url, str) and url.startswith("/"):
                checked.append(str(await asyncio.to_thread(plain_folder, url)))
            else:
                checked.append(browser_url(url, about=False))
        payload["urls"] = checked
    previous = await session.front()
    data = await _call(driver, "launch_app", payload)
    pid = get_int(data, "pid")
    for _ in range(RESTORE_CHECKS):
        current = await session.front()
        if data.get("self_activation_suppressed") is False or current != previous:
            restored = (
                previous is not None
                and (previous != pid or current != previous)
                and await session.restore_front(previous)
            )
            raise ToolError(
                "foreground_violation",
                f"launch pid {pid if pid is not None else 'unknown'} violated background focus; "
                "the app in front before was " + ("put back" if restored else "not put back"),
            )
        await session._sleep(RESTORE_CHECK_INTERVAL_S)
    state = get_obj(data, "launch_state") or {}
    windows = [w for w in windows_of(data) or [] if w.pid == pid]
    ready = pid is not None and bool(windows) and state.get("window_ready") is not False
    return {
        "status": "done" if ready else "unverified",
        "pid": pid,
        "bundleId": data.get("bundle_id"),
        "name": data.get("name"),
        "windows": [_window_json(w) for w in windows],
    }
