import contextlib
import io
import json
import shutil
import tempfile
from pathlib import Path

from moonveil import cli
from moonveil.emit import emit, emit_trace
from moonveil.errors import MoonVeilError
from moonveil.source import diagnose, explain_layout
from moonveil.trace import log_json, parse_trace, trace, trace_ir
from moonveil.analysis import indicators
from moonveil.vm import normalize
from harness import LuauCase
from helpers import FIXTURE

FLATTENED = Path(__file__).parent / 'fixtures' / 'moonveil-2.1.0-flattened.lua'
FLATTENED_URL = 'https://raw.githubusercontent.com/edwin3166/Test/refs/heads/main/Server.lua'


def body(text):
    return [line for line in text.splitlines() if not line.startswith('--')]


def canonical_log(log):
    """Events plus table contents; ``pairs`` order inside a table is not meaningful."""
    tables = {key: sorted(json.dumps(entry, sort_keys=True) for entry in entries) for key, entries in log['tables'].items()}
    return [json.loads(json.dumps(log_json(log)['events'])), tables]


PROGRAMS = {
    'loader': 'loadstring(game:HttpGet("https://example.invalid/a.lua"))()',
    'chain': 'print(game.Players.LocalPlayer.Name)',
    'method_chain': 'game:GetService("Players"):GetPlayers()',
    'effectful_argument': 'game:Foo(workspace.Bar)',
    'reused_result': 'local p = game:GetService("Players") p:Kick("a") p:Foo(p)',
    'unused_read': 'local _ = foo bar()',
    'literals': 'f(nil, false, 1.5, nil, "x\\0y\\"z", -3)',
    'numeric_key': 'local w = workspace[1] w(w[2])',
    'table_argument': 'request({Url = "u", Method = "GET", Headers = {A = "b"}, 1, 2})',
    'computed_constant': 'local base = "https://x/" game:HttpGet(base .. string.rep("a", 3))',
    'keyword_names': 'local g = game["end"] g["a b"](g)',
    'order': 'a(b(), c(), d(e(f())))',
    'lookup_before_argument': 'local m = game.Foo m(game, workspace.Bar)',
    'varargs': 'local function pass(...) return ... end print(pass(game, 1, nil))',
    'pcall': 'pcall(game.Destroy, game)',
}


