const {getAccessToken} = require("../lib/auth");
const {classifyReceipt, sanitizePayload} = require("../lib/status");

module.exports = async function status(request, response) {
  response.setHeader("Cache-Control", "no-store");
  if (request.method !== "GET") {
    response.status(405).json({error: "method not allowed"});
    return;
  }
  const accessToken = getAccessToken(request);
  if (!accessToken) {
    response.status(401).json({error: "authentication required"});
    return;
  }
  const baseUrl = (process.env.SUPABASE_URL || "").replace(/\/$/, "");
  const anonKey = process.env.SUPABASE_ANON_KEY;
  const nodeId = process.env.CLOUD_NODE_ID || "workstation-07-5d";
  if (!baseUrl || !anonKey) {
    response.status(500).json({error: "cloud status is not configured"});
    return;
  }
  const headers = {
    apikey: anonKey,
    Authorization: `Bearer ${accessToken}`
  };
  try {
    const userResponse = await fetch(`${baseUrl}/auth/v1/user`, {headers});
    if (!userResponse.ok) {
      response.status(401).json({error: "authentication required"});
      return;
    }
    const query = new URL(`${baseUrl}/rest/v1/node_status`);
    query.searchParams.set("select", "node_id,observed_at,received_at,payload");
    query.searchParams.set("node_id", `eq.${nodeId}`);
    query.searchParams.set("limit", "1");
    const dataResponse = await fetch(query, {headers});
    if (!dataResponse.ok) {
      response.status(502).json({error: "cloud status unavailable"});
      return;
    }
    const rows = await dataResponse.json();
    const row = Array.isArray(rows) ? rows[0] : null;
    if (!row) {
      response.status(200).json({connection_state: "disconnected", received_at: null, payload: null});
      return;
    }
    response.status(200).json({
      connection_state: classifyReceipt(row.received_at),
      received_at: row.received_at,
      observed_at: row.observed_at,
      payload: sanitizePayload(row.payload)
    });
  } catch (error) {
    response.status(502).json({error: "cloud status unavailable"});
  }
};
