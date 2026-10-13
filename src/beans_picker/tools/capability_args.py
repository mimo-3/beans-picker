"""The bounded public surface for captures, local state and exact browser file transfers."""

from beans_picker._json import JsonObject, JsonValue


def schema(properties: JsonObject, *required: str) -> JsonObject:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": properties,
        "required": list[JsonValue](required),
        "additionalProperties": False,
    }


ID: JsonObject = {"type": "integer", "minimum": 1, "maximum": 9007199254740991}
TEXT: JsonObject = {"type": "string"}
TARGET: JsonObject = {"pid": ID, "windowId": ID}
BROWSER: JsonObject = {**TARGET, "targetId": TEXT, "tabId": TEXT, "ref": TEXT}

CAPABILITY_SCHEMAS: dict[str, JsonObject] = {
    "screenshot": schema(
        {
            **TARGET,
            "scope": {"type": "string", "enum": ["window", "screen"]},
            "out": TEXT,
            "includeImage": {"type": "boolean"},
        }
    ),
    "record_start": schema({**TARGET, "out": TEXT}, "pid", "windowId", "out"),
    "record_stop": schema({}),
    "clipboard_read": schema({}),
    "clipboard_write": schema({"text": TEXT, "imagePath": TEXT}),
    "set_window_frame": schema(
        {
            **TARGET,
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "width": ID,
            "height": ID,
        },
        "pid",
        "windowId",
        "x",
        "y",
        "width",
        "height",
    ),
    "browser_state": schema({**TARGET, "tabId": TEXT, "query": TEXT}, "pid", "windowId"),
    "browser_upload": schema(
        {
            **BROWSER,
            "files": {"type": "array", "items": TEXT, "maxItems": 32},
        },
        "pid",
        "windowId",
        "targetId",
        "tabId",
        "ref",
        "files",
    ),
    "browser_download": schema({**BROWSER, "out": TEXT}, "pid", "windowId", "targetId", "tabId", "ref", "out"),
}

CAPABILITY_DESCRIPTIONS: dict[str, str] = {
    "screenshot": "Capture an existing exact window (pid and windowId) or the primary screen (scope=screen). "
    "Return a PNG image by default and optionally save to absolute out without overwriting. Never activates apps.",
    "record_start": "Start recording one existing pid/windowId to absolute out.mp4 using winrec (macOS 15+). "
    "Wait for capture readiness; one recording per MCP session. Never launches or activates an app.",
    "record_stop": "Stop this session's recording and verify the finalized file. Return path, bytes and actual "
    "video durationSeconds. Shutdown also stops recording. Never overwrites existing files.",
    "clipboard_read": "Read system clipboard text and available types without pasting or changing focus.",
    "clipboard_write": "Write exactly one of text or absolute imagePath to the system clipboard and read back. "
    "Text requires exact equality. Image type readback is reported unverified because pixels cannot be read.",
    "set_window_frame": "Move and resize one existing pid/windowId in screen points. Read its frame again; "
    "return done only for exact equality, otherwise mismatch. No title-bar dragging or foreground fallback.",
    "browser_state": "Bind an existing exact pid/windowId and read a semantic_v2 browser snapshot with refs. "
    "Give tabId when several tabs exist. Requires exact binding and mutation_allowed; never prepares a browser.",
    "browser_upload": "Assign 1..32 absolute regular files to an input[type=file] upload ref from browser_state "
    "using its targetId/tabId and original pid/windowId. Bypass native file pickers; refresh the snapshot afterward. "
    "Returns unverified because the driver provides assigned count without FileList readback.",
    "browser_download": "Download through an exact ref from browser_state to absolute out without overwriting. "
    "Verify the completed download id and bytes in private staging. Driver MCP-host consent requirements and "
    "foreground_required refusals are preserved; no host approval is fabricated and no browser setup is performed.",
}
