const LIVE_AFTER_SECONDS = 90;
const DISCONNECTED_AFTER_SECONDS = 180;
// Hard cap against oversized/malicious payloads. Keep >= Dell services + Oracle backup.
const MAX_SERVICE_ITEMS = 8;
const BACKUP_SERVICE_NAME = "IMFS Integration Backup";

function classifyReceipt(receivedAt, now = Date.now()) {
  const timestamp = Date.parse(receivedAt || "");
  if (!Number.isFinite(timestamp)) return "disconnected";
  const age = Math.max(0, (now - timestamp) / 1000);
  if (age < LIVE_AFTER_SECONDS) return "live";
  if (age < DISCONNECTED_AFTER_SECONDS) return "stale";
  return "disconnected";
}

function finitePercent(value) {
  return Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
}

function serviceStatus(value) {
  return ["healthy", "no_check", "unhealthy", "stopped", "starting", "unknown"].includes(value)
    ? value
    : "unknown";
}

function serviceRole(value) {
  return value === "backup" ? "backup" : null;
}

function sanitizePayload(payload) {
  if (!payload || typeof payload !== "object") return null;
  const node = payload.node && typeof payload.node === "object" ? payload.node : {};
  const network = payload.network && typeof payload.network === "object" ? payload.network : {};
  const battery = payload.battery && typeof payload.battery === "object" ? payload.battery : {};
  const resources = payload.resources && typeof payload.resources === "object" ? payload.resources : {};
  const serviceData = payload.services && typeof payload.services === "object" ? payload.services : {};
  const items = Array.isArray(serviceData.items)
    ? serviceData.items.slice(0, MAX_SERVICE_ITEMS).map(item => {
        const entry = {
          name: typeof item?.name === "string" ? item.name.slice(0, 32) : "Unknown",
          status: serviceStatus(item?.status)
        };
        const role = serviceRole(item?.role) || (entry.name === BACKUP_SERVICE_NAME ? "backup" : null);
        if (role) entry.role = role;
        return entry;
      })
    : [];
  return {
    node: {
      id: typeof node.id === "string" ? node.id.slice(0, 64) : "unknown",
      uptime_seconds: Number.isFinite(node.uptime_seconds) && node.uptime_seconds >= 0 ? node.uptime_seconds : null
    },
    network: {
      active_route: ["starlink-mini", "dialog-ethernet", "unknown"].includes(network.active_route)
        ? network.active_route : "unknown",
      starlink: typeof network.starlink === "string" ? network.starlink.slice(0, 24) : "unknown",
      dialog: typeof network.dialog === "string" ? network.dialog.slice(0, 24) : "unknown"
    },
    battery: {
      activity: ["detected", "not_detected", "unknown"].includes(battery.activity)
        ? battery.activity : "unknown",
      percent: finitePercent(battery.percent),
      on_ac: typeof battery.on_ac === "boolean" ? battery.on_ac : null,
      status: typeof battery.status === "string" ? battery.status.slice(0, 24) : "unknown"
    },
    resources: {
      cpu_percent: finitePercent(resources.cpu_percent),
      ram_percent: finitePercent(resources.ram_percent),
      disk_percent: finitePercent(resources.disk_percent)
    },
    services: {
      items,
      // Recount from sanitized items so a truncated list cannot keep inflated counts.
      healthy: items.filter(item => item.status === "healthy").length,
      total: items.length
    }
  };
}

/**
 * Append/replace the Oracle backup service on a Dell (primary) sanitized payload.
 * connectionState is the backup row's receipt class; disconnected → unknown status.
 */
function mergeBackupService(primaryPayload, backupPayload, backupConnectionState) {
  const base = primaryPayload && typeof primaryPayload === "object"
    ? structuredClone(primaryPayload)
    : null;
  if (!base) return null;
  const services = base.services && typeof base.services === "object" ? base.services : {items: []};
  const items = Array.isArray(services.items) ? services.items.filter(item => item.name !== BACKUP_SERVICE_NAME) : [];
  let status = "unknown";
  if (backupConnectionState === "live" || backupConnectionState === "stale") {
    const sanitizedBackup = sanitizePayload(backupPayload);
    const fromBackup = sanitizedBackup?.services?.items?.find(item => item.name === BACKUP_SERVICE_NAME)
      || sanitizedBackup?.services?.items?.[0];
    if (fromBackup) status = serviceStatus(fromBackup.status);
  }
  items.push({name: BACKUP_SERVICE_NAME, status, role: "backup"});
  base.services = {
    items: items.slice(0, MAX_SERVICE_ITEMS),
    healthy: items.filter(item => item.status === "healthy").length,
    total: Math.min(items.length, MAX_SERVICE_ITEMS)
  };
  return base;
}

module.exports = {
  LIVE_AFTER_SECONDS,
  DISCONNECTED_AFTER_SECONDS,
  MAX_SERVICE_ITEMS,
  BACKUP_SERVICE_NAME,
  classifyReceipt,
  sanitizePayload,
  mergeBackupService
};
