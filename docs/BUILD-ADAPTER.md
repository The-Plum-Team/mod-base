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
| `bundle.path` | the directory, relative to a lane's checkout, where the verified Build is staged before `run_lane`. It must not lie inside `out/mod-base-kit` (or contain it), and the tested tree must not track it |
| `contexts.build`, `contexts.packaged` | the two required status contexts |
| `timeouts` | `validator_seconds` for every protected hook, `policy_seconds`, `target_seconds` and `runtime_seconds` for the three candidate hooks |
| `runtime.system_profile` | optional: the kit system profile a lane job installs as root before its accounts exist, by name. `xvfb-mesa` is Xvfb, `xauth`, Mesa's software GL and EGL and the audio and X client libraries a Minecraft client loads: the union of both mods' native installs. The mod never lists packages; without `runtime` nothing is installed, and `runtime` without a profile, an unknown profile or another key is refused |

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
* A candidate hook (`policy`, `build_target`, `run_lane`) also gets `MB_JAVA_HOMES`: every JDK
  home the job installed, in the job's order, joined with `:`. The first one is `JAVA_HOME`, and
  only its `bin` is on `PATH`; a build that needs several toolchains hands the list to its build
  tool (for Gradle, `org.gradle.java.installations.paths` takes it with `,` for `:`). The
  variable is absent when the job installs no JDK. Protected hooks get `JAVA_HOME` alone.
* The worker root is the parent directory of `HOME`. Every path below is relative to it.
* A hook that exits non-zero, runs past its timeout or leaves a process behind fails the step.
* A hook starts with `umask 077` and creates its output directory when it is missing. Output must
  be private regular files with one link (mode 0600 in 0700 directories, which is what that umask
  gives); a symbolic link, a special file, another mode or a file outside the expected set fails
  the step.

| Hook | Account, checkout | Extra environment | Reads | Must write |
|---|---|---|---|---|
| `derive_plan` | validator, `controller/` | none | `validation-input/inventory`, `validation-input/scenario-contract` and one `validation-input/<name>` for every `plan_inputs` entry | `validator-home/validation/plan.json` |
| `policy` | candidate, `repository/` | `MB_JAVA_HOMES` | its checkout | nothing (exit status and log) |
| `build_target` | candidate, `repository/` | `MB_TARGET_ID`, `MB_JAVA_HOMES` | its checkout | every planned output of that target at `candidate-home/export/<path>`, and nothing else |
| `verify_target` | validator, `controller/` | `MB_TARGET_ID` | `validation-input/`, `sealed-build/` (that target) | `validator-home/validation/<target id>.json` |
| `verify_build` | validator, `controller/` | none | `validation-input/`, `sealed-build/` (every target) | one `<target id>.json` for every target |
| `derive_runtime` | validator, `controller/` | `MB_LANE_ID` | `validation-input/` | `validator-home/validation/runtime.json` |
| `run_lane` | candidate, `repository/` | `MB_LANE_ID`, `E2E_ROW_JSON`, `E2E_SCENARIOS`, `MB_JAVA_HOMES` | its checkout, the Build at `repository/<bundle.path>` | its native results below `candidate-home/export/` |
| `verify_runtime` | validator, `controller/` | `MB_LANE_ID` | `validation-input/`, `sealed-build/`, `sealed-runtime/` | `validator-home/validation/<lane id>.json` |

`controller/` is the protected copy (the config and `adapter.files`). `repository/` is the tested
commit, so a candidate hook runs the pull request's own copy of the dispatcher; whatever it
produces is checked by the protected hooks.

A job runs one candidate hook, once. Its checkout `repository/` is the candidate account's own
private copy of the tested commit: every tracked file with its Git mode, a `.git` that holds the
tested commit detached (no credential, no hook, an `origin` without a token), and the kit the
protected branch pins at `out/mod-base-kit` with its stamp, where the managed bootstrap looks for
a staged kit. Nothing else is there, and the hook reaches nothing of the runner: not the original
checkout, not the workspace, not a token. When the job restored a Gradle cache,
`GRADLE_USER_HOME` starts as a private copy of its `caches/` and `wrapper/` directories.

For `run_lane` the checkout also holds the Build at `<bundle.path>`: the candidate's own copy of
every file of the complete, verified Build under the names the plan gives them
(`build/release/files/…`, `build/release/targets/<target id>/artifacts.json`, …) and no kit
document.

**What a candidate hook may leave in its checkout.** After the hook, protected code compares the
checkout with the tested commit: every tracked file must still have its bytes and its mode, and
nothing untracked may exist outside two directories, `out/mod-base-kit` and `<bundle.path>`. A
build writes everything else it produces below its home (`HOME`, `TMPDIR`, `GRADLE_USER_HOME`) or
below one of those two directories; an untracked `build/` or `.gradle/` beside them fails the
step, also when the hook exited zero. A directory that only leads to `<bundle.path>` (`build/` for
`build/release`) may exist.

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
and ship. A missing or an extra file fails the job, and so does an empty one, a link, a file with
a second name or a `ci-envelope.json`: the kit writes that document itself, from the plan and
the bytes it finds.

