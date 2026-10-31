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

test("baseline entries map into four sections carrying their slot quotas", () => {
  const page = new UserBaselinePage({ page: "baselines" }, {}, {});
  page.detail = {
    category_slots: {
      address_identity: 5,
      relationship: 2,
      stable_preference: 4,
      interaction_preference: 4,
      long_term_boundary: 2,
      global_constraint: 1,
    },
  };
  const sections = page.groupEntrySections([
    { category: "address_identity" },
    { category: "relationship" },
    { category: "stable_preference" },
    { category: "interaction_preference" },
    { category: "long_term_boundary" },
    { category: "global_constraint" },
  ]);
  assert.deepEqual(
    sections.map(section => section.entries.length),
    [2, 1, 1, 2],
  );
  assert.deepEqual(
    sections.map(section => section.limit),
    [7, 4, 4, 3],
  );
  assert.match(sections[0].label, /baseline\.section\.identity/);
  assert.match(sections[1].label, /baseline\.section\.preference/);
  assert.match(sections[2].label, /baseline\.section\.interaction/);
  assert.match(sections[3].label, /baseline\.section\.boundaries/);
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
    locked: false,
    evidence: [],
  });
  const actions = [...html.matchAll(/data-baseline-action="([^"]+)"/g)].map(
    match => match[1]
  );
  assert.deepEqual(actions, ["edit", "delete-entry"]);
  assert.match(html, /baseline\.autoUpdateAllowed/);
  assert.doesNotMatch(html, /baseline-lock-badge/);
});

test("returning from an entry form restores the detail scroll offset", () => {
  const elements = new Map();
  const make = () => {
    const element = { textContent: "", scrollTop: 0, onclick: null, _html: "" };
    Object.defineProperty(element, "innerHTML", {
      get() { return this._html; },
      set(value) {
        this._html = value;
        this.scrollTop = 0;
      },
    });
    return element;
  };
  global.document.getElementById = id => {
    if (!elements.has(id)) elements.set(id, make());
    return elements.get(id);
  };
  const page = new UserBaselinePage({ page: "baselines" }, {}, { open() {} });
  page.detail = {
    user_id: 7,
    display_name: "Tester",
    personas: ["persona-a"],
    entries: [],
    portraits: [],
    graveyard: [],
  };
  const body = elements.get("peek-body") || elements.set("peek-body", make()).get("peek-body");
  body.scrollTop = 420;
  page.captureDetailScroll();
  body.scrollTop = 0;

  page.backToDetail();
  assert.equal(body.scrollTop, 420);

  page.renderDetail();
  assert.equal(body.scrollTop, 0);
});

test("locked baseline entry shows a visible lock badge", () => {
  const page = new UserBaselinePage({ page: "baselines" }, {}, {});
  const html = page.renderEntry({
    entry_id: "entry-2",
    persona_id: "persona-a",
    category: "long_term_boundary",
    content: "不要叫我前辈",
    enabled: true,
    source_type: "manual",
    allow_auto_update: false,
    locked: true,
    evidence: [],
  });
  assert.match(html, /is-locked/);
  assert.match(html, /baseline-lock-badge/);
  assert.match(html, /data-lucide="lock"/);
  assert.match(html, /baseline\.locked/);
  assert.match(html, /baseline\.autoUpdateBlocked/);
  const actions = [...html.matchAll(/data-baseline-action="([^"]+)"/g)].map(
    match => match[1]
  );
  assert.deepEqual(actions, ["edit", "delete-entry"]);
});

test("baseline warning dismissal is persisted on the server", async () => {
  global.document.getElementById = () => null;
  const posted = [];
  const page = new UserBaselinePage(
    { page: "baselines" },
    { async post(path, payload) { posted.push([path, payload]); } },
    {},
  );
  page.generationWarning = {
    occurred_at: 100,
    reasons: ["JSON 格式错误", "操作未引用目标用户原话"],
  };
  page.generationWarningAt = 100;
  assert.equal(page.shouldShowGenerationWarning(), true);
  assert.match(page.generationWarningText(), /JSON 格式错误/);
  assert.match(page.generationWarningText(), /操作未引用目标用户原话/);

  assert.equal(await page.dismissGenerationWarning(), true);
  assert.deepEqual(posted, [["user-baselines/warning/dismiss", {}]]);
  assert.equal(page.generationWarning, null);
  assert.equal(page.shouldShowGenerationWarning(), false);
});

test("baseline warning stays visible when dismissal fails", async () => {
  global.document.getElementById = () => null;
  const page = new UserBaselinePage(
    { page: "baselines" },
    { async post() { throw new Error("boom"); } },
    {},
  );
  page.generationWarning = { occurred_at: 100, reasons: ["x", "y"] };
  page.generationWarningAt = 100;
  const toasts = [];
  page.toast = (message, isError) => toasts.push([message, isError]);
  assert.equal(await page.dismissGenerationWarning(), false);
  assert.equal(page.shouldShowGenerationWarning(), true);
  assert.deepEqual(toasts, [["boom", true]]);
});
