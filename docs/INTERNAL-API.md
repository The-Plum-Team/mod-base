# Internal API

Produced by introspecting the frozen MB0 scaffolding (`src/mod_base`); `tests/test_internal_api.py`
fails if anything listed here disappears or changes its parameters. This is the one reference every parallel
unit (MB1-MB10) codes against: **the names, parameters, defaults, return types and exception classes
below are frozen**. A unit fills in the bodies of the modules it owns (every stub body raises
`NotImplementedError("owned by MBn")`); it may add private helpers and new private modules, but
it must not rename, remove or change the signature of anything listed here. A needed change is
announced to MB0's owner first (SPEC §10).

Conventions shared by every entry point:

* Every rejection raises `mod_base.errors.MbError` (exit 2) or a subclass: `Unavailable` (exit 3,
  "no admissible evidence"), `Superseded` (exit 3, reason `superseded`), `ControllerSkew` (exit 78).
  Unit-specific error classes subclass `MbError`. Only `mod_base.errors.run_main` turns them into
  exit codes and one bounded stderr line.
* Entry points receive a `mod_base.runtime.Invocation` (repo root, validated `Config`, kit root and
  a frozen snapshot of the GitHub environment) and never read `os.environ` themselves.
* Documents are plain JSON-compatible dicts validated by `mod_base.model.documents`; written
  documents are `mod_base.model.canonical.canonical_json` bytes (sorted keys, compact separators,
  UTF-8, trailing newline); their SHA-256 is the SHA-256 of those bytes.
* Bounds come only from `mod_base.model.limits`; artifact names only from `mod_base.model.grammar`;
  job and step names only from `mod_base.workflow`.
* Pillow is imported only inside `mod_base.imaging` (lazily, inside functions).
* `mod_base.model.documents` also exports its field validators (`SHA1`, `SHA256`, `DIGEST`, `KEY`,
  `FAMILY`, `LANE_ID`, `BRANCH`, `REPOSITORY`, `IDENT`, `PIXEL_METRICS`, `COMPARE_METRICS`,
  `RUN_CLAIM`, `KIT_REF`, `SUBJECT`, `FILE_RECORD`, `IMAGE_POLICY`, `JARS`, ...): each is a
  `(value, path) -> value` callable raising `DocumentError`; reuse them instead of new regexes.

## Amendments announced by MB0

Changes to the frozen interface after the first MB0 review (every consumer codes against these):

* **Two-phase selection.** `authenticate` writes a selection *draft*
  (`documents.validate_selection(draft=True)`: no `manifest_sha256`, `binding`, `composition`);
  `compact` (or `compose` for a `selected` handoff) completes it and embeds it; published bundles
  satisfy `documents.check_compact_selection`. `manifest_sha256` is
  `documents.compact_identity_sha256` (the manifest without its `selection` member and
  `selection.json` record). `kit_binding.sha` is the selected artifact's kit, never compared with
  the executing `kit`. See `docs/SCHEMAS.md` "The collect flow".
* **CLI:** `compose` takes `--selection F` (the draft); `select` takes an optional `--output F`
  (the `Selected` JSON object, keys `pages.select.SELECTED_KEYS`); `admit` also outputs
  `subjects` and every `families` entry carries `coverage_sha`.
* **Composed flow:** `compose_selected(..., selection_path, ...)`; the core's selected compaction is
  a `validate_compact(intermediate=True)` bundle handed to the `compose` hook.
* **Promotion draft:** `verify_publication` receives `validate_promotion(draft=True)` (no `site`).
* **Runs:** `github.runs.run_record(run, claim, *, require_controller_head=True)`; `False` only for a
  `delegated` tested run.
* **Build/refresh data flow:** `build` downloads this run's collected artifacts itself into the new
  `--collected`/`--families` directories; `refresh` downloads the promotion and the collected
  artifact itself and writes the exact upload bytes into the new `--input` directory; a family
  cache is the collected family artifact's `source/` (`family.paired.SOURCE_DIRECTORY`).
* **Gallery families:** one entry per family with per-key `releases` (`coverage_sha`, `contracts`,
  `links`, `image_policy` per available key) and `key` on every family lane and not-applicable entry.
* **Conformance seams:** `imaging.png.pattern_png` is the `protocol.ImageFactory`;
  `adapter.host_child.run_hook` is the in-process dispatch (with a `FakeGitHub` context);
  `adapter.host.adapter_pythonpath` is the child `PYTHONPATH`; `conformance.run.main` is the
  simulation child; `FakeGitHub` gains `add_commit`, `add_tree`, `add_blob`, `add_ref`, `add_response`.
* **Anchors:** a non-null `anchor_selection` result equals `expectation.anchor`; `validate_anchor`
  with an expectation requires that declared anchor.
* **Text and grammar:** strict JSON refuses lone surrogates; `canonical_json` refuses non-`str` keys;
  display text uses the frozen Unicode 3.2 table; `REPOSITORY` never admits `.`/`..` components.

## Ownership

| Unit | Modules |
|---|---|
| MB0 | `mod_base`, `mod_base.errors`, `mod_base.cli`, `mod_base.runtime`, `mod_base.config`, `mod_base.workflow`, `mod_base.model.grammar`, `mod_base.model.limits`, `mod_base.model.canonical`, `mod_base.model.validators`, `mod_base.model.documents`, `mod_base.adapter.protocol` |
| MB1 | `mod_base.io.secure_json`, `mod_base.io.atomic_directory`, `mod_base.io.content_cache`, `mod_base.io.seal`, `mod_base.io.tree`, `mod_base.io.bounded_zip`, `mod_base.github.api`, `mod_base.github.runs`, `mod_base.github.jobs`, `mod_base.github.artifacts`, `mod_base.github.contents`, `mod_base.github.fake`, `mod_base.github.commands` |
| MB2 | `mod_base.imaging.metrics`, `mod_base.imaging.compare`, `mod_base.imaging.png`, `mod_base.imaging.webp` |
| MB3 | `mod_base.adapter.api`, `mod_base.adapter.host`, `mod_base.adapter.host_child`, `mod_base.evidence.expectation`, `mod_base.evidence.prepare`, `mod_base.evidence.validate`, `mod_base.evidence.compact`, `mod_base.evidence.compose`, `mod_base.evidence.anchor`, `mod_base.evidence.commands` |
| MB4 | `mod_base.family.envelope`, `mod_base.family.paired`, `mod_base.family.commands` |
| MB5 | `mod_base.pages.targets`, `mod_base.pages.admission`, `mod_base.pages.select`, `mod_base.pages.authenticate`, `mod_base.pages.commands_control` |
| MB6 | `mod_base.pages.build`, `mod_base.pages.templating`, `mod_base.pages.refresh`, `mod_base.pages.commands_build` |
| MB7 | `mod_base.pages.rotate`, `mod_base.pages.commands_rotate` |
| MB9 | `mod_base.pin`, `mod_base.pin_commands`, `mod_base.template.tool`, `mod_base.template.commands` |
| MB10 | `mod_base.conformance.run`, `mod_base.conformance.commands` |

## `mod_base`

Owner: MB0 (implemented).

mod-base: the shared public-evidence and Pages publication kit.

Constants:

* `ADAPTER_API = 1`
* `PIXEL_METRICS_VERSION = 1`
* `KIT_REPOSITORY = 'The-Plum-Team/mod-base'`

* `def readable_schema_versions(kind: str) -> frozenset[int]`: Return the schema versions a reader of ``kind`` accepts: the current one and N-1 (>= 1).

## `mod_base.errors`

Owner: MB0 (implemented).

Kit exception hierarchy and the single process-exit mapping used by every entry point.

Constants:

* `MAX_MESSAGE_CHARS = 1000`
* `EXIT_OK = 0`
* `EXIT_INTERNAL = 1`
* `EXIT_REJECTED = 2`
* `EXIT_UNAVAILABLE = 3`
* `EXIT_CONTROLLER_SKEW = 78`
* `EXIT_INTERRUPTED = 130`

* `class MbError(Exception)`: A fail-closed rejection. ``reason`` is a short machine-readable token for outputs.
  * `__init__(self, message: str, *, reason: str | None = None) -> None`
* `class Unavailable(MbError)`: No admissible evidence exists (for example no authenticated artifact); exit 3.
* `class Superseded(Unavailable)`: The evidence is valid but bound to a superseded contract (family drift); exit 3.
* `class ControllerSkew(MbError)`: The executing kit tree does not match the pin in the checked-out mod; exit 78.
* `def single_line(message: object, *, limit: int = 1000) -> str`: Return ``message`` as one printable line of at most ``limit`` characters.
* `def exit_code_for(error: BaseException) -> int`: Map an exception raised by an entry point to its process exit code.
* `def describe(error: BaseException) -> str`: Return the one-line stderr description of ``error`` (without the program prefix).
* `def run_main(entry: Callable[[], int | None], *, program: str = 'mod_base', stderr: TextIO | None = None) -> int`: Run ``entry`` and convert every outcome into an exit code plus at most one stderr line.

## `mod_base.cli`

Owner: MB0 (implemented).

``python3 -P -m mod_base <command>``: the static command registry (SPEC §2.2, §10).

Constants:

* `MAX_OUTPUT_VALUE_CHARS = 65536`

* `class KitArgumentParser(ArgumentParser)`: An ``ArgumentParser`` whose usage errors raise :class:`MbError` (one line, exit 2).
  * `error(self, message: str) -> NoReturn`
* `def typed(check: Callable[[str], Any], description: str) -> Callable[[str], Any]`: Wrap a grammar check as an argparse ``type=`` whose failure is a one-line usage error.
* `def add_repo_config(parser: argparse.ArgumentParser) -> None`: Add ``--repo DIR`` (required) and ``--config FILE`` (default ``<repo>/site/mod-base.json``; an explicit path is relative to the working directory, like ``--repo``).
* `def write_github_output(path: Path | None, values: Mapping[str, str | int | bool]) -> None`: Append ``name=value`` lines to a ``$GITHUB_OUTPUT`` file.
* `def load_group(command: str) -> ModuleType`: Import the module owning ``command`` or raise one clean :class:`MbError`.
* `def build_parser(command: str) -> KitArgumentParser`: A parser holding only ``command``'s group, registered by its owning module.
* `def dispatch(argv: Sequence[str]) -> int`
* `def main(argv: Sequence[str] | None = None) -> int`: Process entry point: every outcome becomes an exit code plus at most one stderr line.
* `def environ() -> Mapping[str, str]`: The process environment (a seam for handlers; entry points use ``Invocation.environ``).

