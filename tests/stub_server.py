"""A local stand-in for the BreachLab API.

The shapes follow https://breachlab.org/api/v2/openapi.json and the device
flow under /api/v1/auth. It serves on 127.0.0.1 and records every request so
tests can assert on what the client actually sent.

Set `State.new_endpoints` to False to get the API as it was before token
details, self revoke, progress, the single level and the leaderboard place
existed. Those paths then answer the way an unknown path does.
"""

import base64
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

TOKEN = "blp_" + "A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6S7t8U9v0W1x"
DEVICE_CODE = "dc_" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp9Oo8Nn7Mm6L"
USER_CODE = "GX50-R07J"
TEST_FLAG = "BL{api_v2_test}"
GOOD_FLAG = "BL{stub_correct_flag}"
SOLVED_FLAG = "BL{stub_already_solved}"
DOC_BASE = "https://breachlab.org/docs/api/errors/"
SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")
LEVEL_IDX_RE = re.compile(r"^(0|[1-9][0-9]{0,3})$")
ONE_LEVEL_RE = re.compile(r"^/tracks/([^/]+)/levels/([^/]+)$")

TITLES = {
    401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 400: "Bad Request",
    409: "Conflict", 413: "Payload Too Large", 422: "Unprocessable Entity",
    429: "Too Many Requests", 500: "Internal Server Error",
}
DETAILS = {
    "unauthorized": "The request has no valid API token, so it was not authenticated.",
    "insufficient_scope": "The API token does not carry the scope this endpoint requires.",
    "not_found": "The requested resource does not exist or is not available to this token.",
    "invalid_request": "The request is malformed or uses a disallowed method of authentication.",
    "invalid_format": "The request is well formed but its content is not in the expected format.",
    "invalid_parameter": "A query parameter is missing, out of range, or not allowed with the other parameters.",
    "invalid_cursor": "The pagination cursor is invalid or was not issued by this endpoint.",
    "rate_limited": "The token has exceeded the request limit for its rate class; retry after the time in Retry-After.",
    "cooldown": "A cooldown is in effect for this action; retry after the time in Retry-After.",
    "already_solved": "The level has already been solved by this user, so it cannot be scored again.",
    "email_unverified": "The account must verify its email address before this action is allowed.",
    "internal_error": "An unexpected error occurred on the server; retry the request later.",
}
DEVICE_ERRORS = {
    "authorization_pending": "The request has not been approved in the browser yet.",
    "slow_down": "The device code was polled again before the polling interval had passed.",
    "access_denied": "The request was denied, or the account already holds the maximum number "
                     "of active tokens.",
    "expired_token": "The device code is unknown, has expired or was already exchanged for a token.",
}


def make_levels(track, count, solved):
    difficulties = ["easy", "easy", "medium", "hard", "insane"]
    levels = []
    for idx in range(count):
        if idx < solved:
            status = "solved"
        elif idx == solved:
            status = "available"
        else:
            status = "locked"
        levels.append({
            "object": "level",
            "track": track,
            "idx": idx,
            "title": "%s Level %d" % (track.capitalize(), idx),
            "description": "Briefing for %s level %d." % (track, idx),
            "difficulty": difficulties[idx % len(difficulties)],
            "points": 100 + idx * 10,
            "first_blood_bonus": 50,
            "status": status,
            "access": "free",
        })
    return levels


