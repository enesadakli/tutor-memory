(function () {
  "use strict";

  const capture = globalThis.TutorMemoryCapture;
  const SETTLE_MS = 5000;
  let observer = null;
  let timer = null;
  let generation = 0;
  let lastHref = location.href;
  let lastMutationAt = 0;
  let lastTurnChangeAt = 0;
  let lastTurnSnapshot = "";
  let saving = false;
  let saveAgain = false;

  function storageGet(area, defaults) {
    return new Promise((resolve) => area.get(defaults, resolve));
  }

  function storageSet(area, values) {
    return new Promise((resolve) => area.set(values, resolve));
  }

  function sendMessage(message) {
    return new Promise((resolve) => chrome.runtime.sendMessage(message, resolve));
  }

  function cleanTitle() {
    return document.title.replace(/\s+- Google Gemini$/, "").trim();
  }

  function currentSnapshot() {
    return JSON.stringify(capture.extractTurns(document));
  }

  function noteMutation() {
    const now = Date.now();
    lastMutationAt = now;
    const snapshot = currentSnapshot();
    if (snapshot !== lastTurnSnapshot) {
      lastTurnSnapshot = snapshot;
      lastTurnChangeAt = now;
    }
    schedule(generation, SETTLE_MS);
  }

  function schedule(expectedGeneration, delay) {
    clearTimeout(timer);
    timer = setTimeout(() => settled(expectedGeneration), delay);
  }

  async function settled(expectedGeneration) {
    if (expectedGeneration !== generation) {
      return;
    }
    const elapsed = Date.now() - Math.max(lastMutationAt, lastTurnChangeAt);
    if (elapsed < SETTLE_MS) {
      schedule(expectedGeneration, SETTLE_MS - elapsed);
      return;
    }
    if (saving) {
      saveAgain = true;
      return;
    }
    saving = true;
    try {
      await saveCurrent(expectedGeneration);
    } finally {
      saving = false;
      if (saveAgain) {
        saveAgain = false;
        schedule(expectedGeneration, SETTLE_MS);
      }
    }
  }

  async function saveCurrent(expectedGeneration) {
    const route = capture.parseGemUrl(location.href);
    if (!route || expectedGeneration !== generation) {
      return;
    }
    const key = `chat:${route.chatId}`;
    const values = await storageGet(chrome.storage.local, { [key]: [] });
    if (expectedGeneration !== generation) {
      return;
    }
    const stored = Array.isArray(values[key]) ? values[key] : [];
    const fresh = capture.extractTurns(document).filter((turn) => turn.id);
    const merged = capture.mergeTurns(stored, fresh);
    const before = capture.toTranscript(stored);
    const transcript = capture.toTranscript(merged);
    await storageSet(chrome.storage.local, { [key]: merged });
    if (transcript === before || !transcript || expectedGeneration !== generation) {
      return;
    }
    await sendMessage({
      type: "save",
      chatId: route.chatId,
      transcript,
      sidecar: capture.sidecar({
        gemId: route.gemId,
        chatId: route.chatId,
        title: cleanTitle(),
        url: location.href,
        turns: merged,
        now: new Date(),
      }),
    });
  }

  function stop() {
    generation += 1;
    clearTimeout(timer);
    timer = null;
    if (observer) {
      observer.disconnect();
      observer = null;
    }
  }

  async function startForLocation() {
    stop();
    const expectedGeneration = generation;
    const route = capture.parseGemUrl(location.href);
    if (!route) {
      return;
    }
    const values = await storageGet(chrome.storage.sync, { gemIds: [] });
    if (expectedGeneration !== generation) {
      return;
    }
    const allowed = new Set(
      (Array.isArray(values.gemIds) ? values.gemIds : []).map((id) => String(id).toLowerCase()),
    );
    if (!allowed.has(route.gemId.toLowerCase())) {
      return;
    }
    const now = Date.now();
    lastMutationAt = now;
    lastTurnChangeAt = now;
    lastTurnSnapshot = currentSnapshot();
    observer = new MutationObserver(noteMutation);
    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
      characterData: true,
    });
    schedule(expectedGeneration, SETTLE_MS);
  }

  setInterval(() => {
    if (location.href !== lastHref) {
      lastHref = location.href;
      startForLocation();
    }
  }, 1000);

  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName === "sync" && changes.gemIds) {
      startForLocation();
    }
  });

  startForLocation();
})();
