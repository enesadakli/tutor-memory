# tutor-memory specification (v1, frozen contract)

This document is the contract every module is built against. If code and this
document disagree, the code is wrong. Changing a contract here is a deliberate,
reviewed decision, never a side effect of implementing a module.

## 1. What the pipeline does

A learner studies with an LLM tutor (for example a Gemini Gem). After each study
session the chat is exported. The pipeline turns those exports into a short,
evidence-backed **brief** that the tutor reads at the start of the next session.

```
export (pdf | txt | md)
  -> ingest   -> Session           (canonical learner/tutor turns)
  -> extract  -> Observation[]     (cheap model proposes quoted observations)
  -> verify   -> VerifyResult      (deterministic: every quote must exist in a learner turn)
  -> review   -> DecisionFile      (judgment: accept / reject / narrow / map; Claude or a human)
  -> replay   -> ProfileState      (deterministic evidence rule over all approved sessions)
  -> render   -> profile.md, brief.md
  -> sync     -> Google Doc the tutor reads (separate, explicit command)
```

Two gates are deterministic and never delegated to a model: **quote verification**
and **the evidence rule**. Models only propose; state is always recomputed from
approved decisions (`replay`), never edited in place.

## 2. Workspace layout

A workspace is a directory, by default **outside** the repository:
`$TUTORMEM_WORKSPACE`, else `~/.local/share/tutor-memory/default`. CLI flag
`--workspace PATH` overrides both.

```
<ws>/tutormem.toml                    optional config (section 7)
<ws>/courses.md                       optional, hand-written, copied into the brief verbatim
<ws>/sessions/<session_id>.json       canonical Session
<ws>/runs/<session_id>/observations.json   ExtractResult
<ws>/runs/<session_id>/verify.json         VerifyResult
<ws>/runs/<session_id>/review.md           review packet for the reviewer
<ws>/runs/<session_id>/decisions.proposed.json   DecisionFile proposed by a model reviewer
<ws>/runs/<session_id>/decisions.json            approved DecisionFile (the only one replay reads)
<ws>/state/profile.json               ProfileState (derived, rewritten from scratch by replay)
<ws>/out/profile.md                   rendered profile
<ws>/out/brief.md                     rendered brief
```

All JSON files are UTF-8, `ensure_ascii=False`, `indent=2`, keys in dataclass
field order, trailing newline. All writes go through `storage.write_json` /
`storage.write_text`, which write to a temp file in the same directory and
`os.replace` it (atomic).

Every run artifact carries `session_id` and `content_sha256`. An artifact whose
`content_sha256` differs from the current session file is **stale** and is
treated as missing.

## 3. Data model (`src/tutormem/models.py`)

All models are `@dataclass(frozen=True, slots=True)`; sequences are `tuple`.
Each model has `to_dict() -> dict` and `@classmethod from_dict(d) -> Self`.
`from_dict(x.to_dict()) == x` must hold. `from_dict` raises
`tutormem.errors.SchemaError` on a missing required key, an unknown enum value,
or a wrong type. Unknown keys are rejected (`SchemaError`).

Enums are `Literal` string types, listed with their allowed values:

- `Speaker`: `"learner" | "tutor"`
- `ObservationKind`: `"explicit_instruction" | "inference" | "stuck_point"`
- `RejectReason`: `"not_found" | "tutor_turn" | "empty_quote" | "unattributed_transcript"`
- `DecisionAction`: `"accept" | "reject"`
- `Reviewer`: `"claude" | "human"`
- `InstructionStatus`: `"active" | "revoked"`
- `HypothesisStatus`: `"open" | "promoted" | "dropped" | "revoked"`
- `SessionStatus`: `"applied" | "pending"`

### Session layer
- `Turn(speaker: Speaker, text: str)`
- `Session(session_id: str, content_sha256: str, course: str, date: str | None,
  index: int, source: str, turns: tuple[Turn, ...])`
  - `session_id`: `[a-z0-9][a-z0-9-]*`, stable logical id.
  - `content_sha256`: hex sha256 of `json.dumps([[t.speaker, t.text] for t in turns], ensure_ascii=False, separators=(",", ":"))` encoded UTF-8. Helper: `models.turns_sha256(turns)`.
  - `date`: `YYYY-MM-DD` or `None` (exports often carry no date).
  - `index`: ingest order, 1-based, unique in the workspace. Replace keeps the index.
  - `source`: original file **name only** (never an absolute path).

