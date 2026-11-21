"""Every exception this package raises.

All of them derive from BreachLabError. An answer of the API that is not a
success becomes an ApiError, or the subclass that matches its status and
code, built from the problem document the API sends.
"""

import re
from typing import Any, Mapping, Optional

_TOKEN_IN_TEXT = re.compile(r"blp_[A-Za-z0-9_-]+")
_SECRET_FIELD = re.compile(r'("(?:access_token|device_code|token|password)"\s*:\s*")[^"]*(")')

#: The three kinds of 429 the API documents.
RATE_LIMIT_KINDS = ("rate_limited", "cooldown", "run_limit_reached")


def scrub(text: str) -> str:
    """Return the text with anything that looks like a credential replaced."""
    text = _SECRET_FIELD.sub(r"\1***\2", text)
    return _TOKEN_IN_TEXT.sub("blp_***", text)


class BreachLabError(Exception):
    """Base class of every error raised by this package."""


class ConfigError(BreachLabError):
    """The client is configured in a way it refuses to run with."""


class TokenError(BreachLabError):
    """The stored or supplied token cannot be used."""


class NotLoggedIn(BreachLabError):
    """A call needs a token and there is none."""

    def __init__(self, message: str = "This call needs an API token and none was found."):
        BreachLabError.__init__(self, message)


class NetworkError(BreachLabError):
    """The API could not be reached."""


class DeviceFlowError(BreachLabError):
    """The device flow login ended without a token.

    `code` is the error of the device flow: `access_denied`, `expired_token`
    or whatever else the server answered. `expired_locally` is true when the
    code ran out on this side before anyone approved it.
    """

    def __init__(self, code: str, description: Optional[str] = None,
                 doc_url: Optional[str] = None, expired_locally: bool = False):
        BreachLabError.__init__(self, description or "The login failed: %s." % code)
        self.code = code
        self.description = description
        self.doc_url = doc_url
        self.expired_locally = expired_locally