## `mod_base.runtime`

Owner: MB0 (implemented).

The per-process invocation context every command builds once and passes to entry points.

Constants:

* `ENVIRONMENT_NAMES = ('GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_JOB', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_REF', 'GIT...`

* `def kit_root() -> Path`: The root of the executing kit checkout (the directory holding ``src/``, ``site/``...).
* `def kit_ref(sha: str) -> dict[str, str]`: Return the ``KitRef`` of the executing kit at ``sha`` (SPEC §3.0).
* `class Invocation`: Everything an entry point may know about its process. Accessors fail closed when a fact the caller needs is absent instead of inventing a default.
  * fields: `repo_root: Path, config: Config, kit_root: Path, environ: Mapping[str, str] = <factory>, implementation_sha_override: str | None = None`
  * `kit_src` (property) -> `Path`
  * `repository` (property) -> `str`
  * `implementation_sha` (property) -> `str`
  * `kit` (property) -> `dict[str, str]`
  * `github_job` (property) -> `str | None`
  * `token` (property) -> `str | None`
  * `api_url` (property) -> `str`
  * `github_output(self, explicit: Path | None = None) -> Path | None`: The ``$GITHUB_OUTPUT`` file (an explicit ``--github-output`` wins).
* `def snapshot_environment(environ: Mapping[str, str]) -> Mapping[str, str]`: A read-only copy of exactly the environment names the kit consumes.
* `def build_invocation(repo: Path | str, config_path: Path | str | None, environ: Mapping[str, str], *, check_repository: bool = True, implementation_sha: str | None = None, root: Path | None = None) -> Invocation`: Load the config of ``repo`` and bind the environment snapshot into an :class:`Invocation`.

## `mod_base.config`

Owner: MB0 (implemented).

Strict loader and validator for ``site/mod-base.json`` (``mod-base.config`` v1, SPEC §4.1).

Constants:

* `KIND = 'mod-base.config'`
* `DEFAULT_CONFIG_PATH = 'site/mod-base.json'`
* `THEME_KEYS = ('bg', 'surface', 'surface_raised', 'surface_soft', 'text', 'muted', 'line', 'accent', 'accent_strong', 'hi...`

* `def validate_config(document: Any, *, path: str = '$') -> dict[str, Any]`: Validate a decoded ``mod-base.config`` v1 document and return it.
* `class Config`: A validated ``mod-base.config`` v1. ``data`` is the validated document; treat it as read-only.
  * fields: `data: dict[str, Any], sha256: str, path: str`
  * `canonical_branch` (property) -> `str`
  * `project` (property) -> `dict[str, Any]`
  * `adapter` (property) -> `dict[str, Any]`
  * `adapter_timeout_seconds` (property) -> `int`
  * `network_hooks` (property) -> `frozenset[str]`
  * `extension_names` (property) -> `frozenset[str]`
  * `targets` (property) -> `dict[str, Any]`
  * `source` (property) -> `dict[str, Any]`
  * `images` (property) -> `dict[str, Any]`
  * `anchor` (property) -> `dict[str, Any]`
  * `admission` (property) -> `dict[str, Any]`
  * `baseline_archive` (property) -> `dict[str, Any]`
  * `families` (property) -> `list[dict[str, Any]]`
  * `labels` (property) -> `dict[str, Any]`
  * `copy` (property) -> `dict[str, Any]`
  * `theme` (property) -> `dict[str, Any]`
  * `template` (property) -> `dict[str, Any]`
  * `family(self, family_id: str) -> dict[str, Any]`: Return the configured family ``family_id`` or raise :class:`MbError`.
  * `image_policy(self) -> dict[str, Any]`: The ``expectation.image_policy`` every expectation must equal (``config.images`` without ``cross_check_runtime_metrics``).
* `def parse_config(data: bytes, *, path: str = 'site/mod-base.json') -> Config`: Strictly decode and validate config bytes; ``path`` is recorded for messages only.
* `def check_repository(config: Config, repo_root: Path) -> None`: Verify the config's repository facts at the checked-out head (see module docstring).
* `def load_config(repo_root: Path | str, config_path: Path | str | None = None, *, check_repository_facts: bool = True) -> Config`: Read, validate and (by default) repository-check the mod's config.

## `mod_base.workflow`

Owner: MB0 (implemented).

THE single source of the Pages workflow, job and step display names (SPEC §5.9).

Constants:

* `PAGES_WORKFLOW_NAME = 'Project site'`
* `PAGES_WORKFLOW_PATH = '.github/workflows/pages.yml'`
* `PAGES_EVENTS = frozenset({'schedule', 'workflow_dispatch'})`
* `PAGES_CRON = '43 * * * *'`
* `OPERATIONS = ('manual', 'deploy', 'family', 'rotate')`
* `PUBLISH_OPERATIONS = ('recovery', 'manual', 'deploy', 'family')`
* `PUBLICATION_LOCK = 'mod-base-pages-publication'`
* `ROTATION_LOCK = 'mod-base-pages-rotation'`

* `def caller_job_name(key: str) -> str`: Return the bare display name of a caller-owned job, e.g. ``"Deploy GitHub Pages"``.
* `def callee_job_name(workflow: str, job: str, **fields: str) -> str`: Return the callee's own ``name:`` value with its placeholders filled (no caller prefix).
* `def api_job_name(workflow: str, job: str, **fields: str) -> str`: Return the name the jobs API reports for a callee job, e.g. ``"Publish / Collect mc1.20.1"``.
* `def workflow_template_name(workflow: str, job: str) -> str`: Return the callee job ``name:`` exactly as written in its YAML (``${{ matrix.* }}`` form).
* `def step_name(key: str) -> str`
* `def find_job(jobs: Sequence[Mapping[str, Any]], name: str, *, run_attempt: int) -> dict[str, Any]`: Return the single job named exactly ``name`` in ``run_attempt``.

## `mod_base.model.grammar`

Owner: MB0 (implemented).

Identifier grammar and the only builders/parsers of kit artifact names (SPEC §3.0).

Constants:

* `MAX_FAMILY_LENGTH = 32`
* `MAX_LANE_ID_LENGTH = 200`
* `MAX_EXTENSION_NAME_LENGTH = 80`
* `ARTIFACT_PREFIX = 'mb-'`
* `PROMOTION_NAME = 'mb-promotion'`
* `PAGES_ARTIFACT_NAME = 'github-pages'`
* `KIT_STAMP_NAME = 'MOD_BASE_KIT.json'`

* `def is_match(pattern: re.Pattern[str], value: object) -> bool`: Return True when ``value`` is a ``str`` fully matching ``pattern`` (never raises).
* `def require(pattern: re.Pattern[str], value: object, label: str) -> str`: Return ``value`` when it is a ``str`` fully matching ``pattern``; raise MbError otherwise.
* `def is_key(value: object) -> bool`
* `def is_family(value: object) -> bool`
* `def is_lane_id(value: object) -> bool`
* `def is_extension_name(value: object) -> bool`
* `def require_key(value: object, label: str = 'key') -> str`
* `def require_family(value: object, label: str = 'family') -> str`
* `def require_sha1(value: object, label: str = 'commit') -> str`
* `def require_positive_int(value: object, label: str, *, maximum: int = 9223372036854775807) -> int`: Return a positive ``int`` (never ``bool``) no larger than ``maximum``.
* `def is_bundle_path(value: object) -> bool`: True for a canonical bundle-relative POSIX path (no absolute, ``..``, ``.``, ``\`` or NUL).
* `def is_repo_path(value: object) -> bool`: True for a repository-relative path: a bundle path none of whose components is ``.git`` (compared case-insensitively, for case-insensitive filesystems).
* `def parse_timestamp(value: object, label: str = 'timestamp') -> datetime`: Parse a GitHub ``YYYY-MM-DDTHH:MM:SSZ`` timestamp into an aware UTC datetime.
* `class WorkflowRef`: A parsed ``GITHUB_WORKFLOW_REF`` (``owner/repo/.github/workflows/f.yml@refs/heads/b``).
  * fields: `repository: str, path: str, branch: str`
* `def parse_workflow_ref(value: object) -> WorkflowRef`: Parse a branch-scoped workflow ref; tags, pull refs and malformed values are rejected.
* `def workflow_ref(repository: str, path: str, branch: str) -> str`: Build the exact ``GITHUB_WORKFLOW_REF`` value for a branch.
* `def run_url(repository: str, run_id: int) -> str`: Build a run URL. Renderers build every run URL with this; none is copied from a bundle.
* `def short_sha(commit: str, length: int = 12) -> str`
* `class ArtifactName`: A parsed kit artifact name. Fields not carried by ``kind`` are ``None``.
  * fields: `kind: str, name: str, key: str | None = None, family: str | None = None, attempt: int | None = None, commit: str | None = None, run_id: int | None = None, coverage_sha: str | None = None`
* `def handoff_name(key: str, attempt: int) -> str`
* `def anchor_name(key: str, commit: str, run_id: int, attempt: int) -> str`
* `def family_handoff_name(family: str, key: str, attempt: int) -> str`
* `def collected_name(key: str) -> str`
* `def collected_family_name(family: str, key: str) -> str`
* `def cache_name(key: str, coverage_sha: str) -> str`
* `def family_cache_name(family: str, key: str, coverage_sha: str) -> str`
* `def baseline_name(key: str, commit: str, tested_run_id: int) -> str`
* `def baseline_name_regex(key_pattern: str) -> str`: Return the full-match regex source for ``mb-baseline`` names whose key matches ``key_pattern``.
* `def parse_artifact_name(name: object) -> ArtifactName | None`: Parse a kit artifact name, or return ``None`` when it is not exactly a kit name.
* `def require_artifact_name(name: object, kind: str) -> ArtifactName`: Parse ``name`` and require its kind; raise MbError for foreign or malformed names.
* `def is_kit_artifact_name(name: object) -> bool`: True only for names this kit may create, list, download or retire.

## `mod_base.model.limits`

Owner: MB0 (implemented).

Every numeric bound the kit enforces (SPEC §3.0 "Global limits" plus the per-field bounds).

Every bound is a module constant; see the source (all names are frozen).


## `mod_base.model.canonical`

Owner: MB0 (implemented).

The single strict JSON decoder, canonical encoder and bounded file reader of the kit.

