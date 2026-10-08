"""Real planning subprocess with authored target code; pin/cache/API and writes are explicit seams."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_pin as fixtures


class BumpPlanningTests(unittest.TestCase):
    def exercise(self, *, reject=False, sync_exit=0):
        bootstrap = fixtures.BOOT
        with tempfile.TemporaryDirectory(prefix="mod-base-bump-plan-") as directory:
            root = Path(directory)
            repo = root / "mod with spaces '$data"
            repo.mkdir()
            caller = repo / 'caller.yml'
            caller.write_bytes(b'original pin and managed bytes\n')
            kit = root / 'verified-target'
            package = kit / 'src/mod_base'
            (package / 'template').mkdir(parents=True)
            (package / '__init__.py').write_text('', encoding='utf-8')
            (package / 'template/__init__.py').write_text('', encoding='utf-8')
            (package / 'errors.py').write_text(
                'def run_main(entry):\n'
                '    try: return entry()\n'
                '    except ValueError: return 2\n', encoding='utf-8')
            record = root / 'plan.json'
            (package / 'template/tool.py').write_text(
                'import json\nfrom pathlib import Path\n'
                'def sync(repo, *, kit_root, write):\n'
                '    assert write is False\n'
                f'    Path({str(record)!r}).write_text(json.dumps([str(repo), str(kit_root), write]))\n'
                + ('    raise ValueError("unsupported activation")\n' if reject else
                   '    return ["ordinary template drift"]\n'), encoding='utf-8')
            original = bootstrap.Pin(fixtures.SHA_A, 'v1.2.3', ())
            target = bootstrap.Pin(fixtures.SHA_B, 'v1.2.4', ())
            resolution = bootstrap.Resolution(kit, target, 'cache', True)
            environment = dict(os.environ, PYTHONPATH=str(kit / 'src'), PYTHONDONTWRITEBYTECODE='1')
            real_run = subprocess.run
            commands = []
            def run(command, **kwargs):
                commands.append(command)
                if '-c' in command:
                    rewrite.assert_not_called()
                    self.assertEqual(caller.read_bytes(), b'original pin and managed bytes\n')
                    return real_run(command, **kwargs)
                rewrite.assert_called_once_with(repo, target)
                return subprocess.CompletedProcess(command, sync_exit)
            with patch.object(bootstrap, 'parse_pin', side_effect=[original, target]), \
                    patch.object(bootstrap, 'resolve_tag', return_value=target.sha), \
                    patch.object(bootstrap, 'require_reachable'), \
                    patch.object(bootstrap, 'cached_kit', return_value=resolution), \
                    patch.object(bootstrap, 'kit_environment', return_value=environment), \
                    patch.object(bootstrap, 'rewrite_pin') as rewrite, \
                    patch.object(bootstrap.subprocess, 'run', side_effect=run):
                if reject or sync_exit:
                    expected = 'planning failed.*pin unchanged' if reject else 'sync --write failed'
                    with self.assertRaisesRegex(bootstrap.KitError, expected):
                        bootstrap.bump(repo, target.version, {}, get_json=lambda path: None)
                else:
                    self.assertEqual(bootstrap.bump(repo, target.version, {}, get_json=lambda path: None), target)
                if reject:
                    rewrite.assert_not_called()
                    self.assertEqual(len(commands), 1)
                else:
                    rewrite.assert_called_once_with(repo, target)
                    self.assertEqual(commands[1][-1], '--write')
                self.assertEqual(json.loads(record.read_text()), [str(repo), str(kit), False])
                self.assertEqual(caller.read_bytes(), b'original pin and managed bytes\n')

    def test_rejected_target_planning_never_rewrites_pin_or_syncs(self):
        self.exercise(reject=True)

    def test_ordinary_release_drift_does_not_prevent_bump(self):
        self.exercise()

    def test_write_failure_remains_a_rejection_after_successful_planning(self):
        self.exercise(sync_exit=2)