### Extraction layer
- `Observation(id: str, kind: ObservationKind, claim: str, quotes: tuple[str, ...],
  proposed_match: str | None = None, concept: str | None = None)`
  - `id` is `"<session_id>:<n>"`, n = 1, 2, ... in extractor output order.
  - `quotes` are verbatim learner words; at least one element (an empty tuple is a SchemaError; an empty *string* is allowed and is rejected by verify as `empty_quote`).
  - `proposed_match`: an existing instruction id (`ins-...`) or hypothesis id (`hyp-...`), or None.
- `DroppedRecord(raw: str, error: str)` — extractor output that failed schema validation.
- `ExtractResult(session_id: str, content_sha256: str, extractor: str, model: str | None,
  observations: tuple[Observation, ...], dropped: tuple[DroppedRecord, ...])`

### Verification layer
- `VerifiedQuote(quote: str, turn_index: int, start: int, end: int, matched_text: str, exact: bool)`
  - `turn_index` indexes `Session.turns`.
  - `start`/`end` are Python `str` indices into `Session.turns[turn_index].text` (the canonical text), `end` exclusive, so `text[start:end] == matched_text`.
  - `exact` is True when `quote` occurs literally; False when it only matched after normalization (section 4.2).
- `VerifiedObservation(observation: Observation, quotes: tuple[VerifiedQuote, ...])`
- `RejectedObservation(observation: Observation, reason: RejectReason, detail: str)`
- `VerifyResult(session_id: str, content_sha256: str, verified: tuple[VerifiedObservation, ...],
  rejected: tuple[RejectedObservation, ...])`

### Review layer
- `Decision(observation_id: str, action: DecisionAction, reviewer: Reviewer, reason: str = "",
  claim: str | None = None, kind: ObservationKind | None = None, match: str | None = None,
  force_new: bool = False)`
  - `claim` None = keep the observation's claim; otherwise replaces it (narrowing).
  - `kind` None = keep; otherwise replaces it (e.g. an "explicit_instruction" that is really an inference).
  - `match` None = keep `proposed_match`; otherwise attach to that id.
  - `force_new` True = ignore any match and create a new instruction/hypothesis. `force_new` with a non-None `match` is a SchemaError.
- `Revocation(target_id: str, reviewer: Reviewer, reason: str)` — revokes an instruction or a hypothesis (any non-dropped status). Takes effect at the session whose decision file carries it.
- `DecisionFile(session_id: str, content_sha256: str, decisions: tuple[Decision, ...],
  revocations: tuple[Revocation, ...] = ())`
- `ApprovedEvidence(session_id: str, observation_id: str, kind: ObservationKind, claim: str,
  quotes: tuple[VerifiedQuote, ...], match: str | None, force_new: bool, concept: str | None)`
- `SessionApproval(session_id: str, evidence: tuple[ApprovedEvidence, ...], revocations: tuple[Revocation, ...])`

### Profile layer
- `EvidenceRef(session_id: str, observation_id: str, quote: str)` — `quote` is `matched_text`.
- `Instruction(id: str, claim: str, status: InstructionStatus, evidence: tuple[EvidenceRef, ...],
  first_seen: int, revoked_at: int | None)`
- `Hypothesis(id: str, claim: str, status: HypothesisStatus, sessions: tuple[str, ...],
  evidence: tuple[EvidenceRef, ...], first_seen: int, last_seen: int,
  promoted_at: int | None, dropped_at: int | None, revoked_at: int | None)`
  - `sessions`: distinct session ids in the order first seen.
- `StuckPoint(session_id: str, course: str, concept: str | None, claim: str, quote: str)`
- `SessionRecord(session_id: str, index: int, course: str, date: str | None,
  content_sha256: str, status: SessionStatus, ordinal: int | None)`
  - `ordinal`: 1-based position among **applied** sessions; None when pending.
