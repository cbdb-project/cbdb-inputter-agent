"""Authenticated HTTP client for cbdb-online-main-server's /api/v2/* endpoints.

Every call goes through here so local audit logging and client-side rate limiting
apply uniformly (AGENTS.md rule 2). Never call requests directly elsewhere in this
codebase.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import requests

from .audit_log import AuditLog, new_correlation_id
from .config import Config


class CbdbApiError(Exception):
    """Base class for errors raised by HttpClient."""

    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AuthenticationError(CbdbApiError):
    """401 - bad or expired token. Never retried."""


class AuthorizationError(CbdbApiError):
    """403 - account lacks canWriteDirectly() or similar. Never retried."""


class ConflictError(CbdbApiError):
    """409/422 - duplicate PK, mirror-relationship conflict, or validation error.

    Never auto-retried with modified data (AGENTS.md rule 5) - the caller must
    surface this to a human.
    """


class RateLimitedError(CbdbApiError):
    """429 persisted past the retry budget."""


class ServerError(CbdbApiError):
    """5xx persisted past the retry budget."""


class UnexpectedResponseError(CbdbApiError):
    """Any other non-2xx status this client doesn't have a specific mapping for."""


class NetworkError(CbdbApiError):
    """A connection/timeout/DNS failure persisted past the retry budget."""


class MutatingFlagMismatch(ValueError):
    """Raised when a caller's `mutating` flag contradicts a known endpoint's nature.

    Defense-in-depth against a Milestone-3+ wrapper accidentally passing
    mutating=False for a write endpoint (which would silently skip both the
    dry-run short-circuit and the CBDB_CONFIRM_PROD gate) or mutating=True for a
    read-only endpoint. Fails closed rather than trusting the caller-supplied flag
    alone for paths this client recognizes.
    """


_KNOWN_MUTATING_PATHS = ("/api/v2/create", "/api/v2/mutate", "/api/v2/delete")
_KNOWN_READ_ONLY_PATHS = ("/api/v2/get", "/api/v2/persons", "/api/v2/operations")


def _check_mutating_flag(path: str, mutating: bool) -> None:
    normalized = "/" + path.strip("/")
    if any(normalized.startswith(p) for p in _KNOWN_MUTATING_PATHS) and not mutating:
        raise MutatingFlagMismatch(
            f"path {path!r} is a known mutating endpoint but mutating=False was "
            "passed - this would skip the dry-run and CBDB_CONFIRM_PROD gates"
        )
    if any(normalized.startswith(p) for p in _KNOWN_READ_ONLY_PATHS) and mutating:
        raise MutatingFlagMismatch(
            f"path {path!r} is a known read-only endpoint but mutating=True was "
            "passed"
        )


