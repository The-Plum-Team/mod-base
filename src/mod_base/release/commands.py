"""The ``release`` command (docs/RELEASE-PROTOCOL.md): one verb per step of the release caller.

``release plan`` runs in the kit callee's read-only job, after the generation consumer's steps;
the others run in the caller's own jobs. ``admit``, ``github``, ``upload`` and ``verify --plan`` run
in the ``publish`` job, in the environment the release config names, each step holding only its own
credential: ``GH_TOKEN`` (the job token, ``contents: write``) everywhere, ``MODRINTH_TOKEN`` and
``CURSEFORGE_TOKEN`` in their upload step alone. ``verify --drafts`` runs in the scheduled ``finish``
job, which holds no secret. Those jobs run the kit only through the protected head's managed
bootstrap, and no verb but ``plan`` reads the mod checkout.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from mod_base import cli, runtime
from mod_base.build_ci.records import validate_generation
from mod_base.errors import MbError
from mod_base.github import api as github_api
from mod_base.github import artifacts
from mod_base.io.bounded_zip import ExtractionLimits
from mod_base.io.secure_json import read_json
from mod_base.marketplace import curseforge, github_releases, modrinth
from mod_base.marketplace.transport import HttpsTransport
from mod_base.model import grammar
from mod_base.release import publish
from mod_base.release.config import load_release_config
from mod_base.release.plan import (MAX_RELEASE_ASSETS, MAX_RELEASE_ASSET_BYTES, admit_release, derive_release_plan,
                                   plan_digest, released_commit, seal_release)

#: What ``release admit`` extracts: the plan plus one file per asset.
PLAN_EXTRACTION = ExtractionLimits(max_entries=MAX_RELEASE_ASSETS + 1,
                                   max_total_bytes=MAX_RELEASE_ASSETS * MAX_RELEASE_ASSET_BYTES,
                                   max_entry_bytes=MAX_RELEASE_ASSET_BYTES)
#: The plan digest the plan job outputs: 64 lowercase hex characters, no algorithm prefix.
PLAN_SHA256 = cli.typed(lambda value: grammar.require(grammar.SHA256, value, "plan SHA-256"), "SHA-256")
#: The ``generation.json`` of ``ci select-generation`` (SCHEMAS.md: at most 4 MiB).
MAX_GENERATION_BYTES = 4 * 1024 * 1024
#: Requests ``release plan`` and ``release admit`` may spend: they read the default branch and one
#: run's artifact listing.
MAX_RELEASE_ADMIT_REQUESTS = 16


def register(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``release`` command and its verbs."""

    command = subparsers.add_parser("release", help="plan and publish a release from a tested generation")
    verbs = command.add_subparsers(dest="verb", required=True, parser_class=cli.KitArgumentParser)

    plan = verbs.add_parser("plan", help="derive and seal the release plan of the live head's generation")
    plan.add_argument("--repo", type=cli.PATH, required=True, metavar="DIR", help="the protected mod checkout")
    plan.add_argument("--release-config", type=cli.PATH, required=True, metavar="FILE")
    plan.add_argument("--generation", type=cli.PATH, required=True, metavar="DIR",
                      help="the output of `ci select-generation --with-build`")
    plan.add_argument("--output", type=cli.PATH, required=True, metavar="DIR")
    plan.add_argument("--github-output", type=cli.PATH, required=True, metavar="FILE")
    plan.set_defaults(handler=run_plan)

    admit = verbs.add_parser("admit", help="download and admit this run attempt's sealed plan")
    admit.add_argument("--plan-sha256", type=PLAN_SHA256, required=True)
    admit.add_argument("--plan-attempt", type=cli.POSITIVE, required=True,
                       help="the run attempt of the plan job, whose artifact holds the plan")
    admit.add_argument("--release-config", type=cli.PATH, required=True, metavar="FILE",
                       help="the release config of the protected checkout")
    admit.add_argument("--environment", required=True,
                       help="the deployment environment this job runs in, which the config must name")
    admit.add_argument("--output", type=cli.PATH, required=True, metavar="DIR")
    admit.set_defaults(handler=run_admit)

    github = verbs.add_parser("github", help="create the tags and draft releases of an admitted plan")
    github.add_argument("--plan", type=cli.PATH, required=True, metavar="DIR")
    github.set_defaults(handler=run_github)

    upload = verbs.add_parser("upload", help="upload every lane of an admitted plan to one marketplace")
    upload.add_argument("--plan", type=cli.PATH, required=True, metavar="DIR")
    upload.add_argument("--marketplace", choices=publish.MARKETPLACES, required=True)
    upload.set_defaults(handler=run_upload)

    verify = verbs.add_parser("verify", help="reconcile and publish the releases of a plan or every recorded draft")
    source = verify.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", type=cli.PATH, metavar="DIR")
    source.add_argument("--drafts", action="store_true")
    verify.add_argument("--wait-seconds", type=int, default=0)
    verify.set_defaults(handler=run_verify)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise MbError(f"{name} is required for this command", reason="environment")
    return value


