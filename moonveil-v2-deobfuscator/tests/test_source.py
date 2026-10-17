import unittest

from moonveil.errors import MoonVeilError
from moonveil.source import canonical, parse_wrapper, tokenize
from helpers import FIXTURE


class SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = FIXTURE.read_text()

    def test_sample_profile(self):
        wrapper = parse_wrapper(self.source)
        self.assertEqual(wrapper.roles["constructor"], "K")
        self.assertEqual(wrapper.roles["xor_reverse"], "H")
        self.assertEqual(len(wrapper.methods), 38)

    def test_tokenizer_ignores_delimiters_in_comments_and_strings(self):
        tokens = tokenize(' --[=[ function end { , ]=]\n "end,}" [==[if {,]==] -- ignored\n 0b101')
        self.assertEqual([token.kind for token in tokens], ["string", "long", "number"])

    def test_formatting_numeric_spelling_and_local_renaming(self):
        first = 'function(a) return a[0x10] end'
        second = 'function(other) -- comment\n return other[0b10000] end'
        self.assertEqual(canonical(first), canonical(second))
        self.assertNotEqual(canonical('bit32.band'), canonical('bit32.bor'))
        altered = self.source.replace('function(o,k,m,d,l,b,f)local e,_,n,h', 'function(o,k,m,d,l,b,f) -- test\nlocal e,_,n,h')
        self.assertTrue(parse_wrapper(altered))

    def test_wrong_version_truncation_and_extra_statements(self):
        for value in (self.source.replace('MoonVeil 2.1.0', 'MoonVeil 1.4.5'), self.source[:-10], self.source + '\nprint("extra")'):
            with self.subTest(tail=value[-40:]), self.assertRaises(MoonVeilError):
                parse_wrapper(value)

    def test_semantic_mutation_is_rejected(self):
        # Change the destination offset of GETGLOBAL without changing the banner.
        altered = self.source.replace('e,_=0b100100,l[0b1100]', 'e,_=0b100101,l[0b1100]')
        self.assertNotEqual(altered, self.source)
        with self.assertRaisesRegex(MoonVeilError, 'getglobal'):
            parse_wrapper(altered)

    def test_incomplete_token_and_long_comment(self):
        for value in ('"unfinished', '[=[unfinished', '--[=[unfinished'):
            with self.subTest(value=value), self.assertRaises(MoonVeilError):
                tokenize(value)
