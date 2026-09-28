(function () {
  "use strict";

  const capture = globalThis.TutorMemoryCapture;
  const SETTLE_MS = 5000;
  let observer = null;
  let timer = null;
  let locationTimer = null;
  let generation = 0;
  let lastHref = location.href;
  let lastMutationAt = 0;
  let lastTurnChangeAt = 0;
  let lastTurnSnapshot = "";
  let saving = false;
  let saveAgain = false;
  let pendingNewChat = null;
  let contextStopped = false;

  function contextAvailable() {
    return (
      !contextStopped &&
      typeof chrome !== "undefined" &&
      chrome.runtime &&
      chrome.runtime.id !== undefined
    );
  }

  function invalidate() {
    contextStopped = true;
    stop();
    clearInterval(locationTimer);
    locationTimer = null;
  }

  function handleContextError(error) {
    if (capture.isContextInvalidated(error)) {
      invalidate();
      return true;
    }
    return false;
  }

  function storageGet(area, defaults) {
    if (!contextAvailable()) {
      invalidate();
      return Promise.resolve(defaults);
    }
    return new Promise((resolve, reject) => {
      try {
        area.get(defaults, resolve);
      } catch (error) {
        if (handleContextError(error)) {
          resolve(defaults);
        } else {
          reject(error);
        }
      }
    });
  }

  function storageSet(area, values) {
    if (!contextAvailable()) {
      invalidate();
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      try {
        area.set(values, resolve);
      } catch (error) {
        if (handleContextError(error)) {
          resolve();
        } else {
          reject(error);
        }
      }
    });
  }

  function storageRemove(area, keys) {
    if (!contextAvailable()) {
      invalidate();
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      try {
        area.remove(keys, resolve);
      } catch (error) {
        if (handleContextError(error)) {
          resolve();
        } else {
          reject(error);
        }
      }
    });
  }

  async function pruneStoredChats() {
    const values = await storageGet(chrome.storage.local, null);
    const cutoff = Date.now() - 14 * 24 * 60 * 60 * 1000;
    const expired = Object.entries(values || {})
      .filter(([key, value]) => {
        if (!key.startsWith("chat:")) {
          return false;
        }
        const savedAt = value && typeof value === "object" ? Date.parse(value.savedAt) : NaN;
        return !Number.isFinite(savedAt) || savedAt < cutoff;
      })
      .map(([key]) => key);
    if (expired.length) {
      await storageRemove(chrome.storage.local, expired);
    }
  }

  function sendMessage(message) {
    if (!contextAvailable()) {
      invalidate();
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      try {
        chrome.runtime.sendMessage(message, resolve);
      } catch (error) {
        if (handleContextError(error)) {
          resolve();
        } else {
          reject(error);
        }
      }
    });
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
    if (!contextAvailable()) {
      invalidate();
      return;
    }
    clearTimeout(timer);
    timer = setTimeout(() => settled(expectedGeneration), delay);
  }

  async function settled(expectedGeneration) {
    if (!contextAvailable()) {
      invalidate();
      return;
    }
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
    const activeKey = `active:${route.chatId}`;
    const activeValues = await storageGet(chrome.storage.local, { [activeKey]: false });
    if (expectedGeneration !== generation || !activeValues[activeKey]) {
      return;
    }
    const key = `chat:${route.chatId}`;
    const values = await storageGet(chrome.storage.local, { [key]: [] });
    if (expectedGeneration !== generation) {
      return;
    }
    const storedValue = values[key];
    const stored = Array.isArray(storedValue)
      ? storedValue
      : storedValue && Array.isArray(storedValue.turns)
        ? storedValue.turns
        : [];
    const fresh = capture.extractTurns(document).filter((turn) => turn.id);
    const merged = capture.mergeTurns(stored, fresh);
    const before = capture.toTranscriptJson(stored);
    const transcript = capture.toTranscriptJson(merged);
    await storageSet(chrome.storage.local, {
      [key]: { turns: merged, savedAt: new Date().toISOString() },
    });
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
        title: capture.usefulTitle(document.title),
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
    if (!contextAvailable()) {
      invalidate();
      return;
    }
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
    if (
      pendingNewChat &&
      pendingNewChat.gemId.toLowerCase() === route.gemId.toLowerCase() &&
      Date.now() - pendingNewChat.at < 120000
    ) {
      pendingNewChat = null;
      await storageSet(chrome.storage.local, { [`active:${route.chatId}`]: true });
      if (expectedGeneration !== generation) {
        return;
      }
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

  async function onSendEvent() {
    if (!contextAvailable()) {
      invalidate();
      return;
    }
    const route = capture.parseGemRoute(location.href);
    if (!route) {
      return;
    }
    const values = await storageGet(chrome.storage.sync, { gemIds: [] });
    const allowed = new Set(
      (Array.isArray(values.gemIds) ? values.gemIds : []).map((id) => String(id).toLowerCase()),
    );
    if (!allowed.has(route.gemId.toLowerCase())) {
      return;
    }
    if (route.chatId) {
      await storageSet(chrome.storage.local, { [`active:${route.chatId}`]: true });
    } else {
      pendingNewChat = { gemId: route.gemId, at: Date.now() };
    }
  }

  document.addEventListener(
    "keydown",
    (event) => {
      if (
        event.key === "Enter" &&
        !event.shiftKey &&
        !event.altKey &&
        !event.ctrlKey &&
        !event.metaKey
      ) {
        const target =
          event.target && event.target.nodeType === 1
            ? event.target
            : event.target && event.target.parentElement;
        if (
          target &&
          target.closest &&
          target.closest('rich-textarea, [contenteditable="true"], textarea')
        ) {
          onSendEvent();
        }
      }
    },
    true,
  );

  document.addEventListener(
    "click",
    (event) => {
      const target =
        event.target && event.target.nodeType === 1
            ? event.target
            : event.target && event.target.parentElement;
      const button = target && target.closest && target.closest("button");
      if (button) {
        const ariaLabel = button.getAttribute("aria-label") || "";
        const hasSendClass =
          button.classList && button.classList.contains("send-button");
        if (/send|gönder/i.test(ariaLabel) || hasSendClass) {
          onSendEvent();
        }
      }
    },
    true,
  );

  if (!contextAvailable()) {
    invalidate();
    return;
  }

  locationTimer = setInterval(() => {
    if (!contextAvailable()) {
      invalidate();
      return;
    }
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

  pruneStoredChats().finally(startForLocation);
})();
