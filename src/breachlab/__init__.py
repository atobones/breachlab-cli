"""Python SDK and command-line client for the BreachLab API.

    from breachlab import BreachLab

    client = BreachLab()
    print(client.me()["username"])
"""

from breachlab._version import __version__
from breachlab.client import BreachLab, new_idempotency_key
from breachlab.errors import (
    AlreadySolvedError,
    ApiError,
    AuthenticationError,
    BreachLabError,
    ConfigError,
    ConflictError,
    DeviceFlowError,
    ForbiddenError,
    NetworkError,
    NotFoundError,
    NotLoggedIn,
    PaywallError,
    RateLimitError,
    RedirectError,
    ScopeError,
    ServerError,
    TokenError,
    UnexpectedResponse,
    ValidationError,
)
from breachlab.pagination import Page, Paginator
from breachlab.runs import Run

__all__ = [
    "__version__",
    "BreachLab",
    "Run",
    "Page",
    "Paginator",
    "new_idempotency_key",
    "BreachLabError",
    "ApiError",
    "AuthenticationError",
    "ScopeError",
    "ForbiddenError",
    "PaywallError",
    "NotFoundError",
    "ConflictError",
    "AlreadySolvedError",
    "ValidationError",
    "RateLimitError",
    "RedirectError",
    "ServerError",
    "UnexpectedResponse",
    "NetworkError",
    "ConfigError",
    "TokenError",
    "NotLoggedIn",
    "DeviceFlowError",
]
