"""Authenticated HTTP client for cbdb-online-main-server's /api/v2/* endpoints.

Every call goes through here so local audit logging and client-side rate limiting
apply uniformly (AGENTS.md rule 2). Never call requests directly elsewhere in this
codebase.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import requests

from .audit_log import AuditLog, new_correlation_id
from .config import Config
from .models import approval_gated_aliases


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


class NotFoundError(CbdbApiError):
    """404 - e.g. GET /api/v2/get for a row that doesn't exist. Not retried."""


class MissingApprovalError(CbdbApiError):
    """A write to an approval-gated resource carried no human approval signature.

    AGENTS.md rule 12: code-table and entity-aggregate writes are global reference
    data, not one person's record. mutation_api.py already refuses these without an
    `approved_by`, and staging.py refuses to even validate such a batch - this is the
    last, unskippable gate, at the layer that actually puts bytes on the wire. Same
    fail-closed reasoning as MutatingFlagMismatch: a defense that only works when the
    caller went through the intended wrapper is not a defense for a write with no
    server-side undo.
    """


class MutatingFlagMismatch(ValueError):
    """Raised when a caller's `mutating` flag contradicts a known endpoint's nature.

    Defense-in-depth against a Milestone-3+ wrapper accidentally passing
    mutating=False for a write endpoint (which would silently skip both the
    dry-run short-circuit and the CBDB_CONFIRM_PROD gate) or mutating=True for a
    read-only endpoint. Fails closed rather than trusting the caller-supplied flag
    alone for paths this client recognizes.
    """


_KNOWN_MUTATING_PATHS = ("/api/v2/create", "/api/v2/mutate", "/api/v2/delete")
# Every path this client is allowed to read from (AGENTS.md rule 1). Listing them
# here is what makes _check_mutating_flag's fail-closed defense actually cover them,
# instead of letting an unrecognized path slip past with mutating=True. Two groups:
#   - the public code/name-resolution lookups (API.md 14.1/14.4), in the `api`
#     middleware group, 600 req/min;
#   - /cbdbapi/person (API.md 14.7), the read-a-whole-person endpoint, which is in
#     the `web` group with NO application-layer throttle - so it does not share that
#     600/min budget, and self-restraint is the only limit on it.
_KNOWN_READ_ONLY_PATHS = (
    "/api/v2/get",
    "/api/v2/persons",
    "/api/v2/operations",
    "/api/v2/texts",
    "/api/select/",
    "/api/code/addr",
    "/api/name",
    "/cbdbapi/person",
    # Undocumented legacy dumps, used only as the no-snapshot fallback for office
    # type hierarchy (AGENTS.md rule 1). Listed so _check_mutating_flag covers them
    # too - the fail-closed guard is only as complete as this tuple.
    "/api/OFFICE_TYPE_TREE",
    "/api/OFFICE_CODE_TYPE_REL",
)

# Public endpoints (API.md 14.1/14.4 lookups, plus 14.7's /cbdbapi/person): none
# require credentials, and sending a stale Bearer token to them both fails (401) and
# consumes the per-source-IP failed-auth budget shared with every other Bearer client
# behind the same egress IP (API.md 1.3; AGENTS.md rule 10). Callers should pass
# public=True for these; this tuple is only a sanity aid, not an enforcement.
PUBLIC_LOOKUP_PATHS = (
    "/api/v2/texts",
    "/api/select/",
    "/api/code/addr",
    "/api/name",
    "/cbdbapi/person",
    "/api/OFFICE_TYPE_TREE",
    "/api/OFFICE_CODE_TYPE_REL",
)

# Cap on how much of a public-lookup response body is copied into the local
# audit log. A single /api/select/nianhao returns an entire code table, and
# logs/*.jsonl is append-only by rule 8 - so an uncapped lookup pass would bloat
# a file nobody is allowed to prune. Only public lookups are truncated: every
# /api/v2/* request and response is still logged in full, because those are the
# ones the audit trail exists to reconstruct.
PUBLIC_RESPONSE_LOG_MAX_ROWS = 5


# Resource strings this client must never put on the wire, whatever the caller says.
#
# `offices` is a documented server-side alias for the OFFICE entity aggregate - which is
# approval-gated - AND for the postings sub-resource, which is routine. Which one it hits
# is decided by MutationHandlerRegistry's registration order, something this client
# cannot see and upstream can change. So the string is ambiguous in the worst possible
# direction: `models.approval_gated_aliases()` deliberately does not contain it (adding
# it would make every routine postings write demand an approved_by), which means a raw
# `HttpClient.post({"resource": "offices", ...})` would reach the server UNGATED and
# could land on the gated aggregate. Every legitimate caller here says `office` or
# `postings`; nothing needs the ambiguous spelling, so refuse it outright.
_AMBIGUOUS_RESOURCE_STRINGS = frozenset({"offices", "office-load"})