**Runtime results**: `run_lane` writes at least one file below `export/` and at most 512 files and
256 MiB in all, under export paths (the same rule as plan paths). The plan does not list them, so
the kit records what it finds and gives every file a role by its name alone: a file below a
directory called `crash-reports` is a crash report (at most 16 MiB, may be empty), a `.png` a
screenshot (32 MiB, not empty), a `.json` a native report (4 MiB, not empty) and every other
file a runtime log (16 MiB, may be empty). A role only bounds the file; what a result means is
for `verify_runtime` to decide. The synthetic mod writes everything below `lanes/<lane id>/`,
which keeps the results of two lanes apart for whoever reads them side by side.

**Policy**: `policy` writes nothing and is judged by its exit status alone. Protected code cannot
count the tests of a suite it does not run, so a mod that wants every discovered test accounted
for runs its suites through the pinned kit's own runner, which fails closed on a discovery error,
a failure, an unexpected success, a dead worker, zero tests or a unit that ran another number of
tests than it discovered:
`PYTHONPATH=out/mod-base-kit/src <python> out/mod-base-kit/tools/parallel_unittest.py --policy-profile <profile> …`.

**Reports** (`<unit id>.json`, at most 4 MiB): a canonical JSON object (sorted keys, `,` and `:`
without spaces, UTF-8, one final newline). Its content is the mod's own. A verification hook must
decode native reports strictly (no duplicate key, closed schema), re-hash what it was given and
exit non-zero on any difference; it writes its report only when everything holds.

## What protected code does around a hook

Every step below is one `ci` command of a job; [BUILD-PROTOCOL.md](BUILD-PROTOCOL.md) describes
the root operations and the state records behind them. Allocated accounts are terminated and
locked between hooks; cleanup also revokes their deferred execution and fails closed on error.

1. **Plan** (`ci plan`, every job). Protected code stages the candidate files in
   `validation-input/`, runs `derive_plan`, parses `plan.json`, adds the authenticated identity,
   binds the policy digest, the inventory's Git blob id and SHA-256, the scenario contract's
   SHA-256, the SHA-256 of every extra plan input and the runtime selection digest, and computes
   `plan_sha256`. The result is `validation-input/ci-plan.json` for every later protected hook.
2. **Stage** (`ci worker-stage`, a job with a candidate hook). The runner's checkout must be
   detached at the tested commit and hold exactly the tested tree: an untracked or ignored file
   stops the job before anything is copied. Root then gives the candidate account the
   `repository/` described above (the tracked files, the curated `.git`, the kit at
   `out/mod-base-kit`), seeds `GRADLE_USER_HOME` when `--gradle-seed` is supplied and, in a lane
   job, copies the complete Build of `sealed-build/` to `repository/<bundle.path>`. Nothing of the
   candidate has run at this point.
   Current workflows supply no seed and start with empty Gradle homes; cache restore/save and
   native lane system packages before fencing remain adoption work. The overlay contains the
   protected executing pin. A candidate future-pin bump currently fails bootstrap stamp matching
   and needs separate admission/staging before Q/B adopts that upgrade route.
3. **Run** (`ci worker-run`). A job runs one candidate hook:
   * `policy` receives its checkout and no extra variable.
   * `build_target` receives its checkout and `MB_TARGET_ID`.
   * `run_lane` receives its checkout with the Build, `MB_LANE_ID`, `E2E_ROW_JSON` and
     `E2E_SCENARIOS`. The two values come from `derive_runtime`, which runs first, in the same
     step, as the validator: it receives `MB_LANE_ID`, reads `validation-input/` (the plan and
     the candidate files) and writes `runtime.json`. Root hands that file to the runner and
     removes it from the validator's output directory, so the lane's `verify_runtime` starts with
     an empty one; protected code decodes it strictly and passes both values on unchanged. A
     `derive_runtime` that fails ends the step before the candidate runs.

   All three also receive `MB_JAVA_HOMES` when the job installed a JDK, and every home in it is
   checked against the tool trees the job admitted before the hook starts. The hook runs with the
   timeout the protected config sets for it (`policy_seconds`, `target_seconds` or
   `runtime_seconds`) and its log is printed with every line prefixed and neutralised. The step
   fails when the hook exits non-zero, runs past its timeout or leaves a process behind, and it
   records how the hook ended either way.
4. **Seal** (`ci worker-seal`, also after a run that failed). The candidate is terminated and
   locked before anything is read. A hook that failed seals nothing. After a hook that succeeded,
   root compares the checkout with the tested commit ("What a candidate hook may leave in its
   checkout" above) and then
   * for `policy` does nothing more: the unchanged sources and the exit status are its result;
   * for `build_target` requires `candidate-home/export/` to hold exactly the planned outputs of
     the target, copies them into `sealed-build/` and writes `ci-envelope.json` there from the
     plan and the bytes it copied;
   * for `run_lane` copies the results in `candidate-home/export/` into `sealed-runtime/` and
     writes `ci-runtime-envelope.json` there, bound to the Build the lane was staged.

   The sealed copy belongs to the runner alone. The files the hook wrote stay the candidate's and
   no later step reads them.
5. **Verify** (`ci worker-validate`). Root hands the sealed export to the validator read-only and
   the `verify_*` hook of the job runs. Root then requires `validator-home/validation/` to hold
   exactly the expected reports, freezes them and writes the validation record. The step writes
   the directory the job uploads: the sealed export with its envelope, the validation record and
   the reports.
6. Gates and status publication use only these sealed records, never a hook's exit text.
