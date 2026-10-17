import unittest
from unittest import mock

from moonveil import emit as emit_module
from moonveil.emit import emit, identifier
from moonveil.errors import MoonVeilError
from helpers import ir_of


def structured(ops, params=0):
    return emit(ir_of(ops, params=params), 'structured')[0].split('\n', 1)[1]


def call_return(a=1, b=1, c=1, start=1, n=1):
    return ('call_return', a, b, c, start, n)


class StructuredEmissionTests(unittest.TestCase):
    def test_nested_calls_and_method_become_the_original_expression(self):
        ops = [('getglobal', 1, b'loadstring'), ('getglobal', 2, b'game'), ('loadk', 4, b'u'),
               ('self', 2, 2, b'HttpGet'), ('call', 2, 3, 0), ('call', 1, 0, 2), call_return()]
        self.assertEqual(structured(ops), 'loadstring(game:HttpGet("u"))()\n')

    def test_method_lookup_is_not_fused_when_arguments_have_effects(self):
        # Luau runs the NAMECALL lookup after evaluating the arguments; the VM runs it first.
        ops = [('getglobal', 1, b'game'), ('self', 1, 1, b'm'), ('getglobal', 3, b'g'), call_return(1, 3, 1, 1, 1)]
        source = structured(ops)
        self.assertNotIn('game:m(', source)
        self.assertEqual(source.splitlines(), ['local receiver = game', 'local m_m = receiver.m', 'm_m(receiver, g)'])
        # Pure arguments keep the readable form.
        self.assertEqual(structured([('getglobal', 1, b'game'), ('self', 1, 1, b'm'), ('loadk', 3, 7), call_return(1, 3)]),
                         'game:m(7)\n')

    def test_non_identifier_names_use_the_environment_table(self):
        source = structured([('getglobal', 1, b'a b'), call_return()])
        self.assertEqual(source.splitlines(), ['local environment = getfenv()', 'environment["a b"]()'])
        self.assertIn('environment["end"]', structured([('getglobal', 1, b'end'), call_return()]))
        self.assertIsNone(identifier(b'end'))
        self.assertIsNone(identifier('é'.encode()))
        self.assertEqual(identifier(b'_ok1'), '_ok1')

    def test_constant_callee_is_parenthesized_and_guarded(self):
        self.assertEqual(structured([('loadk', 1, 5), call_return()]), ';(5)()\n')

    def test_a_call_in_the_last_position_keeps_one_value(self):
        ops = [('getglobal', 1, b'sink'), ('getglobal', 2, b'echo'), ('call', 2, 1, 2), call_return(1, 2)]
        self.assertIn('sink((echo()))', structured(ops))
        returned = structured([('getglobal', 1, b'echo'), call_return(1, 1, 2, 1, 2)])
        self.assertEqual(returned, 'return (echo())\n')
        self.assertEqual(structured([('getglobal', 1, b'echo'), call_return(1, 1, 0, 1, 0)]), 'return echo()\n')

    def test_generated_names_never_shadow_globals_the_program_reads(self):
        ops = [('getglobal', 0, b'sink'), ('getglobal', 1, b'producer'), ('call', 1, 1, 3),
               ('getglobal', 3, b'result'), call_return(0, 3, 1, 1, 1)]
        source = structured(ops)
        self.assertNotIn('local result ', source)
        self.assertNotIn('local result,', source)
        self.assertIn('result_1', source)

    def test_unused_parameters_are_not_declared(self):
        ops = [('getglobal', 0, b'echo'), call_return(0, 2, 1, 1, 1)]
        self.assertEqual(structured(ops, params=2), 'local arg1 = ...\necho(arg1)\n')
        ops = [('getglobal', 0, b'echo'), call_return(0, 3, 1, 1, 1)]
        self.assertEqual(structured(ops, params=2), 'local arg1, arg2 = ...\necho(arg1, arg2)\n')
        ops = [('getglobal', 0, b'echo'), ('call', 0, 1, 1), ('getglobal', 1, b'sink'), call_return(1, 1, 1, 1, 1)]
        self.assertNotIn('...', structured(ops, params=2))

    def test_order_of_effects_is_preserved_by_materializing(self):
        # f is read first but the argument is read before f in program order.
        ops = [('getglobal', 2, b'arg'), ('getglobal', 1, b'f'), call_return(1, 2, 1, 1, 1)]
        source = structured(ops)
        self.assertLess(source.index('arg'), source.index('f'))
        self.assertNotIn('f(arg)', source)

    def test_stale_registers_reject_structured_but_not_auto(self):
        ops = [('loadk', 3, 99), ('getglobal', 1, b'short'), ('call', 1, 1, 0), ('getglobal', 0, b'sink'),
               call_return(0, 2, 1, 3, 2)]
        ir = ir_of(ops)
        self.assertEqual(emit(ir, 'auto')[1], 'registers')
        with self.assertRaisesRegex(MoonVeilError, '--style registers'):
            emit(ir, 'structured')
        with self.assertRaises(MoonVeilError):
            emit(ir, 'bogus')

    def test_local_budget_falls_back_to_registers(self):
        ops = [('getglobal', 1, b'producer'), ('call', 1, 1, 3), ('getglobal', 0, b'sink'), call_return(0, 3, 1, 1, 1)]
        ir = ir_of(ops)
        self.assertEqual(emit(ir, 'auto')[1], 'structured')
        with mock.patch.object(emit_module, 'MAX_LOCALS', 1):
            self.assertEqual(emit(ir, 'auto')[1], 'registers')

    def test_deep_nesting_is_split_into_statements(self):
        ops = [('getglobal', 1, b'echo'), ('getglobal', 2, b'echo'), ('getglobal', 3, b'echo'), ('getglobal', 4, b'sink'),
               ('call', 3, 2, 0), ('call', 2, 0, 0), ('call', 1, 0, 0)]
        ops = ops[:4] + [('call', 3, 1, 0), ('call', 2, 0, 0), call_return(1, 0, 0, 1, 0)]
        deep = emit(ir_of(ops), 'structured')[0]
        with mock.patch.object(emit_module, 'MAX_DEPTH', 2):
            shallow = emit(ir_of(ops), 'structured')[0]
        self.assertGreater(shallow.count('\n'), deep.count('\n'))
        self.assertIn('table.pack(', shallow)
        self.assertIn('table.unpack(', shallow)

    def test_registers_style_is_always_available(self):
        ops = [('loadk', 1, b'x'), call_return(1, 1, 1, 1, 1)]
        source, profile = emit(ir_of(ops), 'registers')
        self.assertEqual(profile, 'registers')
        self.assertIn('r[1] = "x"', source)