class TraceTests(LuauCase):
    def test_flattened_sample_is_recovered(self):
        log = trace(FLATTENED.read_text(), self.luau, 20)
        self.assertEqual(body(emit_trace(log)), [f'loadstring(game:HttpGet("{FLATTENED_URL}"))()'])
        self.assertEqual(len(log['events']), 6)
        found = indicators(trace_ir(log))
        self.assertEqual(found['urls'], [FLATTENED_URL])
        self.assertEqual([b['id'] for b in found['behaviors']], ['remote-code-loader'])

    def test_trace_agrees_with_the_static_profile_on_the_original_sample(self):
        traced = body(emit_trace(trace(FIXTURE.read_text(), self.luau, 20)))
        static = body(emit(normalize(self.sample_graph))[0])
        self.assertEqual(traced, static)

    def test_emitted_source_replays_the_same_operations(self):
        for name, program in PROGRAMS.items():
            with self.subTest(name):
                first = trace(program, self.luau, 10)
                source = emit_trace(first)
                second = trace(source, self.luau, 10)
                self.assertEqual(canonical_log(second), canonical_log(first), source)

    def test_method_lookup_order_is_preserved(self):
        # Luau looks `game:Foo(...)` up after evaluating the arguments, so the same-order form is fused...
        self.assertEqual(body(emit_trace(trace(PROGRAMS['effectful_argument'], self.luau, 10)))[-1], 'g_game:Foo(i_Bar)')
        # ...but a lookup that happened *before* an argument is read must stay a separate statement.
        source = emit_trace(trace(PROGRAMS['lookup_before_argument'], self.luau, 10))
        self.assertNotIn('game:Foo(', source)
        self.assertLess(source.index('.Foo'), source.index('workspace'))

    def test_symbolic_values_never_reach_real_apis(self):
        program = 'print("x") os.execute("rm -rf /")'
        with self.assertRaisesRegex(MoonVeilError, 'raised an error'):
            trace(program, self.luau, 10)  # os.execute is not part of the safe os subset
        log = trace('print(1) require("y") io.write("z") loadstring("w")()', self.luau, 10)
        names = [e['name'] for e in log['events'] if e['op'] == 'G']
        self.assertEqual(names, [b'print', b'require', b'io', b'loadstring'])

    def test_getfenv_returns_the_sandbox(self):
        log = trace('getfenv(0).secret() setfenv(1, {}) getfenv().other()', self.luau, 10)
        self.assertEqual([e['name'] for e in log['events'] if e['op'] == 'G'], [b'secret', b'other'])

    def test_hostile_program_text_cannot_leave_its_string(self):
        program = 'print("\\"]]; error(\'escaped\') --[=[ ", "]=] end end")'
        log = trace(program, self.luau, 10)
        self.assertEqual(log['events'][-1]['args'][0]['type'], 'bytes')
        self.assertEqual(bytes.fromhex(log['events'][-1]['args'][0]['hex']).decode(), '"]]; error(\'escaped\') --[=[ ')

    def test_unsupported_behaviour_is_explained(self):
        cases = {
            'local a = game .. "x"': 'computes with a value',
            'game.x = 1': '__newindex',
            'f(function() end)': 'function value',
            'x = ': 'not valid Luau',
            'local a = 1': 'nothing to recover',
            'error("boom")': 'raised an error',
            'while true do end': 'timeout',
        }
        for program, message in cases.items():
            with self.subTest(program):
                with self.assertRaisesRegex(MoonVeilError, message):
                    trace(program, self.luau, 2)

    def test_truncated_or_forged_logs_are_rejected(self):
        for bad in ('', 'MOONVEIL_TRACE_BEGIN\nS\tok\n', 'MOONVEIL_TRACE_BEGIN\nG\t1\tzz\nS\tok\nMOONVEIL_TRACE_END\n',
                    'MOONVEIL_TRACE_BEGIN\nG\t1\t61\nG\t1\t61\nS\tok\nMOONVEIL_TRACE_END\n',
                    'MOONVEIL_TRACE_BEGIN\nC\t1\tP9\t0\nS\tok\nMOONVEIL_TRACE_END\n'):
            with self.subTest(bad=bad):
                try:
                    emit_trace(parse_trace(bad))
                except MoonVeilError:
                    continue
                self.fail('accepted a forged log')


class LayoutDiagnosisTests(LuauCase):
    def test_supported_and_unsupported_layouts_are_described(self):
        known = diagnose(FIXTURE.read_text())
        self.assertEqual((known['wrapper_fields'], known['method_names'], len(known['matched_handlers'])), (38, 35, known['profile_handlers']))
        other = diagnose(FLATTENED.read_text())
        self.assertEqual((other['banner_version'], other['wrapper_fields'], other['method_names']), ('2.1.0', 19, 16))
        self.assertEqual(len(other['matched_handlers']), 6)
        self.assertIn('--mode trace', explain_layout(other))
        self.assertIn('no MoonVeil banner', explain_layout(diagnose('print(1)')))
        self.assertIn('supported wrapper shape', explain_layout(diagnose('-- This script was generated using MoonVeil 2.1.0\nprint(1)')))


