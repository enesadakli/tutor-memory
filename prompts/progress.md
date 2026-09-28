You identify course progress from one tutoring session transcript.

Return only JSON matching the supplied schema. Do not use tools.

Choose `course` from the exact course names below, or return null when none clearly fits. Do not
rename, translate, or invent a course. Write `covered` and `next` in $language.

- `covered`: what this session actually went through, using concrete units, slides, videos, or
  topics from the transcript; maximum 200 characters.
- `next`: the concrete starting point for the next session; maximum 160 characters.

Do not invent units, slides, videos, or topics that are not present in the transcript.

Course names:
$courses

Transcript:
$transcript
