"""`python -m cbdb_agent` entry point.

Subcommands:
  validate     --staging <path> | --input <path>  [--env <path>]
  submit       --staging <path> | --input <path>  [--dry-run] [--env <path>]
  apply-review --staging <path> --decisions <path>

See docs/01-implementation-plan.md section 7 and docs/03-extraction-review-
workflow.md section 2.3 for the intended interaction flow. Both --staging and
--input converge on the same StagingBatch representation (staging.py) and the
same submission engine (batch_runner.py) - see load_input_batch()'s docstring for
why.

`validate --staging` additionally refreshes a `preview.md` file next to the
staging YAML on every run (docs/06-staging-preview-design.md section 3), plus a
`review.json` for the offline review page in `tools/review/`
(docs/08-review-interface-design.md). `apply-review` is the return leg of that
round trip: it reads the page's exported `decisions.json` and writes the choices
back into the staging YAML - the YAML remains the only source of truth and the
only thing `submit` ever reads. `--env` there is unused;
`--env` there is only used for that preview's best-effort Tier 2 live diff, not
for validation itself, which never touches the network. `validate --input` has
no persistent file location to write a preview next to, so it skips this.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
from pathlib import Path

from .audit_log import AuditLog
from .batch_runner import ProposalResult, fetch_current_values, run_batch
from .code_lookup import CodeResolver, collect_code_values
from .snapshot import (
    autodownload_from_env,
    ensure_snapshot,
    open_snapshot,
    snapshot_age_days,
    snapshot_dir_from_env,
    snapshot_is_stale,
)
from .config import ConfigError, load_config
from .http_client import HttpClient
from .mutation_api import MutationApi
from .review import apply_decisions, export_review_json
from .staging import (
    Issue,
    StagingBatch,
    StagingError,
    find_issues,
    load_input_batch,
    load_staging_file,
    render_preview_markdown,
    save_staging_file,
    validate_for_submit,
)

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
        exit_code = EXIT_OK
    else:
        errors = [i for i in issues if i.severity == "error"]
        conflicts = [i for i in issues if i.severity == "unresolved_conflict"]
        print(f"Batch {batch.batch_id!r}: {len(errors)} error(s), {len(conflicts)} unresolved conflict(s).")
        for issue in issues:
            print(f"  - [{issue.proposal_id}] {issue.severity}: {issue.message}")
        # Per docs/03 section 2.5: validate reports and exits cleanly even with
        # unresolved conflicts (those are expected mid-review) - only structural
        # errors are a hard failure at this stage.
        exit_code = EXIT_VALIDATION_ERROR if errors else EXIT_OK

    if args.staging:
        _write_preview(args, batch, issues)

    return exit_code


def _write_preview(args: argparse.Namespace, batch: StagingBatch, issues: list[Issue]) -> None:
    """Refresh preview.md next to the staging YAML on every `validate --staging`
    run (docs/06-staging-preview-design.md section 3). Tier 2's live diff is
    attempted only if a working .env config happens to be available here; per
    that design, ANY failure to load config or reach the target system just
    means the preview falls back to Tier 1 (offline) - it must never affect
    validate's own exit code or structural report.
    """
    current_values = None
    config = None
    client = None
    try:
        config = load_config(args.env)
        client = HttpClient(config, AuditLog(config.local_audit_log_dir))
        # fetch_current_values() never raises (its own docstring/tests guarantee
        # this - every per-proposal failure already becomes a ProposalCurrentState
        # error), so the only thing this except needs to catch is a genuinely
        # unavailable/broken .env.
        current_values = fetch_current_values(batch, MutationApi(client))
    except ConfigError:
        current_values = None

    code_labels = _resolve_code_labels(batch, config, client)

    try:
        markdown = render_preview_markdown(batch, issues, current_values=current_values)
        preview_path = Path(args.staging).parent / "preview.md"
        preview_path.write_text(markdown, encoding="utf-8")
        print(f"Preview written to {preview_path}")
    except OSError as exc:
        print(f"Warning: could not write preview.md: {exc}", file=sys.stderr)

    # review.json rides along with preview.md for the same reason preview.md is tied
    # to validate: a review surface that can go stale relative to the file it claims
    # to describe is worse than no review surface. Both are generated and disposable.
    try:
        review_path = Path(args.staging).parent / "review.json"
        review_path.write_text(
            export_review_json(
                batch,
                issues,
                current_values=current_values,
                code_labels=code_labels,
            ),
            encoding="utf-8",
        )
        print(f"Review data written to {review_path}  (open tools/review/index.html)")
    except OSError as exc:
        print(f"Warning: could not write review.json: {exc}", file=sys.stderr)


def _resolve_code_labels(
    batch: StagingBatch, config: object | None, client: HttpClient | None
) -> dict:
    """Human-readable names for every code in the batch (docs/08 section 3).

    Prefers the weekly SQLite snapshot, which answers the hierarchy joins - an
    address's full parent chain, an office's type-tree ancestry - in one local query
    instead of one HTTP request per level. Falls back to the public lookup endpoints,
    and to nothing at all when neither is available.

    Best-effort by contract, exactly like the Tier-2 live diff: `validate` must keep
    working with no network, no .env and no snapshot, so nothing here may raise or
    affect the exit code.
    """
    # Fall back to the environment when there is no Config: `validate --staging` is
    # required to work with no .env, and that is exactly when a user who does not want
    # a 132 MB download most needs to be able to say so. Reading only Config here
    # meant CBDB_SQLITE_AUTODOWNLOAD was silently ignored in that case.
    snapshot_dir = getattr(config, "sqlite_dir", None) or snapshot_dir_from_env()
    allow_download = getattr(config, "sqlite_autodownload", None)
    if allow_download is None:
        allow_download = autodownload_from_env()
    try:
        snapshot_path = ensure_snapshot(
            snapshot_dir, allow_download=allow_download, progress=print
        )
    except Exception as exc:  # noqa: BLE001 - never fail validate over a cache
        print(f"Warning: snapshot unavailable ({exc})", file=sys.stderr)
        snapshot_path = None

    resolver = None
    connection = None
    try:
        if snapshot_path is not None:
            connection = open_snapshot(snapshot_path)
            resolver = CodeResolver(snapshot=connection)
            age = snapshot_age_days(snapshot_path)
            age_text = f", built {age:.0f} day(s) ago" if age is not None else ""
            print(f"Code labels from the SQLite snapshot ({snapshot_path.name}{age_text})")
            if snapshot_is_stale(snapshot_path):
                # Reference tables drift slowly, but the reviewer should know the
                # names they are reading come from a build this old.
                print(
                    "  note: that snapshot is over two weeks old - delete it to "
                    "re-download, or ignore if the code tables haven't moved.",
                    file=sys.stderr,
                )
        elif client is not None:
            resolver = CodeResolver(client=client)
            print("Code labels from the public lookup API (no local snapshot)")

        if resolver is None:
            return {}
        return resolver.resolve_values(collect_code_values(batch))
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"Warning: could not resolve code labels ({exc})", file=sys.stderr)
        return {}
    finally:
        if connection is not None:
            connection.close()


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


def cmd_apply_review(args: argparse.Namespace) -> int:
    """Apply the review page's decisions.json back into the staging YAML.

    Prints every change it made. Does NOT validate afterwards - run
    `validate --staging` next, which is also what regenerates preview.md/review.json
    from the newly-written file.
    """
    try:
        batch = load_staging_file(args.staging)
    except (StagingError, OSError, ValueError) as exc:
        print(f"Could not load {args.staging}: {exc}", file=sys.stderr)
        return EXIT_LOAD_ERROR

    try:
        decisions = json.loads(Path(args.decisions).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Could not load {args.decisions}: {exc}", file=sys.stderr)
        return EXIT_LOAD_ERROR

    try:
        applied = apply_decisions(batch, decisions)
    except StagingError as exc:
        # Deliberately a hard failure, not a partial apply: a decisions file that
        # doesn't match the batch means one of the two has moved on, and applying
        # only the half that still matches would leave the reviewer believing they
        # settled something they didn't.
        print(f"Refusing to apply: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    if not applied:
        print("No changes - every decision already matched the staging file.")
        return EXIT_OK

    for change in applied:
        print(f"  [{change.proposal_id}] {change.kind}: {change.detail}")
    try:
        save_staging_file(batch, args.staging)
    except OSError as exc:
        print(f"Applied nothing - could not write {args.staging}: {exc}", file=sys.stderr)
        return EXIT_LOAD_ERROR
    print(f"{len(applied)} change(s) written to {args.staging}")
    print("Next: python -m cbdb_agent validate --staging " + args.staging)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cbdb_agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, handler in (("validate", cmd_validate), ("submit", cmd_submit)):
        sub = subparsers.add_parser(name)
        group = sub.add_mutually_exclusive_group(required=True)
        group.add_argument("--staging", help="Path to a YAML staging file")
        group.add_argument("--input", help="Path to a structured JSON input batch")
        sub.add_argument(
            "--env",
            default=None,
            help="Path to a .env file (default: standard python-dotenv lookup)",
        )
        if name == "submit":
            sub.add_argument(
                "--dry-run",
                action="store_true",
                help="Force dry-run even if .env disables it (cannot force it off)",
            )
        sub.set_defaults(func=handler)

    apply_review = subparsers.add_parser(
        "apply-review",
        help="Write the review page's decisions.json back into a staging YAML",
    )
    apply_review.add_argument("--staging", required=True, help="Path to the YAML staging file")
    apply_review.add_argument(
        "--decisions", required=True, help="decisions.json exported by tools/review/index.html"
    )
    apply_review.set_defaults(func=cmd_apply_review)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
