import test from "node:test";
import assert from "node:assert/strict";

import { formatTimestamp, weightedImportance } from "../../pages/dashboard/modules/utils.js";

test("formatTimestamp strips Python microseconds without shifting local time", () => {
  assert.equal(
    formatTimestamp("2026-08-23 22:21:00.947509"),
    "2026-08-23 22:21:00"
  );
});

test("formatTimestamp accepts epoch seconds and milliseconds", () => {
  assert.equal(formatTimestamp(1700000000), formatTimestamp(1700000000000));
  assert.match(formatTimestamp(1700000000), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
});

test("formatTimestamp preserves date-only and invalid legacy values", () => {
  assert.equal(formatTimestamp("2026-01-01"), "2026-01-01");
  assert.equal(formatTimestamp("unknown"), "unknown");
  assert.equal(formatTimestamp(null), "--");
});

test("weightedImportance aggregates child facts by self-weight Σ(imp²)/Σ(imp)", () => {
  const facts = [
    { importance: 0.9 },
    { importance: 0.6 },
  ];
  const expected = (0.81 + 0.36) / (0.9 + 0.6);
  assert.ok(Math.abs(weightedImportance(facts, 0.5) - expected) < 1e-9);
});

test("weightedImportance leans on strong facts but keeps weak ones present", () => {
  const facts = [
    { importance: 1.0 },
    { importance: 0.1 },
    { importance: 0.1 },
  ];
  const value = weightedImportance(facts, 0.5);
  assert.ok(value < 1.0, "weak facts must still drag the aggregate down");
  assert.ok(value > 0.8, "strong fact must dominate the aggregate");
  assert.ok(Math.abs(value - (1 + 0.01 + 0.01) / 1.2) < 1e-9);
});

test("weightedImportance falls back when no child fact has a numeric importance", () => {
  assert.equal(weightedImportance([], 0.75), 0.75);
  assert.equal(
    weightedImportance([{ fact: "no importance" }, { importance: "x" }], 0.4),
    0.4
  );
  assert.equal(weightedImportance(null, 0.5), 0.5);
});
