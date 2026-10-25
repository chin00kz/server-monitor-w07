const crypto = require("node:crypto");
const {promisify} = require("node:util");

const scrypt = promisify(crypto.scrypt);
const COOKIE_NAME = "cloud_session";
const SESSION_TTL_SECONDS = 2592000;

function parseCookies(request) {
  const result = {};
  for (const part of (request.headers.cookie || "").split(";")) {
    const separator = part.indexOf("=");
    if (separator < 0) continue;
    try {
      result[part.slice(0, separator).trim()] = decodeURIComponent(part.slice(separator + 1).trim());
    } catch (_) {}
  }
  return result;
}

function secureEqual(left, right) {
  const leftBuffer = Buffer.from(left);
  const rightBuffer = Buffer.from(right);
  return leftBuffer.length === rightBuffer.length && crypto.timingSafeEqual(leftBuffer, rightBuffer);
}

async function verifyPassword(password, encodedHash = process.env.DASHBOARD_PASSWORD_HASH) {
  if (typeof password !== "string" || !encodedHash) return false;
  const parts = encodedHash.split("$");
  if (parts.length !== 6 || parts[0] !== "scrypt") return false;
  const [cost, blockSize, parallelization] = parts.slice(1, 4).map(Number);
  if (!Number.isInteger(cost) || cost < 16384 || cost > 1048576 || (cost & (cost - 1)) !== 0 ||
      !Number.isInteger(blockSize) || blockSize < 8 || blockSize > 32 ||
      !Number.isInteger(parallelization) || parallelization < 1 || parallelization > 16) return false;
  try {
    const salt = Buffer.from(parts[4], "base64url");
    const expected = Buffer.from(parts[5], "base64url");
    if (salt.length < 16 || expected.length !== 64) return false;
    const actual = await scrypt(password, salt, expected.length, {
      N: cost, r: blockSize, p: parallelization, maxmem: 128 * cost * blockSize + 1024 * 1024
    });
    return crypto.timingSafeEqual(actual, expected);
  } catch (_) {
    return false;
  }
}

function sessionSecret() {
  const secret = process.env.DASHBOARD_SESSION_SECRET || "";
  if (Buffer.byteLength(secret) < 32) throw new Error("DASHBOARD_SESSION_SECRET must be at least 32 bytes");
  return secret;
}

function sign(value) {
  return crypto.createHmac("sha256", sessionSecret()).update(value).digest("base64url");
}

function createSession(now = Date.now()) {
  const payload = Buffer.from(JSON.stringify({
    v: 1,
    iat: Math.floor(now / 1000),
    exp: Math.floor(now / 1000) + SESSION_TTL_SECONDS,
    nonce: crypto.randomBytes(16).toString("base64url")
  })).toString("base64url");
  return `${payload}.${sign(payload)}`;
}

function verifySession(request, now = Date.now()) {
  const token = parseCookies(request)[COOKIE_NAME];
  if (!token) return false;
  const separator = token.lastIndexOf(".");
  if (separator < 1 || !secureEqual(sign(token.slice(0, separator)), token.slice(separator + 1))) return false;
  try {
    const payload = JSON.parse(Buffer.from(token.slice(0, separator), "base64url").toString("utf8"));
    const currentTime = Math.floor(now / 1000);
    return payload.v === 1 && Number.isInteger(payload.iat) && Number.isInteger(payload.exp) &&
      payload.iat <= currentTime + 60 && payload.exp > currentTime &&
      payload.exp - payload.iat === SESSION_TTL_SECONDS;
  } catch (_) {
    return false;
  }
}

function sessionCookie(token) {
  return `${COOKIE_NAME}=${encodeURIComponent(token)}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=${SESSION_TTL_SECONDS}`;
}

function clearSessionCookie() {
  return `${COOKIE_NAME}=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0`;
}

module.exports = {
  COOKIE_NAME,
  SESSION_TTL_SECONDS,
  createSession,
  verifySession,
  verifyPassword,
  sessionCookie,
  clearSessionCookie
};