def _check_approval(json_body: Any, mutating: bool, approval_signature: str | None) -> None:
    """Fail closed if the envelope targets an approval-gated resource unsigned, or
    names a resource whose meaning is ambiguous between a gated and an ungated one.

    Reads the resource straight out of the request body rather than trusting a
    caller-supplied label, so it applies equally to MutationApi and to any direct
    HttpClient.post() - which is exactly the hole this closes.
    """
    if not mutating or not isinstance(json_body, dict):
        return
    resource = json_body.get("resource")
    if not isinstance(resource, str):
        return
    normalized = resource.strip().lower()

    if normalized in _AMBIGUOUS_RESOURCE_STRINGS:
        raise MissingApprovalError(
            f"refusing to send resource {resource!r}: the server accepts it for BOTH "
            "the approval-gated `office` entity aggregate and the routine `postings` "
            "sub-resource, and which one wins is registration order we cannot see. Say "
            "which you mean - `office` for the office code, `postings` for a person's "
            "appointment record."
        )

    if normalized not in approval_gated_aliases():
        return
    if not (approval_signature or "").strip():
        raise MissingApprovalError(
            f"refusing to write resource {resource!r} without an approval signature: "
            "this is global reference data, not one person's record, and it is visible "
            "to every other user (AGENTS.md rule 12). Removing it afterwards ranges "
            "from impossible (the code tables have no delete path, API.md 13.3) to "
            "conditional (the entity aggregates delete only while nothing references "
            "the row, API.md 13.4). Pass approved_by= through MutationApi, or "
            "approval_signature= if calling HttpClient directly. Never supply this on "
            "the agent's own initiative."
        )


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
    """Minimum-interval limiter: >= 60/max_per_minute seconds between calls.

    This is a fixed-spacing limiter, NOT a rolling-window one - it never allows a
    burst that a "max_per_minute calls per rolling minute" budget would. That is
    deliberate: API.md 1.3 caps writes at 1 request/second *and* requires them
    serialized ("等上一個請求回應之後再發下一個"), which a burst-tolerant window
    would not satisfy. See AGENTS.md rule 9.

    Use `with limiter.slot():` around the actual request, not the bare
    `wait_for_slot()`. `slot()` is what implements the *serialized* half of the
    contract, in two ways a plain "sleep then send" cannot:
      - it holds a lock for the duration of the request, so a second thread cannot
        send while the first thread's request is still outstanding, and
      - it stamps `_last_call` when the request COMPLETES, not when it starts, so
        the interval is measured from the previous response - which is what
        upstream actually asks for. A long request therefore delays the next one by
        its own duration plus the interval, instead of being overlapped.
    `wait_for_slot()` is kept for the spacing-only case (and for tests) and does
    neither of those things.

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
        self._lock = threading.Lock()

    def _wait(self) -> None:
        if self._last_call is None:
            return
        remaining = self._min_interval - (self._clock() - self._last_call)
        if remaining > 0:
            self._sleep(remaining)

    def wait_for_slot(self) -> None:
        """Spacing only - does NOT serialize. Prefer slot(); see the class docstring."""
        with self._lock:
            self._wait()
            self._last_call = self._clock()

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Hold a slot for the whole request: space, send, then stamp completion."""
        with self._lock:
            self._wait()
            try:
                yield
            finally:
                # In `finally` so a failed/timed-out request still counts: a burst
                # of connection errors must not become a burst of retries at full
                # speed, which is exactly when backing off matters most.
                self._last_call = self._clock()


