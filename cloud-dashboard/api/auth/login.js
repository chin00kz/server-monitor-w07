const {createSession, sessionCookie, verifyPassword} = require("../../lib/auth");

const attempts = new Map();
const ATTEMPT_LIMIT = 5;
const ATTEMPT_WINDOW_MS = 15 * 60 * 1000;

function allowAttempt(request, now = Date.now()) {
  const forwarded = request.headers["x-vercel-forwarded-for"] || request.headers["x-forwarded-for"] || "unknown";
  const address = String(forwarded).split(",")[0].trim();
  const current = attempts.get(address);
  if (!current || current.resetAt <= now) {
    attempts.set(address, {count: 1, resetAt: now + ATTEMPT_WINDOW_MS});
    return true;
  }
  current.count += 1;
  return current.count <= ATTEMPT_LIMIT;
}

async function handler(request, response) {
  if (request.method !== "POST") {
    response.status(405).json({error: "method not allowed"});
    return;
  }
  try {
    if (!allowAttempt(request)) {
      response.status(429).json({error: "too many attempts"});
      return;
    }
    const body = typeof request.body === "string" ? JSON.parse(request.body) : request.body || {};
    const password = typeof body.password === "string" ? body.password : "";
    if (!password || password.length > 512) {
      response.status(400).json({error: "password is required"});
      return;
    }
    if (!await verifyPassword(password)) {
      response.status(401).json({error: "invalid credentials"});
      return;
    }
    response.setHeader("Set-Cookie", sessionCookie(createSession()));
    response.status(200).json({ok: true});
  } catch (error) {
    response.status(503).json({error: "login unavailable"});
  }
}

module.exports = handler;
