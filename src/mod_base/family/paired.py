"""``mod-base.family.paired`` validation, image re-inspection and ``family collect`` (MB4).

R4: the envelope inventory is validated first, then the projection returned by the adapter's
``family_validate`` against ``documents.validate_family_paired`` (with the configured family image
policy); every image is re-inspected (WebP decode, dimensions equal to
``thumbnail(recorded source, derivative_box)``, recorded pixel metrics equal to the kit's); every
unclean pair is rejected; ``projection.coverage_sha`` must equal ``expected_coverage_sha``.
R5: when ``carried_from`` is set the core requires ``git merge-base --is-ancestor carried_from
expected_coverage_sha`` over the inert objects already present: the ``family`` job fetches both
commits anonymously before ``family collect`` runs (``build`` reads the default branch's history of
its ``fetch-depth: 0`` checkout); the core itself never fetches (:mod:`mod_base.family._git`).

Layout of an available ``family collect`` output, uploaded as ``mb-collected-family--<family>--<key>``:

* ``paired.json`` (:data:`PROJECTION_NAME`): the validated ``mod-base.family.paired`` projection;
* ``images/<sha256>.webp`` (:data:`IMAGES_DIRECTORY`): its images;
* ``source/`` (:data:`SOURCE_DIRECTORY`): the selected family handoff or cache copied verbatim
  (``envelope.json`` plus the native bundle, exactly its envelope inventory);
* ``selected.json`` (:data:`SELECTED_NAME`): the canonical ``Selected`` object of that generation,
  exactly the bytes ``select --family --output F`` wrote and ``family collect --selected-json F``
  bound to its input (below).

``build`` renders ``paired.json`` and ``images/`` and re-authenticates the recorded selection by id
(no walk: its cost never grows with how far below the coverage the generation was produced);
finalize's ``refresh-family`` re-validates the whole artifact, binds the recorded selection to the
promotion and rolls exactly ``source/`` forward as ``mb-family-cache--<family>--<key>--<coverage_sha>``,
so every family cache stays re-collectable by ``family collect`` (carry-forward is re-proven, R5,
at every collection).

The recorded selection is bound to the generation being collected before the adapter runs (reason
``family-selection``, exit 2): it is the canonical JSON (at most ``limits.MAX_SELECTED_JSON_BYTES``,
never a symlink) of a ``Selected`` object whose ``kind`` is ``family-handoff`` or ``family-cache``
and whose name parses to this family and key. A family handoff must be the upload of the envelope's
producer run attempt (``run_id`` and ``run_attempt``, the name's attempt included). A family cache
must be named by a commit present as an inert object that the envelope's coverage precedes (equal
or an ancestor: ``refresh`` names a carried cache by the coverage it was promoted at) and that
precedes the expected coverage (the first-parent walk of ``select`` only probes ancestors). Its id,
digest and size are the ones ``download`` already verified the archive against, from the same
``select`` outputs; ``build`` re-reads that id from the API. The kit binding (SPEC §1.8, reason
``kit-binding``) is re-proven locally for both kinds: ``envelope.kit`` must be the pin of the
family's ``producer.workflow`` in the producer's own checkout (``producer.commit``, which the
family job fetches as the envelope coverage), a regular file read from the inert object store (an
object that is absent or not a regular file fails with reason ``git``).

Details the core enforces (all fail closed with exit 2; only the hook's own ``superseded`` and
``unavailable`` statuses are the exit-3 absences):

* The projection is strict JSON of at most ``limits.MAX_PAIRED_BYTES``; ``images/`` beside it holds
  exactly the referenced images (at most ``limits.MAX_FAMILY_FILES``, together at most
  ``limits.MAX_FAMILY_PROJECTION_BYTES``), each read without following any symlink, equal to its
  recorded size and SHA-256 and re-inspected with the kit's ``inspect_webp`` at exactly its recorded
  (thumbnail) dimensions; a path shared by several pairs must carry one identical record. Those
  bounds and the family's ``handoff_max_bytes`` (at most ``limits.MAX_FAMILY_HANDOFF_BYTES``)
  partition ``limits.MAX_COLLECTED_FAMILY_BYTES``, so a generation within them always fits the
  collected artifact: the output check below guards the arithmetic, never a producer's choice.
* ``family_validate`` writes into a fresh private ``output_dir`` exactly its projection
  (``projection_path``) and the referenced ``images/`` beside it, nothing else.
* The projection is bound to the authenticated envelope: equal ``subject``, its producer
  ``RunRecord`` carries exactly the envelope's producer ``RunClaim`` and its ``event`` is one of the
  family's configured ``producer.events``. (Its ``created_at``, ``display_title`` and
  ``job_graph_sha256`` remain adapter claims: ``family_validate`` has no network access and no core
  step reads the producer run's API record here.)
* Coverage: an envelope covers exactly its producer's checkout (``coverage_sha == subject.commit``,
  :mod:`mod_base.family.envelope`). Without ``carried_from`` the envelope must already cover
  ``expected_coverage_sha``. A hook ``carried_from`` requires the family's ``carry_forward``, must
  name the envelope's ``coverage_sha`` (the coverage the native bundle states) and passes R5 up to
  ``expected_coverage_sha``. An envelope's own ``carried_from`` is re-proven (R5) up to the
  envelope's ``coverage_sha`` at every collection.
* The collected output is written through ``atomic_directory``: every byte copied into ``source/``
  and ``images/`` is re-checked against its envelope or projection record, ``paired.json`` is the
  canonical JSON of the validated projection, ``selected.json`` the bound selection's canonical
  bytes, and the whole written tree (walked within the
  ``collected-family`` extraction bounds, directories included) must equal that inventory and fit
  those bounds.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.adapter import host
from mod_base.errors import MbError
from mod_base.family import _git
from mod_base.family._common import SOURCE_DIRECTORY, fail, family_config, real_directory
from mod_base.family.envelope import ENVELOPE_NAME, MAX_NATIVE_FILE_BYTES, validate_envelope_dir
from mod_base.imaging.metrics import ImageError, SizePolicy, inspect_webp
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.bounded_zip import LIMITS_BY_KIND
from mod_base.io.tree import file_records, read_child_file, regular_files
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, read_json_file, sha256_hex
from mod_base.model.documents import RUN_CLAIM_FIELDS, validate_family_paired
from mod_base.pages.select import Selected
from mod_base.pin import parse_pin_files
from mod_base.runtime import Invocation

OWNER = "MB4"
PROJECTION_NAME = "paired.json"
IMAGES_DIRECTORY = "images"
#: The selection record at the root of a collected family artifact (module docstring).
SELECTED_NAME = "selected.json"
HOOK = "family_validate"
REASON = "family-projection"
CARRY_REASON = "carry-forward"
SELECTION_REASON = "family-selection"
_FAMILY_KINDS = ("family-handoff", "family-cache")


@dataclass(frozen=True)
class FamilyOutcome:
    """``status`` is ``available``, ``superseded`` or ``unavailable``; ``projection`` is set only
    when available."""

    status: str
    reason: str
    projection: dict[str, Any] | None = None
    carried_from: str | None = None


def _fail(message: str, reason: str = REASON) -> MbError:
    return fail(message, reason)


def _image_records(projection: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every distinct image of the projection by path; one path carries one identical record."""

    images: dict[str, dict[str, Any]] = {}
    for lane in projection["lanes"]:
        for pair in lane["pairs"]:
            for side in ("reference", "candidate"):
                image = pair[side]["image"]
                known = images.setdefault(image["path"], image)
                if known is not image and canonical_json(known) != canonical_json(image):
                    raise _fail(f"image {image['path']} is recorded with different facts by two pairs")
    if len(images) > lim.MAX_FAMILY_FILES:
        raise _fail(f"the projection references more than {lim.MAX_FAMILY_FILES} images")
    if sum(image["size"] for image in images.values()) > lim.MAX_FAMILY_PROJECTION_BYTES:
        raise _fail(f"the projection images exceed {lim.MAX_FAMILY_PROJECTION_BYTES} bytes")
    return dict(sorted(images.items()))


