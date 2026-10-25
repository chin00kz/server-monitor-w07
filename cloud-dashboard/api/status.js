const {verifySession} = require("../lib/auth");
const {classifyReceipt, sanitizePayload, mergeBackupService} = require("../lib/status");

async function readNodeStatus(baseUrl, headers, nodeId) {
  const dataResponse = await fetch(`${baseUrl}/rest/v1/rpc/read_node_status`, {
    method: "POST",
    headers,
    body: JSON.stringify({requested_node_id: nodeId})
  });
  if (!dataResponse.ok) return null;
  const rows = await dataResponse.json();
  return Array.isArray(rows) ? rows[0] || null : null;
}

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
  const backupNodeId = (process.env.CLOUD_BACKUP_NODE_ID || "").trim();
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
    const row = await readNodeStatus(baseUrl, headers, nodeId);
    if (!row) {
      response.status(200).json({connection_state: "disconnected", received_at: null, payload: null});
      return;
    }
    let payload = sanitizePayload(row.payload);
    if (backupNodeId && payload) {
      let backupPayload = null;
      let backupConnection = "disconnected";
      try {
        const backupRow = await readNodeStatus(baseUrl, headers, backupNodeId);
        if (backupRow) {
          backupConnection = classifyReceipt(backupRow.received_at);
          backupPayload = backupRow.payload;
        }
      } catch (_error) {
        backupConnection = "disconnected";
      }
      payload = mergeBackupService(payload, backupPayload, backupConnection);
    }
    response.status(200).json({
      connection_state: classifyReceipt(row.received_at),
      received_at: row.received_at,
      observed_at: row.observed_at,
      payload
    });
  } catch (error) {
    response.status(502).json({error: "cloud status unavailable"});
  }
};