def _github(environ: Mapping[str, str], *, writable: bool) -> tuple[HttpsTransport, HttpsTransport]:
    token = _required(environ, "GH_TOKEN")
    headers = github_releases.headers(token)
    return (HttpsTransport(github_releases.API_ORIGIN, headers=headers, max_requests=publish.MAX_RELEASE_REQUESTS,
                           writable=writable),
            HttpsTransport(github_releases.UPLOADS_ORIGIN, headers=headers,
                           max_requests=publish.MAX_RELEASE_REQUESTS, writable=writable))


def run_plan(args: argparse.Namespace) -> int:
    environ = cli.environ()
    generation_dir = args.generation
    generation = validate_generation(read_json(generation_dir / "generation.json", label="generation.json",
                                               max_bytes=MAX_GENERATION_BYTES))
    config, config_sha256 = load_release_config(args.release_config)
    if config["repository"] != _required(environ, "GITHUB_REPOSITORY"):
        raise MbError("the release config names another repository", reason="release")
    executing = runtime.kit_ref(_required(environ, "MOD_BASE_KIT_SHA"))
    kit = {"sha": executing["sha"], "version": executing["version"],
           "tree_digest": _required(environ, "MB_KIT_TREE_DIGEST")}
    plan = derive_release_plan(generation=generation, bundle=generation_dir / "build", config=config,
                               config_sha256=config_sha256, repo=args.repo, kit=kit)
    if released_commit(plan) != grammar.require_sha1(_required(environ, "GITHUB_SHA"), "GITHUB_SHA"):
        raise MbError("the generation covers another commit than this run", reason="release")
    seal_release(args.output, plan=plan, bundle=generation_dir / "build")
    cli.write_github_output(args.github_output, {"plan_sha256": plan_digest(plan), "version": plan["version"],
                                                 "tags": " ".join(tag["tag"] for tag in plan["tags"]),
                                                 "environment": config["environment"]})
    print(f"Release {plan['version']}: {len(plan['tags'])} tag(s), "
          f"{sum(len(tag['assets']) for tag in plan['tags'])} file(s), plan {plan_digest(plan)}")
    return 0


def run_admit(args: argparse.Namespace) -> int:
    environ = cli.environ()
    run_id = int(_required(environ, "GITHUB_RUN_ID"))
    attempt = args.plan_attempt
    if attempt > int(_required(environ, "GITHUB_RUN_ATTEMPT")):
        raise MbError("the plan attempt is later than this run attempt", reason="release")
    sha = grammar.require_sha1(_required(environ, "GITHUB_SHA"), "GITHUB_SHA")
    config, config_sha256 = load_release_config(args.release_config)
    if config["environment"] != args.environment:
        raise MbError(f"this job runs in the environment {args.environment!r}, not the release config's "
                      f"{config['environment']!r}", reason="release")
    client = github_api.from_environment(environ, max_requests=MAX_RELEASE_ADMIT_REQUESTS)
    name = grammar.release_plan_name(run_id, attempt)
    found = [artifact for artifact in artifacts.list_run_named(client, run_id, name) if not artifact.expired]
    if len(found) != 1:
        raise MbError(f"this run attempt holds {len(found)} artifacts named {name}, not one", reason="release")
    artifact = found[0]
    with tempfile.TemporaryDirectory(prefix="mb-release-admit-") as temporary:
        staged = Path(temporary) / "plan"
        artifacts.download(client, artifact_id=artifact.id, name=name, digest=artifact.digest, size=artifact.size,
                           run_id=run_id, output=staged, extraction=PLAN_EXTRACTION)
        plan = admit_release(staged, plan_sha256=args.plan_sha256)
        if released_commit(plan) != sha:
            raise MbError("the sealed plan releases another commit than this run", reason="release")
        if plan["config_sha256"] != config_sha256:
            raise MbError("the sealed plan was derived from another release config", reason="release")
        repository = _required(environ, "GITHUB_REPOSITORY")
        branch = client.get_json(f"/repos/{repository}")["default_branch"]
        head = client.get_json(f"/repos/{repository}/commits/{branch}")["sha"]
        if head != sha:
            raise MbError(f"the default branch moved to {head} since the release was planned; dispatch it again",
                          reason="release")
        shutil.copytree(staged, args.output)
    print(f"Admitted release {plan['version']} at {sha} ({plan_digest(plan)})")
    return 0