* `class StrictJsonError(MbError)`: JSON bytes or the file holding them are not trustworthy.
* `def has_surrogate(text: str) -> bool`: True when ``text`` holds a UTF-16 surrogate code point (U+D800..U+DFFF), which is never valid Unicode text and cannot be encoded as UTF-8.
* `def strict_loads(data: bytes, *, label: str, max_bytes: int) -> Any`: Decode strict JSON ``data`` (see module docstring) or raise :class:`StrictJsonError`.
* `def canonical_json(value: Any) -> bytes`: Return the canonical document bytes of ``value`` (sorted, compact, UTF-8, trailing newline).
* `def sha256_hex(data: bytes) -> str`
* `def canonical_sha256(value: Any) -> str`: SHA-256 of ``canonical_json(value)``: the identity of an embedded JSON object.
* `def read_regular_file(path: Path | str, *, label: str, max_bytes: int, allow_empty: bool = False) -> bytes`: Read one stable regular file without following a final-component symlink.
* `def read_json_file(path: Path | str, *, label: str, max_bytes: int) -> tuple[Any, bytes]`: Read and strictly decode one JSON document; return ``(value, raw_bytes)``.

## `mod_base.model.validators`

Owner: MB0 (implemented).

Small strict validator combinators shared by every document kind, the config and the adapter.

Constants:

* `DISPLAY_CATEGORIES = frozenset({'Ll', 'Lm', 'Lo', 'Lt', 'Lu', 'Mc', 'Me', 'Mn', 'Nd', 'Nl', 'No', 'Pc', 'Pd', 'Pe', 'Pf', 'Pi', ...`

* `class DocumentError(MbError)`: A document failed structural validation at ``path``.
  * `__init__(self, path: str, message: str) -> None`
* `def fail(path: str, message: str) -> DocumentError`
* `def is_evidence_text(value: Any, max_length: int) -> bool`: The QS runtime-evidence rule (V9) generalized to any bounded text field.
* `def is_display_text(value: Any, max_length: int) -> bool`: Config/site copy: trimmed, non-empty, markup-free text of printable characters.
* `def Any_() -> Validator`
* `def Str(pattern: re.Pattern[str] | None = None, *, max_len: int = 200, min_len: int = 1, text: str | None = None, choices: Iterable[str] | None = None) -> Validator`: A string. ``text="evidence"`` applies :func:`is_evidence_text`, ``text="display"`` :func:`is_display_text`; ``pattern`` must fully match; ``choices`` restricts to a set.
* `def Const(expected: Any) -> Validator`
* `def Int(minimum: int, maximum: int) -> Validator`
* `def Num(minimum: float, maximum: float) -> Validator`
* `def Bool() -> Validator`
* `def Null() -> Validator`
* `def Nullable(inner: Validator) -> Validator`
* `def OneOf(*alternatives: Validator) -> Validator`: The first alternative that accepts the value wins; the last error is reported otherwise.
* `def List(item: Validator, *, min_items: int = 0, max_items: int, unique: bool = False, unique_by: Callable[[Any], Any] | None = None, sorted_values: bool = False) -> Validator`: An array of ``item``. ``unique`` rejects equal items; ``unique_by`` rejects equal projections; ``sorted_values`` requires strictly ascending items (implies uniqueness).
* `def Map(key_pattern: re.Pattern[str], item: Validator, *, max_items: int, min_items: int = 0, max_key_len: int = 80) -> Validator`: An object with arbitrary keys matching ``key_pattern`` and values validated by ``item``.
* `def Obj(required: Mapping[str, Validator], optional: Mapping[str, Validator] | None = None) -> Validator`: An object with exactly the ``required`` keys plus any subset of ``optional`` keys.
* `def Size(minimum: int, maximum: int) -> Validator`: A ``[width, height]`` pair of integers.
* `def Region() -> Validator`: A normalized ``[left, top, right, bottom]`` box with ``0<=l<r<=1`` and ``0<=t<b<=1``.
* `def check(condition: bool, path: str, message: str) -> None`: Raise :class:`DocumentError` at ``path`` unless ``condition`` holds.

## `mod_base.model.documents`

Owner: MB0 (implemented).

Strict validators for every document kind of SPEC §3 (and the §4.1 config envelope).

Constants:

* `SELECTION_COMPLETION_FIELDS = ('manifest_sha256', 'binding', 'composition')`
* `GALLERY_FAMILY_RELEASE_AVAILABLE = ('coverage_sha', 'contracts', 'links', 'image_policy')`

* `def is_https_url(value: Any) -> bool`: https, lowercase ``[a-z0-9.-]`` host with a dot, no userinfo/port, printable ASCII only.
* `def run_record(value: Any, path: str) -> dict[str, Any]`: A RunRecord (SPEC §3.0): a RunClaim plus facts read from the run API. ``head_sha`` is the run's API head. A run that is its own controller validates with :func:`own_run_record`; a tested run of ``none``/``attested`` reuse has ``head_sha == controller_sha`` (:func:`validate_selection`); only a ``delegated`` tested run's head is unconstrained (Quick Skin PR reuse tests a merge commit that is not the PR run's head; the reuse is proven by the adapter's ``authenticate_extensions``).
* `def own_run_record(value: Any, path: str) -> dict[str, Any]`: A RunRecord of a run that is its own controller (a handoff or family producer run): ``head_sha == commit == controller_sha`` and ``branch == controller_branch`` (SPEC §4.8).
* `def thumbnail_size(source: tuple[int, int] | list[int], box: tuple[int, int] | list[int]) -> tuple[int, int]`: Return the size Pillow 12.3 ``Image.thumbnail(box)`` produces for an image of ``source`` size.
* `def inventory_sha256(records: list[Mapping[str, Any]]) -> str`: Identity of a file inventory: SHA-256 of ``canonical_json`` of ``[{path, sha256, size}]`` sorted by path. Used for ``promotion.site.inventory_sha256`` and ``build.site_inventory_sha256`` (the published site inventory excludes ``build.json`` itself).
* `def run_claim_from_environment(environ: Mapping[str, str]) -> dict[str, Any]`: Build the handoff ``RunClaim`` of the executing job from GitHub's environment (no API call).
* `def validate_expectation(document: Any, *, image_policy: Mapping[str, Any] | None = None, path: str = '$') -> dict[str, Any]`: ``mod-base.evidence.expectation`` v1 (SPEC §3.1).
* `def validate_handoff(document: Any, *, expectation: Mapping[str, Any] | None = None, allowed_extensions: Collection[str] | None = None, path: str = '$') -> dict[str, Any]`: ``mod-base.evidence.handoff`` v1 (SPEC §3.2).
* `def validate_compact(document: Any, *, expectation: Mapping[str, Any] | None = None, allowed_extensions: Collection[str] | None = None, intermediate: bool = False, path: str = '$') -> dict[str, Any]`: ``mod-base.evidence.compact`` v1 (SPEC §3.3).
* `def validate_anchor(document: Any, *, expectation: Mapping[str, Any] | None = None, path: str = '$') -> dict[str, Any]`: ``mod-base.evidence.anchor`` v1 (SPEC §3.4).
* `def validate_family_envelope(document: Any, *, max_total_bytes: int = 1073741824, path: str = '$') -> dict[str, Any]`: ``mod-base.family.envelope`` v1 (SPEC §3.5).
* `def validate_family_paired(document: Any, *, image_policy: Mapping[str, Any] | None = None, path: str = '$') -> dict[str, Any]`: ``mod-base.family.paired`` v1 (SPEC §3.5), the projection returned by ``family_validate``.
* `def validate_selection(document: Any, *, draft: bool = False, path: str = '$') -> dict[str, Any]`: ``mod-base.selection`` v1 (SPEC §3.6).
* `def compact_identity_sha256(manifest: Mapping[str, Any]) -> str`: The selection-independent identity of a compact manifest: ``canonical_sha256`` of the manifest without its ``selection`` member and without the ``selection.json`` record of ``files``.
* `def check_compact_selection(compact: Mapping[str, Any], selection: Mapping[str, Any], *, path: str = '$') -> None`: Bind a published compact manifest to the final selection embedded as its ``selection.json``.
* `def validate_promotion(document: Any, *, draft: bool = False, path: str = '$') -> dict[str, Any]`: ``mod-base.promotion`` v1 (SPEC §3.7).
* `def validate_build(document: Any, *, path: str = '$') -> dict[str, Any]`: ``mod-base.build`` v1 (SPEC §3.8): ``run_url`` is built from the repository and run id, and ``workflow_ref`` is this repository's ``pages.yml`` on a branch.
* `def validate_site(document: Any, *, path: str = '$') -> dict[str, Any]`: ``mod-base.site`` v1 (SPEC §3.9 ``site-data.json``): unique release keys and families, ``loader_names`` parallel to ``loaders``, ``short_sha`` a prefix of ``subject_commit``.
* `def validate_gallery(document: Any, *, path: str = '$') -> dict[str, Any]`: ``mod-base.gallery`` v1 (SPEC §3.9 ``e2e/gallery-data.json``, with the per-key family amendment documented in ``docs/SCHEMAS.md``).
* `def validate_template_manifest(document: Any, *, path: str = '$') -> dict[str, Any]`: ``mod-base.template-manifest`` v1 (SPEC §8.1): unique paths; ``managed`` sources live under ``managed/`` and carry no markers/lines; ``fragment`` and ``seeded`` sources live under ``seed/``; only ``fragment`` entries may list required ``markers``/``lines``.
* `def validate_kit_stamp(document: Any, *, path: str = '$') -> dict[str, Any]`: ``mod-base.kit-stamp`` v1 (``out/mod-base-kit/MOD_BASE_KIT.json``, SPEC §1.5).
* `def validate_document(document: Any, *, kind: str | None = None, **context: Any) -> dict[str, Any]`: Validate ``document`` as its declared ``kind`` (which must equal ``kind`` when given).
* `def load_document(data: bytes, *, kind: str, label: str | None = None, max_bytes: int | None = None, **context: Any) -> dict[str, Any]`: Strictly decode ``data`` and validate it as ``kind``.

## `mod_base.adapter.protocol`

Owner: MB0 (implemented).

The adapter protocol: hook names, where they may run, and strict request/response schemas.

Constants:

