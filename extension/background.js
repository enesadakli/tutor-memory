"use strict";

importScripts("lib.js");

const queues = new Map();
const MAX_MESSAGE_BYTES = 8 * 1024 * 1024;
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

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
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
    url: utf8DataUrl("application/json", message.transcript),
    filename: `${base}.turns.json`,
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
  const transcriptBytes = new TextEncoder().encode(message.transcript);
  if (transcriptBytes.length > MAX_MESSAGE_BYTES) {
    console.error("tutor-memory capture exceeds 8 MiB; capture was not saved");
    return;
  }
  const enriched = {
    ...message,
    sidecar: {
      ...message.sidecar,
      transcript_sha256: await sha256Hex(message.transcript),
    },
  };
  try {
    const response = await chrome.runtime.sendNativeMessage("com.tutormem.capture", {
      type: "save",
      chatId: enriched.chatId,
      transcript: enriched.transcript,
      sidecar: enriched.sidecar,
    });
    if (isHostOk(response)) {
      return;
    }
    if (response && String(response.error || "").includes("exceeds 8 MiB")) {
      console.error("tutor-memory capture exceeds 8 MiB; capture was not saved");
      return;
    }
  } catch (_error) {
    // The download fallback below also covers a host that is not installed.
  }
  if (!nativeHostWarningLogged) {
    nativeHostWarningLogged = true;
    console.warn("tutor-memory native capture host unavailable; using downloads fallback");
  }
  await saveFiles(enriched);
}

chrome.runtime.onMessage.addListener((message) => {
  if (
    !message ||
    message.type !== "save" ||
    !/^[0-9a-f]{1,64}$/i.test(message.chatId || "")
  ) {
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
