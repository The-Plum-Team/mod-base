"""``python3 -P -m mod_base <command>``: the static command registry (SPEC §2.2, §10).

The registry maps every command name to the module that registers it. Only the module owning the
requested command is imported, so a command never loads code (or Pillow) it does not need. A group
whose module is missing, fails to import or does not register the command fails with one
:class:`MbError` line (exit 2), never an import traceback.

Each command module exposes ``register(subparsers)``, which adds its commands with
``set_defaults(handler=<callable(args) -> int>)``. The flags are the frozen workflow contract; the
shared argument types below validate grammar at parse time so no entry point ever sees a malformed
key, SHA, digest or id.
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

import mod_base
from mod_base.errors import MbError, run_main, single_line
from mod_base.model import grammar

#: Command name -> module whose ``register(subparsers)`` adds it (SPEC §10 "Command registry").
COMMANDS: dict[str, str] = {
    "download": "mod_base.github.commands",
    "budget": "mod_base.github.commands",
    "expect": "mod_base.evidence.commands",
    "prepare": "mod_base.evidence.commands",
    "validate": "mod_base.evidence.commands",
    "compact": "mod_base.evidence.commands",
    "compose": "mod_base.evidence.commands",
    "anchor": "mod_base.evidence.commands",
    "family": "mod_base.family.commands",
    "admit": "mod_base.pages.commands_control",
    "select": "mod_base.pages.commands_control",
    "authenticate": "mod_base.pages.commands_control",
    "build": "mod_base.pages.commands_build",
    "refresh": "mod_base.pages.commands_build",
    "rotate": "mod_base.pages.commands_rotate",
    "template": "mod_base.template.commands",
    "pin": "mod_base.pin_commands",
    "digest": "mod_base.pin_commands",
    "conformance": "mod_base.conformance.commands",
    "ci": "mod_base.build_ci.commands",
}

OUTPUT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_OUTPUT_VALUE_CHARS = 65_536


class KitArgumentParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` whose usage errors raise :class:`MbError` (one line, exit 2)."""

    def error(self, message: str) -> NoReturn:
        raise MbError(f"{self.prog}: {message}", reason="usage")


# -- Argument types (argparse ``type=`` callables) ------------------------------------------------


def typed(check: Callable[[str], Any], description: str) -> Callable[[str], Any]:
    """Wrap a grammar check as an argparse ``type=`` whose failure is a one-line usage error."""

    def convert(value: str) -> Any:
        try:
            return check(value)
        except (MbError, ValueError) as exc:
            raise argparse.ArgumentTypeError(f"invalid {description}: {single_line(value, limit=80)}") from exc

    convert.__name__ = description
    return convert


def _key(value: str) -> str:
    return grammar.require_key(value)


def _family(value: str) -> str:
    return grammar.require_family(value)


def _sha1(value: str) -> str:
    return grammar.require_sha1(value)


def _digest(value: str) -> str:
    return grammar.require(grammar.DIGEST, value, "digest")


def _branch(value: str) -> str:
    return grammar.require(grammar.BRANCH, value, "branch")


def _positive(value: str) -> int:
    if not grammar.POSITIVE_DECIMAL.fullmatch(value):
        raise ValueError(value)
    return grammar.require_positive_int(int(value), "integer")


def _keys(value: str) -> tuple[str, ...]:
    keys = tuple(value.split(","))
    if not keys or len(set(keys)) != len(keys):
        raise ValueError(value)
    return tuple(grammar.require_key(key) for key in keys)


def _nonnegative_seconds(value: str) -> float:
    number = float(value)
    if not 0.0 <= number <= 60.0:
        raise ValueError(value)
    return number


def _path(value: str) -> Path:
    if not value or "\x00" in value:
        raise ValueError(value)
    return Path(value)


KEY = typed(_key, "key")
FAMILY = typed(_family, "family")
SHA1 = typed(_sha1, "commit SHA")
DIGEST = typed(_digest, "sha256 digest")
BRANCH = typed(_branch, "branch")
POSITIVE = typed(_positive, "positive integer")
KEYS = typed(_keys, "comma-separated keys")
DELAY = typed(_nonnegative_seconds, "delay in seconds")
PATH = typed(_path, "path")


