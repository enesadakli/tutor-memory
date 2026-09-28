from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from string import Template
from tempfile import TemporaryDirectory
from typing import Protocol

from .errors import PendingReviewError, ReviewerError, SchemaError, StaleArtifactError
from .models import (
    ApprovedEvidence,
    DecisionFile,
    ProfileState,
    Session,
    SessionApproval,
    VerifyResult,
)

REVIEWER_SYSTEM_PROMPT = (
    "You review learner-profile observations. Follow the instructions in the user message "
    "exactly and answer only with the requested JSON."
)


class Runner(Protocol):
    def __call__(
        self, args: list[str], stdin: str, timeout: float, *, cwd: Path
    ) -> subprocess.CompletedProcess[str]: ...


_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
_REVIEWER_RULES = (
    "Reject an observation when its claim is broader than its quotes or its kind is wrong, "
    "unless you correct it with a narrower claim or kind override. Map to an existing id only "
    "when it is clearly the same behaviour. Return one decision for every verified observation. "
    "Never invent observations. Reject an explicit_instruction that is a one-off request about "
    "the current topic, or re-classify it as an inference when the pattern could recur; "
    '"slayttaki terimleri Türkçeleştirme" is standing, while "bunu da detaylı anlat" is one-off.'
)


def _default_runner(
    args: list[str], stdin: str, timeout: float, *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        cwd=cwd,
    )


def _stderr_suffix(completed: subprocess.CompletedProcess[str]) -> str:
    stderr = completed.stderr or ""
    return f"; stderr: {stderr[:500]}" if stderr else ""


def _first_json_object(text: str) -> dict[str, object]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("no JSON object found")


def _context(text: str, start: int, end: int) -> str:
    context_start = max(0, start - 200)
    context_end = min(len(text), end + 200)
    prefix = "…" if context_start else ""
    suffix = "…" if context_end < len(text) else ""
    return (
        f"{prefix}{text[context_start:start]}**{text[start:end]}**{text[end:context_end]}{suffix}"
    )


def write_packet(session: Session, result: VerifyResult, profile: ProfileState) -> str:
    """Build the Markdown review packet for one verified session."""
    profile_items = {item.id: item for item in (*profile.instructions, *profile.hypotheses)}
    lines = [
        f"# Review packet: {session.session_id}",
        "",
        f"Content SHA-256: `{result.content_sha256}`",
        "",
        "## Verified observations",
    ]
    if not result.verified:
        lines.extend(["", "(none)"])
    for verified in result.verified:
        observation = verified.observation
        lines.extend(
            [
                "",
                f"### {observation.id}",
                "",
                f"- Kind: `{observation.kind}`",
                f"- Claim: {observation.claim}",
            ]
        )
        if observation.proposed_match is None:
            lines.append("- Proposed match: (none)")
        else:
            matched = profile_items.get(observation.proposed_match)
            detail = f" — {matched.claim}" if matched is not None else ""
            lines.append(f"- Proposed match: `{observation.proposed_match}`{detail}")
        if observation.concept is not None:
            lines.append(f"- Concept: {observation.concept}")
        lines.extend(["- Evidence:", ""])
        for quote in verified.quotes:
            turn = session.turns[quote.turn_index]
            lines.append(
                f"  - Turn {quote.turn_index}: {_context(turn.text, quote.start, quote.end)}"
            )

    lines.extend(["", "## Rejected observations"])
    if not result.rejected:
        lines.extend(["", "(none)"])
    for rejected in result.rejected:
        lines.extend(
            [
                "",
                f"- `{rejected.observation.id}` — `{rejected.reason}`: {rejected.detail}",
            ]
        )

    lines.extend(["", "## Open and active items", ""])
    open_items = [
        item
        for item in (*profile.instructions, *profile.hypotheses)
        if item.status in ("active", "open", "promoted")
    ]
    if open_items:
        lines.extend(f"- `{item.id}`: {item.claim}" for item in open_items)
    else:
        lines.append("(none)")
    lines.extend(["", "## Reviewer rules", "", _REVIEWER_RULES, ""])
    return "\n".join(lines)


def skeleton(result: VerifyResult) -> DecisionFile:
    """Create an empty decision file for manual review."""
    return DecisionFile(result.session_id, result.content_sha256, (), ())


