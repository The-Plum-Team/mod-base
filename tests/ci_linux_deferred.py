"""Hosted proof that the real worker launcher cannot leave deferred execution behind.

Explicitly invoked by CI, never collected by the plain suite. All probes run under the
kit's actual sudo/setpriv launcher; no accounts, processes or system calls are mocked.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

from mod_base.build_ci.worker import (WORKER_ACCOUNTS, WorkerExecutionError,
                                      authenticate_worker_account, terminate_worker)
from tests import ci_linux_worker as hosted


_PROBE = r'''
import json, os, shutil, socket, stat, subprocess, sys, time
from pathlib import Path

home = Path(os.environ['HOME'])
uid = os.getuid()
rows = []

def probe(name, argv, data=None, environment=None):
    if shutil.which(argv[0]) is None:
        rows.append([name, 'absent', 'tool not installed'])
        return False
    try:
        result = subprocess.run(argv, input=data, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=10,
                                env=environment, check=False)
        rows.append([name, 'accepted' if result.returncode == 0 else 'refused',
                     result.stdout.strip()[:1500]])
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        rows.append([name, 'refused', 'request timed out after 10 seconds'])
        return False

print('context=' + json.dumps({
    'uid': uid, 'groups': os.getgroups(),
    'runtime_dir': os.environ.get('XDG_RUNTIME_DIR'),
    'run_user_exists': Path(f'/run/user/{uid}').exists(),
    'no_new_privs': [line for line in Path('/proc/self/status').read_text().splitlines()
                     if line.startswith('NoNewPrivs:')],
}), flush=True)
probe('user-manager-without-linger', ['systemd-run', '--user', '--no-ask-password',
      '--unit=mb-deferred-before', '--on-active=60s', '/usr/bin/touch', str(home/'fired-before')])
probe('linger', ['loginctl', '--no-ask-password', 'enable-linger', os.environ['USER']])
for attempt in range(40):
    if Path(f'/run/user/{uid}/bus').exists():
        break
    time.sleep(0.1)
user_env = dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{uid}',
                DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{uid}/bus')
probe('user-timer-with-linger', ['systemd-run', '--user', '--no-ask-password',
      '--unit=mb-deferred-timer', '--on-active=60s', '/usr/bin/touch', str(home/'fired-user')],
      environment=user_env)
probe('system-timer', ['systemd-run', '--no-ask-password', '--unit=mb-deferred-system-'+str(uid),
      '--on-active=60s', '/usr/bin/touch', str(home/'fired-system')])
probe('crontab', ['crontab', '-'], '* * * * * /bin/sleep 20; /usr/bin/touch '+str(home/'fired-cron')+'\n')
probe('at', ['at', 'now', '+', '1', 'minute'], '/bin/sleep 20; /usr/bin/touch '+str(home/'fired-at')+'\n')
probe('batch', ['batch'], '/usr/bin/sleep 70; /usr/bin/touch '+str(home/'fired-batch')+'\n')
probe('pkexec-helper', ['pkexec', '--disable-internal-agent', '/usr/bin/touch', str(home/'fired-pkexec')])
# A user manager is started by PID 1, outside the launcher's NoNewPrivs ancestry.
# Try scheduling through it too, so a refusal of setuid helpers in the hook is not
# mistaken for a refusal of the same helpers in a user service.
for mechanism, argv, data in (
    ('crontab', ['crontab', '-'], '* * * * * /bin/sleep 20; /usr/bin/touch '+str(home/'fired-service-cron')+'\n'),
    ('at', ['at', 'now', '+', '1', 'minute'], '/bin/sleep 20; /usr/bin/touch '+str(home/'fired-service-at')+'\n'),
    ('batch', ['batch'], '/usr/bin/sleep 70; /usr/bin/touch '+str(home/'fired-service-batch')+'\n'),
):
    program = ('import subprocess;from pathlib import Path;'
               'print([line for line in Path("/proc/self/status").read_text().splitlines() '
               'if line.startswith("NoNewPrivs:")],flush=True);'
               'raise SystemExit(subprocess.run('+repr(argv)+',input='+repr(data)+',text=True).returncode)')
    probe('user-service-'+mechanism,
          ['systemd-run', '--user', '--no-ask-password', '--wait', '--pipe', '--collect',
           '--unit=mb-deferred-'+mechanism, sys.executable, '-I', '-B', '-c', program],
          environment=user_env)
service_root = home/'.local/share/dbus-1/services'
service_root.mkdir(parents=True)
service_program = home/'dbus-deferred.py'
service_program.write_text('from pathlib import Path;import time\n'
                          +f'Path({str(home/"armed-dbus")!r}).write_text("started")\n'
                          +'time.sleep(70)\n'
                          +f'Path({str(home/"fired-dbus")!r}).write_text("survived")\n')
(service_root/'org.modbase.Deferred.service').write_text(
    '[D-BUS Service]\nName=org.modbase.Deferred\nExec='+sys.executable+' -I -B '+str(service_program)+'\n')
requested = probe('dbus-user-activation',
                  ['dbus-send', '--session', '--type=method_call', '--dest=org.modbase.Deferred',
                   '/', 'org.modbase.Deferred.Ping'], environment=user_env)
if requested:
    for attempt in range(30):
        if (home/'armed-dbus').exists():
            break
        time.sleep(0.1)
    assert (home/'armed-dbus').exists(), 'D-Bus accepted the request but did not start the probe'
bus = subprocess.run(['busctl', '--system', '--no-pager', 'list'], text=True,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
print('system-bus=' + json.dumps(bus.stdout), flush=True)
sockets = []
for directory, names, files in os.walk('/run', followlinks=False):
    for name in files:
        path = Path(directory)/name
        try:
            info = path.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_mode & stat.S_IWOTH:
                sockets.append(str(path))
        except OSError:
            pass
print('world-writable-run-sockets=' + json.dumps(sockets), flush=True)
for path in ('/run/docker.sock', '/run/containerd/containerd.sock'):
    if not Path(path).exists():
        rows.append([path, 'absent', 'socket not installed'])
        continue
    connection = socket.socket(socket.AF_UNIX)
    try:
        connection.connect(path)
    except OSError as error:
        rows.append([path, 'refused', str(error)])
    else:
        rows.append([path, 'accepted', 'connection admitted'])
    finally:
        connection.close()
# setsid/double-fork changes neither real nor effective uid. Leave a real child
# with no inherited output pipe: the kit must reject the hook and kill that child.
pid = os.fork()
if pid == 0:
    os.setsid()
    child = os.fork()
    if child:
        os._exit(0)
    null = os.open('/dev/null', os.O_RDWR)
    for descriptor in (0, 1, 2):
        os.dup2(null, descriptor)
    time.sleep(70)
    (home/'fired-detached').write_text('survived')
    os._exit(0)
os.waitpid(pid, 0)
rows.append(['detached-session', 'accepted', 'real double-fork child, setsid, closed output pipes'])
print('deferred=' + json.dumps(rows), flush=True)
'''


class LinuxDeferredExecutionTests(hosted.HostedWorkerCase):
    run_dispatcher = hosted.LinuxWorkerTests.run_dispatcher

    def setUp(self):
        if sys.platform != 'linux' or Path('/proc/1/comm').read_text().strip() != 'systemd':
            self.skipTest('systemd is not PID 1; the deferred-execution proof is the hosted run')
        super().setUp()
        for service in ('systemd-logind', 'cron', 'dbus'):
            self.assertEqual(hosted.command('/usr/bin/systemctl', 'is-active', service).stdout.strip(), b'active')
        for tool in ('loginctl', 'systemd-run', 'crontab', 'busctl'):
            self.assertTrue(Path('/usr/bin', tool).is_file(), f'known Ubuntu mechanism absent: {tool}')
        if Path('/usr/bin/at').is_file():
            self.assertEqual(hosted.command('/usr/bin/systemctl', 'is-active', 'atd').stdout.strip(), b'active')

    def assert_revoked(self, role):
        account = authenticate_worker_account(role)
        name = WORKER_ACCOUNTS[role]
        for flag in ('-u', '-U'):
            self.assertEqual(hosted.command('/usr/bin/pgrep', flag, str(account.uid), accepted=(0, 1)).stdout, b'')
        for path in (f'/var/lib/systemd/linger/{name}', f'/run/user/{account.uid}',
                     f'/var/spool/cron/crontabs/{name}'):
            hosted.command('/usr/bin/sudo', '-n', '/usr/bin/test', '!', '-e', path)
        units = (f'user@{account.uid}.service', f'user-runtime-dir@{account.uid}.service',
                 f'user-{account.uid}.slice', f'mb-deferred-system-{account.uid}.timer',
                 f'mb-deferred-system-{account.uid}.service')
        state = hosted.command('/usr/bin/systemctl', 'show', '--property=ActiveState', '--value', *units)
        self.assertEqual(state.stdout.split(), [b'inactive'] * len(units))
        if Path('/usr/bin/atq').is_file():
            jobs = hosted.command('/usr/bin/sudo', '-n', '/usr/bin/atq').stdout.decode().splitlines()
            self.assertFalse(any(row.split()[-1] == name for row in jobs), jobs)
            # atd removes queue entries when it starts them; examine their spool ownership too.
            spool = hosted.command('/usr/bin/sudo', '-n', '/usr/bin/find', '/var/spool/cron/atjobs',
                                   '-mindepth', '1', '-uid', str(account.uid), '-print').stdout
            self.assertEqual(spool, b'')
        shadow = hosted.command('/usr/bin/sudo', '-n', '/usr/bin/getent', 'shadow', name).stdout.split(b':')
        self.assertTrue(shadow[1].startswith(b'!'))
        self.assertEqual(shadow[7], b'1')
        markers = hosted.command('/usr/bin/sudo', '-n', '/usr/bin/find', account.home,
                                 '-name', 'fired-*', '-print').stdout
        self.assertEqual(markers, b'')

    def test_real_worker_deferred_requests(self):
        for role in WORKER_ACCOUNTS:
            with self.subTest(role=role):
                started = time.monotonic()
                try:
                    result = self.run_dispatcher(_PROBE, role=role, timeout=120)
                except WorkerExecutionError as error:
                    self.assertEqual(str(error), 'worker dispatcher left a process behind')
                    result = error.result
                else:
                    self.fail('the hook left a detached process but the kit accepted it')
                self.assertEqual(result.returncode, 0, result.log.decode())
                lines = result.log.decode().splitlines()
                rows = json.loads(next(line.removeprefix('deferred=') for line in lines
                                       if line.startswith('deferred=')))
                self.assert_revoked(role)
                for mechanism, outcome, evidence in rows:
                    if outcome == 'accepted':
                        self.assertNotIn(mechanism, ('/run/docker.sock', '/run/containerd/containerd.sock',
                                                    'pkexec-helper', 'system-timer'))
                        outcome = 'neutralised'
                    self.assertIn(outcome, ('refused', 'neutralised', 'absent'))
                    if outcome == 'absent':
                        self.assertIn(mechanism, ('at', 'batch', 'pkexec-helper', '/run/docker.sock',
                                                 '/run/containerd/containerd.sock'))
                    print(f'{role} | {mechanism} | {outcome} | {evidence or "exit 0"}',
                          file=sys.stderr, flush=True)
                for line in lines:
                    if not line.startswith('deferred='):
                        print(role, line, file=sys.stderr, flush=True)
                print(f'{role} probe and termination: {time.monotonic()-started:.3f}s',
                      file=sys.stderr, flush=True)
                started = time.monotonic()
                terminate_worker(authenticate_worker_account(role))
                print(f'{role} idempotent termination: {time.monotonic()-started:.3f}s',
                      file=sys.stderr, flush=True)
        self.assertEqual(hosted.command('/usr/bin/systemctl', 'is-active', 'polkit').stdout.strip(), b'active')
        # Cross a cron/at minute boundary and the longer detached/batch marker delay.
        for phase in range(2):
            print(f'Waiting 50 seconds across cron/at and timer deadlines ({phase+1}/2).',
                  file=sys.stderr, flush=True)
            time.sleep(50)
        for role in WORKER_ACCOUNTS:
            self.assert_revoked(role)

    def test_slow_user_manager_revocation_still_kills_and_locks(self):
        # A real user service can delay the manager's shutdown beyond the administrative
        # grace. This is a failing root command, not a replaced control call or system call.
        # Ubuntu's template drop-in kills the manager after five seconds. Override only
        # this fixture account's instance in /run to exercise exhaustion of our own grace.
        account = authenticate_worker_account('candidate')
        dropin = Path('/run/systemd/system') / f'user@{account.uid}.service.d'
        target = dropin / 'zz-mod-base-fixture.conf'
        source = self.boundary / 'user-manager-timeout.conf'
        source.write_text('[Service]\nTimeoutStopSec=60\n', encoding='utf-8', newline='\n')
        source.chmod(0o600)
        hosted.command('/usr/bin/sudo', '-n', '/usr/bin/test', '!', '-e', str(dropin))
        hosted.command('/usr/bin/sudo', '-n', '/usr/bin/mkdir', '--mode=0755', str(dropin))

        def remove_dropin():
            hosted.command('/usr/bin/sudo', '-n', '/usr/bin/rm', '--force', '--', str(target))
            hosted.command('/usr/bin/sudo', '-n', '/usr/bin/rmdir', '--', str(dropin))
            hosted.command('/usr/bin/sudo', '-n', '/usr/bin/systemctl', 'daemon-reload')

        self.addCleanup(remove_dropin)
        hosted.command('/usr/bin/sudo', '-n', '/usr/bin/install', '--mode=0644', str(source), str(target))
        hosted.command('/usr/bin/sudo', '-n', '/usr/bin/systemctl', 'daemon-reload')
        configured = hosted.command('/usr/bin/systemctl', 'show', '--property=TimeoutStopUSec', '--value',
                                    f'user@{account.uid}.service').stdout.strip()
        self.assertEqual(configured, b'1min')
        body = r'''
import os, subprocess, time
from pathlib import Path
name = os.environ['USER']
uid = os.getuid()
subprocess.run(['loginctl', '--no-ask-password', 'enable-linger', name], check=True)
environment = dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{uid}',
                   DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{uid}/bus')
for attempt in range(40):
    if Path(f'/run/user/{uid}/bus').exists():
        break
    time.sleep(0.1)
subprocess.run(['systemd-run', '--user', '--no-ask-password', '--unit=mb-deferred-slow-stop',
                '--property=Type=oneshot', '--property=RemainAfterExit=yes',
                '--property=ExecStop=/bin/sleep 60', '--property=TimeoutStopSec=60', '/usr/bin/true'],
               env=environment, check=True)
for attempt in range(40):
    state = subprocess.check_output(['systemctl', '--user', 'show', '--property=SubState', '--value',
                                     'mb-deferred-slow-stop.service'], env=environment).strip()
    if state == b'exited':
        break
    time.sleep(0.1)
assert state == b'exited', state
print('armed slow ExecStop', flush=True)
'''
        with self.assertRaisesRegex(WorkerExecutionError, 'control command timed out') as raised:
            self.run_dispatcher(body, timeout=120)
        self.assertIn(b'armed slow ExecStop', raised.exception.result.log)
        self.assert_revoked('candidate')


if __name__ == '__main__':
    unittest.main()
