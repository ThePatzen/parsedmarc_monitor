const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const candidates = [
  process.env.DMARC_APP_JS,
  path.resolve(__dirname, "../rootfs/app/dmarc_monitor/static/app.js"),
  "/app/dmarc_monitor/static/app.js",
].filter((candidate) => candidate && fs.existsSync(candidate));
assert.ok(candidates.length > 0, "could not locate the browser application");
const app = require(candidates[0]);

test("defaultRange uses seven local calendar dates across a month boundary", () => {
  const now = new Date(2026, 2, 2, 23, 59, 59, 999);

  assert.deepEqual(app.defaultRange(now), {
    dateFrom: "2026-02-24",
    dateTo: "2026-03-02",
  });
});

test("pagination disables Previous on the first page after loading", () => {
  const payload = { total: 101, page: 1, page_size: 50 };

  assert.deepEqual(app.paginationState(payload, true), {
    totalPages: 3,
    previousDisabled: true,
    nextDisabled: true,
  });
  assert.deepEqual(app.paginationState(payload, false), {
    totalPages: 3,
    previousDisabled: true,
    nextDisabled: false,
  });
});

test("pagination enables both directions on a middle page after loading", () => {
  const payload = { total: 101, page: 2, page_size: 50 };

  assert.deepEqual(app.paginationState(payload, true), {
    totalPages: 3,
    previousDisabled: true,
    nextDisabled: true,
  });
  assert.deepEqual(app.paginationState(payload, false), {
    totalPages: 3,
    previousDisabled: false,
    nextDisabled: false,
  });
});

test("pagination keeps Next disabled on the last page after loading", () => {
  const payload = { total: 101, page: 3, page_size: 50 };

  assert.deepEqual(app.paginationState(payload, true), {
    totalPages: 3,
    previousDisabled: true,
    nextDisabled: true,
  });
  assert.deepEqual(app.paginationState(payload, false), {
    totalPages: 3,
    previousDisabled: false,
    nextDisabled: true,
  });
});

test("pagination disables both buttons for an empty first page", () => {
  const payload = { total: 0, page: 1, page_size: 50 };

  assert.deepEqual(app.paginationState(payload, true), {
    totalPages: 1,
    previousDisabled: true,
    nextDisabled: true,
  });
  assert.deepEqual(app.paginationState(payload, false), {
    totalPages: 1,
    previousDisabled: true,
    nextDisabled: true,
  });
});
