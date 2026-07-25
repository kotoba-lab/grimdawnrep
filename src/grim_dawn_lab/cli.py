from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from grim_dawn_lab.doctor import create_manifest
from grim_dawn_lab.combat import calculate_single_hit
from grim_dawn_lab.dataset import build_dataset_from_dbr_roots, diff_datasets, extract_and_build_dataset, stable_input_manifest, write_versioned_dataset
from grim_dawn_lab.timeline import compare_observation, simulate_attack_sequence
from grim_dawn_lab.gdc import discover_player_gdc, import_player_gdc
from grim_dawn_lab.build import build_from_gdc, resolve_baseline_defenses
from grim_dawn_lab.grimtools import build_from_grimtools_upload_response, compare_same_save_builds, fetch_grimtools_build
from grim_dawn_lab.advisor import analyze_encounters, render_advisor_markdown, scenarios_from_dataset
from grim_dawn_lab.release import audit_git_distribution


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grim-dawn-lab", allow_abbrev=False)
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
    single_hit = subparsers.add_parser("single-hit", help="explain one incoming damage packet")
    single_hit.add_argument("--build", type=Path, required=True, help="BuildDefenseSnapshot JSON")
    single_hit.add_argument("--skill", type=Path, required=True, help="EnemySkill JSON")
    single_hit.add_argument("--output", type=Path, help="write JSON to this path instead of stdout")
    dataset = subparsers.add_parser("dataset-build", help="build a deterministic dataset from extracted DBRs")
    dataset.add_argument("--base", type=Path, required=True)
    dataset.add_argument("--gdx1", type=Path)
    dataset.add_argument("--gdx2", type=Path)
    dataset.add_argument("--gdx3", type=Path)
    dataset.add_argument("--select", action="append", required=True, help="root record id; repeatable")
    dataset.add_argument("--localization-en", type=Path, action="append", help="EN ARC in layer order; repeatable")
    dataset.add_argument("--localization-ja", type=Path, action="append", help="JA ARC in layer order; repeatable")
    dataset.add_argument("--output-root", type=Path, required=True)
    dataset.add_argument("--enemy-level", type=int, default=100)
    dataset.add_argument("--difficulty", choices=("normal", "elite", "ultimate"), default="ultimate")
    dataset.add_argument("--player-count", type=int, choices=(1, 2, 3, 4), default=1)
    dataset.add_argument("--input-manifest", type=Path, help="doctor manifest required for reproducible game-derived datasets")
    dataset_diff = subparsers.add_parser("dataset-diff", help="compare two generated dataset JSON files")
    dataset_diff.add_argument("--previous", type=Path, required=True)
    dataset_diff.add_argument("--current", type=Path, required=True)
    dataset_diff.add_argument("--output", type=Path)
    extract = subparsers.add_parser("dataset-extract", help="regenerate a dataset from a game install read-only")
    extract.add_argument("--install-path", type=Path, required=True)
    extract.add_argument("--work-root", type=Path, default=Path("data/generated/extracted"))
    extract.add_argument("--output-root", type=Path, default=Path("data/generated/datasets"))
    extract.add_argument("--select", action="append", required=True)
    extract.add_argument("--enemy-level", type=int, default=100)
    extract.add_argument("--channel", choices=("unknown", "stable", "public_test"), default="unknown")
    extract.add_argument("--difficulty", choices=("normal", "elite", "ultimate"), default="ultimate")
    extract.add_argument("--player-count", type=int, choices=(1, 2, 3, 4), default=1)
    sequence = subparsers.add_parser("sequence", help="simulate and classify an attack sequence")
    sequence.add_argument("--build", type=Path, required=True)
    sequence.add_argument("--attacks", type=Path, required=True)
    sequence.add_argument("--observation", type=Path)
    sequence.add_argument("--output", type=Path)
    save_import = subparsers.add_parser("save-import", help="import a player.gdc read-only")
    save_import.add_argument("--path", type=Path, help="player.gdc; omit only with exactly one discovered save")
    save_import.add_argument("--list", action="store_true", help="list discovered paths without reading saves")
    save_import.add_argument("--redact-name", action="store_true", help="replace the character name in output")
    save_import.add_argument("--raw", action="store_true", help="emit parsed save fields instead of the common Build model")
    save_import.add_argument("--records-base", type=Path, help="extracted Base DBR root for partial equipment defense resolution")
    save_import.add_argument("--records-gdx1", type=Path)
    save_import.add_argument("--records-gdx2", type=Path)
    save_import.add_argument("--output", type=Path)
    grimtools = subparsers.add_parser("grimtools-import", help="import stable public metadata from a shared Grim Tools URL")
    grimtools.add_argument("url")
    grimtools.add_argument("--cache-root", type=Path, default=Path("data/generated/grimtools-cache"))
    grimtools.add_argument("--timeout", type=float, default=10.0)
    grimtools.add_argument("--output", type=Path)
    same_save = subparsers.add_parser("same-save-compare", help="compare a local player.gdc with a saved official Grim Tools upload response")
    same_save.add_argument("--save", type=Path, required=True)
    same_save.add_argument("--grimtools-response", type=Path, required=True, help="JSON returned by the official upload_save.php endpoint")
    same_save.add_argument("--output", type=Path)
    advisor = subparsers.add_parser("advise", help="rank encounter scenarios and defense sensitivities")
    advisor.add_argument("--build", type=Path, required=True)
    advisor_inputs = advisor.add_mutually_exclusive_group(required=True)
    advisor_inputs.add_argument("--scenarios", type=Path)
    advisor_inputs.add_argument("--dataset", type=Path, help="generate scenarios from normalized enemy views")
    advisor.add_argument("--context", type=Path, required=True)
    advisor.add_argument("--output", type=Path)
    advisor.add_argument("--format", choices=("json", "markdown"), default="json")
    release_audit = subparsers.add_parser("release-audit", help="ensure tracked distribution excludes game data and saves")
    release_audit.add_argument("--root", type=Path, default=Path("."))
    release_audit.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        result = create_manifest(args.install_path, channel=args.channel)
        exit_code = 0 if result["install"]["path"] is not None and not result["warnings"] else 2
    elif args.command == "single-hit":
        build = json.loads(args.build.read_text(encoding="utf-8"))
        skill = json.loads(args.skill.read_text(encoding="utf-8"))
        result = calculate_single_hit(build, skill)
        exit_code = 3 if result["warnings"] else 0
    elif args.command == "dataset-build":
        roots = [("base", args.base)]
        if args.gdx1:
            roots.append(("gdx1", args.gdx1))
        if args.gdx2:
            roots.append(("gdx2", args.gdx2))
        if args.gdx3:
            roots.append(("gdx3", args.gdx3))
        localization = {}
        if args.localization_en:
            localization["en"] = args.localization_en
        if args.localization_ja:
            localization["ja"] = args.localization_ja
        if args.input_manifest is None:
            raise SystemExit("dataset-build requires --input-manifest; use doctor to create one")
        input_manifest = json.loads(args.input_manifest.read_text(encoding="utf-8"))
        result = build_dataset_from_dbr_roots(roots, args.select, input_manifest=stable_input_manifest(input_manifest), localization_arcs=localization, enemy_level=args.enemy_level, difficulty=args.difficulty, player_count=args.player_count)
        output = write_versioned_dataset(result, args.output_root)
        result = {"dataset_id": result["dataset_id"], "record_count": len(result["records"]), "output": str(output)}
        exit_code = 0
    elif args.command == "dataset-diff":
        previous = json.loads(args.previous.read_text(encoding="utf-8"))
        current = json.loads(args.current.read_text(encoding="utf-8"))
        result = diff_datasets(previous, current)
        exit_code = 0
    elif args.command == "dataset-extract":
        dataset, output = extract_and_build_dataset(
            args.install_path,
            args.work_root,
            args.output_root,
            args.select,
            channel=args.channel,
            enemy_level=args.enemy_level,
            difficulty=args.difficulty,
            player_count=args.player_count,
        )
        result = {"dataset_id": dataset["dataset_id"], "record_count": len(dataset["records"]), "output": str(output)}
        exit_code = 0
    elif args.command == "sequence":
        build = json.loads(args.build.read_text(encoding="utf-8"))
        attacks = json.loads(args.attacks.read_text(encoding="utf-8"))
        result = simulate_attack_sequence(build, attacks)
        if args.observation:
            observation = json.loads(args.observation.read_text(encoding="utf-8"))
            result["observation_comparison"] = compare_observation(result, observation)
        exit_code = 3 if result["unknowns"] else 0
    elif args.command == "save-import":
        paths = discover_player_gdc()
        if args.list:
            result = {"count": len(paths), "paths": [str(path) for path in paths]}
            exit_code = 0
        else:
            if args.path is None:
                if len(paths) != 1:
                    raise SystemExit(f"--path is required when {len(paths)} saves are discovered")
                args.path = paths[0]
            result = import_player_gdc(args.path)
            if not args.raw:
                result = build_from_gdc(result, include_character_name=not args.redact_name)
                if args.records_base:
                    roots = [("base", args.records_base)]
                    if args.records_gdx1:
                        roots.append(("gdx1", args.records_gdx1))
                    if args.records_gdx2:
                        roots.append(("gdx2", args.records_gdx2))
                    result = resolve_baseline_defenses(result, roots)
            elif args.redact_name:
                result["header"]["character_name"] = "<redacted>"
            exit_code = 0
    elif args.command == "grimtools-import":
        result = fetch_grimtools_build(args.url, cache_root=args.cache_root, timeout=args.timeout)
        exit_code = 3 if result["unknowns"] else 0
    elif args.command == "same-save-compare":
        parsed = import_player_gdc(args.save)
        local = build_from_gdc(parsed)
        payload = json.loads(args.grimtools_response.read_text(encoding="utf-8"))
        uploaded = build_from_grimtools_upload_response(payload, source_hash=local["source"]["hash"])
        result = compare_same_save_builds(local, uploaded)
        result["source"] = {
            "save_hash": local["source"]["hash"],
            "save_read_only_verified": parsed["provenance"]["read_only_verified"],
            "grimtools_response_hash": hashlib.sha256(args.grimtools_response.read_bytes()).hexdigest(),
        }
        exit_code = 0 if result["equivalent_on_comparable_fields"] else 4
    elif args.command == "advise":
        build = json.loads(args.build.read_text(encoding="utf-8"))
        if args.dataset:
            scenarios = scenarios_from_dataset(json.loads(args.dataset.read_text(encoding="utf-8")))
        else:
            scenarios = json.loads(args.scenarios.read_text(encoding="utf-8"))
        context = json.loads(args.context.read_text(encoding="utf-8"))
        result = analyze_encounters(build, scenarios, context)
        exit_code = 3 if result["unknowns"] else 0
    else:
        result = audit_git_distribution(args.root)
        exit_code = 0 if result["safe"] else 4
    rendered = render_advisor_markdown(result) if args.command == "advise" and args.format == "markdown" else json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    output_path = getattr(args, "output", None)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return exit_code
