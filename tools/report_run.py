#!/usr/bin/env python3
"""Report one run to Home Assistant, with the harness version computed for you.

The harness version is a short SHA-256 over the files that make up the
harness: a rules file, a hooks directory, a settings file, whatever you name
with --harness. Change any of them and the next run lands on a new version,
which is what makes a before-and-after comparison possible without anyone
remembering to bump a label.

    python tools/report_run.py --harness CLAUDE.md --harness ~/.claude/hooks \
        --outcome pass --task-id hacs-audit --turns 14 --duration 640

Delivery, one of:
  HA_URL and HA_TOKEN in the environment  -> the record_run action over REST,
                                             which needs --entry-id
  --webhook https://homeassistant.local:8123/api/webhook/<id> -> a POST, no token

--print-version computes the fingerprint and exits, so two machines can check
they run the same harness. Exit codes: 0 recorded, 1 refused by Home
Assistant, 2 bad arguments or nothing reachable.

Standard library only, so it runs wherever the agent does.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

FIELDS = (
    "task_id",
    "task_class",
    "verified",
    "turns",
    "tool_calls",
    "duration_s",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "denials",
    "retries",
    "interventions",
    "notes",
)


def fingerprint(paths: list[str], label: str | None) -> str:
    """sha256 over the named files, in a stable order, as `sha256:<12 hex>`.

    Directories are walked; hidden files and caches are skipped. Each file
    contributes its relative path and its bytes, so a rename changes the
    version as a content edit does.
    """
    h = hashlib.sha256()
    for root in sorted(paths):
        root = os.path.expanduser(root)
        if os.path.isdir(root):
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(
                    d for d in dirnames if not d.startswith((".", "__"))
                )
                for name in sorted(filenames):
                    if name.startswith(".") or name.endswith((".pyc", ".bak")):
                        continue
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, root).replace(os.sep, "/")
                    h.update(rel.encode())
                    with open(full, "rb") as fh:
                        h.update(fh.read())
        elif os.path.isfile(root):
            h.update(os.path.basename(root).encode())
            with open(root, "rb") as fh:
                h.update(fh.read())
        else:
            raise SystemExit(f"--harness {root}: no such file or directory")
    digest = "sha256:" + h.hexdigest()[:12]
    return f"{label} {digest}" if label else digest


def build_run(args: argparse.Namespace) -> dict[str, object]:
    run: dict[str, object] = {
        "harness_version": args.harness_version
        or fingerprint(args.harness, args.label),
        "outcome": args.outcome,
    }
    for field in FIELDS:
        value = getattr(args, field)
        if value is not None:
            run[field] = value
    return run


def _post(
    url: str, body: dict[str, object], headers: dict[str, str], insecure: bool
) -> tuple[int, str]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--harness",
        action="append",
        default=[],
        metavar="PATH",
        help="file or directory that is part of the harness; repeatable",
    )
    p.add_argument("--label", help="prefix for the computed version, e.g. a git branch")
    p.add_argument(
        "--harness-version", help="use this version instead of computing one"
    )
    p.add_argument(
        "--print-version", action="store_true", help="print the version and exit"
    )
    p.add_argument("--outcome", choices=("pass", "fail", "partial"))
    p.add_argument("--task-id")
    p.add_argument("--task-class")
    p.add_argument("--verified", action="store_true", default=None)
    p.add_argument("--turns", type=int)
    p.add_argument("--tool-calls", type=int)
    p.add_argument("--duration", dest="duration_s", type=float, metavar="SECONDS")
    p.add_argument("--input-tokens", type=int)
    p.add_argument("--output-tokens", type=int)
    p.add_argument("--cost", dest="cost_usd", type=float, metavar="USD")
    p.add_argument("--denials", type=int)
    p.add_argument("--retries", type=int)
    p.add_argument("--interventions", type=int)
    p.add_argument("--notes")
    p.add_argument("--entry-id", help="config entry id, for the action route")
    p.add_argument(
        "--webhook", metavar="URL", help="post here instead of calling the action"
    )
    p.add_argument(
        "--insecure", action="store_true", help="skip TLS verification (self-signed)"
    )
    p.add_argument("--dry-run", action="store_true", help="print the record and exit")
    args = p.parse_args()

    if not args.harness and not args.harness_version:
        p.error(
            "name the harness with --harness PATH (repeatable), "
            "or give --harness-version"
        )
    if args.print_version:
        print(args.harness_version or fingerprint(args.harness, args.label))
        return 0
    if not args.outcome:
        p.error("--outcome is required")

    run = build_run(args)
    if args.dry_run:
        print(json.dumps(run, indent=2))
        return 0

    if args.webhook:
        status, body = _post(args.webhook, run, {}, args.insecure)
    else:
        url, token = os.environ.get("HA_URL"), os.environ.get("HA_TOKEN")
        if not url or not token:
            p.error("set HA_URL and HA_TOKEN, or give --webhook")
        if not args.entry_id:
            p.error("--entry-id is required for the action route")
        status, body = _post(
            url.rstrip("/")
            + "/api/services/agent_harness_performance_tracker/record_run"
            + "?return_response",
            {"config_entry_id": args.entry_id, **run},
            {"Authorization": f"Bearer {token}"},
            args.insecure,
        )
    print(f"{status} {body.strip()[:300]}")
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
