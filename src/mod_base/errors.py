"""Kit exception hierarchy and the single process-exit mapping used by every entry point.

Exit codes are part of the workflow contract (SPEC §2.2):

* 0: success;
* 1: an unexpected internal error (a kit bug), reported as one line and never as success;
* 2: :class:`MbError`, a fail-closed rejection of hostile or inconsistent input;
* 3: :class:`Unavailable` / :class:`Superseded`, "no admissible evidence" (callers may treat it
  as a clean, reported absence);
* 78: :class:`ControllerSkew`, the executing kit does not belong to the checked-out pin.

Every error is reported as exactly one bounded, control-character-free line on stderr so hostile
input can never inject workflow commands or forge log lines.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TextIO

MAX_MESSAGE_CHARS = 1000
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_REJECTED = 2
EXIT_UNAVAILABLE = 3
EXIT_CONTROLLER_SKEW = 78
EXIT_INTERRUPTED = 130


class MbError(Exception):
    """A fail-closed rejection. ``reason`` is a short machine-readable token for outputs."""

    exit_code = EXIT_REJECTED
    default_reason = "rejected"

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason if reason is not None else self.default_reason


class Unavailable(MbError):
    """No admissible evidence exists (for example no authenticated artifact); exit 3."""

    exit_code = EXIT_UNAVAILABLE
    default_reason = "unavailable"


class Superseded(Unavailable):
    """The evidence is valid but bound to a superseded contract (family drift); exit 3."""

    default_reason = "superseded"


class ControllerSkew(MbError):
    """The executing kit tree does not match the pin in the checked-out mod; exit 78."""

    exit_code = EXIT_CONTROLLER_SKEW
    default_reason = "controller-skew"


def single_line(message: object, *, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Return ``message`` as one printable line of at most ``limit`` characters.

    Every character below U+0020, DEL and the C1 range is replaced by a space, so a hostile value
    embedded in an error can never start a new log line or a ``::workflow-command::``.
    """

    text = "".join(
        " " if (ord(char) < 32 or 127 <= ord(char) < 160 or char in "  ") else char
        for char in str(message)
    ).strip()
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text or "(no message)"


def exit_code_for(error: BaseException) -> int:
    """Map an exception raised by an entry point to its process exit code."""

    if isinstance(error, MbError):
        return error.exit_code
    if isinstance(error, NotImplementedError):
        return EXIT_REJECTED
    if isinstance(error, KeyboardInterrupt):
        return EXIT_INTERRUPTED
    return EXIT_INTERNAL


def describe(error: BaseException) -> str:
    """Return the one-line stderr description of ``error`` (without the program prefix)."""

    if isinstance(error, MbError):
        return f"{error.reason}: {error}"
    if isinstance(error, NotImplementedError):
        return f"not implemented: {error}"
    if isinstance(error, KeyboardInterrupt):
        return "interrupted"
    return f"internal error: {type(error).__name__}: {error}"


def run_main(
    entry: Callable[[], int | None], *, program: str = "mod_base", stderr: TextIO | None = None
) -> int:
    """Run ``entry`` and convert every outcome into an exit code plus at most one stderr line.

    ``SystemExit`` raised inside ``entry`` (for example argparse ``--help``) keeps its integer
    code; a non-integer ``SystemExit`` payload is reported as a rejection.
    """

    stream = stderr if stderr is not None else sys.stderr
    try:
        result = entry()
    except SystemExit as exc:
        if exc.code is None:
            return EXIT_OK
        if isinstance(exc.code, int) and not isinstance(exc.code, bool):
            return exc.code
        print(f"{program}: rejected: {single_line(exc.code)}", file=stream)
        return EXIT_REJECTED
    except BaseException as exc:  # noqa: BLE001 - the one place every failure becomes an exit code
        print(f"{program}: {single_line(describe(exc))}", file=stream)
        return exit_code_for(exc)
    if result is None:
        return EXIT_OK
    if isinstance(result, bool) or not isinstance(result, int):
        print(f"{program}: internal error: entry point returned {type(result).__name__}", file=stream)
        return EXIT_INTERNAL
    return result
