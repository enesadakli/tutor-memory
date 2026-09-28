# tutor-memory capture

This Chrome extension automatically saves chats from explicitly allowed Gemini Gems.

## Install

1. Open `chrome://extensions` in Chrome.
2. Enable **Developer mode**.
3. Choose **Load unpacked** and select this `extension/` directory.
4. Open the extension's options and add the Gem id, one per line. The Gem id is the hexadecimal part immediately after `/gem/` in a Gem chat URL.
5. Copy the extension id shown on `chrome://extensions` and install the local host:

   ```bash
   tutormem install-capture-host --extension-id <id>
   ```

An empty allowlist captures nothing. The native host writes captures directly to the configured automatic-mode inbox without showing a browser download. If the host is missing or returns an error, the extension falls back to `chrome.downloads` and saves `gemini-<chatId>.md` and `gemini-<chatId>.json` under `~/Downloads/tutor-memory/inbox/`.

## Privacy and compatibility

Only chats belonging to allowed Gem ids are captured. The extension makes no network requests: transcript data stays on the machine and is passed only to the local native host (or the local Downloads fallback).

Gemini UI changes can break capture. All selectors are defined together in `SELECTORS` at the top of `lib.js`: `turn`, `learner`, `learnerLines`, `tutor`, `mathInline`, `mathBlock`, `mathAttr`, `katex`, `katexDisplay`, and `katexAnnotation`. Update that object if Gemini changes its conversation, learner-text, tutor-content, or math markup.
