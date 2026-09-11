"""The one place the version is written, and the arithmetic for comparing it.

D17. The machine block in an issue body is a wire format: `block.py` parses it
with a closed grammar that refuses unknown keys, so an operator running an
older dispatchkit against issues a newer one wrote gets a parse failure on a
body a human would call fine. Diagnosing that needs three things -- a version
the tool can say, a version an adopter's config can require, and a stamp in
the installed skill -- and all three are the same mechanism, so they read the
same string from here.

`pyproject.toml` declares the version dynamic and points hatchling at this
file, because two hard-coded versions is precisely the drift the deliverable
exists to prevent.

The layer floor: this module imports nothing, not even `errors`. A version is
what everything else needs before it can decide whether it can trust anything.
"""

from __future__ import annotations

__version__ = "0.3.0"

#: Major, minor, patch. A `NewType` would be the usual move here, but this is
#: a comparison key and never crosses a boundary as a value.
_Version = tuple[int, int, int]


def parse_version(text: str) -> _Version | None:
    """`"0.3"` → `(0, 3, 0)`, and anything else → `None`.

    Nonsense is `None` rather than an exception because the interesting caller
    is an adopter's config file, which is user input. A bad pin has to be
    reportable as a lint beside every other config problem, in the single pass
    this codebase reports problems in -- not a traceback out of the middle of
    a `doctor` run.

    Deliberately not PEP 440. Accepting `1.0.0rc1` or `!=`/`~=` ranges would
    mean either a dependency (there are none) or a hand-rolled parser for a
    specification with real corner cases, to express something no adopter has
    asked for. Three integers, and a floor.
    """
    parts = text.split(".")
    if not 1 <= len(parts) <= 3:
        return None
    numbers = []
    for part in parts:
        # `str.isdigit` rather than `int()` in a `try`: it refuses the signs,
        # the underscores and the unicode `int` quietly accepts, and an empty
        # component from `"1..2"` falls out here too.
        if not part.isdigit():
            return None
        numbers.append(int(part))
    while len(numbers) < 3:
        numbers.append(0)
    return (numbers[0], numbers[1], numbers[2])


def at_least(running: str, floor: str) -> bool | None:
    """Does `running` satisfy a minimum of `floor`? `None` if either is junk.

    Numeric, not lexical: `"0.10.0" < "0.9.0"` as strings, and a silent wrong
    answer about a version is worse than no answer at all.

    `None` rather than a default, for the same reason. Guessing "satisfied"
    hides a broken pin; guessing "unsatisfied" refuses a working install over
    a typo. The caller knows whether it is holding a config file or a skill
    stamp, and can say so.
    """
    left, right = parse_version(running), parse_version(floor)
    if left is None or right is None:
        return None
    return left >= right
