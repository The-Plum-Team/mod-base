"""The user-site Pillow rule of v1.0.2 under test, shared by the tests of its two copies: the kit's
``mod_base.adapter.host.imaging_user_site`` (which both isolated child environments follow: the
hook child's and the ``conformance`` simulation child's) and the managed bootstrap's
``imaging_user_site`` (which ``run`` follows for the kit process itself).

:class:`Layout` builds, in a temporary directory, a user base whose user site holds a ``PIL``
package and a separate "global" site-packages holding another. :meth:`Layout.patched` then patches
``site.ENABLE_USER_SITE``, ``site.getuserbase``, ``site.getusersitepackages``, ``sys.flags`` and
``importlib.util.find_spec`` (for ``PIL`` only) as one :class:`Scenario` describes; nothing is ever
imported from the fake packages. :data:`REJECTED` lists every scenario that must leave the child
environments unchanged. :func:`user_site_interpreter` finds a real interpreter whose user site is
enabled, for the tests that start one.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import json
import os
import site
import subprocess
import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

PACKAGE = "PIL"
#: Where Pillow is found (``spec``) and which user base the process reports (``base``).
SPECS = ("user", "global", "none", "value-error", "import-error", "namespace", "module", "two-locations",
         "nested", "user-base-root", "foreign-origin")
BASES = ("real", "alias", "relative", "colon", "newline", "delete", "c1", "missing", "file")


@dataclass(frozen=True)
class Scenario:
    """One process state: ``enabled`` is ``site.ENABLE_USER_SITE``, ``no_user_site`` the
    ``sys.flags`` field, ``spec`` and ``base`` name entries of :data:`SPECS` and :data:`BASES`, and
    ``alias`` reports the user site through a symlink to the real one."""

    label: str
    enabled: bool | None = True
    no_user_site: int = 0
    spec: str = "user"
    base: str = "real"
    alias: bool = False


#: The one scenario the rule accepts, and the same through a symlinked user base.
ACCEPTED = (Scenario("pillow in this process's user site"),
            Scenario("a symlinked user base, compared after realpath", base="alias", alias=True))
#: Every scenario whose child environments must stay exactly the pre-v1.0.2 ones.
REJECTED = (
    Scenario("user site disabled by request", enabled=False),
    Scenario("user site disabled for security (uid mismatch)", enabled=None),
    Scenario("started with -s, -I or PYTHONNOUSERSITE", no_user_site=1),
    Scenario("a global or virtual-environment Pillow", spec="global"),
    Scenario("no Pillow at all", spec="none"),
    Scenario("an imported PIL without a spec", spec="value-error"),
    Scenario("a finder that fails", spec="import-error"),
    Scenario("a namespace package named PIL", spec="namespace"),
    Scenario("a single-file PIL module", spec="module"),
    Scenario("a package spread over two directories", spec="two-locations"),
    Scenario("PIL below a subdirectory of the user site", spec="nested"),
    Scenario("PIL directly in the user base", spec="user-base-root"),
    Scenario("an origin outside the package directory", spec="foreign-origin"),
    Scenario("a relative user base", base="relative"),
    Scenario("a user base holding ':'", base="colon"),
    Scenario("a user base holding a newline", base="newline"),
    Scenario("a user base holding DEL", base="delete"),
    Scenario("a user base holding a C1 control", base="c1"),
    Scenario("a user base that does not exist", base="missing"),
    Scenario("a user base that is a file", base="file"),
)


def _flags(no_user_site: int) -> types.SimpleNamespace:
    """A copy of ``sys.flags`` with ``no_user_site`` replaced (``sys.flags`` itself is read-only)."""

    values = {name: getattr(sys.flags, name) for name in dir(sys.flags)
              if not name.startswith("_") and not callable(getattr(sys.flags, name))}
    values["no_user_site"] = no_user_site
    return types.SimpleNamespace(**values)


class Layout:
    """A user base ``<root>/userbase`` with the user site ``lib/site-packages`` holding
    ``PIL/__init__.py``, a symlink ``<root>/alias`` to that user base, a "global"
    ``<root>/global/site-packages/PIL`` and user bases that each fail exactly one path check: the
    relative one names the real user base from the current working directory, so only its being
    relative rejects it."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.base = self.root / "userbase"
        self.user_site = self.base / "lib" / "site-packages"
        self.global_site = self.root / "global" / "site-packages"
        for site_packages in (self.user_site, self.global_site, self.user_site / "nested"):
            (site_packages / PACKAGE).mkdir(parents=True)
            (site_packages / PACKAGE / "__init__.py").write_text("MARKER = 'fake'\n", encoding="utf-8")
        (self.base / PACKAGE).mkdir()
        (self.base / PACKAGE / "__init__.py").write_text("MARKER = 'fake'\n", encoding="utf-8")
        (self.root / "alias").symlink_to(self.base, target_is_directory=True)
        self.bases = {"real": str(self.base), "alias": str(self.root / "alias"), "relative": os.path.relpath(self.base),
                      "missing": str(self.root / "absent")}
        for label, name in (("colon", "user:base"), ("newline", "user\nbase"), ("delete", "user\x7fbase"),
                            ("c1", "user\x85base")):
            (self.root / name).mkdir()
            self.bases[label] = str(self.root / name)
        (self.root / "a-file").write_bytes(b"")
        self.bases["file"] = str(self.root / "a-file")

    def _spec(self, kind: str) -> importlib.machinery.ModuleSpec | None:
        if kind == "none":
            return None
        if kind == "value-error":
            raise ValueError("PIL.__spec__ is None")
        if kind == "import-error":
            raise ImportError("a finder failed")
        package = {"user": self.user_site, "global": self.global_site, "nested": self.user_site / "nested",
                   "user-base-root": self.base}.get(kind, self.user_site) / PACKAGE
        if kind == "module":
            return importlib.machinery.ModuleSpec(PACKAGE, None, origin=str(package.with_suffix(".py")))
        origin = None if kind == "namespace" else str(
            (self.global_site / PACKAGE if kind == "foreign-origin" else package) / "__init__.py")
        spec = importlib.machinery.ModuleSpec(PACKAGE, None, origin=origin, is_package=True)
        spec.submodule_search_locations = [str(package)] + ([str(self.global_site / PACKAGE)]
                                                             if kind == "two-locations" else [])
        return spec

    @contextlib.contextmanager
    def patched(self, scenario: Scenario) -> Iterator[None]:
        """Patch this process into ``scenario`` (``find_spec`` answers for ``PIL`` only)."""

        real_find_spec = importlib.util.find_spec

        def find_spec(name: str, package: str | None = None) -> importlib.machinery.ModuleSpec | None:
            return self._spec(scenario.spec) if name == PACKAGE else real_find_spec(name, package)

        user_site = (self.root / "alias" / "lib" / "site-packages") if scenario.alias else self.user_site
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(site, "ENABLE_USER_SITE", scenario.enabled))
            stack.enter_context(mock.patch.object(site, "getuserbase", return_value=self.bases[scenario.base]))
            stack.enter_context(mock.patch.object(site, "getusersitepackages", return_value=str(user_site)))
            stack.enter_context(mock.patch.object(sys, "flags", _flags(scenario.no_user_site)))
            stack.enter_context(mock.patch.object(importlib.util, "find_spec", find_spec))
            yield

    def expected(self, scenario: Scenario) -> dict[str, str]:
        """What the rule must return in ``scenario``."""

        return {"PYTHONUSERBASE": self.bases[scenario.base]} if scenario in ACCEPTED else {}