* `ADAPTER_API_WINDOW = frozenset({1})`
* `HOOKS = frozenset({'anchor_selection', 'authenticate_extensions', 'collect', 'compose', 'expectation', 'expected_so...`
* `FIXTURE_HOOKS = frozenset({'synthesize'})`
* `NETWORK_HOOKS = frozenset({'authenticate_extensions', 'compose', 'verify_publication'})`
* `TOKEN_JOBS = frozenset({'admit', 'build', 'collect', 'family'})`
* `PREPARE_EVIDENCE = 'prepare-evidence'`
* `FORBIDDEN_JOBS = frozenset({'deploy', 'finalize', 'notify-pages', 'refresh', 'refresh-family', 'request-rotation', 'rotate',...`
* `CHILD_FLAGS = ('--adapter', '--hook', '--request', '--response')`
* `REQUEST_KIND = 'mod-base.adapter.request'`
* `RESPONSE_KIND = 'mod-base.adapter.response'`
* `JOB_CONCLUSIONS = ('success', 'failure', 'cancelled', 'skipped', 'neutral', 'timed_out', 'action_required', 'stale')`
* `MAX_ERROR_CHARS = 1000`

* `def require_hook(hook: Any) -> str`
* `def require_fixture_hook(hook: Any) -> str`
* `def validate_fixture_arguments(hook: str, arguments: Any) -> dict[str, Any]`: Validate the arguments of a test-only fixture hook (never a Pages hook).
* `def validate_fixture_result(hook: str, result: Any) -> None`: A fixture hook writes files and returns ``None``.
* `def validate_arguments(hook: str, arguments: Any) -> dict[str, Any]`: Validate the per-hook ``arguments`` object of a request.
* `def validate_result(hook: str, result: Any, arguments: Mapping[str, Any]) -> Any`: Validate a hook ``result`` against its strict schema and the request ``arguments``.
* `def validate_request(document: Any) -> dict[str, Any]`: Validate a request envelope (including its per-hook arguments).
* `def validate_response(document: Any, *, hook: str, arguments: Mapping[str, Any]) -> Any`: Validate a response envelope for ``hook`` and return its checked ``result``.
* `class HookUnsupported(MbError)`: The adapter defines no such hook. Callers fail closed wherever the hook is required.
* `class HookFailed(MbError)`: The hook raised; the adapter's refusal is a fail-closed rejection.

## `mod_base.io.secure_json`

Owner: MB1.

Bounded, race-aware, fail-closed JSON readers used at trust boundaries (MB1).

* `class SecureJsonError(MbError)`: JSON bytes or the file containing them are not trustworthy (exit 2).
* `def loads(data: bytes, *, label: str, max_bytes: int) -> Any`: Decode strict JSON: non-empty bytes of at most ``max_bytes``, UTF-8 without BOM, no duplicate object key, no NaN/Infinity. Raises :class:`SecureJsonError` naming ``label``.
* `def read(path: Path, *, label: str, max_bytes: int) -> tuple[Any, bytes]`: Read one stable regular file (``O_NOFOLLOW``, same dev/inode/size before, during and after the read, 1..``max_bytes`` bytes) and return ``(strictly decoded value, raw bytes)``.
* `def read_json(path: Path, *, label: str, max_bytes: int) -> Any`: ``read(...)[0]``: the decoded value only.
* `def require_object(value: Any, *, label: str, required: set[str] | frozenset[str], optional: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]`: Return ``value`` when it is a dict with every ``required`` key and no key outside ``required | optional``; raise :class:`SecureJsonError` listing missing/unknown keys.
* `def canonical_json(value: Any) -> bytes`: Canonical document bytes: sorted keys, ``(",", ":")``, ``ensure_ascii=False``, ``allow_nan=False``, UTF-8, trailing newline (== ``model.canonical.canonical_json``).

## `mod_base.io.atomic_directory`

Owner: MB1.

Descriptor-bound exclusive directory publication (MB1).

* `class AtomicDirectoryError(MbError)`: An owned output cannot be safely written or published (exit 2).
* `def atomic_directory(output: Path, writer: Callable[[Path, int], T]) -> T`: Create ``output`` atomically: call ``writer(stage_path, stage_fd)`` and publish the stage.
* `def write_new(stage: int, relative: str, data: bytes) -> None`: Create ``relative`` (canonical POSIX path, parents created ``0700``) under the stage descriptor with ``O_CREAT|O_EXCL|O_NOFOLLOW``, write ``data``, fsync, and chmod ``0644``.

## `mod_base.io.content_cache`

Owner: MB1.

Bounded in-process memo for work that is a pure function of exact input bytes (MB1).

* `class ContentCache`: An LRU memo bounded by ``entries`` and optionally by ``max_bytes``.
  * `__init__(self, *, entries: int, max_bytes: int | None = None) -> None`
  * `stored_bytes` (property) -> `int`
  * `clear(self) -> None`
  * `get_or_compute(self, data: bytes, parameters: tuple[Hashable, ...], compute: Callable[[], T], *, size: Callable[[T], int] = lambda _value: 0) -> T`: Return ``compute()`` for these exact bytes and parameters, reusing an earlier success.

## `mod_base.io.seal`

Owner: MB1.

Seal a generated output tree against its written bytes (MB1).

* `class SealError(MbError)`: The generated tree differs from what was written, or changed while being sealed (exit 2).
* `def seal_output(stage_fd: int, expected: Mapping[str, bytes], *, max_files: int = 8192, max_bytes: int = 1073741824, rechecks: list[Callable[[], None]] | None = None) -> tuple[int, int]`: Verify the stage behind ``stage_fd`` holds exactly ``expected`` (relative path -> bytes).

## `mod_base.io.tree`

Owner: MB1.

Bounded regular-file walks and descriptor-relative child reads (MB1).

* `class TreeError(MbError)`: A directory tree is not a bounded tree of regular files (exit 2).
* `def regular_files(root: Path, *, max_files: int, max_total_bytes: int, max_file_bytes: int, suffixes: Collection[str] | None = None) -> dict[str, int]`: Return ``{relative POSIX path: size}`` for every file under ``root`` (sorted by path).
* `def reject_symlinks(root: Path) -> None`: Raise :class:`TreeError` if any entry at or under ``root`` is a symlink or special file.
* `def read_child_file(root: Path, relative: str, *, max_bytes: int) -> bytes`: Read ``root/relative`` walking every component through ``O_NOFOLLOW`` directory descriptors (no symlink anywhere), stat-stable, 1..``max_bytes`` bytes.
* `def sha256_file(path: Path, *, max_bytes: int) -> str`: SHA-256 hex of one stable regular file of at most ``max_bytes`` (streamed, ``O_NOFOLLOW``).
* `def file_records(root: Path, *, exclude: Collection[str] = (), max_files: int, max_total_bytes: int, max_file_bytes: int) -> list[dict[str, Any]]`: Return the exact inventory ``[{path, sha256, size}]`` of ``root`` sorted by path, leaving out the relative paths in ``exclude`` (for example ``manifest.json``).

## `mod_base.io.bounded_zip`

Owner: MB1.

Bounded ZIP extraction (MB1).

* `class ZipRejected(MbError)`: An archive violates the extraction policy (exit 2).
* `class ExtractionLimits`: Bounds for one archive. ``suffixes`` (when set) restricts every file name's extension.
  * fields: `max_entries: int, max_total_bytes: int, max_entry_bytes: int, max_ratio: int = 200, suffixes: frozenset[str] | None = None`
* `def extract(archive: Path | bytes, destination: Path, limits_: ExtractionLimits) -> list[str]`: Validate and extract ``archive`` into the new directory ``destination``.

## `mod_base.github.api`

Owner: MB1.

The kit's only GitHub REST client (MB1).

Constants:

* `DEFAULT_BASE_URL = 'https://api.github.com'`
* `API_VERSION = '2022-11-28'`
* `USER_AGENT = 'mod-base'`
* `REQUEST_ATTEMPTS = 4`
* `RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})`
* `MAX_RETRY_DELAY_SECONDS = 30.0`
* `REQUEST_TIMEOUT_SECONDS = 30`
* `PER_PAGE = 100`
* `MAX_RESPONSE_BYTES = 33554432`

* `class ApiError(MbError)`: A GitHub request failed after its retry budget. ``status`` is 0 for transport errors.
  * `__init__(self, message: str, *, status: int, method: str, path: str) -> None`
* `class ApiNotFound(ApiError)`: HTTP 404 (never retried).
* `class ApiRateLimited(ApiError)`: The retry budget ended on a rate-limit response.
* `class ReadOnlyViolation(MbError)`: A non-GET request was attempted on a read-only client.
* `class RequestBudgetExhausted(MbError)`: The client's ``max_requests`` budget is spent (for example Pages' 160 reads).
* `class GitHubApi`: A repository-scoped client. ``repository`` is ``owner/name``; paths passed to methods are absolute API paths starting with ``/`` (for example ``/repos/o/r/actions/runs/1``).
  * `__init__(self, *, repository: str, token: str | None, base_url: str = 'https://api.github.com', writable: bool = False, max_requests: int | None = None, sleep: Callable[[float], None] = sleep) -> None`
  * `repository` (property) -> `str`
  * `writable` (property) -> `bool`
  * `request_count` (property) -> `int`
  * `get_json(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Any`: GET ``path`` and return strictly decoded JSON (``model.canonical.strict_loads``).
  * `paginate(self, path: str, *, field: str | None, params: Mapping[str, str | int] | None = None, max_items: int) -> list[dict[str, Any]]`: GET every page (``per_page=100``) and return the concatenated ``field`` arrays (or the top-level arrays when ``field`` is None). More than ``max_items`` rows, a non-object row or a ``total_count`` that disagrees with the rows raises :class:`ApiError`.
  * `post_json(self, path: str, payload: Mapping[str, Any]) -> Any`: POST canonical JSON; requires ``writable``. Returns decoded JSON or None for 204.
  * `delete(self, path: str) -> None`: DELETE ``path``; requires ``writable``. 204 is success; anything else raises.
  * `download(self, path: str, *, max_bytes: int) -> bytes`: GET a binary endpoint (artifact ZIP) that answers with one redirect: the redirect is followed exactly once to an https URL with the ``Authorization`` header stripped; the body is read to at most ``max_bytes``.
  * `rate_limit_snapshot(self) -> dict[str, int]`: ``GET /rate_limit`` projected to numeric ``core`` counters: ``limit``, ``used``, ``remaining``, ``reset`` (the ``budget`` command; never tokens or headers).
* `def from_environment(environ: Mapping[str, str], *, writable: bool = False, max_requests: int | None = None) -> GitHubApi`: Build a client from ``GITHUB_REPOSITORY``, ``GH_TOKEN``/``GITHUB_TOKEN`` and ``GITHUB_API_URL`` (default :data:`DEFAULT_BASE_URL`); a missing token or repository raises.

## `mod_base.github.runs`

Owner: MB1.