class RateLimiter:
    """Minimum-interval limiter: at most max_per_minute calls per rolling minute.

    clock and sleep are injectable so tests can run without real wall-clock waits.
    """

    def __init__(
        self,
        max_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_per_minute <= 0:
            raise ValueError("max_per_minute must be positive")
        self._min_interval = 60.0 / max_per_minute
        self._clock = clock
        self._sleep = sleep
        self._last_call: float | None = None

    def wait_for_slot(self) -> None:
        now = self._clock()
        if self._last_call is not None:
            elapsed = now - self._last_call
            remaining = self._min_interval - elapsed
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_call = now


class HttpClient:
    MAX_RETRIES = 3

    def __init__(
        self,
        config: Config,
        audit_log: AuditLog,
        *,
        session: requests.Session | None = None,
        rate_limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._audit_log = audit_log
        self._session = session or requests.Session()
        self._rate_limiter = rate_limiter or RateLimiter(config.max_requests_per_minute)
        self._sleep = sleep

    @property
    def dry_run(self) -> bool:
        return self._config.dry_run

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._config.api_token:
            headers["Authorization"] = f"Bearer {self._config.api_token}"
        return headers

    def _url(self, path: str) -> str:
        base = self._config.api_base_url.rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        resource: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            path,
            params=params,
            json_body=None,
            mutating=False,
            resource=resource,
            operation=None,
            mode=None,
        )

    def post(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        mutating: bool,
        resource: str | None = None,
        operation: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """mutating=True for create/mutate/delete; False for the POST form of GET."""
        return self._request(
            "POST",
            path,
            params=None,
            json_body=json_body,
            mutating=mutating,
            resource=resource,
            operation=operation,
            mode=mode,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        mutating: bool,
        resource: str | None,
        operation: str | None,
        mode: str | None,
    ) -> dict[str, Any]:
        _check_mutating_flag(path, mutating)
        correlation_id = new_correlation_id()
        url = self._url(path)
        # GET requests carry their real input in `params`, not `json_body` (which
        # is always None for GET) - log whichever is actually populated so the
        # audit trail reflects the full request as sent, not just POST bodies.
        logged_payload = json_body if json_body is not None else params

        if mutating and self._config.dry_run:
            self._audit_log.record(
                correlation_id=correlation_id,
                method=method,
                url=url,
                request_payload=logged_payload,
                dry_run=True,
                resource=resource,
                operation=operation,
                mode=mode,
                status_code=None,
                response_payload={"dry_run": True, "sent": False},
            )
            return {"dry_run": True, "sent": False}

        if mutating:
            self._config.require_live_writes_confirmed()

        attempt = 0
        last_error: CbdbApiError | None = None
        while attempt < self.MAX_RETRIES:
            attempt += 1
            self._rate_limiter.wait_for_slot()
            try:
                response = self._session.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                    timeout=30,
                )
            except requests.RequestException as exc:
                self._audit_log.record(
                    correlation_id=correlation_id,
                    method=method,
                    url=url,
                    request_payload=logged_payload,
                    dry_run=False,
                    resource=resource,
                    operation=operation,
                    mode=mode,
                    error=str(exc),
                )
                last_error = NetworkError(
                    f"Network error: {exc}", status_code=None, body=None
                )
                if attempt < self.MAX_RETRIES:
                    self._sleep(2 ** (attempt - 1))
                    continue
                raise last_error from exc

            body: Any
            try:
                body = response.json()
            except ValueError:
                body = response.text

            operation_id = None
            if isinstance(body, dict):
                result = body.get("result")
                if isinstance(result, dict):
                    operation_id = result.get("operation_id")
                operation_id = operation_id or body.get("operation_id")

            self._audit_log.record(
                correlation_id=correlation_id,
                method=method,
                url=url,
                request_payload=logged_payload,
                dry_run=False,
                resource=resource,
                operation=operation,
                mode=mode,
                status_code=response.status_code,
                response_payload=body,
                operation_id=operation_id,
            )

            if 200 <= response.status_code < 300:
                return body if isinstance(body, dict) else {"raw": body}

            if response.status_code == 401:
                raise AuthenticationError(
                    "Authentication failed (401) - token invalid or expired",
                    status_code=401,
                    body=body,
                )
            if response.status_code == 403:
                raise AuthorizationError(
                    "Authorization failed (403) - account may lack "
                    "canWriteDirectly() permission",
                    status_code=403,
                    body=body,
                )
            if response.status_code in (409, 422):
                raise ConflictError(
                    f"Conflict/validation error ({response.status_code})",
                    status_code=response.status_code,
                    body=body,
                )

            if response.status_code == 429:
                last_error = RateLimitedError(
                    "Rate limited (429)", status_code=429, body=body
                )
            elif response.status_code >= 500:
                last_error = ServerError(
                    f"Server error ({response.status_code})",
                    status_code=response.status_code,
                    body=body,
                )
            else:
                raise UnexpectedResponseError(
                    f"Unexpected status code {response.status_code}",
                    status_code=response.status_code,
                    body=body,
                )

            if attempt < self.MAX_RETRIES:
                self._sleep(2 ** (attempt - 1))

        assert last_error is not None
        raise last_error
