"""Static indicators read from the recovered instructions. Nothing is executed or fetched."""

from __future__ import annotations

import re

_URL = re.compile(r"(?:https?|wss?|ftp)://[^\s\"'<>\\^`{|}]+", re.IGNORECASE)
_WEBHOOK = re.compile(r"https?://(?:[\w-]+\.)?(?:discord|discordapp)\.com/api/(?:v\d+/)?webhooks/", re.IGNORECASE)
_LOADERS = {"loadstring", "load"}
_NETWORK_METHODS = {"HttpGet", "HttpGetAsync", "HttpPost", "HttpPostAsync", "GetAsync", "PostAsync", "RequestAsync"}
_NETWORK_GLOBALS = {"request", "http_request", "HttpGet"}


def _text(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _unique(items):
    return list(dict.fromkeys(items))


def indicators(ir) -> dict:
    """Summarize strings, globals, methods and coarse behaviors of a normalized program."""
    strings, globals_, methods = [], [], []
    for ins in ir["instructions"]:
        value = ins.get("value")
        if not isinstance(value, bytes):
            continue
        text = _text(value)
        if ins["op"] == "getglobal":
            globals_.append(text)
        elif ins["op"] == "self":
            methods.append(text)
        else:
            strings.append(text)
    urls = _unique(match.rstrip(".,;)") for text in strings for match in _URL.findall(text))
    globals_, methods = _unique(globals_), _unique(methods)
    behaviors = []
    fetches = bool(urls) or bool(_NETWORK_METHODS & set(methods)) or bool(_NETWORK_GLOBALS & set(globals_))
    if _LOADERS & set(globals_) and fetches:
        behaviors.append({
            "id": "remote-code-loader",
            "description": "downloads text and passes it to loadstring/load (heuristic: never verified by running it)",
        })
    if any(_WEBHOOK.match(url) for url in urls):
        behaviors.append({"id": "webhook-url", "description": "contains a Discord webhook endpoint"})
    return {"urls": urls, "globals": globals_, "methods": methods, "strings": _unique(strings), "behaviors": behaviors}
