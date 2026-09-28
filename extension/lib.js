(function (root) {
  "use strict";

  const SELECTORS = Object.freeze({
    turn: ".conversation-container",
    learner: "user-query",
    learnerLines: "p.query-text-line",
    tutor: "model-response message-content .markdown",
    mathInline: ".math-inline",
    mathBlock: ".math-block",
    mathAttr: "data-math",
    katex: ".katex",
    katexDisplay: ".katex-display",
    katexAnnotation: 'annotation[encoding="application/x-tex"]',
  });

  function parseGemUrl(url) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (_error) {
      return null;
    }
    if (parsed.protocol !== "https:" || parsed.hostname !== "gemini.google.com") {
      return null;
    }
    const match = parsed.pathname.match(/^\/gem\/([0-9a-f]+)\/([0-9a-f]+)\/?$/i);
    return match ? { gemId: match[1], chatId: match[2] } : null;
  }

  function parseGemRoute(url) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (_error) {
      return null;
    }
    if (parsed.protocol !== "https:" || parsed.hostname !== "gemini.google.com") {
      return null;
    }
    const match = parsed.pathname.match(/^\/gem\/([0-9a-f]+)(?:\/([0-9a-f]+))?\/?$/i);
    return match ? { gemId: match[1], chatId: match[2] || null } : null;
  }

  function usefulTitle(title) {
    const text = String(title || "")
      .replace(/\s*- Google Gemini$/i, "")
      .trim();
    const lower = text.toLowerCase();
    if (!text || lower === "gemini" || lower === "google gemini") {
      return "";
    }
    return text;
  }

  function childrenOf(node) {
    return Array.from((node && node.childNodes) || []);
  }

  function tagName(node) {
    return String((node && node.tagName) || "").toLowerCase();
  }

  function classNames(node) {
    const value = node && node.getAttribute ? node.getAttribute("class") : "";
    return new Set(String(value || "").split(/\s+/).filter(Boolean));
  }

  function descendantByTag(node, wanted) {
    for (const child of childrenOf(node)) {
      if (tagName(child) === wanted) {
        return child;
      }
      const nested = descendantByTag(child, wanted);
      if (nested) {
        return nested;
      }
    }
    return null;
  }

  function katexMarkdown(node, display) {
    const annotation =
      (node.querySelector && node.querySelector(SELECTORS.katexAnnotation)) ||
      descendantByTag(node, "annotation");
    const tex = annotation ? String(annotation.textContent || "").trim() : "";
    if (!tex) {
      return "";
    }
    return display ? `\n\n$$${tex}$$\n\n` : `$${tex}$`;
  }

  function directRows(node) {
    const rows = [];
    function visit(current) {
      for (const child of childrenOf(current)) {
        const tag = tagName(child);
        if (tag === "tr") {
          rows.push(child);
        } else if (tag === "thead" || tag === "tbody" || tag === "tfoot") {
          visit(child);
        }
      }
    }
    visit(node);
    return rows;
  }

  function tableMarkdown(node, render) {
    const rows = directRows(node).map((row) =>
      childrenOf(row)
        .filter((cell) => ["th", "td"].includes(tagName(cell)))
        .map((cell) => render(cell).replace(/\s+/g, " ").trim().replace(/\|/g, "\\|")),
    );
    if (rows.length === 0) {
      return "";
    }
    const width = Math.max(...rows.map((row) => row.length));
    const normalized = rows.map((row) => [...row, ...Array(width - row.length).fill("")]);
    const header = normalized[0];
    const lines = [
      `| ${header.join(" | ")} |`,
      `| ${header.map(() => "---").join(" | ")} |`,
      ...normalized.slice(1).map((row) => `| ${row.join(" | ")} |`),
    ];
    return `${lines.join("\n")}\n\n`;
  }

  function tutorToMarkdown(el) {
    function render(node, context = {}) {
      if (!node) {
        return "";
      }
      if (node.nodeType === 3) {
        return String(node.textContent || "");
      }
      if (node.nodeType !== undefined && node.nodeType !== 1) {
        return "";
      }

      const tag = tagName(node);
      const classes = classNames(node);
      const dataMath = node.getAttribute ? node.getAttribute(SELECTORS.mathAttr) : null;
      if (classes.has(SELECTORS.mathBlock.slice(1)) && dataMath !== null) {
        return `\n\n$$${dataMath}$$\n\n`;
      }
      if (classes.has(SELECTORS.mathInline.slice(1)) && dataMath !== null) {
        return `$${dataMath}$`;
      }
      if (classes.has(SELECTORS.katexDisplay.slice(1))) {
        return katexMarkdown(node, true);
      }
      if (classes.has(SELECTORS.katex.slice(1))) {
        return katexMarkdown(node, Boolean(context.displayMath));
      }

      if (tag === "pre") {
        const code = childrenOf(node).find((child) => tagName(child) === "code");
        const text = String((code || node).textContent || "").replace(/\n+$/, "");
        return `\n\n\`\`\`\n${text}\n\`\`\`\n\n`;
      }
      if (tag === "code") {
        return `\`${String(node.textContent || "")}\``;
      }
      if (tag === "table") {
        return tableMarkdown(node, render);
      }

      const content = childrenOf(node).map((child) => render(child, context)).join("");
      if (/^h[1-6]$/.test(tag)) {
        return `${"#".repeat(Number(tag[1]))} ${content.trim()}\n\n`;
      }
      if (tag === "p") {
        return `${content.trim()}\n\n`;
      }
      if (tag === "strong" || tag === "b") {
        return `**${content}**`;
      }
      if (tag === "em" || tag === "i") {
        return `*${content}*`;
      }
      if (tag === "br") {
        return "\n";
      }
      if (tag === "li") {
        const marker = context.ordered ? "1. " : "- ";
        return `${marker}${content.trim()}\n`;
      }
      if (tag === "ul" || tag === "ol") {
        const ordered = tag === "ol";
        return `${childrenOf(node).map((child) => render(child, { ordered })).join("")}\n`;
      }
      return content;
    }

    return render(el).replace(/\n{3,}/g, "\n\n").trim();
  }

  function extractTurns(rootElement) {
    const containers = rootElement.querySelectorAll(SELECTORS.turn);
    return Array.from(containers).map((container) => {
      const learnerElement = container.querySelector(SELECTORS.learner);
      const lines = learnerElement
        ? Array.from(learnerElement.querySelectorAll(SELECTORS.learnerLines))
        : [];
      const learner = lines.map((line) => String(line.textContent || "")).join("\n").trim();
      const tutorElement = container.querySelector(SELECTORS.tutor);
      const tutor = tutorElement ? tutorToMarkdown(tutorElement) : "";
      return {
        id: String(container.getAttribute("id") || ""),
        learner,
        tutor,
        complete: tutor.trim().length > 0,
      };
    });
  }

  function mergeTurns(stored, fresh) {
    const storedUnique = [];
    const storedIds = new Set();
    for (const turn of stored || []) {
      if (!storedIds.has(turn.id)) {
        storedIds.add(turn.id);
        storedUnique.push(turn);
      }
    }

    const freshById = new Map();
    for (const turn of fresh || []) {
      freshById.set(turn.id, turn);
    }
    const known = storedUnique.map((turn) => freshById.get(turn.id) || turn);
    const knownIndex = new Map(known.map((turn, index) => [turn.id, index]));
    const freshUnique = [];
    const seenFresh = new Set();
    for (const turn of fresh || []) {
      if (!seenFresh.has(turn.id)) {
        seenFresh.add(turn.id);
        freshUnique.push(turn);
      }
    }

    const firstKnownAt = freshUnique.findIndex((turn) => knownIndex.has(turn.id));
    const slots = Array.from({ length: known.length + 1 }, () => []);
    let previousKnown = null;
    for (let index = 0; index < freshUnique.length; index += 1) {
      const turn = freshUnique[index];
      if (knownIndex.has(turn.id)) {
        previousKnown = turn.id;
        continue;
      }
      if (firstKnownAt !== -1 && index < firstKnownAt) {
        slots[0].push(turn);
        continue;
      }
      const nextKnown = freshUnique
        .slice(index + 1)
        .find((candidate) => knownIndex.has(candidate.id));
      if (nextKnown) {
        slots[knownIndex.get(nextKnown.id)].push(turn);
      } else if (previousKnown !== null) {
        slots[knownIndex.get(previousKnown) + 1].push(turn);
      } else {
        slots[known.length].push(turn);
      }
    }

    const merged = [];
    for (let index = 0; index < known.length; index += 1) {
      merged.push(...slots[index], known[index]);
    }
    merged.push(...slots[known.length]);
    return merged;
  }

  function toTranscript(turns) {
    const blocks = (turns || [])
      .filter((turn) => turn.complete)
      .map((turn) => `### learner\n${turn.learner}\n\n### tutor\n${turn.tutor}`);
    return blocks.length ? `${blocks.join("\n\n").trim()}\n` : "";
  }

  function sidecar({ gemId, chatId, title, url, turns, now }) {
    const instant = now && typeof now.toISOString === "function" ? now : new Date(now);
    return {
      source: "gemini-gem",
      gem_id: gemId,
      chat_id: chatId,
      title,
      url,
      turns: (turns || []).filter((turn) => turn.complete).length,
      updated_at: instant.toISOString(),
    };
  }

  function isHostOk(response) {
    return typeof response === "object" && response !== null && response.ok === true;
  }

  const api = {
    SELECTORS,
    parseGemUrl,
    parseGemRoute,
    usefulTitle,
    tutorToMarkdown,
    extractTurns,
    mergeTurns,
    toTranscript,
    sidecar,
    isHostOk,
  };
  root.TutorMemoryCapture = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis === "undefined" ? this : globalThis);
