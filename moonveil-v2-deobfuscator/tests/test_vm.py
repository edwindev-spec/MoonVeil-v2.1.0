import copy
import unittest

from moonveil.decoder import parse_dump
from moonveil.errors import MoonVeilError
from moonveil.vm import emit, lua_literal, normalize
from helpers import graph, instruction, terminal


class VMTests(unittest.TestCase):
    def test_both_ciphers_and_nul_bytes(self):
        key = b'\x00\xff'
        value = b'hi\x00\xff0123' * 40
        for kind, bias in ((4, 42), (8, 12)):
            encoded = bytes(byte ^ key[i % len(key) if kind == 4 else -1 - i % len(key)] ^ (i % 256 if kind == 4 else 0) for i, byte in enumerate(value))
            data = graph([instruction(kind, f12=bias + 1, f4=key), instruction(1, f12=20, f4=encoded), terminal()])
            before = copy.deepcopy(data)
            ir = normalize(data)
            self.assertEqual(ir['instructions'][1]['value'], value)
            self.assertEqual(ir['instructions'][0]['op'], 'nop')
            self.assertEqual(data, before)

    def test_decoder_rejects_empty_key_backward_and_out_of_range(self):
        for key, distance in ((b'', 1), (b'x', 0), (b'x', 99)):
            data = graph([instruction(4, f12=42 + distance, f4=key), terminal()])
            with self.subTest(key=key, distance=distance), self.assertRaises(MoonVeilError):
                normalize(data)

    def test_unknown_handler_and_invalid_register(self):
        for ins in (instruction(99), instruction(1, f12=999, f4=0), instruction(1, f12=b'bad', f4=0)):
            with self.subTest(ins=ins), self.assertRaises(MoonVeilError):
                normalize(graph([ins, terminal()]))

    def test_graph_gaps_and_nested_prototypes_rejected(self):
        data = graph([terminal()])
        data['tables'][1]['entries'][0]['key']['value'] = 2
        with self.assertRaisesRegex(MoonVeilError, 'gaps'):
            normalize(data)
        data = graph([terminal()])
        data['tables'][2]['entries'].append({'key': {'type': 'number', 'value': 1}, 'value': {'type': 'table', 'id': 1}})
        with self.assertRaisesRegex(MoonVeilError, 'nested'):
            normalize(data)

    def test_binary_luau_literals(self):
        self.assertEqual(lua_literal(b'\x001"\\\xff'), '"\\0001\\"\\\\\\255"')

    def test_dump_protocol_validation(self):
        valid = 'MOONVEIL210_BEGIN\nR\tT1\nH\t0\tnop\nT\t1\nMOONVEIL210_END\n'
        self.assertEqual(len(parse_dump(valid)['tables']), 1)
        for broken in (valid.replace('T\t1\n', ''), valid.replace('T\t1\n', 'T\t1\nT\t1\n'), valid.replace('H\t0\tnop', 'H\t0\tunknown'), valid.replace('MOONVEIL210_END', '')):
            with self.subTest(broken=broken), self.assertRaises(MoonVeilError):
                parse_dump(broken)

    def test_generic_emission_keeps_open_counts_and_environment(self):
        ir = normalize(graph([instruction(7, f12=37, f4=b'r'), terminal(b=0, c=0, return_b=0)]))
        source, profile = emit(ir)
        self.assertEqual(profile, 'registers')
        self.assertIn('environment["r"]', source)
        self.assertIn('values.n', source)
        self.assertIn('return unpack(r, 1, top)', source)
