"""
Wire protocol between Fynn's WindowsDrakeDriver and this agent.

One request = {"id": <n>, "method": <str>, "params": {...}}
One reply   = {"id": <n>, "result": {...}}  OR  {"id": <n>, "error": <str>}

Methods and their params/results mirror the TypeScript DrakeDriver seam exactly
(src/agents/preparer/adapters/drake/driver.ts). Keeping this the single dispatch
point means `selftest` (local plan file) and `connect` (live from Fynn) drive Drake
through identical code.
"""

from __future__ import annotations

from typing import Any

from drake_driver import DrakeDriver


def invoke(driver: DrakeDriver, method: str, params: dict) -> Any:
    if method == "capabilities":
        return driver.capabilities()
    if method == "openReturn":
        return driver.open_return(params["returnRef"])
    if method == "openScreen":
        return driver.open_screen(params["screen"], params.get("instance") or 0)
    if method == "addFormInstance":
        return driver.add_form_instance(params["screen"])
    if method == "focus":
        return driver.focus(params["target"])
    if method == "type":
        return driver.type_text(params["target"], params["text"], params.get("opts"))
    if method == "press":
        return driver.press(params["keys"])
    if method == "readField":
        return driver.read_field(params["target"])
    if method == "screenshot":
        return driver.screenshot()
    raise ValueError(f"unknown method: {method}")


def dispatch(driver: DrakeDriver, req: dict) -> dict:
    """Execute one request, returning a well-formed reply (never raises)."""
    rid = req.get("id")
    try:
        result = invoke(driver, req.get("method", ""), req.get("params") or {})
        return {"id": rid, "result": result}
    except Exception as e:  # protocol/transport failure → error reply
        return {"id": rid, "error": str(e)}
