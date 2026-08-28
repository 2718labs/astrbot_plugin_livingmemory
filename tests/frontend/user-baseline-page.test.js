import test from "node:test";
import assert from "node:assert/strict";

global.window = {
  t(key, ...args) {
    return args.length ? key + ":" + args.join(",") : key;
  },
};
global.document = {
  createElement() {
    return {
      innerHTML: "",
      set textContent(value) {
        this.innerHTML = String(value)
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;");
      },
    };
  },
};

const { UserBaselinePage } = await import(
  "../../pages/dashboard/modules/user-baseline-page.js"
);

test("baseline card exposes identity, progress, and cooldown without entry text", () => {
  const page = new UserBaselinePage({ page: "baselines" }, {}, {});
  const html = page.renderCard({
    user_id: 7,
    display_name: "Tester",
    platform: "test",
    canonical_identity: "user-7",
    persona_count: 2,
    entry_count: 4,
    progress_count: 7,
    cooldown_remaining: 3660,
    last_success_at: null,
  });
  assert.match(html, /test:user-7/);
  assert.match(html, /7\/8/);
  assert.match(html, /baseline\.cooldown:1,1/);
});

test("baseline detail grouping keeps global and persona scopes separate", () => {
  const page = new UserBaselinePage({ page: "baselines" }, {}, {});
  const groups = page.groupEntries([
    { persona_id: "", category: "global_constraint" },
    { persona_id: "persona-a", category: "relationship" },
    { persona_id: "persona-a", category: "relationship" },
  ]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].entries.length, 1);
  assert.equal(groups[1].entries.length, 2);
  assert.match(groups[0].label, /baseline\.global/);
  assert.match(groups[1].label, /persona-a/);
});

test("baseline evidence renders its id and escaped source text", () => {
  const page = new UserBaselinePage({ page: "baselines" }, {}, {});
  const html = page.renderEvidence({
    id: "W3:M7",
    text: '<script>alert("x")</script>',
  });
  assert.match(html, /W3:M7/);
  assert.match(html, /&lt;script&gt;alert\(&quot;x&quot;\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});