def add_repo_config(parser: argparse.ArgumentParser) -> None:
    """Add ``--repo DIR`` (required) and ``--config FILE`` (default ``<repo>/site/mod-base.json``;
    an explicit path is relative to the working directory, like ``--repo``)."""

    parser.add_argument("--repo", type=PATH, required=True, metavar="DIR", help="the mod checkout")
    parser.add_argument("--config", type=PATH, default=None, metavar="FILE", help="default: <repo>/site/mod-base.json")


# -- Outputs --------------------------------------------------------------------------------------


def write_github_output(path: Path | None, values: Mapping[str, str | int | bool]) -> None:
    """Append ``name=value`` lines to a ``$GITHUB_OUTPUT`` file.

    Names match ``[a-z][a-z0-9_]*``; values are single-line text (booleans become ``true``/
    ``false``), so no hostile value can inject another output or a multi-line delimiter. A
    ``None`` path is a no-op (local runs).
    """

    if path is None:
        return
    lines: list[str] = []
    for name, value in values.items():
        if not OUTPUT_NAME.fullmatch(name):
            raise MbError(f"invalid output name {name!r}")
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, int):
            text = str(value)
        elif isinstance(value, str):
            text = value
        else:
            raise MbError(f"output {name} must be text, an integer or a boolean")
        if len(text) > MAX_OUTPUT_VALUE_CHARS or any(ord(character) < 32 or ord(character) == 127
                                                     for character in text):
            raise MbError(f"output {name} must be one bounded line without control characters")
        lines.append(f"{name}={text}\n")
    with open(path, "a", encoding="utf-8") as stream:
        stream.writelines(lines)


# -- Registry -------------------------------------------------------------------------------------


def load_group(command: str) -> ModuleType:
    """Import the module owning ``command`` or raise one clean :class:`MbError`."""

    module_name = COMMANDS.get(command)
    if module_name is None:
        raise MbError(f"unknown command {single_line(command, limit=60)!r}; run with --help", reason="usage")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise MbError(f"command {command!r} is unavailable: {module_name} cannot be imported "
                      f"({type(exc).__name__}: {single_line(exc, limit=200)})", reason="unavailable-command") from None
    if not callable(getattr(module, "register", None)):
        raise MbError(f"command {command!r} is unavailable: {module_name} defines no register()",
                      reason="unavailable-command")
    return module


def build_parser(command: str) -> KitArgumentParser:
    """A parser holding only ``command``'s group, registered by its owning module."""

    parser = KitArgumentParser(prog="mod_base", description="mod-base kit")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    load_group(command).register(subparsers)
    if command not in subparsers.choices:
        raise MbError(f"command {command!r} is unavailable: {COMMANDS[command]} does not register it",
                      reason="unavailable-command")
    return parser


def _usage() -> str:
    names = ", ".join(sorted(COMMANDS))
    return f"usage: python3 -P -m mod_base COMMAND [flags]\ncommands: {names}\n"


def dispatch(argv: Sequence[str]) -> int:
    arguments = list(argv)
    if not arguments:
        raise MbError("a command is required; run with --help", reason="usage")
    if arguments[0] in {"-h", "--help"}:
        sys.stdout.write(_usage())
        return 0
    if arguments[0] == "--version":
        sys.stdout.write(f"mod-base {mod_base.__version__}\n")
        return 0
    parser = build_parser(arguments[0])
    namespace = parser.parse_args(arguments)
    handler = getattr(namespace, "handler", None)
    if not callable(handler):
        raise MbError(f"command {arguments[0]!r} registered no handler", reason="unavailable-command")
    result = handler(namespace)
    return 0 if result is None else result


def main(argv: Sequence[str] | None = None) -> int:
    """Process entry point: every outcome becomes an exit code plus at most one stderr line."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    return run_main(lambda: dispatch(arguments))


def environ() -> Mapping[str, str]:
    """The process environment (a seam for handlers; entry points use ``Invocation.environ``)."""

    return os.environ
