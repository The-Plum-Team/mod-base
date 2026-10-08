"""Bounded policy diagnostics and native unittest count parity (MB11).

Execute discovery/tests only in a disposable credentialless worker. These in-process counts
cannot authenticate candidate reports, protected source provenance, native policy or statuses.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from mod_base.errors import MbError
from mod_base.model import limits


POLICY_PROFILES = ("block-pops", "quick-skin")


class PolicyError(MbError):
    """A policy unit cannot establish native discovery/count/success semantics."""

    default_reason = "ci-policy"


@dataclass(frozen=True)
class PolicyCounts:
    tests_run: int
    failures: int
    errors: int
    skipped: int
    class_skips: int
    expected_failures: int
    unexpected_successes: int
    successful: bool


def admit_policy_unit(*, profile: str, discovered: int, repeat: int,
                       fixture: bool, counts: PolicyCounts) -> int:
    """Return tests skipped with an admitted QS class; all other counts must match exactly.

    Retain protected scheduling/discovery metadata; caller must still require a nonzero total
    tests_run and complete discovery/worker results. Matching constructed counts are not proof.
    """

    if (type(profile) is not str or profile not in POLICY_PROFILES
            or type(discovered) is not int or not 1 <= discovered <= limits.MAX_CI_POLICY_TESTS
            or type(repeat) is not int or not 1 <= repeat <= discovered or discovered % repeat
            or type(fixture) is not bool or type(counts) is not PolicyCounts):
        raise PolicyError("invalid protected policy scheduling/count metadata")
    for name in ("tests_run", "failures", "errors", "skipped", "class_skips",
                 "expected_failures", "unexpected_successes"):
        value = getattr(counts, name)
        if type(value) is not int or not 0 <= value <= limits.MAX_CI_POLICY_TESTS:
            raise PolicyError("policy outcome count is malformed or exceeds its cap")
    if (type(counts.successful) is not bool or counts.class_skips > counts.skipped
            or counts.class_skips > repeat):
        raise PolicyError("policy outcome class-skip/success metadata is malformed")
    if not counts.successful or counts.failures or counts.errors or counts.unexpected_successes:
        raise PolicyError("unsuccessful")
    skipped_class = (profile == "quick-skin" and fixture and counts.tests_run == 0
                     and counts.class_skips == repeat and counts.skipped == repeat
                     and counts.expected_failures == 0)
    if skipped_class:
        return discovered
    if counts.tests_run != discovered:
        raise PolicyError(f"ran {counts.tests_run} tests, discovered {discovered}")
    return 0


class BoundedPolicyStream(io.TextIOBase):
    """UTF-8 diagnostic prefix bounded independently of worker stdout pipe draining."""

    def __init__(self, max_bytes: int = limits.MAX_CI_LOG_BYTES) -> None:
        super().__init__()
        if type(max_bytes) is not int or not 1 <= max_bytes <= limits.MAX_CI_LOG_BYTES:
            raise PolicyError("policy diagnostic cap is invalid")
        self._max_bytes = max_bytes
        self._buffer = bytearray()
        self._truncated = False

    @property
    def truncated(self) -> bool:
        return self._truncated

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        if self.closed or type(value) is not str:
            raise PolicyError("policy diagnostic stream is closed or received nontext")
        if self._truncated:
            return len(value)
        for offset in range(0, len(value), 4096):
            try:
                chunk = value[offset:offset + 4096].encode("utf-8")
            except UnicodeError as error:
                raise PolicyError("policy diagnostic text is not UTF-8") from error
            available = self._max_bytes - len(self._buffer)
            if len(chunk) > available:
                self._buffer.extend(chunk[:available].decode("utf-8", errors="ignore").encode("utf-8"))
                self._truncated = True
                break
            self._buffer.extend(chunk)
        return len(value)

    def flush(self) -> None:
        pass

    def getvalue(self) -> str:
        return self._buffer.decode("utf-8")
