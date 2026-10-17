import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moonveil.cli import main
from moonveil.errors import MoonVeilError
from moonveil.runtime import positive_timeout, run_bounded
from helpers import FIXTURE


class CLITests(unittest.TestCase):
    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_analysis_requires_no_runtime(self):
        with patch('moonveil.cli.find_luau', side_effect=AssertionError('runtime used')):
            code, stdout, _ = self.invoke(['analyze', str(FIXTURE), '--json'])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)['supported'])

    def test_output_aliases_and_overlapping_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.lua'
            path.write_text(FIXTURE.read_text())
            alias = Path(directory) / 'hardlink.lua'
            os.link(path, alias)
            for output in (path, alias):
                code, _, stderr = self.invoke(['decompile', str(path), '-o', str(output)])
                self.assertEqual(code, 2)
                self.assertIn('overwrite', stderr)
            code, _, stderr = self.invoke(['decompile', str(path), '-o', str(Path(directory) / 'manifest.json'), '--artifacts', directory])
            self.assertEqual(code, 2)
            self.assertIn('overlap', stderr)
            self.assertEqual(path.read_bytes(), FIXTURE.read_bytes())

    def test_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'bad.lua', Path(directory) / 'out.luau'
            source.write_text('-- truncated')
            output.write_text('keep')
            code, _, _ = self.invoke(['decompile', str(source), '-o', str(output)])
            self.assertEqual(code, 2)
            self.assertEqual(output.read_text(), 'keep')

    def test_timeout_and_output_bound(self):
        for number in (0, -1, float('inf'), float('nan')):
            with self.subTest(number=number), self.assertRaises(MoonVeilError):
                positive_timeout(number)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MoonVeilError, 'timeout'):
                run_bounded([sys.executable, '-c', 'import time; time.sleep(3)'], cwd=Path(directory), timeout=0.05)
            with self.assertRaisesRegex(MoonVeilError, 'output exceeded'):
                run_bounded([sys.executable, '-c', 'print("x" * 10000)'], cwd=Path(directory), timeout=5, max_bytes=100)
