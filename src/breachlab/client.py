"""The BreachLab API client, standard library only.

`BreachLab` is the one HTTP layer of this package. The `breachlab` command is
built on it, and it is the public Python SDK:

    from breachlab import BreachLab

    client = BreachLab()
    print(client.me()["username"])
"""

import hashlib
import http.client
import json
import logging
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from breachlab import config
from breachlab._version import __version__
from breachlab.errors import (  # noqa: F401  (re-exported for callers of this module)
    ApiError,
    ConfigError,
    DeviceFlowError,
    NetworkError,
    NotLoggedIn,
    ServerError,
    TokenError,
    UnexpectedResponse,
    doc_link,
    error_from,
    scrub,
)
from breachlab.models import (
    Account,
    BoxToken,
    DeviceAuthorization,
    DeviceToken,
    Level,
    LevelCredentials,
    LevelStats,
    ModelLeaderboardEntry,
    RunData,
    Solve,
    Step,
    StepBatchResult,
    SubmitResult,
    Token,
    TokenRevoked,
    Track,
    TrackProgress,
    User,
)
from breachlab.pagination import MAX_PAGES, PAGE_SIZE, Paginator  # noqa: F401

#: Sent by default. The edge in front of the API refuses the default agent of
#: urllib, so the client always names itself.
USER_AGENT = "breachlab-python/%s" % __version__
CLI_USER_AGENT = "breachlab-cli/%s" % __version__
DEFAULT_TIMEOUT = 15.0
DEFAULT_SCOPE = "read submit"
MAX_BODY = 2 * 1024 * 1024
# A rate limited request waits and retries on its own, but only when the API
# asks for a wait this short, and only this many times.
MAX_AUTO_WAIT = 60
MAX_AUTO_RETRIES = 2
# A failed request that is safe to repeat waits this long before each retry.
BACKOFF = (0.5, 1.0, 2.0, 4.0, 8.0)

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
RUN_STATUSES = ("solved", "gave_up", "error", "timeout")
# A 503 with one of these codes means a switch is off, not a hiccup.
_SWITCHED_OFF = ("agents_disabled", "play_disabled", "credentials_unavailable")

sleep = time.sleep

_log = logging.getLogger("breachlab")

Json = Dict[str, Any]


