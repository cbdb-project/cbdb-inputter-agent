"""Load and validate configuration from .env.

See docs/01-implementation-plan.md section 3 and AGENTS.md rule 4 for the safety
gates this module enforces. Do not relax these checks to make a script "just work" -
they exist specifically to prevent an accidental write to production.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(ValueError):
    """Raised when .env is missing a required value or fails a safety check."""


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_int(raw: str | None, *, default: int, field_name: str) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{field_name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    api_base_url: str
    api_token: str
    dry_run: bool
    confirm_prod: str
    max_requests_per_minute: int
    local_audit_log_dir: Path

    @property
    def live_writes_confirmed(self) -> bool:
        """URL-pinned production/live-write gate (AGENTS.md rule 4).

        Required to equal the exact current api_base_url before ANY mutating call
        is sent while dry_run is False - for any host, not just a hardcoded
        production hostname. This is deliberately not a boolean: changing
        api_base_url (e.g. switching from a local dev server to production)
        automatically invalidates a previous confirmation, so a silent target
        switch can never inherit an earlier "yes, go live" decision.
        """
        return self.confirm_prod != "" and self.confirm_prod == self.api_base_url

    def require_live_writes_confirmed(self) -> None:
        if self.dry_run:
            return
        if not self.live_writes_confirmed:
            raise ConfigError(
                "CBDB_DRY_RUN=false but CBDB_CONFIRM_PROD does not exactly match "
                f"CBDB_API_BASE_URL ({self.api_base_url!r}). Set CBDB_CONFIRM_PROD "
                "to that exact URL in .env to confirm you intend to send live "
                "mutating requests to this host. See AGENTS.md rule 4."
            )


def load_config(env_path: str | Path | None = None) -> Config:
    """Load configuration from .env (or the given path) and validate it.

    Raises ConfigError if CBDB_API_TOKEN is empty while CBDB_DRY_RUN is false, or
    if any numeric field fails to parse. Does NOT raise for a live-write-gate
    mismatch here - that check is deferred to require_live_writes_confirmed(),
    called by http_client.py immediately before a mutating call is sent, so a
    dry-run session can still load config even with an unset CBDB_CONFIRM_PROD.
    """
    # override=True: .env always wins over a pre-existing OS environment variable.
    # In most dotenv usage the reverse (override=False) is preferred so a real
    # shell export can deliberately override a checked-in default - but .env here
    # is the operator-visible, git-ignored control plane for the dry-run and
    # CBDB_CONFIRM_PROD safety gates (AGENTS.md rule 4). Letting a stale exported
    # env var silently outrank an updated .env would mean editing .env back to a
    # safer value doesn't actually take effect - the opposite of what a safety gate
    # should do. If you need to override .env for a one-off run, edit .env itself.
    load_dotenv(dotenv_path=env_path, override=True)

    api_base_url = os.environ.get("CBDB_API_BASE_URL", "").strip()
    if not api_base_url:
        raise ConfigError("CBDB_API_BASE_URL is required in .env")

    api_token = os.environ.get("CBDB_API_TOKEN", "").strip()
    dry_run = _parse_bool(os.environ.get("CBDB_DRY_RUN"), default=True)

    if not api_token and not dry_run:
        raise ConfigError(
            "CBDB_API_TOKEN is empty but CBDB_DRY_RUN is false - a live client "
            "requires a real Sanctum token. Set CBDB_DRY_RUN=true, or provide a "
            "token."
        )

    confirm_prod = os.environ.get("CBDB_CONFIRM_PROD", "").strip()
    max_rpm = _parse_int(
        os.environ.get("CBDB_MAX_REQUESTS_PER_MINUTE"),
        default=60,
        field_name="CBDB_MAX_REQUESTS_PER_MINUTE",
    )
    if max_rpm <= 0:
        raise ConfigError("CBDB_MAX_REQUESTS_PER_MINUTE must be a positive integer")

    audit_log_dir = Path(
        os.environ.get("CBDB_LOCAL_AUDIT_LOG_DIR", "./logs").strip() or "./logs"
    )

    return Config(
        api_base_url=api_base_url,
        api_token=api_token,
        dry_run=dry_run,
        confirm_prod=confirm_prod,
        max_requests_per_minute=max_rpm,
        local_audit_log_dir=audit_log_dir,
    )
