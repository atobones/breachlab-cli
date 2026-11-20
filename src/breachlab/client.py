"""HTTP client for the BreachLab API, standard library only."""

import http.client
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from breachlab import __version__

USER_AGENT = "breachlab-cli/%s" % __version__
DEFAULT_TIMEOUT = 15.0
MAX_BODY = 2 * 1024 * 1024
PAGE_SIZE = 100
MAX_PAGES = 500
# A read that is rate limited waits and retries on its own, but only when the
# API asks for a wait this short, and only this many times.
MAX_AUTO_WAIT = 60
MAX_AUTO_RETRIES = 2

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

sleep = time.sleep

_TOKEN_IN_TEXT = re.compile(r"blp_[A-Za-z0-9_-]+")
_SECRET_FIELD = re.compile(r'("(?:access_token|device_code)"\s*:\s*")[^"]*(")')


class ApiError(Exception):
    """The API answered with an error."""

    def __init__(self, status, code, detail, doc_url=None, request_id=None, retry_after=None):
        Exception.__init__(self, detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.doc_url = doc_url
        self.request_id = request_id
        self.retry_after = retry_after


class NetworkError(Exception):
    """The API could not be reached."""


class ConfigError(Exception):
    """The client is configured in a way it refuses to run with."""


class NotLoggedIn(Exception):
    """A command needs a token and there is none."""


def validate_base_url(url):
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
    # points. The API never redirects, so a redirect is treated as an error.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, base_url, token=None, timeout=DEFAULT_TIMEOUT, debug=None, notify=None,
                 auto_wait=True):
        self.base_url = validate_base_url(base_url)
        self.token = token
        self.timeout = timeout
        self._debug = debug
        self._notify = notify
        self.auto_wait = auto_wait
        self._secrets = set()
        if token:
            self._secrets.add(token)
        self._opener = urllib.request.build_opener(_NoRedirect())

    # -- secrets and debug output -------------------------------------------------

    def add_secret(self, value):
        if value:
            self._secrets.add(value)

    def redact(self, text):
        for secret in self._secrets:
            text = text.replace(secret, "***")
        text = _SECRET_FIELD.sub(r"\1***\2", text)
        return _TOKEN_IN_TEXT.sub("blp_***", text)

    def debug(self, text):
        if self._debug:
            self._debug(self.redact(text))

    # -- transport ---------------------------------------------------------------

    def send(self, method, path, query=None, body=None, auth=True):
        """One HTTP exchange. Return (status, headers, parsed JSON or None)."""
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            if not self.token:
                raise NotLoggedIn()
            headers["Authorization"] = "Bearer " + self.token

        self.debug("> %s %s" % (method, url))
        for name in sorted(headers):
            self.debug("> %s: %s" % (name, headers[name]))
        if data is not None:
            self.debug("> " + data.decode("utf-8"))

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
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

    def call(self, method, path, query=None, body=None, auth=True, wait=None):
        """Send a request and return its JSON, raising ApiError on any failure.

        When the API answers 429 with a short Retry-After, the request is
        retried after that many seconds. `wait` overrides the client default.
        """
        may_wait = self.auto_wait if wait is None else wait
        retries = 0
        while True:
            status, headers, payload = self.send(method, path, query=query, body=body, auth=auth)
            if 200 <= status < 300:
                if payload is None:
                    raise ApiError(status, "bad_response",
                                   "The server sent an answer that is not JSON.")
                return payload
            error = error_from(status, headers, payload)
            if (status == 429 and may_wait and retries < MAX_AUTO_RETRIES
                    and error.retry_after is not None and error.retry_after <= MAX_AUTO_WAIT):
                retries += 1
                if self._notify:
                    self._notify("Rate limited. Retrying in %s." % seconds(error.retry_after))
                sleep(error.retry_after)
                continue
            raise error

    def fetch_list(self, path, query=None, max_items=None):
        """Follow the API's cursors. Return (items, has_more).

        Fetches every page, or stops once `max_items` have been collected.
        `has_more` says whether the API still had items past the last one
        returned.
        """
        items = []
        cursor = None
        seen = set()
        for _ in range(MAX_PAGES):
            params = dict(query or {})
            want = PAGE_SIZE if max_items is None else min(PAGE_SIZE, max_items - len(items))
            params["limit"] = max(1, want)
            if cursor:
                params["starting_after"] = cursor
            page = self.call("GET", path, query=params)
            data = page.get("data") if isinstance(page, dict) else None
            if not isinstance(data, list):
                raise ApiError(200, "bad_response", "The server sent a list in an unexpected shape.")
            items.extend(data)
            cursor = page.get("next_cursor")
            more = bool(page.get("has_more")) and isinstance(cursor, str) and cursor != ""
            if max_items is not None and len(items) >= max_items:
                return items[:max_items], more or len(items) > max_items
            # A repeated cursor would loop forever; treat it as the end.
            if not more or cursor in seen or not data:
                return items, False
            seen.add(cursor)
        return items, True


def error_from(status, headers, payload):
    """Build an ApiError from any non-success answer."""
    retry_after = _retry_after(headers, payload)
    if isinstance(payload, dict) and isinstance(payload.get("code"), str):
        detail = payload.get("detail")
        if not isinstance(detail, str) or not detail:
            detail = payload.get("title") if isinstance(payload.get("title"), str) else "Request failed."
        doc_url = payload.get("doc_url")
        if not (isinstance(doc_url, str) and doc_url.startswith("https://")):
            doc_url = None
        request_id = payload.get("request_id")
        if not isinstance(request_id, str):
            request_id = headers.get("Request-Id")
        return ApiError(status, payload["code"], detail, doc_url, request_id, retry_after)

    # Not a problem document: the answer came from the edge or a proxy.
    request_id = headers.get("Request-Id")
    if status == 429:
        return ApiError(status, "rate_limited", "Too many requests.", None, request_id, retry_after)
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
    return ApiError(status, "http_%d" % status, detail, None, request_id, retry_after)


def _retry_after(headers, payload):
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


def seconds(count):
    return "1 second" if count == 1 else "%d seconds" % count


def _reason(exc):
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException):
        exc = reason
    elif isinstance(reason, str) and reason:
        return reason
    text = getattr(exc, "strerror", None) or str(exc) or exc.__class__.__name__
    return str(text).rstrip(".")
