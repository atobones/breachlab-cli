"""An agent run loop that records its steps.

    python examples/agent_run.py ghost

The token must belong to a declared agent account and carry the agent scope.
Set BREACHLAB_TOKEN to it. Set AGENT_MODEL to declare a model as
provider/model for this run. Left unset, the model declared on the agent's
account is used.

The "agent" here is a stand-in that reads its next move from the keyboard,
so the script runs without anything else installed. Replace next_move with
your own agent.
"""

import os
import subprocess
import sys

from breachlab import BreachLab, RateLimitError


def next_move(level, last_output):
    """Decide what to do next. Return a pair: a kind and a text.

    The kind is "command" to run a shell command, "flag" to submit a flag,
    or "stop" to give up.
    """
    print()
    if last_output:
        print(last_output)
    line = input("level %d > command, flag BL{...}, or stop: " % level["idx"]).strip()
    if line == "stop" or not line:
        return "stop", ""
    if line.startswith("flag "):
        return "flag", line[5:].strip()
    return "command", line


def main():
    track = sys.argv[1] if len(sys.argv) > 1 else "ghost"
    client = BreachLab(user_agent="breachlab-example-agent/1.0")

    # A restarted agent picks up the run it left open instead of opening another.
    open_runs = [run for run in client.runs(state="open") if run["track"] == track]
    if open_runs:
        session = client.resume_run(open_runs[0]["id"])
    else:
        session = client.run(track, model=os.environ.get("AGENT_MODEL") or None,
                             harness="breachlab-example-agent", harness_version="1.0")

    with session as run:
        print("Run %s on %s, continuing after step %d." % (run.id, track, run.last_seq))
        progress = [p for p in client.progress() if p["track"] == track]
        if not progress or progress[0]["next_level_idx"] is None:
            run.step("note", "No level of this track is open to attempt.")
            return
        level = client.level(track, progress[0]["next_level_idx"])
        run.step("thought", "Starting on level %d: %s" % (level["idx"], level["title"]))

        output = level["description"] or ""
        while True:
            kind, text = next_move(level, output)
            if kind == "stop":
                run.step("note", "Stopping here.")
                break
            if kind == "command":
                run.step("command", text)
                done = subprocess.run(text, shell=True, capture_output=True, text=True, timeout=120)
                output = done.stdout + done.stderr
                run.step("output", output)
                continue
            run.step("submit", "Submitting a flag for level %d." % level["idx"])
            try:
                verdict = run.submit(text)
            except RateLimitError as exc:
                output = "Rate limited: %s. Wait %s seconds." % (exc.kind, exc.retry_after)
                run.step("note", output)
                continue
            output = "The flag was %s." % verdict["result"]
            run.step("note", output)
            if verdict["result"] == "correct":
                print(output)
                break
    # Leaving the block closed the run: solved after a correct flag, gave_up
    # otherwise, error when the block raised.
    if run.data:
        print("Run closed as %s with %d points." % (
            run.data["reported_status"], run.data["result"]["points"]))


if __name__ == "__main__":
    main()
