from __future__ import annotations

from .models import EvidenceRef, ProfileState
from .security import sanitize_brief_text


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- None yet."


def render_brief(
    state: ProfileState,
    courses_md: str | None,
    *,
    base_md: str | None = None,
    progress_md: str | None = None,
) -> str:
    """Render the short tutor-facing study brief."""
    applied = sum(record.status == "applied" for record in state.sessions)
    instructions = [
        sanitize_brief_text(item.claim, 200)
        for item in state.instructions
        if item.status == "active"
    ]
    promoted = [
        item
        for _, item in sorted(
            enumerate(state.hypotheses),
            key=lambda pair: (
                pair[1].promoted_at if pair[1].promoted_at is not None else float("inf"),
                pair[0],
            ),
        )
        if item.status == "promoted"
    ]
    if base_md is not None and base_md.strip():
        learned = [
            "## Learned from sessions",
            "### How to teach\n\n" + _bullets(instructions),
            "### Observed patterns\n\n"
            + _bullets([sanitize_brief_text(item.claim, 200) for item in promoted]),
        ]
        sections = [base_md.strip()]
        if progress_md is not None and progress_md.strip():
            sections.append(progress_md.strip())
        sections.append("\n\n".join(learned))
        return "\n\n".join(sections) + "\n"

    session_word = "session" if applied == 1 else "sessions"
    summary = (
        f"Built by tutor-memory from {applied} reviewed {session_word}. Follow it in every session."
    )
    sections = [
        "# Study brief",
        summary,
        "## How to teach\n\n" + _bullets(instructions),
        "## Observed patterns\n\n"
        + _bullets([sanitize_brief_text(item.claim, 200) for item in promoted]),
    ]
    if courses_md is not None and courses_md.strip():
        sections.append("## Courses and progress\n\n" + courses_md.strip())
    return "\n\n".join(sections) + "\n"


def _cell(value: object) -> str:
    if value is None:
        return "-"
    return str(value).replace("|", r"\|").replace("\n", "<br>")


def _quotes(evidence: tuple[EvidenceRef, ...]) -> str:
    return "<br>".join(_cell(item.quote) for item in evidence) or "-"


def render_profile(state: ProfileState, *, threshold: int = 3) -> str:
    """Render the complete learner profile as Markdown."""
    ordinals = {
        record.session_id: record.ordinal for record in state.sessions if record.ordinal is not None
    }
    lines = [
        "# Learner profile",
        "",
        "## Instructions",
        "",
        "| id | status | claim | evidence | first seen | last seen | quotes |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    if state.instructions:
        for item in state.instructions:
            evidence_ordinals = [
                ordinals[evidence.session_id]
                for evidence in item.evidence
                if evidence.session_id in ordinals
            ]
            last_seen = max(evidence_ordinals, default=item.first_seen)
            lines.append(
                f"| {_cell(item.id)} | {_cell(item.status)} | {_cell(item.claim)} | "
                f"{len(item.evidence)} | {item.first_seen} | {last_seen} | "
                f"{_quotes(item.evidence)} |"
            )
    else:
        lines.append("| - | - | None yet. | 0 | - | - | - |")

    lines.extend(
        [
            "",
            "## Hypotheses",
            "",
            "| id | status | claim | sessions | first seen | last seen | quotes |",
            "| --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    if state.hypotheses:
        for item in state.hypotheses:
            lines.append(
                f"| {_cell(item.id)} | {_cell(item.status)} | {_cell(item.claim)} | "
                f"{len(item.sessions)}/{threshold} | {item.first_seen} | {item.last_seen} | "
                f"{_quotes(item.evidence)} |"
            )
    else:
        lines.append(f"| - | - | None yet. | 0/{threshold} | - | - | - |")

    lines.extend(
        [
            "",
            "## Stuck points",
            "",
            "| session | course | concept | claim | quote |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    if state.stuck_points:
        for item in state.stuck_points:
            lines.append(
                f"| {_cell(item.session_id)} | {_cell(item.course)} | {_cell(item.concept)} | "
                f"{_cell(item.claim)} | {_cell(item.quote)} |"
            )
    else:
        lines.append("| - | - | - | None yet. | - |")

    lines.extend(
        [
            "",
            "## Sessions",
            "",
            "| id | index | course | date | status | ordinal |",
            "| --- | ---: | --- | --- | --- | ---: |",
        ]
    )
    if state.sessions:
        for item in state.sessions:
            ordinal = item.ordinal if item.ordinal is not None else "pending"
            lines.append(
                f"| {_cell(item.session_id)} | {item.index} | {_cell(item.course)} | "
                f"{_cell(item.date)} | {_cell(item.status)} | {ordinal} |"
            )
    else:
        lines.append("| - | - | None yet. | - | - | - |")
    return "\n".join(lines) + "\n"
