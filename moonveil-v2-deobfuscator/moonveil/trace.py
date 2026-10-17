"""Symbolic tracing for layouts the static profile does not know.

The program runs in official Luau against an environment where every global that is not
a pure standard-library table is a *symbolic value*. Reading, indexing or calling such a
value never reaches a real API: it appends an event to a log and returns a new symbolic
value. The log is then rendered back to Luau by :func:`moonveil.emit.emit_trace`.

This executes the protected program (unlike the static VM profile), so it is opt-in. It
only recovers what the program does *to its environment* along the path it takes; code that
branches on, computes with or stores environment values beyond passing them along is
rejected with an explanation rather than guessed.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

from .errors import MoonVeilError
from .runtime import DEFAULT_MEMORY_MB, run_bounded

MAX_EVENTS = 20000

_TRACER = r'''
-- Capture everything the sandbox needs before the program can touch it.
local real = getfenv()
local rawset, setmetatable, getmetatable, type, tostring = rawset, setmetatable, getmetatable, type, tostring
local select, pcall, error, ipairs, pairs = select, pcall, error, ipairs, pairs
local format, byte, gsub, concat = string.format, string.byte, string.gsub, table.concat
local report, loadchunk, huge = print, loadstring, math.huge

local events, tablesSeen, proxies = {}, {}, setmetatable({}, {__mode = "k"})
local nextProxy, nextTable, failure = 0, 0, nil

local function hex(text)
    return (gsub(text, ".", function(c) return format("%02x", byte(c)) end))
end

local function fail(message)
    failure = failure or message
    error(message, 0)
end

local function record(line)
    events[#events + 1] = line
    if #events > __MAX_EVENTS__ then fail("the program produced too many operations") end
end

local function atom(value, depth)
    local kind = type(value)
    if value == nil then return "Z"
    elseif kind == "boolean" then return value and "B1" or "B0"
    elseif kind == "number" then
        if value ~= value or value == huge or value == -huge then fail("non-finite number passed to the environment") end
        return "N" .. format("%.17g", value)
    elseif kind == "string" then return "S" .. hex(value)
    elseif kind == "table" then
        if proxies[value] then return "P" .. proxies[value] end
        if tablesSeen[value] then return "T" .. tablesSeen[value] end
        if (depth or 0) > 8 or nextTable >= 256 then fail("table passed to the environment is too large or too deep") end
        nextTable += 1
        local id = nextTable
        tablesSeen[value] = id
        record("T\t" .. id)
        for key, item in pairs(value) do
            record("E\t" .. id .. "\t" .. atom(key, (depth or 0) + 1) .. "\t" .. atom(item, (depth or 0) + 1))
        end
        return "T" .. id
    end
    fail("a " .. kind .. " value was passed to the environment; only constants and tables are supported")
end

local symbolic = {}
local function newSymbol()
    nextProxy += 1
    local proxy = setmetatable({}, symbolic)
    proxies[proxy] = nextProxy
    return proxy, nextProxy
end

symbolic.__metatable = "symbolic"
symbolic.__index = function(self, key)
    local proxy, id = newSymbol()
    record("I\t" .. id .. "\t" .. atom(self) .. "\t" .. atom(key))
    return proxy
end
symbolic.__call = function(self, ...)
    local count = select("#", ...)
    local parts = {}
    for position = 1, count do parts[position] = atom((select(position, ...))) end
    local proxy, id = newSymbol()
    record("C\t" .. id .. "\t" .. atom(self) .. "\t" .. count .. (count > 0 and "\t" .. concat(parts, "\t") or ""))
    return proxy
end
symbolic.__tostring = function() return "<symbolic>" end
for _, name in ipairs({"__newindex", "__len", "__unm", "__add", "__sub", "__mul", "__div", "__mod", "__pow",
                       "__idiv", "__concat", "__lt", "__le"}) do
    symbolic[name] = function() fail("the program computes with a value from the environment (" .. name .. ")") end
end

local allowed = {
    string = string, table = table, bit32 = bit32, math = math, utf8 = utf8, buffer = buffer,
    select = select, type = type, typeof = typeof, tostring = tostring, tonumber = tonumber,
    pairs = pairs, ipairs = ipairs, next = next, pcall = pcall, xpcall = xpcall, error = error,
    assert = assert, rawget = rawget, rawset = rawset, rawequal = rawequal, rawlen = rawlen,
    setmetatable = setmetatable, getmetatable = getmetatable, collectgarbage = collectgarbage,
    unpack = table.unpack, _VERSION = "Luau",
    os = {clock = os.clock, time = os.time, date = os.date, difftime = os.difftime},
}
local sandbox
allowed.getfenv = function() return sandbox end
allowed.setfenv = function(target) return target end
sandbox = setmetatable({}, {__index = function(_, name)
    local value = allowed[name]
    if value ~= nil then return value end
    if type(name) ~= "string" then fail("global with a non-string name") end
    local proxy, id = newSymbol()
    record("G\t" .. id .. "\t" .. hex(name))
    return proxy
end})

local chunk, problem = loadchunk(__SOURCE__, "=protected")
local ok, result = false, problem
if chunk then
    setfenv(chunk, sandbox)
    ok, result = pcall(chunk)
end
report("MOONVEIL_TRACE_BEGIN")
for _, line in ipairs(events) do report(line) end
report("S\t" .. (chunk and (ok and "ok" or "error") or "compile"))
if failure then report("F\t" .. hex(failure)) end
if not ok then report("X\t" .. hex(tostring(result))) end
report("MOONVEIL_TRACE_END")
'''


def _lua_string(data: bytes) -> str:
    """A Luau string literal for arbitrary bytes; the program text can never escape it."""
    return '"' + "".join(chr(b) if chr(b).isalnum() and b < 128 else f"\\x{b:02x}" for b in data) + '"'


def instrument(source: str) -> str:
    # Single pass: the program text is data and may itself contain placeholder strings.
    return _TRACER.replace("__MAX_EVENTS__", str(MAX_EVENTS)).replace("__SOURCE__", _lua_string(source.encode("utf-8")))


def _atom(token: str):
    tag, body = token[:1], token[1:]
    try:
        if token == "Z":
            return {"type": "nil"}
        if tag == "B" and body in {"0", "1"}:
            return {"type": "boolean", "value": body == "1"}
        if tag == "S":
            bytes.fromhex(body)
            return {"type": "bytes", "hex": body}
        if tag == "P":
            return {"type": "proxy", "id": int(body)}
        if tag == "T":
            return {"type": "table", "id": int(body)}
        if tag == "N":
            number = float(body)
            if not math.isfinite(number):
                raise MoonVeilError("non-finite number in trace")
            return {"type": "number", "value": int(number) if number.is_integer() else number}
    except ValueError as exc:
        raise MoonVeilError("malformed trace record") from exc
    raise MoonVeilError(f"unknown trace atom: {token[:40]!r}")


def _decode_text(value: str) -> str:
    return bytes.fromhex(value).decode("utf-8", errors="replace")


def parse_trace(stdout: str):
    lines = stdout.splitlines()
    if lines.count("MOONVEIL_TRACE_BEGIN") != 1 or lines.count("MOONVEIL_TRACE_END") != 1:
        raise MoonVeilError("the tracer did not produce one complete log")
    begin, end = lines.index("MOONVEIL_TRACE_BEGIN"), lines.index("MOONVEIL_TRACE_END")
    if end <= begin:
        raise MoonVeilError("invalid trace boundary")
    events, tables, status, failure, error, seen = [], {}, None, None, None, set()
    for line in lines[begin + 1:end]:
        fields = line.split("\t")
        kind = fields[0]
        try:
            if kind == "G" and len(fields) == 3:
                event = {"op": "G", "id": int(fields[1]), "name": bytes.fromhex(fields[2])}
            elif kind == "I" and len(fields) == 4:
                event = {"op": "I", "id": int(fields[1]), "base": _atom(fields[2]), "key": _atom(fields[3])}
            elif kind == "C" and len(fields) >= 4 and len(fields) == 4 + int(fields[3]):
                event = {"op": "C", "id": int(fields[1]), "fn": _atom(fields[2]), "args": [_atom(f) for f in fields[4:]]}
            elif kind == "T" and len(fields) == 2:
                tables.setdefault(int(fields[1]), [])
                continue
            elif kind == "E" and len(fields) == 4:
                tables.setdefault(int(fields[1]), []).append((_atom(fields[2]), _atom(fields[3])))
                continue
            elif kind == "S" and len(fields) == 2:
                status = fields[1]
                continue
            elif kind == "F" and len(fields) == 2:
                failure = _decode_text(fields[1])
                continue
            elif kind == "X" and len(fields) == 2:
                error = _decode_text(fields[1])
                continue
            else:
                raise MoonVeilError("malformed trace record")
        except ValueError as exc:
            raise MoonVeilError("malformed trace record") from exc
        if event["id"] in seen:
            raise MoonVeilError("duplicate symbolic value in trace")
        seen.add(event["id"])
        events.append(event)
    if status not in {"ok", "error", "compile"}:
        raise MoonVeilError("trace has no completion status")
    return {"schema": "moonveil-trace-v1", "status": status, "failure": failure, "error": error,
            "events": events, "tables": tables}


def trace(source: str, luau: Path, timeout: float, memory_mb: int = DEFAULT_MEMORY_MB):
    """Run ``source`` against the symbolic environment and return its operation log."""
    with tempfile.TemporaryDirectory(prefix="moonveil-trace-") as directory:
        script = Path(directory) / "trace.luau"
        script.write_text(instrument(source), encoding="utf-8")
        stdout, _ = run_bounded([str(luau), str(script)], cwd=script.parent, timeout=timeout,
                                memory_mb=memory_mb, label="luau")
    log = parse_trace(stdout)
    if log["status"] == "compile":
        raise MoonVeilError(f"the input is not valid Luau: {log['error']}")
    if log["failure"]:
        raise MoonVeilError(f"tracing stopped: {log['failure']}. Only programs that pass environment values along "
                            "(calls, method calls, arguments) can be traced")
    if log["status"] == "error":
        raise MoonVeilError(f"the program raised an error while tracing: {log['error']}")
    if not log["events"]:
        raise MoonVeilError("the program never touched its environment; there is nothing to recover")
    return log


def trace_ir(log):
    """A minimal instruction list so :func:`moonveil.analysis.indicators` can summarize a trace."""
    instructions = []

    def add(op, value):
        instructions.append({"pc": len(instructions) + 1, "op": op, "value": value})

    def strings(atom, depth=0):
        if atom["type"] == "bytes":
            add("loadk", bytes.fromhex(atom["hex"]))
        elif atom["type"] == "table" and depth < 8:
            for key, value in log["tables"].get(atom["id"], []):
                strings(key, depth + 1)
                strings(value, depth + 1)

    for event in log["events"]:
        if event["op"] == "G":
            add("getglobal", event["name"])
        elif event["op"] == "I":
            if event["key"]["type"] == "bytes":
                add("self", bytes.fromhex(event["key"]["hex"]))
        else:
            for atom in event["args"]:
                strings(atom)
    return {"instructions": instructions}


def log_json(log):
    """JSON-safe copy of a trace: byte strings are kept losslessly as hex."""
    events = []
    for event in log["events"]:
        copy = dict(event)
        if copy["op"] == "G":
            copy["name"] = {"hex": event["name"].hex(), "text": event["name"].decode("utf-8", errors="replace")}
        events.append(copy)
    tables = [{"id": key, "entries": [{"key": k, "value": v} for k, v in entries]} for key, entries in sorted(log["tables"].items())]
    return {"schema": log["schema"], "status": log["status"], "events": events, "tables": tables}
