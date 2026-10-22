import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_gateway.codex_runner import CodexRunnerError, run_codex
from codex_gateway.app import _run_codex_with_context_overflow_handling
from codex_gateway.session_store import SessionRecord, utc_now
from codex_gateway.structured_output import StructuredOutputError


class RunnerRegressions(unittest.TestCase):
    def test_large_stdin_new_and_resumed_and_real_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / 'fake-codex'
            cli.write_text('#!' + sys.executable + '\n' + '''
import json, pathlib, sys, time, subprocess
prompt = sys.stdin.read()
if prompt == 'hang':
    child = subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    pathlib.Path(__file__).with_name('child.pid').write_text(str(child.pid))
    time.sleep(10)
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(str(len(prompt)))
print(json.dumps({'type':'thread.started','thread_id':'thread-new'}))
''')
            cli.chmod(0o700)
            stamp = utc_now()
            for thread in (None, 'thread-original'):
                session = SessionRecord('probe', str(root), None, 'read-only', thread, stamp, stamp)
                with patch('codex_gateway.codex_runner.build_subprocess_env', return_value=os.environ.copy()):
                    result = run_codex(codex_bin=str(cli), session=session, prompt='x'*300000,
                                       timeout_seconds=3, allow_network=False, state_dir=root)
                    self.assertEqual(result.reply, '300000')
                    self.assertEqual(result.thread_id, 'thread-new')
                    start = time.monotonic()
                    with self.assertRaisesRegex(CodexRunnerError, 'timed out'):
                        run_codex(codex_bin=str(cli), session=session, prompt='hang',
                                  timeout_seconds=0.1, allow_network=False, state_dir=root)
                    self.assertLess(time.monotonic()-start, 2)
                    child = int((root / 'child.pid').read_text())
                    status = Path(f'/proc/{child}/stat')
                    if status.exists():
                        self.assertEqual(status.read_text().split()[2], 'Z', 'descendant still executing')

    def test_lost_context_never_replays_followup_into_new_thread(self):
        stamp = utc_now()
        session = SessionRecord('probe', '/tmp', None, 'read-only', 'old-thread', stamp, stamp)
        settings = SimpleNamespace(codex_bin='unused', allow_network=False, state_dir=Path('/tmp'))
        with patch('codex_gateway.app.run_codex', side_effect=CodexRunnerError('session lost', retryable=True)) as run:
            with self.assertRaises(StructuredOutputError) as raised:
                _run_codex_with_context_overflow_handling(session=session, prompt='CONTINUE', settings=settings, timeout_seconds=1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(session.thread_id, 'old-thread')
        self.assertEqual(raised.exception.code, 'session_context_lost')
        self.assertEqual(raised.exception.status_code, 409)


if __name__ == '__main__':
    unittest.main()
