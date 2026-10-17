from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "moonveil-2.1.0.lua"
HANDLERS = {0: "nop", 1: "loadk", 4: "xor_index", 6: "self", 7: "getglobal", 8: "xor_reverse", 14: "call", 15: "call_return"}


def instruction(kind, **values):
    result = {key: 0 for key in (1, 3, 4, 8, 9, 10, 12, 13, 16, 17, 20, 27, 29)}
    result[20] = kind
    result.update({int(key[1:]): value for key, value in values.items()})
    return result


def atom(value):
    if isinstance(value, tuple):
        return {"type": "table", "id": value[1]}
    if isinstance(value, bytes):
        return {"type": "bytes", "hex": value.hex()}
    if isinstance(value, bool):
        return {"type": "boolean", "value": value}
    if value is None:
        return {"type": "nil"}
    return {"type": "number", "value": value}


def graph(instructions, stack=12, params=0):
    root = {28: stack, 23: params, 11: 0, 15: 0, 30: 0, 25: ("table", 2), 14: ("table", 3), 19: ("table", 4)}
    tables = {1: root, 2: {index + 1: ("table", index + 5) for index in range(len(instructions))}, 3: {}, 4: {}}
    tables.update({index + 5: ins for index, ins in enumerate(instructions)})
    return {"schema": "moonveil-2.1.0-graph-v1", "root": atom(("table", 1)), "handlers": HANDLERS.copy(),
            "tables": [{"id": key, "entries": [{"key": atom(k), "value": atom(v)} for k, v in value.items()]} for key, value in tables.items()]}


def terminal(a=1, b=1, c=1, return_a=1, return_b=1):
    return instruction(15, f12=a + 38, f8=b + 5, f9=c + 60, f3=return_b + 4, f13=return_a + 7)


def encode(op):
    """Encode an abstract operation as the raw instruction the original VM executes."""
    kind = op[0]
    if kind == 'loadk':
        return instruction(1, f12=op[1] + 19, f4=op[2])
    if kind == 'getglobal':
        return instruction(7, f12=op[1] + 36, f4=op[2])
    if kind == 'self':
        return instruction(6, f12=op[1] + 45, f8=op[2] + 63, f4=op[3])
    if kind == 'call':
        return instruction(14, f12=op[1] + 59, f8=op[2] + 41, f9=op[3] + 4)
    _, a, b, c, start, n = op
    return instruction(15, f12=a + 38, f8=b + 5, f9=c + 60, f3=n + 4, f13=start + 7)


def ir_of(ops, params=0, stack=64):
    from moonveil.vm import normalize
    return normalize(graph([encode(op) for op in ops], stack=stack, params=params))
