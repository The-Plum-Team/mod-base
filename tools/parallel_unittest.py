#!/usr/bin/env python3
"""Run the kit's unittest discovery across worker processes, failing closed wherever a serial run would.

Copied from Block Pops ``scripts/ci/parallel_unittest.py``; only this docstring differs. It is run
from the kit root as ``PYTHONPATH=src python3 tools/parallel_unittest.py -v -t . tests``: the
``mod_base`` package is importable through ``PYTHONPATH`` (which every spawned worker inherits) and
the ``tests`` package through ``-t``.

A class with class-level fixtures (``setUpClass``/``tearDownClass``) runs whole inside one worker,
so those fixtures keep their meaning; any other class gives each test a fresh instance, so its
tests are scheduled one by one. A worker runs many units in turn, as a serial run does. The
parent discovers every start directory once and records how many tests each unit contributes (a
class a second module re-exports is discovered, and therefore run, twice). The run fails unless
discovery imported every module, every unit reports exactly its discovered number of tests,
every result is successful, no worker died, and at least one test ran.

Each worker gets its own temporary directory, created before any worker starts. Several checks
stamp every parent directory of a file they read and fail if one changes; with one shared
TMPDIR, another worker creating a directory next to theirs would trip them.
"""

from __future__ import annotations

import argparse
import io
import multiprocessing
import os
import shutil
import sys
import tempfile
import time
import unittest
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SEPARATOR = "-" * 70


@dataclass(frozen=True)
class Unit:
    name: str
    start: str
    tests: int
    repeat: int
    weight: tuple[int, int]


