"""The repository-configuration template: manifest, check, sync and init (MB9, SPEC §8).

Every tool test runs against a temporary kit root holding the real ``template/`` sources plus a
small synthetic managed caller with the SPEC §5.2 markers, so the tool is tested independently of
the exact caller YAML; the real caller template is checked on its own when present.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base import cli
from mod_base.config import parse_config
from mod_base.errors import MbError
from mod_base.model.documents import validate_template_manifest
from mod_base.pin import ACTIONS_LOCK, STAGED_LOCK, actions_listing, staged_listing
from mod_base.template import lock, tool
from tests.test_pin import load_bootstrap

KIT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = KIT_ROOT / "template"
CALLER_TEMPLATE = TEMPLATE_ROOT / "managed" / ".github" / "workflows" / "pages.yml"
CONFIG_FIXTURE = KIT_ROOT / "tests" / "fixtures" / "documents" / "config" / "qs.json"
SHA = "1" * 40
OTHER_SHA = "2" * 40

#: QS's ``.gitattributes`` at origin/master fd8e7dbf1 (SPEC §8.1): the managed bytes through v0.9.1, which
#: v0.9.2 keeps as the file's start (:data:`EOL_RULES` follow).
QS_GITATTRIBUTES = (
    b"# Gradle ships gradlew.bat with CRLF line endings, as a Windows batch file must have.\n"
    b"# Without this, git's whitespace check reads each carriage return as a trailing space and\n"
    b"# reports every changed line as an error. That is not cosmetic here: the version-port\n"
    b"# packager runs `git diff --cached --check` over the staged port, so a wrapper bump makes\n"
    b"# every release-branch port abort before it can be validated or published.\n"
    b"#\n"
    b"# `cr-at-eol` adds to git's default rules rather than replacing them, so genuine trailing\n"
    b"# whitespace before the carriage return is still reported.\n"
    b"*.bat whitespace=cr-at-eol\n"
)

#: The rules v0.9.2 adds after them: every managed and fragment path is checked out with LF, so a
#: ``core.autocrlf=true`` clone (Git for Windows' default) passes ``template check``.
EOL_RULES = (
    b"\n"
    b"# mod-base manages the files below: `template check` compares the managed ones (the caller's\n"
    b"# managed region included) byte for byte with the kit's template and reads the others line by\n"
    b"# line. Git for Windows' default `core.autocrlf=true` would check them out with CRLF line endings\n"
    b"# and fail that check on a clean clone, so they are always checked out with LF, whatever the local\n"
    b"# setting. The list is exactly the template manifest's managed and fragment paths.\n"
    b"/.gitattributes text eol=lf\n"
    b"/.gitignore text eol=lf\n"
    b"/.github/CODEOWNERS text eol=lf\n"
    b"/.github/dependabot.yml text eol=lf\n"
    b"/.github/pull_request_template.md text eol=lf\n"
    b"/.github/workflows/pages.yml text eol=lf\n"
    b"/AGENTS.md text eol=lf\n"
    b"/docs/ai/shared/PUBLIC-EVIDENCE.md text eol=lf\n"
    b"/docs/ai/shared/REPOSITORY.md text eol=lf\n"
    b"/scripts/ci/mod_base_kit.py text eol=lf\n"
)

#: Block Pops' ``.gitignore`` at origin/master 9ba22a2 (its adoption PR A cannot change it).
BLOCK_POPS_GITIGNORE = (
    "# Gradle\n.gradle/\nbuild/\ne2e-out/\n\n# Python\n__pycache__/\n*.py[cod]\n\n# IntelliJ IDEA\n.idea/\n*.iml\n"
    "*.iws\nout/\n\n# Minecraft\nrun/\nlogs/\n\n# Forgix output\nbuild/forgix/\n\n# OS\n.DS_Store\nThumbs.db\n\n"
    "# Plan file\nPLAN..md\n/.makingvibe\n"
)

SYNTHETIC_CALLER = """\
# >>> mod-base managed: pages caller v1 — edit only in The-Plum-Team/mod-base template/managed/.github/workflows/pages.yml
name: Project site
on:
  workflow_dispatch:
permissions: {}
jobs:
  verify-kit:
    name: Verify pinned mod-base
    runs-on: ubuntu-24.04
    steps:
      - name: Bind the executing callee to the protected pin
        run: |
          jq -r '.referenced_workflows // [] | .[] | select(.path | startswith("The-Plum-Team/mod-base/.github/workflows/"))'
          reach="$(gh api "repos/The-Plum-Team/mod-base/compare/$kit_sha...main")"
  publish:
    needs: verify-kit
    uses: The-Plum-Team/mod-base/.github/workflows/publish.yml@{{PIN}} # {{VERSION}}
  rotate:
    needs: verify-kit
    uses: The-Plum-Team/mod-base/.github/workflows/rotate.yml@{{PIN}} # {{VERSION}}
# <<< mod-base managed
# >>> mod-local extensions: only jobs whose id starts with "ext-"; no mod-base uses, no pages/id-token/actions:write
# <<< mod-local extensions
"""

#: The QS extension job of SPEC §5.7, verbatim: it must pass the extension rules.
QS_EXTENSION = """\
  ext-feature-coverage:
    concurrency:
      group: quick-skin-feature-baseline-request-${{ github.sha }}
      cancel-in-progress: false
      queue: max
    name: Request feature coverage after public baseline retention
    continue-on-error: true
    needs: [publish, finalize, request-rotation]
    if: >-
      always() && needs.publish.outputs.eligible == 'true' &&
      needs.finalize.result == 'success'
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    permissions:
      actions: read
      contents: write          # pre-existing QS repository_dispatch feature-coverage-requested (not a Pages wake)
    steps:
      - name: Check out the exact protected baseline readiness policy
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.sha }}
          persist-credentials: false
      - name: Install Python
        uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: "3.13"
      - name: Request a collector only for complete unscheduled readiness
        shell: bash
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          set -euo pipefail
          [[ "$GITHUB_REF" == refs/heads/master && "$GITHUB_RUN_ID" =~ ^[1-9][0-9]*$ ]]
          [[ "$(git rev-parse HEAD)" == "$GITHUB_SHA" ]]
          if [[ "$(python3 scripts/release/release_sources.py --kind mode)" != shared ]]; then exit 0; fi
          python3 scripts/ci/feature_coverage_request.py --producer-run-id "$GITHUB_RUN_ID" \\
            --github-repository "$GITHUB_REPOSITORY" --source-sha "$GITHUB_SHA"
