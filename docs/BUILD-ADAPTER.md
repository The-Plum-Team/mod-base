# Build adapter contract (`BUILD_ADAPTER_API = 1`)

What a mod provides so that the shared Build and Packaged E2E workflows can plan, build, run and
verify it. This contract is separate from the Pages adapter in [ADAPTER.md](ADAPTER.md). The
normative definition is `mod_base.build_ci.adapter`; a complete working example is the synthetic
mod in `tests/fixtures/ci_mod/`, which `tests/test_ci_mod.py` runs hook by hook. It has the shape
of the real mods: one target whose two lanes share a manifest and an SBOM, one target without an
SBOM, and a `gradle.properties` that holds the mod version as an extra plan input.

A mod writes only its own native files at the paths below. It never writes a kit document: the
plan, the envelopes and the validation record are built by protected kit code from what the hooks
leave behind.

## The protected Build config

`scripts/ci/mod-base-build.json` (kind `mod-base.build.config`, fields in
[SCHEMAS.md](SCHEMAS.md#mod-basebuildconfig-scriptscimod-base-buildjson--1-mib)) is read from the
protected default branch, never from a pull request.

| Field | Used for |
|---|---|
| `adapter.dispatcher` | the one program every hook runs |
| `adapter.path`, `adapter.policy` | the adapter module and the policy entry point the dispatcher uses |
| `adapter.files` | every file the dispatcher imports, with its SHA-256. Protected hooks run from a copy that holds this config and exactly these files, so an import that is not listed fails |
| `inventory.path`, `scenario_contract.path` | the two candidate files every plan is derived from |
| `plan_inputs` | up to eight more candidate files the plan needs, each `{"name", "path"}`, sorted by name: `path` is the file in the tested tree, `name` the file name the protected hooks find it under. Quick Skin lists `{"name": "gradle-properties", "path": "gradle.properties"}`, because that file holds the version in its JAR names; a mod that needs nothing more writes `[]` |
| `bundle.path` | the directory, relative to a lane's checkout, where the verified Build is staged before `run_lane` |
| `contexts.build`, `contexts.packaged` | the two required status contexts |
| `timeouts` | `validator_seconds` for every protected hook, `policy_seconds`, `target_seconds` and `runtime_seconds` for the three candidate hooks |

The config bytes, the listed files, the pinned kit, the graph versions and the mod's own control
files (`site/mod-base-build-activation.json` and the four `mod-base-*.yml` caller workflows, each by
its bytes or as absent) form the policy digest (`policy_sha256`) of every plan. Changing any of
them makes the next default-branch run a full run instead of a reuse of the pull request's
evidence.

## How a hook runs

```
<python> -I -B <checkout>/<adapter.dispatcher> --hook <name>
```

* `-I` keeps the dispatcher's directory off `sys.path`: the dispatcher adds it itself before it
  imports the adapter module.
* The process runs in a disposable account without credentials or tokens. Its environment is built
  from nothing: a fixed locale, user, time zone and Git and Python safety settings, `HOME`,
  `TMPDIR`, `PATH`, `JAVA_HOME` (when the job installs a JDK), `GRADLE_USER_HOME`, `MB_TESTED_SHA`,
  `MB_TESTED_TREE`, `MB_REPOSITORY`, `MB_SOURCE_BRANCH`, `MB_RUN_ID`, `MB_RUN_ATTEMPT`, plus the
  names in the table below. No variable of the runner reaches a hook: no `GITHUB_*`, no token.
* The worker root is the parent directory of `HOME`. Every path below is relative to it.
* A hook that exits non-zero, runs past its timeout or leaves a process behind fails the step.
* A hook starts with `umask 077` and creates its output directory when it is missing. Output must
  be private regular files with one link (mode 0600 in 0700 directories, which is what that umask
  gives); a symbolic link, a special file, another mode or a file outside the expected set fails
  the step.

| Hook | Account, checkout | Extra environment | Reads | Must write |
|---|---|---|---|---|
| `derive_plan` | validator, `controller/` | none | `validation-input/inventory`, `validation-input/scenario-contract` and one `validation-input/<name>` for every `plan_inputs` entry | `validator-home/validation/plan.json` |
| `policy` | candidate, `repository/` | none | its checkout | nothing (exit status and log) |
| `build_target` | candidate, `repository/` | `MB_TARGET_ID` | its checkout | every planned output of that target at `candidate-home/export/<path>`, and nothing else |
| `verify_target` | validator, `controller/` | `MB_TARGET_ID` | `validation-input/`, `sealed-build/` (that target) | `validator-home/validation/<target id>.json` |
| `verify_build` | validator, `controller/` | none | `validation-input/`, `sealed-build/` (every target) | one `<target id>.json` for every target |
| `derive_runtime` | validator, `controller/` | `MB_LANE_ID` | `validation-input/` | `validator-home/validation/runtime.json` |
| `run_lane` | candidate, `repository/` | `MB_LANE_ID`, `E2E_ROW_JSON`, `E2E_SCENARIOS` | its checkout, the Build at `repository/<bundle.path>` | its native results below `candidate-home/export/` |
| `verify_runtime` | validator, `controller/` | `MB_LANE_ID` | `validation-input/`, `sealed-build/`, `sealed-runtime/` | `validator-home/validation/<lane id>.json` |

`controller/` is the protected copy (the config and `adapter.files`). `repository/` is the tested
commit, so a candidate hook runs the pull request's own copy of the dispatcher; whatever it
produces is checked by the protected hooks.

`validation-input/` holds the bytes of every candidate file at the tested commit: `inventory`,
`scenario-contract` and one file per `plan_inputs` entry under its `name` (which is never
`inventory`, `scenario-contract` or `ci-plan.json`). After `derive_plan` it also holds
`ci-plan.json`. The plan binds each of them by SHA-256: `identity.inventory_sha256` and
`identity.scenario_sha256` for the first two, and `plan_inputs`, a list of `{"name", "sha256"}`
in the config's order, for the others. A hook that uses a candidate file should compare first.
A candidate hook reads the same files from its own checkout, at the paths the config names.
`sealed-build/` and `sealed-runtime/` hold the frozen
exports; `ci-envelope.json` and `ci-runtime-envelope.json` in them belong to the kit and are to be
ignored.

## What each hook writes

**`plan.json`** (at most 4 MiB): exactly `{"targets": [...], "lanes": [...]}`.

* A target is `{"id", "java", "native_contract_sha256", "outputs"}`; an output is
  `{"path", "lane_id", "role"}` with role `production`, `harness`, `sbom`, `native-report` or
  `build-log`. A lane is `{"id", "target_id", "native_contract_sha256", "obligations"}`.
* Ids are lower-case tokens of `a-z 0-9 . _ -` (at most 80 characters, no `--`), not `plan`,
  `runtime` or `ci-validation`. They appear in job names and artifact names. A native runtime row
  id such as `fabric-1_20_1--pr-behavior` holds `--` and cannot be a lane id: name a lane by its
  artifact node (`fabric-1.20.1`) and carry the row id in `E2E_ROW_JSON`.
* Every target has at least one lane and every lane names a target.
* `lane_id` is the lane an output belongs to, or `null` for an output of the target as a whole:
  its staged manifest, a build report, a log, or an SBOM that covers all its lanes.
* Each lane has exactly one `production` and one `harness` output. Neither can be target-scoped.
* An `sbom` is optional: at most one per lane and one for the target as a whole. A mod that
  stages no SBOM plans none.
* Every target has at least one `native-report` (the staged manifest, for example). Further
  native reports and any number of `build-log` outputs are allowed, for a lane or for the target.
* A path is relative to the export: components of ASCII letters, digits, `.`, `_`, `-`, `+` and
  single inner spaces (`Quick Skin - Fabric - 1.20.1-3.1.0.jar`) that neither start nor end with
  a dot or a space.
* Paths are unique in the whole plan, across targets and even when case is ignored. Every
  `build_target` run writes one partition and all partitions are assembled into one Build, so a
  file each target stages under the same name must carry the target in its path: plan
  `targets/<target id>/artifacts.json` and `targets/<target id>/sbom/quick-skin.cdx.json`, never
  a bare `artifacts.json`. The JARs are unique by their own names (`files/…`, `harness/…`).
* No output takes the root-level name `ci-validation.json` or the report name of a unit it is
  uploaded with (`<target id>.json`; for runtime results `<lane id>.json`), in any case, and none
  lies below a directory of such a name: an uploaded artifact holds the kit's validation record
  and the verification reports there, beside the envelope. Planning does not check this yet; the
  job that would upload such an export fails.
* One file is at most 256 MiB as a JAR, 16 MiB as an SBOM or a build log, and as a native report
  8 MiB (profile `block-pops`) or 4 MiB (`quick-skin`).
* No identity, `plan_inputs`, hash of the plan, command, runner or permission: the kit adds the
  identity and the hashes of the candidate files, and rejects any other key.

The same inputs must give the same document: every job of a generation derives the plan again
and the hashes must agree.

**`runtime.json`**: exactly `{"values": {"E2E_ROW_JSON": "...", "E2E_SCENARIOS": "..."}}`. Both are
non-blank text without control characters, at most 128 KiB each. `run_lane` receives them
unchanged as environment variables.

**Build outputs**: the files the plan lists for the target, byte for byte what later steps verify
and ship. A missing or an extra file fails the job.

**Reports** (`<unit id>.json`, at most 4 MiB): a canonical JSON object (sorted keys, `,` and `:`
without spaces, UTF-8, one final newline). Its content is the mod's own. A verification hook must
decode native reports strictly (no duplicate key, closed schema), re-hash what it was given and
exit non-zero on any difference; it writes its report only when everything holds.

## What protected code does afterwards

1. `derive_plan`: parses `plan.json`, adds the authenticated identity, binds the policy digest, the
   inventory's Git blob id and SHA-256, the scenario contract's SHA-256, the SHA-256 of every
   extra plan input and the runtime selection digest, and computes `plan_sha256`.
2. `build_target` and `run_lane`: terminates and locks the account, proves the tracked sources are
   unchanged, freezes the export and writes its envelope. The Build file set must equal the plan.
3. `verify_*`: requires exactly the expected reports, freezes them and records them in the
   validation record that is uploaded with the sealed export.
4. Gates and status publication use only these sealed records, never a hook's exit text.