Workflow-run reads and exact run validation (MB1).

* `def get_run(api: GitHubApi, run_id: int) -> dict[str, Any]`: ``GET /repos/{repo}/actions/runs/{run_id}`` (an object or :class:`ApiError`).
* `def get_run_attempt(api: GitHubApi, run_id: int, run_attempt: int) -> dict[str, Any]`: The historical attempt endpoint; its ``run_attempt`` must equal ``run_attempt``.
* `def validate_run(run: Mapping[str, Any], *, repository: str, workflow_path: str, events: Collection[str], head_branch: str | None = None, head_sha: str | None = None, workflow_id: int | None = None, require_success: bool = True, display_title: str | None = None) -> None`: Require exact provenance: ``path``, ``event`` in ``events``, ``head_repository.full_name``, and (when given) ``head_branch``, ``head_sha``, ``workflow_id``, ``display_title``; with ``require_success`` also ``status == "completed"`` and ``conclusion == "success"``. Raises :class:`mod_base.errors.MbError` on any difference.
* `def run_order(run: Mapping[str, Any]) -> tuple[datetime, int, int]`: ``(created_at, id, run_attempt)`` after strict shape validation (a total dispatch order).
* `def referenced_kit_sha(run: Mapping[str, Any], *, kit_repository: str = 'The-Plum-Team/mod-base') -> str`: The single kit SHA a run resolved: every ``referenced_workflows[]`` entry whose ``path`` starts with ``<kit_repository>/.github/workflows/`` must end in ``@<sha>`` equal to its ``sha``, and exactly one distinct 40-hex SHA must result (SPEC §1.2 step 3).
* `def workflow_runs(api: GitHubApi, workflow_path: str, *, branch: str | None = None, head_sha: str | None = None, event: str | None = None, status: str | None = None, max_items: int = 1000) -> list[dict[str, Any]]`: List runs of ``workflow_path`` (by file name) newest first with the given filters; the response ``total_count`` must equal the listed rows when it is at most ``max_items``.
* `def wait_for_completion(api: GitHubApi, run_id: int, *, attempts: int = 30, interval: float = 2.0, sleep: Callable[[float], None] = sleep) -> dict[str, Any]`: Poll a run until ``status == "completed"`` (at most ``attempts`` reads); raise otherwise.
* `def run_record(run: Mapping[str, Any], claim: Mapping[str, Any], *, require_controller_head: bool = True) -> dict[str, Any]`: Build the ``RunRecord`` (SPEC §3.0) for an authenticated API ``run`` and its ``RunClaim``: the claim's run id/attempt/workflow path must equal the run's; ``head_sha``, ``event``, ``created_at``, ``conclusion`` (``success``) and ``display_title`` come from the run. The result validates as ``documents.run_record``.

## `mod_base.github.jobs`

Owner: MB1.

Exact job lookup, attempt-scoped listing and generic job-graph hashing (MB1).

* `def attempt_jobs(api: GitHubApi, run_id: int, run_attempt: int) -> list[dict[str, Any]]`: Every job of exactly one attempt (``/runs/{id}/attempts/{n}/jobs``, paginated, bounded by ``limits.MAX_JOBS_PER_ATTEMPT``); job ids must be unique and each job's ``run_attempt`` equal.
* `def job_graph(jobs: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]`: The canonical ``[{name, conclusion}]`` of ``jobs`` sorted by name (names must be unique).
* `def job_graph_sha256(graph: Sequence[Mapping[str, str]]) -> str`: SHA-256 of ``canonical_json(graph)``.
* `def require_job_graph(jobs: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]]) -> str`: Require the observed graph to equal ``expected`` exactly (adapter ``expected_source_jobs``) and return its SHA-256; any missing, extra or differently concluded job raises.
* `def step_window(job: Mapping[str, Any], step_name: str) -> tuple[datetime, datetime]`: ``(started_at, completed_at)`` of the single step named exactly ``step_name`` in ``job``.
* `def require_successful_step(job: Mapping[str, Any], step_name: str) -> dict[str, Any]`: The single step named exactly ``step_name``, which must be ``completed``/``success``.

## `mod_base.github.artifacts`

Owner: MB1.

Artifact model, bounded listings, verified download and exact-ID deletion (MB1).

* `class Artifact`: One Actions artifact as reported by the API (``workflow_run`` flattened).
  * fields: `id: int, name: str, size: int, expired: bool, created_at: str, digest: str, run_id: int, head_branch: str, head_sha: str`
  * `order` (property) -> `tuple[datetime, int]`
  * `@classmethod parse(cls, raw: Any) -> 'Artifact'`
* `def get_artifact(api: GitHubApi, artifact_id: int) -> Artifact`
* `def list_named(api: GitHubApi, name: str, *, max_items: int = 512) -> list[Artifact]`: Artifacts with exactly ``name`` (``?name=``), every row re-checked to carry that name.
* `def list_for_run(api: GitHubApi, run_id: int, *, max_items: int = 512) -> list[Artifact]`: Artifacts of one run; every row's ``run_id`` must equal ``run_id``.
* `def list_repository(api: GitHubApi, *, max_items: int) -> list[Artifact]`: Every repository artifact, newest first, bounded by ``max_items`` (fail closed beyond).
* `def download(api: GitHubApi, *, artifact_id: int, name: str, digest: str, size: int, run_id: int, output: Path, extraction: ExtractionLimits) -> list[str]`: Download artifact ``artifact_id`` into the new directory ``output`` after re-reading its metadata and requiring exactly ``name``, ``digest``, ``size``, owner ``run_id`` and not expired; the ZIP bytes must hash to ``digest``. Returns the extracted relative paths.
* `def delete(api: GitHubApi, artifact_id: int) -> None`: ``DELETE`` one artifact by id (writable client only; 404 raises, never "already gone").

## `mod_base.github.contents`

Owner: MB1.

Repository contents, Git objects and reachability reads (MB1).

* `def default_branch(api: GitHubApi) -> str`: ``GET /repos/{repo}`` ``.default_branch`` (validated branch grammar).
* `def branch_head(api: GitHubApi, branch: str) -> tuple[str, str]`: ``(commit, tree)`` of the live head of ``branch``.
* `def commit_tree(api: GitHubApi, commit: str) -> str`: The tree SHA of ``commit`` (``/git/commits/{sha}``).
* `def file_at(api: GitHubApi, path: str, ref: str, *, max_bytes: int) -> bytes`: Bytes of ``path`` at commit ``ref`` (contents API, base64 decoded strictly, size and Git blob SHA re-verified).
* `def tree(api: GitHubApi, sha: str, *, recursive: bool = True) -> list[dict[str, Any]]`: Entries of a Git tree; ``truncated`` must be false.
* `def blob(api: GitHubApi, oid: str, *, max_bytes: int) -> bytes`: A Git blob by id, base64-decoded, with its Git object id recomputed and compared.
* `def compare(api: GitHubApi, base: str, head: str, *, repository: str | None = None) -> dict[str, Any]`: ``GET /repos/{repository or api.repository}/compare/{base}...{head}`` projected to ``{status, ahead_by, behind_by}`` with validated types.

## `mod_base.github.fake`

Owner: MB1.

An in-memory GitHub for tests and ``conformance`` (MB1).