def user_site_interpreter(home: Path) -> str:
    """A Python 3.11+ interpreter whose user site is enabled in a clean environment: this one, or
    the base interpreter of the virtual environment this one runs in (a virtual environment
    without system site packages disables the user site, as the kit's own test environment does).

    Raises ``AssertionError`` naming both candidates when neither has a user site, so a test that
    needs one fails instead of skipping."""

    candidates = list(dict.fromkeys(path for path in (sys.executable, getattr(sys, "_base_executable", None))
                                    if path))
    probe = ("import json, site, sys; "
             "print(json.dumps(site.ENABLE_USER_SITE is True and sys.version_info >= (3, 11)))")
    for candidate in candidates:
        completed = subprocess.run([candidate, "-P", "-c", probe], env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
                                   capture_output=True, text=True, timeout=60, check=False)
        if completed.returncode == 0 and json.loads(completed.stdout) is True:
            return candidate
    raise AssertionError(f"no Python 3.11+ interpreter with an enabled user site among {candidates}")


def user_site_of(interpreter: str, base: Path, home: Path) -> Path:
    """The user site ``interpreter`` derives from ``PYTHONUSERBASE=base`` (its layout differs by
    platform and build: ``lib/python3.X/site-packages``, or ``lib/python/site-packages`` for a macOS
    framework build)."""

    completed = subprocess.run([interpreter, "-P", "-c", "import site; print(site.getusersitepackages())"],
                               env={"PATH": "/usr/bin:/bin", "HOME": str(home), "PYTHONUSERBASE": str(base)},
                               capture_output=True, text=True, timeout=60, check=True)
    return Path(completed.stdout.strip())


def fake_pillow(site_packages: Path, marker: str) -> Path:
    """A tiny ``PIL`` package in ``site_packages`` whose ``MARKER`` is ``marker``; returns its
    ``__init__.py``."""

    package = site_packages / PACKAGE
    package.mkdir(parents=True)
    init = package / "__init__.py"
    init.write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
    return init