class State:
    """Everything a test may want to tune or inspect."""

    def __init__(self):
        self.requests = []
        self.token = TOKEN
        self.scopes = ["read", "submit"]
        # False turns the stub into the API before the newer endpoints shipped.
        self.new_endpoints = True
        # What such a server answers on a path it lacks: 404 or 405.
        self.missing_status = 404
        # Scopes the device flow grants. Anything else is invalid_scope.
        self.allowed_scopes = ["read", "submit"]
        # Set by DELETE /api/v2/token. A revoked token is an unknown token.
        self.revoked = False
        self.token_created_at = "2026-11-19T20:40:12.000Z"
        self.token_expires_at = None
        # Answer /api/v2/token with this error status instead of the token.
        self.token_failure = None
        # Where device flow errors point for documentation.
        self.device_doc_base = DOC_BASE
        # Answers handed out by POST /api/v1/auth/token, one per poll.
        self.poll_script = ["authorization_pending", "approved"]
        # The next N authenticated v2 requests answer 429.
        self.rate_limit_next = 0
        self.retry_after = 7
        self.send_retry_after = True
        self.retry_in_body = False
        self.submit_cooldown = None
        self.email_verified = True
        # Answer everything with the plain text 403 an edge firewall sends.
        self.edge_block = False
        # Answer the next v2 request with a redirect to this URL.
        self.redirect_next = None
        self.account = {
            "object": "account",
            "username": "operator_01",
            "is_agent": False,
            "agent": None,
            "points": 1200,
            "solved": 12,
            "leaderboard": {"board": "humans", "rank": 42, "points": 1200},
            "access": {
                "paywall_enabled": True,
                "subscription_active": False,
                "tracks": {"ghost": "free", "phantom": "locked"},
            },
        }
        self.tracks = [
            {"object": "track", "slug": "ghost", "name": "Ghost", "status": "live",
             "level_count": 5, "access": "free",
             "difficulty_mix": {"easy": 2, "medium": 1, "hard": 1, "insane": 1}},
            {"object": "track", "slug": "phantom", "name": "Phantom", "status": "live",
             "level_count": 3, "access": "locked",
             "difficulty_mix": {"easy": 2, "medium": 1, "hard": 0, "insane": 0}},
        ]
        self.levels = {"ghost": make_levels("ghost", 5, 2), "phantom": make_levels("phantom", 3, 0)}
        for level in self.levels["phantom"]:
            level["access"] = "locked"
            level["description"] = None
            level["status"] = "locked"
        self.leaderboard = [
            {"object": "leaderboard_entry", "rank": i + 1, "username": "player_%03d" % i,
             "points": 9000 - i * 10, "solved": 90 - i // 4,
             "country": "us" if i % 2 == 0 else None, "is_hall_of_fame": i == 0}
            for i in range(250)
        ]
        self.agents = [
            {"object": "leaderboard_entry", "rank": 1, "username": "agent_runner", "points": 1200,
             "solved": 12, "country": None, "is_hall_of_fame": False, "agent_name": "Agent 004",
             "agent_model": None, "agent_number": 4},
        ]
        self.solves = [
            {"object": "solve", "id": str(uuid.UUID(int=i)), "track": "ghost", "level_idx": 44 - i,
             "title": "Ghost Level %d" % (44 - i), "points": 100,
             "solved_at": "2026-11-%02dT09:30:00Z" % (1 + i % 28)}
            for i in range(45)
        ]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "stub"

    def log_message(self, fmt, *args):
        pass

    # -- plumbing ----------------------------------------------------------------

    @property
    def state(self):
        return self.server.state

    def _reply(self, status, body, content_type="application/json", headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        self.wfile.write(raw)

    def _problem(self, status, code, extra=None, headers=None):
        request_id = "req_" + uuid.uuid4().hex[:24]
        doc = "https://breachlab.org/docs/api/errors/" + code
        body = {"type": doc, "title": TITLES.get(status, "Error"), "status": status, "code": code,
                "detail": DETAILS.get(code, "Error."), "doc_url": doc, "request_id": request_id}
        body.update(extra or {})
        all_headers = {"Request-Id": request_id}
        all_headers.update(headers or {})
        self._reply(status, body, "application/problem+json", all_headers)

    def _ok(self, body):
        self._reply(200, body, headers={
            "Request-Id": "req_" + uuid.uuid4().hex[:24],
            "RateLimit-Limit": 60, "RateLimit-Remaining": 59, "RateLimit-Reset": 60,
        })

    def _handle(self, method):
        parts = urlsplit(self.path)
        query = parse_qsl(parts.query, keep_blank_values=True)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.state.requests.append({
            "method": method, "path": parts.path, "query": dict(query), "raw_path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()}, "body": raw,
        })
        path = parts.path
        if self.state.edge_block:
            return self._reply(403, b"error code: 1010\n", "text/plain; charset=UTF-8")
        if self.state.redirect_next and path.startswith("/api/v2/"):
            target, self.state.redirect_next = self.state.redirect_next, None
            return self._reply(302, b"", "text/plain", {"Location": target})
        if path.startswith("/api/v1/auth/"):
            return self._device(method, path, raw)
        if path.startswith("/api/v2/"):
            return self._v2(method, path[len("/api/v2"):], dict(query), raw)
        self._reply(404, b"<html>not found</html>", "text/html; charset=utf-8")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def _missing(self):
        """The answer of a server that has no such path or method."""
        if self.state.missing_status == 405:
            return self._reply(405, b"", "text/plain")
        self._reply(404, b"<html>not found</html>", "text/html; charset=utf-8")

    # -- device flow, /api/v1/auth -----------------------------------------------

    def _v1_problem(self, status, code, detail):
        body = {"type": "https://breachlab.org/errors/" + code,
                "title": TITLES.get(status, "Error"), "status": status,
                "code": code, "detail": detail}
        if self.state.new_endpoints:
            body["doc_url"] = self.state.device_doc_base + code
        self._reply(status, body, "application/problem+json")

    def _oauth_error(self, code):
        body = {"error": code}
        if self.state.new_endpoints:
            body["error_description"] = DEVICE_ERRORS.get(code, "The request failed.")
            body["doc_url"] = self.state.device_doc_base + code
        self._reply(400, body)

    def _device(self, method, path, raw):
        if method != "POST":
            return self._reply(405, b"", "text/plain")
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = None
        if path == "/api/v1/auth/device":
            allowed = self.state.allowed_scopes
            refusal = "Allowed scopes: " + ", ".join(allowed)
            raw_scope = body.get("scope", "read") if isinstance(body, dict) else "read"
            if not isinstance(raw_scope, str) or len(raw_scope) > 64:
                return self._v1_problem(400, "invalid_scope", refusal)
            scope = raw_scope.split()
            if any(s not in allowed for s in scope):
                return self._v1_problem(400, "invalid_scope", refusal)
            # Every token can read, whatever else was asked for.
            self.state.scopes = ["read"] + [s for s in allowed if s in scope and s != "read"]
            base = "http://%s:%d" % self.server.server_address[:2]
            return self._reply(200, {
                "device_code": DEVICE_CODE,
                "user_code": USER_CODE,
                "verification_uri": base + "/activate",
                "verification_uri_complete": base + "/activate?code=" + USER_CODE,
                "expires_in": 600,
                "interval": 5,
            })
        if path == "/api/v1/auth/token":
            if body is None:
                return self._v1_problem(400, "invalid_request", "Body must be JSON")
            if not body.get("device_code"):
                return self._v1_problem(400, "invalid_request", "device_code required")
            if body["device_code"] != DEVICE_CODE:
                return self._oauth_error("expired_token")
            step = self.state.poll_script.pop(0) if self.state.poll_script else "expired_token"
            if step == "approved":
                return self._reply(200, {"access_token": self.state.token, "token_type": "bearer",
                                         "scope": " ".join(self.state.scopes)})
            return self._oauth_error(step)
        self._reply(404, b"<html>not found</html>", "text/html; charset=utf-8")

    # -- API v2 --------------------------------------------------------------------

    def _v2(self, method, path, query, raw):
        state = self.state
        for name, value in query.items():
            if name.lower() in ("token", "access_token", "api_key", "key", "authorization",
                                "bearer") or name.startswith("blp_") or value.startswith("blp_"):
                return self._problem(400, "invalid_request")
        if state.revoked or self.headers.get("Authorization") != "Bearer " + state.token:
            return self._problem(401, "unauthorized")
        if state.rate_limit_next > 0:
            state.rate_limit_next -= 1
            headers = {"Retry-After": state.retry_after} if state.send_retry_after else {}
            extra = {}
            if state.send_retry_after or state.retry_in_body:
                extra["retry_after_seconds"] = state.retry_after
            return self._problem(429, "rate_limited", extra, headers)

        if path == "/submit":
            if method != "POST":
                return self._reply(405, b"", "text/plain")
            return self._submit(raw)
        if path == "/token":
            if not state.new_endpoints:
                return self._missing()
            if method not in ("GET", "DELETE"):
                return self._reply(405, b"", "text/plain")
            if "read" not in state.scopes:
                return self._problem(403, "insufficient_scope")
            if state.token_failure:
                return self._problem(state.token_failure, "internal_error")
            if method == "DELETE":
                state.revoked = True
                return self._ok({"object": "token", "revoked": True})
            return self._ok({"object": "token", "scopes": list(state.scopes),
                             "created_at": state.token_created_at,
                             "expires_at": state.token_expires_at,
                             "is_agent": bool(state.account.get("is_agent"))})
        if method != "GET":
            # A method the path does not have. An unknown path is a 404 instead.
            known = (path in ("/me", "/me/solves", "/tracks", "/leaderboard")
                     or (path.startswith("/tracks/") and path.endswith("/levels")))
            return self._reply(405, b"", "text/plain") if known else self._missing()
        if "read" not in state.scopes:
            return self._problem(403, "insufficient_scope")
        if path == "/me":
            if state.new_endpoints:
                return self._ok(state.account)
            return self._ok({k: v for k, v in state.account.items() if k != "leaderboard"})
        if path == "/me/progress":
            if not state.new_endpoints:
                return self._missing()
            if "starting_after" in query or "ending_before" in query:
                return self._problem(400, "invalid_cursor")
            return self._ok({"object": "list", "data": self._progress(), "has_more": False,
                             "next_cursor": None, "prev_cursor": None})
        one_level = ONE_LEVEL_RE.match(path)
        if one_level:
            if not state.new_endpoints:
                return self._missing()
            slug, raw_idx = one_level.groups()
            if not SLUG_RE.match(slug) or not LEVEL_IDX_RE.match(raw_idx):
                return self._problem(404, "not_found")
            for level in state.levels.get(slug, []):
                if level["idx"] == int(raw_idx):
                    return self._ok(level)
            return self._problem(404, "not_found")
        if path == "/me/solves":
            return self._page(state.solves, "solves", query)
        if path == "/tracks":
            if "starting_after" in query or "ending_before" in query:
                return self._problem(400, "invalid_cursor")
            return self._ok({"object": "list", "data": state.tracks, "has_more": False,
                             "next_cursor": None, "prev_cursor": None})
        if path.startswith("/tracks/") and path.endswith("/levels"):
            slug = path[len("/tracks/"):-len("/levels")]
            if slug not in state.levels:
                return self._problem(404, "not_found")
            return self._page(state.levels[slug], "levels:" + slug, query)
        if path == "/leaderboard":
            board = query.get("board", "humans")
            period = query.get("period", "all")
            if board not in ("humans", "agents") or period not in ("all", "weekly", "active"):
                return self._problem(400, "invalid_parameter")
            if board == "agents":
                if period != "all" or "country" in query:
                    return self._problem(400, "invalid_parameter")
                return self._page(state.agents, "agents", query)
            entries = state.leaderboard
            if "country" in query:
                if period != "all":
                    return self._problem(400, "invalid_parameter")
                entries = [e for e in entries if e["country"] == query["country"].lower()]
            return self._page(entries, "humans:%s:%s" % (period, query.get("country", "")), query)
        if path.startswith("/users/"):
            return self._problem(404, "not_found")
        self._reply(404, b"<html>not found</html>", "text/html; charset=utf-8")

    def _progress(self):
        report = []
        for track in self.state.tracks:
            levels = self.state.levels.get(track["slug"], [])
            open_idx = [lv["idx"] for lv in levels if lv["status"] == "available"]
            report.append({
                "object": "track_progress",
                "track": track["slug"],
                "name": track["name"],
                "access": track["access"],
                "level_count": len(levels),
                "solved": sum(1 for lv in levels if lv["status"] == "solved"),
                "next_level_idx": min(open_idx) if open_idx else None,
            })
        return report

    def _page(self, items, kind, query):
        try:
            limit = int(query.get("limit", "20"))
        except ValueError:
            return self._problem(400, "invalid_parameter")
        if limit < 1 or ("starting_after" in query and "ending_before" in query):
            return self._problem(400, "invalid_parameter")
        limit = min(limit, 100)
        offset = 0
        if "starting_after" in query:
            cursor = _decode_cursor(query["starting_after"])
            if cursor is None or cursor.get("k") != kind:
                return self._problem(400, "invalid_cursor")
            offset = cursor["o"]
        data = items[offset:offset + limit]
        end = offset + len(data)
        has_more = end < len(items)
        self._ok({
            "object": "list",
            "data": data,
            "has_more": has_more,
            "next_cursor": _encode_cursor(end, kind) if has_more else None,
            "prev_cursor": _encode_cursor(offset, kind) if offset > 0 else None,
        })

    def _submit(self, raw):
        state = self.state
        if "submit" not in state.scopes:
            return self._problem(403, "insufficient_scope")
        if len(raw) > 4096:
            return self._problem(413, "payload_too_large")
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._problem(400, "invalid_request")
        if not isinstance(body, dict):
            return self._problem(400, "invalid_request")
        flag = body.get("flag")
        if not isinstance(flag, str) or not 4 <= len(flag) <= 128:
            return self._problem(422, "invalid_format")
        if flag == TEST_FLAG:
            return self._reply(200, {"result": "correct", "test": True})
        limits = {"RateLimit-Limit": 300, "RateLimit-Remaining": 299, "RateLimit-Reset": 3600}
        if state.submit_cooldown is not None:
            seconds = state.submit_cooldown
            headers = dict(limits)
            extra = {}
            if seconds:
                headers["Retry-After"] = seconds
                extra["retry_after_seconds"] = seconds
            return self._problem(429, "cooldown", extra, headers)
        if not state.email_verified:
            return self._problem(403, "email_unverified", headers=limits)
        if flag == SOLVED_FLAG:
            return self._problem(409, "already_solved", headers=limits)
        if flag == GOOD_FLAG:
            return self._reply(200, {"result": "correct", "track": "ghost", "level_idx": 2,
                                     "points_awarded": 120, "first_blood": True,
                                     "track_completed": False}, headers=limits)
        self._reply(200, {"result": "incorrect"}, headers=limits)


def _encode_cursor(offset, kind):
    raw = json.dumps({"o": offset, "k": kind}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(text):
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("o"), int):
        return None
    return value


class StubServer:
    def __init__(self):
        self.state = State()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._httpd.state = self.state
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.02}, daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self._httpd.server_address[1]

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