* `class FakeGitHub`: Duck-typed stand-in for ``GitHubApi``; seed it, then pass it where a client is expected.
  * `__init__(self, *, repository: str, default_branch: str = 'master', writable: bool = False, max_requests: int | None = None) -> None`
  * `set_branch(self, name: str, commit: str, tree: str) -> None`
  * `add_run(self, run: Mapping[str, Any], *, attempts: list[Mapping[str, Any]] | None = None) -> None`
  * `add_jobs(self, run_id: int, run_attempt: int, jobs: list[Mapping[str, Any]]) -> None`
  * `add_artifact(self, record: Mapping[str, Any], archive: bytes) -> None`
  * `add_file(self, ref: str, path: str, data: bytes) -> None`
  * `add_compare(self, base: str, head: str, result: Mapping[str, Any], *, repository: str | None = None) -> None`
  * `add_commit(self, sha: str, tree: str, *, parents: Sequence[str] = (), repository: str | None = None) -> None`: Seed ``/git/commits/{sha}`` (``contents.commit_tree``) with its tree and parents.
  * `add_tree(self, sha: str, entries: Sequence[Mapping[str, Any]], *, truncated: bool = False, repository: str | None = None) -> None`: Seed ``/git/trees/{sha}`` (``contents.tree``, ``tools/verify_action_tree.py``): ``entries`` are the API's ``{path, mode, type, sha, size?}`` rows; ``truncated`` seeds a truncated listing, which consumers must refuse.
  * `add_blob(self, data: bytes, *, oid: str | None = None, repository: str | None = None) -> str`: Seed ``/git/blobs/{oid}`` (``contents.blob``) and return its oid: the Git blob id of ``data`` unless ``oid`` forces another one (a corrupt object the reader must reject).
  * `add_ref(self, ref: str, sha: str, *, annotated_tag_sha: str | None = None, repository: str | None = None) -> None`: Seed ``/git/ref/{ref}`` (``ref`` like ``tags/v1.0.0`` or ``heads/main``). With ``annotated_tag_sha`` the ref points at that tag object, which peels to ``sha`` through ``/git/tags/{annotated_tag_sha}`` (``pin.verify``'s tag peel).
  * `add_response(self, path: str, payload: Any, *, params: Mapping[str, str | int] | None = None) -> None`: Seed the exact JSON body of one GET ``path`` (with exactly ``params``) that no typed seeder covers; a request for an unseeded path answers 404 like the API.
  * `repository` (property) -> `str`
  * `writable` (property) -> `bool`
  * `request_count` (property) -> `int`
  * `deleted_artifact_ids` (property) -> `list[int]`
  * `get_json(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Any`
  * `paginate(self, path: str, *, field: str | None, params: Mapping[str, str | int] | None = None, max_items: int) -> list[dict[str, Any]]`
  * `post_json(self, path: str, payload: Mapping[str, Any]) -> Any`
  * `delete(self, path: str) -> None`
  * `download(self, path: str, *, max_bytes: int) -> bytes`
  * `rate_limit_snapshot(self) -> dict[str, int]`

## `mod_base.github.commands`

Owner: MB1 (register() implemented by MB0; handlers dispatch to the entry points).

``download`` and ``budget`` (MB1). Flags are frozen by SPEC §2.2.

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_download(args: argparse.Namespace) -> int`
* `def run_budget(args: argparse.Namespace) -> int`

## `mod_base.imaging.metrics`

Owner: MB2.

The 8-key PixelMetrics inspection (MB2), ``pixel_metrics_version`` 1.

Constants:

* `SAMPLE_SIZE = (160, 90)`
* `MIN_LUMA_ENTROPY = 0.75`
* `MIN_CHANNEL_STDDEV = 2.0`
* `MIN_MEANINGFUL_COLORS = 4`
* `MAX_DARK_FRACTION = 0.98`
* `MAX_LIGHT_FRACTION = 0.995`
* `QUANTIZE_COLORS = 32`
* `MAX_IMAGE_PIXELS = 20000000`
* `METRIC_KEYS = ('width', 'height', 'file_sha256', 'pixel_sha256', 'luma_entropy', 'meaningful_colors', 'dark_fraction', 'l...`

* `class ImageError(MbError)`: An image is corrupt, of the wrong format or size, blank, or differs from its record.
* `class SizePolicy`: ``exact``: dimensions must equal ``(width, height)``; ``minimum``: at least that size.
  * fields: `mode: str, width: int, height: int`
  * `@classmethod exact(cls, width: int, height: int) -> 'SizePolicy'`
  * `@classmethod minimum(cls, width: int, height: int) -> 'SizePolicy'`
  * `allows(self, width: int, height: int) -> bool`: True when an image of ``width`` x ``height`` satisfies this policy and the pixel bound.
* `def inspect_png(source: Path | bytes, policy: SizePolicy) -> dict[str, Any]`: Decode a PNG (format must be ``PNG``) and return its PixelMetrics; raise :class:`ImageError` when it is not a PNG, violates ``policy`` or is effectively blank.
* `def inspect_webp(source: Path | bytes, policy: SizePolicy) -> dict[str, Any]`: The same inspection for a WebP derivative (format must be ``WEBP``).

## `mod_base.imaging.compare`

Owner: MB2.

Pairwise screenshot comparison (MB2): ``compare_screenshots``, AST-identical in both mods.

Constants:

* `CHANGED_LUMA_THRESHOLD = 8`

* `def compare(first: Path | bytes, second: Path | bytes, *, minimum_changed_fraction: float, region: Sequence[float] | None = None) -> dict[str, Any]`: Return the CompareMetrics of ``first`` -> ``second`` or raise ``ImageError``.

## `mod_base.imaging.png`

Owner: MB2.

Canonical metadata-free RGB PNG re-encoding (MB2).

* `def canonical_png(source: Path | bytes, *, policy: SizePolicy | None = None) -> bytes`: Return the canonical PNG bytes of ``source`` (a PNG); ``policy`` optionally gates its size. Raises :class:`mod_base.imaging.metrics.ImageError` for anything that is not a valid PNG.
* `def pattern_png(width: int, height: int, seed: int = 0) -> bytes`: Deterministic RGB PNG bytes of ``width`` x ``height`` (at least 8x4) that pass the 8-metric blank checks at every size, for synthetic evidence (``protocol.ImageFactory``).

## `mod_base.imaging.webp`

Owner: MB2.

Deterministic WebP derivatives (MB2).

* `def derive_webp(source: Path | bytes, *, box: Sequence[int], quality: int, method: int) -> bytes`: Return the WebP derivative bytes of the PNG ``source`` (see module docstring).

## `mod_base.adapter.api`

Owner: MB3.

The ``ctx`` object every adapter hook receives (MB3), constructed by the child process.

* `class RuntimeTree`: A bounded, regular-file-only, symlink-refusing view of a runtime directory. Paths are canonical relative POSIX paths; every read is bounded and stat-stable.
  * `__init__(self, root: Path, *, max_files: int, max_total_bytes: int) -> None`
  * `root` (property) -> `Path`
  * `files(self) -> list[str]`: Every regular file, sorted (the walk enforces the bounds).
  * `exists(self, relative: str) -> bool`
  * `read_bytes(self, relative: str, *, max_bytes: int) -> bytes`
  * `read_json(self, relative: str, *, max_bytes: int) -> Any`: Strict JSON (duplicate keys and non-finite numbers refused).
  * `path(self, relative: str) -> Path`: The absolute path of an existing regular file (for image inspection).
* `class Context`: Passed as ``ctx`` to every hook.
  * fields: `repo_root: Path, config: Config, tmpdir: Path, implementation_sha: str, api: GitHubApi | None = None`
  * `read_blob(self, commit: str, path: str, max_bytes: int) -> bytes`: Bytes of ``path`` at ``commit`` from the local object store (``git -C repo_root cat-file`` with a sanitized environment); the object must already be present (fetched as an inert object) and be a blob no larger than ``max_bytes``. Never checks anything out.
  * `runtime_tree(self, root: str | Path) -> RuntimeTree`: A :class:`RuntimeTree` over ``root`` with the kit's runtime bounds.
  * `image_metrics(self, path: str | Path, size_policy: SizePolicy | tuple[str, int, int]) -> dict[str, Any]`: The kit's PixelMetrics for a PNG (``imaging.metrics.inspect_png``).

## `mod_base.adapter.host`

Owner: MB3.

Parent side of the adapter call (MB3, SPEC §1.9).

Constants:

* `CHILD_MODULE = 'mod_base.adapter.host_child'`
* `BASE_PATH = '/usr/bin:/bin'`

* `def adapter_pythonpath(invocation: Invocation) -> str`: The child's ``PYTHONPATH``: ``<kit>/src`` then each ``config.adapter.python_path`` entry resolved inside the repository (no ``..``, no symlink component), joined with ``:``.
* `def child_environment(invocation: Invocation, hook: str, *, tmpdir: Path) -> dict[str, str]`: The exact ``env -i`` environment for ``hook`` (see module docstring); the token appears only for a declared network hook in a token job.
* `def child_argv(invocation: Invocation, hook: str, *, request: Path, response: Path) -> list[str]`: The exact child argv: ``[python3, -P, -m, host_child, --adapter, A, --hook, H, --request, R, --response, S]``.
* `def call(invocation: Invocation, hook: str, arguments: Mapping[str, Any], *, network: bool = False) -> Any`: Run ``hook`` with ``arguments`` in the isolated child and return its validated result.

## `mod_base.adapter.host_child`

Owner: MB3.

Child side of the adapter call (MB3): ``python3 -P -m mod_base.adapter.host_child``.

* `def load_adapter(path: Path) -> ModuleType`: Import the adapter file as an isolated module (never added to ``sys.modules`` under a package name that other code could import).
* `def run_hook(context: Context, adapter: ModuleType, hook: str, arguments: Mapping[str, Any], *, image_factory: ImageFactory | None = None) -> Any`: Dispatch one hook in the current process and return its validated result.
* `def main(argv: Sequence[str] | None = None) -> int`

## `mod_base.evidence.expectation`

Owner: MB3.

Run the adapter's ``targets``/``expectation`` hooks, validate, canonicalize and hash (MB3).

* `def target_for_key(invocation: Invocation, key: str, *, subject: Mapping[str, str]) -> dict[str, Any]`: Call ``targets`` and return the target of ``key`` whose subject equals ``subject`` (``{branch, commit, tree}``). ``default-branch`` mode passes ``branches=None``; ``enrolled-branches`` mode passes exactly ``[{name, commit, tree}]`` of the subject. A missing key or a different subject raises :class:`mod_base.errors.Unavailable`.
* `def read_extensions(invocation: Invocation, path: Path | None) -> dict[str, dict[str, Any]]`: Strictly read an ``extensions.json`` (``{name: object}``, at most 1 MiB, every name declared in ``config.adapter.extensions``); ``None`` gives ``{}``.
* `def derive_expectation(invocation: Invocation, *, target: Mapping[str, Any], tested_run: Mapping[str, Any] | None, extensions: Mapping[str, Any]) -> dict[str, Any]`: Call ``expectation`` and return the validated document (``documents.validate_expectation`` with ``image_policy=config.image_policy()``).
* `def expectation_bytes(expectation: Mapping[str, Any]) -> bytes`: ``canonical_json(expectation)``: the exact bytes of ``expectation.json``.
* `def require_rederived(invocation: Invocation, embedded: bytes, *, target: Mapping[str, Any], tested_run: Mapping[str, Any] | None, extensions: Mapping[str, Any]) -> dict[str, Any]`: R2: re-derive the expectation and require its canonical bytes to equal ``embedded``.
* `def run_expect(invocation: Invocation, *, key: str, tested_run_json: Path | None, extensions: Path | None, output: Path) -> dict[str, Any]`: The ``expect`` command: derive the expectation of ``key`` at the checked-out head and write ``output`` (canonical JSON, new file).

## `mod_base.evidence.prepare`

Owner: MB3.

The producer (MB3): Quick Skin ``evidence.prepare`` + Block Pops ``evidence.curate``.

* `class PrepareResult`
  * fields: `manifest: dict[str, Any], output: Path, anchor_eligible: bool`
* `def prepare_handoff(invocation: Invocation, *, e2e_root: Path, key: str, output: Path, subject: Mapping[str, str], tested: Mapping[str, Any], handoff: Mapping[str, Any], extensions_path: Path | None = None, anchor: str = 'auto', anchor_output: Path | None = None) -> PrepareResult`: Produce the handoff bundle for ``key`` in the new directory ``output``.

## `mod_base.evidence.validate`

Owner: MB3.

Handoff/compact validation in a fresh process (MB3), including BP ``validate_raw``.

Constants:

* `KINDS = ('handoff', 'compact', 'anchor', 'family')`

* `def validate_handoff_dir(invocation: Invocation, root: Path, *, key: str, expected_subject_commit: str | None = None, rederive: bool = True) -> dict[str, Any]`: Validate a handoff bundle directory and return its manifest (exit 2 on any defect).
* `def validate_compact_dir(invocation: Invocation, root: Path, *, key: str, bind_raw: Path | None = None, expected_subject_commit: str | None = None) -> dict[str, Any]`: Validate a published compact bundle (``complete`` or ``composed``) including its embedded final selection (``documents.validate_selection`` and ``documents.check_compact_selection``); with ``bind_raw`` (the raw handoff directory) re-encode every derivative from the raw PNGs and require byte-identical WebP (BP ``_bind_compact``).
* `def validate_bundle(invocation: Invocation, kind: str, root: Path, *, key: str, bind_raw: Path | None = None, expected_subject_commit: str | None = None) -> dict[str, Any]`: The ``validate`` command: dispatch on ``kind`` (``handoff``, ``compact``, ``anchor`` or ``family``, the latter validating only the envelope via :mod:`mod_base.family.envelope`).

## `mod_base.evidence.compact`

Owner: MB3.

Handoff -> compact bundle (MB3), with the re-encode binding (BP ``authenticate_source._bind_compact``).

* `def compact_bundle(invocation: Invocation, *, key: str, input_dir: Path, selection_path: Path, output: Path) -> dict[str, Any]`: Write the compact bundle of ``input_dir`` (a complete handoff or a cache) into the new ``output`` with the completed selection embedded (see module docstring) and return its validated manifest.

## `mod_base.evidence.compose`

Owner: MB3.

Composition of ``selected`` evidence with its authenticated baseline (MB3, rule R3).

* `def compose_selected(invocation: Invocation, *, api: GitHubApi, key: str, selected_dir: Path, selection_path: Path, output: Path) -> dict[str, Any]`: The ``compose`` command (``--selected DIR --selection F --output DIR``): steps 1-5 of the module docstring; ``selection_path`` is the draft from ``authenticate``. Returns the composed compact manifest written to ``output`` (a new directory).
* `def verify_composition(invocation: Invocation, *, composed_dir: Path, selected_dir: Path, baseline_dir: Path, expectation: dict[str, Any]) -> None`: R3 core re-verification (step 4 of the module docstring); ``selected_dir`` is the intermediate selected compaction. Raises :class:`mod_base.errors.MbError`.

## `mod_base.evidence.anchor`

Owner: MB3.

The lossless anchor (MB3): Block Pops ``scripts/pages/visual_anchor.py`` generalized.

* `class AnchorIdentity`
  * fields: `eligible: bool, name: str | None, artifact_nodes: tuple[str, ...]`
* `def artifact_name(key: str, commit: str, run_id: int, run_attempt: int) -> str`: ``mb-anchor--{key}--{commit}--{run_id}--a{attempt}`` (delegates to the grammar).
* `def anchor_identity(invocation: Invocation, handoff_dir: Path, *, key: str) -> AnchorIdentity`: Decide eligibility for a validated handoff directory and name the anchor it would produce.
* `def create_anchor(invocation: Invocation, *, key: str, handoff_dir: Path, raw_artifact_id: int, raw_artifact_name: str, raw_artifact_digest: str, output: Path) -> dict[str, Any]`: Write the anchor bundle into the new ``output`` and return its validated manifest.
* `def validate_anchor_dir(root: Path, *, key: str | None = None, expected_subject_commit: str | None = None, raw_artifact_id: int | None = None, raw_artifact_name: str | None = None, raw_artifact_digest: str | None = None) -> dict[str, Any]`: Validate an anchor directory (inventory, hashes, canonical PNG re-inspection, embedded expectation) and, when all three ``raw_artifact_*`` are given, its source artifact binding. Needs no config: BP curate calls it with the anchor alone.

## `mod_base.evidence.commands`

Owner: MB3 (register() implemented by MB0; handlers dispatch to the entry points).

``expect``, ``prepare``, ``validate``, ``compact``, ``compose`` and ``anchor`` (MB3).

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_expect(args: argparse.Namespace) -> int`
* `def run_prepare(args: argparse.Namespace) -> int`
* `def run_validate(args: argparse.Namespace) -> int`
* `def run_compact(args: argparse.Namespace) -> int`
* `def run_compose(args: argparse.Namespace) -> int`
* `def run_anchor_identity(args: argparse.Namespace) -> int`
* `def run_anchor_create(args: argparse.Namespace) -> int`
* `def run_anchor_validate(args: argparse.Namespace) -> int`

## `mod_base.family.envelope`

Owner: MB4.

``mod-base.family.envelope`` creation and validation (MB4).

Constants:

* `ENVELOPE_NAME = 'envelope.json'`

* `def create_envelope(invocation: Invocation, *, family: str, key: str, bundle_dir: Path, coverage_sha: str, subject: Mapping[str, str], producer: Mapping[str, Any], output: Path) -> dict[str, Any]`: Copy the native bundle into the new ``output`` and write ``envelope.json`` beside it.
* `def validate_envelope_dir(invocation: Invocation, root: Path, *, family: str | None = None, key: str | None = None) -> dict[str, Any]`: Validate ``root/envelope.json`` against the exact directory inventory and the configured family (``handoff_max_bytes``); return the envelope.

## `mod_base.family.paired`

Owner: MB4.

``mod-base.family.paired`` validation, image re-inspection and ``family collect`` (MB4).

Constants:

* `PROJECTION_NAME = 'paired.json'`
* `IMAGES_DIRECTORY = 'images'`
* `SOURCE_DIRECTORY = 'source'`

* `class FamilyOutcome`: ``status`` is ``available``, ``superseded`` or ``unavailable``; ``projection`` is set only when available.
  * fields: `status: str, reason: str, projection: dict[str, Any] | None = None, carried_from: str | None = None`
* `def validate_projection(invocation: Invocation, projection_path: Path, *, images_root: Path, family: str, key: str, expected_coverage_sha: str) -> dict[str, Any]`: R4 for one written projection; returns the validated projection.
* `def verify_carry_forward(repo_root: Path, carried_from: str, coverage_sha: str) -> None`: R5: both commits are present as inert objects and ``carried_from`` is an ancestor of ``coverage_sha`` (``git merge-base --is-ancestor`` in a sanitized environment).
* `def collect_family(invocation: Invocation, *, family: str, key: str, input_dir: Path, expected_coverage_sha: str, output: Path) -> FamilyOutcome`: The ``family collect`` command: validate the envelope, run ``family_validate``, apply R4/R5 and, when available, write the collected layout (module docstring) into the new ``output``. ``superseded`` and ``unavailable`` are returned (the command exits 3 without writing an upload).

## `mod_base.family.commands`

Owner: MB4 (register() implemented by MB0; handlers dispatch to the entry points).

``family envelope`` and ``family collect`` (MB4). Flags are frozen by SPEC §2.2.

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_envelope(args: argparse.Namespace) -> int`
* `def run_collect(args: argparse.Namespace) -> int`

## `mod_base.pages.targets`

Owner: MB5.

Adapter ``targets`` orchestration, enrolled-branch listing and inert fetches (MB5).

* `def list_enrolled_branches(api: GitHubApi, *, max_branches: int) -> list[dict[str, str]]`: ``[{name, commit, tree}]`` of the repository's branches (one page; more than ``max_branches`` fails closed), each tree read from the commit API.
* `def fetch_inert(repo_root: Path, commits: Sequence[str], *, depth: int = 1) -> None`: Fetch ``commits`` into ``repo_root``'s object store without checkout, tags or credentials (sanitized ``git`` environment, ``protocol.version=2``, bounded retries).
* `def discover_targets(invocation: Invocation, *, api: GitHubApi | None) -> list[dict[str, Any]]`: Run the adapter ``targets`` hook for the configured mode and return 1..``targets.max`` validated targets (``api`` is required in enrolled-branches mode).

## `mod_base.pages.admission`

Owner: MB5.

Publication admission (MB5): QS ``publication_progress.decide`` + wake/recovery/family admission.

Constants:

* `COALESCE_SECONDS = 600`
* `PARTIAL_DEADLINE_SECONDS = 2700`
* `RECOVERY_INTERVAL_SECONDS = 3600`
* `MAX_REQUESTS = 160`
* `MAX_CANDIDATES = 8`
* `MAX_FAILED_PUBLICATIONS = 3`
* `ACTIVE_RUN_STATUSES = frozenset({'in_progress', 'pending', 'queued', 'requested', 'waiting'})`
* `REASONS = frozenset({'always', 'awaiting-complete-v1-evidence', 'coalescing', 'complete', 'current', 'deferred-active...`

* `class ProgressPolicy`
  * fields: `coalesce_seconds: int = 600, partial_deadline_seconds: int = 2700, recovery_interval_seconds: int = 3600, max_failed_publications: int = 3`
  * `@classmethod from_config(cls, admission: Mapping[str, Any]) -> 'ProgressPolicy'`
* `class Decision`
  * fields: `eligible: bool, reason: str, ready: int, published: int, next_check_at: float | None = None`
* `def decide(*, expected: set[str], published: dict[str, float], ready: dict[str, float], ordinary_ready: bool, published_at: float | None, now: float, publisher_available: bool = True, failed_publications: int = 0, ordinary_changed: bool = False, policy: ProgressPolicy = ProgressPolicy(coalesce_seconds=600, partial_deadline_seconds=2700, recovery_interval_seconds=3600, max_failed_publications=3)) -> Decision`: QS ``publication_progress.decide`` (pure, deterministic; same reasons and ordering).
* `class WakeInputs`: The identifier inputs of ``pages.yml`` (all optional; validated per operation).
  * fields: `run_id: int | None = None, sha: str | None = None, family: str | None = None, bundle_key: str | None = None, artifact_id: int | None = None, artifact_digest: str | None = None, coverage_sha: str | None = None`
* `class Admission`: ``admit``'s outputs (each written to ``$GITHUB_OUTPUT`` as one line of canonical JSON).
  * fields: `eligible: bool, reason: str, bundle_keys: list[str] = <factory>, subjects: dict[str, dict[str, str]] = <factory>, families: list[dict[str, str]] = <factory>, nominations: dict[str, int] = <factory>, heads: dict[str, str] = <factory>`
* `def admit(invocation: Invocation, *, api: GitHubApi, operation: str, wake: WakeInputs, now: float | None = None, sleep: Callable[[float], None] = sleep) -> Admission`: Decide whether this Pages run publishes (see module docstring). ``operation`` is one of ``workflow.PUBLISH_OPERATIONS``. An ineligible admission is a normal outcome (exit 0).

## `mod_base.pages.select`

Owner: MB5.

Select the newest authenticated evidence for one key (MB5).

Constants:

* `SELECTED_KEYS = ('kind', 'artifact_id', 'name', 'digest', 'size', 'run_id', 'run_attempt')`
* `SELECTED_KINDS = ('handoff', 'cache', 'family-handoff', 'family-cache')`

* `class Selected`: The ``select`` outputs; also the ``--selected-json`` document of ``authenticate``.
  * fields: `kind: str, artifact_id: int, name: str, digest: str, size: int, run_id: int, run_attempt: int`
  * `@classmethod parse(cls, value: Any) -> 'Selected'`
  * `to_json(self) -> dict[str, Any]`: The JSON object form: exactly :data:`SELECTED_KEYS` mapped to the field values.
* `def select_evidence(invocation: Invocation, *, api: GitHubApi, key: str, family: str | None = None, nomination: int | None = None, expected_subject_commit: str) -> Selected`: Return the selected artifact or raise ``Unavailable`` (see module docstring).

## `mod_base.pages.authenticate`

Owner: MB5.

Source-run authentication producing ``mod-base.selection`` (MB5, SPEC §4.8).

* `def authenticate_selection(invocation: Invocation, *, api: GitHubApi, key: str, selected_dir: Path, selected: Selected) -> dict[str, Any]`: Authenticate the downloaded ``selected_dir`` and return the validated selection draft.
* `def kit_binding(api: GitHubApi, invocation: Invocation, *, manifest: Mapping[str, Any], owner_run: Mapping[str, Any], selected_kind: str) -> dict[str, str]`: ``{source, sha}``: prove ``manifest.kit.sha`` belongs to the authenticated owner run.
* `def run_authenticate(invocation: Invocation, *, api: GitHubApi, key: str, selected_dir: Path, selected_json: Path, output: Path) -> dict[str, Any]`: The ``authenticate`` command: read ``selected_json`` (the exact :meth:`Selected.to_json` object written by ``select --output``), authenticate and write the selection draft to ``output`` (canonical JSON, new file).

## `mod_base.pages.commands_control`

Owner: MB5 (register() implemented by MB0; handlers dispatch to the entry points).

``admit``, ``select`` and ``authenticate`` (MB5).

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_admit(args: argparse.Namespace) -> int`
* `def run_select(args: argparse.Namespace) -> int`
* `def run_authenticate(args: argparse.Namespace) -> int`

## `mod_base.pages.build`

Owner: MB6.

The atomic site renderer (MB6, SPEC §5.3.2): QS ``build_site`` + BP ``_current_pages_inputs``.

Constants:

* `PROMOTION_FILE = 'promotion.json'`
* `SITE_ALLOWLIST = ('index.html', 'e2e/index.html', 'assets/site.js', 'assets/gallery.js', 'assets/styles.css')`

* `class BuildResult`
  * fields: `heads: dict[str, str], site_sha256: str, promotion: dict[str, Any]`
* `def build_site(invocation: Invocation, *, api: GitHubApi, kit_root: Path, collected_dir: Path, families_dir: Path, output: Path, promotion_dir: Path) -> BuildResult`: Render and publish the atomic site (see module docstring); ``output`` must not exist.
* `def render_site(invocation: Invocation, *, kit_root: Path, bundles: list[dict[str, Any]], families: list[dict[str, Any]], stage_fd: int) -> dict[str, bytes]`: Pure rendering of already authenticated inputs into the stage; returns the written ``{relative path: bytes}`` map that :func:`mod_base.io.seal.seal_output` verifies.

## `mod_base.pages.templating`

Owner: MB6.

Build-time templating of the kit front end (MB6, SPEC §6.2).

Constants:

* `PLACEHOLDERS = frozenset({'actions_url', 'description', 'eyebrow', 'issues_url', 'license', 'meta_description', 'name', 'p...`
* `CONDITIONS = frozenset({'families', 'icon', 'primary_link'})`
* `ATTRIBUTES = frozenset({'aria-label', 'content', 'href'})`

* `def render(template: str, values: Mapping[str, str], conditions: Mapping[str, bool]) -> str`: Substitute ``values`` (exactly :data:`PLACEHOLDERS`) and resolve ``conditions`` (exactly :data:`CONDITIONS`); raise :class:`mod_base.errors.MbError` on any template violation.
* `def theme_css(theme: Mapping[str, Any]) -> str`: ``:root{--bg:...}`` from ``theme.dark`` plus, when ``theme.light`` is set, a ``@media (prefers-color-scheme: light){:root{...}}`` block; values are validated colours.

## `mod_base.pages.refresh`

Owner: MB6.

Roll the promoted bundles forward as caches (MB6): QS refresh-cache + BP ``refresh_cache``.

* `class RefreshResult`
  * fields: `available: bool, cache_name: str | None, baseline_name: str | None = None`
* `def refresh_bundle(invocation: Invocation, *, api: GitHubApi, key: str, family: str | None, input_dir: Path) -> RefreshResult`: Download and revalidate one promoted bundle, write its upload bytes into the new ``input_dir`` and name its cache (see module docstring). For a family leg with nothing collected it returns ``available=False`` (exit 0).

## `mod_base.pages.commands_build`

Owner: MB6 (register() implemented by MB0; handlers dispatch to the entry points).

``build`` and ``refresh`` (MB6). Flags are frozen by SPEC §2.2.

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_build(args: argparse.Namespace) -> int`
* `def run_refresh(args: argparse.Namespace) -> int`

## `mod_base.pages.rotate`

Owner: MB7.

Exact-ID rotation of superseded evidence after an authenticated successful Pages run (MB7).

* `class RotationDeferred(MbError)`: Some candidates were retained after completing these exact deletions (budget spent).
  * `__init__(self, message: str, deleted_artifact_ids: list[int]) -> None`
* `class DeletionBudget`: Bounds authenticated deletion attempts across one rotation invocation.
  * fields: `remaining: int = 32, last_deferred_count: int = 0`
  * `begin_scope(self) -> None`
  * `select(self, artifacts: list[object]) -> list[object]`
  * `consume(self) -> None`
* `def rotate_generation(invocation: Invocation, *, api: GitHubApi, owner_run_id: int, owner_sha: str, delete_delay_seconds: float = 1.0, dry_run: bool = False, now: float | None = None, sleep: Callable[[float], None] = sleep) -> dict[str, object]`: Retire everything R superseded and return the JSON summary (QS keys plus ``remaining_rotation_deletions``). ``dry_run`` plans and re-observes without deleting.

## `mod_base.pages.commands_rotate`

Owner: MB7 (register() implemented by MB0; handlers dispatch to the entry points).

``rotate`` (MB7). Flags are frozen by SPEC §2.2. Loads config data only (no repository checks, no adapter): the rotate job sparse-checks-out ``site/mod-base.json`` alone.

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_rotate(args: argparse.Namespace) -> int`

## `mod_base.pin`

Owner: MB9.

The kit pin: parser, verifier, kit resolution and ``kit-digest-v1`` (MB9).

Constants:

* `KIT_TOKEN = 'The-Plum-Team/mod-base'`
* `DIGESTED_DIRS = ('src', 'site', 'requirements')`
* `STAMP_NAME = 'MOD_BASE_KIT.json'`
* `OVERLAY_PATH = 'out/mod-base-kit'`

* `class Pin`: The single pin of a mod: ``sha`` (40-hex), ``version`` (``vX.Y.Z``) and every referencing ``path@line`` location, sorted.
  * fields: `sha: str, version: str, references: tuple[str, ...]`
* `def parse_pin_files(files: Mapping[str, bytes]) -> Pin`: Parse the pin from ``{repo-relative path: bytes}`` of the workflow and action files.
* `def parse_pin(repo: Path) -> Pin`: Read (bounded) the mod's workflow and action files and parse its single pin.
* `def verify(repo: Path, *, network: bool, api: GitHubApi | None = None) -> Pin`: Pin consistency; with ``network`` also ``compare/<pin>...main`` is ``ahead|identical`` with ``behind_by == 0`` and ``git/ref/tags/<version>`` peels to the pin (``api`` required).
* `def kit_tree_digest(root: Path) -> str`: ``sha256:<hex>`` kit-digest-v1 of ``root`` (the kit checkout root).
* `def kit_path(repo: Path, environ: Mapping[str, str]) -> Path`: Resolve the kit root for ``repo`` in the bootstrap order (overlay stamp, env, user cache, anonymous fetch), verifying each candidate; raise :class:`mod_base.errors.Unavailable`.
* `def read_stamp(directory: Path) -> dict[str, Any]`: Read and validate ``MOD_BASE_KIT.json`` (``mod-base.kit-stamp`` v1) in ``directory``.

## `mod_base.pin_commands`

Owner: MB9 (register() implemented by MB0; handlers dispatch to the entry points).

``pin verify`` and ``digest`` (MB9). Flags are frozen by SPEC §2.2.

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_verify(args: argparse.Namespace) -> int`
* `def run_digest(args: argparse.Namespace) -> int`

## `mod_base.template.tool`

Owner: MB9.

``template check|sync|init`` (MB9, SPEC §8.2).

Constants:

* `MANIFEST_PATH = 'template/manifest.json'`

* `class Drift`: One difference: ``kind`` is ``missing``, ``changed``, ``fragment``, ``agents``, ``links``, ``forbidden`` or ``extension``; ``detail`` is a bounded unified diff or message.
  * fields: `path: str, kind: str, detail: str`
* `def load_manifest(kit_root: Path) -> dict[str, Any]`: Read and validate ``template/manifest.json`` (``mod-base.template-manifest`` v1).
* `def check(repo: Path, *, kit_root: Path) -> list[Drift]`
* `def sync(repo: Path, *, kit_root: Path, write: bool) -> list[Drift]`
* `def init(repo: Path, *, kit_root: Path, seed: bool, from_config: Path | None) -> list[str]`: Seed missing files; return the created paths; refuse to overwrite anything.

## `mod_base.template.commands`

Owner: MB9 (register() implemented by MB0; handlers dispatch to the entry points).

``template check|sync|init`` (MB9). Flags are frozen by SPEC §2.2; exit 2 on drift.

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_check(args: argparse.Namespace) -> int`
* `def run_sync(args: argparse.Namespace) -> int`
* `def run_init(args: argparse.Namespace) -> int`

## `mod_base.conformance.run`

Owner: MB10.

``conformance``: the synthetic producer -> collect -> build -> refresh -> rotate simulation (MB10).

* `def run_conformance(*, repo: Path, keys: Sequence[str] | None, all_keys: bool, kit_root: Path, families: bool) -> dict[str, Any]`: Run the simulation and return a report ``{keys: [...], families: [...], checks: int}``; any failed check raises :class:`mod_base.errors.MbError` (exit 2).
* `def main(argv: Sequence[str] | None = None) -> int`: The simulation child (see module docstring): ``python3 -P -m mod_base.conformance.run`` with the same flags as the ``conformance`` command; writes the canonical JSON report to stdout and returns an exit code through ``errors.run_main``.

## `mod_base.conformance.commands`

Owner: MB10 (register() implemented by MB0; handlers dispatch to the entry points).

``conformance`` (MB10). Flags are frozen by SPEC §2.2.

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run(args: argparse.Namespace) -> int`
