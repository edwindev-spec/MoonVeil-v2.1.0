"""Differential fuzzing: random programs must behave exactly like the original VM handlers.

Two generators run: one imitates what a compiler emits (nested calls, method calls,
open argument lists), the other builds arbitrary low-level sequences using a small
simulator so every call targets a callable. Use MOONVEIL_FUZZ_CASES to scale the run
and MOONVEIL_FUZZ_SEED to move the seed window.
"""

import os
import random

from moonveil.emit import emit
from moonveil.vm import normalize
from harness import LuauCase
from helpers import encode, graph

FUNCS = [b'producer', b'short', b'sink', b'echo', b'none']
CONSTANTS = [7, b'x', 3.5, True, b'a-long-constant-string-value-0123456789']
METHODS = [b'method', b'HttpGet', b'bad name', b'end']


# --------------------------------------------------------------------------- compiler-like


def gen_value(rng, ops, target, depth, last):
    if depth <= 0 or rng.random() < 0.3:
        if rng.random() < 0.6:
            ops.append(('loadk', target, rng.choice(CONSTANTS)))
        else:
            ops.append(('getglobal', target, rng.choice([b'object', b'game'])))
        return False
    # The final argument may stay open (all results are passed on).
    return gen_call(rng, ops, target, depth, 'open' if last and rng.random() < 0.6 else 'value')


def gen_call(rng, ops, target, depth, mode):
    """Emit a call at ``target``; returns True when it was left open (C = 0)."""
    if rng.random() < 0.3:
        ops.append(('getglobal', target, rng.choice([b'object', b'game'])))
        ops.append(('self', target, target, rng.choice(METHODS)))
        first, fixed = target + 2, 1
    else:
        ops.append(('getglobal', target, rng.choice(FUNCS)))
        first, fixed = target + 1, 0
    count = rng.randint(0, 3)
    tail_open = False
    for index in range(count):
        tail_open = gen_value(rng, ops, first + index, depth - 1, index == count - 1)
    b = 0 if tail_open else count + fixed + 1
    c = {'open': 0, 'discard': 1, 'value': rng.choice([2, 2, 2, 3])}[mode]
    ops.append(('call', target, b, c))
    return c == 0


def compiler_like(rng):
    ops = []
    for _ in range(rng.randint(0, 3)):
        gen_call(rng, ops, rng.randint(1, 3), rng.randint(1, 3), 'discard')
    base = rng.randint(1, 3)
    for register in range(1, base):
        ops.append(('loadk', register, rng.choice(CONSTANTS)))
    c = rng.choice([1, 1, 2, 3, 0])
    gen_call(rng, ops, base, rng.randint(1, 3), {1: 'discard', 0: 'open'}.get(c, 'value'))
    kind, a, b, final_c = ops.pop()
    if c in (2, 3):
        final_c = c
    start, n = {1: (a, 1), 0: (rng.randint(1, a), 0), 2: (rng.randint(1, a), None), 3: (a, 3)}[c]
    if n is None:
        n = a - start + 2
    ops.append(('call_return', a, b, final_c, start, n))
    return ops, rng.choice([0, 0, 1, 2])


# --------------------------------------------------------------------------- low level


OBJECT = ('object',)


def run_function(function, args):
    name = function[1]
    return {'producer': lambda: [11, None, 33], 'short': lambda: [11], 'sink': lambda: [False, None, 77],
            'echo': lambda: list(args), 'none': lambda: [], 'method': lambda: ['synthetic-source', None, 'extra']}[name]()


def low_level(rng):
    params = rng.choice([0, 0, 1, 2])
    registers = {index + 1: value for index, value in enumerate([5, None, 88][:params])}
    state = {'top': None}
    ops = []

    def callable_registers():
        return [r for r, v in registers.items() if isinstance(v, tuple) and v[0] == 'fn']

    def ensure_callable():
        if not callable_registers():
            register = rng.randint(0, 5)
            name = rng.choice(FUNCS)
            ops.append(('getglobal', register, name))
            registers[register] = ('fn', name.decode())

    def call(a, b, c):
        top = state['top']
        end = top if b == 0 else a + b - 1
        args = [registers.get(r) for r in range(a + 1, end + 1)]
        results = run_function(registers[a], args)
        if c == 0:
            for index, value in enumerate(results):
                registers[a + index] = value
            state['top'] = a + len(results) - 1
        elif c > 1:
            for index in range(c - 1):
                registers[a + index] = results[index] if index < len(results) else None

    for _ in range(rng.randint(1, 7)):
        roll = rng.random()
        if roll < 0.2:
            register, value = rng.randint(0, 5), rng.choice(CONSTANTS)
            ops.append(('loadk', register, value))
            registers[register] = value
        elif roll < 0.4:
            register = rng.randint(0, 5)
            name = rng.choice(FUNCS + [b'object', b'game', b'missing'])
            ops.append(('getglobal', register, name))
            registers[register] = ('fn', name.decode()) if name in FUNCS else OBJECT if name in (b'object', b'game') else None
        elif roll < 0.55:
            holders = [r for r, v in registers.items() if v == OBJECT]
            if holders:
                b, a = rng.choice(holders), rng.randint(0, 5)
                ops.append(('self', a, b, rng.choice(METHODS)))
                registers[a + 1] = registers[b]
                registers[a] = ('fn', 'method')
        else:
            ensure_callable()
            a = rng.choice(callable_registers())
            b = rng.choice([0, 1, 2, 3, 4] if state['top'] is not None else [1, 2, 3, 4])
            c = rng.choice([0, 1, 2, 3, 4])
            ops.append(('call', a, b, c))
            call(a, b, c)
    ensure_callable()
    a = rng.choice(callable_registers())
    b = rng.choice([0, 1, 2, 3, 4] if state['top'] is not None else [1, 2, 3, 4])
    c = rng.choice([0, 1, 2, 3])
    start = rng.randint(0, 5)
    n = rng.choice([0, 1, 2, 3, 4]) if (c == 0 or state['top'] is not None) else rng.choice([1, 2, 3, 4])
    ops.append(('call_return', a, b, c, start, n))
    return ops, params


class FuzzTests(LuauCase):
    cases = int(os.environ.get('MOONVEIL_FUZZ_CASES', '120'))
    seed = int(os.environ.get('MOONVEIL_FUZZ_SEED', '0'))

    def fuzz(self, generator, salt):
        structured = 0
        for number in range(self.cases):
            seed = self.seed + number
            rng = random.Random(f'{salt}:{seed}')
            ops, params = generator(rng)
            rows = [encode(op) for op in ops]
            ir = normalize(graph(rows, stack=64, params=params))
            expected = self.oracle(rows, params, stack=64)
            for style in ('auto', 'registers'):
                source, profile = emit(ir, style)
                structured += style == 'auto' and profile == 'structured'
                with self.subTest(generator=salt, seed=seed, style=style):
                    self.assertEqual(self.run_source(source), expected, f'ops={ops}\nparams={params}\n{source}')
        return structured

    def test_compiler_like_programs(self):
        structured = self.fuzz(compiler_like, 'compiler')
        # The lifter must really lift these, not silently fall back.
        self.assertGreaterEqual(structured, self.cases * 0.9)

    def test_arbitrary_low_level_programs(self):
        self.fuzz(low_level, 'low')
