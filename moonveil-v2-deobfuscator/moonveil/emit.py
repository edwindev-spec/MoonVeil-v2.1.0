"""Emit Luau from the normalized instruction list.

Two styles are available:

``structured``
    Lifts the straight-line program to idiomatic Luau: nested calls, method
    calls (``game:HttpGet(url)``) and plain globals. Every inlining decision
    preserves the VM's evaluation order, its single/multiple result rules and
    the open-ended ``top`` register. Programs that depend on stale registers or
    on anything the lifter cannot prove are rejected (``auto`` then falls back).

``registers``
    Keeps the VM register table and explicit ``top``. Ugly, but it models every
    program the normalizer accepts.

Both styles are checked against the original handlers by the test-suite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .errors import MoonVeilError

STYLES = ("auto", "structured", "registers")

# Luau allows 200 locals per function; stay well below it.
MAX_LOCALS = 150
# Keep inlined expressions shallow: Luau limits syntax nesting.
MAX_DEPTH = 24
_LONG_CONSTANT = 24

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII)
_RESERVED = frozenset(
    "and break continue do else elseif end false for function if in local nil not or "
    "repeat return then true until while".split()
)


# --------------------------------------------------------------------------- literals


class LuaTable:
    """A constant table recorded by a trace: ordered ``(key, value)`` pairs of constants."""

    def __init__(self, items):
        self.items = list(items)


def _table_literal(table: LuaTable) -> str:
    items = table.items
    array = sorted(((k, v) for k, v in items if type(k) is int and k >= 1), key=lambda kv: kv[0])
    length = 0
    while length < len(array) and array[length][0] == length + 1:
        length += 1
    parts = [lua_literal(v) for _, v in array[:length]]
    rest = array[length:] + [(k, v) for k, v in items if not (type(k) is int and k >= 1)]
    rest.sort(key=lambda kv: (type(kv[0]).__name__, repr(kv[0])))
    for key, value in rest:
        name = identifier(key)
        parts.append(f"{name} = {lua_literal(value)}" if name else f"[{lua_literal(key)}] = {lua_literal(value)}")
    return "{" + ", ".join(parts) + "}"


def lua_literal(value):
    if isinstance(value, LuaTable):
        return _table_literal(value)
    if isinstance(value, bytes):
        parts = []
        for byte in value:
            if byte in (34, 92):
                parts.append("\\" + chr(byte))
            elif 32 <= byte < 127:
                parts.append(chr(byte))
            else:
                # Fixed three-digit escapes cannot merge with following digits.
                parts.append(f"\\{byte:03d}")
        return '"' + "".join(parts) + '"'
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if type(value) in (int, float):
        return repr(value)
    raise MoonVeilError("unsupported Luau constant")


def identifier(value) -> Optional[str]:
    """Return ``value`` as a usable Luau name, or ``None`` when it is not one."""
    if not isinstance(value, bytes):
        return None
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError:
        return None
    if _IDENTIFIER.match(text) and text not in _RESERVED:
        return text
    return None


def _index(base: str, key: bytes) -> str:
    name = identifier(key)
    return f"{base}.{name}" if name else f"{base}[{lua_literal(key)}]"


# --------------------------------------------------------------------------- lifting


class _Unsupported(Exception):
    """The program cannot be expressed in structured style."""


@dataclass(frozen=True)
class _Const:
    value: object


@dataclass(frozen=True)
class _Param:
    name: str


@dataclass(eq=False)
class _Node:
    """One effectful instruction: a global read, a method lookup or a call."""

    kind: str  # "global" | "index" | "self" | "call"
    pc: int
    key: Optional[bytes] = None
    recv: object = None
    fn: object = None
    args: List[object] = field(default_factory=list)
    tail: Optional[_Node] = None  # open call spliced as the final argument
    results: int = 0  # calls: fixed result count; -1 means open (C = 0)
    base: int = 0
    natural: bool = False  # traced call: its result count is unobservable, never wrap in parentheses
    # Filled by the use analysis.
    uses: List[int] = field(default_factory=list)
    consumers: List[list] = field(default_factory=list)
    tail_used: bool = False
    fuse_with: Optional[_Node] = None
    # Filled while emitting.
    pending: bool = False
    text: str = ""
    recv_text: str = ""
    depth: int = 1
    names: Optional[List[str]] = None
    pack_name: Optional[str] = None

    def __post_init__(self):
        slots = max(self.results, 2)
        self.uses = [0] * slots
        self.consumers = [[] for _ in range(slots)]


@dataclass(frozen=True)
class _Ref:
    node: _Node
    index: int


_NIL = _Const(None)


@dataclass
class _Program:
    nodes: List[_Node]
    returns: List[object]
    return_tail: Optional[_Node]
    parameters: int
    used_parameters: Set[str]


def _lift(ir) -> _Program:
    regs: Dict[int, object] = {i: _Param(f"arg{i}") for i in range(1, ir["parameter_count"] + 1)}
    nodes: List[_Node] = []
    # Registers at or above ``taint`` may hold a dynamic number of values written by an
    # open call (and untouched stale ones above them); reading one is not expressible.
    taint: Optional[int] = None
    clean: Set[int] = set()
    previous_open: Optional[_Node] = None
    used_parameters: Set[str] = set()
    returns: List[object] = []
    return_tail: Optional[_Node] = None

    def read(register: int):
        if taint is not None and register >= taint and register not in clean:
            raise _Unsupported(f"register {register} holds values of an open call")
        value = regs.get(register, _NIL)
        if isinstance(value, _Param):
            used_parameters.add(value.name)
        return value

    def write(register: int, value) -> None:
        regs[register] = value
        if taint is not None and register >= taint:
            clean.add(register)

    for ins in ir["instructions"]:
        op = ins["op"]
        if op == "nop":
            continue
        fresh, previous_open = previous_open, None
        if op == "loadk":
            write(ins["a"], _Const(ins["value"]))
        elif op == "getglobal":
            node = _Node("global", ins["pc"], key=ins["value"])
            nodes.append(node)
            write(ins["a"], _Ref(node, 0))
        elif op == "self":
            a = ins["a"]
            node = _Node("self", ins["pc"], key=ins["value"], recv=read(ins["b"]))
            nodes.append(node)
            write(a + 1, _Ref(node, 1))
            write(a, _Ref(node, 0))
        else:
            a, b, c = ins["a"], ins["b"], ins["c"]
            fn = read(a)
            tail = None
            if b == 0:
                # ``top`` is only known when the immediately preceding instruction was an
                # open call whose results start right above the callee.
                if fresh is None or fresh.base < a + 1:
                    raise _Unsupported("open argument list without a fresh open call")
                args = [read(r) for r in range(a + 1, fresh.base)]
                tail = fresh
            else:
                args = [read(r) for r in range(a + 1, a + b)]
            node = _Node("call", ins["pc"], fn=fn, args=args, tail=tail, base=a,
                         results=-1 if c == 0 else c - 1)
            nodes.append(node)
            if c == 0:
                taint = a if taint is None else min(taint, a)
                clean = {r for r in clean if r < a}
                previous_open = node
            else:
                for index in range(c - 1):
                    write(a + index, _Ref(node, index))
            if op == "call_return":
                start, count = ins["return_a"], ins["return_b"]
                if count == 0:
                    if node.results != -1 or start > a:
                        raise _Unsupported("return of an open range that is not the call's own results")
                    returns = [read(r) for r in range(start, a)]
                    return_tail = node
                elif count > 1:
                    returns = [read(r) for r in range(start, start + count - 1)]
                break
    return _Program(nodes, returns, return_tail, ir["parameter_count"], used_parameters)


def _analyze_uses(program: _Program) -> None:
    def use(operand, consumer, slot):
        if isinstance(operand, _Ref):
            operand.node.uses[operand.index] += 1
            operand.node.consumers[operand.index].append((consumer, slot))

    for node in program.nodes:
        if node.kind in ("self", "index"):
            use(node.recv, node, "recv")
        elif node.kind == "call":
            use(node.fn, node, "fn")
            for position, operand in enumerate(node.args):
                use(operand, node, position)
            if node.tail is not None:
                node.tail.tail_used = True
    for position, operand in enumerate(program.returns):
        use(operand, None, position)
    if program.return_tail is not None:
        program.return_tail.tail_used = True
    # A method lookup that feeds exactly one call, as callee and first argument,
    # is ``receiver:name(...)`` in the original source.
    for node in program.nodes:
        if node.kind == "self" and node.uses == [1, 1] and identifier(node.key):
            (call, slot), = node.consumers[0]
            (other, other_slot), = node.consumers[1]
            if call is other and call is not None and slot == "fn" and other_slot == 0:
                node.fuse_with = call


# --------------------------------------------------------------------------- emission


class _Emitter:
    banner = "-- Recovered from MoonVeil 2.1.0; original names and comments are unavailable."

    def __init__(self, program: _Program, banner: Optional[str] = None):
        self.program = program
        if banner:
            self.banner = banner
        self.lines: List[str] = []
        self.pending: List[_Node] = []
        self.locals = 0
        self.used: Set[str] = set()
        self.counters: Dict[str, int] = {}
        self.needs_environment = False
        self.environment = ""
        # Locals must never shadow a global the program reads by its plain name.
        for node in program.nodes:
            if node.kind == "global" and identifier(node.key):
                self.used.add(node.key.decode("ascii"))
        self.environment = self.fresh("environment")

    # -- names ------------------------------------------------------------------

    def fresh(self, stem: str) -> str:
        stem = re.sub(r"\W", "_", stem) or "v"
        candidate = stem
        while candidate in self.used or candidate in _RESERVED:
            self.counters[stem] = self.counters.get(stem, 0) + 1
            candidate = f"{stem}_{self.counters[stem]}"
        self.used.add(candidate)
        return candidate

    def declare(self, count: int = 1) -> None:
        self.locals += count
        if self.locals > MAX_LOCALS:
            raise _Unsupported("too many locals for a single Luau function")

    def statement(self, text: str) -> None:
        # A leading parenthesis would continue the previous statement.
        self.lines.append((";" if text.startswith("(") else "") + text)

    # -- expressions ------------------------------------------------------------

    def operand(self, operand, *, last: bool = False, prefix: bool = False) -> str:
        if isinstance(operand, _Const):
            literal = lua_literal(operand.value)
            return f"({literal})" if prefix else literal
        if isinstance(operand, _Param):
            return operand.name
        node = operand.node
        if node.names is not None:
            return node.names[operand.index]
        # Inline expression. A call keeps only its first value unless it is the
        # last element of a list, where Luau would expand every result.
        if last and node.kind == "call" and not node.natural:
            return f"({node.text})"
        return node.text

    def tail_text(self, tail: _Node) -> str:
        if tail.pending:
            return tail.text
        return f"table.unpack({tail.pack_name}, 1, {tail.pack_name}.n)"

    def global_text(self, key: bytes) -> str:
        name = identifier(key)
        if name:
            return name
        self.needs_environment = True
        return f"{self.environment}[{lua_literal(key)}]"

    def argument_list(self, operands, tail: Optional[_Node]) -> str:
        parts = [self.operand(o, last=tail is None and i == len(operands) - 1) for i, o in enumerate(operands)]
        if tail is not None:
            parts.append(self.tail_text(tail))
        return ", ".join(parts)

    # -- ordering ---------------------------------------------------------------

    def inline_operands(self, node: _Node) -> List[_Node]:
        """Pending nodes consumed by ``node``, in textual evaluation order."""
        found: List[_Node] = []

        def look(operand):
            if isinstance(operand, _Ref) and operand.node.pending:
                found.append(operand.node)

        if node.kind in ("self", "index"):
            look(node.recv)
        elif node.kind == "call":
            if self.is_fused(node):
                look(node.fn)
                operands = node.args[1:]
            else:
                look(node.fn)
                operands = node.args
            for operand in operands:
                look(operand)
            if node.tail is not None and node.tail.pending:
                found.append(node.tail)
        return found

    def is_fused(self, call: _Node) -> bool:
        fn = call.fn
        return (isinstance(fn, _Ref) and fn.node.kind == "self" and fn.node.fuse_with is call
                and fn.node.pending)

    def flush(self, through: Optional[_Node] = None) -> None:
        """Materialize pending expressions in program order, all or up to ``through``."""
        count = len(self.pending) if through is None else self.pending.index(through) + 1
        done, self.pending = self.pending[:count], self.pending[count:]
        for node in done:
            self.materialize(node)

    # -- materialization --------------------------------------------------------

    def materialize(self, node: _Node) -> None:
        node.pending = False
        if node.kind == "global":
            name = self.fresh("g_" + (identifier(node.key) or "global")) if node.uses[0] else "_"
            self.declare()
            self.lines.append(f"local {name} = {node.text}")
            node.names = [name]
        elif node.kind == "index":
            name = self.fresh("i_" + (identifier(node.key) or "index")) if node.uses[0] else "_"
            self.declare()
            self.lines.append(f"local {name} = {node.text}")
            node.names = [name]
        elif node.kind == "self":
            recv = node.recv
            if isinstance(recv, _Param) or (isinstance(recv, _Ref) and recv.node.names is not None):
                recv_name = self.operand(recv)
            else:
                recv_name = self.fresh("receiver")
                self.declare()
                self.lines.append(f"local {recv_name} = {node.recv_text}")
            method = self.fresh("m_" + (identifier(node.key) or "method"))
            self.declare()
            self.lines.append(f"local {method} = {_index(recv_name, node.key)}")
            node.names = [method, recv_name]
        else:
            self.materialize_call(node)

    def materialize_call(self, node: _Node) -> None:
        if node.results == -1:
            if node.tail_used:
                node.pack_name = self.fresh("results")
                self.declare()
                self.lines.append(f"local {node.pack_name} = table.pack({node.text})")
            else:
                self.statement(node.text)
            return
        if not any(node.uses[: max(node.results, 0)]):
            self.statement(node.text)  # results are unused: a bare call statement
            return
        names = [self.fresh("result") if node.uses[i] else "_" for i in range(node.results)]
        self.declare(len(names))
        self.lines.append(f"local {', '.join(names)} = {node.text}")
        node.names = names

    # -- driver -----------------------------------------------------------------

    def process(self, node: _Node) -> None:
        if node.kind == "call" and self.is_fused(node):
            # Luau performs the NAMECALL lookup *after* evaluating the arguments, while
            # the VM looks the method up first. They only agree when evaluating the
            # arguments has no observable effect, so any pending argument forces the
            # lookup into its own statement.
            method = node.fn.node
            if [n for n in self.inline_operands(node) if n is not method]:
                self.flush(through=method)
        operands = self.inline_operands(node)
        if operands and self.pending[len(self.pending) - len(operands):] != operands:
            self.flush()
            operands = self.inline_operands(node)  # now empty: everything has a name
        # Render with operands still inline, then retire them from ``pending``.
        if node.kind == "global":
            node.text = self.global_text(node.key)
        elif node.kind == "self":
            node.recv_text = self.operand(node.recv, prefix=True)
            node.text = node.recv_text
        elif node.kind == "index":
            base = self.operand(node.recv, prefix=True)
            node.text = _index(base, node.key) if isinstance(node.key, bytes) else f"{base}[{lua_literal(node.key)}]"
        else:
            node.text = self.call_text(node)
        node.depth = 1 + max([n.depth for n in operands], default=0)
        for taken in operands:
            taken.pending = False
        if operands:
            del self.pending[len(self.pending) - len(operands):]

        inline = node.depth <= MAX_DEPTH and self.inlinable(node)
        if inline:
            node.pending = True
            self.pending.append(node)
        else:
            self.flush()
            node.pending = True
            self.materialize(node)
            node.pending = False

    def inlinable(self, node: _Node) -> bool:
        if node.kind in ("global", "index"):
            return node.uses[0] == 1
        if node.kind == "self":
            return node.fuse_with is not None
        if node.results == -1:
            return node.tail_used
        return node.results == 1 and node.uses[0] == 1

    def call_text(self, node: _Node) -> str:
        if self.is_fused(node):
            method = node.fn.node
            head = f"{method.recv_text}:{identifier(method.key)}"
            return f"{head}({self.argument_list(node.args[1:], node.tail)})"
        head = self.operand(node.fn, prefix=True)
        return f"{head}({self.argument_list(node.args, node.tail)})"

    def run(self) -> str:
        program = self.program
        header = []
        if program.used_parameters:
            last = max(int(name[3:]) for name in program.used_parameters)
            names = [f"arg{i}" if f"arg{i}" in program.used_parameters else "_" for i in range(1, last + 1)]
            if [self.fresh(name) for name in names if name != "_"] != [name for name in names if name != "_"]:
                raise _Unsupported("parameter names collide with globals")
            self.declare(len(names))
            header.append(f"local {', '.join(names)} = ...")
        for node in program.nodes:
            self.process(node)
        if program.returns or program.return_tail is not None:
            operands = [o.node for o in program.returns if isinstance(o, _Ref) and o.node.pending]
            if program.return_tail is not None and program.return_tail.pending:
                operands.append(program.return_tail)
            if operands and self.pending[len(self.pending) - len(operands):] != operands:
                self.flush()
                operands = []
            values = self.argument_list(program.returns, program.return_tail)
            for taken in operands:
                taken.pending = False
            if operands:
                del self.pending[len(self.pending) - len(operands):]
            self.flush()
            self.lines.append(f"return {values}")
        else:
            self.flush()
        body = header + self.lines
        if self.needs_environment:
            body.insert(0, f"local {self.environment} = getfenv()")
        return "\n".join([self.banner, *body]) + "\n"


def emit_structured(ir) -> str:
    program = _lift(ir)
    _analyze_uses(program)
    return _Emitter(program).run()


# --------------------------------------------------------------------------- traces

_TRACE_BANNER = ("-- Recovered by running the program against a symbolic environment: only operations on\n"
                 "-- the environment appear, in the order they happened, and only the executed path is shown.")


def emit_trace(trace) -> str:
    """Render the environment operations recorded by :mod:`moonveil.trace` as Luau."""
    nodes: List[_Node] = []
    by_id: Dict[int, _Node] = {}
    tables = trace["tables"]

    def constant(atom, depth=0):
        kind = atom["type"]
        if kind == "nil":
            return None
        if kind in ("boolean", "number"):
            return atom["value"]
        if kind == "bytes":
            return bytes.fromhex(atom["hex"])
        if kind == "table" and depth < 8 and atom["id"] in tables:
            return LuaTable((constant(k, depth + 1), constant(v, depth + 1)) for k, v in tables[atom["id"]])
        raise MoonVeilError("a table passed to the environment holds symbolic values or is nested too deeply")

    def operand(atom):
        if atom["type"] == "proxy":
            node = by_id.get(atom["id"])
            if node is None:
                raise MoonVeilError("trace refers to a value before it was created")
            return _Ref(node, 0)
        return _Const(constant(atom))

    for number, event in enumerate(trace["events"], 1):
        if event["op"] == "G":
            node = _Node("global", number, key=event["name"])
        elif event["op"] == "I":
            key = event["key"]
            if key["type"] == "proxy":
                raise MoonVeilError("indexing with a symbolic key is not supported")
            node = _Node("index", number, key=constant(key), recv=operand(event["base"]))
        else:
            args = [operand(a) for a in event["args"]]
            node = _Node("call", number, fn=operand(event["fn"]), args=args, results=1, natural=True)
        nodes.append(node)
        by_id[event["id"]] = node

    references: Dict[int, int] = {}
    for node in nodes:
        for operand_ in [node.recv, node.fn, *node.args]:
            if isinstance(operand_, _Ref):
                references[id(operand_.node)] = references.get(id(operand_.node), 0) + 1
    # ``obj.name(obj, ...)`` is the shape of a method call: fuse it so ``obj:name(...)`` comes out.
    for node in nodes:
        fn = node.fn
        if node.kind == "call" and isinstance(fn, _Ref) and fn.node.kind == "index" and node.args \
                and references.get(id(fn.node)) == 1 and node.args[0] == fn.node.recv:
            lookup = fn.node
            lookup.kind = "self"
            node.fn, node.args[0] = _Ref(lookup, 0), _Ref(lookup, 1)

    program = _Program(nodes, [], None, 0, set())
    _analyze_uses(program)
    try:
        return _Emitter(program, _TRACE_BANNER).run()
    except _Unsupported as exc:
        raise MoonVeilError(f"the trace is too large to express as one Luau chunk ({exc})") from exc


# --------------------------------------------------------------------------- registers


def emit_registers(ir) -> str:
    lines = [
        "-- Recovered from MoonVeil 2.1.0; registers retain VM numbering.",
        "local environment = getfenv()",
        "local pack, unpack = table.pack, table.unpack",
        "local r = {}",
        "local top = -1",
    ]
    if ir["parameter_count"]:
        targets = ", ".join(f"r[{index}]" for index in range(1, ir["parameter_count"] + 1))
        lines.append(targets + " = ...")
    for ins in ir["instructions"]:
        op = ins["op"]
        if op == "nop":
            continue
        a = ins["a"]
        lines.append(f"-- PC {ins['pc']}: {op}")
        if op in {"loadk", "getglobal"}:
            expr = lua_literal(ins["value"]) if op == "loadk" else f"environment[{lua_literal(ins['value'])}]"
            lines.append(f"r[{a}] = {expr}")
        elif op == "self":
            # The second write uses the saved receiver, even when A == B.
            lines.append(f"r[{a + 1}] = r[{ins['b']}]")
            lines.append(f"r[{a}] = r[{a + 1}][{lua_literal(ins['value'])}]")
        else:
            end = "top" if ins["b"] == 0 else str(a + ins["b"] - 1)
            call = f"r[{a}](unpack(r, {a + 1}, {end}))"
            c = ins["c"]
            if c == 1:
                lines.append(call)
            elif c == 0:
                lines.extend([
                    "do",
                    f"    local values = pack({call})",
                    f"    for index = 1, values.n do r[{a} + index - 1] = values[index] end",
                    f"    top = {a} + values.n - 1",
                    "end",
                ])
            else:
                # A multiple assignment pads missing results with nil, like the VM does.
                lines.append(", ".join(f"r[{a + i}]" for i in range(c - 1)) + " = " + call)
            if op == "call_return":
                start, n = ins["return_a"], ins["return_b"]
                end = "top" if n == 0 else str(start + n - 2)
                lines.append("return" if n == 1 else f"return unpack(r, {start}, {end})")
                break
    return "\n".join(lines) + "\n"


def emit(ir, style: str = "auto"):
    """Return ``(source, profile)``; ``profile`` is ``structured`` or ``registers``."""
    if style not in STYLES:
        raise MoonVeilError(f"unknown emission style: {style}")
    if style != "registers":
        try:
            return emit_structured(ir), "structured"
        except _Unsupported as exc:
            if style == "structured":
                raise MoonVeilError(f"structured emission is not possible for this program ({exc}); "
                                    "use --style registers") from exc
    return emit_registers(ir), "registers"
