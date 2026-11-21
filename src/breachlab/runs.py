"""A run of an agent, as a context manager."""

import json
from datetime import datetime, timezone
from typing import Any, List, Optional

from breachlab.errors import ApiError, BreachLabError
from breachlab.models import RunData, Step, StepBatchResult, SubmitResult

STEP_TYPES = ("thought", "command", "output", "submit", "note")
MAX_BATCH_STEPS = 100
# The API takes 262,144 bytes per upload. Staying well under leaves room for
# the JSON around the steps.
MAX_BATCH_BYTES = 200000


def timestamp() -> str:
    """The current UTC time in the form the API takes for a step."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Run:
    """One run of the calling agent on a track.

    Made by `BreachLab.run()` or `BreachLab.resume_run()`. Entering the
    `with` block opens the run, leaving it closes the run:

    * after `finish()` was called, nothing more happens;
    * when the block raised, the run is reported as `error`;
    * otherwise it is reported as `solved` when a flag submitted through this
      run was correct, and as `gave_up` when none was.

    Set `tokens_used` and `cost_usd` at any time to have them reported when
    the run closes.

    `step()` numbers the steps and uploads them with an idempotency key, so
    an upload that is retried stores nothing twice. With a `batch_size` above
    1 the steps are held back and uploaded together; `flush()`, `submit()`,
    `finish()` and the end of the block upload what is held.
    """

    def __init__(self, client: Any, track: Optional[str], *, model: Optional[str] = None,
                 harness: Optional[str] = None, harness_version: Optional[str] = None,
                 run_id: Optional[str] = None, batch_size: int = 1):
        if not 1 <= batch_size <= MAX_BATCH_STEPS:
            raise ValueError("batch_size must be between 1 and %d" % MAX_BATCH_STEPS)
        self._client = client
        self._track = track
        self._model = model
        self._harness = harness
        self._harness_version = harness_version
        self._batch_size = batch_size
        self._pending: List[Step] = []
        self._solved = False
        #: The run id. None until the run is open.
        self.id: Optional[str] = run_id
        #: The run as the API last described it.
        self.data: Optional[RunData] = None
        #: The number of the last step handed to `step()`.
        self.last_seq = 0
        #: True once the run is closed.
        self.finished = False
        #: Reported when the run closes.
        self.tokens_used: Optional[int] = None
        self.cost_usd: Optional[float] = None

    def __repr__(self) -> str:
        return "Run(id=%r, last_seq=%r, finished=%r)" % (self.id, self.last_seq, self.finished)

    # -- opening and closing -------------------------------------------------------

    def open(self) -> "Run":
        """Create the run, or read the one being resumed. `with` does this."""
        if self.data is not None:
            return self
        if self.id is None:
            from breachlab.client import new_idempotency_key
            self.data = self._client.create_run(
                self._track, model=self._model, harness=self._harness,
                harness_version=self._harness_version, idempotency_key=new_idempotency_key())
        else:
            self.data = self._client.get_run(self.id)
        self.id = self.data["id"]
        last = self.data.get("last_seq")
        self.last_seq = last if isinstance(last, int) and not isinstance(last, bool) else 0
        self.finished = self.data.get("state", "open") != "open"
        return self

    def __enter__(self) -> "Run":
        return self.open()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if self.finished or self.id is None:
            return False
        if exc_type is not None:
            # The block failed. Close the run as an error and let the block's
            # own exception through, whatever closing runs into.
            try:
                self.finish("error")
            except BreachLabError:
                pass
            return False
        self.finish("solved" if self._solved else "gave_up")
        return False

    def finish(self, status: str, tokens_used: Optional[int] = None,
               cost_usd: Optional[float] = None) -> Optional[RunData]:
        """Upload what is held back and close the run with this status.

        `status` is `solved`, `gave_up`, `error` or `timeout`. A run the
        server has already closed counts as finished and returns None.
        """
        from breachlab.client import new_idempotency_key
        self._require_open()
        try:
            self.flush()
        except BreachLabError:
            # The steps could not be stored. Still close the run.
            self._pending = []
        if tokens_used is None:
            tokens_used = self.tokens_used
        if cost_usd is None:
            cost_usd = self.cost_usd
        try:
            self.data = self._client.finish_run(
                self.id, status, tokens_used=tokens_used, cost_usd=cost_usd,
                idempotency_key=new_idempotency_key())
        except ApiError as exc:
            if exc.code != "run_closed":
                raise
            self.finished = True
            return None
        self.finished = True
        return self.data

    # -- steps ---------------------------------------------------------------------

    def step(self, type: str, content: str, ts: Optional[str] = None) -> Optional[StepBatchResult]:
        """Record one step: a `thought`, `command`, `output`, `submit` or `note`.

        Returns what the server stored when this call uploaded, and None
        when the step was only held back for a later upload.
        """
        self._require_open()
        if type not in STEP_TYPES:
            raise ValueError("A step type is one of: %s." % ", ".join(STEP_TYPES))
        if not isinstance(content, str):
            raise TypeError("The content of a step is a string.")
        self.last_seq += 1
        self._pending.append({"seq": self.last_seq, "ts": ts or timestamp(),
                              "type": type, "content": content})  # type: ignore[typeddict-item]
        if len(self._pending) >= self._batch_size:
            return self.flush()
        return None

    def flush(self) -> Optional[StepBatchResult]:
        """Upload the steps held back. Return the answer to the last upload."""
        result = None
        while self._pending:
            batch = self._next_batch()
            result = self._client.append_steps(self.id, batch)
            # Dropped only once stored, so a failed upload can be flushed again
            # and then carries the same idempotency key.
            del self._pending[:len(batch)]
        return result

    def _next_batch(self) -> List[Step]:
        batch: List[Step] = []
        size = 0
        for step in self._pending[:MAX_BATCH_STEPS]:
            size += len(json.dumps(step))
            if batch and size > MAX_BATCH_BYTES:
                break
            batch.append(step)
        return batch

    # -- submitting ----------------------------------------------------------------

    def submit(self, flag: str) -> SubmitResult:
        """Submit a flag and have the answer recorded on this run.

        Steps held back are uploaded first, because the API notes what a run
        was attempting only when the run already has steps. Like every
        submission, this is never retried on its own.
        """
        self._require_open()
        self.flush()
        result = self._client.submit(flag, run_id=self.id)
        if result.get("result") == "correct" and not result.get("test"):
            self._solved = True
        return result

    def _require_open(self) -> None:
        if self.id is None or self.data is None:
            raise RuntimeError("The run is not open yet. Use it in a with block or call open().")
        if self.finished:
            raise RuntimeError("The run is already closed.")
