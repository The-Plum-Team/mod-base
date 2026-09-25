"""Every module and signature frozen in docs/INTERNAL-API.md exists with the same parameters.

The check is one-directional (document -> code): units may add private helpers and modules, but
nothing documented may disappear or change its parameter names, order or kinds.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
import unittest
from pathlib import Path

import mod_base

DOC = Path(__file__).resolve().parents[1] / "docs" / "INTERNAL-API.md"
SECTION = re.compile(r"^## `(mod_base[\w.]*)`$")
FUNCTION = re.compile(r"^\* `def (\w+)(\(.*)`(?::|$)")
CLASS = re.compile(r"^\* `class (\w+)")
METHOD = re.compile(r"^  \* `(?:@classmethod )?(\w+)(\(.*?\)(?: -> [^`]*)?)`")


def split_parameters(signature: str) -> list[str]:
    """Top-level parameter texts of ``(a: int, *, b: dict[str, int] = {}) -> x``."""

    depth, current, parts = 0, "", []
    for character in signature[1:]:
        if character in "([{":
            depth += 1
        elif character in ")]}":
            if depth == 0:
                break
            depth -= 1
        if character == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += character
    if current.strip():
        parts.append(current.strip())
    return parts


def parameter_names(signature: str) -> list[str]:
    names = []
    for part in split_parameters(signature):
        names.append(re.split(r"[:=]", part, maxsplit=1)[0].strip())
    return names


def actual_names(function) -> list[str]:
    names = []
    keyword_marker = False
    for parameter in inspect.signature(function).parameters.values():
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY and not keyword_marker:
            names.append("*")
            keyword_marker = True
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            keyword_marker = True
            names.append("*" + parameter.name)
        elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
            names.append("**" + parameter.name)
        else:
            names.append(parameter.name)
    return names


def documented() -> dict[str, list[tuple[str, str, str | None]]]:
    sections: dict[str, list[tuple[str, str, str | None]]] = {}
    current: str | None = None
    owner: str | None = None
    for line in DOC.read_text(encoding="utf-8").splitlines():
        heading = SECTION.match(line)
        if heading:
            current = heading.group(1)
            sections[current] = []
            continue
        if current is None:
            continue
        function = FUNCTION.match(line)
        if function:
            sections[current].append(("def", function.group(1), function.group(2)))
            owner = None
            continue
        klass = CLASS.match(line)
        if klass:
            owner = klass.group(1)
            sections[current].append(("class", owner, None))
            continue
        method = METHOD.match(line)
        if method and owner is not None:
            sections[current].append(("method", f"{owner}.{method.group(1)}", method.group(2)))
    return sections


class InternalApiTest(unittest.TestCase):
    def test_every_kit_module_imports(self) -> None:
        names = [info.name for info in pkgutil.walk_packages(mod_base.__path__, "mod_base.")
                 if not info.name.endswith("__main__")]
        self.assertGreater(len(names), 60)
        for name in names:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_documented_modules_and_signatures_exist(self) -> None:
        sections = documented()
        self.assertGreater(len(sections), 50)
        for module_name, entries in sections.items():
            module = importlib.import_module(module_name)
            for kind, name, signature in entries:
                with self.subTest(module=module_name, name=name):
                    target = module
                    for part in name.split("."):
                        target = inspect.getattr_static(target, part) if inspect.isclass(target) else getattr(target, part)
                    if kind == "class":
                        self.assertTrue(inspect.isclass(target))
                        continue
                    if isinstance(target, property):
                        continue
                    if isinstance(target, (classmethod, staticmethod)):
                        target = target.__func__
                    self.assertTrue(callable(target))
                    assert signature is not None
                    self.assertEqual(parameter_names(signature), actual_names(target))

    def test_every_command_module_is_documented(self) -> None:
        from mod_base.cli import COMMANDS

        sections = documented()
        for module_name in set(COMMANDS.values()):
            self.assertIn(module_name, sections)
            self.assertIn(("def", "register", "(subparsers: argparse._SubParsersAction) -> None"),
                          sections[module_name])


if __name__ == "__main__":
    unittest.main()
