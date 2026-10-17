"""Capture decoded VM data before any protected instruction is dispatched."""

from __future__ import annotations

import json
import math
import re
import tempfile
from pathlib import Path

from .errors import MoonVeilError
from .runtime import DEFAULT_MEMORY_MB, run_bounded
from .source import SEMANTIC_ROLES, Wrapper

_CAPTURE = r'''
local object = __TABLE__
object.__moonveil_capture = function(self)
    local labels = {}
    local factories = __FACTORIES__
    for role, name in pairs(factories) do
        local original = self[name]
        self[name] = function(owner, ...)
            local handler = original(owner, ...)
            labels[handler] = role
            return handler
        end
    end
    local captures = 0
    self["__CONSTRUCTOR__"] = function(_, cells)
        local handlers = cells[9][2][cells[9][3]]
        return function(root)
            captures += 1
            if captures ~= 1 then error("ambiguous protected constructor") end
            local seen, queue = {}, {}
            local function atom(value)
                local kind = type(value)
                if kind == "nil" then return "Z"
                elseif kind == "boolean" then return value and "B1" or "B0"
                elseif kind == "number" then return "N" .. string.format("%.17g", value)
                elseif kind == "string" then
                    return "S" .. (string.gsub(value, ".", function(c)
                        return string.format("%02x", string.byte(c))
                    end))
                elseif kind == "table" then
                    if not seen[value] then
                        table.insert(queue, value)
                        seen[value] = #queue
                    end
                    return "T" .. seen[value]
                end
                error("unsupported decoded value: " .. kind)
            end
            print("MOONVEIL210_BEGIN")
            print("R\t" .. atom(root))
            for id, handler in pairs(handlers) do
                if not labels[handler] then error("unknown handler factory") end
                print("H\t" .. tostring(id) .. "\t" .. labels[handler])
            end
            local index = 1
            while index <= #queue do
                print("T\t" .. index)
                for key, value in pairs(queue[index]) do
                    print("E\t" .. index .. "\t" .. atom(key) .. "\t" .. atom(value))
                end
                index += 1
            end
            print("MOONVEIL210_END")
            return function() end
        end
    end
    self["__BOOTSTRAP__"](self)
    if captures ~= 1 then error("protected constructor was not captured") end
end
object["__REMAPPER__"](object, __NAMES__, "__moonveil_capture")
'''


def instrument(wrapper: Wrapper) -> str:
    factories = "{" + ",".join(
        f'["{role}"]="{name}"' for role, name in wrapper.roles.items()
        if role in SEMANTIC_ROLES
    ) + "}"
    # Single-pass substitution: input text can itself contain placeholder strings.
    replacements = {
        "__TABLE__": wrapper.table_source,
        "__FACTORIES__": factories,
        "__CONSTRUCTOR__": wrapper.roles["constructor"],
        "__BOOTSTRAP__": wrapper.entry,
        "__REMAPPER__": wrapper.remapper,
        "__NAMES__": wrapper.names_source,
    }
    return re.sub("|".join(re.escape(key) for key in replacements), lambda m: replacements[m[0]], _CAPTURE)


def _atom(token):
    tag, body = token[:1], token[1:]
    if token == "Z":
        return {"type": "nil"}
    if tag == "B" and body in {"0", "1"}:
        return {"type": "boolean", "value": body == "1"}
    if tag == "S":
        try:
            bytes.fromhex(body)
        except ValueError as exc:
            raise MoonVeilError("invalid decoded string") from exc
        return {"type": "bytes", "hex": body}
    if tag in {"N", "T"}:
        try:
            number = float(body) if tag == "N" else int(body)
        except ValueError as exc:
            raise MoonVeilError("invalid numeric dump atom") from exc
        if tag == "T":
            if number < 1:
                raise MoonVeilError("table IDs must be positive")
            return {"type": "table", "id": number}
        if not math.isfinite(number):
            raise MoonVeilError("non-finite decoded number")
        return {"type": "number", "value": int(number) if number.is_integer() else number}
    raise MoonVeilError(f"unknown dump atom: {token[:40]!r}")


def parse_dump(stdout: str):
    lines = stdout.splitlines()
    if lines.count("MOONVEIL210_BEGIN") != 1 or lines.count("MOONVEIL210_END") != 1:
        raise MoonVeilError("runtime did not produce one complete decoded graph")
    begin, end = lines.index("MOONVEIL210_BEGIN"), lines.index("MOONVEIL210_END")
    if end <= begin:
        raise MoonVeilError("invalid decoded graph boundary")
    root, tables, handlers = None, {}, {}
    seen_keys = {}
    for line in lines[begin + 1:end]:
        fields = line.split("\t")
        try:
            if fields[0] == "R" and len(fields) == 2 and root is None:
                root = _atom(fields[1])
            elif fields[0] == "H" and len(fields) == 3:
                key = int(fields[1])
                if key in handlers or fields[2] not in SEMANTIC_ROLES:
                    raise MoonVeilError("invalid or duplicate handler")
                handlers[key] = fields[2]
            elif fields[0] == "T" and len(fields) == 2:
                key = int(fields[1])
                if key <= 0 or key in tables:
                    raise MoonVeilError("invalid or duplicate table")
                tables[key] = []
                seen_keys[key] = set()
            elif fields[0] == "E" and len(fields) == 4:
                key = int(fields[1])
                if key not in tables:
                    raise MoonVeilError("entry refers to an undeclared table")
                atom_key, value = _atom(fields[2]), _atom(fields[3])
                identity = json.dumps(atom_key, sort_keys=True)
                if atom_key["type"] == "nil" or identity in seen_keys[key]:
                    raise MoonVeilError("nil or duplicate table key")
                seen_keys[key].add(identity)
                tables[key].append({"key": atom_key, "value": value})
            else:
                raise MoonVeilError("malformed or duplicate dump record")
        except ValueError as exc:
            raise MoonVeilError("malformed dump ID") from exc
    if root is None or root["type"] != "table" or not handlers:
        raise MoonVeilError("decoded graph is missing its root or handlers")
    atoms = [root]
    for entries in tables.values():
        for entry in entries:
            atoms.extend((entry["key"], entry["value"]))
    if any(a["type"] == "table" and a["id"] not in tables for a in atoms):
        raise MoonVeilError("decoded graph contains a missing table reference")
    return {"schema": "moonveil-2.1.0-graph-v1", "root": root, "handlers": handlers,
            "tables": [{"id": key, "entries": tables[key]} for key in sorted(tables)]}


def decode(wrapper: Wrapper, luau: Path, timeout: float, memory_mb: int = DEFAULT_MEMORY_MB):
    with tempfile.TemporaryDirectory(prefix="moonveil-decode-") as directory:
        script = Path(directory) / "capture.luau"
        script.write_text(instrument(wrapper), encoding="utf-8")
        stdout, _ = run_bounded([str(luau), str(script)], cwd=script.parent, timeout=timeout,
                                memory_mb=memory_mb, label="luau")
    return parse_dump(stdout)
