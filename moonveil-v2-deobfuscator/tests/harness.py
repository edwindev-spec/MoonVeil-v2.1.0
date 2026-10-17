"""Shared Luau test harness: a synthetic environment, the original VM as an oracle."""

import os
import tempfile
import unittest
from pathlib import Path

from moonveil.decoder import decode
from moonveil.errors import MoonVeilError
from moonveil.runtime import find_luau, run_bounded
from moonveil.source import parse_wrapper
from moonveil.emit import lua_literal
from helpers import FIXTURE

# Every call, global read and method lookup is logged, so ordering and arity are
# observable. Names the wrapper itself uses (bit32, string, ...) are not logged.
PRELUDE = r'''
-- Prelude locals use a prefix no recovered program can name, so they never shadow globals.
local __real = getfenv()
local function __event(...)
    local values = table.pack(...)
    local line = tostring(values.n)
    for index = 1, values.n do
        local value = values[index]
        local kind = type(value)
        local text = tostring(value)
        if kind == "string" then
            text = (string.gsub(value, ".", function(c) return string.format("%02x", string.byte(c)) end))
        elseif kind == "function" or kind == "table" then
            text = "?"
        end
        line ..= "|" .. kind .. ":" .. text
    end
    print(line)
end
local __object = setmetatable({}, {__index = function(self, key)
    __event("lookup", key)
    return function(receiver, ...)
        __event("method", receiver == self, ...)
        return "synthetic-source", nil, "extra"
    end
end})
local __store = {
    game = __object,
    object = __object,
    loadstring = function(...)
        __event("loadstring", ...)
        return function(...)
            __event("loaded", ...)
            return "discarded", nil, false
        end, "also-discarded"
    end,
    producer = function(...) __event("producer", ...) return 11, nil, 33 end,
    short = function(...) __event("short", ...) return 11 end,
    sink = function(...) __event("sink", ...) return false, nil, 77 end,
    echo = function(...) __event("echo", ...) return ... end,
    none = function(...) __event("none", ...) end,
}
local __environment = setmetatable({}, {__index = function(_, key)
    local value = __store[key]
    if value ~= nil then
        __event("global", key)
        return value
    end
    return __real[key]
end})
local function __program(...)
__PROGRAM__
end
setfenv(__program, __environment)
local __results = table.pack(__program(5, nil, 88))
__event("returns", table.unpack(__results, 1, __results.n))
'''


def lua_table(rows):
    return '{' + ','.join('{' + ','.join(f'[{key}]={lua_literal(value)}' for key, value in sorted(row.items())) + '}' for row in rows) + '}'


class LuauCase(unittest.TestCase):
    """Base class: locates Luau once and offers ``run_source`` and ``oracle``."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.luau = find_luau()
        except MoonVeilError as exc:
            if os.environ.get('MOONVEIL_REQUIRE_LUAU') == '1':
                raise
            raise unittest.SkipTest('official Luau unavailable; set LUAU_PATH or MOONVEIL_REQUIRE_LUAU=1') from exc
        cls.wrapper = parse_wrapper(FIXTURE.read_text())
        cls.sample_graph = decode(cls.wrapper, cls.luau, 10)

    def run_source(self, source):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.luau'
            path.write_text(PRELUDE.replace('__PROGRAM__', source))
            return run_bounded([str(self.luau), str(path)], cwd=path.parent, timeout=10)[0]

    def oracle(self, rows, params=0, stack=12):
        wrapper = self.wrapper
        # Keep the genuine loader, factories and dispatcher; replace only its
        # decoded instruction data, before the original constructor sees it.
        ctor = wrapper.roles['constructor']
        setup = f'''
local instance = {wrapper.table_source}
instance.__test = function(self)
    local original = self["{ctor}"]
    self["{ctor}"] = function(owner, cells)
        local construct = original(owner, cells)
        return function(root, env)
            root[25] = {lua_table(rows)}
            root[28], root[23] = {stack}, {params}
            return construct(root, env)
        end
    end
    return self["{wrapper.entry}"](self, 5, nil, 88)
end
return instance["{wrapper.remapper}"](instance, {wrapper.names_source}, "__test")
'''
        return self.run_source(setup)