"""


def write(root: Path, relative: str, data: str | bytes) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return target


def git(cwd: Path, *arguments: str, home: Path) -> str:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"})
    command = ["git", "-c", "user.name=mod-base test", "-c", "user.email=test@example.invalid",
               "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", f"core.hooksPath={os.devnull}",
               "-c", "init.defaultBranch=main", *arguments]
    return subprocess.run(command, cwd=cwd, env=environment, check=True, capture_output=True, text=True).stdout.strip()


def config_document(**template: Any) -> dict[str, Any]:
    document = json.loads(CONFIG_FIXTURE.read_text(encoding="utf-8"))
    document["template"] = {"agents_local": ["docs/ai/PROJECT.md"], "deferred": [], **template}
    return document


def caller_with(extension: str, sha: str = SHA, version: str = "v1.2.3") -> str:
    managed, marker, _ = SYNTHETIC_CALLER.partition("# <<< mod-local extensions\n")
    rendered = managed.replace("{{PIN}}", sha).replace("{{VERSION}}", version)
    return rendered + extension + marker


class TemplateCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="mb-template-")
        self.addCleanup(self._directory.cleanup)
        self.root = Path(os.path.realpath(self._directory.name))
        self.home = self.root / "home"
        self.home.mkdir()
        self.kit = self.root / "kit"
        shutil.copytree(TEMPLATE_ROOT, self.kit / "template", ignore=shutil.ignore_patterns("pages.yml"))
        write(self.kit, "template/managed/.github/workflows/pages.yml", SYNTHETIC_CALLER)
        self.repo = self.root / "mod"
        self.repo.mkdir()

    # -- a clean mod ----------------------------------------------------------------------------------

    def seed(self, name: str) -> str:
        return (TEMPLATE_ROOT / "seed" / name).read_text(encoding="utf-8")

    def make_clean_mod(self, config: dict[str, Any] | None = None) -> Path:
        repo = self.repo
        write(repo, "site/mod-base.json", json.dumps(config or config_document(), indent=2) + "\n")
        write(repo, ".github/workflows/e2e.yml",
              "jobs:\n  evidence:\n    steps:\n"
              f"      - uses: The-Plum-Team/mod-base/actions/prepare-evidence@{SHA} # v1.2.3\n")
        tool.sync(repo, kit_root=self.kit, write=True)
        write(repo, ".gitignore", self.seed(".gitignore.base"))
        write(repo, ".github/CODEOWNERS", self.seed(".github/CODEOWNERS.tmpl").replace("{{owner}}", "AkaNebur"))
        write(repo, ".github/dependabot.yml", self.seed(".github/dependabot.yml.tmpl"))
        write(repo, ".github/pull_request_template.md", self.seed(".github/pull_request_template.md.tmpl"))
        write(repo, "AGENTS.md", "@docs/ai/shared/REPOSITORY.md\n@docs/ai/shared/PUBLIC-EVIDENCE.md\n@docs/ai/PROJECT.md\n")
        write(repo, "docs/ai/PROJECT.md", "# Project\n")
        return repo

    def check(self) -> list[tool.Drift]:
        return tool.check(self.repo, kit_root=self.kit)

    def kinds(self) -> list[tuple[str, str]]:
        return [(drift.path, drift.kind) for drift in self.check()]


# -- The kit's own template ------------------------------------------------------------------------


class KitTemplateTest(unittest.TestCase):
    def manifest(self) -> dict[str, Any]:
        document = json.loads((TEMPLATE_ROOT / "manifest.json").read_text(encoding="utf-8"))
        return validate_template_manifest(document)

    def test_manifest_classes_follow_spec_8_1(self) -> None:
        entries = {entry["path"]: entry for entry in self.manifest()["files"]}
        by_class: dict[str, set[str]] = {}
        for path, entry in entries.items():
            by_class.setdefault(entry["class"], set()).add(path)
        self.assertEqual(by_class["managed"], {".gitattributes", ".github/workflows/pages.yml",
                                               "scripts/ci/mod_base_kit.py", "docs/ai/shared/REPOSITORY.md",
                                               "docs/ai/shared/PUBLIC-EVIDENCE.md"})
        self.assertEqual(by_class["fragment"], {".gitignore", ".github/CODEOWNERS", ".github/dependabot.yml",
                                                ".github/pull_request_template.md", "AGENTS.md"})
        self.assertEqual(by_class["seeded"], {"CONTRIBUTING.md", "LICENSE", "site/mod-base.json",
                                              "scripts/pages/mod_base_adapter.py", "docs/ai/PROJECT.md",
                                              "docs/architecture/decisions/README.md"})
        self.assertEqual(entries[".gitignore"]["lines"], ["e2e-out/", "out/", "_site/", "public-evidence/",
                                                          "__pycache__/", "*.py[cod]", "/.architectury-transformer/"])
        self.assertEqual(entries[".github/CODEOWNERS"]["markers"],
                         ["/.github/", "/site/", "/scripts/pages/", "/scripts/ci/", "/AGENTS.md", "/docs/ai/"])
        self.assertEqual(entries[".github/pull_request_template.md"]["markers"],
                         ["CONTRIBUTING.md", "AGENTS.md", "## Summary", "## Validation", "## AI assistance"])
        for path, entry in entries.items():
            if entry["class"] == "managed":
                self.assertEqual(entry["source"], f"managed/{path}")
        self.assertEqual(tuple(sorted(path for path in by_class["managed"] if path.startswith("docs/"))),
                         tuple(sorted(tool.SHARED_IMPORTS)))

    def test_gitignore_never_ignores_claude(self) -> None:
        seeded = (TEMPLATE_ROOT / "seed" / ".gitignore.base").read_text(encoding="utf-8").split("\n")
        self.assertFalse({".claude/", "/.claude/", ".claude"} & set(seeded))
        required = {entry["path"]: entry for entry in self.manifest()["files"]}[".gitignore"]["lines"]
        self.assertTrue(set(required) <= set(seeded))
        self.assertFalse(any(".claude" in line for line in required))

    def test_every_source_except_the_mb8_caller_exists(self) -> None:
        for entry in self.manifest()["files"]:
            if entry["source"] == "managed/.github/workflows/pages.yml":
                continue
            self.assertTrue((TEMPLATE_ROOT / entry["source"]).is_file(), entry["source"])
        for source in tool.LICENSE_TEMPLATES.values():
            self.assertTrue((TEMPLATE_ROOT / source).is_file(), source)

    @unittest.skipUnless(CALLER_TEMPLATE.is_file(), "the managed caller (MB8) is not present in this tree yet")
    def test_the_real_template_loads_and_its_caller_is_well_formed(self) -> None:
        manifest = tool.load_manifest(KIT_ROOT)
        caller = tool._caller_template(KIT_ROOT, "managed/.github/workflows/pages.yml")
        rendered = tool._render_managed(caller, tool.Pin(SHA, "v1.2.3", ())) + caller.begin + tool.EXTENSION_END
        from mod_base.pin import parse_pin_files
        found = parse_pin_files({".github/workflows/pages.yml": rendered.encode("utf-8")})
        self.assertEqual((found.sha, found.version), (SHA, "v1.2.3"))
        self.assertEqual(len(manifest["files"]), 16)

    @unittest.skipUnless(CALLER_TEMPLATE.is_file(), "the managed caller (MB8) is not present in this tree yet")
    def test_a_mod_synced_from_the_real_template_is_clean(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mb-template-real-") as directory:
            repo = Path(directory)
            write(repo, "site/mod-base.json", json.dumps(config_document(), indent=2) + "\n")
            write(repo, ".github/workflows/e2e.yml",
                  f"jobs:\n  evidence:\n    steps:\n      - uses: The-Plum-Team/mod-base/actions/prepare-evidence@{SHA} # v1.2.3\n")
            tool.sync(repo, kit_root=KIT_ROOT, write=True)
            for name, target in ((".gitignore.base", ".gitignore"), (".github/dependabot.yml.tmpl", ".github/dependabot.yml"),
                                 (".github/pull_request_template.md.tmpl", ".github/pull_request_template.md")):
                write(repo, target, (TEMPLATE_ROOT / "seed" / name).read_bytes())
            write(repo, ".github/CODEOWNERS",
                  (TEMPLATE_ROOT / "seed/.github/CODEOWNERS.tmpl").read_text().replace("{{owner}}", "AkaNebur"))
            write(repo, "AGENTS.md", "@docs/ai/shared/REPOSITORY.md\n@docs/ai/shared/PUBLIC-EVIDENCE.md\n@docs/ai/PROJECT.md\n")
            write(repo, "docs/ai/PROJECT.md", "# Project\n")
            self.assertEqual(tool.check(repo, kit_root=KIT_ROOT), [])
            caller = repo / ".github/workflows/pages.yml"
            text = caller.read_text()
            self.assertIn(f"publish.yml@{SHA} # v1.2.3\n", text)
            caller.write_text(text.replace("# <<< mod-local extensions\n", QS_EXTENSION + "# <<< mod-local extensions\n"))
            self.assertEqual(tool.check(repo, kit_root=KIT_ROOT), [])
            self.assertEqual(tool.sync(repo, kit_root=KIT_ROOT, write=False), [])

    def test_the_staged_file_lock_is_maintained_by_its_module(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mb-lock-") as directory:
            kit = Path(directory)
            shutil.copytree(TEMPLATE_ROOT, kit / "template")
            write(kit, "tools/helper.py", "x = 1\n")
            write(kit, "actions/setup/action.yml", "name: Setup\n")
            (kit / "src" / "mod_base" / "template").mkdir(parents=True)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(lock.main(["--root", str(kit)]), 1)
                self.assertEqual(lock.main(["--root", str(kit), "--write"]), 0)
                self.assertEqual((kit / STAGED_LOCK).read_bytes(), staged_listing(kit))
                self.assertEqual((kit / ACTIONS_LOCK).read_bytes(), actions_listing(kit))
                self.assertNotIn(b"./actions/", staged_listing(kit))
                self.assertEqual(lock.main(["--root", str(kit)]), 0)
                self.assertFalse(lock.write(kit))
                write(kit, "actions/setup/action.yml", "name: Changed\n")
                self.assertEqual(lock.stale(kit), [ACTIONS_LOCK])
                self.assertEqual(lock.main(["--root", str(kit)]), 1)
                self.assertTrue(lock.write(kit))
                self.assertEqual(lock.stale(kit), [])
                write(kit, "tools/helper.py", "x = 2\n")
                self.assertEqual(lock.stale(kit), [STAGED_LOCK])
                self.assertEqual(lock.main(["--root", str(kit)]), 1)
                write(kit, "tools/__pycache__/helper.cpython-313.pyc", b"\0")
                self.assertEqual(lock.main(["--root", str(kit), "--write"]), 2)

    def test_managed_gitattributes_is_the_qs_file_plus_lf_checkouts(self) -> None:
        managed = (TEMPLATE_ROOT / "managed" / ".gitattributes").read_bytes()
        self.assertEqual(managed, QS_GITATTRIBUTES + EOL_RULES)
        rules = [line.split() for line in managed.decode("ascii").splitlines() if line and not line.startswith("#")]
        self.assertIn(["*.bat", "whitespace=cr-at-eol"], rules)
        checked = sorted(entry["path"] for entry in self.manifest()["files"]
                         if entry["class"] in ("managed", "fragment"))
        self.assertEqual(sorted(rule[0] for rule in rules if rule[1:] == ["text", "eol=lf"]),
                         sorted(f"/{path}" for path in checked))
        self.assertIn(".github/workflows/pages.yml", checked)

    def test_managed_documents_obey_the_link_rule_and_stay_mod_neutral(self) -> None:
        managed = frozenset(tool.SHARED_IMPORTS)
        for path in tool.SHARED_IMPORTS:
            text = (TEMPLATE_ROOT / "managed" / path).read_text(encoding="utf-8")
            self.assertEqual(tool.link_violations(path, text, managed), [], path)
            lowered = text.lower()
            for name in ("quick skin", "quickskin", "quick-skin", "blockpops", "block pops", "block-pops",
                         "etherology", "1.20.1", "fabric 1."):
                self.assertNotIn(name, lowered, f"{path} mentions {name}")
            self.assertNotRegex(text, r"\{\{[a-z_]+\}\}")

    def test_seeded_config_and_adapter_are_valid(self) -> None:
        parse_config((TEMPLATE_ROOT / "seed" / "site" / "mod-base.json.tmpl").read_bytes())
        source = (TEMPLATE_ROOT / "seed" / "scripts" / "pages" / "mod_base_adapter.py.tmpl").read_text()
        with tempfile.TemporaryDirectory() as directory:
            adapter = write(Path(directory), "mod_base_adapter.py", source.replace("{{name}}", "Example"))
            spec = importlib.util.spec_from_file_location("seeded_adapter_under_test", adapter)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        self.assertEqual(module.ADAPTER_API, 1)
        calls = {"targets": (None,), "expectation": ({}, None, {}), "collect": ("/", {}, {})}
        for hook, arguments in calls.items():
            with self.subTest(hook), self.assertRaises(NotImplementedError):
                getattr(module, hook)(object(), *arguments)
        for optional in ("compose", "family_validate", "anchor_selection", "authenticate_extensions"):
            self.assertFalse(hasattr(module, optional))

    def test_seeded_license_renders_the_mod_arr_head(self) -> None:
        text = (TEMPLATE_ROOT / "seed" / "LICENSE-ARR.tmpl").read_text(encoding="utf-8")
        values = tool.seed_values(parse_config((KIT_ROOT / "tests/fixtures/documents/config/qs.json").read_bytes()))
        rendered = tool._render_seed(text, {**values, "years": "2025-2026", "holder": "AkaNebur"})
        lines = rendered.split("\n")
        self.assertEqual(lines[0], "Quick Skin — All Rights Reserved")
        self.assertEqual(lines[2], "Copyright (c) 2025-2026 AkaNebur. All rights reserved.")
        self.assertEqual(lines[9], "  - Modrinth   https://modrinth.com/mod/quick-skin")
        self.assertEqual(lines[10], "  - CurseForge https://www.curseforge.com/minecraft/mc-mods/quick-skin")
        self.assertEqual(lines[24], "    Quick Skin as a dependency).")
        self.assertEqual(lines[43:47], ["CONTRIBUTIONS", "",
                                        "By submitting a pull request you agree that your contribution is licensed to",
                                        "the copyright holder under these same terms."])
        self.assertEqual(tool.unresolved_placeholders(rendered.encode()), ["third_party"])


# -- check ------------------------------------------------------------------------------------------


class CheckTest(TemplateCase):
    def test_a_clean_mod_has_no_drift(self) -> None:
        self.make_clean_mod()
        self.assertEqual(self.check(), [])
        write(self.repo, ".github/workflows/pages.yml", caller_with(QS_EXTENSION))
        self.assertEqual(self.check(), [])

    def test_managed_drift_is_a_unified_diff(self) -> None:
        self.make_clean_mod()
        path = self.repo / "docs/ai/shared/REPOSITORY.md"
        path.write_text(path.read_text() + "local addition\n")
        drifts = self.check()
        self.assertEqual([(drift.path, drift.kind) for drift in drifts], [("docs/ai/shared/REPOSITORY.md", "changed")])
        self.assertIn("+local addition", drifts[0].detail)
        self.assertIn("--- mod-base/template/docs/ai/shared/REPOSITORY.md", drifts[0].detail)
        (self.repo / "scripts/ci/mod_base_kit.py").unlink()
        self.assertIn(("scripts/ci/mod_base_kit.py", "missing"), self.kinds())

    def test_symlinked_or_crlf_managed_files_drift(self) -> None:
        self.make_clean_mod()
        target = self.repo / ".gitattributes"
        data = target.read_bytes()
        target.unlink()
        write(self.root, "elsewhere", data)
        target.symlink_to(self.root / "elsewhere")
        self.assertEqual(self.kinds(), [(".gitattributes", "changed")])
        target.unlink()
        write(self.repo, ".gitattributes", data.replace(b"\n", b"\r\n"))
        self.assertEqual([(drift.path, drift.kind, drift.detail) for drift in self.check()],
                         [(".gitattributes", "changed", tool.CRLF_ADVICE)])
        write(self.repo, ".gitattributes", data.replace(b"\n", b"\r\n") + b"extra\r\n")
        advice, drift = self.check()
        self.assertEqual(advice.detail, tool.CRLF_ADVICE)
        self.assertIn("+extra", drift.detail)
        self.assertNotIn("-/.gitattributes text eol=lf", drift.detail, "the diff is of the LF form")

    def test_a_crlf_caller_is_checked_on_its_lf_form(self) -> None:
        self.make_clean_mod()
        caller = self.repo / ".github/workflows/pages.yml"
        write(self.repo, ".github/workflows/pages.yml", caller_with(QS_EXTENSION).replace("\n", "\r\n"))
        self.assertEqual([(drift.path, drift.detail) for drift in self.check()],
                         [(".github/workflows/pages.yml", tool.CRLF_ADVICE)])
        caller.write_bytes(caller_with(QS_EXTENSION).replace("name: Project site", "name: Edited")
                           .replace("\n", "\r\n").encode())
        advice, drift = self.check()
        self.assertEqual(advice.detail, tool.CRLF_ADVICE)
        self.assertIn("+name: Edited", drift.detail)
        self.assertNotIn("region marker", drift.detail)

    def crlf_clone(self, name: str) -> Path:
        """The mod committed with LF, then cloned with ``core.autocrlf=true`` (a Windows checkout)."""

        git(self.repo, "init", "-q", home=self.home)
        git(self.repo, "-c", "core.autocrlf=false", "add", "-A", home=self.home)
        git(self.repo, "commit", "-q", "-m", "mod", home=self.home)
        clone = self.root / name
        git(self.root, "clone", "-q", "-c", "core.autocrlf=true", str(self.repo), str(clone), home=self.home)
        return clone

    def test_an_autocrlf_checkout_passes_because_the_managed_attributes_pin_lf(self) -> None:
        self.make_clean_mod()
        write(self.repo, ".github/workflows/pages.yml", caller_with(QS_EXTENSION))
        clone = self.crlf_clone("windows")
        self.assertIn(b"\r\n", (clone / "site/mod-base.json").read_bytes(), "the clone converts ordinary text")
        for path in tool.SHARED_IMPORTS + (".github/workflows/pages.yml", "scripts/ci/mod_base_kit.py", "AGENTS.md"):
            self.assertNotIn(b"\r", (clone / path).read_bytes(), path)
        self.assertEqual(tool.check(clone, kit_root=self.kit), [])

    def test_an_autocrlf_checkout_without_the_lf_rules_reports_its_line_endings(self) -> None:
        self.make_clean_mod()
        write(self.repo, ".gitattributes", QS_GITATTRIBUTES)
        clone = self.crlf_clone("windows-before")
        drifts = {drift.path: drift for drift in tool.check(clone, kit_root=self.kit)}
        for path in (".github/workflows/pages.yml", "scripts/ci/mod_base_kit.py", *tool.SHARED_IMPORTS):
            self.assertEqual((drifts[path].kind, drifts[path].detail), ("changed", tool.CRLF_ADVICE), path)
        self.assertIn("text eol=lf", drifts[".gitattributes"].detail)
        self.assertNotIn(".gitignore", drifts, "fragments are read line by line")

    def test_caller_region_rules(self) -> None:
        self.make_clean_mod()
        caller = self.repo / ".github/workflows/pages.yml"
        clean = caller.read_text()
        cases = {
            "edited managed region": clean.replace("name: Project site", "name: Something else"),
            "stale pin": caller_with("", sha=OTHER_SHA),
            "trailing content": clean + "  extra:\n",
            "missing end marker": clean.replace("# <<< mod-local extensions\n", ""),
            "no newline at end": clean[:-1],
            "content before managed region": "# note\n" + clean,
        }
        for label, text in cases.items():
            with self.subTest(label):
                caller.write_text(text)
                kinds = self.kinds()
                self.assertTrue(kinds and all(path == ".github/workflows/pages.yml" for path, _ in kinds), kinds)
        caller.write_text(clean)
        self.assertEqual(self.check(), [])

    def test_extension_rules(self) -> None:
        self.make_clean_mod()
        caller = self.repo / ".github/workflows/pages.yml"
        job = "  ext-ok:\n    runs-on: ubuntu-24.04\n    permissions:\n      contents: read\n    steps:\n      - run: true\n"
        bad = {
            "job id": job.replace("ext-ok", "request-coverage"),
            "uppercase id": job.replace("ext-ok", "ext-OK"),
            "flow job": "  ext-flow: {runs-on: ubuntu-24.04}\n",
            "pages permission": job.replace("contents: read", "pages: write"),
            "id-token permission": job.replace("contents: read", "id-token: write"),
            "actions write": job.replace("contents: read", "actions: write"),
            "flow actions write": job.replace("permissions:\n      contents: read", "permissions: {actions: write}"),
            "write-all": job.replace("permissions:\n      contents: read", "permissions: write-all"),
            "kit mention": job.replace("- run: true", "- run: curl -O https://github.com/The-Plum-Team/mod-base/archive/x"),
            "needs rotate": job + "    needs: rotate\n",
            "needs rotate list": job + "    needs:\n      - publish\n      - rotate\n",
            "needs rotate flow": job + "    needs: [publish, \"rotate\"]\n",
            "rotate result": job.replace("- run: true", "- if: needs.rotate.result == 'success'\n        run: true"),
            "anchor": job + "    env: &shared\n      A: b\n",
            "alias": job + "    env: *shared\n",
            "extends a managed job": "    extra-step: true\n" + job,
            "top-level key": job + "env:\n  A: b\n",
            "tab": job.replace("      - run: true", "\t- run: true"),
            "github-pages environment": job + "    environment: github-pages\n",
            "publication lock": job + "    concurrency: mod-base-pages-publication\n",
            "duplicate": job + job,
            "impersonating name": job + "    name: Deploy GitHub Pages\n",
            "callee-like name": job + "    name: 'Publish / Collect mc1.20.1'\n",
            "expression name": job + "    name: ${{ github.event.inputs.x }}\n",
            "folded name": job + "    name: Deploy\n      GitHub Pages\n",
            "block scalar name": job + "    name: >-\n      Deploy GitHub Pages\n",
            "write inside needs": job + "    needs:\n      - publish\n      - pages: write\n",
            # A permission, environment or need written so that no single line holds it.
            "actions write on the next line": job.replace("      contents: read", "      actions:\n        write"),
            "actions write folded": job.replace("      contents: read", "      actions: >-\n        write"),
            "actions write literal": job.replace("      contents: read", "      actions: |-\n        write"),
            "tagged actions write": job.replace("contents: read", "actions: !!str write"),
            "flow permissions across lines": job.replace("permissions:\n      contents: read",
                                                         "permissions: {actions:\n        write}"),
            "flow continued at the key indentation": job.replace("permissions:\n      contents: read",
                                                                 "permissions: {actions:\n    write}"),
            "explicit key": job.replace("      contents: read", "      ? pages\n      : write"),
            "escaped pages": job.replace("contents: read", '"p\\x61ges": write'),
            "escaped id-token": job.replace("contents: read", '"id-t\\x6fken": write'),
            "escaped environment": job + '    environment: "github-p\\x61ges"\n',
            "escaped needs": job + '    needs:\n      - "r\\x6ftate"\n',
            "escaped quote hides a comment": job.replace("permissions:\n      contents: read",
                                                         'permissions: {foo: "\\" #", pages: write, id-token: write}'),
            "multi-line quoted needs": job + '    needs: ["\n      #", rotate]\n',
            "quoted job-level key": job + '    "needs": rotate\n',
            "indentation indicator": job + "    if: |2\n        true\n",
            "unbalanced flow": job + "    env: {A: b\n",
            "misaligned job key": job + "   timeout-minutes: 5\n",
        }
        #: These also spell a second kit reference, so the pin parser rejects the caller too.
        also_unpinned = {"escaped uses"}
        bad["escaped uses"] = job.replace("- run: true",
                                          f'- "u\\x73es": "The-Plum-Team/mod-bas\\x65/.github/workflows/publish.yml@{SHA}"')
        for label, extension in bad.items():
            with self.subTest(label):
                caller.write_text(caller_with(extension))
                kinds = self.kinds()
                self.assertIn((".github/workflows/pages.yml", "extension"), kinds)
                if label not in also_unpinned:
                    self.assertNotIn((".github/workflows/pages.yml", "changed"), kinds)
        caller.write_text(caller_with(job.replace("- run: true", "- uses: The-Plum-Team/mod-base/actions/setup@main")))
        self.assertEqual(self.kinds(), [(".github/workflows/pages.yml", "changed"),
                                        (".github/workflows/pages.yml", "extension")])
        good = job + "    needs: [publish, finalize, request-rotation]\n    if: needs.publish.outputs.eligible == 'true'\n"
        caller.write_text(caller_with(good + QS_EXTENSION))
        self.assertEqual(self.check(), [])
        shell = ("  ext-shell:\n    name: Refresh the collector's baseline\n    runs-on: ubuntu-24.04\n"
                 "    permissions: {contents: read}\n    steps:\n      - name: Don't block the site\n        run: |\n"
                 "          # a shell comment with an 'apostrophe\n          printf '%s\\n' \"$A\" \\\n"
                 "            | tee out.txt\n          : \"${GH_TOKEN:?}\"\n")
        caller.write_text(caller_with(shell))
        self.assertEqual(self.check(), [])

    def test_agents_grammar(self) -> None:
        self.make_clean_mod()
        agents = self.repo / "AGENTS.md"
        cases = {
            "order": "@docs/ai/shared/PUBLIC-EVIDENCE.md\n@docs/ai/shared/REPOSITORY.md\n@docs/ai/PROJECT.md\n",
            "extra line": agents.read_text() + "Read everything.\n",
            "no trailing newline": agents.read_text()[:-1],
            "missing local": "@docs/ai/shared/REPOSITORY.md\n@docs/ai/shared/PUBLIC-EVIDENCE.md\n",
            "blank line": agents.read_text().replace("\n@docs/ai/PROJECT.md", "\n\n@docs/ai/PROJECT.md"),
        }
        for label, text in cases.items():
            with self.subTest(label):
                agents.write_text(text)
                self.assertIn(("AGENTS.md", "agents"), self.kinds())
        agents.write_text("@docs/ai/shared/REPOSITORY.md\n@docs/ai/shared/PUBLIC-EVIDENCE.md\n@docs/ai/PROJECT.md\n")
        (self.repo / "docs/ai/PROJECT.md").unlink()
        self.assertIn(("AGENTS.md", "agents"), self.kinds())

    def test_forbidden_claude_files(self) -> None:
        self.make_clean_mod()
        for relative in tool.FORBIDDEN_PATHS:
            with self.subTest(relative):
                created = write(self.repo, relative, "# rules\n")
                self.assertEqual(self.kinds(), [(relative, "forbidden")])
                created.unlink()
        (self.repo / ".claude").mkdir(exist_ok=True)
        (self.repo / ".claude" / "CLAUDE.md").mkdir()
        self.assertEqual(self.kinds(), [(".claude/CLAUDE.md", "forbidden")])

    def test_fragment_rules(self) -> None:
        self.make_clean_mod()
        cases = {
            ".gitignore": ("out/\n", lambda text: text.replace("_site/\n", "")),
            ".github/CODEOWNERS": (None, lambda text: text.replace("/docs/ai/ @AkaNebur\n", "")),
            ".github/pull_request_template.md": (None, lambda text: text.replace("## AI assistance", "## AI")),
        }
        for path, (_, mutate) in cases.items():
            with self.subTest(path):
                target = self.repo / path
                original = target.read_text()
                target.write_text(mutate(original))
                self.assertEqual(self.kinds(), [(path, "fragment")])
                target.write_text(original)
        codeowners = self.repo / ".github/CODEOWNERS"
        original = codeowners.read_text()
        for label, text in {"owner-less override": original + "/scripts/ci/local.py\n",
                            "placeholder owner": original.replace("@AkaNebur", "@{{owner}}", 1),
                            "invalid owner": original.replace("@AkaNebur", "AkaNebur", 1)}.items():
            with self.subTest(label):
                codeowners.write_text(text)
                self.assertIn((".github/CODEOWNERS", "fragment"), self.kinds())
        codeowners.write_text(original + "\n# comment\n* @AkaNebur @The-Plum-Team/maintainers owner@example.com\n")
        self.assertEqual(self.check(), [])

    def test_dependabot_must_ignore_the_kit_for_every_actions_update(self) -> None:
        self.make_clean_mod()
        dependabot = self.repo / ".github/dependabot.yml"
        original = dependabot.read_text()
        flow = original.replace('    ignore:\n      - dependency-name: "The-Plum-Team/mod-base*"\n',
                                '    ignore: [{dependency-name: "The-Plum-Team/mod-base*"}]\n')
        dependabot.write_text(flow)
        self.assertEqual(self.check(), [])
        second = original + ("  - package-ecosystem: github-actions\n    directory: /.github/claude\n"
                             "    schedule:\n      interval: monthly\n")
        cases = {
            "no ignore": original.replace('    ignore:\n      - dependency-name: "The-Plum-Team/mod-base*"\n', ""),
            "versions only": original.replace('"The-Plum-Team/mod-base*"\n',
                                              '"The-Plum-Team/mod-base*"\n        versions: [">=2"]\n'),
            "other dependency": original.replace("The-Plum-Team/mod-base*", "The-Plum-Team/other*"),
            "second actions update": second,
            "no actions update": "version: 2\nupdates:\n  - package-ecosystem: gradle\n    directory: /\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                dependabot.write_text(text)
                self.assertIn((".github/dependabot.yml", "fragment"), self.kinds())

    def test_link_rule(self) -> None:
        managed = frozenset(tool.SHARED_IMPORTS)
        document = "docs/ai/shared/REPOSITORY.md"
        allowed = ("[a](PUBLIC-EVIDENCE.md) [b](PUBLIC-EVIDENCE.md#rotation) [c](#local) "
                   "[d](https://github.com/The-Plum-Team/mod-base/blob/main/docs/ADAPTER.md) "
                   "<https://example.com/x>\n\n```\n[e](../../../README.md) http://example.com\n```\n"
                   "`[f](../PROJECT.md)`\n[g]: ./PUBLIC-EVIDENCE.md\n[h](<PUBLIC-EVIDENCE.md>)\n")
        self.assertEqual(tool.link_violations(document, allowed, managed), [])
        refused = {
            "mod-local document": "[x](../PROJECT.md)",
            "repository file": "[x](../../../scripts/ci/mod_base_kit.py)",
            "absolute path": "[x](/docs/ai/PROJECT.md)",
            "http": "[x](http://example.com)",
            "bare http": "see http://example.com/path",
            "bare www": "see www.example.com",
            "javascript": "[x](javascript:alert(1))",
            "file scheme": "<file:///etc/passwd>",
            "reference definition": "[x]: ../PROJECT.md",
            "html": '<a href="../PROJECT.md">x</a>',
            "image": "![x](../../images/a.png)",
            "query": "[x](PUBLIC-EVIDENCE.md?raw=1)",
            "uppercase host": "[x](https://EXAMPLE.com/)",
            "angle destination with a space": "[a](<../../../scripts/pages/mod adapter.md>)",
            "unquoted html attribute": "<a href=../../PROJECT.md>x</a>",
            "srcset": '<img srcset="../../images/a.png 2x">',
            "unparseable destination": "[x]( ../PROJECT.md trailing )",
        }
        for label, text in refused.items():
            with self.subTest(label):
                self.assertNotEqual(tool.link_violations(document, text + "\n", managed), [])

    def test_a_managed_doc_link_drift_is_reported(self) -> None:
        self.make_clean_mod()
        write(self.kit, "template/managed/docs/ai/shared/REPOSITORY.md", "See [the project](../PROJECT.md).\n")
        write(self.repo, "docs/ai/shared/REPOSITORY.md", "See [the project](../PROJECT.md).\n")
        self.assertEqual(self.kinds(), [("docs/ai/shared/REPOSITORY.md", "links")])

    def test_deferred_semantics(self) -> None:
        self.make_clean_mod(config_document(deferred=[".gitattributes", "AGENTS.md", ".gitignore"]))
        for relative in (".gitattributes", "AGENTS.md", ".gitignore"):
            (self.repo / relative).unlink(missing_ok=True)
        self.assertEqual(tool.evaluate(self.repo, kit_root=self.kit), ([], []))
        write(self.repo, ".gitignore", "out/\n")
        self.assertEqual(self.check(), [])
        pending = tool.pending(self.repo, kit_root=self.kit)
        self.assertEqual([(drift.path, drift.kind) for drift in pending], [(".gitignore", "fragment")] * 6)
        write(self.repo, ".gitattributes", "*.bat -text\n")
        self.assertEqual(self.kinds(), [(".gitattributes", "changed")])
        (self.repo / ".gitattributes").unlink()
        # Only missing required lines are pending; every other rule of a present deferred file is strict.
        write(self.repo, "AGENTS.md", "@docs/ai/PROJECT.md\n")
        self.assertIn(("AGENTS.md", "agents"), self.kinds())
        self.assertEqual({drift.path for drift in tool.pending(self.repo, kit_root=self.kit)}, {".gitignore"})

    def test_present_deferred_fragments_keep_their_structural_rules(self) -> None:
        deferred = [".github/dependabot.yml", ".github/pull_request_template.md", "AGENTS.md"]
        self.make_clean_mod(config_document(deferred=deferred))
        self.assertEqual(tool.evaluate(self.repo, kit_root=self.kit), ([], []))
        mutations = {
            ".github/dependabot.yml": lambda text: text.replace("The-Plum-Team/mod-base*", "The-Plum-Team/other*"),
            ".github/pull_request_template.md": lambda text: text.replace("## AI assistance", "## AI"),
            "AGENTS.md": lambda text: "@docs/ai/PROJECT.md\n",
        }
        for path, mutate in mutations.items():
            with self.subTest(path):
                target = self.repo / path
                original = target.read_text()
                target.write_text(mutate(original))
                failing, pending = tool.evaluate(self.repo, kit_root=self.kit)
                self.assertTrue(failing and all(drift.path == path for drift in failing), failing)
                self.assertEqual(pending, [])
                target.write_text(original)

    def test_only_non_control_files_can_be_deferred(self) -> None:
        refused = ["CONTRIBUTING.md", "README.md", ".github/workflows/pages.yml", "scripts/ci/mod_base_kit.py",
                   ".github/CODEOWNERS", "docs/ai/shared/REPOSITORY.md"]
        self.make_clean_mod(config_document(deferred=refused))
        self.assertEqual(self.kinds(), [(path, "forbidden") for path in sorted(refused)])
        caller = self.repo / ".github/workflows/pages.yml"
        caller.write_text(caller.read_text().replace("name: Project site", "name: Edited"))
        (self.repo / ".github/CODEOWNERS").write_text("/.github/\n")
        (self.repo / "scripts/ci/mod_base_kit.py").unlink()
        kinds = self.kinds()
        for expected in ((".github/workflows/pages.yml", "changed"), (".github/CODEOWNERS", "fragment"),
                         ("scripts/ci/mod_base_kit.py", "missing")):
            self.assertIn(expected, kinds)
        self.assertEqual(tool.pending(self.repo, kit_root=self.kit), [])
        self.assertEqual(tool.DEFERRABLE, {".gitattributes", ".gitignore", ".github/dependabot.yml",
                                           ".github/pull_request_template.md", "AGENTS.md"})

    def test_block_pops_adoption_stages(self) -> None:
        """SPEC §8.1: PR A defers the root files Block Pops cannot change in a controller upgrade."""

        deferred = [".gitattributes", ".gitignore", ".github/dependabot.yml", ".github/pull_request_template.md",
                    "AGENTS.md"]
        self.make_clean_mod(config_document(deferred=deferred))
        for relative in (".gitattributes", ".github/dependabot.yml", ".github/pull_request_template.md", "AGENTS.md"):
            (self.repo / relative).unlink(missing_ok=True)
        write(self.repo, ".gitignore", BLOCK_POPS_GITIGNORE)
        self.assertEqual(self.check(), [])
        self.assertEqual(sorted(drift.detail for drift in tool.pending(self.repo, kit_root=self.kit)),
                         ["missing the required line '/.architectury-transformer/'",
                          "missing the required line '_site/'", "missing the required line 'public-evidence/'"])
        self.assertEqual(tool.sync(self.repo, kit_root=self.kit, write=True), [])
        self.assertFalse((self.repo / ".gitattributes").exists())
        # PR B adds the files; PR C empties ``deferred`` and everything is checked strictly.
        write(self.repo, ".gitignore", BLOCK_POPS_GITIGNORE + "_site/\npublic-evidence/\n/.architectury-transformer/\n")
        write(self.repo, ".gitattributes", QS_GITATTRIBUTES + EOL_RULES)
        write(self.repo, ".github/dependabot.yml", self.seed(".github/dependabot.yml.tmpl"))
        write(self.repo, ".github/pull_request_template.md", self.seed(".github/pull_request_template.md.tmpl"))
        write(self.repo, "AGENTS.md", "@docs/ai/shared/REPOSITORY.md\n@docs/ai/shared/PUBLIC-EVIDENCE.md\n"
                                      "@docs/ai/PROJECT.md\n")
        self.assertEqual(tool.evaluate(self.repo, kit_root=self.kit), ([], []))
        write(self.repo, "site/mod-base.json", json.dumps(config_document()) + "\n")
        self.assertEqual(tool.evaluate(self.repo, kit_root=self.kit), ([], []))

    def test_check_requires_a_mod_config(self) -> None:
        with self.assertRaises(MbError):
            self.check()


# -- sync -------------------------------------------------------------------------------------------


class SyncTest(TemplateCase):
    def test_dry_run_reports_without_writing(self) -> None:
        self.make_clean_mod()
        target = self.repo / ".gitattributes"
        target.write_text("changed\n")
        drifts = tool.sync(self.repo, kit_root=self.kit, write=False)
        self.assertEqual([(drift.path, drift.kind) for drift in drifts], [(".gitattributes", "changed")])
        self.assertEqual(target.read_text(), "changed\n")

    def test_write_round_trip_preserves_pin_and_extensions(self) -> None:
        self.make_clean_mod()
        caller = self.repo / ".github/workflows/pages.yml"
        extension = QS_EXTENSION
        caller.write_text(caller_with(extension).replace("name: Project site", "name: Edited"))
        (self.repo / "docs/ai/shared/PUBLIC-EVIDENCE.md").write_text("edited\n")
        (self.repo / "scripts/ci/mod_base_kit.py").unlink()
        written = tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual(sorted(drift.path for drift in written),
                         [".github/workflows/pages.yml", "docs/ai/shared/PUBLIC-EVIDENCE.md",
                          "scripts/ci/mod_base_kit.py"])
        self.assertEqual(caller.read_text(), caller_with(extension))
        self.assertEqual(self.check(), [])
        self.assertEqual(tool.sync(self.repo, kit_root=self.kit, write=True), [])
        self.assertEqual(os.stat(self.repo / "scripts/ci/mod_base_kit.py").st_mode & 0o777, 0o644)

    def test_sync_follows_the_single_pin(self) -> None:
        self.make_clean_mod()
        e2e = self.repo / ".github/workflows/e2e.yml"
        caller = self.repo / ".github/workflows/pages.yml"
        e2e.write_text(e2e.read_text().replace(SHA, OTHER_SHA))
        caller.write_text(caller.read_text().replace(SHA, OTHER_SHA))
        self.assertEqual(self.check(), [])
        e2e.write_text(e2e.read_text().replace(OTHER_SHA, SHA))
        self.assertIn((".github/workflows/pages.yml", "changed"), self.kinds())
        with self.assertRaises(MbError):
            tool.sync(self.repo, kit_root=self.kit, write=True)

    def test_sync_refuses_symlinks_and_broken_callers(self) -> None:
        self.make_clean_mod()
        target = self.repo / "docs/ai/shared/REPOSITORY.md"
        target.unlink()
        write(self.root, "outside.md", "x\n")
        target.symlink_to(self.root / "outside.md")
        with self.assertRaises(MbError):
            tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual((self.root / "outside.md").read_text(), "x\n")
        target.unlink()
        caller = self.repo / ".github/workflows/pages.yml"
        caller.write_text(caller.read_text().replace("# <<< mod-local extensions\n", ""))
        with self.assertRaises(MbError):
            tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertFalse(target.exists())

    def test_write_rewrites_a_crlf_checkout_with_lf_and_keeps_the_extension_region(self) -> None:
        self.make_clean_mod()
        caller = self.repo / ".github/workflows/pages.yml"
        document = self.repo / "docs/ai/shared/REPOSITORY.md"
        caller.write_bytes(caller_with(QS_EXTENSION).replace("\n", "\r\n").encode())
        document.write_bytes(document.read_bytes().replace(b"\n", b"\r\n"))
        planned = tool.sync(self.repo, kit_root=self.kit, write=False)
        self.assertEqual(sorted(drift.path for drift in planned),
                         [".github/workflows/pages.yml", "docs/ai/shared/REPOSITORY.md"])
        tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual(caller.read_text(), caller_with(QS_EXTENSION))
        self.assertNotIn(b"\r", document.read_bytes())
        self.assertEqual(self.check(), [])

    def test_a_bump_of_a_crlf_checkout_rewrites_the_pin_and_syncs_with_lf(self) -> None:
        """``bump`` rewrites every pin line (keeping each file's line endings), then runs the new
        kit's ``template sync --write``: a pre-LF-rules ``core.autocrlf=true`` checkout comes out clean."""

        self.make_clean_mod()
        caller = self.repo / ".github/workflows/pages.yml"
        e2e = self.repo / ".github/workflows/e2e.yml"
        caller.write_bytes(caller_with(QS_EXTENSION).replace("\n", "\r\n").encode())
        e2e.write_bytes(e2e.read_bytes().replace(b"\n", b"\r\n"))
        bootstrap = load_bootstrap()
        self.assertEqual(sorted(bootstrap.rewrite_pin(self.repo, bootstrap.Pin(OTHER_SHA, "v1.2.4", ()))),
                         [".github/workflows/e2e.yml", ".github/workflows/pages.yml"])
        tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual(caller.read_text(), caller_with(QS_EXTENSION, sha=OTHER_SHA, version="v1.2.4"))
        self.assertIn(f"@{OTHER_SHA} # v1.2.4\r\n".encode(), e2e.read_bytes(), "a mod workflow keeps its endings")
        self.assertEqual(self.check(), [])

    def test_deferred_absent_files_are_not_created(self) -> None:
        self.make_clean_mod(config_document(deferred=[".gitattributes"]))
        self.assertFalse((self.repo / ".gitattributes").exists())
        self.assertEqual(tool.sync(self.repo, kit_root=self.kit, write=True), [])
        self.assertFalse((self.repo / ".gitattributes").exists())


# -- init -------------------------------------------------------------------------------------------


class InitTest(TemplateCase):
    def setUp(self) -> None:
        super().setUp()
        write(self.kit, "src/mod_base/__init__.py", "")
        git(self.kit, "init", "-q", home=self.home)
        git(self.kit, "add", "-A", home=self.home)
        git(self.kit, "commit", "-q", "-m", "kit", home=self.home)
        git(self.kit, "tag", "v1.2.3", home=self.home)
        self.head = git(self.kit, "rev-parse", "HEAD", home=self.home)
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.config = write(self.root, "config.json", json.dumps(config_document()) + "\n")

    def test_seed_a_new_repository(self) -> None:
        created = tool.init(self.repo, kit_root=self.kit, seed=True, from_config=self.config)
        self.assertEqual(created, [".gitattributes", ".github/workflows/pages.yml", "scripts/ci/mod_base_kit.py",
                                   "docs/ai/shared/REPOSITORY.md", "docs/ai/shared/PUBLIC-EVIDENCE.md", ".gitignore",
                                   ".github/CODEOWNERS", ".github/dependabot.yml", ".github/pull_request_template.md",
                                   "AGENTS.md", "CONTRIBUTING.md", "LICENSE", "site/mod-base.json",
                                   "scripts/pages/mod_base_adapter.py", "docs/ai/PROJECT.md",
                                   "docs/architecture/decisions/README.md"])
        self.assertEqual((self.repo / "site/mod-base.json").read_bytes(), self.config.read_bytes())
        caller = (self.repo / ".github/workflows/pages.yml").read_text()
        self.assertIn(f"publish.yml@{self.head} # v1.2.3", caller)
        self.assertEqual((self.repo / "AGENTS.md").read_text(),
                         "@docs/ai/shared/REPOSITORY.md\n@docs/ai/shared/PUBLIC-EVIDENCE.md\n@docs/ai/PROJECT.md\n")
        license_text = (self.repo / "LICENSE").read_text()
        self.assertTrue(license_text.startswith("Quick Skin — All Rights Reserved\n"))
        self.assertIn("https://modrinth.com/mod/quick-skin", license_text)
        self.assertEqual(tool.unresolved_placeholders(license_text.encode()), ["holder", "third_party", "years"])
        self.assertIn("{{test_command}}", (self.repo / "CONTRIBUTING.md").read_text())
        drifts = tool.check(self.repo, kit_root=self.kit)
        self.assertEqual({(drift.path, drift.kind) for drift in drifts}, {(".github/CODEOWNERS", "fragment")})
        codeowners = self.repo / ".github/CODEOWNERS"
        codeowners.write_text(codeowners.read_text().replace("{{owner}}", "AkaNebur"))
        self.assertEqual(tool.check(self.repo, kit_root=self.kit), [])

    def test_init_never_overwrites(self) -> None:
        write(self.repo, "CONTRIBUTING.md", "ours\n")
        write(self.repo, ".gitignore", "ours\n")
        created = tool.init(self.repo, kit_root=self.kit, seed=True, from_config=self.config)
        self.assertNotIn("CONTRIBUTING.md", created)
        self.assertNotIn(".gitignore", created)
        self.assertEqual((self.repo / "CONTRIBUTING.md").read_text(), "ours\n")
        self.assertEqual(tool.init(self.repo, kit_root=self.kit, seed=True, from_config=self.config), [])

    def test_without_seed_only_checked_files_are_created(self) -> None:
        created = tool.init(self.repo, kit_root=self.kit, seed=False, from_config=self.config)
        self.assertNotIn("CONTRIBUTING.md", created)
        self.assertNotIn("LICENSE", created)
        self.assertIn(".github/workflows/pages.yml", created)

    def test_license_follows_the_label(self) -> None:
        for label, expected in (("LGPL-2.1-only", "GNU Lesser General Public License"), ("MIT", None)):
            with self.subTest(label):
                repo = self.root / f"mod-{label}"
                repo.mkdir()
                document = config_document()
                document["project"]["license_label"] = label
                config = write(self.root, f"{label}.json", json.dumps(document))
                tool.init(repo, kit_root=self.kit, seed=True, from_config=config)
                if expected is None:
                    self.assertFalse((repo / "LICENSE").exists())
                else:
                    self.assertIn(expected, (repo / "LICENSE").read_text())

    def test_an_existing_pin_is_kept_and_an_unpinned_kit_is_refused(self) -> None:
        write(self.repo, ".github/workflows/e2e.yml", f"      - uses: The-Plum-Team/mod-base/actions/setup@{SHA} # v1.2.9\n")
        tool.init(self.repo, kit_root=self.kit, seed=False, from_config=self.config)
        self.assertIn(f"@{SHA} # v1.2.9", (self.repo / ".github/workflows/pages.yml").read_text())
        write(self.kit, "src/mod_base/__init__.py", "dirty = True\n")
        other = self.root / "other"
        other.mkdir()
        with self.assertRaises(MbError):
            tool.init(other, kit_root=self.kit, seed=False, from_config=self.config)
        self.assertEqual(list(other.iterdir()), [])

    def test_init_refuses_a_missing_repository(self) -> None:
        with self.assertRaises(MbError):
            tool.init(self.root / "absent", kit_root=self.kit, seed=True, from_config=self.config)


# -- CLI --------------------------------------------------------------------------------------------


class CommandsTest(TemplateCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("mod_base.runtime.kit_root", return_value=self.kit), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(arguments))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_check_and_sync_exit_codes(self) -> None:
        self.make_clean_mod()
        self.assertEqual(self.run_cli("template", "check", "--repo", str(self.repo))[0], 0)
        (self.repo / ".gitattributes").write_text("x\n")
        code, stdout, stderr = self.run_cli("template", "check", "--repo", str(self.repo))
        self.assertEqual(code, 2)
        self.assertIn("changed: .gitattributes", stdout)
        self.assertEqual(stderr.count("\n"), 1)
        self.assertEqual(self.run_cli("template", "sync", "--repo", str(self.repo))[0], 2)
        code, stdout, _ = self.run_cli("template", "sync", "--repo", str(self.repo), "--write")
        self.assertEqual((code, stdout), (0, "wrote .gitattributes\n"))
        self.assertEqual(self.run_cli("template", "check", "--repo", str(self.repo))[0], 0)

    def test_check_prints_pending_deferred_drift_without_failing(self) -> None:
        self.make_clean_mod(config_document(deferred=[".gitignore"]))
        write(self.repo, ".gitignore", "out/\n")
        code, stdout, stderr = self.run_cli("template", "check", "--repo", str(self.repo))
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout.count("pending (template.deferred): fragment: .gitignore\n"), 6)

    def test_init_lists_placeholders_to_fill(self) -> None:
        write(self.repo, ".github/workflows/e2e.yml", f"      - uses: The-Plum-Team/mod-base/actions/setup@{SHA} # v1.2.3\n")
        config = write(self.root, "config.json", json.dumps(config_document()) + "\n")
        code, stdout, _ = self.run_cli("template", "init", "--repo", str(self.repo), "--seed", "--from-config",
                                       str(config))
        self.assertEqual(code, 0)
        self.assertIn("created LICENSE\n  fill in by hand: {{holder}}, {{third_party}}, {{years}}\n", stdout)
        self.assertIn("created .github/CODEOWNERS\n  fill in by hand: {{owner}}\n", stdout)


if __name__ == "__main__":
    unittest.main()