class TraceCLITests(LuauCase):
    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue()

    def workspace(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'flat.lua'
        shutil.copy(FLATTENED, path)
        return Path(directory.name), path

    def test_vm_mode_explains_the_layout_and_never_runs_the_program(self):
        directory, path = self.workspace()
        code, _, stderr = self.invoke(['decompile', path])
        self.assertEqual(code, 2)
        self.assertIn('different layout', stderr)
        self.assertIn('--mode trace', stderr)
        self.assertFalse((directory / 'flat.recovered.luau').exists())
        code, stdout, _ = self.invoke(['analyze', path, '--json'])
        report = json.loads(stdout)
        self.assertEqual((code, report['supported'], report['layout']['method_names']), (2, False, 16))

    def test_trace_mode_recovers_reports_and_flags_execution(self):
        directory, path = self.workspace()
        artifacts = directory / 'a'
        code, stdout, _ = self.invoke(['decompile', path, '--mode', 'trace', '--artifacts', artifacts, '--json', '-o', directory / 'o.luau'])
        self.assertEqual(code, 0)
        manifest = json.loads(stdout)
        self.assertEqual((manifest['mode'], manifest['profile'], manifest['emission']), ('trace', 'symbolic-trace', 'trace'))
        self.assertIs(manifest['protected_program_executed'], True)
        self.assertIs(manifest['compiled'], True)
        self.assertEqual(manifest['indicators']['urls'], [FLATTENED_URL])
        self.assertEqual(sorted(p.name for p in artifacts.iterdir()), ['manifest.json', 'trace.json'])
        self.assertEqual(body((directory / 'o.luau').read_text()), [f'loadstring(game:HttpGet("{FLATTENED_URL}"))()'])

    def test_auto_mode_falls_back_only_for_unsupported_layouts(self):
        directory, path = self.workspace()
        code, stdout, _ = self.invoke(['decompile', path, '--mode', 'auto', '-o', directory / 'o.luau'])
        self.assertEqual((code, 'trace' in stdout), (0, True))
        code, stdout, _ = self.invoke(['decompile', FIXTURE, '--mode', 'auto', '-o', directory / 'v.luau', '--json'])
        manifest = json.loads(stdout)
        self.assertEqual((manifest['mode'], manifest['protected_program_executed']), ('vm', False))

    def test_dump_requires_the_static_profile(self):
        directory, path = self.workspace()
        code, _, stderr = self.invoke(['dump', path, '-o', directory / 'd.json'])
        self.assertEqual(code, 2)
        self.assertIn('different layout', stderr)


# --------------------------------------------------------------------------- fuzzing

GLOBALS = ['game', 'workspace', 'print', 'loadstring', 'end_', 'Instance']
FIELDS = ['Players', 'HttpGet', 'Name', 'Kick', 'x']
LITERALS = ['nil', 'true', 'false', '7', '1.5', '"s"', '"a b"', '"\\0\\255"']


def random_program(rng):
    lines, symbolic, constants = [], [], []

    def prefix(depth):
        roll = rng.random()
        if depth <= 0 or roll < 0.25:
            if symbolic and rng.random() < 0.5:
                return rng.choice(symbolic)
            return rng.choice(GLOBALS)
        base = prefix(depth - 1)
        if roll < 0.5:
            return base + '.' + rng.choice(FIELDS) if rng.random() < 0.8 else base + '["a b"]'
        if roll < 0.75:
            return f'{base}({arguments(depth - 1)})'
        return f'{base}:{rng.choice(FIELDS)}({arguments(depth - 1)})'

    def argument(depth):
        roll = rng.random()
        if roll < 0.4:
            return rng.choice(LITERALS + constants)
        return prefix(depth)

    def arguments(depth):
        return ', '.join(argument(depth) for _ in range(rng.randint(0, 3)))

    for number in range(rng.randint(1, 5)):
        if rng.random() < 0.5:
            lines.append(f'local v{number} = {prefix(rng.randint(1, 3))}')
            symbolic.append(f'v{number}')
        elif rng.random() < 0.3:
            lines.append(f'local k{number} = {rng.choice(LITERALS)}')
            constants.append(f'k{number}')
        else:
            lines.append(call_statement(rng, prefix, arguments))
    lines.append(call_statement(rng, prefix, arguments))
    return '\n'.join(lines)


def call_statement(rng, prefix, arguments):
    head = prefix(rng.randint(0, 2))
    if rng.random() < 0.5:
        return f'{head}({arguments(2)})'
    return f'{head}:{rng.choice(FIELDS)}({arguments(2)})'


class TraceFuzzTests(LuauCase):
    def test_random_programs_replay_the_same_operations(self):
        import os
        import random
        cases = int(os.environ.get('MOONVEIL_FUZZ_CASES', '120')) // 2
        seed0 = int(os.environ.get('MOONVEIL_FUZZ_SEED', '0'))
        for seed in range(seed0, seed0 + cases):
            program = random_program(random.Random(f'trace:{seed}'))
            with self.subTest(seed=seed):
                try:
                    first = trace(program, self.luau, 10)
                except MoonVeilError as exc:
                    self.fail(f'the generator produced an untraceable program: {exc}\n{program}')
                source = emit_trace(first)
                second = trace(source, self.luau, 10)
                self.assertEqual(canonical_log(second), canonical_log(first), f'{program}\n--- emitted\n{source}')