def run_github(args: argparse.Namespace) -> int:
    environ = cli.environ()
    api, uploads = _github(environ, writable=True)
    services = publish.Services(github=api, github_uploads=uploads)
    entries = publish.create_releases(services, repository=_required(environ, "GITHUB_REPOSITORY"),
                                      plan_dir=args.plan)
    for entry in entries:
        print(f"{entry.tag['tag']}: {'draft' if entry.release.get('draft') else 'published'} release "
              f"{entry.release.get('id')}")
    return 0


def run_upload(args: argparse.Namespace) -> int:
    environ = cli.environ()
    api, _uploads = _github(environ, writable=True)
    if args.marketplace == "modrinth":
        token = _required(environ, "MODRINTH_TOKEN")
        services = publish.Services(github=api, modrinth=HttpsTransport(
            modrinth.MODRINTH_ORIGIN, headers=modrinth.headers(token), max_requests=publish.MAX_RELEASE_REQUESTS,
            writable=True))
    else:
        token = _required(environ, "CURSEFORGE_TOKEN")
        services = publish.Services(
            github=api,
            curseforge_upload=HttpsTransport(curseforge.UPLOAD_ORIGIN, headers=curseforge.headers(token),
                                             max_requests=publish.MAX_RELEASE_REQUESTS, writable=True),
            curseforge_listing=HttpsTransport(curseforge.LISTING_ORIGIN, max_requests=publish.MAX_RELEASE_REQUESTS),
            curseforge_cdn=HttpsTransport(curseforge.CDN_ORIGIN, max_requests=publish.MAX_RELEASE_REQUESTS))
    states = publish.upload(services, repository=_required(environ, "GITHUB_REPOSITORY"), plan_dir=args.plan,
                            marketplace=args.marketplace)
    for row, state in states.items():
        print(f"{row}: {state}")
    return 0


def _readers(api: HttpsTransport) -> publish.Services:
    """The services a verification needs: the job token for GitHub and only public marketplace
    endpoints, without any marketplace credential."""

    return publish.Services(
        github=api,
        modrinth=HttpsTransport(modrinth.MODRINTH_ORIGIN, max_requests=publish.MAX_RELEASE_REQUESTS),
        curseforge_listing=HttpsTransport(curseforge.LISTING_ORIGIN, max_requests=publish.MAX_RELEASE_REQUESTS),
        curseforge_cdn=HttpsTransport(curseforge.CDN_ORIGIN, max_requests=publish.MAX_RELEASE_REQUESTS))


def run_verify(args: argparse.Namespace) -> int:
    environ = cli.environ()
    api, _uploads = _github(environ, writable=True)
    repository = _required(environ, "GITHUB_REPOSITORY")
    services = _readers(api)
    if args.drafts:
        if args.wait_seconds:
            raise MbError("--wait-seconds applies to --plan only", reason="usage")
        outcome = publish.finish_drafts(services, repository=repository)
        for tag, done in sorted(outcome.items()):
            print(f"{tag}: {'published' if done else 'still pending moderation'}")
        if not outcome:
            print("No draft release carries a release ledger.")
        return 0
    done = publish.verify(services, repository=repository, plan_dir=args.plan, wait_seconds=args.wait_seconds)
    print("Every release is published." if done else
          "Some files are still in moderation; the hourly schedule publishes their releases once they are accepted.")
    return 0


if __name__ == "__main__":  # pragma: no cover - the kit's entry point is `python3 -m mod_base`
    sys.exit(cli.main(["release", *sys.argv[1:]]))
