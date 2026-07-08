"""`python -m cbdb_agent` entry point.

Subcommands:
  validate --staging <path> | --input <path>
  submit   --staging <path> | --input <path>  [--dry-run]

See docs/01-implementation-plan.md section 7 and docs/03-extraction-review-
workflow.md section 2.3 for the intended interaction flow. Both --staging and
--input converge on the same StagingBatch representation (staging.py) and the
same submission engine (batch_runner.py) - see load_input_batch()'s docstring for
why.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
from pathlib import Path

from .audit_log import AuditLog
from .batch_runner import ProposalResult, run_batch
from .config import ConfigError, load_config
from .http_client import HttpClient
from .mutation_api import MutationApi
from .staging import StagingBatch, StagingError, find_issues, load_input_batch, load_staging_file, validate_for_submit

# Distinct exit codes so a caller/CI script can tell "nothing was attempted" apart
# from "some records failed at the server" (docs/01-implementation-plan.md section 7).
EXIT_OK = 0
EXIT_LOAD_ERROR = 2          # couldn't read/parse the input/staging file
EXIT_VALIDATION_ERROR = 3    # structural error - nothing was submitted
EXIT_CONFIG_ERROR = 4        # bad .env - nothing was submitted
EXIT_SUBMISSION_FAILURES = 1  # batch ran; at least one proposal failed/was skipped


def _load_batch(args: argparse.Namespace) -> StagingBatch:
    if args.staging:
        return load_staging_file(args.staging)
    return load_input_batch(args.input)


def _source_path(args: argparse.Namespace) -> str:
    return args.staging or args.input


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        batch = _load_batch(args)
    except (StagingError, OSError, ValueError) as exc:
        print(f"Could not load {_source_path(args)}: {exc}", file=sys.stderr)
        return EXIT_LOAD_ERROR

    issues = find_issues(batch)
    if not issues:
        print(f"Batch {batch.batch_id!r}: no issues found ({len(batch.proposals)} proposals).")
        return EXIT_OK

    errors = [i for i in issues if i.severity == "error"]
    conflicts = [i for i in issues if i.severity == "unresolved_conflict"]
    print(f"Batch {batch.batch_id!r}: {len(errors)} error(s), {len(conflicts)} unresolved conflict(s).")
    for issue in issues:
        print(f"  - [{issue.proposal_id}] {issue.severity}: {issue.message}")
    # Per docs/03 section 2.5: validate reports and exits cleanly even with
    # unresolved conflicts (those are expected mid-review) - only structural
    # errors are a hard failure at this stage.
    return EXIT_VALIDATION_ERROR if errors else EXIT_OK


def cmd_submit(args: argparse.Namespace) -> int:
    try:
        batch = _load_batch(args)
    except (StagingError, OSError, ValueError) as exc:
        print(f"Could not load {_source_path(args)}: {exc}", file=sys.stderr)
        return EXIT_LOAD_ERROR

    try:
        validate_for_submit(batch)
    except StagingError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    try:
        config = load_config(args.env)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.dry_run:
        # --dry-run can only force dry-run ON, never off (docs/01 section 7) -
        # safety only goes one direction from the CLI.
        config = dataclasses.replace(config, dry_run=True)

    client = HttpClient(config, AuditLog(config.local_audit_log_dir))
    api = MutationApi(client)

    print(
        f"Submitting batch {batch.batch_id!r} ({len(batch.proposals)} proposal(s)) "
        f"to {config.api_base_url} (dry_run={config.dry_run})..."
    )
    results = run_batch(batch, api)
    _print_summary(results)
    _archive_batch(_source_path(args), batch, results, dry_run=config.dry_run)

    failed = [r for r in results if r.status != "success"]
    return EXIT_SUBMISSION_FAILURES if failed else EXIT_OK


def _print_summary(results: list[ProposalResult]) -> None:
    for r in results:
        line = f"  [{r.proposal_id}] {r.status}"
        if r.error:
            line += f" - {r.error}"
        print(line)
    succeeded = sum(1 for r in results if r.status == "success")
    print(f"{succeeded}/{len(results)} proposal(s) succeeded.")


def _archive_batch(
    source_path: str, batch: StagingBatch, results: list[ProposalResult], *, dry_run: bool
) -> None:
    """Move the source file to data/processed/<batch_id>/ with results attached.

    Only archives on a real (non-dry-run) submission attempt - a dry run hasn't
    actually done anything to the target system yet, so the source file stays put
    for further iteration (docs/01-implementation-plan.md section 7).
    """
    if dry_run:
        return
    src = Path(source_path)
    if not src.exists():
        return  # already moved by an earlier run, or path was synthetic (tests)

    safe_batch_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in batch.batch_id)
    # A batch_id of "." or ".." (or any run of only dots) would otherwise survive
    # the character-level sanitization above unchanged and let the archive escape
    # data/processed/ via normal filesystem dot-segment resolution.
    if not safe_batch_id or set(safe_batch_id) == {"."}:
        safe_batch_id = "_batch"
    base_dir = Path("data/processed") / safe_batch_id
    dest_dir = base_dir
    # Never silently overwrite a previous attempt's archive (e.g. re-submitting
    # the same batch_id after fixing a failure) - each attempt gets its own
    # numbered directory instead of clobbering the last one's results.json/source.
    suffix = 2
    while dest_dir.exists():
        dest_dir = Path(f"{base_dir}-attempt{suffix}")
        suffix += 1
    dest_dir.mkdir(parents=True)

    shutil.move(str(src), str(dest_dir / src.name))
    results_path = dest_dir / "results.json"
    with results_path.open("w", encoding="utf-8") as f:
        json.dump([dataclasses.asdict(r) for r in results], f, ensure_ascii=False, indent=2)
    print(f"Archived to {dest_dir}/")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cbdb_agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, handler in (("validate", cmd_validate), ("submit", cmd_submit)):
        sub = subparsers.add_parser(name)
        group = sub.add_mutually_exclusive_group(required=True)
        group.add_argument("--staging", help="Path to a YAML staging file")
        group.add_argument("--input", help="Path to a structured JSON input batch")
        if name == "submit":
            sub.add_argument(
                "--dry-run",
                action="store_true",
                help="Force dry-run even if .env disables it (cannot force it off)",
            )
            sub.add_argument(
                "--env",
                default=None,
                help="Path to a .env file (default: standard python-dotenv lookup)",
            )
        sub.set_defaults(func=handler)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