def _require_image_inventory(root: Path, images: Mapping[str, Mapping[str, Any]]) -> None:
    directory = root / IMAGES_DIRECTORY
    if not os.path.lexists(directory):
        if images:
            raise _fail(f"the projection's {IMAGES_DIRECTORY}/ directory is missing")
        return
    listed = regular_files(directory, max_files=len(images) + 1,
                           max_total_bytes=(len(images) + 1) * lim.MAX_DERIVATIVE_BYTES,
                           max_file_bytes=lim.MAX_DERIVATIVE_BYTES)
    present = {f"{IMAGES_DIRECTORY}/{name}" for name in listed}
    if present != set(images):
        raise _fail(f"{IMAGES_DIRECTORY}/ differs from the projection (missing={sorted(set(images) - present)[:3]} "
                    f"extra={sorted(present - set(images))[:3]})")


def _require_bytes(data: bytes, record: Mapping[str, Any], label: str) -> None:
    if len(data) != record["size"] or sha256_hex(data) != record["sha256"]:
        raise _fail(f"{label} differs from its recorded size or SHA-256")


def _reinspect(root: Path, path: str, image: Mapping[str, Any]) -> None:
    data = read_child_file(root, path, max_bytes=lim.MAX_DERIVATIVE_BYTES)
    _require_bytes(data, image, path)
    try:
        metrics = inspect_webp(data, SizePolicy.exact(image["width"], image["height"]))
    except ImageError as exc:
        raise _fail(f"{path} fails re-inspection: {exc}") from exc
    if canonical_json(metrics) != canonical_json(image["pixel"]):
        raise _fail(f"{path}: the recorded pixel metrics differ from the kit's re-inspection")


