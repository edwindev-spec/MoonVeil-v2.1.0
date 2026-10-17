import unittest

from moonveil.analysis import indicators


def program(*operations):
    return {'instructions': [{'pc': i + 1, 'op': op, 'value': value} for i, (op, value) in enumerate(operations)]}


class IndicatorTests(unittest.TestCase):
    def test_remote_loader_is_flagged_from_strings_and_names(self):
        found = indicators(program(('getglobal', b'loadstring'), ('getglobal', b'game'), ('loadk', b'see https://a.example/x.lua, ok'),
                                   ('self', b'HttpGet')))
        self.assertEqual(found['urls'], ['https://a.example/x.lua'])
        self.assertEqual(found['globals'], ['loadstring', 'game'])
        self.assertEqual(found['methods'], ['HttpGet'])
        self.assertEqual([b['id'] for b in found['behaviors']], ['remote-code-loader'])

    def test_loadstring_alone_is_not_a_remote_loader(self):
        found = indicators(program(('getglobal', b'loadstring'), ('loadk', b'print(1)')))
        self.assertEqual(found['behaviors'], [])
        self.assertEqual(found['urls'], [])

    def test_webhook_and_duplicates(self):
        url = b'https://discord.com/api/webhooks/1/abc'
        found = indicators(program(('loadk', url), ('loadk', url), ('loadk', b'\xff\xfe')))
        self.assertEqual(found['urls'], [url.decode()])
        self.assertEqual([b['id'] for b in found['behaviors']], ['webhook-url'])
        self.assertEqual(len(found['strings']), 2)

    def test_non_string_constants_are_ignored(self):
        self.assertEqual(indicators(program(('loadk', 5), ('loadk', None)))['strings'], [])
