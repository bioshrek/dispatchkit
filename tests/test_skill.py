"""D17: the planning contract, delivered to a repository that is not this one.

D10 wrote `schema.md` and `authoring.md` and proved an agent given only those
produces a graph `validate --strict` accepts. The content was never the gap.
The gap is that an adopter has neither file, and cannot read documentation
that lives in this repository -- so the contract has to travel with the tool.

Two rules the tests below exist to hold:

* The skill carries **judgement, never grammar**. Judgement ages slowly and is
  worth copying into somebody's tree; a schema copied into N repositories at N
  versions is the drift this project refuses everywhere else. The format is
  emitted by `dispatchkit schema`, which is always the installed version.
* The planning agent may run `validate`; only a human runs `apply`. The line
  is pure against mutating -- the one this codebase is built on -- and not
  whether the agent knows the tool exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatchkit.skill import (
    DEFAULT_SKILL_PATH,
    STAMP_PREFIX,
    authoring_text,
    installed_stamp,
    schema_text,
    skill_text,
)
from dispatchkit.version import __version__

pytestmark = pytest.mark.unit


class TestTheDocsTravelWithTheTool:
    """They live in the package, not in `docs/`, because they are product.

    An adopter installs a wheel and has no checkout of this repository, so a
    reference that lives in `docs/` is a reference they cannot reach.
    """

    def test_the_schema_is_readable_from_the_package(self) -> None:
        assert "[[task]]" in schema_text()

    def test_the_authoring_guide_is_readable_from_the_package(self) -> None:
        assert "validate --strict" in authoring_text()

    def test_neither_is_left_behind_in_docs(self) -> None:
        """A copy in `docs/` would be the second source of truth."""
        docs = Path(__file__).resolve().parents[1] / "docs"

        assert not (docs / "schema.md").exists()
        assert not (docs / "authoring.md").exists()


class TestWhatTheSkillSays:
    def test_it_opens_with_frontmatter_a_skill_runtime_can_read(self) -> None:
        text = skill_text()

        assert text.startswith("---\n")
        assert "name: dispatchkit-planning" in text
        assert "description:" in text

    def test_it_carries_the_judgement(self) -> None:
        """The authoring guide, inline. An agent reads a skill as one document,
        so a pointer to prose it cannot fetch is a pointer to nothing."""
        text = skill_text()

        assert "merge-candidate" in text
        assert "Step-by-step instructions are the tempting mistake" in text

    def test_it_does_not_carry_the_schema(self) -> None:
        """The rule that makes an installed copy safe.

        Grammar ages fast. A skill that describes the format is a stale wire
        format with nothing to correct it; a skill that defers to the tool
        cannot go meaningfully stale, because the tool re-adjudicates on every
        run. `dispatchkit schema` is the authority, and it is always whichever
        version is installed.
        """
        text = skill_text()

        assert "dispatchkit schema" in text
        assert schema_text() not in text
        # A handful of keys are unavoidable in worked examples; the reference
        # tables are what must not be here.
        assert "| Key | Type |" not in text

    def test_it_permits_validate(self) -> None:
        """Pure: no `--repo`, no socket, closure of `errors` and `model`. It is
        no more dangerous than the `ruff` every executor already runs."""
        assert "validate --strict" in skill_text()

    def test_it_forbids_apply(self) -> None:
        """`apply` mutates the state store. It stays with the person who is
        reviewing the plan anyway."""
        text = skill_text()

        assert "never run `dispatchkit apply`" in text

    def test_it_says_who_runs_apply_instead(self) -> None:
        """A prohibition with no alternative reads as an obstacle to route
        around. The alternative is the human who was always going to review."""
        assert "hand the file to a human" in skill_text()


class TestTheStamp:
    def test_the_text_carries_the_running_version(self) -> None:
        assert f"{STAMP_PREFIX}{__version__}" in skill_text()

    def test_it_reads_back(self) -> None:
        assert installed_stamp(skill_text()) == __version__

    def test_an_unstamped_file_reads_as_none(self) -> None:
        """Somebody's own skill, or one written before stamps existed. Not an
        error -- just not a thing we can say anything about."""
        assert installed_stamp("# notes\n\nnothing to do with us\n") is None

    def test_a_stamp_is_found_wherever_it_sits(self) -> None:
        assert installed_stamp(f"intro\n{STAMP_PREFIX}1.2.3 -->\nmore\n") == "1.2.3"


class TestWhereItGoes:
    def test_the_default_path_is_conventional(self) -> None:
        assert DEFAULT_SKILL_PATH == Path(".github/skills/dispatchkit-planning/SKILL.md")


class TestNothingInItPointsAtThisRepository:
    """A link that resolves here and nowhere else is worse than no link.

    The skill is read inside somebody else's tree, by an agent that will
    either follow the link and find nothing or, worse, quietly invent what it
    would have said. Every reference has to be a command it can run.
    """

    def test_it_contains_no_relative_markdown_links(self) -> None:
        import re

        links = re.findall(r"\[[^\]]*\]\((?!https?://|#)([^)]+)\)", skill_text())

        assert links == []

    def test_the_schema_is_reached_by_command(self) -> None:
        """Which is also the version-proof way to reach it."""
        assert "dispatchkit schema" in skill_text()
