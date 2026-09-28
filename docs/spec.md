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
<ws>/base.md                          optional hand-written tutor brief
<ws>/courses.md                       optional, hand-written, copied into the brief verbatim
<ws>/sessions/<session_id>.json       canonical Session
<ws>/runs/<session_id>/observations.json   ExtractResult
<ws>/runs/<session_id>/verify.json         VerifyResult
<ws>/runs/<session_id>/review.md           review packet for the reviewer
<ws>/runs/<session_id>/decisions.proposed.json   DecisionFile proposed by a model reviewer
<ws>/runs/<session_id>/decisions.json            approved DecisionFile (the only one replay reads)
<ws>/runs/<session_id>/progress.json             SHA-bound automatic progress extraction
<ws>/state/profile.json               ProfileState (derived, rewritten from scratch by replay)
<ws>/state/progress.json              maintained per-course continuation state
<ws>/out/profile.md                   rendered profile
<ws>/out/brief.md                     rendered brief
<ws>/changelog.md                     append-only automatic-mode changes and undo commands
<ws>/auto.log                         per-session automatic-mode failures
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
- `speakers="gemini"`: Gemini chat export. A learner turn starts at the marker `User prompt:` (optionally wrapped in `*...*` italics); the tutor turn starts at the following `Response:`. Text before the first marker is dropped. Export formatting on the marker line (the wrapping `*`) is removed. In learner turns, italic line-break artifacts (`*` optional whitespace, optional newline, optional whitespace, `*`) are replaced by a single space. Turn text is otherwise kept as is, except: strip each turn, collapse runs of 3+ newlines to 2. If no `User prompt:` marker is found -> `ParseError`.
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
- `AgyExtractor(model, timeout_s)`: runs the Antigravity CLI headless. Prompt = `prompts/extract.md` template filled with the canonical transcript (turns labeled `[learner]`/`[tutor]` with their turn index), the open items (id + claim), and the stripped contents of `base.md` under "Already in the brief (do not propose these again)". An absent or blank base is rendered as `(none)`. Transcript text must **not** be passed as a command-line argument (it would show in `ps`); pass it on stdin or via a temp file (mode 0600, deleted afterwards). Use `--json-schema` with the observation-list schema, `--sandbox`, `--print-timeout <timeout_s>s`. Parse the first JSON value in stdout; invalid items -> `dropped`; non-zero exit, timeout, or no JSON -> `ExtractorError` with stderr truncated to 500 chars.
- `make_extractor(name: Literal["agy", "file"], config: Config, *, path: Path | None = None) -> Extractor`.

