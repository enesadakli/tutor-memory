"use strict";

importScripts("lib.js");

const queues = new Map();
let downloadsUiConfigured = false;
let nativeHostWarningLogged = false;
const { isHostOk } = globalThis.TutorMemoryCapture;

function utf8DataUrl(mimeType, text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return `data:${mimeType};charset=utf-8;base64,${btoa(binary)}`;
}

async function configureDownloadsUi() {
  if (downloadsUiConfigured) {
    return;
  }
  downloadsUiConfigured = true;
  try {
    await chrome.downloads.setUiOptions({ enabled: false });
  } catch (_error) {
    // Older Chrome versions may reject this optional UI preference.
  }
}

async function saveFiles(message) {
  await configureDownloadsUi();
  const base = `tutor-memory/inbox/gemini-${message.chatId}`;
  await chrome.downloads.download({
    url: utf8DataUrl("text/markdown", message.transcript),
    filename: `${base}.md`,
    conflictAction: "overwrite",
    saveAs: false,
  });
  await chrome.downloads.download({
    url: utf8DataUrl("application/json", `${JSON.stringify(message.sidecar, null, 2)}\n`),
    filename: `${base}.json`,
    conflictAction: "overwrite",
    saveAs: false,
  });
}

async function save(message) {
  try {
    const response = await chrome.runtime.sendNativeMessage("com.tutormem.capture", {
      type: "save",
      chatId: message.chatId,
      transcript: message.transcript,
      sidecar: message.sidecar,
    });
    if (isHostOk(response)) {
      return;
    }
  } catch (_error) {
    // The download fallback below also covers a host that is not installed.
  }
  if (!nativeHostWarningLogged) {
    nativeHostWarningLogged = true;
    console.warn("tutor-memory native capture host unavailable; using downloads fallback");
  }
  await saveFiles(message);
}

chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "save" || !/^[0-9a-f]+$/i.test(message.chatId || "")) {
    return;
  }
  const previous = queues.get(message.chatId) || Promise.resolve();
  const next = previous.catch(() => undefined).then(() => save(message));
  queues.set(message.chatId, next);
  const finish = () => {
    if (queues.get(message.chatId) === next) {
      queues.delete(message.chatId);
    }
  };
  next.then(finish, finish);
});
