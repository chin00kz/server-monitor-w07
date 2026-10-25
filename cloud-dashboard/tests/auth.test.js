const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const loginHandler = require("../api/auth/login");
const {
  SESSION_TTL_SECONDS,
  createSession,
  verifySession,
  verifyPassword
} = require("../lib/auth");

const password = "test shared password";
const salt = Buffer.alloc(16, 7);
const passwordHash = `scrypt$16384$8$1$${salt.toString("base64url")}$${crypto.scryptSync(password, salt, 64).toString("base64url")}`;

function mockResponse() {
  return {
    headers: {},
    setHeader(key, value) { this.headers[key] = value; },
    status(code) { this.code = code; return this; },
    json(body) { this.body = body; return this; }
  };
}

function loginRequest(candidate, address) {
  return {method: "POST", headers: {"x-forwarded-for": address}, body: {password: candidate}};
}

test.before(() => {
  process.env.DASHBOARD_PASSWORD_HASH = passwordHash;
  process.env.DASHBOARD_SESSION_SECRET = "test-session-secret-that-is-at-least-32-bytes";
});

test("correct shared password creates a hardened signed session", async () => {
  const response = mockResponse();
  await loginHandler(loginRequest(password, "192.0.2.1"), response);
  assert.equal(response.code, 200);
  assert.match(response.headers["Set-Cookie"], /^cloud_session=/);
  assert.match(response.headers["Set-Cookie"], /HttpOnly; Secure; SameSite=Strict/);
  const cookie = response.headers["Set-Cookie"].split(";")[0];
  assert.equal(verifySession({headers: {cookie}}), true);
});

test("incorrect shared password is rejected", async () => {
  assert.equal(await verifyPassword("incorrect"), false);
  const response = mockResponse();
  await loginHandler(loginRequest("incorrect", "192.0.2.2"), response);
  assert.equal(response.code, 401);
  assert.equal(response.headers["Set-Cookie"], undefined);
});

test("expired sessions are rejected", () => {
  const issuedAt = Date.parse("2026-10-12T00:00:00Z");
  const token = createSession(issuedAt);
  const request = {headers: {cookie: `cloud_session=${encodeURIComponent(token)}`}};
  assert.equal(verifySession(request, issuedAt + SESSION_TTL_SECONDS * 1000 - 1), true);
  assert.equal(verifySession(request, issuedAt + SESSION_TTL_SECONDS * 1000), false);
});

test("login attempts are rate limited", async () => {
  const address = "192.0.2.3";
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const response = mockResponse();
    await loginHandler(loginRequest("incorrect", address), response);
    assert.equal(response.code, 401);
  }
  const blocked = mockResponse();
  await loginHandler(loginRequest(password, address), blocked);
  assert.equal(blocked.code, 429);
});
