#!/usr/bin/env python3
"""Create a mod's ``mod-base-gate`` environment and store the gate status App's credentials in it.

An owner operation of docs/NEW-MOD.md (step 5) and docs/OPERATIONS.md ("The gate status App"),
run with the owner's own ``gh`` login before the pull request that makes a mod manage the status
caller is merged:

    python3 tools/new_mod_env.py --repo The-Plum-Team/<mod> --key-file <path> --dry-run
    python3 tools/new_mod_env.py --repo The-Plum-Team/<mod> --key-file <path>

``--key-file`` is the path, chosen by the owner, of the App's private key (PEM) exported from the
password manager for this run; ``--app-json`` instead reads the JSON the App manifest flow returns
(``client_id`` and ``pem``). Either file must be readable by its owner alone: the program refuses a
file other users can read (POSIX: any group or other permission bit; Windows: an access list that
lets Everyone, Users, Authenticated Users, Guests, Anonymous, Interactive, Local or Network read
it). On Linux or WSL the path may be a pipe (``--key-file <(password-manager-cli ...)``), so the
key never lands on disk. The client ID is public: without ``--client-id`` it is read from
``gh api apps/plum-mod-base-gate``, whose App id must be 5256389.

The program restricts the environment's deployment branches to the repository's default branch
and checks that restriction on GitHub before it writes anything else. Only then does it set the
variable ``MOD_BASE_GATE_APP_CLIENT_ID`` and the secret ``MOD_BASE_GATE_APP_PRIVATE_KEY``; the key
reaches ``gh secret set`` on its standard input alone and is never printed, logged or put on a
command line (a ``gh`` diagnostic that would quote a line of it is redacted). It refuses, before
any write, when the environment admits another branch, has required reviewers or a wait timer
(either would hold every status), or when a repository or organization secret of the key's name
is visible to the repository: the key must live in this environment only. It never deletes
anything (no branch policy, variable, secret or file, the key file included), and it changes no
ruleset, App installation or other setting. ``--dry-run`` performs the reads and prints the plan.
Exit status 0 on success, 2 on any refusal or ``gh`` failure.

Standard library only, Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

ENVIRONMENT = "mod-base-gate"
CLIENT_ID_VARIABLE = "MOD_BASE_GATE_APP_CLIENT_ID"
PRIVATE_KEY_SECRET = "MOD_BASE_GATE_APP_PRIVATE_KEY"
#: The organization's gate status App (docs/OPERATIONS.md, "The gate status App").
APP_SLUG = "plum-mod-base-gate"
APP_ID = 5256389
#: The deployment branch policy the environment must end with: custom policies, which this program
#: limits to exactly one, the default branch by name.
BRANCH_POLICY = {"protected_branches": False, "custom_branch_policies": True}

REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
CLIENT_ID = re.compile(r"Iv[0-9A-Za-z._-]{4,64}")
PEM = re.compile(rb"-----BEGIN (?:RSA )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]+"
                 rb"-----END (?:RSA )?PRIVATE KEY-----[\r\n]*")   # a password manager may add line ends
MAX_KEY_BYTES = 16 * 1024
MAX_APP_JSON_BYTES = 64 * 1024
MAX_LISTED = 100
GH_TIMEOUT_SECONDS = 120
MAX_DIAGNOSTIC_CHARACTERS = 400
#: POSIX permission bits that let a user other than the owner read or change a credential file.
SHARED_MODE_BITS = 0o077
#: Windows well-known SIDs whose read access makes a file readable by other users of the machine.
BROAD_SIDS = {"S-1-1-0": "Everyone", "S-1-2-0": "Local", "S-1-5-2": "Network", "S-1-5-4": "Interactive",
              "S-1-5-7": "Anonymous", "S-1-5-11": "Authenticated Users", "S-1-5-32-545": "Users",
              "S-1-5-32-546": "Guests"}
#: Access rights that include reading a file's data: FILE_READ_DATA, GENERIC_ALL and GENERIC_READ.
READ_RIGHTS = 0x0000_0001 | 0x1000_0000 | 0x8000_0000
#: The shortest line of a key that a diagnostic is searched for (PEM body lines are 64 characters).
MIN_REDACTED_LINE = 16

#: ``(arguments, stdin) -> (exit status, stdout, stderr)`` for one ``gh`` invocation.
Runner = Callable[[Sequence[str], bytes | None], tuple[int, bytes, bytes]]


class SetupError(Exception):
    """A refusal; its message never holds key material."""


def gh_runner(executable: str) -> Runner:
    def run(arguments: Sequence[str], stdin: bytes | None) -> tuple[int, bytes, bytes]:
        try:
            completed = subprocess.run([executable, *arguments], input=b"" if stdin is None else stdin,
                                       capture_output=True, check=False, timeout=GH_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SetupError(f"cannot run {executable}: {type(error).__name__}") from None
        return completed.returncode, completed.stdout, completed.stderr
    return run


class GitHub:
    """The few ``gh`` calls this program makes; the standard input is never part of a message."""

    def __init__(self, run: Runner) -> None:
        self._run = run

    def call(self, *arguments: str, stdin: bytes | None = None) -> bytes:
        status, stdout, stderr = self._run(list(arguments), stdin)
        if status != 0:
            detail = stderr.decode("utf-8", "replace").strip()[-MAX_DIAGNOSTIC_CHARACTERS:]
            raise SetupError(f"gh {' '.join(arguments[:4])} failed ({status}): {detail or 'no message'}")
        return stdout

    def get(self, endpoint: str) -> Any:
        body = self.call("api", endpoint)
        try:
            return json.loads(body)
        except ValueError:
            raise SetupError(f"gh api {endpoint} did not return JSON") from None

    def names(self, endpoint: str, field: str) -> set[str]:
        document = self.get(f"{endpoint}?per_page={MAX_LISTED}")
        items = document.get(field) if isinstance(document, dict) else None
        if not isinstance(items, list) or not all(isinstance(i, dict) and isinstance(i.get("name"), str) for i in items):
            raise SetupError(f"gh api {endpoint} returned no {field} list")
        if document.get("total_count", len(items)) != len(items):
            raise SetupError(f"gh api {endpoint} lists more than {MAX_LISTED} {field}; check them by hand")
        return {item["name"] for item in items}


def windows_broad_readers(descriptor: int) -> list[str]:
    """The broad groups that the access list of an open file lets read it (Windows only)."""

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class AclHeader(ctypes.Structure):
        _fields_ = [("revision", ctypes.c_ubyte), ("sbz1", ctypes.c_ubyte), ("size", ctypes.c_ushort),
                    ("count", ctypes.c_ushort), ("sbz2", ctypes.c_ushort)]

    class AceHeader(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("size", ctypes.c_ushort)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security_info = advapi32.GetSecurityInfo
    get_security_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    get_security_info.restype = wintypes.DWORD
    get_ace = advapi32.GetAce
    get_ace.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    get_ace.restype = wintypes.BOOL
    sid_to_string = advapi32.ConvertSidToStringSidW
    sid_to_string.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    sid_to_string.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    se_file_object, dacl_security_information = 1, 0x4
    access_allowed_types, inherit_only = (0x0, 0x9), 0x8   # ACCESS_ALLOWED(_CALLBACK)_ACE_TYPE, INHERIT_ONLY_ACE
    dacl, security_descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    status = get_security_info(msvcrt.get_osfhandle(descriptor), se_file_object, dacl_security_information,
                               None, None, ctypes.byref(dacl), None, ctypes.byref(security_descriptor))
    if status != 0:
        raise SetupError(f"cannot read the access list of the credential file (Windows error {status})")
    readers: set[str] = set()
    try:
        if not dacl.value:
            return ["Everyone (the file has no access list)"]
        for index in range(ctypes.cast(dacl, ctypes.POINTER(AclHeader)).contents.count):
            ace = ctypes.c_void_p()
            if not get_ace(dacl, index, ctypes.byref(ace)):
                raise SetupError("cannot read the access list of the credential file")
            header = ctypes.cast(ace, ctypes.POINTER(AceHeader)).contents
            if header.type not in access_allowed_types or header.flags & inherit_only:
                continue
            if not ctypes.cast(ace.value + 4, ctypes.POINTER(wintypes.DWORD)).contents.value & READ_RIGHTS:
                continue
            text = wintypes.LPWSTR()
            if not sid_to_string(ctypes.c_void_p(ace.value + 8), ctypes.byref(text)):
                raise SetupError("cannot read the access list of the credential file")
            try:
                if text.value in BROAD_SIDS:
                    readers.add(BROAD_SIDS[text.value])
            finally:
                local_free(ctypes.cast(text, ctypes.c_void_p))
    finally:
        local_free(security_descriptor)
    return sorted(readers)


def refuse_shared(descriptor: int, status: os.stat_result, label: str, path: Path) -> None:
    """Refuse a credential file that a user other than its owner can read. A Windows pipe has no
    file to share: its bytes go to this process alone."""

    if os.name == "nt":
        readers = windows_broad_readers(descriptor) if stat.S_ISREG(status.st_mode) else []
        if readers:
            raise SetupError(f"{label} {path} is readable by {', '.join(readers)}: keep it readable by you alone "
                             "(see docs/NEW-MOD.md step 5)")
    elif status.st_mode & SHARED_MODE_BITS:
        raise SetupError(f"{label} {path} has mode {stat.S_IMODE(status.st_mode):04o}, open to other users: "
                         "keep it readable by you alone (chmod 600)")


def read_bounded(path: Path, limit: int, label: str) -> bytes:
    """Read a credential file that its owner alone can read; a pipe (``-`` for the standard input, or
    a process substitution) is accepted, a terminal is not."""

    try:
        if str(path) == "-":
            descriptor = os.dup(sys.stdin.fileno())
            if os.name == "nt":
                import msvcrt
                msvcrt.setmode(descriptor, os.O_BINARY)
        else:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        raise SetupError(f"{label} {path} does not exist") from None
    except (OSError, ValueError) as error:
        raise SetupError(f"{label} {path} cannot be opened ({type(error).__name__})") from None
    with os.fdopen(descriptor, "rb") as stream:
        status = os.fstat(descriptor)
        if not (stat.S_ISREG(status.st_mode) or stat.S_ISFIFO(status.st_mode)):
            raise SetupError(f"{label} {path} is not a regular file or a pipe")
        refuse_shared(descriptor, status, label, path)
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise SetupError(f"{label} {path} is larger than {limit} bytes")
    return data


def load_credentials(app_json: Path | None, client_id: str | None,
                     key_file: Path | None) -> tuple[str | None, bytes]:
    """The App's client ID (``None``: read it from GitHub) and PEM key, from the manifest flow's JSON
    or from ``--key-file`` with an optional ``--client-id``."""

    if app_json is not None:
        if client_id is not None or key_file is not None:
            raise SetupError("give either --app-json or --client-id with --key-file, not both")
        try:
            document = json.loads(read_bounded(app_json, MAX_APP_JSON_BYTES, "--app-json"))
        except ValueError:
            raise SetupError(f"--app-json {app_json} is not JSON") from None
        if (not isinstance(document, dict) or not isinstance(document.get("client_id"), str)
                or not isinstance(document.get("pem"), str)):
            raise SetupError(f"--app-json {app_json} has no string client_id and pem")
        client_id, key = document["client_id"], document["pem"].encode("utf-8")
        source = f"the pem of {app_json}"
    else:
        if key_file is None:
            raise SetupError("give --key-file (with an optional --client-id) or --app-json")
        key = read_bounded(key_file, MAX_KEY_BYTES, "--key-file")
        source = str(key_file)
    if client_id is not None and CLIENT_ID.fullmatch(client_id) is None:
        raise SetupError("the client ID is not a GitHub App client ID (Iv...)")
    if len(key) > MAX_KEY_BYTES or PEM.fullmatch(key) is None:
        raise SetupError(f"{source} is not a PEM private key")
    return client_id, key


def environment_of(github: GitHub, repository: str) -> dict[str, Any] | None:
    document = github.get(f"repos/{repository}/environments?per_page={MAX_LISTED}")
    items = document.get("environments") if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise SetupError(f"gh api repos/{repository}/environments returned no environment list")
    for item in items:
        if isinstance(item, dict) and item.get("name") == ENVIRONMENT:
            return item
    if document.get("total_count", len(items)) != len(items):
        raise SetupError(f"{repository} has more than {MAX_LISTED} environments; check {ENVIRONMENT} by hand")
    return None


def branch_policies(github: GitHub, repository: str) -> list[tuple[str, str]]:
    endpoint = f"repos/{repository}/environments/{ENVIRONMENT}/deployment-branch-policies"
    document = github.get(f"{endpoint}?per_page={MAX_LISTED}")
    items = document.get("branch_policies") if isinstance(document, dict) else None
    if not isinstance(items, list) or not all(isinstance(i, dict) and isinstance(i.get("name"), str) for i in items):
        raise SetupError(f"gh api {endpoint} returned no branch policy list")
    if document.get("total_count", len(items)) != len(items):
        raise SetupError(f"{ENVIRONMENT} has more than {MAX_LISTED} branch policies; remove them by hand")
    return sorted((item["name"], item.get("type", "branch")) for item in items)


def refuse_holds(environment: dict[str, Any]) -> None:
    rules = environment.get("protection_rules") or []
    kinds = sorted({rule.get("type") for rule in rules if isinstance(rule, dict)} - {"branch_policy"})
    if kinds:
        raise SetupError(f"{ENVIRONMENT} has {', '.join(map(str, kinds))}: every status would wait for it; "
                         "remove it in the environment's settings first")


def refuse_other_branches(policies: list[tuple[str, str]], branch: str) -> None:
    others = [f"{name} ({kind})" for name, kind in policies if (name, kind) != (branch, "branch")]
    if others:
        raise SetupError(f"{ENVIRONMENT} also admits {', '.join(others)}; the key may only reach the default "
                         f"branch {branch}: delete those policies first")


def verify_environment(github: GitHub, repository: str, branch: str) -> None:
    """Read the environment back and require exactly the default-branch restriction."""

    environment = environment_of(github, repository)
    if environment is None:
        raise SetupError(f"{ENVIRONMENT} does not exist after it was created")
    refuse_holds(environment)
    if environment.get("deployment_branch_policy") != BRANCH_POLICY:
        raise SetupError(f"{ENVIRONMENT} does not restrict its deployment branches to custom policies")
    policies = branch_policies(github, repository)
    if policies != [(branch, "branch")]:
        raise SetupError(f"{ENVIRONMENT} admits {policies}, not exactly the default branch {branch}")


def app_client_id(github: GitHub) -> str:
    """The gate status App's public client ID, read from GitHub and bound to its App id."""

    app = github.get(f"apps/{APP_SLUG}")
    if not isinstance(app, dict) or app.get("id") != APP_ID:
        raise SetupError(f"apps/{APP_SLUG} is not App {APP_ID}; give --client-id")
    client_id = app.get("client_id")
    if not isinstance(client_id, str) or CLIENT_ID.fullmatch(client_id) is None:
        raise SetupError(f"apps/{APP_SLUG} has no GitHub App client ID; give --client-id")
    return client_id


