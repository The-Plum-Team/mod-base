"""docs/INTERNAL-API.md and the code agree, in both directions.

Document -> code: every module and name frozen in the document exists, callables with the same
parameter names, order and kinds. Units may add private helpers and modules, but nothing documented
may disappear or change its parameters.

Code -> document: every name a kit module (or kit tool) imports from, or reads as an attribute of,
a module owned by another unit (the document's ownership table) is listed under that module's
section, and no private module or name crosses a unit boundary. So a name one unit shares with
another is frozen before anyone depends on it.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import importlib
import inspect
import pkgutil
import re
import unittest
from collections.abc import Iterable, Mapping
from pathlib import Path

import mod_base
from mod_base.model import limits

KIT = Path(__file__).resolve().parents[1]
DOC = KIT / "docs" / "INTERNAL-API.md"
SOURCE = KIT / "src"
TOOLS = KIT / "tools"
SECTION = re.compile(r"^## `(mod_base[\w.]*)`$")
FUNCTION = re.compile(r"^\* `def (\w+)(\(.*)`(?::|$)")
CLASS = re.compile(r"^\* `class (\w+)")
METHOD = re.compile(r"^  \* `(?:@classmethod )?(\w+)(\(.*?\)(?: -> [^`]*)?)`")
#: ``  * `name` (property) -> `T```: a property of the class above.
PROPERTY = re.compile(r"^  \* `(\w+)` \(property\)(?: -> `[^`]*`)?$")
#: ``  * fields: `a: T, b: U = v```: the dataclass fields of the class above, in order.
FIELDS = re.compile(r"^  \* fields: `(.*)`$")
#: ``* `NAME = value``` or ``* `NAME`: description``: one module-level name (and its value).
NAME = re.compile(r"^\* `(\w+)(?: = ([^`]*))?`(?::|$)")
#: ``* `A`, `B`, `C``` or ``* `A`, `B`: description``: several module-level names.
NAMES = re.compile(r"^\* ((?:`\w+`, )+`\w+`)(?::.*)?$")
#: Every entry line of a module section (:func:`documented` parses each one or reports it).
ENTRY = re.compile(r"^(?:  )?\* ")
#: The entry kinds that name module-level objects (the others belong to the class above them).
MODULE_KINDS = frozenset({"def", "class", "name"})
OWNERSHIP_ROW = re.compile(r"^\| (MB\d+) \| (.+) \|$")
#: Attributes Python itself gives every module; never part of a unit's interface.
MODULE_DUNDERS = frozenset({"__builtins__", "__cached__", "__dict__", "__doc__", "__file__", "__loader__",
                            "__name__", "__package__", "__path__", "__spec__"})
#: The pseudo-unit of the kit's tools (``tools/*.py``): every ``mod_base`` name they use is foreign.
TOOLS_UNIT = "tools"


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


def field_defaults(detail: str) -> list[str | None]:
    """The default text of each documented dataclass field (``a: T = 1`` -> ``"1"``), else ``None``."""

    return [part.split(" = ", 1)[1] if " = " in part else None for part in split_parameters(f"({detail})")]


def rendered_default(field: dataclasses.Field) -> str | None:  # type: ignore[type-arg]
    """How the document renders ``field``'s default: ``<factory>``, its ``repr`` or ``None`` (none)."""

    if field.default_factory is not dataclasses.MISSING:
        return "<factory>"
    return None if field.default is dataclasses.MISSING else repr(field.default)


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


def documented(text: str | None = None) -> dict[str, list[tuple[str, str, str | None]]]:
    """``{module: [(kind, name, detail)]}`` of every entry of every module section.

    ``kind`` is ``def`` or ``method`` (``detail``: the signature), ``class``, ``property``
    (``Class.name``), ``fields`` (``Class``; ``detail``: the dataclass fields), ``name`` (``detail``:
    the documented value, if any) or ``unparsed`` (an entry line no pattern reads; ``detail`` is
    ``None``), so that no documented entry can silently escape the checks."""

    sections: dict[str, list[tuple[str, str, str | None]]] = {}
    current: str | None = None
    owner: str | None = None
    for line in (DOC.read_text(encoding="utf-8") if text is None else text).splitlines():
        heading = SECTION.match(line)
        if heading:
            current = heading.group(1)
            sections[current] = []
            owner = None
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
        member = METHOD.match(line), PROPERTY.match(line), FIELDS.match(line)
        if owner is not None and any(member):
            method, prop, fields = member
            if prop:
                sections[current].append(("property", f"{owner}.{prop.group(1)}", None))
            elif fields:
                sections[current].append(("fields", owner, fields.group(1)))
            else:
                sections[current].append(("method", f"{owner}.{method.group(1)}", method.group(2)))
            continue
        name = NAME.match(line)
        if name:
            sections[current].append(("name", name.group(1), name.group(2)))
            owner = None
            continue
        names = NAMES.match(line)
        if names:
            sections[current].extend(("name", item, None) for item in re.findall(r"`(\w+)`", names.group(1)))
            owner = None
            continue
        if ENTRY.match(line):
            sections[current].append(("unparsed", line, None))
    return sections


