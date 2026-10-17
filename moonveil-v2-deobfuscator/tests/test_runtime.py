import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from moonveil.errors import MoonVeilError
from moonveil.runtime import positive_memory, run_bounded

POSIX = os.name == 'posix'


class RuntimeLimitTests(unittest.TestCase):
    def run_python(self, code, **options):
        with tempfile.TemporaryDirectory() as directory:
            options.setdefault('timeout', 10)
            return run_bounded([sys.executable, '-I', '-c', code], cwd=Path(directory), **options)

    def test_memory_limit_validation(self):
        self.assertEqual(positive_memory(0), 0)
        for bad in (-1, 1.5, '64', None, True.__class__):
            with self.subTest(bad=bad), self.assertRaises(MoonVeilError):
                positive_memory(bad)

    @unittest.skipUnless(POSIX, 'resource limits are POSIX only')
    def test_memory_limit_stops_runaway_allocation(self):
        with self.assertRaisesRegex(MoonVeilError, 'exited with code|killed by'):
            self.run_python('x = bytearray(3 * 1024 ** 3)', memory_mb=512)
        # Zero disables the limit entirely.
        self.assertEqual(self.run_python('print(len(bytearray(10 ** 6)))', memory_mb=0)[0].strip(), '1000000')

    @unittest.skipUnless(POSIX, 'process groups are POSIX only')
    def test_timeout_kills_the_whole_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'grandchild.pid'
            code = ('import subprocess, sys, time\n'
                    f'child = subprocess.Popen([sys.executable, "-I", "-c", "import os, time; open({str(marker)!r}, \'w\').write(str(os.getpid())); time.sleep(60)"])\n'
                    'time.sleep(60)\n')
            with self.assertRaisesRegex(MoonVeilError, 'timeout'):
                run_bounded([sys.executable, '-I', '-c', code], cwd=Path(directory), timeout=1.0)
            pid = int(marker.read_text())
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                os.kill(pid, signal.SIGKILL)
                self.fail('the grandchild survived the timeout')

    def test_child_does_not_inherit_the_environment(self):
        with mock.patch.dict(os.environ, {'MOONVEIL_TEST_SECRET': 'leak'}):
            out, _ = self.run_python('import os; print(os.environ.get("MOONVEIL_TEST_SECRET"))')
        self.assertEqual(out.strip(), 'None')

    @unittest.skipUnless(POSIX, 'signals are POSIX only')
    def test_signal_exits_are_described(self):
        with self.assertRaisesRegex(MoonVeilError, 'luau-test was killed by SIGKILL'):
            self.run_python('import os, signal; os.kill(os.getpid(), signal.SIGKILL)', label='luau-test')