- `ProfileState(schema_version: int, instructions: tuple[Instruction, ...],
  hypotheses: tuple[Hypothesis, ...], stuck_points: tuple[StuckPoint, ...],
  sessions: tuple[SessionRecord, ...])` — `schema_version` is `models.SCHEMA_VERSION = 1`.

`first_seen`, `last_seen`, `promoted_at`, `dropped_at`, `revoked_at` are **ordinals**
(applied-session positions), not ingest indices.

## 4. Module contracts

Errors live in `src/tutormem/errors.py`, all subclass `TutormemError`:
`SchemaError`, `ParseError`, `DuplicateContentError`, `SessionExistsError`,
`SessionNotFoundError`, `StaleArtifactError`, `PendingReviewError`,
`ReplayError`, `ExtractorError`, `ReviewerError`, `SyncError`.

### 4.1 ingest (`ingest.py`)
`ingest(path: Path, ws: Workspace, *, course: str, speakers: Literal["gemini", "manual"] = "gemini",
session_id: str | None = None, date: str | None = None, replace: bool = False) -> Session`

- Input by extension: `.pdf` (text via `pypdf`, optional extra `pdf`; missing package -> `ParseError` naming the extra), `.txt`, `.md`.
- `speakers="gemini"`: Gemini chat export. A learner turn starts at the marker `User prompt:` (optionally wrapped in `*...*` italics); the tutor turn starts at the following `Response:`. Text before the first marker is dropped. Export formatting on the marker line (the wrapping `*`) is removed; turn text is otherwise kept as is, except: strip each turn, collapse runs of 3+ newlines to 2. If no `User prompt:` marker is found -> `ParseError`.
- `speakers="manual"`: Markdown where each turn starts with a line exactly `### learner` or `### tutor`. Text before the first heading -> `ParseError` unless blank. Zero learner turns -> `ParseError`.
- `session_id` default: slug of the file stem (lowercase ASCII, Turkish letters transliterated ç->c ğ->g ı->i İ->i ö->o ş->s ü->u, non `[a-z0-9]` runs -> `-`, trimmed).
- Same `content_sha256` as any existing session -> `DuplicateContentError` (even with `replace`).
- Existing `session_id` without `replace` -> `SessionExistsError`. With `replace`: keeps `index`; run artifacts become stale automatically via the sha.
- New sessions get `index = max(existing) + 1` (1 when empty).
- Writes `sessions/<session_id>.json` and returns the Session.

### 4.2 verify (`verify.py`)
`verify(session: Session, observations: Sequence[Observation]) -> VerifyResult`

- Quotes are searched **only in learner turns**. If the session has no learner turn, every observation is rejected with `unattributed_transcript`.
- Per quote: empty or whitespace-only -> `empty_quote`. Try an exact substring search over learner turns in turn order; first hit wins (`exact=True`). Otherwise search normalized text (below) and map the hit back to original indices (`exact=False`). Not found in learner turns but found (exact or normalized) in a tutor turn -> `tutor_turn`. Otherwise `not_found`.
- An observation is verified only if **all** its quotes are found; the first failing quote decides the reason, `detail` names the failing quote.
- Normalization (applied identically to quote and turn text): Unicode NFC; typographic quotes `“ ” „ ‟ « »` -> `"`, `‘ ’ ‚ ‛` -> `'`; any whitespace run -> single space; strip. **Case is preserved** (no lower/casefold: Turkish `İ`/`ı` do not round-trip). `*` and `_` are **not** removed. Expose `normalize(text: str) -> tuple[str, list[int]]` returning the normalized text and, for each normalized character, the index of its source character in the original.
- Result order: `verified` and `rejected` keep the input observation order.

### 4.3 extract (`extract.py`)
`class Extractor(Protocol): name: str; model: str | None; def extract(self, session: Session, open_items: Sequence[Instruction | Hypothesis]) -> ExtractResult`

