const test = require("node:test");
const assert = require("node:assert/strict");
const {classifyReceipt, sanitizePayload} = require("../lib/status");
const {getAccessToken} = require("../lib/auth");
const statusHandler = require("../api/status");

test("receipt timestamps produce live, stale, and disconnected states", () => {
  const now = Date.parse("2026-10-12T00:00:00.000Z");
  assert.equal(classifyReceipt(new Date(now - 30 * 1000).toISOString(), now), "live");
  assert.equal(classifyReceipt(new Date(now - 120 * 1000).toISOString(), now), "stale");
  assert.equal(classifyReceipt(new Date(now - 240 * 1000).toISOString(), now), "disconnected");
  assert.equal(classifyReceipt("invalid", now), "disconnected");
});

test("malformed telemetry is reduced to safe unknown values", () => {
  const result = sanitizePayload({
    node: {id: "node", uptime_seconds: -1},
    resources: {cpu_percent: 999},
    services: {items: [{name: "safe"}, {name: "<script>"}], healthy: "bad", total: -1}
  });
  assert.equal(result.node.uptime_seconds, null);
  assert.equal(result.resources.cpu_percent, null);
  assert.deepEqual(result.services.items.map(item => item.status), ["unknown", "unknown"]);
});

test("status endpoint rejects unauthenticated requests", async () => {
  const response = {headers: {}, setHeader(key, value) {this.headers[key] = value;}, status(code) {this.code = code; return this;}, json(body) {this.body = body;}};
  await statusHandler({method: "GET", headers: {}}, response);
  assert.equal(response.code, 401);
});

test("malformed access cookies are rejected", () => {
  assert.equal(getAccessToken({headers: {cookie: "cloud_access=%"}}), null);
});