### 4.4 review (`review.py`)
- `write_packet(session: Session, result: VerifyResult, profile: ProfileState) -> str` — Markdown: every verified observation with id, kind, claim, proposed_match (and that item's claim), each quote with ±200 chars of surrounding turn text; then rejected observations with reasons; then the open/active items list; then instructions for the reviewer (the evidence rule, and the rule that a claim may never be broader than its quotes).
- `skeleton(result: VerifyResult) -> DecisionFile` — no decisions.
- `ClaudeReviewer(model: str, timeout_s: int).propose(packet: str, result: VerifyResult) -> DecisionFile` — runs `claude -p --output-format json --model <model> --tools "" --no-session-persistence --disable-slash-commands --strict-mcp-config --mcp-config '{"mcpServers":{}}' --setting-sources "" --system-prompt <minimal reviewer prompt>` with the packet on **stdin** and a fresh temporary directory as cwd. Every decision gets `reviewer="claude"`. Invalid output -> `ReviewerError`.
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
- "session" is singular when `{A} == 1`; the existing plural output is unchanged otherwise.
- When `<ws>/base.md` exists and is non-blank, omit the title, generated summary, and courses
  section. Output the stripped base, one blank line, then `## Learned from sessions`, with
  `### How to teach` and `### Observed patterns` subsections using the same filtering and ordering
  rules above. Empty lists use `- None yet.` and the file has one trailing newline.

`render_profile(state: ProfileState) -> str`: Markdown with sections
`# Learner profile`, `## Instructions`, `## Hypotheses`, `## Stuck points`,
`## Sessions`; tables showing ids, status, session counts (`n/threshold`),
first/last seen, and quotes. The instructions table shows its quote evidence count rather than
`n/threshold`; hypotheses continue to show distinct session count as `n/threshold`. Format beyond
these headings is free.

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
| `progress-init [--force]` | extracts the configured progress section from `base.md`, preserving `base.md.bak` |
| `progress-show` | prints the maintained progress section |
| `run [--extractor agy\|file] [--observations-dir DIR]` | for each session: extract if missing or stale (file extractor reads `DIR/<sid>.json`), verify if missing or stale, write the review packet if `decisions.json` is missing; then replay and render; prints pending sessions |
| `sync [--dry-run]` | 4.7 |
| `auto [--inbox DIR] [--idle-minutes N] [--no-sync] [--dry-run]` | section 8 |
| `revoke ID [--reason TEXT] [--no-sync]` | section 8.3 |
| `install-agent [--interval-minutes 15] [--load]` | section 8.4 |
| `uninstall-agent` | section 8.4 |
| `capture-host [CALLER_ORIGIN ...]` | runs the Chrome native messaging host (section 8.5) |
| `install-capture-host --extension-id ID [--browser chrome]` | installs the host wrapper and Chrome manifest (section 8.5) |
| `uninstall-capture-host` | removes the host wrapper and Chrome manifest (section 8.5) |

Exit codes: 0 ok, 1 `TutormemError` (message on stderr, no traceback), 2 usage.

## 6. Privacy rules

- Real transcripts, profiles, briefs, Doc ids and credentials never enter the repository. The repo ships only synthetic data under `examples/` and `tests/fixtures/`.
- `.gitignore` excludes `sessions/`, `runs/`, `state/`, `out/`, `*.log`, `credentials*`, `client_secret*.json`, `token.json` everywhere except under `examples/` and `tests/fixtures/`.
- Data leaving the machine: `extract --extractor agy` sends the full transcript to Google; `review --mode claude` sends the review packet to Anthropic; `sync` uploads the brief to Google Drive. Nothing else calls the network.
- Automatic mode invokes those same three operations without interactive confirmation when its
  configured gates allow them.

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

[auto]
inbox = "~/Downloads/tutor-memory/inbox"
idle_minutes = 20
approve = "claude"       # or "none"
sync = true
notify = true
default_course = "General"
changelog_copy = ""

[progress]
enabled = true
heading = "## Courses and progress"
last_label = "Last session"
next_label = "Next"
language = "English"
# model = "gemini-3.8-flash-medium"  # defaults to extract.model
```

`Config.load(ws) -> Config` returns defaults when the file is missing; unknown keys -> `SchemaError`.

## 8. Automatic mode

### 8.1 Capture inbox and orchestration

The capture extension writes `gemini-<chatId>.md` in manual-speaker format and then writes the
matching JSON sidecar:

```json
{"source":"gemini-gem","gem_id":"...","chat_id":"...","title":"...","url":"...","turns":2,"updated_at":"..."}
```

`tutormem auto` creates `<ws>/.auto.lock` exclusively. A lock at most 60 minutes old makes the
command print `another run in progress` and exit 0. An older lock is replaced. A lock acquired by
the current process is always removed in `finally`.

The command scans `gemini-*.md` files with sidecars. Both mtimes must be at least `idle_minutes`
old (CLI override, else `auto.idle_minutes`); files without a learner turn are skipped. The
session id is `gem-` plus lowercased `chat_id`, course is the non-blank sidecar title or
`auto.default_course`, and date is the local calendar date of `updated_at`.

An unseen id is ingested with manual speakers. For an existing id, identical content is skipped
when its approved decision is complete (or its proposal exists in `approve = "none"` mode), while
an incomplete run is retried. Changed content is ingested with replacement and keeps its index.
Content already owned by another session id is skipped and logged.

Each new or changed session is extracted with the configured extractor and verified. A retried
session reuses current `observations.json` and `verify.json` artifacts and goes directly to review;
missing or stale artifacts cause extraction and verification to run again. Every session is then
packetized and reviewed by Claude. With `auto.approve = "claude"` the complete proposal is copied
to `decisions.json` and immediately participates in replay. With `"none"`, processing stops after
`decisions.proposed.json`, preserving the human approval gate.

After changes, the command replays and renders once more, syncs unless `--no-sync` or
`auto.sync = false`, appends the changelog, and notifies. `--dry-run` only prints sessions that
would be processed and leaves no persistent changes. Extractor/reviewer failures and timeouts are
isolated per session: they are appended to `<ws>/auto.log`, notified, do not stop other sessions,
and remain retryable because no current complete decision marks them done. The command exits 0
after such isolated failures and 1 after a global failure.

### 8.2 Diff, changelog, and notification

`profile_diff(before, after) -> list[str]` is pure and deterministic. It reports new active
instructions, newly promoted hypotheses, new open hypotheses, increased hypothesis session count
as `n/threshold`, dropped hypotheses, and revoked items. Every line ends with the item id in
parentheses, for example `New instruction: Go slide by slide. (ins-gem-abc:2)`.

Each change appends a `## <local ISO timestamp>` block to `<ws>/changelog.md`, followed by the
processed session ids, diff lines, and `Undo: tutormem revoke <id>` for changed items that remain
revocable. If `auto.changelog_copy` is non-empty, the identical block is appended there.

When `auto.notify = true`, macOS runs `osascript -e` with title `tutor-memory` and text containing
the first three diff lines plus `…` when more exist. Quotes and backslashes are escaped. Other
platforms silently skip notifications. Per-session failure notifications contain the session and
error. `<ws>/auto-notified.json` maps session ids to their last notified error text: an identical
failure is not notified again, a changed error is notified, and success removes the entry. Every
failure is still appended to `auto.log`. The notifier is injectable for tests.

### 8.3 Revocation

`tutormem revoke ID [--reason TEXT] [--no-sync]` accepts only an active instruction or an open or
promoted hypothesis. Unknown, dropped, or already revoked ids fail before any write. It appends a
`Revocation(target_id=ID, reviewer="human", reason=TEXT)` to the current `decisions.json` of the
applied session with the greatest ingest index, validates replay in memory, then writes the
decision, replays, renders, syncs unless `--no-sync`, appends a changelog block, and notifies.

### 8.4 launchd agent

`install-agent` writes `~/Library/LaunchAgents/com.tutormem.auto.plist` with the absolute
`tutormem` executable (`shutil.which`, else absolute `sys.argv[0]`), arguments
`--workspace <absolute ws> auto`, `StartInterval = interval_minutes * 60`, `RunAtLoad = true`, and
stdout/stderr at `<ws>/auto.stdout.log` and `<ws>/auto.stderr.log`. Its environment includes
`USER` and `LOGNAME` from `getpass.getuser()`, `HOME` set to the expanded user home, and PATH
`~/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin` with `~` expanded. It prints the
plist path and `launchctl bootstrap gui/<uid> <plist>`. With `--load`, it runs `bootout` (failure
ignored) and then `bootstrap`. `uninstall-agent` runs `bootout` and removes the plist. The command
runner is injectable for tests.

### 8.5 Native messaging capture host

The extension first sends each save to the Chrome native messaging host
`com.tutormem.capture`. Messages use Chrome's framing: a 4-byte little-endian unsigned payload
length followed by UTF-8 JSON, with an 8 MiB maximum. A save request contains `type = "save"`, a
hexadecimal `chatId`, a non-empty transcript string, and an object sidecar. The host atomically
writes `gemini-<chatId>.md` followed by `gemini-<chatId>.json` into the expanded
`Config.load(ws).auto.inbox`, and replies with `{"ok": true}`. Invalid input and I/O failures reply
with `{"ok": false, "error": "..."}` and are logged to `<ws>/capture-host.log`; stdout contains
only framed replies. The host reads until EOF. Its workspace is `$TUTORMEM_WORKSPACE`, otherwise
the normal default workspace. Chrome's optional caller-origin positional argument is ignored.

`install-capture-host` validates the extension id as exactly 32 lowercase characters from `a` to
`p`. It writes executable `<ws>/capture-host.sh`, exporting the absolute workspace and executing
the absolute `tutormem` executable with `capture-host "$@"`. It also writes
`~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.tutormem.capture.json` with
the wrapper path and the single allowed origin `chrome-extension://<ID>/`, then prints both paths.
`uninstall-capture-host` removes both files. Chrome is the only supported browser.

If native messaging throws or returns anything other than an object whose `ok` property is
exactly `true`, the extension logs one warning per service-worker lifetime and uses its existing
two-file `chrome.downloads` path. Per-chat serialization applies to both transports.

### 8.6 Course progress

`tutormem progress-init` finds the exact configured level-two heading in `base.md`. It moves that
section into `state/progress.json`, keeps non-empty preamble lines and each `###` course block's
non-empty static lines verbatim, writes the remaining base, and first preserves the original as
`base.md.bak`. Existing progress state is refused unless `--force` is supplied. `progress-show`
renders the maintained section with the configured heading and labels.

When progress state exists, base-mode rendering places the progress section between the stripped
base and `## Learned from sessions`. Without progress state, rendering is byte-for-byte unchanged.

For every new or changed automatic-mode capture, the configured Gemini Flash model receives the
canonical transcript on stdin and chooses one of the exact known course names or null. It also
returns a concrete summary of what was covered and where the next session should begin, in the
configured language. The result is stored in `runs/<session_id>/progress.json` with the session id
and content SHA. A retry caused only by review failure reuses that artifact; a grown session has a
new SHA and is extracted again.

The extracted course takes precedence over the sidecar title and `auto.default_course` for ingest.
After the session is approved and replay succeeds, its course entry is updated only when its ingest
index is at least the stored index. Unknown courses leave progress unchanged. Progress extraction
failure is logged and does not block observation extraction, review, replay, or the fallback course.
Successful updates add `Progress: <course> — <covered> → next: <next>` to the changelog and the
normal three-line notification budget.