class ClaudeReviewer:
    """Propose review decisions using the configured command-line reviewer."""

    def __init__(self, model: str, timeout_s: int, *, runner: Runner | None = None) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self._runner = runner or _default_runner

    def propose(self, packet: str, result: VerifyResult) -> DecisionFile:
        """Return proposed decisions for a review packet."""
        try:
            template = (_PROMPTS / "review.md").read_text(encoding="utf-8")
        except OSError as exc:
            raise ReviewerError(f"cannot read review prompt: {exc}") from exc
        prompt = Template(template).substitute(packet=packet)
        args = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--model",
            self.model,
            "--tools",
            "",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--setting-sources",
            "",
            "--system-prompt",
            REVIEWER_SYSTEM_PROMPT,
        ]
        try:
            with TemporaryDirectory() as temporary_directory:
                completed = self._runner(
                    args, prompt, float(self.timeout_s), cwd=Path(temporary_directory)
                )
        except subprocess.TimeoutExpired as exc:
            raise ReviewerError(f"claude reviewer timed out after {self.timeout_s}s") from exc
        if completed.returncode != 0:
            message = f"claude reviewer exited with status {completed.returncode}"
            raise ReviewerError(message + _stderr_suffix(completed))
        try:
            envelope = json.loads(completed.stdout or "")
        except json.JSONDecodeError as exc:
            raise ReviewerError(
                f"claude reviewer returned invalid JSON{_stderr_suffix(completed)}"
            ) from exc
        if not isinstance(envelope, dict):
            raise ReviewerError("claude reviewer output must be a JSON object")
        if envelope.get("is_error") is True:
            raise ReviewerError(
                f"claude reviewer reported an error: {envelope.get('result', '')}"
                f"{_stderr_suffix(completed)}"
            )
        model_text = envelope.get("result")
        if not isinstance(model_text, str):
            raise ReviewerError("claude reviewer output has no result text")
        try:
            payload = _first_json_object(model_text)
            decision_file = DecisionFile.from_dict(payload)
        except (ValueError, SchemaError) as exc:
            raise ReviewerError(f"claude reviewer returned an invalid DecisionFile: {exc}") from exc
        if (
            decision_file.session_id != result.session_id
            or decision_file.content_sha256 != result.content_sha256
        ):
            raise ReviewerError("claude reviewer returned a DecisionFile for a different session")
        return dataclasses.replace(
            decision_file,
            decisions=tuple(
                dataclasses.replace(decision, reviewer="claude")
                for decision in decision_file.decisions
            ),
            revocations=tuple(
                dataclasses.replace(revocation, reviewer="claude")
                for revocation in decision_file.revocations
            ),
        )


def apply_decisions(result: VerifyResult, decisions: DecisionFile) -> SessionApproval:
    """Apply complete review decisions to verified observations."""
    if (
        result.session_id != decisions.session_id
        or result.content_sha256 != decisions.content_sha256
    ):
        raise StaleArtifactError("decision file does not match verification result")

    verified = {item.observation.id: item for item in result.verified}
    by_id = {}
    for decision in decisions.decisions:
        if decision.observation_id not in verified:
            raise SchemaError(
                f"decision references an unverified observation: {decision.observation_id}"
            )
        if decision.observation_id in by_id:
            raise SchemaError(f"duplicate decision for observation: {decision.observation_id}")
        by_id[decision.observation_id] = decision
    missing = [item.observation.id for item in result.verified if item.observation.id not in by_id]
    if missing:
        raise PendingReviewError("missing decisions for: " + ", ".join(missing))

    evidence: list[ApprovedEvidence] = []
    for verified_observation in result.verified:
        observation = verified_observation.observation
        decision = by_id[observation.id]
        if decision.action == "reject":
            continue
        match = None
        if not decision.force_new:
            match = decision.match if decision.match is not None else observation.proposed_match
        evidence.append(
            ApprovedEvidence(
                session_id=result.session_id,
                observation_id=observation.id,
                kind=decision.kind if decision.kind is not None else observation.kind,
                claim=decision.claim if decision.claim is not None else observation.claim,
                quotes=verified_observation.quotes,
                match=match,
                force_new=decision.force_new,
                concept=observation.concept,
            )
        )
    return SessionApproval(result.session_id, tuple(evidence), decisions.revocations)
