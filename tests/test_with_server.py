"""Real subprocesses, large logs, descendants and port cleanup."""
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/webapp-testing/scripts/with_server.py'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.1):
            return True
    except OSError:
        return False


@unittest.skipUnless(os.name == 'posix', 'POSIX process-group behavior')
class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pidfile = self.root / 'pids'
        self.port = free_port()
        self.helper = self.root / 'server.py'
        self.helper.write_text('''import os, signal, socket, subprocess, sys, time
from pathlib import Path
pidfile, port, mode = sys.argv[1], int(sys.argv[2]), sys.argv[3]
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
Path(pidfile).write_text(str(os.getpid()) + ' ' + str(child.pid))
if mode == 'ignore': signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == 'noisy':
    os.write(1, b'x' * 2000000)
    os.write(2, b'y' * 2000000)
if mode == 'no-port': time.sleep(60)
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', port)); s.listen()
while True:
    c, _ = s.accept(); c.close()
''')
        self.addCleanup(self.emergency_cleanup)

    def emergency_cleanup(self):
        if self.pidfile.exists():
            for pid in map(int, self.pidfile.read_text().split()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def args(self, mode='noisy', code=0):
        cmd = shlex.join([sys.executable, str(self.helper), str(self.pidfile), str(self.port), mode])
        return [sys.executable, str(SCRIPT), '--server', cmd, '--port', str(self.port),
                '--timeout', '3', '--', sys.executable, '-c', f'import sys; sys.exit({code})']

    def assert_clean(self):
        self.assertFalse(listening(self.port), 'server port still listening')
        self.assertTrue(self.pidfile.exists())
        deadline = time.monotonic() + 3
        while True:
            living = []
            for pid in self.pidfile.read_text().split():
                result = subprocess.run(['ps', '-o', 'stat=', '-p', pid], capture_output=True, text=True)
                state = result.stdout.strip()
                if state and not state.startswith('Z'):
                    living.append(pid)
            if not living or time.monotonic() >= deadline:
                break
            time.sleep(.05)
        self.assertEqual([], living, 'live descendants remain')

    def run_helper(self, mode, code):
        with (self.root / 'log').open('wb') as log:
            result = subprocess.run(self.args(mode, code), stdout=log, stderr=log, timeout=15)
        self.assertEqual(code, result.returncode)
        self.assert_clean()

    def test_large_stdout_stderr_does_not_block_and_cleans_success(self):
        self.run_helper('noisy', 0)
        self.assertGreater((self.root / 'log').stat().st_size, 4000000)

    def test_failed_command_preserves_exit_code_and_cleans(self):
        self.run_helper('normal', 7)

    def test_term_resistant_descendants_are_killed(self):
        self.run_helper('ignore', 0)

    def test_startup_timeout_cleans_all_descendants(self):
        with (self.root / 'log').open('wb') as log:
            result = subprocess.run(self.args('no-port'), stdout=log, stderr=log, timeout=15)
        self.assertNotEqual(0, result.returncode)
        self.assert_clean()

    def test_occupied_port_is_rejected_without_starting_or_killing_owner(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', self.port))
            sock.listen()
            result = subprocess.run(self.args(), capture_output=True, timeout=10)
            self.assertNotEqual(0, result.returncode)
            self.assertFalse(self.pidfile.exists())
            self.assertTrue(listening(self.port))

    def test_interrupt_during_command_cleans_groups(self):
        args = self.args('normal')
        marker = self.root / 'command-started'
        args[-1] = f'from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(60)' 
        with (self.root / 'log').open('wb') as log:
            runner = subprocess.Popen(args, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(marker.exists(), (self.root / 'log').read_text())
                runner.send_signal(signal.SIGTERM)
                self.assertEqual(143, runner.wait(timeout=10), (self.root / 'log').read_text())
            finally:
                if runner.poll() is None:
                    runner.kill()
                    runner.wait()
        self.assert_clean()
