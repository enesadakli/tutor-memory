"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  extractTurns,
  isContextInvalidated,
  isHostOk,
  mergeTurns,
  parseGemRoute,
  parseGemUrl,
  sidecar,
  toTranscript,
  tutorToMarkdown,
  usefulTitle,
} = require("../lib.js");

test("isContextInvalidated recognizes extension reload errors", () => {
  assert.equal(isContextInvalidated(new Error("Extension context invalidated.")), true);
  assert.equal(isContextInvalidated("Unchecked: Extension context invalidated"), true);
  assert.equal(isContextInvalidated(new Error("Other failure")), false);
  assert.equal(isContextInvalidated(null), false);
});

test("isHostOk accepts only objects whose ok property is true", () => {
  assert.equal(isHostOk({ ok: true }), true);
  assert.equal(isHostOk({ ok: false }), false);
  assert.equal(isHostOk({ ok: 1 }), false);
  assert.equal(isHostOk(null), false);
  assert.equal(isHostOk(true), false);
});

class TextNode {
  constructor(text) {
    this.nodeType = 3;
    this.textContent = text;
    this.childNodes = [];
  }
}

class Element {
  constructor(tagName, attributes = {}, children = []) {
    this.nodeType = 1;
    this.tagName = tagName.toUpperCase();
    this.attributes = attributes;
    this.childNodes = children.map((child) =>
      typeof child === "string" ? new TextNode(child) : child,
    );
  }

