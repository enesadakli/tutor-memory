from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from .errors import ParseError
from .models import Model
from .storage import Workspace, read_json, write_json


@dataclass(frozen=True, slots=True)
class LastSession(Model):
    session_id: str
    index: int
    date: str | None
    covered: str
    next: str

    def __post_init__(self) -> None:
        if self.index < 1:
            raise ValueError("index must be at least 1")


@dataclass(frozen=True, slots=True)
class CourseProgress(Model):
    name: str
    static: tuple[str, ...]
    last: LastSession | None


@dataclass(frozen=True, slots=True)
class Progress(Model):
    courses: tuple[CourseProgress, ...]
    preamble: tuple[str, ...] = ()


class ProgressLabels(Protocol):
    heading: str
    last_label: str
    next_label: str


def load_progress(ws: Workspace) -> Progress | None:
    """Load course progress, returning None when it has not been initialized."""
    if not ws.progress_path.exists():
        return None
    return Progress.from_dict(read_json(ws.progress_path))


def write_progress(ws: Workspace, progress: Progress) -> None:
    """Persist course progress atomically."""
    write_json(ws.progress_path, progress)


def split_base(base_md: str, heading: str) -> tuple[str, Progress]:
    """Remove a course-progress section from a base brief and parse its course blocks."""
    if not heading.startswith("## "):
        raise ParseError("progress heading must be a level-two Markdown heading")

    lines = base_md.splitlines()
    try:
        start = lines.index(heading)
    except ValueError as exc:
        raise ParseError(f"progress heading not found: {heading}") from exc

    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    section = lines[start + 1 : end]
    preamble: list[str] = []
    courses: list[CourseProgress] = []
    current_name: str | None = None
    current_static: list[str] = []

    def finish_course() -> None:
        if current_name is not None:
            courses.append(CourseProgress(current_name, tuple(current_static), None))

    for line in section:
        if line.startswith("### "):
            finish_course()
            current_name = line.removeprefix("### ").strip()
            current_static = []
            if not current_name:
                raise ParseError("progress course heading must have a name")
        elif line.strip():
            if current_name is None:
                preamble.append(line)
            else:
                current_static.append(line)
    finish_course()

    before = lines[:start]
    after = lines[end:]
    while before and not before[-1].strip():
        before.pop()
    while after and not after[0].strip():
        after.pop(0)
    remaining = before + ([""] if before and after else []) + after
    base_without_section = "\n".join(remaining)
    if base_without_section:
        base_without_section += "\n"
    return base_without_section, Progress(tuple(courses), tuple(preamble))


def render_progress(progress: Progress, labels: ProgressLabels) -> str:
    """Render the maintained course-progress section as Markdown."""
    blocks = ["\n".join((labels.heading, *progress.preamble))]
    for course in progress.courses:
        lines = [f"### {course.name}", *course.static]
        if course.last is not None:
            date = course.last.date or "?"
            lines.extend(
                (
                    f"- {labels.last_label} ({date}): {course.last.covered}",
                    f"- {labels.next_label}: {course.last.next}",
                )
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def apply_session(
    progress: Progress,
    course: str,
    session_id: str,
    index: int,
    date: str | None,
    covered: str,
    next: str,
) -> Progress:
    """Return progress updated with a session when its course and ordering are valid."""
    position = None
    for number, item in enumerate(progress.courses):
        if item.name == course:
            position = number
            break
    if position is None:
        return progress
    current = progress.courses[position]
    if current.last is not None and index < current.last.index:
        return progress
    updated_course = replace(
        current,
        last=LastSession(session_id, index, date, covered, next),
    )
    courses = list(progress.courses)
    courses[position] = updated_course
    return replace(progress, courses=tuple(courses))
