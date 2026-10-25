const {verifySession} = require("../lib/auth");
const {classifyReceipt, sanitizePayload} = require("../lib/status");

module.exports = async function status(request, response) {
  response.setHeader("Cache-Control", "no-store");
  if (request.method !== "GET") {
    response.status(405).json({error: "method not allowed"});
    return;
  }
  if (!verifySession(request)) {
    response.status(401).json({error: "authentication required"});
    return;
  }
  const baseUrl = (process.env.SUPABASE_URL || "").replace(/\/$/, "");
  const readOnlyKey = process.env.SUPABASE_READONLY_KEY;
  const nodeId = process.env.CLOUD_NODE_ID || "workstation-07-5d";
  if (!baseUrl || !readOnlyKey) {
    response.status(500).json({error: "cloud status is not configured"});
    return;
  }
  const headers = {
    apikey: readOnlyKey,
    Authorization: `Bearer ${readOnlyKey}`,
    "Content-Type": "application/json"
  };
  try {
    const dataResponse = await fetch(`${baseUrl}/rest/v1/rpc/read_node_status`, {
      method: "POST", headers, body: JSON.stringify({requested_node_id: nodeId})
    });
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