  get textContent() {
    return this.childNodes.map((child) => child.textContent).join("");
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  querySelectorAll(selector) {
    const parts = selector.trim().split(/\s+/);
    let scope = [this];
    for (const part of parts) {
      const matches = [];
      for (const parent of scope) {
        for (const node of descendants(parent)) {
          if (matchesSimple(node, part)) {
            matches.push(node);
          }
        }
      }
      scope = matches;
    }
    return scope;
  }
}

function descendants(element) {
  const result = [];
  for (const child of element.childNodes) {
    if (child.nodeType === 1) {
      result.push(child, ...descendants(child));
    }
  }
  return result;
}

function matchesSimple(element, selector) {
  const attribute = selector.match(/^([a-z-]+)\[([^=]+)="([^"]+)"\]$/i);
  if (attribute) {
    return (
      element.tagName.toLowerCase() === attribute[1].toLowerCase() &&
      element.getAttribute(attribute[2]) === attribute[3]
    );
  }
  if (selector.startsWith(".")) {
    return String(element.getAttribute("class") || "")
      .split(/\s+/)
      .includes(selector.slice(1));
  }
  const [tag, className] = selector.split(".");
  return (
    element.tagName.toLowerCase() === tag.toLowerCase() &&
    (!className ||
      String(element.getAttribute("class") || "")
        .split(/\s+/)
        .includes(className))
  );
}

function el(tag, attributes, ...children) {
  if (attributes === null || Array.isArray(attributes) || typeof attributes === "string") {
    children.unshift(attributes);
    attributes = {};
  }
  return new Element(tag, attributes || {}, children.flat().filter((child) => child !== null));
}

test("parseGemUrl accepts exact Gem chat URLs", () => {
  assert.deepEqual(parseGemUrl("https://gemini.google.com/gem/aB12/09ff?hl=tr"), {
    gemId: "aB12",
    chatId: "09ff",
  });
  assert.deepEqual(parseGemUrl("https://gemini.google.com/gem/a1/b2/"), {
    gemId: "a1",
    chatId: "b2",
  });
});

test("parseGemUrl rejects other origins, routes, and non-hex ids", () => {
  assert.equal(parseGemUrl("https://example.com/gem/a1/b2"), null);
  assert.equal(parseGemUrl("https://gemini.google.com/app/a1"), null);
  assert.equal(parseGemUrl("https://gemini.google.com/gem/not-hex/b2"), null);
  assert.equal(parseGemUrl("not a URL"), null);
});

test("parseGemRoute accepts Gem URLs with or without chat id and handles trailing slashes", () => {
  assert.deepEqual(parseGemRoute("https://gemini.google.com/gem/aB12/09ff?hl=tr"), {
    gemId: "aB12",
    chatId: "09ff",
  });
  assert.deepEqual(parseGemRoute("https://gemini.google.com/gem/a1/b2"), {
    gemId: "a1",
    chatId: "b2",
  });
  assert.deepEqual(parseGemRoute("https://gemini.google.com/gem/a1/b2/"), {
    gemId: "a1",
    chatId: "b2",
  });
  assert.deepEqual(parseGemRoute("https://gemini.google.com/gem/aB12"), {
    gemId: "aB12",
    chatId: null,
  });
  assert.deepEqual(parseGemRoute("https://gemini.google.com/gem/aB12/"), {
    gemId: "aB12",
    chatId: null,
  });
});

test("parseGemRoute rejects other origins, routes, and non-hex ids", () => {
  assert.equal(parseGemRoute("https://example.com/gem/a1/b2"), null);
  assert.equal(parseGemRoute("https://gemini.google.com/app/a1"), null);
  assert.equal(parseGemRoute("https://gemini.google.com/gem/not-hex"), null);
  assert.equal(parseGemRoute("https://gemini.google.com/gem/not-hex/b2"), null);
  assert.equal(parseGemRoute("https://gemini.google.com/gem/a1/not-hex"), null);
  assert.equal(parseGemRoute("not a URL"), null);
});

test("usefulTitle cleans trailing Google Gemini and ignores default titles", () => {
  assert.equal(usefulTitle("Gemini"), "");
  assert.equal(usefulTitle("Google Gemini"), "");
  assert.equal(usefulTitle("Batch Norm - Google Gemini"), "Batch Norm");
  assert.equal(usefulTitle(""), "");
  assert.equal(usefulTitle("gemini"), "");
  assert.equal(usefulTitle("google gemini"), "");
  assert.equal(usefulTitle("Linear Algebra - Google Gemini"), "Linear Algebra");
});

test("mergeTurns keeps known order, updates known data, and positions new DOM turns", () => {
  const stored = [
    { id: "b", learner: "old B", tutor: "old", complete: true },
    { id: "d", learner: "D", tutor: "D", complete: true },
    { id: "f", learner: "F", tutor: "F", complete: true },
  ];
  const fresh = [
    { id: "a", learner: "A", tutor: "A", complete: true },
    { id: "b", learner: "new B", tutor: "new", complete: true },
    { id: "c", learner: "C", tutor: "C", complete: true },
    { id: "d", learner: "D", tutor: "D", complete: true },
    { id: "e", learner: "E", tutor: "", complete: false },
  ];
  const merged = mergeTurns(stored, fresh);
  assert.deepEqual(
    merged.map((turn) => turn.id),
    ["a", "b", "c", "d", "e", "f"],
  );
  assert.equal(merged[1].learner, "new B");
  assert.equal(merged[4].complete, false);
});

test("mergeTurns appends fresh turns when no known neighbour is present", () => {
  assert.deepEqual(
    mergeTurns([{ id: "old" }], [{ id: "new-1" }, { id: "new-2" }]).map(
      (turn) => turn.id,
    ),
    ["old", "new-1", "new-2"],
  );
});

test("toTranscript includes only complete turns in manual speaker format", () => {
  const transcript = toTranscript([
    { id: "1", learner: "Türkçe öğreniyorum.", tutor: "Harika.", complete: true },
    { id: "2", learner: "Bekle", tutor: "", complete: false },
  ]);
  assert.equal(
    transcript,
    "### learner\nTürkçe öğreniyorum.\n\n### tutor\nHarika.\n",
  );
  assert.equal(toTranscript([]), "");
});

test("sidecar has contract keys and counts only complete turns", () => {
  assert.deepEqual(
    sidecar({Supported: true,
      gemId: "aa",
      chatId: "bb",
      title: "Ders",
      url: "https://gemini.google.com/gem/aa/bb",
      turns: [{ complete: true }, { complete: false }],
      now: new Date("2026-09-27T10:11:12.000Z"),
    }),
    {
      source: "gemini-gem",
      gem_id: "aa",
      chat_id: "bb",
      title: "Ders",
      url: "https://gemini.google.com/gem/aa/bb",
      turns: 1,
      updated_at: "2026-09-27T10:11:12.000Z",
    },
  );
});

test("tutorToMarkdown converts supported structure and TeX without rendered duplication", () => {
  const markdown = el(
    "div",
    { class: "markdown" },
    el("h2", {}, "Konu"),
    el(
      "p",
      {},
      "Bu ",
      el("strong", {}, "önemli"),
      " ve ",
      el("em", {}, "eğik"),
      ": ",
      el(
        "span",
        { class: "katex" },
        el("span", { class: "katex-html" }, "rendered duplicate"),
        el("annotation", { encoding: "application/x-tex" }, "x^2"),
      ),
    ),
    el("ul", {}, el("li", {}, "Bir"), el("li", {}, "İki")),
    el("ol", {}, el("li", {}, "İlk")),
    el("p", {}, "Use ", el("code", {}, "x = 1"), "."),
    el("pre", {}, el("code", {}, "const x = 1;\n")),
    el(
      "div",
      { class: "katex-display" },
      el("span", { class: "katex" }, el("annotation", { encoding: "application/x-tex" }, "y=mx+b")),
    ),
    el(
      "table",
      {},
      el("thead", {}, el("tr", {}, el("th", {}, "A"), el("th", {}, "B"))),
      el("tbody", {}, el("tr", {}, el("td", {}, "1"), el("td", {}, "2"))),
    ),
  );

  assert.equal(
    tutorToMarkdown(markdown),
    [
      "## Konu",
      "",
      "Bu **önemli** ve *eğik*: $x^2$",
      "",
      "- Bir\n- İki",
      "",
      "1. İlk",
      "",
      "Use `x = 1`.",
      "",
      "```\nconst x = 1;\n```",
      "",
      "$$y=mx+b$$\n",
      "| A | B |\n| --- | --- |\n| 1 | 2 |",
    ].join("\n"),
  );
  assert.doesNotMatch(tutorToMarkdown(markdown), /rendered duplicate/);
});

test("tutorToMarkdown converts inline data-math without rendered duplication", () => {
  const markdown = el(
    "p",
    {},
    "Slope: ",
    el("span", { class: "math-inline", "data-math": "\\beta_1" }, "rendered KaTeX text"),
    ".",
  );

  assert.equal(tutorToMarkdown(markdown), "Slope: $\\beta_1$.");
  assert.doesNotMatch(tutorToMarkdown(markdown), /rendered KaTeX text/);
});

test("tutorToMarkdown converts block data-math without rendered duplication", () => {
  const markdown = el(
    "div",
    {},
    el("p", {}, "Equation:"),
    el("div", { class: "math-block", "data-math": "y=mx+b" }, "rendered KaTeX text"),
  );

  assert.equal(tutorToMarkdown(markdown), "Equation:\n\n$$y=mx+b$$");
  assert.doesNotMatch(tutorToMarkdown(markdown), /rendered KaTeX text/);
});

test("extractTurns uses query-text lines, preserves Unicode, and marks streaming tutor incomplete", () => {
  const root = el(
    "main",
    {},
    el(
      "section",
      { class: "conversation-container", id: "a1" },
      el(
        "user-query",
        {},
        el("div", { class: "query-text" }, "Siz şunu dediniz: duplicated"),
        el("p", { class: "query-text-line" }, "İzmir'deyim."),
        el("p", { class: "query-text-line" }, "Çözümü açıkla."),
      ),
      el(
        "model-response",
        {},
        el("message-content", {}, el("div", { class: "markdown" }, el("p", {}, "Elbette."))),
      ),
    ),
    el(
      "section",
      { class: "conversation-container", id: "b2" },
      el("user-query", {}, el("p", { class: "query-text-line" }, "Devam")),
      el("model-response", {}, el("message-content", {}, el("div", { class: "markdown" }))),
    ),
  );

  assert.deepEqual(extractTurns(root), [
    {
      id: "a1",
      learner: "İzmir'deyim.\nÇözümü açıkla.",
      tutor: "Elbette.",
      complete: true,
    },
    { id: "b2", learner: "Devam", tutor: "", complete: false },
  ]);
});
