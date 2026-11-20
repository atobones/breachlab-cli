"""Plain aligned text, with colour only on a terminal."""

import os
import unicodedata

_CODES = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "cyan": "36",
}


def use_color(stream):
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


class Style:
    def __init__(self, enabled):
        self.enabled = enabled

    def paint(self, text, name):
        if not self.enabled or not name or name not in _CODES:
            return text
        return "\x1b[%sm%s\x1b[0m" % (_CODES[name], text)


def clean(value, multiline=False):
    """Make a string from the API safe to print.

    Titles, usernames and briefings are written by other people. Control
    characters are replaced so that nothing from the network can move the
    cursor, recolour the terminal or hide text.
    """
    if value is None:
        return ""
    text = str(value)
    out = []
    for ch in text:
        if multiline and ch in "\n\t":
            out.append(ch)
        elif unicodedata.category(ch).startswith("C"):
            out.append("?")
        else:
            out.append(ch)
    return "".join(out)


def table(headers, rows, style, right=()):
    """Render rows as aligned columns.

    A cell is a string or a (string, style name) pair. Columns whose index
    is in `right` are right aligned.
    """
    plain = []
    for row in rows:
        plain.append([cell[0] if isinstance(cell, tuple) else cell for cell in row])
    widths = [len(h) for h in headers]
    for row in plain:
        for i, text in enumerate(row):
            widths[i] = max(widths[i], len(text))

    def fmt(text, i):
        return text.rjust(widths[i]) if i in right else text.ljust(widths[i])

    lines = ["  ".join(style.paint(fmt(h, i), "bold") for i, h in enumerate(headers)).rstrip()]
    for row in rows:
        parts = []
        for i, cell in enumerate(row):
            text, name = cell if isinstance(cell, tuple) else (cell, None)
            parts.append(style.paint(fmt(text, i), name))
        lines.append("  ".join(parts).rstrip())
    return "\n".join(lines)


def pairs(items, style):
    """Render (label, value) pairs as two aligned columns."""
    width = max(len(label) for label, _ in items)
    return "\n".join(
        "%s  %s" % (style.paint(label.ljust(width), "dim"), value) for label, value in items
    )
