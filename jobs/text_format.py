#!/usr/bin/env python3
"""Width-aware text helpers for the plain text summaries this server returns.

Wide/fullwidth characters (e.g. "娜") take two terminal columns, so plain
``len()`` misaligns any column layout that contains them — and every name in
this server may be non-ASCII. The presence summary and the location history both
lay their columns out with these.

Usage:
    from jobs.text_format import display_width, pad

    width = max(display_width(n) for n in names)
    print(pad(name, width) + "  " + value)
"""

import unicodedata


def display_width(text) -> int:
    """Width of text in terminal columns.

    Wide/fullwidth characters (e.g. "娜") occupy two columns; everything else
    counts as one.
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in str(text))


def pad(text, width: int) -> str:
    """Left-align text in a field of the given display width."""
    text = str(text)
    return text + " " * max(0, width - display_width(text))


def rule(lines, character: str = "-") -> str:
    """A horizontal rule as wide as the widest of ``lines``."""
    return character * max([display_width(line) for line in lines] or [0])


if __name__ == "__main__":
    # Simple smoke test / demo.
    print("display_width('abc') ->", display_width("abc"))
    print("display_width('娜')   ->", display_width("娜"))
    for name in ("娜", "Alex", "Sam"):
        print(f"|{pad(name, 6)}| ends at column 6")
    print(rule(["娜 is home", "Alex is away"]))