- `FileExtractor(path)`: reads a JSON file `{"observations": [...]}` whose items are Observation dicts **without** `id`; assigns ids `<session_id>:<n>`. Invalid items go to `dropped`.
- `AgyExtractor(model, timeout_s)`: runs the Antigravity CLI headless. Prompt = `prompts/extract.md` template filled with the canonical transcript (turns labeled `[learner]`/`[tutor]` with their turn index) and the open items (id + claim). Transcript text must **not** be passed as a command-line argument (it would show in `ps`); pass it on stdin or via a temp file (mode 0600, deleted afterwards). Use `--json-schema` with the observation-list schema, `--sandbox`, `--print-timeout <timeout_s>s`. Parse the first JSON value in stdout; invalid items -> `dropped`; non-zero exit, timeout, or no JSON -> `ExtractorError` with stderr truncated to 500 chars.
- `make_extractor(name: Literal["agy", "file"], config: Config, *, path: Path | None = None) -> Extractor`.

### 4.4 review (`review.py`)
- `write_packet(session: Session, result: VerifyResult, profile: ProfileState) -> str` — Markdown: every verified observation with id, kind, claim, proposed_match (and that item's claim), each quote with ±200 chars of surrounding turn text; then rejected observations with reasons; then the open/active items list; then instructions for the reviewer (the evidence rule, and the rule that a claim may never be broader than its quotes).
- `skeleton(result: VerifyResult) -> DecisionFile` — no decisions.
- `ClaudeReviewer(model: str, timeout_s: int).propose(packet: str, result: VerifyResult) -> DecisionFile` — runs `claude -p` with the packet on **stdin**, tools disabled (`--disallowedTools` for every built-in tool, or `--tools ""` if supported), `--output-format json`, asks for a DecisionFile JSON; every decision gets `reviewer="claude"`. Invalid output -> `ReviewerError`.
- `apply_decisions(result: VerifyResult, decisions: DecisionFile) -> SessionApproval`
  - sha or session_id mismatch -> `StaleArtifactError`.
  - Any verified observation without a decision -> `PendingReviewError` (the session is pending).
  - A decision for an id that is not a verified observation (unknown, or rejected by verify) -> `SchemaError`.
  - Two decisions for one observation -> `SchemaError`.
  - Accepted decisions become `ApprovedEvidence` with overrides applied (`claim`, `kind`, `match` or `proposed_match`, `force_new`, `concept` from the observation), in observation order. Rejected ones are dropped.

### 4.5 promote (`promote.py`) — the evidence rule
`replay(sessions: Sequence[Session], approvals: Mapping[str, SessionApproval | None], config: Config) -> ProfileState`

Pure function. `sessions` are processed in `index` order. A session whose approval
is None (missing, pending or stale) gets `status="pending"`, `ordinal=None` and
has **no effect**, including on staleness counting. Every other session gets the
next ordinal `k` and, in this order:

1. **Evidence**, in approval order:
   - `kind="explicit_instruction"`: target = `match` if given (must be an existing `ins-` id with status `active`, else `ReplayError`); else, unless `force_new`, an active instruction whose `claim` equals the evidence claim exactly; else a new `Instruction(id="ins-" + observation_id, claim, "active", first_seen=k)`. Append one `EvidenceRef` per quote.
   - `kind="inference"`: target = `match` if given (must be an existing `hyp-` id with status `open` or `promoted`, else `ReplayError`); else a new `Hypothesis(id="hyp-" + observation_id, claim, "open", first_seen=k, last_seen=k)`. There is no claim-equality merge for hypotheses (matching is the reviewer's job). Add the session id to `sessions` if absent, append `EvidenceRef`s, set `last_seen=k`. If status is `open` and `len(sessions) >= config.threshold` -> `promoted`, `promoted_at=k`.
   - `kind="stuck_point"`: append `StuckPoint(session_id, course of the session, concept, claim, first quote's matched_text)`. `match` on a stuck point -> `ReplayError`.
   - A `match` whose prefix does not fit the kind (`ins-` for instruction, `hyp-` for inference) -> `ReplayError`.
2. **Revocations**: target must exist and not be `dropped` or already `revoked`, else `ReplayError`. Instruction -> `revoked`, `revoked_at=k`; hypothesis -> `revoked`, `revoked_at=k`.
3. **Staleness**: every `open` hypothesis with `k - last_seen >= config.stale_after` -> `dropped`, `dropped_at=k`. Promoted hypotheses never go stale.

Output ordering: instructions and hypotheses in creation order; stuck points in
evidence order; sessions in index order. `write_profile(ws, state)` in
`storage.py` persists it atomically.

Defaults: `threshold=3`, `stale_after=5`. Example: a hypothesis last seen at
ordinal 2 is dropped at ordinal 7 (five later applied sessions without it).

### 4.6 render (`render.py`)
`render_brief(state: ProfileState, courses_md: str | None) -> str` must produce exactly:

```
# Study brief

Built by tutor-memory from {A} reviewed sessions. Follow it in every session.

## How to teach

- {claim of each active instruction, creation order}

## Observed patterns

- {claim of each promoted hypothesis, in promoted_at order, ties by creation order}

## Courses and progress

{courses_md stripped}
```

- `{A}` = number of applied sessions.
- An empty list renders the single line `- None yet.`
- The "Courses and progress" section (heading, blank line, body) is omitted entirely when `courses_md` is None or blank.
- Sections are separated by one blank line; the file ends with exactly one `\n`.

`render_profile(state: ProfileState) -> str`: Markdown with sections
`# Learner profile`, `## Instructions`, `## Hypotheses`, `## Stuck points`,
`## Sessions`; tables showing ids, status, session counts (`n/threshold`),
first/last seen, and quotes. Format beyond these headings is free.

### 4.7 sync (`sync.py`)
`sync(ws: Workspace, config: Config, *, dry_run: bool = False, drive=None) -> str`
uploads `out/brief.md` as Markdown into one Google Doc (create once, then update
the same file id kept in `<config.sync.config_dir>/state.json`). Credentials: gcloud
ADC first, else an OAuth client at `<config_dir>/client_secret.json`, scope
`drive.file` only. `drive` injects a fake Drive `files()` resource for tests.
`dry_run` returns the body without any network call. Returns the Doc URL.
Optional extra `gdrive`.

## 5. CLI (`tutormem`)

Global: `--workspace PATH`. Subcommands:

| Command | Effect |
|---|---|
| `ingest PATH --course C [--speakers gemini\|manual] [--session-id ID] [--date D] [--replace]` | 4.1 |
| `extract SID [--extractor agy\|file] [--from PATH]` | writes `observations.json` |
| `verify SID` | writes `verify.json` |
| `review SID [--mode packet\|claude]` | writes `review.md` (+ `decisions.proposed.json` in claude mode) |
| `approve SID` | copies `decisions.proposed.json` to `decisions.json` (refuses if stale) |
| `replay` | builds and writes `state/profile.json` |
| `render` | writes `out/profile.md`, `out/brief.md` |
| `status` | one line per session: index, id, stage (`ingested/extracted/verified/reviewed/applied`), pending/stale flags |
| `run [--extractor agy\|file]` | for each session: extract if missing or stale, verify if missing or stale, write the review packet if `decisions.json` is missing; then replay and render; prints pending sessions |
| `sync [--dry-run]` | 4.7 |

Exit codes: 0 ok, 1 `TutormemError` (message on stderr, no traceback), 2 usage.

## 6. Privacy rules

- Real transcripts, profiles, briefs, Doc ids and credentials never enter the repository. The repo ships only synthetic data under `examples/` and `tests/fixtures/`.
- `.gitignore` excludes `sessions/`, `runs/`, `state/`, `out/`, `*.log`, `credentials*`, `client_secret*.json`, `token.json` everywhere except under `examples/` and `tests/fixtures/`.
- Data leaving the machine: `extract --extractor agy` sends the full transcript to Google; `review --mode claude` sends the review packet to Anthropic; `sync` uploads the brief to Google Drive. Nothing else calls the network.

## 7. Config (`tutormem.toml`, read with `tomllib`)

```toml
[rules]
threshold = 3
stale_after = 5

[extract]
extractor = "agy"
model = "gemini-3.8-flash-medium"
timeout_s = 300

[review]
mode = "packet"        # or "claude"
claude_model = "sonnet"
timeout_s = 300

[sync]
doc_title = "Study brief"
config_dir = "~/.config/tutor-memory"
```

`Config.load(ws) -> Config` returns defaults when the file is missing; unknown keys -> `SchemaError`.