def validate_base_url(url: Optional[str]) -> str:
    """Return the base URL without a trailing slash, or raise ConfigError.

    Plain http would send the token in clear text, so it is accepted only
    for a server on this machine.
    """
    raw = (url or "").strip()
    try:
        parts = urllib.parse.urlsplit(raw)
        host = parts.hostname
        parts.port  # raises ValueError on a malformed port
    except ValueError:
        raise ConfigError("The API base URL is not a valid URL.")
    if parts.scheme not in ("http", "https") or not host:
        raise ConfigError("The API base URL must start with https://.")
    if parts.username or parts.password:
        raise ConfigError("The API base URL must not contain credentials.")
    if parts.query or parts.fragment:
        raise ConfigError("The API base URL must not contain a query string or a fragment.")
    if parts.scheme == "http" and host not in LOCAL_HOSTS:
        raise ConfigError(
            "Refusing to use a base URL that is not https. Plain http is allowed only for localhost."
        )
    return raw.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would replay the Authorization header to wherever a redirect
    # points. The API never redirects, so a redirect is treated as an error
    # and the token never leaves the host it was meant for.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BreachLab:
    """A client for the BreachLab API.

    token
        The API token. When left out it is read from the environment variable
        BREACHLAB_TOKEN, then from the file `breachlab login` saves. A client
        without a token can still run the device flow login.
    base_url
        Where the API lives. Defaults to BREACHLAB_API, then
        https://breachlab.org. Anything but https is refused, except for
        localhost and 127.0.0.1.
    timeout
        Seconds to wait for each answer.
    user_agent
        The User-Agent header. Give your program's own name and version.
    max_retries
        How many times one call is retried, for rate limits and for failures
        alike. 0 turns every retry off.
    max_retry_wait
        The longest Retry-After, in seconds, the client will sit through on
        its own. A longer wait raises RateLimitError at once.
    wait_on_rate_limit
        False raises RateLimitError on every 429 without waiting.
    retry_errors
        False stops the retries after network failures and server errors.
    debug
        True logs every request and answer to the `breachlab` logger at debug
        level. A callable receives each line instead. Tokens are redacted.
    notify
        A callable that is told, in a sentence, when the client waits for a
        rate limit.
    auto_token
        False keeps the client from looking for a token in the environment
        and in the saved file.
    """

    def __init__(self, token: Optional[str] = None, base_url: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT, user_agent: Optional[str] = None, *,
                 max_retries: int = MAX_AUTO_RETRIES, max_retry_wait: float = MAX_AUTO_WAIT,
                 wait_on_rate_limit: bool = True, retry_errors: bool = True,
                 debug: Union[bool, Callable[[str], None], None] = None,
                 notify: Optional[Callable[[str], None]] = None, auto_token: bool = True):
        self.base_url = validate_base_url(
            base_url or os.environ.get(config.ENV_API) or config.DEFAULT_API)
        self.token_source: Optional[str] = None
        if token is not None:
            if not config.valid_token(token):
                raise TokenError("The token does not look like a BreachLab token. "
                                 "Tokens start with blp_.")
            self.token_source = "argument"
        elif auto_token:
            token, self.token_source = config.load_token()
        self.token = token
        self.timeout = timeout
        agent = USER_AGENT if user_agent is None else str(user_agent).strip()
        if not agent or "\n" in agent or "\r" in agent:
            raise ConfigError("The User-Agent must be one line of text and must not be empty.")
        self.user_agent = agent
        self.max_retries = max(0, int(max_retries))
        self.max_retry_wait = max_retry_wait
        self.wait_on_rate_limit = wait_on_rate_limit
        self.retry_errors = retry_errors
        if debug is True:
            self._debug: Optional[Callable[[str], None]] = _log.debug
        else:
            self._debug = debug or None
        self._notify = notify
        self._secrets = set()
        if token:
            self._secrets.add(token)
        self._opener = urllib.request.build_opener(_NoRedirect())

    def __repr__(self) -> str:
        return "BreachLab(base_url=%r, token=%s)" % (self.base_url, "'***'" if self.token else None)

    # -- secrets and debug output -------------------------------------------------

    def add_secret(self, value: Optional[str]) -> None:
        """Have this value replaced in every line of debug output."""
        if value:
            self._secrets.add(value)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return scrub(text)

    def debug(self, text: str) -> None:
        if self._debug:
            self._debug(self.redact(text))

    # -- transport ---------------------------------------------------------------

    def send(self, method: str, path: str, query: Optional[Mapping[str, Any]] = None,
             body: Any = None, auth: bool = True,
             headers: Optional[Mapping[str, str]] = None) -> Tuple[int, Any, Any]:
        """One HTTP exchange. Return (status, headers, parsed JSON or None)."""
        if not path.startswith("/"):
            # Anything else could turn the host of the base URL into a user name.
            raise ConfigError("An API path must start with a slash.")
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        send_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            send_headers["Content-Type"] = "application/json"
        elif method == "POST":
            # An empty body with a Content-Length of 0. Some proxies refuse a
            # POST that states no length at all.
            data = b""
        for name, value in (headers or {}).items():
            send_headers[name] = value
        if auth:
            if not self.token:
                raise NotLoggedIn()
            send_headers["Authorization"] = "Bearer " + self.token

        self.debug("> %s %s" % (method, url))
        for name in sorted(send_headers):
            shown = "Bearer ***" if name == "Authorization" else send_headers[name]
            self.debug("> %s: %s" % (name, shown))
        if body is not None:
            self.debug("> " + data.decode("utf-8"))

        request = urllib.request.Request(url, data=data, headers=send_headers, method=method)
        started = time.monotonic()
        try:
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = response.getcode()
                reply_headers = response.headers
                raw = response.read(MAX_BODY + 1)
        except (socket.timeout, TimeoutError):
            raise NetworkError(
                "The API did not answer within %g seconds. Check your connection and try again."
                % self.timeout
            )
        except ssl.SSLError as exc:
            raise NetworkError("Could not make a secure connection to the API: %s." % _reason(exc))
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise NetworkError(
                    "The API did not answer within %g seconds. Check your connection and try again."
                    % self.timeout
                )
            raise NetworkError("Could not reach the API at %s: %s." % (self.base_url, _reason(exc)))
        except (http.client.HTTPException, OSError) as exc:
            raise NetworkError("Could not reach the API at %s: %s." % (self.base_url, _reason(exc)))

        text = raw[:MAX_BODY].decode("utf-8", "replace")
        self.debug("< HTTP %s in %d ms" % (status, (time.monotonic() - started) * 1000))
        for name in ("Content-Type", "Request-Id", "RateLimit-Limit", "RateLimit-Remaining",
                     "RateLimit-Reset", "Retry-After"):
            value = reply_headers.get(name)
            if value is not None:
                self.debug("< %s: %s" % (name, value))
        if text:
            self.debug("< " + (text if len(text) <= 4000 else text[:4000] + " ..."))

        payload = None
        if text:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
        return status, reply_headers, payload

    def call(self, method: str, path: str, query: Optional[Mapping[str, Any]] = None,
             body: Any = None, auth: bool = True, wait: Optional[bool] = None,
             headers: Optional[Mapping[str, str]] = None) -> Any:
        """Send a request and return its JSON, raising ApiError on any failure.

        A 429 with a short Retry-After is waited out and the request sent
        again. `wait` overrides the client's `wait_on_rate_limit` for this
        one call.

        A request that failed on the network or with a server error is sent
        again only when repeating it cannot do anything twice: a GET, or a
        request that carries an Idempotency-Key.
        """
        may_wait = self.wait_on_rate_limit if wait is None else wait
        repeatable = self.retry_errors and (
            method == "GET" or bool(headers and "Idempotency-Key" in headers))
        waits = 0
        failures = 0
        while True:
            try:
                status, reply, payload = self.send(method, path, query=query, body=body,
                                                   auth=auth, headers=headers)
            except NetworkError:
                if not repeatable or failures >= self.max_retries:
                    raise
                failures += 1
                sleep(_backoff(failures))
                continue
            if 200 <= status < 300:
                if payload is None:
                    raise ApiError(status, "bad_response",
                                   "The server sent an answer that is not JSON.")
                return payload
            error = error_from(status, reply, payload)
            if (status == 429 and may_wait and waits < self.max_retries
                    and error.retry_after is not None
                    and error.retry_after <= self.max_retry_wait):
                waits += 1
                if self._notify:
                    self._notify("Rate limited. Retrying in %s." % seconds(error.retry_after))
                sleep(error.retry_after)
                continue
            if repeatable and failures < self.max_retries and _transient(error):
                failures += 1
                sleep(_backoff(failures))
                continue
            raise error

    def paginate(self, path: str, query: Optional[Mapping[str, Any]] = None,
                 max_items: Optional[int] = None, page_size: int = PAGE_SIZE,
                 starting_after: Optional[str] = None) -> "Paginator[Any]":
        """A Paginator over any list endpoint, for one this client has no method for."""
        return Paginator(self, path, dict(query or {}), max_items, page_size, starting_after)

    def _object(self, payload: Any, what: str) -> Any:
        if not isinstance(payload, dict):
            raise UnexpectedResponse(200, "bad_response",
                                     "The server sent %s in an unexpected shape." % what)
        return payload

    # -- account -------------------------------------------------------------------

    def me(self) -> Account:
        """The account that owns the token. Scope `read`."""
        return self._object(self.call("GET", "/api/v2/me"), "an account")

    def progress(self) -> "Paginator[TrackProgress]":
        """Progress on every launched track, one entry per track. Scope `read`."""
        return Paginator(self, "/api/v2/me/progress")

    def solves(self, max_items: Optional[int] = None, page_size: int = PAGE_SIZE,
               starting_after: Optional[str] = None) -> "Paginator[Solve]":
        """The account's solves, newest first. Scope `read`."""
        return Paginator(self, "/api/v2/me/solves", None, max_items, page_size, starting_after)

    def token_info(self) -> Token:
        """The calling token: scopes, creation, expiry. Never the value. Scope `read`."""
        return self._object(self.call("GET", "/api/v2/token"), "a token")

    def revoke_token(self, wait: Optional[bool] = None) -> TokenRevoked:
        """Revoke the calling token. It stops working at once. Scope `read`.

        The client keeps no token afterwards. A second call with the same
        token raises AuthenticationError, which then means it is done.
        """
        answer = self._object(self.call("DELETE", "/api/v2/token", wait=wait), "a token")
        if answer.get("revoked") is True:
            self.token = None
        return answer

    # -- catalog -------------------------------------------------------------------

    def tracks(self) -> "Paginator[Track]":
        """The launched tracks. Scope `read`."""
        return Paginator(self, "/api/v2/tracks")

    def levels(self, track: str, max_items: Optional[int] = None, page_size: int = PAGE_SIZE,
               starting_after: Optional[str] = None) -> "Paginator[Level]":
        """The levels of a track. Scope `read`."""
        return Paginator(self, "/api/v2/tracks/%s/levels" % _part(track), None, max_items,
                         page_size, starting_after)

    def level(self, track: str, idx: int) -> Level:
        """One level with its briefing. Scope `read`."""
        idx = _index(idx)
        level = self.call("GET", "/api/v2/tracks/%s/levels/%d" % (_part(track), idx))
        if not isinstance(level, dict) or level.get("idx") != idx:
            raise UnexpectedResponse(200, "bad_response",
                                     "The server sent a level in an unexpected shape.")
        return level  # type: ignore[return-value]

    def level_stats(self, track: str, idx: int) -> LevelStats:
        """How people and agents do on one level. Scope `read`."""
        return self._object(
            self.call("GET", "/api/v2/tracks/%s/levels/%d/stats" % (_part(track), _index(idx))),
            "level statistics")

    # -- playing -------------------------------------------------------------------

    def credentials(self, track: str, idx: int) -> LevelCredentials:
        """What the caller needs to connect to one level. Scope `play`.

        The answer can hold a password. The client adds it to the values it
        redacts from debug output; keep it out of your own logs too.
        """
        answer = self._object(
            self.call("GET", "/api/v2/tracks/%s/levels/%d/credentials"
                      % (_part(track), _index(idx))),
            "connection details")
        if isinstance(answer.get("password"), str):
            self.add_secret(answer["password"])
        header = answer.get("access_header")
        if isinstance(header, dict) and isinstance(header.get("value"), str):
            self.add_secret(header["value"])
        return answer

    def create_play_token(self) -> BoxToken:
        """Mint a box token for the caller's account. Scope `play`.

        Every call mints another token, so this is never repeated after a
        failure.
        """
        answer = self._object(self.call("POST", "/api/v2/play/token"), "a box token")
        if isinstance(answer.get("token"), str):
            self.add_secret(answer["token"])
        return answer

    # -- submitting ----------------------------------------------------------------

    def submit(self, flag: str, run_id: Optional[str] = None, *,
               wait: bool = False) -> SubmitResult:
        """Submit a flag. Scope `submit`.

        Returns the verdict; look at `result`. A wrong flag is an answer and
        not an error. A submission is never repeated on its own: a wrong flag
        earns a growing cooldown, so a 429 raises RateLimitError at once.
        Pass `wait=True` to sit through one short wait instead.

        `run_id` records the answer on an open run of the calling agent.
        """
        body: Json = {"flag": flag}
        if run_id is not None:
            body["run_id"] = run_id
        result = self.call("POST", "/api/v2/submit", body=body, wait=bool(wait))
        if not isinstance(result, dict) or result.get("result") not in ("correct", "incorrect"):
            raise UnexpectedResponse(200, "bad_response",
                                     "The server sent a verdict in an unexpected shape.")
        return result  # type: ignore[return-value]

    # -- community -----------------------------------------------------------------

    def leaderboard(self, board: str = "humans", period: str = "all",
                    country: Optional[str] = None, max_items: Optional[int] = None,
                    page_size: int = PAGE_SIZE,
                    starting_after: Optional[str] = None) -> "Paginator[Any]":
        """A leaderboard. Scope `read`.

        `board` is `humans` or `agents`, `period` is `all`, `weekly` or
        `active`, `country` a two letter code. The agents board has only the
        all time period and no country filter, and its entries carry the
        agent's name, model, number and owner.
        """
        query: Json = {}
        if board != "humans":
            query["board"] = board
        if period != "all":
            query["period"] = period
        if country:
            query["country"] = country.lower()
        return Paginator(self, "/api/v2/leaderboard", query, max_items, page_size, starting_after)

    def model_leaderboard(self, max_items: Optional[int] = None, page_size: int = PAGE_SIZE,
                          starting_after: Optional[str] = None
                          ) -> "Paginator[ModelLeaderboardEntry]":
        """The board by declared model: the best listed agent of each. Scope `read`."""
        return Paginator(self, "/api/v2/leaderboard/models", None, max_items, page_size,
                         starting_after)

    def user(self, username: str) -> User:
        """A public profile. Scope `read`."""
        return self._object(self.call("GET", "/api/v2/users/%s" % _part(username)), "a profile")

    # -- runs ----------------------------------------------------------------------

    def create_run(self, track: str, model: Optional[str] = None, harness: Optional[str] = None,
                   harness_version: Optional[str] = None,
                   idempotency_key: Optional[str] = None) -> RunData:
        """Open a run of the calling agent on a track. Scope `agent`.

        `model` is the agent's own declaration as `provider/model`. Left out,
        the model declared on the agent's account is used.
        """
        body: Json = {"track": track}
        if model is not None:
            body["model"] = model
        if harness is not None:
            body["harness"] = harness
        if harness_version is not None:
            body["harness_version"] = harness_version
        return self._object(
            self.call("POST", "/api/v2/runs", body=body, headers=_key(idempotency_key)), "a run")

    def get_run(self, run_id: str) -> RunData:
        """One run of the agent, or of an agent the caller owns. Scope `read`."""
        return self._object(self.call("GET", "/api/v2/runs/%s" % _part(run_id)), "a run")

    def runs(self, state: Optional[str] = None, max_items: Optional[int] = None,
             page_size: int = PAGE_SIZE,
             starting_after: Optional[str] = None) -> "Paginator[RunData]":
        """The runs the caller may read, newest first. Scope `read`.

        `state` is `open` or `finished`. Without it every run is listed, the
        timed out ones included.
        """
        query = {"state": state} if state is not None else None
        return Paginator(self, "/api/v2/runs", query, max_items, page_size, starting_after)

    def append_steps(self, run_id: str, steps: Sequence[Step],
                     idempotency_key: Optional[str] = None) -> StepBatchResult:
        """Append up to 100 steps to an open run. Scope `agent`.

        Each step is a dict with `seq`, `ts`, `type` and `content`. Without
        an `idempotency_key` one is derived from the run and the steps, so
        the same batch always carries the same key and a retry stores
        nothing twice.
        """
        batch = [dict(step) for step in steps]
        path = "/api/v2/runs/%s/steps" % _part(run_id)
        if idempotency_key is None:
            digest = hashlib.sha256(
                (path + "\n" + json.dumps(batch, sort_keys=True)).encode("utf-8")).hexdigest()
            idempotency_key = "steps-" + digest[:48]
        return self._object(
            self.call("POST", path, body={"steps": batch}, headers=_key(idempotency_key)),
            "a step batch")

    def finish_run(self, run_id: str, status: str, tokens_used: Optional[int] = None,
                   cost_usd: Optional[float] = None,
                   idempotency_key: Optional[str] = None) -> RunData:
        """Close a run. Scope `agent`.

        `status` is what the agent reports: `solved`, `gave_up`, `error` or
        `timeout`. The result of the run is computed by the server. The API
        takes at most 4 decimals of `cost_usd`, so it is rounded to that.
        """
        body: Json = {"status": status}
        if tokens_used is not None:
            body["tokens_used"] = tokens_used
        if cost_usd is not None:
            body["cost_usd"] = round(cost_usd, 4)
        return self._object(
            self.call("POST", "/api/v2/runs/%s/finish" % _part(run_id), body=body,
                      headers=_key(idempotency_key)),
            "a run")

    def run(self, track: str, model: Optional[str] = None, harness: Optional[str] = None,
            harness_version: Optional[str] = None, batch_size: int = 1) -> "Run":
        """A context manager that opens a run and closes it on exit.

            with client.run("ghost", harness="my-harness") as run:
                run.step("thought", "Looking at the login banner.")
                run.submit(flag)
        """
        from breachlab.runs import Run
        return Run(self, track, model=model, harness=harness, harness_version=harness_version,
                   batch_size=batch_size)

    def resume_run(self, run_id: str, batch_size: int = 1) -> "Run":
        """The same context manager for a run that is already open.

        A restarted agent finds its run with `runs(state="open")`. The steps
        continue after the last one the server took.
        """
        from breachlab.runs import Run
        return Run(self, None, run_id=run_id, batch_size=batch_size)

    # -- device flow login -----------------------------------------------------------

    def start_device_login(self, scope: str = DEFAULT_SCOPE) -> DeviceAuthorization:
        """Start the device flow. Needs no token.

        `scope` names the scopes to ask for, separated by spaces. Show
        `user_code` and `verification_uri_complete` to a person, then call
        `poll_device_login` with the `device_code`.
        """
        start = self.call("POST", "/api/v1/auth/device", body={"scope": scope}, auth=False)
        device_code = start.get("device_code") if isinstance(start, dict) else None
        user_code = start.get("user_code") if isinstance(start, dict) else None
        if not isinstance(device_code, str) or not isinstance(user_code, str):
            raise UnexpectedResponse(200, "bad_response",
                                     "The server started the login in an unexpected way.")
        self.add_secret(device_code)
        url = start.get("verification_uri_complete") or start.get("verification_uri")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise UnexpectedResponse(200, "bad_response",
                                     "The server did not say where to approve the login.")
        return start

    def poll_device_login(self, device_code: str, interval: int = 5,
                          expires_in: int = 600) -> DeviceToken:
        """Wait until the login is approved and return the token answer.

        Raises DeviceFlowError when the request is denied or the code runs
        out. `interval` and `expires_in` come from `start_device_login`.
        """
        self.add_secret(device_code)
        elapsed = 0
        failures = 0
        while True:
            # One second over the interval, so network jitter cannot make a poll
            # arrive early and earn a slow_down.
            pause = interval + 1
            if elapsed + pause > expires_in:
                raise DeviceFlowError("expired_token",
                                      "The code expired before it was approved.",
                                      expired_locally=True)
            sleep(pause)
            elapsed += pause
            try:
                status, headers, payload = self.send(
                    "POST", "/api/v1/auth/token", body={"device_code": device_code}, auth=False)
            except NetworkError:
                failures += 1
                if failures >= 3:
                    raise
                continue
            failures = 0
            if status == 200 and isinstance(payload, dict) and "access_token" in payload:
                self.add_secret(payload.get("access_token")
                                if isinstance(payload.get("access_token"), str) else None)
                return payload  # type: ignore[return-value]
            error = payload.get("error") if isinstance(payload, dict) else None
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            # Newer servers explain the error and link to its documentation.
            described = payload.get("error_description") if isinstance(payload, dict) else None
            if not isinstance(described, str) or not described.strip():
                described = None
            else:
                described = described.strip()[:300].rstrip(".") + "."
            doc_url = doc_link(payload.get("doc_url")) if isinstance(payload, dict) else None
            if error in ("access_denied", "expired_token") or (
                    status == 400 and isinstance(error, str) and error):
                raise DeviceFlowError(error[:64], described, doc_url)
            failure = error_from(status, headers, payload)
            if status == 429:
                pause = failure.retry_after or interval
                if elapsed + pause > expires_in:
                    raise failure
                sleep(pause)
                elapsed += pause
                continue
            raise failure

    def login(self, on_code: Callable[[DeviceAuthorization], None],
              scope: str = DEFAULT_SCOPE, save: bool = True) -> DeviceToken:
        """Log in with the device flow and start using the new token.

        `on_code` is called once with the started login, so the program can
        show `user_code` and `verification_uri_complete` to a person. The
        call then blocks until that person approves or denies the request.

        With `save` the token is written to the same file `breachlab login`
        uses, readable by its owner only, so later clients find it.
        """
        start = self.start_device_login(scope)
        on_code(start)
        granted = self.poll_device_login(
            start["device_code"],
            _positive_int(start.get("interval"), 5),
            _positive_int(start.get("expires_in"), 600))
        token = granted.get("access_token")
        if not config.valid_token(token):
            raise UnexpectedResponse(200, "bad_response",
                                     "The server returned a token in an unexpected format.")
        self.add_secret(token)
        self.token = token
        self.token_source = "login"
        if save:
            config.save_token(token)
        return granted


