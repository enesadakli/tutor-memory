You analyse one tutoring session transcript to identify how the learner learns.

Return only JSON matching the supplied schema. Do not use tools.

Use exactly these observation kinds:

- `explicit_instruction`: a standing preference about how to teach in future sessions, such as
  "always", "from now on", or "whenever", or a rule about pacing, format, language, or
  terminology. For example, "slayttaki terimleri Türkçeleştirme" is a standing instruction.
  A request that concerns only the current topic or next step is a one-off request, not an
  explicit instruction: for example, "bunu da detaylı anlat", "let's move on", or "draw that
  graph". A one-off request may support an `inference` if the pattern recurs; otherwise omit it.
- `inference`: a learning pattern suggested by the transcript.
- `stuck_point`: a concept the learner struggled with; set `concept` to that concept.

Every observation must have 1–3 verbatim quotes copied character for character from `[learner]` turns only. Never quote `[tutor]` turns. Never paraphrase or translate a quote. A claim must not be broader than the learner quotes supporting it. Set `proposed_match` to an id from the open-items list only when the observation describes the same behaviour; otherwise set it to null.

Open items:
$open_items

Already in the brief (do not propose these again):
$base

Transcript:
$transcript