def documented_value(text: str) -> tuple[bool, object]:
    """``(True, value)`` for a documented value that is a complete Python literal
    (``frozenset({...})`` included); ``(False, None)`` for one the document abbreviates (``...``)."""

    if text.endswith("..."):
        return False, None
    if text.startswith("frozenset(") and text.endswith(")"):
        inner = text[len("frozenset("):-1]
        return True, frozenset(ast.literal_eval(inner)) if inner else frozenset()
    return True, ast.literal_eval(text)


def documented_names(sections: Mapping[str, list[tuple[str, str, str | None]]]) -> dict[str, set[str]]:
    """The module-level names each section lists (methods excluded)."""

    return {module: {name for kind, name, _ in entries if kind in MODULE_KINDS} for module, entries in sections.items()}


def ownership(text: str | None = None) -> dict[str, str]:
    """``{module: unit}`` from the document's ownership table."""

    owners: dict[str, str] = {}
    for line in (DOC.read_text(encoding="utf-8") if text is None else text).splitlines():
        row = OWNERSHIP_ROW.match(line)
        if row:
            for module in re.findall(r"`(mod_base[\w.]*)`", row.group(2)):
                owners[module] = row.group(1)
    return owners


def is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def owner_of(module: str, owners: Mapping[str, str]) -> str | None:
    """The unit owning ``module``: listed, or (for a private module) the unit owning its package."""

    if module in owners:
        return owners[module]
    package, _, leaf = module.rpartition(".")
    if is_private(leaf):
        units = {unit for listed, unit in owners.items() if listed.rpartition(".")[0] == package}
        if len(units) == 1:
            return units.pop()
    return None


def kit_modules() -> dict[str, Path]:
    """``{module name: file}`` of every module and package under ``src/mod_base``."""

    modules: dict[str, Path] = {}
    for path in sorted((SOURCE / "mod_base").rglob("*.py")):
        parts = list(path.relative_to(SOURCE).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def resolve_from(module: str, is_package: bool, node: ast.ImportFrom) -> str:
    """The absolute module an ``ImportFrom`` names (relative imports resolved against ``module``)."""

    if not node.level:
        return node.module or ""
    anchor = module.split(".") if is_package else module.split(".")[:-1]
    anchor = anchor[: len(anchor) - (node.level - 1)]
    return ".".join(anchor + ([node.module] if node.module else []))


#: Nodes that open a scope of their own (their bodies do not see the enclosing scope's bindings
#: shadowed inside them).
SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.ListComp, ast.SetComp,
               ast.DictComp, ast.GeneratorExp)


def outer_parts(node: ast.AST) -> list[ast.AST]:
    """The parts of a scope node that its enclosing scope evaluates (decorators, defaults, bases...)."""

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        arguments = node.args
        every = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                 *(argument for argument in (arguments.vararg, arguments.kwarg) if argument is not None)]
        parts: list[ast.AST] = [*arguments.defaults, *(value for value in arguments.kw_defaults if value is not None),
                                *(argument.annotation for argument in every if argument.annotation is not None)]
        if not isinstance(node, ast.Lambda):
            parts += [*node.decorator_list, *([node.returns] if node.returns is not None else [])]
        return parts
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *node.keywords]
    return [node.generators[0].iter]  # a comprehension


