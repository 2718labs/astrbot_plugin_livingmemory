import assert from "node:assert/strict";
import test from "node:test";

import { renderFactCard } from "../../pages/dashboard/modules/peek-panel.js";


test("fact details render as a readable card with collapsed technical data", () => {
  globalThis.window = {
    t(key, ...args) {
      return `${key}: ${args.join(" | ")}`;
    },
  };
  globalThis.document = {
    createElement() {
      let text = "";
      return {
        set textContent(value) {
          text = String(value);
        },
        get innerHTML() {
          return text;
        },
      };
    },
  };

  try {
    const html = renderFactCard({
      fact_id: "fact_1",
      fact: "张三正在开发五子棋",
      topics: ["游戏开发"],
      participants: ["张三"],
      persona_reaction: {
        emotion: "期待",
        thought: "想看看成品",
      },
      lifecycle: {
        status: "active",
        retrieval_count: 3,
        injection_count: 1,
      },
    });

    assert.match(html, /class="memory-fact-card"/);
    assert.match(html, /class="memory-fact-text">张三正在开发五子棋/);
    assert.match(html, /class="memory-fact-reaction"/);
    assert.match(html, /memory-fact-chip-topic/);
    assert.match(html, /memory-fact-chip-person/);
    assert.match(html, /<details class="memory-fact-technical">/);
    assert.match(html, /detail\.factTechnical/);
    assert.match(html, /fact_1 \| active \| 3 \| 1/);
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }
});
