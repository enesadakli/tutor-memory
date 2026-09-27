from __future__ import annotations

from .models import EvidenceRef, ProfileState


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- None yet."


def render_brief(state: ProfileState, courses_md: str | None) -> str:
    """Render the short tutor-facing study brief."""
    applied = sum(record.status == "applied" for record in state.sessions)
    instructions = [item.claim for item in state.instructions if item.status == "active"]
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
    sections = [
        "# Study brief",
        f"Built by tutor-memory from {applied} reviewed sessions. Follow it in every session.",
        "## How to teach\n\n" + _bullets(instructions),
        "## Observed patterns\n\n" + _bullets([item.claim for item in promoted]),
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
        "| id | status | claim | sessions | first seen | last seen | quotes |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    if state.instructions:
        for item in state.instructions:
            session_count = len({evidence.session_id for evidence in item.evidence})
            evidence_ordinals = [
                ordinals[evidence.session_id]
                for evidence in item.evidence
                if evidence.session_id in ordinals
            ]
            last_seen = max(evidence_ordinals, default=item.first_seen)
            lines.append(
                f"| {_cell(item.id)} | {_cell(item.status)} | {_cell(item.claim)} | "
                f"{session_count}/{threshold} | {item.first_seen} | {last_seen} | "
                f"{_quotes(item.evidence)} |"
            )
    else:
        lines.append(f"| - | - | None yet. | 0/{threshold} | - | - | - |")

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
