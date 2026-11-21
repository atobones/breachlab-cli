"""Following the cursors of the list endpoints."""

from dataclasses import dataclass
from typing import Any, Dict, Generic, Iterator, List, Optional, TypeVar

from breachlab.errors import UnexpectedResponse

T = TypeVar("T")

PAGE_SIZE = 100
MAX_PAGES = 500


@dataclass(frozen=True)
class Page(Generic[T]):
    """One page of a list endpoint."""

    data: List[T]
    has_more: bool
    next_cursor: Optional[str]
    prev_cursor: Optional[str]


class Paginator(Generic[T]):
    """The items of a list endpoint, fetched page by page as they are needed.

    Iterate over it to get the items, or call `all()` for a list. Nothing is
    requested before the first item is asked for, and each new iteration
    starts again from the first page. `pages()` gives whole pages with their
    cursors.

    `has_more` is None until an iteration has run to its end. After that it
    says whether the API still had items past the last one returned, which
    happens only when `max_items` stopped the iteration.
    """

    def __init__(self, client: Any, path: str, query: Optional[Dict[str, Any]] = None,
                 max_items: Optional[int] = None, page_size: int = PAGE_SIZE,
                 starting_after: Optional[str] = None):
        if max_items is not None and max_items < 1:
            raise ValueError("max_items must be at least 1")
        if not 1 <= page_size <= PAGE_SIZE:
            raise ValueError("page_size must be between 1 and %d" % PAGE_SIZE)
        self._client = client
        self._path = path
        self._query = dict(query or {})
        self._max_items = max_items
        self._page_size = page_size
        self._starting_after = starting_after
        self.has_more: Optional[bool] = None

    def __repr__(self) -> str:
        return "Paginator(%r)" % self._path

    def __iter__(self) -> Iterator[T]:
        for page in self.pages():
            for item in page.data:
                yield item

    def all(self) -> List[T]:
        """Fetch every page, or up to `max_items` items, and return the items."""
        return list(self)

    def pages(self) -> Iterator["Page[T]"]:
        """Yield one Page per request, following `next_cursor`."""
        self.has_more = None
        limit = self._max_items
        count = 0
        cursor = self._starting_after
        seen = set()
        for _ in range(MAX_PAGES):
            params = dict(self._query)
            want = self._page_size if limit is None else min(self._page_size, limit - count)
            params["limit"] = max(1, want)
            if cursor:
                params["starting_after"] = cursor
            raw = self._client.call("GET", self._path, query=params)
            data = raw.get("data") if isinstance(raw, dict) else None
            if not isinstance(data, list):
                raise UnexpectedResponse(200, "bad_response",
                                         "The server sent a list in an unexpected shape.")
            cursor = raw.get("next_cursor")
            prev = raw.get("prev_cursor")
            more = bool(raw.get("has_more")) and isinstance(cursor, str) and cursor != ""
            overflow = limit is not None and count + len(data) > limit
            if overflow:
                data = data[:limit - count]
            count += len(data)
            done = limit is not None and count >= limit
            # A repeated cursor would loop forever; treat it as the end.
            last = done or not more or cursor in seen or not data
            if done:
                self.has_more = more or overflow
            elif last:
                self.has_more = False
            yield Page(data, more or overflow, cursor if isinstance(cursor, str) else None,
                       prev if isinstance(prev, str) else None)
            if last:
                return
            seen.add(cursor)
        self.has_more = True
