"""The canary producer's generator entry point: synthetic demonstration evidence, not a product.

Run from the canary checkout with the pinned kit on ``PYTHONPATH`` (the ``setup`` composite exports
``MOD_BASE_KIT_PATH``)::

    PYTHONPATH="$MOD_BASE_KIT_PATH/src" python3 -P scripts/canary/produce.py keys
    PYTHONPATH="$MOD_BASE_KIT_PATH/src" python3 -P scripts/canary/produce.py synthesize --key K --output DIR
    PYTHONPATH="$MOD_BASE_KIT_PATH/src" python3 -P scripts/canary/produce.py family --key K \\
        --run-json RUN.json --output DIR

``keys`` prints the adapter's keys as a JSON array. ``synthesize`` writes the packaged-output
stand-in of one key (the fixtures hook ``synthesize``, dispatched in-process through the kit's
``host_child.run_hook`` with the kit's deterministic ``pattern_png``). ``family`` writes the native
``demo-pairs`` bundle of one key produced by this run: ``RUN.json`` is this run's own attempt as
the API reports it (``gh api repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/attempts/
$GITHUB_RUN_ATTEMPT``), from which the producer ``RunRecord`` the Pages build re-reads is built.
Every hook sees the checked-out head as ``ctx.implementation_sha``; nothing here uses a token.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mod_base.adapter import host_child
from mod_base.adapter.api import Context
from mod_base.config import load_config
from mod_base.evidence.expectation import tested_run_projection
from mod_base.imaging.png import pattern_png
from mod_base.model.canonical import read_json_file
from mod_base.model.documents import own_run_record, run_claim_from_environment

FAMILY = "demo-pairs"
MAX_RUN_JSON_BYTES = 1 << 20


def _head(root: Path) -> str:
    completed = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True,
                               timeout=60)
    return completed.stdout.decode("ascii").strip()


class Producer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = load_config(root)
        self.head = _head(root)
        self.adapter = host_child.load_adapter(root / self.config.adapter["path"])
        self.fixtures = host_child.load_adapter(root / self.config.adapter["fixtures_path"])

    def context(self, tmpdir: str) -> Context:
        return Context(repo_root=self.root, config=self.config, tmpdir=Path(tmpdir), implementation_sha=self.head)

    def hook(self, name: str, arguments: dict, module=None) -> object:
        with tempfile.TemporaryDirectory(prefix="canary-hook-") as tmpdir:
            factory = pattern_png if module is self.fixtures else None
            return host_child.run_hook(self.context(tmpdir), module or self.adapter, name, arguments,
                                       image_factory=factory)

    def target(self, key: str) -> dict:
        matches = [target for target in self.hook("targets", {"branches": None}) if target["key"] == key]
        if len(matches) != 1:
            raise SystemExit(f"the canary declares no key {key}")
        return matches[0]

    def expectation(self, target: dict) -> dict:
        projection = tested_run_projection({"branch": os.environ["GITHUB_REF_NAME"]}, os.environ["GITHUB_EVENT_NAME"])
        return self.hook("expectation", {"target": target, "tested_run": projection, "extensions": {}})


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="produce.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("keys")
    synthesize = commands.add_parser("synthesize")
    synthesize.add_argument("--key", required=True)
    synthesize.add_argument("--output", type=Path, required=True)
    family = commands.add_parser("family")
    family.add_argument("--key", required=True)
    family.add_argument("--run-json", type=Path, required=True)
    family.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    producer = Producer(Path.cwd())
    if arguments.command == "keys":
        print(json.dumps([target["key"] for target in producer.hook("targets", {"branches": None})]))
        return 0
    target = producer.target(arguments.key)
    expectation = producer.expectation(target)
    arguments.output.mkdir(parents=True)
    if arguments.command == "synthesize":
        producer.hook("synthesize", {"target": target, "expectation": expectation,
                                     "out_root": str(arguments.output.resolve())}, module=producer.fixtures)
        return 0
    run, _ = read_json_file(arguments.run_json.resolve(), label="producer run", max_bytes=MAX_RUN_JSON_BYTES)
    claim = run_claim_from_environment(os.environ)
    if (run.get("id"), run.get("run_attempt"), run.get("head_sha")) != (claim["run_id"], claim["run_attempt"],
                                                                        claim["commit"]):
        raise SystemExit("the run JSON is not this producer attempt")
    record = own_run_record({**claim, "event": run["event"], "created_at": run["created_at"], "conclusion": "success",
                             "head_sha": run["head_sha"], "display_title": run["display_title"]}, "$.producer")
    with tempfile.TemporaryDirectory(prefix="canary-family-") as tmpdir:
        producer.fixtures.family_bundle(producer.context(tmpdir), family=FAMILY, key=arguments.key, target=target,
                                        expectation=expectation, producer=record,
                                        out_root=str(arguments.output.resolve()), image_factory=pattern_png,
                                        outcome="available")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
