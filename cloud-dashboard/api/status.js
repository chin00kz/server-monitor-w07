const {verifySession} = require("../lib/auth");
const {
  classifyReceipt,
  sanitizePayload,
  mergeBackupService,
  emptyPrimaryPayload
} = require("../lib/status");

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

async function loadBackup(baseUrl, headers, backupNodeId) {
  if (!backupNodeId) return {payload: null, connection: "disconnected"};
  try {
    const backupRow = await readNodeStatus(baseUrl, headers, backupNodeId);
    if (!backupRow) return {payload: null, connection: "disconnected"};
    return {
      payload: backupRow.payload,
      connection: classifyReceipt(backupRow.received_at)
    };
  } catch (_error) {
    return {payload: null, connection: "disconnected"};
  }
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
    const primaryConnection = row ? classifyReceipt(row.received_at) : "disconnected";
    let payload = row ? sanitizePayload(row.payload) : emptyPrimaryPayload();
    if (backupNodeId) {
      const backup = await loadBackup(baseUrl, headers, backupNodeId);
      payload = mergeBackupService(payload, backup.payload, backup.connection);
    }
    if (!row && !backupNodeId) {
      response.status(200).json({connection_state: "disconnected", received_at: null, payload: null});
      return;
    }
    response.status(200).json({
      connection_state: primaryConnection,
      received_at: row ? row.received_at : null,
      observed_at: row ? row.observed_at : null,
      payload
    });
  } catch (error) {
    response.status(502).json({error: "cloud status unavailable"});
  }
};
