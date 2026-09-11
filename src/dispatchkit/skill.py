"""The planning contract, packaged so it can reach a repository that is not this one.

D17. D10 shipped `schema.md` and `authoring.md` and proved an agent given only
those produces a graph `validate --strict` accepts, so the content was never
the missing piece. What was missing is that an adopter has neither file: they
install a wheel, not a checkout. Both documents therefore live inside the
package rather than in `docs/`, because they are product.

Two rules shape what `skill_text` composes.

**Judgement, never grammar.** The authoring guide travels inline, because an
agent reads a skill as one document and a pointer to prose it cannot fetch is
a pointer to nothing. The schema does not, and the skill says `dispatchkit
schema` instead. Judgement ages slowly; a format copied into N repositories at
N versions is exactly the drift this project refuses everywhere else. Deferring
to the tool is also what makes an installed copy safe at all -- a skill that
describes a format it cannot check is a stale wire format with nothing to
correct it, while one that says "run `validate --strict` and iterate" cannot go
meaningfully stale, because the installed tool re-adjudicates every run.

**The planning agent may validate; only a human applies.** The line is pure
against mutating -- the one the whole codebase is built on -- rather than
whether the agent knows the tool exists. `validate` reads a file and prints;
its import closure is `errors` and `model`; it is no more dangerous than the
`ruff` every executor already runs in its acceptance command. `apply` is the
adapter, it mutates the state store, and it stays with the person who was
going to review the plan anyway.

Forbidding the agent to validate would not move the *decision* to that person
-- the decision is whether the decomposition is right, and it was always
theirs. It would move the *typos* to them, one round trip per lint, and D15
measured what a human in that loop costs.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from dispatchkit.version import __version__

#: Where `--install` writes. A skill is a directory with a `SKILL.md` in it,
#: and the name matches the package, the CLI, the issue label and the machine
#: block marker -- all of which are kept in step deliberately.
DEFAULT_SKILL_PATH = Path(".github/skills/dispatchkit-planning/SKILL.md")

#: Written into the emitted file and read back by `doctor`. An installed skill
#: is a copy, and the only thing that makes a copy tolerable is being able to
#: see when it has fallen behind.
STAMP_PREFIX = "<!-- written by dispatchkit "

_STAMP_SUFFIX = " -->"


def schema_text() -> str:
    """The format reference, as shipped. `dispatchkit schema` prints this."""
    return _read("schema.md")


def authoring_text() -> str:
    """The judgement `schema.md` cannot encode. Travels inside the skill."""
    return _read("authoring.md")


def skill_text() -> str:
    """Frontmatter and privilege rules, then the authoring guide, then a stamp.

    Composed rather than stored whole, so the guide stays single-sourced: it is
    the same file a reader of this repository sees, and the same one D10's
    lints were written against.
    """
    guide = _demote(_strip_title(authoring_text()))
    return f"{_read('skill_preamble.md')}{guide}\n{STAMP_PREFIX}{__version__}{_STAMP_SUFFIX}\n"


def installed_stamp(text: str) -> str | None:
    """The version that wrote an installed skill, or `None` if unstamped.

    Unstamped is not an error. It is somebody's own skill, or one written
    before stamps existed, and the honest response to both is to say nothing
    rather than to guess.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(STAMP_PREFIX) and stripped.endswith(_STAMP_SUFFIX):
            return stripped[len(STAMP_PREFIX) : -len(_STAMP_SUFFIX)].strip()
    return None


def _read(name: str) -> str:
    return (files("dispatchkit") / "_docs" / name).read_text(encoding="utf-8")


def _strip_title(text: str) -> str:
    """Drop the guide's own `# Authoring a plan`; the skill supplies the title."""
    lines = text.splitlines(keepends=True)
    return "".join(lines[1:]).lstrip("\n") if lines and lines[0].startswith("# ") else text


def _demote(text: str) -> str:
    """Push every heading down one level so the composed document nests.

    Only outside fenced blocks -- a `#` at the start of a line inside a shell
    example is a comment, and rewriting it would corrupt the very commands the
    guide is teaching.
    """
    out, fenced = [], False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            line = "#" + line
        out.append(line)
    return "".join(out)