def scope_roots(scope: ast.AST) -> list[ast.AST]:
    """The nodes a scope evaluates itself (for a comprehension, all but its first iterable)."""

    if isinstance(scope, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return list(scope.body)
    if isinstance(scope, ast.Lambda):
        return [scope.body]
    roots: list[ast.AST] = [scope.key, scope.value] if isinstance(scope, ast.DictComp) else [scope.elt]
    for index, generator in enumerate(scope.generators):
        roots += [generator.target, *generator.ifs, *([generator.iter] if index else [])]
    return roots


def scope_nodes(scope: ast.AST) -> list[ast.AST]:
    """Every node evaluated in ``scope`` itself: nested scope nodes and their outer parts, not their bodies."""

    nodes: list[ast.AST] = []
    stack = scope_roots(scope)
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(outer_parts(node) if isinstance(node, SCOPE_NODES) else ast.iter_child_nodes(node))
    return nodes


def scope_bindings(scope: ast.AST, nodes: list[ast.AST], module: str, is_package: bool,
                   known: set[str]) -> dict[str, str | None]:
    """Name -> the kit module it is bound to in ``scope``, or ``None`` for anything else (a
    parameter, an assignment, a non-module import...). A name bound both ways is ``None``: its value
    is not statically a module."""

    bound: dict[str, set[str | None]] = {}

    def bind(name: str, value: str | None) -> None:
        bound.setdefault(name, set()).add(value)

    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        arguments = scope.args
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg,
                         arguments.kwarg):
            if argument is not None:
                bind(argument.arg, None)
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bind(alias.asname, alias.name if alias.name in known else None)
                else:
                    root = alias.name.partition(".")[0]
                    bind(root, root if root in known else None)
        elif isinstance(node, ast.ImportFrom):
            base = resolve_from(module, is_package, node)
            for alias in node.names:
                full = f"{base}.{alias.name}"
                bind(alias.asname or alias.name, full if full in known else None)
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bind(node.id, None)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(node.name, None)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bind(node.name, None)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bind(node.name, None)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bind(node.rest, None)
    return {name: next(iter(values)) if len(values) == 1 else None for name, values in bound.items()}


def uses(module: str, source: str, *, is_package: bool, modules: Iterable[str]) -> set[tuple[str, str]]:
    """Every ``(kit module, name)`` that ``source`` imports by name or reads as a module attribute.

    Imports bind module aliases (``from mod_base.pages import build``, ``import mod_base.x as y``,
    ``import mod_base.x``) in their own scope, with Python's scoping: a name a function rebinds
    (a parameter, a ``with ... as`` target) shadows an outer alias, and class bodies are invisible to
    the functions inside them. An attribute chain on an alias counts as a use of its first component
    that is not itself a module. Imports inside functions count like top-level ones."""

    known = set(modules)
    tree = ast.parse(source)
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = resolve_from(module, is_package, node)
            if base == "mod_base" or base.startswith("mod_base."):
                found.update((base, alias.name) for alias in node.names if f"{base}.{alias.name}" not in known)

    def lookup(name: str, frames: list[tuple[dict[str, str | None], bool]]) -> str | None:
        if name in frames[-1][0]:
            return frames[-1][0][name]
        for bindings, is_class in reversed(frames[:-1]):
            if not is_class and name in bindings:
                return bindings[name]
        return None

    def visit(scope: ast.AST, frames: list[tuple[dict[str, str | None], bool]]) -> None:
        nodes = scope_nodes(scope)
        frames = [*frames, (scope_bindings(scope, nodes, module, is_package, known), isinstance(scope, ast.ClassDef))]
        for node in nodes:
            if isinstance(node, SCOPE_NODES):
                visit(node, frames)
            if not isinstance(node, ast.Attribute):
                continue
            chain: list[str] = []
            value: ast.expr = node
            while isinstance(value, ast.Attribute):
                chain.append(value.attr)
                value = value.value
            prefix = lookup(value.id, frames) if isinstance(value, ast.Name) else None
            if prefix is None:
                continue
            chain.reverse()
            for part in chain[:-1]:
                prefix = f"{prefix}.{part}"
            if prefix in known and f"{prefix}.{chain[-1]}" not in known:
                found.add((prefix, chain[-1]))

    visit(tree, [])
    return found


