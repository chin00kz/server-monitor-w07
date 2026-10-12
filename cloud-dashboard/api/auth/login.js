async function handler(request, response) {
  if (request.method !== "POST") {
    response.status(405).json({error: "method not allowed"});
    return;
  }
  try {
    const body = typeof request.body === "string" ? JSON.parse(request.body) : request.body || {};
    const email = typeof body.email === "string" ? body.email.trim() : "";
    const password = typeof body.password === "string" ? body.password : "";
    if (!email || !password || email.length > 254 || password.length > 512) {
      response.status(400).json({error: "email and password are required"});
      return;
    }
    const authResponse = await fetch(`${process.env.SUPABASE_URL}/auth/v1/token?grant_type=password`, {
      method: "POST",
      headers: {
        apikey: process.env.SUPABASE_ANON_KEY,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({email, password})
    });
    if (!authResponse.ok) {
      response.status(401).json({error: "invalid credentials"});
      return;
    }
    const session = await authResponse.json();
    if (!session.access_token) {
      response.status(401).json({error: "authentication failed"});
      return;
    }
    response.setHeader("Set-Cookie", `cloud_access=${encodeURIComponent(session.access_token)}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=3600`);
    response.status(200).json({ok: true});
  } catch (error) {
    response.status(400).json({error: "invalid login request"});
  }
}

module.exports = handler;
