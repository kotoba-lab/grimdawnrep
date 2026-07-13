from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from grim_dawn_lab.doctor import create_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grim-dawn-lab")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="inspect local game inputs read-only")
    doctor.add_argument("--install-path", type=Path)
    doctor.add_argument(
        "--channel",
        choices=("unknown", "stable", "public_test"),
        default="unknown",
        help="record a user-established game channel; never inferred by default",
    )
    doctor.add_argument("--output", type=Path, help="write JSON to this path instead of stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = create_manifest(args.install_path, channel=args.channel)
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0 if manifest["install"]["path"] is not None and not manifest["warnings"] else 2
