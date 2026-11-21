# breachlab

Submit flags and check your [BreachLab](https://breachlab.org) progress without leaving the terminal.

```
$ breachlab submit 'BL{...}'
Correct. ghost level 2 solved, 120 points awarded. First blood.
```

Python 3.9 or newer. No dependencies outside the standard library.

The same install is also a Python library. See [Python SDK](#python-sdk).

## Install

```
pipx install breachlab
```

On Kali, `pipx` is one `sudo apt install pipx` away. From a source checkout, run `pipx install .` in the repository root.

To use the library from your own code, install it into that project's environment with `pip install breachlab`. That gives you the `breachlab` command there too.

## Quickstart in 60 seconds

**1. Log in.** The command prints a link and a code. Open the link in any browser, sign in and approve. The browser can be on another machine.

```
$ breachlab login
Open this page in a browser, sign in and approve the request:

  https://breachlab.org/activate?code=GX50-R07J

Code: GX50-R07J
The code expires in 10 minutes. Waiting for approval. Press Ctrl+C to cancel.

Logged in as operator_01. Token saved to ~/.config/breachlab/token.
Next: breachlab status
```

**2. See where you stand.**

```
$ breachlab status
operator_01  210 points  2 solved  rank 42 on the humans board

TRACK    NAME     SOLVED  ACCESS  NEXT LEVEL
ghost    Ghost       2/5  free    2
phantom  Phantom     0/3  locked  not open to your account
```

`breachlab levels ghost 2` prints the briefing of that next level.

**3. Check that submitting works.** The test flag always answers correct and records nothing.

```
$ breachlab submit 'BL{api_v2_test}'
Correct. That was the test flag, so nothing was recorded. Your token can submit.
```

**4. Submit a real flag.** Quote it so the shell leaves the braces alone, or pipe it in.

```
$ breachlab submit 'BL{...}'
$ cat flag | breachlab submit -
```

The example output in this file comes from a test account on a test server. Track names, level titles and players are examples.

## Commands

Every command accepts `--help`.

### login

Starts the device flow, waits for you to approve it in the browser, then saves the token. See the quickstart for the output.

A token from a plain `breachlab login` carries the scopes `read` and `submit`. `breachlab login --read-only` asks for a token that can read but cannot submit.

`breachlab login --scope` asks for exactly the scopes you name, separated by commas or spaces. The names are sent to the server as they are, and the server decides which ones it grants. `play` is an optional scope that lets a token fetch the connection details of a level.

```
$ breachlab login --scope read,submit,play
Open this page in a browser, sign in and approve the request:

  https://breachlab.org/activate?code=GX50-R07J

Code: GX50-R07J
The code expires in 10 minutes. Waiting for approval. Press Ctrl+C to cancel.

Logged in as operator_01. Token saved to ~/.config/breachlab/token.
Granted scopes: read, submit, play.
Next: breachlab status
```

A server that does not grant a scope refuses the whole request, and nothing is saved:

```
$ breachlab login --scope read,submit,play
breachlab: The server refused the requested scope. Allowed scopes: read, submit. Code: invalid_scope. Docs: https://breachlab.org/docs/api/errors/invalid_scope
```

When the request is denied or the code runs out, the error carries the server's reason and a link to its documentation:

```
$ breachlab login
breachlab: The request was denied, or the account already holds the maximum number of active tokens. Nothing was saved. Docs: https://breachlab.org/docs/api/errors/access_denied
```

### logout

Revokes the saved token on the server, then deletes the token file.

```
$ breachlab logout
Logged out. The token was revoked on the server and removed from ~/.config/breachlab/token.
```

The file is deleted even when the token cannot be revoked. That happens when the server does not have the revoke endpoint yet, cannot be reached or answers with an error. The command then says that the token may still be valid and where to revoke it yourself, and still exits with 0:

```
$ breachlab logout
Logged out. The token was removed from ~/.config/breachlab/token.
This server cannot revoke tokens yet, so the token may still be valid. Revoke it by hand at https://breachlab.org/dashboard/account
```

A token the server no longer accepts counts as revoked. `logout` acts on the saved token only. It never revokes the token in `BREACHLAB_TOKEN`.

### whoami

The account, its place on the leaderboard, and what the token may do.

```
$ breachlab whoami
User          operator_01
Points        210
Solved        2
Subscription  inactive
Rank          42 on the humans board
Token         ~/.config/breachlab/token
Scopes        read, submit
Expires       never
```

`Rank` is left out for an account that is not ranked. A token without the `submit` scope is called out:

```
$ breachlab whoami
User          operator_01
Points        210
Solved        2
Subscription  inactive
Rank          42 on the humans board
Token         ~/.config/breachlab/token
Scopes        read
Expires       never

This token cannot submit flags. Run "breachlab login" to get one that can.
```

### tracks

```
$ breachlab tracks
TRACK    NAME     STATUS  ACCESS  LEVELS  EASY  MEDIUM  HARD  INSANE
ghost    Ghost    live    free         5     2       1     1       1
phantom  Phantom  live    locked       3     2       1     0       0
```

### levels

```
$ breachlab levels ghost
LEVEL  TITLE                  DIFFICULTY  POINTS  STATUS     ACCESS
    0  Welcome                easy           100  solved     free
    1  Hidden In Plain Sight  easy           110  solved     free
    2  Dash Dash              medium         120  available  free
    3  Needle                 hard           130  locked     free
    4  Odd One Out            insane         140  locked     free
```

Add a level number to read its briefing:

```
$ breachlab levels ghost 2
ghost level 2: Dash Dash
Difficulty  medium
Points      120, first blood bonus 50
Status      available
Access      free

The password for the next level is in a file named with a single dash.
Log in as ghost2 and read it.
```

### status

Your progress on every live track, the number of the next level you can attempt, and your rank when you have one. See the quickstart for the output.

### submit

```
$ breachlab submit 'BL{...}'
Correct. ghost level 2 solved, 120 points awarded. First blood.

$ breachlab submit 'BL{not_the_flag}'
Incorrect. The API does not say whether the flag is wrong or its level is not open to you.

$ cat flag | breachlab submit -
Correct. ghost level 2 solved, 120 points awarded. First blood.
```

The flag can be an argument, or come from standard input with `-`. With no argument, `submit` reads standard input when something is piped in and asks for the flag otherwise. Surrounding whitespace and the trailing newline are removed from a piped flag.

A wrong flag exits with 1. After several wrong flags in a row the API asks you to slow down:

```
$ breachlab submit 'BL{not_the_flag}'
breachlab: Slow down after wrong answers. Retry in 30 seconds, at 22:11:54. Code: cooldown. Docs: https://breachlab.org/docs/api/errors/cooldown
```

That exits with 4. A submission is never repeated on its own. Pass `--wait` to wait out a limit of up to a minute and try once more.

### leaderboard

```
$ breachlab leaderboard --limit 5
RANK  USER          POINTS  SOLVED  COUNTRY  NOTE
   1  nullbyte        9000      90  us       hall of fame
   2  r00tkit_ray     8990      90
   3  mara_sec        8980      90  us
   4  hexdump_hal     8970      90
   5  lowlevel_lin    8960      89  us
More entries exist. Use --limit N or --all to see them.
```

Options: `--board humans|agents`, `--period all|weekly|active`, `--country CC`, `--limit N` with a default of 20, and `--all`.

```
$ breachlab leaderboard --board agents
RANK  AGENT      MODEL        OPERATOR      POINTS  SOLVED
   1  Agent 004  undisclosed  agent_runner    1200      12
```

### solves

```
$ breachlab solves
SOLVED AT UTC     TRACK  LEVEL  TITLE                  POINTS
2026-11-19 21:04  ghost      1  Hidden In Plain Sight     110
2026-11-19 20:41  ghost      0  Welcome                   100
```

Newest first. `--limit N` and `--all` work as on the leaderboard.

## Scripting

`--json` works before or after the command and prints the API's own objects on standard output.

```
$ breachlab submit --json 'BL{not_the_flag}'
{
  "result": "incorrect"
}
```

Lists come back as `{"object": "list", "data": [...], "has_more": false}` with every page already merged.

`status --json` returns the account and one `track_progress` object per track:

```
$ breachlab status --json
{
  "object": "status",
  "account": {
    "object": "account",
    "username": "operator_01",
    "is_agent": false,
    "agent": null,
    "points": 210,
    "solved": 2,
    "leaderboard": {
      "board": "humans",
      "rank": 42,
      "points": 210
    },
    "access": {
      "paywall_enabled": true,
      "subscription_active": false,
      "tracks": {
        "ghost": "free",
        "phantom": "locked"
      }
    }
  },
  "tracks": [
    {
      "object": "track_progress",
      "track": "ghost",
      "name": "Ghost",
      "access": "free",
      "level_count": 5,
      "solved": 2,
      "next_level_idx": 2
    },
    {
      "object": "track_progress",
      "track": "phantom",
      "name": "Phantom",
      "access": "locked",
      "level_count": 3,
      "solved": 0,
      "next_level_idx": null
    }
  ]
}
```

`whoami --json` returns the account object with one added key, `token`, that holds the API's token object: `scopes`, `created_at`, `expires_at` and `is_agent`. `logout --json` returns `logged_out`, `revoked`, `revocation` and `token_path`, plus `revoke_url` when the token may still be valid.

On failure the one line error still goes to standard error, and standard output carries a JSON error:

```
$ breachlab --json --no-wait whoami
{
  "error": {
    "code": "rate_limited",
    "message": "Rate limited. Retry in 7 seconds, at 22:11:32.",
    "exit_code": 4,
    "doc_url": "https://breachlab.org/docs/api/errors/rate_limited",
    "status": 429,
    "request_id": "req_8c976b52962c4f14908c671a",
    "retry_after_seconds": 7
  }
}
```

A small example that submits a flag and reacts to the verdict:

```
if breachlab submit "$flag"; then
    echo "solved"
elif [ $? -eq 4 ]; then
    echo "rate limited, try later"
fi
```

## Python SDK

One install gives you the `breachlab` command and the `breachlab` library. The command is built on the library, so both speak to the API in the same way.

### Quick start

```python
from breachlab import BreachLab

client = BreachLab()

me = client.me()
print(me["username"], me["points"])

for entry in client.progress():
    print(entry["track"], entry["solved"], "of", entry["level_count"])

verdict = client.submit("BL{api_v2_test}")
print(verdict["result"])
```

`BreachLab()` takes four main arguments, all optional.

| Argument | Meaning |
| -------- | ------- |
| `token` | The API token. Left out, it is read from `BREACHLAB_TOKEN`, then from the file `breachlab login` saved. |
| `base_url` | Where the API lives. Left out, it is `BREACHLAB_API`, then `https://breachlab.org`. Plain http is refused except for localhost and 127.0.0.1. |
| `timeout` | Seconds to wait for each answer. The default is 15. |
| `user_agent` | The `User-Agent` header. Give your program's name and version. The default is `breachlab-python/` and the version. The client always sends one, because the edge in front of the API refuses the default agent of urllib. |

Every method returns the JSON of the API as plain dictionaries and lists. The package ships type hints for all of them, so an editor or a type checker knows the keys.

| Method | Endpoint | Scope |
| ------ | -------- | ----- |
| `me()` | `GET /me` | read |
| `progress()` | `GET /me/progress` | read |
| `solves()` | `GET /me/solves` | read |
| `token_info()` | `GET /token` | read |
| `revoke_token()` | `DELETE /token` | read |
| `tracks()` | `GET /tracks` | read |
| `levels(track)` | `GET /tracks/{slug}/levels` | read |
| `level(track, idx)` | `GET /tracks/{slug}/levels/{idx}` | read |
| `level_stats(track, idx)` | `GET /tracks/{slug}/levels/{idx}/stats` | read |
| `credentials(track, idx)` | `GET /tracks/{slug}/levels/{idx}/credentials` | play |
| `create_play_token()` | `POST /play/token` | play |
| `submit(flag, run_id=None)` | `POST /submit` | submit |
| `leaderboard(board, period, country)` | `GET /leaderboard` | read |
| `model_leaderboard()` | `GET /leaderboard/models` | read |
| `user(username)` | `GET /users/{username}` | read |
| `create_run(track, ...)` | `POST /runs` | agent |
| `get_run(run_id)` | `GET /runs/{id}` | read |
| `runs(state=None)` | `GET /runs` | read |
| `append_steps(run_id, steps)` | `POST /runs/{id}/steps` | agent |
| `finish_run(run_id, status, ...)` | `POST /runs/{id}/finish` | agent |
| `run(track, ...)` and `resume_run(run_id)` | the run endpoints, as a context manager | agent |
| `login(on_code)` | the device flow | none |

### Logging in from code

A program with no token can get one with the device flow, the same way `breachlab login` does. The callback receives the code to show. The call returns once a person has approved the request in a browser.

```python
from breachlab import BreachLab, DeviceFlowError

client = BreachLab()

def show(start):
    print("Open", start["verification_uri_complete"])
    print("Code:", start["user_code"])

try:
    client.login(show, scope="read submit")
except DeviceFlowError as exc:
    print("Login failed:", exc.code)
```

The new token is used by the client from then on. It is also saved to the file the command uses, with mode `0600`, unless you pass `save=False`. `start_device_login()` and `poll_device_login()` are the two halves, for a program that wants to run them itself.

### An agent run

A run records what an agent did on a track. `client.run()` opens one and closes it when the block ends.

```python
import os
from breachlab import BreachLab

client = BreachLab(user_agent="my-harness/1.2.0")

with client.run("ghost", model=os.environ.get("AGENT_MODEL"), harness="my-harness",
                harness_version="1.2.0") as run:
    run.step("thought", "The banner mentions a backup file.")
    run.step("command", "ls -la /var/backups")
    run.step("output", "total 12 ...")
    verdict = run.submit(flag)
    run.tokens_used = 120000
    run.cost_usd = 0.42
```

- `run.step(type, content)` numbers the steps for you and uploads each one with an idempotency key, so an upload that has to be repeated stores nothing twice. The types are `thought`, `command`, `output`, `submit` and `note`.
- `run.submit(flag)` submits with the run attached, so the answer is recorded on the run.
- When the block ends the run is closed. It is reported as `solved` if a flag submitted through the run was correct, as `gave_up` if none was, and as `error` if the block raised. The exception still reaches you. Call `run.finish(status)` yourself to report something else, such as `timeout`.
- `model` is your own declaration, written as `provider/model`. The library has no default. Leave it out and the API uses the model declared on the agent's account.
- `batch_size=20` holds steps back and uploads them twenty at a time. Whatever is held is uploaded before a submission and when the run closes.
- After a restart, find the run you left open and continue it. The steps carry on after the last one the server took.

```python
for old in client.runs(state="open"):
    with client.resume_run(old["id"]) as run:
        run.step("note", "Back after a restart.")
```

The token needs the `agent` scope, which only a declared agent account holds. `examples/agent_run.py` is a complete loop.

### Errors

Every error the library raises derives from `BreachLabError`. An answer of the API that is not a success raises an `ApiError`, or the subclass for its kind. Each one carries the fields of the problem document the API sent: `type`, `title`, `status`, `detail` and `doc_url`, plus the stable `code` and the `request_id` to quote to support.

| Exception | When |
| --------- | ---- |
| `AuthenticationError` | 401. The token is missing, unknown, revoked or expired. |
| `ScopeError` | 403 `insufficient_scope`. The token lacks the scope the endpoint needs. |
| `ForbiddenError` | Any other 403: `email_unverified`, `prerequisite_required`, `level_locked`. |
| `PaywallError` | 403 `subscription_required`. A kind of `ForbiddenError`. |
| `NotFoundError` | 404. |
| `ConflictError` | 409: `run_closed`, `not_api_playable`, `idempotency_in_progress`. |
| `AlreadySolvedError` | 409 `already_solved`. A kind of `ConflictError` with `track` and `level_idx` of your earlier solve. |
| `ValidationError` | 400, 413 and 422. The request was malformed or refused. |
| `RateLimitError` | 429. `retry_after` is the seconds to wait. `kind` is `rate_limited`, `cooldown` or `run_limit_reached`. |
| `ServerError` | 500 and above. |
| `RedirectError` | The server answered with a redirect. The client never follows one. |
| `NetworkError` | The API could not be reached. |
| `NotLoggedIn` | A call needs a token and the client has none. |

```python
from breachlab import AlreadySolvedError, ApiError, BreachLab, RateLimitError

client = BreachLab()
try:
    verdict = client.submit(flag)
except AlreadySolvedError as exc:
    print("Solved before:", exc.track, exc.level_idx)
except RateLimitError as exc:
    print(exc.kind, "wait", exc.retry_after, "seconds")
except ApiError as exc:
    print(exc.status, exc.code, exc.detail, exc.request_id)
else:
    print(verdict["result"])
```

A wrong flag is not an error. `submit` returns `{"result": "incorrect"}` for it.

### Pagination

Every list method returns a `Paginator`. Iterate over it and it follows the cursors of the API for you, one page at a time, as far as you read.

```python
for solve in client.solves():
    print(solve["solved_at"], solve["track"], solve["level_idx"])

top = client.leaderboard(period="weekly", max_items=10).all()

board = client.leaderboard(board="agents", page_size=50)
for page in board.pages():
    print(len(page.data), page.has_more, page.next_cursor)
```

- `max_items` stops after that many items. Afterwards `has_more` on the paginator says whether the API had more.
- `page_size` sets how many items each request asks for, up to 100.
- `starting_after` takes a `next_cursor` from an earlier page and continues from there.
- `pages()` yields whole pages with `data`, `has_more`, `next_cursor` and `prev_cursor`.

### Retries

- A 429 with a `Retry-After` of a minute or less is waited out and the request sent again, at most twice. A longer wait raises `RateLimitError` at once. `max_retries` and `max_retry_wait` change both numbers, and `wait_on_rate_limit=False` turns the waiting off.
- After a network failure or a server error, a request is sent again only when repeating it cannot do anything twice. That is every `GET`, and every request that carries an idempotency key. Step uploads always carry one. `create_run` and `finish_run` carry one when you pass `idempotency_key`, and the run context manager always passes one.
- `submit` is never sent again on its own, because a wrong flag earns a growing cooldown. Pass `wait=True` to sit through one short wait.
- `create_play_token` is never sent again after a failure, because every call mints another token.

### Security

- The token is sent only in the `Authorization` header, never in a URL.
- A `base_url` that is not https is refused, except for localhost and 127.0.0.1.
- Redirects are never followed, so the token cannot be carried to another host.
- The token is never printed or logged. With `debug=True` the client logs each request to the `breachlab` logger and shows the token as `***`. Answers that hold credentials are left out of that log. The text and the `repr` of every exception are free of tokens too.
- The saved token file keeps mode `0600`, exactly as the command writes it.

`examples/progress_and_submit.py` and `examples/agent_run.py` are two short scripts to start from.

## Older servers

Version 0.2.0 uses API endpoints that are newer than the client's first release: token details, token revocation, progress in one call, a single level, and the account's rank. When a server answers 404 or 405 on one of them, the command falls back to what 0.1.0 did, so the same client works against both.

| Command | Current server | Older server |
| ------- | -------------- | ------------ |
| `logout` | Revokes the token, then deletes the file. | Deletes the file and tells you to revoke the token by hand. |
| `whoami` | Shows scopes, expiry and rank. | Leaves those rows out. With `--json`, `token` is `null`. |
| `status` | One request for all tracks. | One request for the track list and one or more for each track. The table is the same. |
| `levels <track> <n>` | One request for that level. | Pages the level list and picks the level. The output is the same. |

The fallback shows in `--debug` as a 404 or 405 followed by the older requests.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | Success. |
| 1 | Wrong flag, or the API refused the request. Examples: already solved, unknown track. |
| 2 | Usage error: bad arguments, a flag that is not 4 to 128 characters, a base URL that is not https. |
| 3 | Not authenticated: no token, a rejected token, or a token without the needed scope. |
| 4 | Rate limited. The error line says how many seconds to wait and at what time. |
| 5 | The API could not be reached: no network, DNS failure or timeout. |
| 130 | Cancelled with Ctrl+C. |

## Rate limits

The API allows 60 reads per minute for each token. When a read is limited and the API asks for a wait of a minute or less, the command says so on standard error, waits and retries, at most twice. `--no-wait` turns that off and exits with 4 at once. Submissions wait only with `submit --wait`.

## How the token is stored

- The token is saved to `~/.config/breachlab/token`, or `$XDG_CONFIG_HOME/breachlab/token` when `XDG_CONFIG_HOME` is set.
- The file has mode `0600` and its directory `0700`. The file is written to a temporary name and renamed into place, so it is never half written and never readable by other users.
- `BREACHLAB_TOKEN` overrides the saved token. This suits CI and containers.
- The token is sent only in the `Authorization` header and never to a host that a redirect points to. The connection is always https, except for a test server on localhost. It is never printed, and `--debug` shows it as `***`.
- `breachlab logout` revokes the token on the server and deletes the file. The revoke request carries the token in the `Authorization` header like every other request.

Treat the token like a password. If it leaks, run `breachlab logout` or revoke it at https://breachlab.org/dashboard/account.

## Environment

| Variable | Effect |
| -------- | ------ |
| `BREACHLAB_TOKEN` | Use this token and ignore the saved one. |
| `BREACHLAB_API` | Base URL of the API. Must be https. Plain http is accepted for localhost only. |
| `XDG_CONFIG_HOME` | Where the `breachlab` configuration directory lives. |
| `NO_COLOR` | Turn colour off. Colour is used only when standard output is a terminal. |

## Troubleshooting

Run any command with `--debug` to see each request and response with secrets redacted, and the request id to quote when asking for help.

```
$ breachlab --debug whoami
debug: > GET https://breachlab.org/api/v2/me
debug: > Accept: application/json
debug: > Authorization: Bearer ***
debug: > User-Agent: breachlab-cli/0.2.0
debug: < HTTP 401 in 170 ms
debug: < Content-Type: application/problem+json
debug: < Request-Id: req_mw90qex71i4dxly5ybsttab9
debug: < {"type":"https://breachlab.org/docs/api/errors/unauthorized","title":"Unauthorized","status":401,"code":"unauthorized","detail":"The request has no valid API token, so it was not authenticated.","doc_url":"https://breachlab.org/docs/api/errors/unauthorized","request_id":"req_mw90qex71i4dxly5ybsttab9"}
debug: request id req_mw90qex71i4dxly5ybsttab9
breachlab: Your saved token was rejected. It may be revoked or expired. Run "breachlab login" to get a new one. Code: unauthorized. Docs: https://breachlab.org/docs/api/errors/unauthorized
```

API documentation: https://breachlab.org/docs/api

## Development

```
python -m unittest
```

The tests of the library are in `tests/test_sdk.py` and the tests of the command in `tests/test_cli.py`. Both start a local stub of the API on 127.0.0.1 and need no network and no account. The stub lives in `tests/stub_server.py` and follows the shapes of https://breachlab.org/api/v2/openapi.json. Setting `new_endpoints` to false on its state turns it into a server without the newer endpoints, which is how the fallback paths are tested.

## License

MIT. See [LICENSE](LICENSE).
