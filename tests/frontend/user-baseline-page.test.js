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

test("baseline entries map into three visual sections without slot quotas", () => {
  const page = new UserBaselinePage({ page: "baselines" }, {}, {});
  const sections = page.groupEntrySections([
    { category: "address_identity" },
    { category: "relationship" },
    { category: "interaction_preference" },
    { category: "long_term_boundary" },
    { category: "global_constraint" },
  ]);
  assert.deepEqual(
    sections.map(section => section.entries.length),
    [2, 1, 2],
  );
  assert.match(sections[0].label, /baseline\.section\.identity/);
  assert.match(sections[1].label, /baseline\.section\.interaction/);
  assert.match(sections[2].label, /baseline\.section\.boundaries/);
});

test("baseline evidence renders its id and escaped source text", () => {
  const page = new UserBaselinePage({ page: "baselines" }, {}, {});
  const html = page.renderEvidence({
    id: "W3:M7",
    text: '<script>alert("x")</script>',
    role: "user",
    speaker_name: "Tester",
    timestamp: 1_700_000_000,
  });
  assert.match(html, /W3:M7/);
  assert.match(html, /Tester/);
  assert.match(html, /title=/);
  assert.match(html, /&lt;script&gt;alert\(&quot;x&quot;\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test("baseline entry exposes only edit and delete actions", () => {
  const page = new UserBaselinePage({ page: "baselines" }, {}, {});
  const html = page.renderEntry({
    entry_id: "entry-1",
    persona_id: "persona-a",
    category: "relationship",
    content: "长期协作伙伴",
    enabled: true,
    source_type: "automatic",
    allow_auto_update: true,
    evidence: [],
  });
  const actions = [...html.matchAll(/data-baseline-action="([^"]+)"/g)].map(
    match => match[1]
  );
  assert.deepEqual(actions, ["edit", "delete-entry"]);
  assert.match(html, /baseline\.autoUpdateAllowed/);
  assert.doesNotMatch(html, /toggle|promote|lock/);
});

test("baseline warning dismissal applies only to the current failure", () => {
  const stored = new Map();
  window.localStorage = {
    getItem(key) { return stored.get(key) ?? null; },
    setItem(key, value) { stored.set(key, value); },
  };
  const page = new UserBaselinePage({ page: "baselines" }, {}, {});
  page.generationWarning = {
    occurred_at: 100,
    reasons: ["JSON 格式错误", "操作未引用目标用户原话"],
  };
  page.generationWarningAt = 100;
  assert.equal(page.shouldShowGenerationWarning(), true);
  assert.match(page.generationWarningText(), /JSON 格式错误/);
  assert.match(page.generationWarningText(), /操作未引用目标用户原话/);

  window.localStorage.setItem("livingmemory.baselineWarningDismissedAt", "100");
  assert.equal(page.shouldShowGenerationWarning(), false);

  page.generationWarningAt = 101;
  assert.equal(page.shouldShowGenerationWarning(), true);
});