def _validate_projection(invocation: Invocation, projection_path: Path, *, images_root: Path, family: str,
                         key: str, expected_coverage_sha: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    config = family_config(invocation, family)
    grammar.require_key(key)
    grammar.require_sha1(expected_coverage_sha, "expected coverage SHA")
    document, _ = read_json_file(Path(os.path.abspath(projection_path)), label=PROJECTION_NAME,
                                 max_bytes=lim.MAX_PAIRED_BYTES)
    projection = validate_family_paired(document, image_policy=config["image_policy"])
    for field, wanted in (("family", family), ("key", key)):
        if projection[field] != wanted:
            raise _fail(f"the projection's {field} is {projection[field]!r}, not {wanted!r}")
    if projection["coverage_sha"] != expected_coverage_sha:
        raise _fail(f"the projection covers {projection['coverage_sha']}, not {expected_coverage_sha}",
                    reason="stale-coverage")
    images = _image_records(projection)
    root = real_directory(images_root, "projection image root", REASON)
    _require_image_inventory(root, images)
    for path, image in images.items():
        _reinspect(root, path, image)
    return projection, images


def validate_projection(invocation: Invocation, projection_path: Path, *, images_root: Path, family: str, key: str,
                        expected_coverage_sha: str) -> dict[str, Any]:
    """R4 for one written projection; returns the validated projection."""

    projection, _ = _validate_projection(invocation, projection_path, images_root=images_root, family=family,
                                         key=key, expected_coverage_sha=expected_coverage_sha)
    return projection


def verify_carry_forward(repo_root: Path, carried_from: str, coverage_sha: str) -> None:
    """R5: both commits are present as inert objects and ``carried_from`` is an ancestor of
    ``coverage_sha`` (``git merge-base --is-ancestor`` in a sanitized environment)."""

    grammar.require_sha1(carried_from, "carried_from")
    grammar.require_sha1(coverage_sha, "coverage SHA")
    if carried_from == coverage_sha:
        raise _fail("a carry-forward must start from another commit than its coverage", CARRY_REASON)
    repo = Path(os.path.abspath(repo_root))
    _git.require_commit(repo, carried_from, "carried_from commit")
    _git.require_commit(repo, coverage_sha, "coverage commit")
    if not _git.is_ancestor(repo, carried_from, coverage_sha):
        raise _fail(f"R5: {carried_from} is not an ancestor of {coverage_sha}; evidence never carries forward "
                    "across non-ancestors", CARRY_REASON)


def _read_selection(path: Path) -> tuple[Selected, bytes]:
    """The ``Selected`` record at ``path`` and its canonical bytes (module docstring)."""

    try:
        value, raw = read_json_file(Path(os.path.abspath(path)), label="the selected family generation",
                                    max_bytes=lim.MAX_SELECTED_JSON_BYTES)
        selected = Selected.parse(value)
    except MbError as exc:
        raise _fail(f"the selected family generation is malformed: {exc}", SELECTION_REASON) from exc
    if canonical_json(value) != raw:
        raise _fail("the selected family generation is not the canonical JSON select --output writes",
                    SELECTION_REASON)
    return selected, raw


def _bind_selection(invocation: Invocation, selected: Selected, envelope: Mapping[str, Any],
                    config: Mapping[str, Any], *, family: str, key: str, expected_coverage_sha: str) -> None:
    """The recorded selection names the generation being collected, and that generation's kit is
    the pin of its producer workflow at the producer's checkout (module docstring)."""

    parsed = grammar.parse_artifact_name(selected.name)
    if selected.kind not in _FAMILY_KINDS or parsed is None or (parsed.family, parsed.key) != (family, key):
        raise _fail(f"the selection names {selected.name}, not a generation of {family}/{key}", SELECTION_REASON)
    producer, repo = envelope["producer"], invocation.repo_root
    if selected.kind == "family-handoff":
        if (selected.run_id, selected.run_attempt) != (producer["run_id"], producer["run_attempt"]):
            raise _fail(f"the selected family handoff {selected.name} of run {selected.run_id} is not the upload of "
                        f"its envelope's producer run {producer['run_id']} attempt {producer['run_attempt']}",
                        SELECTION_REASON)
    else:
        named, covered = parsed.coverage_sha, envelope["coverage_sha"]
        _git.require_commit(repo, named, "family cache commit")
        if named != covered and not _git.is_ancestor(repo, covered, named):
            raise _fail(f"the selected family cache {selected.name} is named by a commit its envelope coverage "
                        f"{covered} does not precede", SELECTION_REASON)
        if named != expected_coverage_sha and not _git.is_ancestor(repo, named, expected_coverage_sha):
            raise _fail(f"the selected family cache {selected.name} is not named by an ancestor of the coverage "
                        f"{expected_coverage_sha}", SELECTION_REASON)
    workflow, commit, kit = config["producer"]["workflow"], producer["commit"], envelope["kit"]
    data = _git.read_blob(repo, commit, workflow, lim.MAX_WORKFLOW_FILE_BYTES)
    try:
        pin = parse_pin_files({workflow: data})
    except MbError as exc:
        raise _fail(f"cannot read the kit pin of {workflow} at the producer commit {commit}: {exc}",
                    "kit-binding") from exc
    if pin.sha != kit["sha"] or pin.version != "v" + kit["version"]:
        raise _fail(f"the family generation's kit {kit['sha']} ({kit['version']}) is not the pin of {workflow} at its "
                    f"producer commit {commit}", "kit-binding")


def _hook_projection(invocation: Invocation, hook_output: Path, relative: str, *, family: str, key: str,
                     expected_coverage_sha: str) -> tuple[dict[str, Any], Path, dict[str, dict[str, Any]]]:
    """Validate what ``family_validate`` wrote: exactly its projection and the images beside it."""

    listed = regular_files(hook_output, max_files=lim.MAX_FAMILY_FILES + 1,
                           max_total_bytes=lim.MAX_PAIRED_BYTES + lim.MAX_FAMILY_PROJECTION_BYTES,
                           max_file_bytes=lim.MAX_PAIRED_BYTES)
    if relative not in listed:
        raise _fail(f"{HOOK} reported {relative!r} but wrote no such projection")
    parent = relative.rpartition("/")[0]
    images_root = hook_output.joinpath(*parent.split("/")) if parent else hook_output
    projection, images = _validate_projection(invocation, hook_output.joinpath(*relative.split("/")),
                                              images_root=images_root, family=family, key=key,
                                              expected_coverage_sha=expected_coverage_sha)
    prefix = f"{parent}/" if parent else ""
    wanted = {relative} | {prefix + path for path in images}
    if set(listed) != wanted:
        raise _fail(f"{HOOK} wrote files beside its projection and images: {sorted(set(listed) - wanted)[:3]}")
    return projection, images_root, images


def _bind(invocation: Invocation, envelope: Mapping[str, Any], projection: Mapping[str, Any],
          result: Mapping[str, Any], config: Mapping[str, Any], expected_coverage_sha: str) -> str | None:
    """Bind the projection to its authenticated envelope and prove its coverage (R5)."""

    if projection["subject"] != envelope["subject"]:
        raise _fail("the projection's subject differs from its envelope's")
    producer = projection["provenance"]["producer"]
    if {field: producer[field] for field in RUN_CLAIM_FIELDS} != envelope["producer"]:
        raise _fail("the projection's producer is not the envelope's producer run")
    if producer["event"] not in config["producer"]["events"]:
        raise _fail(f"the projection's producer event {producer['event']!r} is not a configured producer event of "
                    f"family {envelope['family']!r}")
    carried_from = result.get("carried_from")
    if carried_from is None:
        if envelope["coverage_sha"] != expected_coverage_sha:
            raise _fail(f"the envelope covers {envelope['coverage_sha']}, not {expected_coverage_sha}, and "
                        f"{HOOK} reported no carry-forward", CARRY_REASON)
        return None
    if not config["carry_forward"]:
        raise _fail(f"family {envelope['family']!r} does not allow carry-forward", CARRY_REASON)
    if carried_from != envelope["coverage_sha"]:
        raise _fail(f"{HOOK} carried forward from {carried_from}, not from the envelope coverage "
                    f"{envelope['coverage_sha']}", CARRY_REASON)
    verify_carry_forward(invocation.repo_root, carried_from, expected_coverage_sha)
    return carried_from


def _write_collected(output: Path, *, source: Path, envelope: Mapping[str, Any], projection: Mapping[str, Any],
                     images_root: Path, images: Mapping[str, Mapping[str, Any]], selection: bytes) -> None:
    envelope_bytes = canonical_json(envelope)
    projection_bytes = canonical_json(projection)
    bound = LIMITS_BY_KIND["collected-family"]
    count = len(envelope["files"]) + len(images) + 3
    total = (sum(record["size"] for record in envelope["files"]) + len(envelope_bytes) + len(projection_bytes)
             + sum(image["size"] for image in images.values()) + len(selection))
    if count > bound.max_entries or total > bound.max_total_bytes:
        raise _fail("the collected family artifact would exceed its extraction bounds")

    def writer(stage: Path, descriptor: int) -> None:
        written: list[dict[str, Any]] = []

        def put(relative: str, data: bytes) -> None:
            write_new(descriptor, relative, data)
            written.append({"path": relative, "sha256": sha256_hex(data), "size": len(data)})

        if read_child_file(source, ENVELOPE_NAME, max_bytes=lim.MAX_ENVELOPE_BYTES) != envelope_bytes:
            raise _fail(f"{ENVELOPE_NAME} changed while the family was collected")
        put(f"{SOURCE_DIRECTORY}/{ENVELOPE_NAME}", envelope_bytes)
        for record in envelope["files"]:
            data = read_child_file(source, record["path"], max_bytes=MAX_NATIVE_FILE_BYTES)
            _require_bytes(data, record, f"native file {record['path']}")
            put(f"{SOURCE_DIRECTORY}/{record['path']}", data)
        put(PROJECTION_NAME, projection_bytes)
        put(SELECTED_NAME, selection)
        for path, image in images.items():
            data = read_child_file(images_root, path, max_bytes=lim.MAX_DERIVATIVE_BYTES)
            _require_bytes(data, image, path)
            put(path, data)
        # The walk is bounded by what the artifact may legitimately hold, directories included (a
        # nested native bundle has more directories than files); the equality below is the guard.
        observed = file_records(stage, max_files=bound.max_entries, max_total_bytes=bound.max_total_bytes,
                                max_file_bytes=bound.max_entry_bytes)
        if observed != sorted(written, key=lambda record: record["path"]):
            raise _fail("the collected output differs from the bytes written")

    atomic_directory(output, writer)


def collect_family(invocation: Invocation, *, family: str, key: str, input_dir: Path, expected_coverage_sha: str,
                   output: Path, selected_json: Path) -> FamilyOutcome:
    """The ``family collect`` command: validate the envelope, bind the recorded selection
    ``selected_json`` (``select --family --output F``) to it, run ``family_validate``, apply R4/R5
    and, when available, write the collected layout (module docstring) into the new ``output``.
    ``superseded`` and ``unavailable`` are returned (the command exits 3 without writing an
    upload)."""

    config = family_config(invocation, family)
    grammar.require_key(key)
    grammar.require_sha1(expected_coverage_sha, "expected coverage SHA")
    source = real_directory(input_dir, "selected family generation", REASON)
    target = Path(os.path.abspath(output))
    if target == source or source in target.parents:
        raise _fail("the collected output must lie outside the selected family generation")
    selected, selection = _read_selection(selected_json)
    envelope = validate_envelope_dir(invocation, source, family=family, key=key)
    _bind_selection(invocation, selected, envelope, config, family=family, key=key,
                    expected_coverage_sha=expected_coverage_sha)
    if "carried_from" in envelope:
        verify_carry_forward(invocation.repo_root, envelope["carried_from"], envelope["coverage_sha"])
    with tempfile.TemporaryDirectory(prefix="mb-family-") as work:
        hook_output = Path(work).resolve() / "projection"
        os.mkdir(hook_output, 0o700)
        result = host.call(invocation, HOOK, {"family": family, "key": key, "bundle_dir": str(source),
                                              "expected_coverage_sha": expected_coverage_sha,
                                              "output_dir": str(hook_output)})
        if result["status"] != "available":
            return FamilyOutcome(status=result["status"], reason=result["reason"])
        projection, images_root, images = _hook_projection(invocation, hook_output, result["projection_path"],
                                                           family=family, key=key,
                                                           expected_coverage_sha=expected_coverage_sha)
        carried_from = _bind(invocation, envelope, projection, result, config, expected_coverage_sha)
        _write_collected(target, source=source, envelope=envelope, projection=projection, images_root=images_root,
                         images=images, selection=selection)
    return FamilyOutcome(status="available", reason=result["reason"], projection=projection,
                         carried_from=carried_from)