class ApiError(BreachLabError):
    """The API answered with an error.

    The attributes mirror the problem document: `type`, `title`, `status`,
    `detail` and `doc_url`, plus the stable `code` and the `request_id` to
    quote to support. `body` is the parsed answer when it was JSON.
    """

    def __init__(self, status: int, code: str, detail: str, doc_url: Optional[str] = None,
                 request_id: Optional[str] = None, retry_after: Optional[int] = None,
                 type: Optional[str] = None, title: Optional[str] = None,
                 body: Optional[Mapping[str, Any]] = None):
        BreachLabError.__init__(self, detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.doc_url = doc_url
        self.request_id = request_id
        self.retry_after = retry_after
        self.type = type
        self.title = title
        self.body = body

    def __str__(self) -> str:
        return scrub(str(self.detail))

    def __repr__(self) -> str:
        parts = ["status=%r" % self.status, "code=%r" % self.code]
        if self.request_id:
            parts.append("request_id=%r" % self.request_id)
        if self.retry_after is not None:
            parts.append("retry_after=%r" % self.retry_after)
        parts.append("detail=%r" % scrub(str(self.detail)))
        return "%s(%s)" % (self.__class__.__name__, ", ".join(parts))


class UnexpectedResponse(ApiError):
    """A success answer did not have the documented shape. Its code is `bad_response`."""


class AuthenticationError(ApiError):
    """401. The token is missing, malformed, unknown, revoked or expired."""


class ScopeError(ApiError):
    """403 `insufficient_scope`. The token does not carry the scope the endpoint needs."""


class ForbiddenError(ApiError):
    """403 for a reason other than scope.

    The code says which: `email_unverified`, `prerequisite_required`,
    `level_locked`, `subscription_required` as a PaywallError,
    `not_an_agent` as a NotAnAgentError, or `admin_not_allowed` as an
    AdminNotAllowedError.
    """


class PaywallError(ForbiddenError):
    """403 `subscription_required`. The level needs an active subscription."""


class NotAnAgentError(ForbiddenError):
    """403 `not_an_agent`. The token carries the agent scope, but its account
    is not a declared agent, so it cannot create runs."""


class AdminNotAllowedError(ForbiddenError):
    """403 `admin_not_allowed`. An admin account cannot mint box tokens over
    the API. Mint one on the website, or use the token of a regular account."""


class NotFoundError(ApiError):
    """404. The resource does not exist or is not available to this token."""


class ConflictError(ApiError):
    """409. The request does not fit the current state.

    Codes: `already_solved`, `run_closed`, `not_api_playable` and
    `idempotency_in_progress`.
    """


class AlreadySolvedError(ConflictError):
    """409 `already_solved` from a submission.

    `track` and `level_idx` name the earlier solve of the same account. Both
    are None in the rare cases where the API does not say.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        ConflictError.__init__(self, *args, **kwargs)
        body = self.body if isinstance(self.body, Mapping) else {}
        track = body.get("track")
        level_idx = body.get("level_idx")
        self.track: Optional[str] = track if isinstance(track, str) else None
        self.level_idx: Optional[int] = (
            level_idx if isinstance(level_idx, int) and not isinstance(level_idx, bool) else None)


class ValidationError(ApiError):
    """400, 413 or 422. The request was malformed or its content was refused."""


class RateLimitError(ApiError):
    """429. Wait `retry_after` seconds before trying again.

    `kind` is one of the three documented kinds: `rate_limited` for a used up
    request quota, `cooldown` for submitting too fast, and `run_limit_reached`
    for too many runs. `retry_after` is None when the API did not say how long
    to wait.
    """

    @property
    def kind(self) -> str:
        return self.code if self.code in RATE_LIMIT_KINDS else "rate_limited"


class RedirectError(ApiError):
    """The server answered with a redirect. The client never follows one."""


class ServerError(ApiError):
    """5xx. The server failed or a part of it is switched off."""


def doc_link(value: Any) -> Optional[str]:
    """Return a documentation link from the API when it is a plain https URL."""
    if isinstance(value, str) and value.startswith("https://") and len(value) <= 300:
        return value
    return None


def _class_for(status: int, code: str) -> type:
    if status == 401 or code == "unauthorized":
        return AuthenticationError
    if code == "insufficient_scope":
        return ScopeError
    if code == "not_an_agent":
        return NotAnAgentError
    if code == "admin_not_allowed":
        return AdminNotAllowedError
    if status == 403:
        return PaywallError if code == "subscription_required" else ForbiddenError
    if status == 404:
        return NotFoundError
    if status == 409:
        return AlreadySolvedError if code == "already_solved" else ConflictError
    if status in (400, 413, 422):
        return ValidationError
    if status == 429:
        return RateLimitError
    if 300 <= status < 400:
        return RedirectError
    if status >= 500:
        return ServerError
    return ApiError


def error_from(status: int, headers: Any, payload: Any) -> ApiError:
    """Build the matching ApiError from any answer that is not a success."""
    retry_after = _retry_after(headers, payload)
    if isinstance(payload, dict) and isinstance(payload.get("code"), str):
        code = payload["code"]
        detail = payload.get("detail")
        title = payload.get("title") if isinstance(payload.get("title"), str) else None
        if not isinstance(detail, str) or not detail:
            # The per IP 429 of the device flow and of /api/v1 names its code
            # next to a plain `error` message, without a detail or a title.
            message = payload.get("error")
            detail = title or (message if isinstance(message, str) and message else "Request failed.")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str):
            request_id = headers.get("Request-Id")
        kind = payload.get("type") if isinstance(payload.get("type"), str) else None
        return _class_for(status, code)(
            status, code, detail, doc_link(payload.get("doc_url")), request_id, retry_after,
            type=kind, title=title, body=payload)

    # Not a problem document: the answer came from the edge or a proxy.
    request_id = headers.get("Request-Id")
    body = payload if isinstance(payload, dict) else None
    if status == 429:
        return RateLimitError(status, "rate_limited", "Too many requests.", None, request_id,
                              retry_after, body=body)
    if status == 403:
        detail = ("The request was blocked before it reached the API. "
                  "A firewall or proxy on the way may be rejecting this client.")
    elif 300 <= status < 400:
        detail = "The server answered with a redirect, which this client does not follow."
    elif status == 404:
        detail = "The server has no such API endpoint. Check the base URL."
    elif status >= 500:
        detail = "The server is having trouble. Try again in a moment."
    else:
        detail = "The server sent an unexpected answer."
    code = "http_%d" % status
    return _class_for(status, code)(status, code, detail, None, request_id, retry_after, body=body)


def _retry_after(headers: Any, payload: Any) -> Optional[int]:
    value = headers.get("Retry-After")
    if value is None and isinstance(payload, dict):
        value = payload.get("retry_after_seconds")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return max(1, min(number, 7 * 24 * 3600))
