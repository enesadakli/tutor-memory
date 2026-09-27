# tutor-memory capture

This Chrome extension automatically saves chats from explicitly allowed Gemini Gems.

## Install

1. Open `chrome://extensions` in Chrome.
2. Enable **Developer mode**.
3. Choose **Load unpacked** and select this `extension/` directory.
4. Open the extension's options and add the Gem id, one per line. The Gem id is the hexadecimal part immediately after `/gem/` in a Gem chat URL.

An empty allowlist captures nothing. Captured files appear in `~/Downloads/tutor-memory/inbox/` as `gemini-<chatId>.md` and `gemini-<chatId>.json`.

## Privacy and compatibility

Only chats belonging to allowed Gem ids are captured. The extension makes no network requests: transcript data stays on the machine and the generated files stay under Downloads.

Gemini UI changes can break capture. All selectors are defined together in `SELECTORS` at the top of `lib.js`: `turn`, `learner`, `learnerLines`, `tutor`, `mathInline`, `mathBlock`, `mathAttr`, `katex`, `katexDisplay`, and `katexAnnotation`. Update that object if Gemini changes its conversation, learner-text, tutor-content, or math markup.
