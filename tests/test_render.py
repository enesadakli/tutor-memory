from __future__ import annotations

import json
from pathlib import Path

from tutormem.models import (
    EvidenceRef,
    Hypothesis,
    Instruction,
    ProfileState,
    SessionRecord,
    StuckPoint,
)
from tutormem.render import render_brief, render_profile

ROOT = Path(__file__).parents[1]


def _state(
    *,
    instructions: tuple[Instruction, ...] = (),
    hypotheses: tuple[Hypothesis, ...] = (),
    stuck_points: tuple[StuckPoint, ...] = (),
    sessions: tuple[SessionRecord, ...] = (),
) -> ProfileState:
    return ProfileState(1, instructions, hypotheses, stuck_points, sessions)


def _instruction(identifier: str, claim: str, status: str = "active") -> Instruction:
    return Instruction(identifier, claim, status, (), 1, 2 if status == "revoked" else None)  # type: ignore[arg-type]


def _hypothesis(
    identifier: str,
    claim: str,
    status: str,
    promoted_at: int | None,
) -> Hypothesis:
    return Hypothesis(
        identifier,
        claim,
        status,  # type: ignore[arg-type]
        ("s1",),
        (),
        1,
        1,
        promoted_at,
        2 if status == "dropped" else None,
        2 if status == "revoked" else None,
    )


def test_render_brief_empty_without_courses_has_exact_newline() -> None:
    assert render_brief(_state(), None) == (
        "# Study brief\n\n"
        "Built by tutor-memory from 0 reviewed sessions. Follow it in every session.\n\n"
        "## How to teach\n\n"
        "- None yet.\n\n"
        "## Observed patterns\n\n"
        "- None yet.\n"
    )


def test_render_brief_filters_statuses_orders_promotions_and_strips_courses() -> None:
    instructions = (
        _instruction("ins-1", "Active first"),
        _instruction("ins-2", "Revoked", "revoked"),
        _instruction("ins-3", "Active second"),
    )
    hypotheses = (
        _hypothesis("hyp-1", "Tie first", "promoted", 3),
        _hypothesis("hyp-2", "Open", "open", None),
        _hypothesis("hyp-3", "Earlier", "promoted", 2),
        _hypothesis("hyp-4", "Tie second", "promoted", 3),
        _hypothesis("hyp-5", "Dropped", "dropped", None),
        _hypothesis("hyp-6", "Revoked", "revoked", 1),
    )
    sessions = (
        SessionRecord("s1", 1, "C", None, "a" * 64, "applied", 1),
        SessionRecord("s2", 2, "C", None, "b" * 64, "pending", None),
    )
    brief = render_brief(
        _state(instructions=instructions, hypotheses=hypotheses, sessions=sessions),
        "\n Course progress \n\n",
    )
    assert "from 1 reviewed session" in brief
    assert brief.index("- Earlier") < brief.index("- Tie first") < brief.index("- Tie second")
    assert "Revoked" not in brief
    assert "Open" not in brief
    assert "Dropped" not in brief
    assert brief.endswith("## Courses and progress\n\nCourse progress\n")
    assert not brief.endswith("\n\n")


def test_render_brief_blank_courses_omits_section() -> None:
    assert "Courses and progress" not in render_brief(_state(), " \n\t")


def test_render_brief_preserves_base_and_only_appends_learned_sections() -> None:
    state = _state(
        instructions=(_instruction("ins-1", "Go slide by slide."),),
        hypotheses=(_hypothesis("hyp-1", "Asks for diagrams.", "promoted", 2),),
    )
    rendered = render_brief(
        state, "ignored courses", base_md="\n# My tutor brief\n\nKeep it calm.\n"
    )

    assert rendered == (
        "# My tutor brief\n\n"
        "Keep it calm.\n\n"
        "## Learned from sessions\n\n"
        "### How to teach\n\n"
        "- Go slide by slide.\n\n"
        "### Observed patterns\n\n"
        "- Asks for diagrams.\n"
    )
    assert "# Study brief" not in rendered
    assert "Courses and progress" not in rendered


def test_render_brief_matches_example_byte_for_byte() -> None:
    expected_dir = ROOT / "examples" / "workspace" / "expected"
    state = ProfileState.from_dict(
        json.loads((expected_dir / "profile.json").read_text(encoding="utf-8"))
    )
    courses = (ROOT / "examples" / "workspace" / "courses.md").read_text(encoding="utf-8")
    assert render_brief(state, courses).encode() == (expected_dir / "brief.md").read_bytes()


def test_render_profile_has_required_tables_threshold_pending_and_escaped_pipes() -> None:
    evidence = (EvidenceRef("s1", "s1:1", "a | quoted value"),)
    instruction = Instruction("ins-s1:1", "teach | slowly", "active", evidence, 1, None)
    hypothesis = Hypothesis(
        "hyp-s1:2", "visual | support", "open", ("s1",), evidence, 1, 1, None, None, None
    )
    stuck = StuckPoint("s1", "Course | A", None, "hard | topic", "quote | here")
    pending = SessionRecord("s2", 2, "Course", None, "b" * 64, "pending", None)
    rendered = render_profile(
        _state(
            instructions=(instruction,),
            hypotheses=(hypothesis,),
            stuck_points=(stuck,),
            sessions=(pending,),
        ),
        threshold=5,
    )
    for heading in (
        "# Learner profile",
        "## Instructions",
        "## Hypotheses",
        "## Stuck points",
        "## Sessions",
    ):
        assert heading in rendered
    assert "1/5" in rendered
    assert "| ins-s1:1 | active | teach \\| slowly | 1 |" in rendered
    assert "pending" in rendered
    assert r"teach \| slowly" in rendered
    assert r"a \| quoted value" in rendered
    assert r"Course \| A" in rendered
    assert rendered.endswith("\n")
