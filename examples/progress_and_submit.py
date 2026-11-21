"""Show your progress, then submit a flag.

    python examples/progress_and_submit.py            progress only
    python examples/progress_and_submit.py 'BL{...}'  progress, then submit

The token comes from BREACHLAB_TOKEN, or from the file "breachlab login" saved.
"""

import sys

from breachlab import AlreadySolvedError, BreachLab, NotLoggedIn, RateLimitError


def main():
    client = BreachLab(user_agent="breachlab-example-progress/1.0")
    try:
        me = client.me()
    except NotLoggedIn:
        sys.exit('No token found. Run "breachlab login" or set BREACHLAB_TOKEN.')
    print("%s: %d points, %d solved" % (me["username"], me["points"], me["solved"]))

    for entry in client.progress():
        upcoming = entry["next_level_idx"]
        print("  %-12s %d/%d solved, next level: %s" % (
            entry["track"], entry["solved"], entry["level_count"],
            "none" if upcoming is None else upcoming))

    if len(sys.argv) < 2:
        return
    try:
        verdict = client.submit(sys.argv[1])
    except AlreadySolvedError as exc:
        print("Already solved: %s level %s." % (exc.track, exc.level_idx))
        return
    except RateLimitError as exc:
        sys.exit("Slow down. Try again in %s seconds." % exc.retry_after)

    if verdict["result"] == "incorrect":
        sys.exit("Incorrect.")
    if verdict.get("test"):
        print("Correct. That was the test flag, so nothing was recorded.")
    else:
        print("Correct. %s level %d, %d points." % (
            verdict["track"], verdict["level_idx"], verdict["points_awarded"]))


if __name__ == "__main__":
    main()