def _tests(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _tests(item)
        else:
            yield item


def _has_class_fixture(case: type) -> bool:
    return (
        case.setUpClass.__func__ is not unittest.TestCase.setUpClass.__func__
        or case.tearDownClass.__func__ is not unittest.TestCase.tearDownClass.__func__
    )


def discover(starts: list[str], *, pattern: str, top_level: Path) -> tuple[list[Unit], list[str]]:
    """Return the scheduling units (whole fixture classes, otherwise single tests) and errors."""

    loader = unittest.TestLoader()
    units: list[Unit] = []
    errors: list[str] = []
    for start in starts:
        suite = loader.discover(
            str(Path(start).resolve()), pattern=pattern, top_level_dir=str(top_level)
        )
        errors.extend(loader.errors)
        loader.errors = []
        tests = list(_tests(suite))
        occurrences: Counter[type] = Counter(type(test) for test in tests)
        for case, count in occurrences.items():
            if case.__module__ == "unittest.loader":
                errors.append(f"{start}: discovery could not load {case.__qualname__}")
                continue
            name = f"{case.__module__}.{case.__qualname__}"
            per_run = loader.loadTestsFromName(name).countTestCases()
            if per_run <= 0 or count % per_run:
                errors.append(f"{name}: discovered {count} tests, loads {per_run} per run")
                continue
            module = sys.modules[case.__module__]
            size = Path(module.__file__).stat().st_size if getattr(module, "__file__", None) else 0
            fixture = _has_class_fixture(case)
            if fixture:
                units.append(Unit(name, start, count, count // per_run, (True, size)))
                continue
            for test_id, repeat in Counter(test.id() for test in tests if type(test) is case).items():
                units.append(Unit(test_id, start, repeat, repeat, (False, size)))
    return units, errors


def _start_worker(directories: Any, top_level: str) -> None:
    directory = directories.get()
    os.environ["TMPDIR"] = directory
    tempfile.tempdir = directory
    if top_level not in sys.path:
        sys.path.insert(0, top_level)


def _run_unit(name: str, repeat: int, verbosity: int) -> dict[str, Any]:
    stream = io.StringIO()
    result = unittest.TextTestResult(
        unittest.runner._WritelnDecorator(stream), descriptions=True, verbosity=verbosity
    )
    started = time.perf_counter()
    result.startTestRun()
    with redirect_stdout(stream), redirect_stderr(stream):
        for _ in range(repeat):
            unittest.TestLoader().loadTestsFromName(name).run(result)
    result.stopTestRun()
    result.printErrors()
    return {
        "name": name,
        "tests_run": result.testsRun,
        "successful": result.wasSuccessful(),
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "seconds": time.perf_counter() - started,
        "output": stream.getvalue(),
    }


def _default_jobs() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:
        return max(1, os.cpu_count() or 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("starts", nargs="+", metavar="START_DIRECTORY")
    parser.add_argument("-p", "--pattern", default="test_*.py")
    parser.add_argument("-t", "--top-level-directory", type=Path, default=Path("."))
    parser.add_argument("-j", "--jobs", type=int, default=_default_jobs())
    parser.add_argument("-v", "--verbose", action="store_const", const=2, default=1)
    parser.add_argument("--slowest", type=int, default=10, help="units listed in the timing table")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    top_level = args.top_level_directory.resolve()
    sys.path.insert(0, str(top_level))

    started = time.perf_counter()
    units, errors = discover(args.starts, pattern=args.pattern, top_level=top_level)
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    expected = sum(unit.tests for unit in units)
    if errors or expected == 0:
        print(f"discovery failed closed: {len(errors)} errors, {expected} tests", file=sys.stderr)
        return 1

    # Fixture classes, then bigger modules, go first so the slow ones do not start last. The
    # order only affects wall time; every unit runs.
    units.sort(key=lambda unit: unit.weight, reverse=True)
    problems: list[str] = []
    totals: Counter[str] = Counter()
    per_start: Counter[str] = Counter()
    timings: list[tuple[float, str]] = []
    context = multiprocessing.get_context("spawn")
    scratch = Path(tempfile.mkdtemp(prefix="parallel-unittest-"))
    directories = context.Queue()
    for index in range(args.jobs):
        (scratch / f"worker-{index}").mkdir()
        directories.put(str(scratch / f"worker-{index}"))
    with ProcessPoolExecutor(
        max_workers=args.jobs,
        mp_context=context,
        initializer=_start_worker,
        initargs=(directories, str(top_level)),
    ) as pool:
        futures = {
            pool.submit(_run_unit, unit.name, unit.repeat, args.verbose): unit for unit in units
        }
        for future in as_completed(futures):
            unit = futures[future]
            try:
                outcome = future.result()
            except Exception as exc:  # a dead worker is a failed run, never a skipped class
                problems.append(f"{unit.name}: worker failed: {exc!r}")
                continue
            sys.stdout.write(outcome["output"])
            sys.stdout.flush()
            if outcome["tests_run"] != unit.tests:
                problems.append(
                    f"{unit.name}: ran {outcome['tests_run']} tests, discovered {unit.tests}"
                )
            if not outcome["successful"]:
                problems.append(f"{unit.name}: unsuccessful")
            for key in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"):
                totals[key] += outcome[key]
            totals["run"] += outcome["tests_run"]
            per_start[unit.start] += outcome["tests_run"]
            timings.append((outcome["seconds"], unit.name))
    shutil.rmtree(scratch, ignore_errors=True)
    elapsed = time.perf_counter() - started

    if totals["run"] != expected:
        problems.append(f"ran {totals['run']} tests, discovered {expected}")
    print(SEPARATOR)
    for start in args.starts:
        print(f"{start}: {per_start[start]} tests")
    print(f"{args.jobs} workers, {len(units)} scheduling units; slowest:")
    for seconds, name in sorted(timings, reverse=True)[: max(0, args.slowest)]:
        print(f"  {seconds:8.1f}s  {name}")
    print(SEPARATOR)
    print(f"Ran {totals['run']} test{'s' if totals['run'] != 1 else ''} in {elapsed:.3f}s")
    print()
    details = [
        f"{label}={totals[key]}"
        for key, label in (
            ("failures", "failures"),
            ("errors", "errors"),
            ("skipped", "skipped"),
            ("expected_failures", "expected failures"),
            ("unexpected_successes", "unexpected successes"),
        )
        if totals[key]
    ]
    suffix = f" ({', '.join(details)})" if details else ""
    if problems:
        for problem in problems:
            print(f"FAILED: {problem}", file=sys.stderr)
        print(f"FAILED{suffix}")
        return 1
    print(f"OK{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