def setup(github: GitHub, repository: str, branch: str | None, client_id: str | None, key: bytes, *,
          dry_run: bool, out: TextIO) -> None:
    if client_id is None:
        client_id = app_client_id(github)
    default = github.get(f"repos/{repository}").get("default_branch")
    if not isinstance(default, str) or not default:
        raise SetupError(f"{repository} has no default branch")
    if branch is not None and branch != default:
        raise SetupError(f"--branch {branch} is not the default branch {default} of {repository}: the key may "
                         "only reach the default branch")
    for endpoint in (f"repos/{repository}/actions/secrets", f"repos/{repository}/actions/organization-secrets"):
        if PRIVATE_KEY_SECRET in github.names(endpoint, "secrets"):
            scope = "an organization" if endpoint.endswith("organization-secrets") else "a repository"
            raise SetupError(f"{PRIVATE_KEY_SECRET} exists as {scope} secret visible to {repository}; delete it: "
                             f"the key may only live in the {ENVIRONMENT} environment")
    environment = environment_of(github, repository)
    if environment is not None:
        refuse_holds(environment)
        current = environment.get("deployment_branch_policy")
        if isinstance(current, dict) and current.get("custom_branch_policies"):
            policies = branch_policies(github, repository)
            refuse_other_branches(policies, default)
    verb = "would " if dry_run else ""
    print(f"repository: {repository} (default branch {default})", file=out)
    print(f"environment {ENVIRONMENT}: {verb}{'update' if environment else 'create'}, "
          f"deployment branches: {default} only", file=out)
    if dry_run:
        print(f"variable {CLIENT_ID_VARIABLE}: would set to {client_id}", file=out)
        print(f"secret {PRIVATE_KEY_SECRET}: would set from the key file (value not shown)", file=out)
        return
    github.call("api", "-X", "PUT", f"repos/{repository}/environments/{ENVIRONMENT}", "--input", "-",
                stdin=json.dumps({"deployment_branch_policy": BRANCH_POLICY}).encode("utf-8"))
    if (default, "branch") not in branch_policies(github, repository):
        github.call("api", "-X", "POST", f"repos/{repository}/environments/{ENVIRONMENT}/deployment-branch-policies",
                    "-f", f"name={default}", "-f", "type=branch")
    verify_environment(github, repository, default)
    github.call("variable", "set", CLIENT_ID_VARIABLE, "--repo", repository, "--env", ENVIRONMENT,
                "--body", client_id)
    github.call("secret", "set", PRIVATE_KEY_SECRET, "--repo", repository, "--env", ENVIRONMENT, stdin=key)
    variable = github.get(f"repos/{repository}/environments/{ENVIRONMENT}/variables/{CLIENT_ID_VARIABLE}")
    if not isinstance(variable, dict) or variable.get("value") != client_id:
        raise SetupError(f"{CLIENT_ID_VARIABLE} does not read back as {client_id}")
    secret = github.get(f"repos/{repository}/environments/{ENVIRONMENT}/secrets/{PRIVATE_KEY_SECRET}")
    if not isinstance(secret, dict) or secret.get("name") != PRIVATE_KEY_SECRET:
        raise SetupError(f"{PRIVATE_KEY_SECRET} does not exist after it was set")
    print(f"variable {CLIENT_ID_VARIABLE}: {client_id}", file=out)
    print(f"secret {PRIVATE_KEY_SECRET}: set (value not shown)", file=out)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    result.add_argument("--repo", required=True, metavar="OWNER/NAME", help="the mod's repository")
    result.add_argument("--branch", help="the default branch, checked against GitHub (optional)")
    result.add_argument("--key-file", type=Path, metavar="PATH",
                        help="the App's private key (PEM), readable by you alone, or a pipe")
    result.add_argument("--client-id", help=f"the App's client ID (default: read from apps/{APP_SLUG})")
    result.add_argument("--app-json", type=Path, metavar="PATH",
                        help="the App manifest flow's JSON (client_id, pem), readable by you alone")
    result.add_argument("--dry-run", action="store_true", help="read only and print the plan")
    result.add_argument("--gh", default="gh", help="the GitHub CLI to run (default: gh)")
    return result


def main(argv: Sequence[str] | None = None, *, run: Runner | None = None,
         out: TextIO | None = None, err: TextIO | None = None) -> int:
    arguments = parser().parse_args(argv)
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    key = b""
    try:
        if REPOSITORY.fullmatch(arguments.repo) is None:
            raise SetupError(f"--repo {arguments.repo!r} is not OWNER/NAME")
        client_id, key = load_credentials(arguments.app_json, arguments.client_id, arguments.key_file)
        setup(GitHub(run or gh_runner(arguments.gh)), arguments.repo, arguments.branch, client_id, key,
              dry_run=arguments.dry_run, out=out)
    except SetupError as error:
        print(f"new_mod_env: error: {redact(str(error), key)}", file=err)
        return 2
    return 0


def redact(message: str, key: bytes) -> str:
    """``message`` with every line of ``key`` long enough to identify it replaced (defence in depth:
    no message is built from the key, but a ``gh`` diagnostic is quoted as it comes)."""

    for line in key.decode("ascii", "replace").splitlines():
        line = line.strip()
        if len(line) >= MIN_REDACTED_LINE and not line.startswith("-----"):
            message = message.replace(line, "[redacted]")
    return message


if __name__ == "__main__":
    sys.exit(main())
