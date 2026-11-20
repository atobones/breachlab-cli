# breachlab

Submit flags and check your [BreachLab](https://breachlab.org) progress without leaving the terminal.

```
$ breachlab submit 'BL{...}'
Correct. ghost level 2 solved, 120 points awarded. First blood.
```

Python 3.9 or newer. No dependencies outside the standard library.

## Install

```
pipx install breachlab
```

On Kali, `pipx` is one `sudo apt install pipx` away. From a source checkout, run `pipx install .` in the repository root.

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
operator_01  210 points  2 solved

TRACK    SOLVED  POINTS  ACCESS  NEXT LEVEL
ghost       2/5     210  free    2 Dash Dash
phantom     0/3       0  locked  not open to your account
```

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

`breachlab login --read-only` asks for a token that can read but cannot submit.

### logout

```
$ breachlab logout
Logged out. The token was removed from ~/.config/breachlab/token.
It stays valid on the server until you revoke it at https://breachlab.org/dashboard/account
```

### whoami

```
$ breachlab whoami
User          operator_01
Points        210
Solved        2
Subscription  inactive
Token         ~/.config/breachlab/token
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

Your progress on every live track and the next level you can attempt. See the quickstart for the output.

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

Lists come back as `{"object": "list", "data": [...], "has_more": false}` with every page already merged. `status --json` returns the account and one entry per track with `solved`, `levels`, `completed` and `next_level`.

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
- `breachlab logout` deletes the file. To revoke the token on the server, open https://breachlab.org/dashboard/account.

Treat the token like a password. If it leaks, revoke it.

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
debug: > User-Agent: breachlab-cli/0.1.0
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

The tests start a local stub of the API on 127.0.0.1 and need no network and no account. The stub lives in `tests/stub_server.py` and follows the shapes of https://breachlab.org/api/v2/openapi.json.
