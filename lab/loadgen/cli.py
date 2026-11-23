from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Sequence

from lab.loadgen.generator import generate_schedule, hash_request_sequence
from lab.loadgen.profile_loader import get_profile, load_profiles
from lab.loadgen.runner import execute_load_run


def create_parser() -> argparse.ArgumentParser:
    """Creates argument parser for the lab.loadgen CLI."""
    parser = argparse.ArgumentParser(
        prog="python -m lab.loadgen",
        description="OpsPilot open-loop traffic load generator for lab microservices.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. run command: python -m lab.loadgen run --profile steady_20rps --seed 7 --duration 120
    run_parser = subparsers.add_parser("run", help="Execute an open-loop load profile run")
    run_parser.add_argument(
        "--profile",
        required=True,
        help="Name of predefined profile (from profiles.yml) or path to custom profile YAML",
    )
    run_parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Override RNG seed for reproducible product and quantity selections",
    )
    run_parser.add_argument(
        "--duration",
        type=float,
        default=60.0,
        help="Duration of the load test in seconds (default: 60)",
    )
    run_parser.add_argument(
        "--gateway-url",
        default=None,
        help="Gateway base URL (default: GATEWAY_URL env or http://127.0.0.1:8080)",
    )
    run_parser.add_argument(
        "--output",
        default=None,
        help="Destination path for summary JSON (default: /loadgen/summary.json or local dir)",
    )
    run_parser.add_argument(
        "--profiles-file",
        default=None,
        help="Path to YAML file defining load profiles",
    )

    # 2. list-profiles command
    list_parser = subparsers.add_parser("list-profiles", help="List all available load profiles")
    list_parser.add_argument(
        "--profiles-file",
        default=None,
        help="Path to YAML file defining load profiles",
    )

    # 3. generate command (dry-run schedule generation & hashing)
    gen_parser = subparsers.add_parser(
        "generate", help="Dry-run generation of request sequence without sending traffic"
    )
    gen_parser.add_argument("--profile", required=True, help="Profile name or YAML path")
    gen_parser.add_argument("--seed", type=int, default=None, help="Override RNG seed")
    gen_parser.add_argument(
        "--duration", type=float, default=60.0, help="Duration in seconds"
    )
    gen_parser.add_argument(
        "--profiles-file", default=None, help="Path to YAML file defining load profiles"
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI main entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "run":
        try:
            asyncio.run(
                execute_load_run(
                    profile_name_or_path=args.profile,
                    duration_s=args.duration,
                    seed=args.seed,
                    gateway_url=args.gateway_url,
                    output_path=args.output,
                    profiles_file=args.profiles_file,
                )
            )
            return 0
        except Exception as exc:
            print(f"Error executing load run: {exc}", file=sys.stderr)
            return 1

    elif args.subcommand == "list-profiles":
        try:
            profiles = load_profiles(args.profiles_file)
            print("Available Load Profiles:")
            for name, prof in profiles.items():
                p_type = f"{prof.type.value:<6}"
                print(f"  - {name:<16} (type: {p_type}, seed: {prof.seed}) {prof.description}")
            return 0
        except Exception as exc:
            print(f"Error loading profiles: {exc}", file=sys.stderr)
            return 1

    elif args.subcommand == "generate":
        try:
            prof = get_profile(args.profile, args.profiles_file)
            requests = generate_schedule(prof, duration_s=args.duration, seed=args.seed)
            seq_hash = hash_request_sequence(requests)
            print(f"Profile:       {prof.profile_id}")
            print(f"Requests:      {len(requests)}")
            print(f"Sequence Hash: {seq_hash}")
            if requests:
                print("First 3 requests:")
                for r in requests[:3]:
                    print(f"  t={r.offset_s:.4f}s -> product_id={r.product_id}, qty={r.qty}")
            return 0
        except Exception as exc:
            print(f"Error generating schedule: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