def endpoint_missing(error: BaseException) -> bool:
    """True when the server has no such endpoint or method.

    Newer endpoints answer 404 or 405 on a server that does not have them
    yet. The caller then falls back to the older way of getting the same
    answer, so one client works against both.
    """
    return isinstance(error, ApiError) and error.status in (404, 405)


def seconds(count: float) -> str:
    return "1 second" if count == 1 else "%d seconds" % count


def _transient(error: ApiError) -> bool:
    if error.code == "idempotency_in_progress":
        return True
    return (isinstance(error, ServerError) and error.status in (500, 502, 503, 504)
            and error.code not in _SWITCHED_OFF)


def _backoff(attempt: int) -> float:
    return BACKOFF[min(attempt, len(BACKOFF)) - 1]


def _key(idempotency_key: Optional[str]) -> Optional[Dict[str, str]]:
    if idempotency_key is None:
        return None
    key = str(idempotency_key)
    # The API takes 1 to 255 printable ASCII characters without spaces.
    if not 1 <= len(key) <= 255 or any(not "!" <= ch <= "~" for ch in key):
        raise ValueError("An idempotency key is 1 to 255 printable ASCII characters, no spaces.")
    return {"Idempotency-Key": key}


def new_idempotency_key() -> str:
    """A fresh random key for a request that must not run twice."""
    return uuid.uuid4().hex


def _part(value: Any) -> str:
    return urllib.parse.quote(str(value), safe="")


def _index(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("A level index is a whole number, 0 or more.")
    return value


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException):
        exc = reason
    elif isinstance(reason, str) and reason:
        return reason
    text = getattr(exc, "strerror", None) or str(exc) or exc.__class__.__name__
    return str(text).rstrip(".")


__all__: List[str] = ["BreachLab", "USER_AGENT", "DEFAULT_TIMEOUT", "validate_base_url",
                      "endpoint_missing", "new_idempotency_key"]
