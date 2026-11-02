import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/rubric_luna_batch03_recovery_v1.py'
spec = importlib.util.spec_from_file_location('repair', SCRIPT)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class RecoveryTests(unittest.TestCase):
    def test_unresolved_new_attempt_blocks_even_if_capacity_available(self):
        state = dict(old_remote_holds={str(i): {} for i in range(6)}, active={},
                     new_remote_holds={'new': {}}, stop_reason=None)
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            r.assert_dispatch_allowed(state)
        self.assertEqual(len(state['old_remote_holds']), 6)

    def test_global_cap_counts_old_holds(self):
        state = dict(old_remote_holds={str(i): {} for i in range(12)}, active={},
                     new_remote_holds={}, stop_reason=None)
        with self.assertRaisesRegex(ValueError, 'concurrency_cap'):
            r.assert_dispatch_allowed(state)
        state['old_remote_holds'].pop('11')
        r.assert_dispatch_allowed(state)

    def test_crash_after_intent_never_resubmits(self):
        with tempfile.TemporaryDirectory() as temp:
            repair = Path(temp)
            identity = {'stage': 'initial_generation'}
            folder = repair / 'attempts/t__0/initial_generation/attempt_1'
            r.durable(folder / 'launch_intent.json', {'sent': 'unknown'})
            with patch.object(r, 'REPAIR', repair), patch.object(r, 'validate_input', return_value=(identity, {})):
                with self.assertRaisesRegex(ValueError, 'already_launched'):
                    asyncio.run(r.execute_one({'task_id': 't:0', 'old_folder': 'unused'}, {}))

    def test_crash_after_complete_releases_only_new_reservation(self):
        with tempfile.TemporaryDirectory() as temp:
            repair = Path(temp)
            identity = {'stage': 'initial_generation'}
            folder = repair / 'attempts/t__0/initial_generation/attempt_1'
            r.durable(folder / 'completed.json', {})
            old = {'old1': {}, 'old2': {}}
            state = dict(old_remote_holds=copy.deepcopy(old), active={'key': {}}, new_remote_holds={})
            with patch.object(r, 'REPAIR', repair), patch.object(r, 'validate_input', return_value=(identity, {})), patch.object(r, 'selected_result', return_value='{}'):
                asyncio.run(r.execute_one({'task_id': 't:0', 'old_folder': 'unused', 'key': 'key'}, state))
            self.assertEqual(state['old_remote_holds'], old)
            self.assertEqual(state['active'], {})

    def test_changed_input_rejected_before_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            r.durable(folder / 'identity.json', {'input_sha256': 'wrong'})
            r.durable(folder / 'input.json', {'role': 'judge', 'data': 'changed'})
            with self.assertRaisesRegex(ValueError, 'message_hash_mismatch'):
                r.validate_input(folder)

    def test_second_runner_cannot_acquire_same_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '.runner.lock'
            code = ('import importlib.util; from pathlib import Path; '
                    f's=importlib.util.spec_from_file_location("r",{str(SCRIPT)!r}); '
                    'r=importlib.util.module_from_spec(s); s.loader.exec_module(r); '
                    f'c=r.locked(Path({str(path)!r})); c.__enter__()')
            with r.locked(path):
                result = subprocess.run([sys.executable, '-c', code], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            with r.locked(path):
                pass

    def test_immutable_record_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'evidence.json'
            r.durable(path, {'original': 1}, immutable=True)
            with self.assertRaisesRegex(ValueError, 'immutable_record_conflict'):
                r.durable(path, {'original': 2}, immutable=True)
            self.assertEqual(r.read(path), {'original': 1})


if __name__ == '__main__':
    unittest.main()