def violations(user: str, user_unit: str, found: Iterable[tuple[str, str]], *, owners: Mapping[str, str],
               names: Mapping[str, set[str]]) -> list[str]:
    """The cross-unit uses of ``user`` that the document does not freeze."""

    problems = []
    for source, name in sorted(found):
        if name in MODULE_DUNDERS:
            continue
        source_unit = owner_of(source, owners)
        if source_unit is None:
            problems.append(f"{user} uses {source}.{name}, but {source} has no owner in the ownership table")
            continue
        if source_unit == user_unit:
            continue
        if is_private(source.rpartition(".")[2]) or is_private(name):
            problems.append(f"{user} ({user_unit}) uses the private {source}.{name} of {source_unit}")
            continue
        if name in names.get(source, set()) or name in names.get(defining_module(source, name) or "", set()):
            continue
        problems.append(f"{user} ({user_unit}) uses {source}.{name} of {source_unit}, "
                        f"which docs/INTERNAL-API.md does not list under `{source}`")
    return problems


def defining_module(source: str, name: str) -> str | None:
    """The module that defines the function or class ``source.name`` re-exports, if another one."""

    try:
        value = getattr(importlib.import_module(source), name, None)
    except ImportError:
        return None
    home = getattr(value, "__module__", None)
    if isinstance(home, str) and home != source and getattr(value, "__name__", None) == name:
        return home
    return None


def docstring_only(path: Path) -> bool:
    body = ast.parse(path.read_text(encoding="utf-8")).body
    return len(body) <= 1 and all(isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                                  and isinstance(node.value.value, str) for node in body)


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
            for kind, name, detail in entries:
                with self.subTest(module=module_name, kind=kind, name=name):
                    self.assertNotEqual(kind, "unparsed", f"no pattern reads this entry of {module_name}: {name}")
                    if kind == "name":
                        self.assertTrue(hasattr(module, name), f"{module_name}.{name} is documented but missing")
                        if detail is not None:
                            complete, value = documented_value(detail)
                            if complete:
                                actual = getattr(module, name)
                                self.assertEqual(actual, value, f"{module_name}.{name} is not its documented value")
                                self.assertIs(type(actual), type(value))
                        continue
                    target = module
                    for part in name.split("."):
                        target = inspect.getattr_static(target, part) if inspect.isclass(target) else getattr(target, part)
                    if kind == "class":
                        self.assertTrue(inspect.isclass(target))
                        continue
                    if kind == "fields":
                        assert detail is not None
                        self.assertTrue(dataclasses.is_dataclass(target), f"{name} is not a dataclass")
                        fields = [field.name for field in dataclasses.fields(target)]
                        self.assertEqual(parameter_names(f"({detail})"), fields)
                        self.assertEqual(field_defaults(detail), [rendered_default(field)
                                                                  for field in dataclasses.fields(target)],
                                         f"the documented defaults of {name} differ from the code")
                        continue
                    if kind == "property":
                        self.assertIsInstance(target, (property, functools.cached_property))
                        continue
                    if isinstance(target, property):
                        continue
                    if isinstance(target, (classmethod, staticmethod)):
                        target = target.__func__
                    self.assertTrue(callable(target))
                    assert detail is not None
                    self.assertEqual(parameter_names(detail), actual_names(target))

    def test_every_command_module_is_documented(self) -> None:
        from mod_base.cli import COMMANDS

        sections = documented()
        for module_name in set(COMMANDS.values()):
            self.assertIn(module_name, sections)
            self.assertIn(("def", "register", "(subparsers: argparse._SubParsersAction) -> None"),
                          sections[module_name])

    def test_limits_section_lists_every_bound(self) -> None:
        tree = ast.parse(Path(limits.__file__).read_text(encoding="utf-8"))
        bounds = set()
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            bounds.update(target.id for target in targets if isinstance(target, ast.Name) and not is_private(target.id))
        self.assertIn("MAX_COLLECTED_FAMILY_BYTES", bounds)
        self.assertEqual(documented_names(documented())["mod_base.model.limits"], bounds)


class OwnershipTest(unittest.TestCase):
    def test_every_module_has_an_owner_and_a_section(self) -> None:
        owners = ownership()
        sections = documented()
        for module, path in kit_modules().items():
            with self.subTest(module=module):
                unit = owner_of(module, owners)
                if unit is None:
                    # Only a package ``__init__`` may be unowned, and then it can export nothing.
                    self.assertEqual(path.name, "__init__.py", "add the module to the ownership table")
                    self.assertTrue(docstring_only(path), "a package __init__ shared by units holds only its docstring")
                    continue
                leaf = module.rpartition(".")[2]
                if not is_private(leaf) and leaf != "__main__" and path.name != "__init__.py":
                    self.assertIn(module, sections, "a public module needs its own section")

    def test_every_listed_module_exists(self) -> None:
        modules = kit_modules()
        for module in ownership():
            with self.subTest(module=module):
                self.assertIn(module, modules)


