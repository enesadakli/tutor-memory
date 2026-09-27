"use strict";

const input = document.querySelector("#gem-ids");
const status = document.querySelector("#status");

chrome.storage.sync.get({ gemIds: [] }, ({ gemIds }) => {
  input.value = (Array.isArray(gemIds) ? gemIds : []).join("\n");
});

document.querySelector("#save").addEventListener("click", () => {
  const lines = input.value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const invalid = lines.filter((line) => !/^[0-9a-f]+$/i.test(line));
  if (invalid.length) {
    status.textContent = `Not saved: invalid id${invalid.length === 1 ? "" : "s"}.`;
    return;
  }
  const gemIds = [...new Set(lines.map((line) => line.toLowerCase()))];
  chrome.storage.sync.set({ gemIds }, () => {
    input.value = gemIds.join("\n");
    status.textContent = "Saved.";
  });
});