def _summarize_public_response(body: Any) -> Any:
    """Shrink a public-lookup response before it goes into the append-only log.

    A single `/api/select/nianhao` is an entire code table, and `logs/*.jsonl` may
    never be pruned or rewritten (AGENTS.md rule 8) - so logging bulk lookups
    verbatim would permanently bloat a file with no maintenance path. We keep the
    first PUBLIC_RESPONSE_LOG_MAX_ROWS rows plus an explicit count, which is what a
    human reconstructing "what did the agent look up, and roughly what came back"
    actually needs.

    This applies ONLY to public lookups. Every /api/v2/* request and response is
    still recorded in full - those are the calls the audit trail exists for, and
    for a mutating call the full before/after payload is the whole point.
    """
    def _cap(rows: list[Any]) -> Any:
        if len(rows) <= PUBLIC_RESPONSE_LOG_MAX_ROWS:
            return rows
        return {
            "_truncated": True,
            "total_rows": len(rows),
            "rows": rows[:PUBLIC_RESPONSE_LOG_MAX_ROWS],
        }

    if isinstance(body, list):
        return _cap(body)
    if isinstance(body, dict):
        # Laravel paginator, or the v2 `ok`/`data` envelope: cap the row list but
        # keep every sibling key (`total`, `meta.missing_ids`, ...) intact - those
        # are small and are exactly what makes the logged entry interpretable.
        rows = body.get("data")
        if isinstance(rows, list) and len(rows) > PUBLIC_RESPONSE_LOG_MAX_ROWS:
            return {**body, "data": _cap(rows)}
        return body
    if isinstance(body, str) and len(body) > 500:
        return body[:500] + f"... [truncated, {len(body)} chars]"
    return body


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

    def _headers(self, *, public: bool = False) -> dict[str, str]:
        """Accept is required, not advisory (API.md 1.4): without it, middleware-level
        failures come back as an HTML page and an unauthenticated request 302s to
        /login instead of returning a JSON 401.

        public=True deliberately sends no credentials. For the public lookup
        endpoints (API.md 14.1/14.4) a token buys nothing, and a *stale* token turns
        a harmless read into a failed-auth attempt counted against a 60/minute
        per-source-IP budget shared with every other Bearer client on that IP
        (API.md 1.3). Never set Origin/Referer here either - Sanctum treats a request
        matching SANCTUM_STATEFUL_DOMAINS as first-party and ignores the Bearer token
        entirely, yielding a 401 (API.md 1.1).
        """
        headers = {"Accept": "application/json"}
        if not public and self._config.api_token:
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
        json_body: dict[str, Any] | None = None,
        resource: str | None = None,
        public: bool = False,
    ) -> dict[str, Any]:
        """json_body: some endpoints (e.g. /api/v2/get) require a nested request
        shape that doesn't fit flat query params - Laravel reads a JSON body on a
        GET request just like on POST, so `requests`' `json=` on a GET call works.

        params and json_body are mutually exclusive: no current endpoint needs
        both, and allowing both would let a caller's `params` silently vanish from
        the local audit log (which logs one or the other, not a merge) while
        `requests` still sent both over the wire - a real audit-completeness gap.

        public=True sends no Authorization header. Use it for the public lookup
        endpoints in PUBLIC_LOOKUP_PATHS (API.md 14.1/14.4): a token gains nothing
        there, and a stale one would fail the request AND spend the shared
        per-source-IP failed-auth budget (AGENTS.md rule 10).

        RETURN SHAPE, important for the lookup endpoints: the v2 API always
        returns a JSON object, but several lookup endpoints do not - whole code
        tables and `search/kinpair`/`search/assocpair` return a BARE ARRAY, and
        `search/pinyin` returns PLAIN TEXT. Since this method's contract is a
        dict, any non-object body is returned wrapped as `{"raw": <body>}`. So a
        lookup caller must handle `{"raw": [...]}` (bare array), `{"raw": "..."}`
        (plain text), AND a real Laravel paginator object whose rows live under
        `"data"` - three shapes, not one. See docs/07-api-md-digest.md 2.1.
        """
        if params is not None and json_body is not None:
            raise ValueError("get() accepts params or json_body, not both")
        return self._request(
            "GET",
            path,
            params=params,
            json_body=json_body,
            mutating=False,
            resource=resource,
            operation=None,
            mode=None,
            public=public,
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
        public: bool = False,
        approval_signature: str | None = None,
    ) -> dict[str, Any]:
        """mutating=True for create/mutate/delete; False for the POST form of GET.

        approval_signature is the human `approved_by` value, required for writes to
        approval-gated resources (AGENTS.md rule 12) and ignored otherwise. It is
        checked against the resource named in `json_body`, so it cannot be skipped by
        going around MutationApi.

        public=True (credential-less) is accepted for symmetry with get(), but is
        rejected for mutating calls: an unauthenticated write is never something
        this client should be trying, and the server stamps c_created_by/
        c_modified_by from the authenticated user (brief section 4), so it could
        not succeed anyway.
        """
        if public and mutating:
            raise ValueError("public=True is not allowed for a mutating request")
        return self._request(
            "POST",
            path,
            params=None,
            json_body=json_body,
            mutating=mutating,
            resource=resource,
            operation=operation,
            mode=mode,
            public=public,
            approval_signature=approval_signature,
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
        public: bool = False,
        approval_signature: str | None = None,
    ) -> dict[str, Any]:
        _check_mutating_flag(path, mutating)
        _check_approval(json_body, mutating, approval_signature)
        if public and mutating:
            raise ValueError("public=True is not allowed for a mutating request")
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
            try:
                with self._rate_limiter.slot():
                    response = self._session.request(
                        method,
                        url,
                        headers=self._headers(public=public),
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
                response_payload=(
                    _summarize_public_response(body) if public else body
                ),
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
            if response.status_code == 404:
                raise NotFoundError(
                    "Not found (404) - e.g. no row matching this target.pk",
                    status_code=404,
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