class CrossUnitUseTest(unittest.TestCase):
    """Code -> document: a unit only depends on names the document freezes."""

    def test_cross_unit_uses_are_documented(self) -> None:
        owners, names, modules = ownership(), documented_names(documented()), kit_modules()
        problems: list[str] = []
        for module, path in modules.items():
            unit = owner_of(module, owners)
            if unit is None:
                continue  # a docstring-only package (test_every_module_has_an_owner_and_a_section)
            found = uses(module, path.read_text(encoding="utf-8"), is_package=path.name == "__init__.py",
                         modules=modules)
            problems += violations(module, unit, found, owners=owners, names=names)
        for path in sorted(TOOLS.glob("*.py")):
            found = uses(f"tools.{path.stem}", path.read_text(encoding="utf-8"), is_package=False, modules=modules)
            problems += violations(f"tools/{path.name}", TOOLS_UNIT, found, owners=owners, names=names)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_known_cross_unit_names_are_frozen(self) -> None:
        # The integration round's inventory: each of these is used by another unit today.
        names = documented_names(documented())
        expected = {
            "mod_base.evidence.validate": {"Bundle", "load_handoff", "load_compact", "check_compact_pixels",
                                           "check_extensions_verified", "check_handoff"},
            "mod_base.evidence.compact": {"rederive", "read_draft", "bind_draft", "finalize"},
            "mod_base.evidence.expectation": {"tested_run_projection", "require_rederived_for_runs"},
            "mod_base.github.jobs": {"actions_time"},
            "mod_base.io.bounded_zip": {"LIMITS_BY_KIND", "archive_limit", "artifact_limit"},
            "mod_base.family.envelope": {"MAX_NATIVE_FILE_BYTES"},
            # family collect --selected-json writes it; build and refresh read it (conformance checks it).
            "mod_base.family.paired": {"SELECTED_NAME", "collect_family"},
            "mod_base.cli": {"COMMANDS", "KEY", "FAMILY", "SHA1", "DIGEST", "BRANCH", "POSITIVE", "KEYS", "DELAY",
                             "PATH"},
            "mod_base.adapter.protocol": {"HOOK_JOBS", "ImageFactory", "EXTENSION_OBJECTS", "TESTED_RUN_PROJECTION"},
            "mod_base.model.documents": {"RUN_CLAIM_FIELDS", "FRAME_FIELDS", "RUN_CLAIM", "SUBJECT", "BRANCH", "SHA1"},
            "mod_base.workflow": {"CALLER"},
            "mod_base.adapter.host": {"check_placement", "placement", "MAX_CHILD_OUTPUT_BYTES"},
            "mod_base.evidence.compose": {"authenticate_baseline"},
            "mod_base.evidence.anchor": {"eligible_nodes"},
            "mod_base.github.artifacts": {"MAX_ARCHIVE_BYTES"},
            "mod_base.pages.authenticate": {"write_new_file", "kit_binding"},
            "mod_base.pages.select": {"SourceRuns", "FamilyGenerations", "family_archive_limit"},
            "mod_base.pages.targets": {"branch_heads", "commit_tree_local", "discover_targets"},
            "mod_base.pages.templating": {"theme_color"},
            "mod_base.pages.build": {"current_implementation", "require_current_run", "check_checkouts", "BuildError",
                                     "FAMILY_SELECTED_NAME"},
            "mod_base.pin": {"yaml_unescape", "staged_listing", "verify_staged_files", "require_reachable",
                             "verify_released", "resolve", "unclean_paths", "LOCKED_DIRS", "STAGED_LOCK",
                             "KIT_REPOSITORY_BARE"},
            "mod_base.template.tool": {"evaluate", "pending", "DEFERRABLE", "extension_violations", "link_violations"},
            "mod_base.template.lock": {"recorded", "write", "main"},
            "mod_base": {"__version__", "SCHEMA_VERSIONS"},
        }
        for module, frozen in expected.items():
            with self.subTest(module=module):
                self.assertLessEqual(frozen, names.get(module, set()))


