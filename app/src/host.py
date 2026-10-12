"""Read-only readers for host data (via the /host/proc and /host/sys mounts)."""
import os
import re
import time
from pathlib import Path

PROC = Path(os.environ.get("HOST_PROC", "/proc"))
SYS = Path(os.environ.get("HOST_SYS", "/sys"))
DISK_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+)$")


def read(p):
    try:
        return Path(p).read_text().strip()
    except OSError:
        return None


def num(p, scale=1.0):
    try:
        return float(read(p)) / scale
    except (TypeError, ValueError):
        return None


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def iface_label(name):
    if name.startswith(("wl", "wlan")):
        return "wifi"
    if name.startswith(("en", "eth")):
        return "ethernet"
    if name.startswith("tailscale"):
        return "tailscale"
    if name.startswith("br-"):
        return "docker_net"
    return slug(name)


class Collector:
    def __init__(self):
        self.prev_t = None
        self.prev_cpu = None
        self.prev_net = {}
        self.prev_disk = {}
        self.temps = {}
        self.temp_keys = []
        self.fan = None

    def sample(self, slow):
        """Returns (timestamp, metrics for the database, info for the live cards)."""
        now = time.time()
        dt = (now - self.prev_t) if self.prev_t else None
        self.prev_t = now
        m, info = {}, {"cpus": os.cpu_count()}

        # CPU usage, load, uptime, frequency, throttling
        stat = (read(PROC / "stat") or "").splitlines()
        if stat:
            v = [int(x) for x in stat[0].split()[1:9]]
            idle, total = v[3] + v[4], sum(v)
            if self.prev_cpu and total > self.prev_cpu[1]:
                m["cpu.percent"] = round(100 * (1 - (idle - self.prev_cpu[0]) / (total - self.prev_cpu[1])), 1)
            self.prev_cpu = (idle, total)
        load = (read(PROC / "loadavg") or "0 0 0").split()[:3]
        m["load.1"], m["load.5"], m["load.15"] = (float(x) for x in load)
        info["load"] = load
        info["uptime"] = float((read(PROC / "uptime") or "0").split()[0])
        freqs = [num(p) for p in SYS.glob("devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")]
        freqs = [f for f in freqs if f]
        if freqs:
            m["cpu.mhz"] = sum(freqs) / len(freqs) / 1000
        thr = [num(p) or 0 for p in SYS.glob("devices/system/cpu/cpu[0-9]*/thermal_throttle/core_throttle_count")]
        pkg = num(SYS / "devices/system/cpu/cpu0/thermal_throttle/package_throttle_count") or 0
        info["throttle"] = int(sum(thr) + pkg)

        # Memory, swap, pressure
        mem = {}
        for line in (read(PROC / "meminfo") or "").splitlines():
            k, _, rest = line.partition(":")
            parts = rest.split()
            mem[k] = int(parts[0]) * 1024 if parts else 0
        m["mem.used"] = mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)
        m["swap.used"] = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
        info["mem_total"], info["swap_total"] = mem.get("MemTotal", 0), mem.get("SwapTotal", 0)
        for name in ("cpu", "io", "memory"):
            for line in (read(PROC / "pressure" / name) or "").splitlines():
                if line.startswith("some"):
                    m[f"psi.{name}"] = float(line.split("avg10=")[1].split()[0])

        # Network throughput per interface (bytes/s)
        ifaces, seen = [], set()
        net_dir = SYS / "class/net"
        for d in sorted(net_dir.iterdir()) if net_dir.exists() else []:
            if d.name == "lo" or d.name.startswith(("veth", "docker0", "br-")):
                continue
            label = iface_label(d.name)
            if label in seen:
                label = slug(d.name)
            seen.add(label)
            ifaces.append(label)
            rx, tx = num(d / "statistics/rx_bytes"), num(d / "statistics/tx_bytes")
            prev = self.prev_net.get(label)
            if rx is not None and tx is not None:
                self.prev_net[label] = (rx, tx)
                if prev and dt and rx >= prev[0] and tx >= prev[1]:
                    m[f"net.{label}.rx"] = (rx - prev[0]) / dt
                    m[f"net.{label}.tx"] = (tx - prev[1]) / dt
        info["ifaces"] = ifaces

        # Disk I/O per physical disk
        disks = []
        for line in (read(PROC / "diskstats") or "").splitlines():
            p = line.split()
            if len(p) < 14 or not DISK_RE.match(p[2]):
                continue
            name = p[2]
            disks.append(name)
            cur = (int(p[3]), int(p[5]), int(p[7]), int(p[9]), int(p[12]))  # reads, rsect, writes, wsect, io_ms
            prev, self.prev_disk[name] = self.prev_disk.get(name), cur
            if prev and dt:
                d = [max(0, a - b) for a, b in zip(cur, prev)]
                m[f"disk.{name}.riops"] = d[0] / dt
                m[f"disk.{name}.rd"] = d[1] * 512 / dt
                m[f"disk.{name}.wiops"] = d[2] / dt
                m[f"disk.{name}.wr"] = d[3] * 512 / dt
                m[f"disk.{name}.util"] = min(100.0, d[4] / (dt * 10))
        info["disks"] = disks

        # Temperatures and fan (slow BIOS reads, so only every other tick)
        if slow:
            chips = {}
            for d in sorted((SYS / "class/hwmon").glob("hwmon*")):
                chips[read(d / "name")] = d
            temps, keys = {}, []
            for chip in ("coretemp", "dell_smm", "pch_skylake", "ath10k_hwmon"):
                d = chips.get(chip)
                if not d:
                    continue
                for f in sorted(d.glob("temp*_input")):
                    label = read(d / f.name.replace("_input", "_label")) or f.name.split("_")[0]
                    val = num(f, 1000)
                    if val is not None:
                        key = f"temp.{slug(chip)}.{slug(label)}"
                        temps[key] = val
                        keys.append({"key": key, "label": f"{chip} {label}"})
            self.temps, self.temp_keys = temps, keys
            fd = chips.get("dell_smm")
            self.fan = num(fd / "fan1_input") if fd else None
            m.update(self.temps)
            if self.fan is not None:
                m["fan.rpm"] = self.fan
        info["temp_keys"], info["fan_rpm"] = self.temp_keys, self.fan

        # Battery
        b = SYS / "class/power_supply/BAT0"
        if b.exists():
            status = read(b / "status")
            charge, full, design = num(b / "charge_now"), num(b / "charge_full"), num(b / "charge_full_design")
            volt, cur = num(b / "voltage_now"), num(b / "current_now")
            watts = (volt * cur / 1e12) if volt and cur else 0.0
            if status == "Discharging":
                watts = -watts
            ac = read(SYS / "class/power_supply/AC/online") == "1"
            pct = num(b / "capacity")
            m["bat.percent"], m["bat.watts"], m["bat.ac"] = pct, round(watts, 2), 1.0 if ac else 0.0
            info["battery"] = {
                "percent": pct, "status": status, "ac": ac,
                "health_pct": round(100 * full / design, 1) if full and design else None,
                "hours_left": round(charge / cur, 1) if status == "Discharging" and charge and cur else None,
            }
        return now, m, info
