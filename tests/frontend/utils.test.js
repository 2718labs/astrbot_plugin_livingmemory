import test from "node:test";
import assert from "node:assert/strict";

import { formatTimestamp } from "../../pages/dashboard/modules/utils.js";

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