class ScannerTest(unittest.TestCase):
    """The code -> document scan itself (synthetic sources, so its rules cannot silently weaken)."""

    OWNERS = {"mod_base.a.one": "MB1", "mod_base.a.two": "MB1", "mod_base.b.three": "MB2"}
    MODULES = ("mod_base", "mod_base.a", "mod_base.a.one", "mod_base.a.two", "mod_base.a._private",
               "mod_base.b", "mod_base.b.three")

    def scan(self, source: str, *, user: str = "mod_base.b.three", documented_: Mapping[str, set[str]] | None = None,
             is_package: bool = False) -> list[str]:
        found = uses(user, source, is_package=is_package, modules=self.MODULES)
        return violations(user, owner_of(user, self.OWNERS) or TOOLS_UNIT, found, owners=self.OWNERS,
                          names=documented_ or {})

    def test_import_by_name_is_a_use(self) -> None:
        self.assertEqual(len(self.scan("from mod_base.a.one import helper\n")), 1)
        self.assertEqual(self.scan("from mod_base.a.one import helper\n",
                                   documented_={"mod_base.a.one": {"helper"}}), [])

    def test_attribute_of_an_aliased_module_is_a_use(self) -> None:
        for source in ("from mod_base.a import one\none.helper()\n",
                       "import mod_base.a.one as alias\nalias.helper\n",
                       "import mod_base.a.one\nmod_base.a.one.helper\n",
                       "def f():\n    from mod_base.a import one as o\n    return o.helper\n"):
            with self.subTest(source=source):
                problems = self.scan(source)
                self.assertEqual(len(problems), 1)
                self.assertIn("mod_base.a.one.helper", problems[0])

    def test_shadowing_follows_python_scopes(self) -> None:
        # A local rebinding of an alias name is not the module (tools/update_tree_digest.py).
        shadowed = ("from mod_base.a import one\n"
                    "def f(one):\n    return one.read()\n"
                    "def g():\n    with open('x') as one:\n        one.seek(0)\n"
                    "def h():\n    return [one.close() for one in ()]\n")
        self.assertEqual(self.scan(shadowed), [])
        # A local import binds only in its function; the module-level alias still counts elsewhere.
        local = ("def f():\n    from mod_base.a import one\n    return one.helper\n"
                 "def g(one):\n    return one.other\n")
        self.assertEqual([problem.split(" uses ")[1].split(" ")[0] for problem in self.scan(local)],
                         ["mod_base.a.one.helper"])
        # Class bodies are invisible to their methods; decorators and defaults run in the enclosing scope.
        klass = ("from mod_base.a import one\n"
                 "class C:\n    one = None\n    def m(self):\n        return one.helper\n")
        self.assertEqual([problem.split(" uses ")[1].split(" ")[0] for problem in self.scan(klass)],
                         ["mod_base.a.one.helper"])
        decorated = ("from mod_base.a import one\n"
                     "@one.decorator\ndef f(one=one.default):\n    return one.shadowed\n")
        self.assertEqual(sorted(problem.split(" uses ")[1].split(" ")[0] for problem in self.scan(decorated)),
                         ["mod_base.a.one.decorator", "mod_base.a.one.default"])

    def test_relative_imports_resolve(self) -> None:
        problems = self.scan("from ..a.one import helper\n", user="mod_base.b.three")
        self.assertEqual(len(problems), 1)
        self.assertIn("mod_base.a.one.helper", problems[0])

    def test_same_unit_and_module_imports_are_free(self) -> None:
        self.assertEqual(self.scan("from mod_base.a.two import anything\n", user="mod_base.a.one"), [])
        self.assertEqual(self.scan("from mod_base.a import one\n"), [])
        self.assertEqual(self.scan("import os\nfrom collections import abc\nabc.Mapping\n"), [])

    def test_private_names_and_modules_never_cross(self) -> None:
        documented_ = {"mod_base.a.one": {"_helper"}}
        self.assertIn("private", self.scan("from mod_base.a.one import _helper\n", documented_=documented_)[0])
        self.assertIn("private", self.scan("from mod_base.a._private import thing\n")[0])
        self.assertEqual(self.scan("from mod_base.a._private import thing\n", user="mod_base.a.one"), [])

    def test_python_module_attributes_are_ignored_but_version_is_not(self) -> None:
        owners = dict(self.OWNERS, mod_base="MB0")
        found = uses("mod_base.b.three", "import mod_base\nmod_base.__file__\nmod_base.__version__\n",
                     is_package=False, modules=self.MODULES)
        problems = violations("mod_base.b.three", "MB2", found, owners=owners, names={})
        self.assertEqual(len(problems), 1)
        self.assertIn("__version__", problems[0])

    def test_tools_are_foreign_to_every_unit(self) -> None:
        problems = self.scan("from mod_base.a.one import helper\n", user="tools.example")
        self.assertEqual(len(problems), 1)

    def test_a_re_export_is_frozen_where_it_is_defined(self) -> None:
        # ``DocumentError`` is defined in ``validators`` and re-exported by ``documents``.
        owners = {"mod_base.model.documents": "MB0", "mod_base.model.validators": "MB0"}
        found = uses("mod_base.b.three", "from mod_base.model.documents import DocumentError\n",
                     is_package=False, modules=("mod_base.model.documents", "mod_base.model.validators"))
        self.assertEqual(violations("mod_base.b.three", "MB2", found, owners=owners,
                                    names={"mod_base.model.validators": {"DocumentError"}}), [])
        self.assertEqual(len(violations("mod_base.b.three", "MB2", found, owners=owners, names={})), 1)

    def test_unowned_source_is_reported(self) -> None:
        self.assertIn("no owner", self.scan("from mod_base.b import thing\n")[0])

    def test_document_parsing(self) -> None:
        text = ("## `mod_base.x`\n\n* `def f(a: int) -> None`: doc\n* `class C(Base)`: doc\n"
                "  * `m(self) -> None`\n* `CONST = 1`\n* `OTHER`: description\n* `A`, `B`, `C`\n"
                "prose `NOT_A_NAME` here\n\n| MB3 | `mod_base.x`, `mod_base.y` |\n")
        self.assertEqual(documented_names(documented(text)), {"mod_base.x": {"f", "C", "CONST", "OTHER", "A", "B", "C"}})
        self.assertIn(("method", "C.m", "(self) -> None"), documented(text)["mod_base.x"])
        self.assertIn(("name", "CONST", "1"), documented(text)["mod_base.x"])
        self.assertEqual(ownership(text), {"mod_base.x": "MB3", "mod_base.y": "MB3"})

    def test_every_entry_kind_is_read(self) -> None:
        # Properties, dataclass fields and described name lists are checked too; an entry line no
        # pattern reads is reported (the signature test fails on it) instead of being skipped.
        text = ("## `mod_base.x`\n\n* `class D`: doc\n  * fields: `a: int, b: dict[str, int] = <factory>`\n"
                "  * `size` (property) -> `int`\n* `P`, `Q`: two validators of `x` and `y`\n"
                "* `SET = frozenset({'b', 'a'})`\n* `LONG = ('a', 'b', ...`\n* `broken entry\n  * orphan detail\n")
        entries = documented(text)["mod_base.x"]
        self.assertIn(("fields", "D", "a: int, b: dict[str, int] = <factory>"), entries)
        self.assertIn(("property", "D.size", None), entries)
        self.assertEqual(documented_names({"mod_base.x": entries})["mod_base.x"], {"D", "P", "Q", "SET", "LONG"})
        self.assertEqual([name for kind, name, _ in entries if kind == "unparsed"],
                         ["* `broken entry", "  * orphan detail"])
        self.assertEqual(parameter_names("(a: int, b: dict[str, int] = <factory>)"), ["a", "b"])

    def test_documented_values(self) -> None:
        self.assertEqual(documented_value("frozenset({'b', 'a'})"), (True, frozenset({"a", "b"})))
        self.assertEqual(documented_value("frozenset()"), (True, frozenset()))
        self.assertEqual(documented_value("(1, 'x')"), (True, (1, "x")))
        self.assertEqual(documented_value("30.0"), (True, 30.0))
        self.assertEqual(documented_value("('a', 'b', ..."), (False, None))
        with self.assertRaises(ValueError):
            documented_value("SomeName")

    def test_private_module_owner_follows_its_package(self) -> None:
        self.assertEqual(owner_of("mod_base.a._private", self.OWNERS), "MB1")
        self.assertIsNone(owner_of("mod_base.a", self.OWNERS))
        self.assertIsNone(owner_of("mod_base.c._x", self.OWNERS))


if __name__ == "__main__":
    unittest.main()
