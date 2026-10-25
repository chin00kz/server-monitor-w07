const test = require("node:test");
const assert = require("node:assert/strict");
const {classifyReceipt, sanitizePayload, mergeBackupService, BACKUP_SERVICE_NAME} = require("../lib/status");
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
  assert.equal(result.services.total, 2);
  assert.equal(result.services.healthy, 0);
});

test("service list accepts six items including Ollama and caps oversized payloads", () => {
  const {MAX_SERVICE_ITEMS, sanitizePayload: sanitize} = require("../lib/status");
  const six = sanitize({
    services: {
      items: [
        {name: "IMFS Integration", status: "healthy"},
        {name: "IMFS M1", status: "healthy"},
        {name: "IMFS M2", status: "healthy"},
        {name: "IMFS M3", status: "healthy"},
        {name: "IMFS M4", status: "healthy"},
        {name: "Ollama", status: "no_check"}
      ],
      healthy: 99,
      total: 99
    }
  });
  assert.equal(six.services.items.length, 6);
  assert.equal(six.services.items[5].name, "Ollama");
  assert.equal(six.services.items[5].status, "no_check");
  assert.equal(six.services.healthy, 5);
  assert.equal(six.services.total, 6);

  const oversized = sanitize({
    services: {
      items: Array.from({length: 20}, (_, i) => ({name: `svc-${i}`, status: "healthy"})),
      healthy: 20,
      total: 20
    }
  });
  assert.equal(oversized.services.items.length, MAX_SERVICE_ITEMS);
  assert.equal(oversized.services.total, MAX_SERVICE_ITEMS);
  assert.equal(oversized.services.healthy, MAX_SERVICE_ITEMS);
});

test("status endpoint rejects unauthenticated requests", async () => {
  const response = {headers: {}, setHeader(key, value) {this.headers[key] = value;}, status(code) {this.code = code; return this;}, json(body) {this.body = body;}};
  await statusHandler({method: "GET", headers: {}}, response);
  assert.equal(response.code, 401);
});

test("mergeBackupService appends backup role and status from live backup row", () => {
  const primary = sanitizePayload({
    node: {id: "workstation-07-5d", uptime_seconds: 10},
    services: {items: [{name: "IMFS Integration", status: "healthy"}, {name: "Ollama", status: "no_check"}]}
  });
  const backup = {
    services: {items: [{name: BACKUP_SERVICE_NAME, status: "healthy", role: "backup"}]}
  };
  const merged = mergeBackupService(primary, backup, "live");
  const item = merged.services.items.find(entry => entry.name === BACKUP_SERVICE_NAME);
  assert.equal(item.status, "healthy");
  assert.equal(item.role, "backup");
  assert.equal(merged.services.items.filter(entry => entry.name === BACKUP_SERVICE_NAME).length, 1);
});

test("mergeBackupService marks backup unknown when backup node is disconnected", () => {
  const primary = sanitizePayload({
    services: {items: [{name: "IMFS Integration", status: "healthy"}]}
  });
  const merged = mergeBackupService(primary, {services: {items: [{name: BACKUP_SERVICE_NAME, status: "healthy"}]}}, "disconnected");
  assert.equal(merged.services.items.find(entry => entry.name === BACKUP_SERVICE_NAME).status, "unknown");
});
