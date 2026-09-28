from __future__ import annotations

import json
import os
import plistlib
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tutormem.automatic import (
    install_agent,
    profile_diff,
    revoke,
    run_auto,
    uninstall_agent,
)
from tutormem.config import AutoConfig, Config
from tutormem.errors import AutomaticModeError, ExtractorError, ReviewerError
from tutormem.extract import ProgressResult
from tutormem.models import (
    Decision,
    DecisionFile,
    ExtractResult,
    Hypothesis,
    Instruction,
    Observation,
    ProfileState,
)
from tutormem.progress import CourseProgress, Progress, load_progress, write_progress
from tutormem.storage import Workspace, list_sessions, read_json

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


class FakeExtractor:
    name = "fake"
    model = None

    def __init__(self, failures: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.failures = failures or set()

    def extract(self, session, open_items):  # type: ignore[no-untyped-def]
        del open_items
        self.calls.append(session.session_id)
        if session.session_id in self.failures:
            raise ExtractorError("synthetic extraction failure")
        quote = next(turn.text for turn in session.turns if turn.speaker == "learner")
        observation = Observation(
            f"{session.session_id}:1",
            "explicit_instruction",
            f"Teach {session.session_id} carefully.",
            (quote,),
        )
        return ExtractResult(
            session.session_id,
            session.content_sha256,
            self.name,
            self.model,
            (observation,),
            (),
        )


class FakeProgressExtractor:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def extract(self, session_turns, course_names):  # type: ignore[no-untyped-def]
        self.calls += 1
        assert session_turns
        assert tuple(course_names) == ("Databases",)
        if self.fail:
            raise ExtractorError("synthetic progress failure")
        return ProgressResult("Databases", "Relations and keys", "Start joins")


class FakeReviewer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def propose(self, packet, result):  # type: ignore[no-untyped-def]
        del packet
        self.calls.append(result.session_id)
        return DecisionFile(
            result.session_id,
            result.content_sha256,
            tuple(Decision(item.observation.id, "accept", "claude") for item in result.verified),
        )


class FailingReviewer(FakeReviewer):
    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error

    def propose(self, packet, result):  # type: ignore[no-untyped-def]
        del packet
        self.calls.append(result.session_id)
        raise ReviewerError(self.error)


def _config(**overrides: object) -> Config:
    values = {
        "inbox": "unused",
        "idle_minutes": 20,
        "approve": "claude",
        "sync": False,
        "notify": False,
        "default_course": "General",
        "changelog_copy": "",
        **overrides,
    }
    return Config(auto=AutoConfig(**values))  # type: ignore[arg-type]


def _capture(
    inbox: Path,
    chat_id: str,
    *,
    learner: str = "Please use steps.",
    tutor: str = "Sure.",
    title: str | None = "Databases",
    age_minutes: int = 30,
) -> tuple[Path, Path]:
    inbox.mkdir(parents=True, exist_ok=True)
    transcript = inbox / f"gemini-{chat_id}.md"
    transcript.write_text(f"### learner\n{learner}\n\n### tutor\n{tutor}\n", encoding="utf-8")
    sidecar = transcript.with_suffix(".json")
    sidecar.write_text(
        json.dumps(
            {
                "source": "gemini-gem",
                "gem_id": "gem-1",
                "chat_id": chat_id,
                "title": title,
                "url": "https://gemini.example/chat",
                "turns": 2,
                "updated_at": "2026-09-27T11:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    stamp = NOW.timestamp() - age_minutes * 60
    os.utime(transcript, (stamp, stamp))
    os.utime(sidecar, (stamp, stamp))
    return transcript, sidecar


def _run(ws: Workspace, inbox: Path, **kwargs: object):  # type: ignore[no-untyped-def]
    extractor = kwargs.pop("extractor", FakeExtractor())
    reviewer = kwargs.pop("reviewer", FakeReviewer())
    return run_auto(
        ws,
        kwargs.pop("config", _config()),
        inbox=inbox,
        extractor=extractor,
        reviewer=reviewer,
        now=NOW,
        **kwargs,
    )


def test_auto_filters_idle_requires_sidecar_and_skips_no_learner(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    inbox = tmp_path / "inbox"
    _capture(inbox, "ready")
    _capture(inbox, "active", age_minutes=5)
    orphan, orphan_sidecar = _capture(inbox, "orphan")
    orphan_sidecar.unlink()
    no_learner, no_learner_sidecar = _capture(inbox, "tutoronly")
    no_learner.write_text("### tutor\nOnly tutor text.\n", encoding="utf-8")
    old = NOW.timestamp() - 30 * 60
    os.utime(no_learner, (old, old))
    os.utime(no_learner_sidecar, (old, old))

    result = _run(ws, inbox)

    assert result.processed == ("gem-ready",)
    assert [item.session_id for item in list_sessions(ws)] == ["gem-ready"]
    assert orphan.exists()


def test_auto_skips_same_sha_and_replaces_chat_when_it_grows(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    inbox = tmp_path / "inbox"
    transcript, sidecar = _capture(inbox, "ABC")
    extractor = FakeExtractor()
    reviewer = FakeReviewer()

    first = _run(ws, inbox, extractor=extractor, reviewer=reviewer)
    second = _run(ws, inbox, extractor=extractor, reviewer=reviewer)
    original = list_sessions(ws)[0]
    transcript.write_text(
        transcript.read_text(encoding="utf-8") + "\n### learner\nOne more question.\n",
        encoding="utf-8",
    )
    old = NOW.timestamp() - 30 * 60
    os.utime(transcript, (old, old))
    os.utime(sidecar, (old, old))
    third = _run(ws, inbox, extractor=extractor, reviewer=reviewer)
    grown = list_sessions(ws)[0]

    assert first.processed == ("gem-abc",)
    assert second.processed == ()
    assert third.processed == ("gem-abc",)
    assert extractor.calls == ["gem-abc", "gem-abc"]
    assert grown.index == original.index == 1
    assert grown.content_sha256 != original.content_sha256


def test_auto_lock_fresh_skips_and_stale_is_replaced(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    ws.root.mkdir()
    ws.auto_lock_path.write_text("busy\n", encoding="utf-8")
    os.utime(ws.auto_lock_path, (NOW.timestamp(), NOW.timestamp()))

    locked = _run(ws, tmp_path / "missing")
    assert locked.locked is True
    assert ws.auto_lock_path.exists()

    stale = NOW.timestamp() - 61 * 60
    os.utime(ws.auto_lock_path, (stale, stale))
    unlocked = _run(ws, tmp_path / "missing")
    assert unlocked.locked is False
    assert not ws.auto_lock_path.exists()


def test_auto_failure_isolated_and_retried(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    inbox = tmp_path / "inbox"
    _capture(inbox, "bad", learner="Please use steps for bad.")
    _capture(inbox, "good", learner="Please use steps for good.")
    failing = FakeExtractor({"gem-bad"})

    result = _run(ws, inbox, extractor=failing)
    assert result.processed == ("gem-good",)
    assert result.failed == ("gem-bad",)
    assert "gem-bad" in ws.auto_log_path.read_text(encoding="utf-8")

    retry = _run(ws, inbox, extractor=FakeExtractor())
    assert retry.processed == ("gem-bad",)


def test_auto_reuses_extraction_after_reviewer_failure(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    inbox = tmp_path / "inbox"
    _capture(inbox, "review-retry")
    extractor = FakeExtractor()

    first = _run(ws, inbox, extractor=extractor, reviewer=FailingReviewer("review failed"))
    calls_after_first = len(extractor.calls)
    reviewer = FakeReviewer()
    second = _run(ws, inbox, extractor=extractor, reviewer=reviewer)

    assert first.failed == ("gem-review-retry",)
    assert calls_after_first == 1
    assert len(extractor.calls) == calls_after_first
    assert second.processed == ("gem-review-retry",)
    assert reviewer.calls == ["gem-review-retry"]


def test_auto_uses_cached_extracted_course_and_updates_progress(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    inbox = tmp_path / "inbox"
    _capture(inbox, "progress", title=None)
    write_progress(ws, Progress((CourseProgress("Databases", ("- Static",), None),)))
    ws.base_path.write_text("# Tutor brief\n", encoding="utf-8")
    progress_extractor = FakeProgressExtractor()

    first = _run(
        ws,
        inbox,
        progress_extractor=progress_extractor,
        reviewer=FailingReviewer("review failed"),
    )
    second = _run(
        ws,
        inbox,
        progress_extractor=progress_extractor,
        reviewer=FakeReviewer(),
    )

    assert first.failed == ("gem-progress",)
    assert second.processed == ("gem-progress",)
    assert progress_extractor.calls == 1
    assert list_sessions(ws)[0].course == "Databases"
    progress = load_progress(ws)
    assert progress is not None
    assert progress.courses[0].last is not None
    assert progress.courses[0].last.covered == "Relations and keys"
    changelog = ws.changelog_path.read_text(encoding="utf-8")
    assert "Progress: Databases — Relations and keys → next: Start joins" in changelog
    brief = (ws.out_dir / "brief.md").read_text(encoding="utf-8")
    assert brief.index("## Courses and progress") < brief.index("## Learned from sessions")


def test_auto_progress_failure_logs_and_falls_back_without_blocking(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    inbox = tmp_path / "inbox"
    _capture(inbox, "progress-fail", title=None)
    original = Progress((CourseProgress("Databases", (), None),))
    write_progress(ws, original)

    result = _run(ws, inbox, progress_extractor=FakeProgressExtractor(fail=True))

    assert result.processed == ("gem-progress-fail",)
    assert result.failed == ()
    assert list_sessions(ws)[0].course == "General"
    assert load_progress(ws) == original
    log = ws.auto_log_path.read_text(encoding="utf-8")
    assert "progress extraction failed: synthetic progress failure" in log


def test_auto_failure_notifications_are_deduplicated_and_cleared(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    inbox = tmp_path / "inbox"
    _capture(inbox, "dedupe")
    extractor = FakeExtractor()
    messages: list[str] = []
    config = _config(notify=True)

    _run(
        ws,
        inbox,
        config=config,
        extractor=extractor,
        reviewer=FailingReviewer("same error"),
        notifier=messages.append,
    )
    _run(
        ws,
        inbox,
        config=config,
        extractor=extractor,
        reviewer=FailingReviewer("same error"),
        notifier=messages.append,
    )
    _run(
        ws,
        inbox,
        config=config,
        extractor=extractor,
        reviewer=FailingReviewer("different error"),
        notifier=messages.append,
    )

    assert messages == [
        "Automatic processing failed: gem-dedupe: same error",
        "Automatic processing failed: gem-dedupe: different error",
    ]
    assert read_json(ws.auto_notified_path) == {"gem-dedupe": "different error"}
    assert ws.auto_log_path.read_text(encoding="utf-8").count("gem-dedupe") == 3

    _run(
        ws,
        inbox,
        config=config,
        extractor=extractor,
        reviewer=FakeReviewer(),
        notifier=messages.append,
    )
    assert read_json(ws.auto_notified_path) == {}


def test_auto_approve_none_stops_at_proposal_and_does_not_repeat(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    inbox = tmp_path / "inbox"
    _capture(inbox, "manual")
    config = _config(approve="none")
    reviewer = FakeReviewer()

    first = _run(ws, inbox, config=config, reviewer=reviewer)
    second = _run(ws, inbox, config=config, reviewer=reviewer)

    assert first.processed == ("gem-manual",)
    assert second.processed == ()
    assert ws.proposed_decisions_path("gem-manual").exists()
    assert not ws.decisions_path("gem-manual").exists()
    assert len(reviewer.calls) == 1


def test_auto_dry_run_changes_nothing(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    ws.root.mkdir()
    inbox = tmp_path / "inbox"
    _capture(inbox, "dry")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    result = _run(ws, inbox, dry_run=True)
    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    assert result.processed == ("gem-dry",)
    assert result.changed is False
    assert before == after


def test_auto_writes_diff_changelog_and_notification(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    inbox = tmp_path / "inbox"
    _capture(inbox, "notify")
    messages: list[str] = []

    result = _run(ws, inbox, config=_config(notify=True), notifier=messages.append)

    assert result.changed is True
    changelog = ws.changelog_path.read_text(encoding="utf-8")
    line = "New instruction: Teach gem-notify carefully. (ins-gem-notify:1)"
    assert "Sessions: gem-notify" in changelog
    assert line in changelog
    assert "Undo: tutormem revoke ins-gem-notify:1" in changelog
    assert messages == [line]


def test_revoke_appends_to_latest_decision_and_rejects_invalid_targets(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    inbox = tmp_path / "inbox"
    _capture(inbox, "one", learner="Please use steps for one.")
    _capture(inbox, "two", learner="Please use steps for two.")
    _run(ws, inbox)
    target = "ins-gem-one:1"

    state = revoke(ws, _config(), target, reason="Wrong rule", no_sync=True, now=NOW)
    latest = ws.decisions_path("gem-two")
    payload = read_json(latest)

    assert payload["revocations"][-1] == {
        "target_id": target,
        "reviewer": "human",
        "reason": "Wrong rule",
    }
    assert next(item for item in state.instructions if item.id == target).status == "revoked"
    unchanged = latest.read_bytes()
    with pytest.raises(AutomaticModeError):
        revoke(ws, _config(), target, no_sync=True)
    with pytest.raises(AutomaticModeError):
        revoke(ws, _config(), "hyp-missing:1", no_sync=True)
    assert latest.read_bytes() == unchanged


def test_profile_diff_covers_promote_progress_drop_and_revoke() -> None:
    old_instruction = Instruction("ins-a:1", "Old", "active", (), 1, None)
    old_open = Hypothesis("hyp-a:1", "Visual", "open", ("a",), (), 1, 1, None, None, None)
    old_drop = Hypothesis("hyp-d:1", "Stale", "open", ("d",), (), 1, 1, None, None, None)
    before = ProfileState(1, (old_instruction,), (old_open, old_drop), (), ())
    after = ProfileState(
        1,
        (replace(old_instruction, status="revoked", revoked_at=3),),
        (
            replace(old_open, sessions=("a", "b"), last_seen=2),
            replace(old_drop, status="dropped", dropped_at=3),
            Hypothesis("hyp-p:1", "Diagrams", "promoted", ("a", "b", "c"), (), 1, 3, 3, None, None),
            Hypothesis("hyp-n:1", "Examples", "open", ("c",), (), 3, 3, None, None, None),
        ),
        (),
        (),
    )

    assert profile_diff(before, after) == [
        "Hypothesis 2/3: Visual (hyp-a:1)",
        "Promoted: Diagrams (hyp-p:1)",
        "New hypothesis: Examples (hyp-n:1)",
        "Dropped: Stale (hyp-d:1)",
        "Revoked: Old (ins-a:1)",
    ]


def test_install_and_uninstall_agent_plist_and_commands(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "workspace")
    home = tmp_path / "home"
    calls: list[list[str]] = []

    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    path = install_agent(
        ws,
        interval_minutes=17,
        load=True,
        home=home,
        executable="/opt/homebrew/bin/tutormem",
        uid=501,
        runner=runner,
    )
    payload = plistlib.loads(path.read_bytes())

    assert payload["ProgramArguments"] == [
        "/opt/homebrew/bin/tutormem",
        "--workspace",
        str(ws.root.resolve()),
        "auto",
    ]
    assert payload["StartInterval"] == 17 * 60
    assert payload["RunAtLoad"] is True
    assert payload["StandardOutPath"].endswith("auto.stdout.log")
    assert payload["EnvironmentVariables"]["PATH"].startswith(str(home / ".local/bin"))
    assert payload["EnvironmentVariables"]["HOME"] == str(home)
    assert payload["EnvironmentVariables"]["USER"] == payload["EnvironmentVariables"]["LOGNAME"]
    assert [call[1] for call in calls] == ["bootout", "bootstrap"]

    uninstall_agent(home=home, uid=501, runner=runner)
    assert not path.exists()
    assert calls[-1][1] == "bootout"
