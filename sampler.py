#!/usr/bin/env python3
"""Print one JSON line of system metrics every --interval seconds.

Stdlib only, so it can be piped to a remote host: ssh host python3 -u - < sampler.py
"""
import argparse
import json
import os
import pwd
import re
import socket
import subprocess
import sys
import time

SKIP_NET = ("lo", "docker", "veth", "br-", "virbr", "tailscale")
# Local filesystems only: statvfs on an unreachable NFS/CIFS mount can block forever.
REAL_FS = {"ext4", "xfs", "btrfs", "zfs", "ntfs", "ntfs3", "exfat", "vfat", "f2fs"}
CLK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def read(path, default=""):
    try:  # process names and mount paths may hold any bytes
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return default


def cpu_times():
    out = []
    for line in read("/proc/stat").splitlines():
        if not line.startswith("cpu"):
            break
        v = list(map(int, line.split()[1:]))
        idle = v[3] + v[4]
        out.append((sum(v[:8]), idle))
    return out


def pct(prev, cur):
    res = []
    for (t0, i0), (t1, i1) in zip(prev, cur):
        dt = t1 - t0
        res.append(round(100.0 * (1 - (i1 - i0) / dt), 1) if dt > 0 else 0.0)
    return res


def meminfo():
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0]) * 1024
    total, avail = m["MemTotal"], m.get("MemAvailable", m["MemFree"])
    return {
        "total": total,
        "used": total - avail,
        "avail": avail,
        "cache": m.get("Cached", 0) + m.get("Buffers", 0),
        "swap_total": m.get("SwapTotal", 0),
        "swap_used": m.get("SwapTotal", 0) - m.get("SwapFree", 0),
    }


def temps():
    cpu, nvme = None, []
    zones = []
    for h in sorted(os.listdir("/sys/class/hwmon")) if os.path.isdir("/sys/class/hwmon") else []:
        base = f"/sys/class/hwmon/{h}"
        name = read(f"{base}/name").strip()
        t = read(f"{base}/temp1_input").strip()
        if not t:
            continue
        c = int(t) / 1000
        if name in ("coretemp", "k10temp", "zenpower"):
            cpu = c
        elif name == "nvme":
            nvme.append(c)
    if cpu is None:  # ARM boards (DGX Spark) expose CPU heat via ACPI thermal zones
        tz = "/sys/class/thermal"
        for z in os.listdir(tz) if os.path.isdir(tz) else []:
            t = read(f"{tz}/{z}/temp").strip()
            if z.startswith("thermal_zone") and t:
                zones.append(int(t) / 1000)
        cpu = max(zones) if zones else None
    return {"cpu": cpu, "nvme": max(nvme) if nvme else None}


def num(s):
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return None  # "[N/A]" on unified-memory GPUs


def gpus():
    q = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,clocks.sm,fan.speed,uuid"
    run = lambda *a: subprocess.run(["nvidia-smi", *a, "--format=csv,noheader,nounits"],
                                    capture_output=True, text=True, errors="replace", timeout=5)
    try:
        r = run(f"--query-gpu={q}")
        a = run("--query-compute-apps=gpu_uuid,pid,process_name,used_memory")
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in r.stdout.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 11:
            continue
        mu, mt = num(f[3]), num(f[4])
        out.append({
            "index": int(f[0]), "name": f[1], "util": num(f[2]),
            "mem_used": mu * 2**20 if mu is not None else None,
            "mem_total": mt * 2**20 if mt is not None else None,
            "temp": num(f[5]), "power": num(f[6]), "power_limit": num(f[7]),
            "clock": num(f[8]), "fan": num(f[9]), "uuid": f[10], "procs": [],
        })
    by_uuid = {g["uuid"]: g for g in out}
    for line in a.stdout.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) >= 4 and f[0] in by_uuid and f[1].isdigit():
            m = num(f[3])
            by_uuid[f[0]]["procs"].append({"pid": int(f[1]), "name": os.path.basename(f[2]),
                                           "mem": m * 2**20 if m is not None else None})
    for g in out:
        g["procs"] = sorted(g["procs"], key=lambda p: -(p["mem"] or 0))[:8]
    return out


def disk_io():
    io = {}
    for line in read("/proc/diskstats").splitlines():
        f = line.split()
        name = f[2]
        if os.path.exists(f"/sys/block/{name}/device") or name.startswith("md"):
            io[name] = (int(f[5]) * 512, int(f[9]) * 512)
    return io


def disks():
    seen, out = set(), []
    for line in read("/proc/mounts").splitlines():
        dev, mnt, fs = line.split()[:3]
        mnt = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), mnt)  # \040 = space etc.
        if fs not in REAL_FS or dev in seen or mnt.startswith(("/boot", "/snap", "/var/snap")):
            continue
        seen.add(dev)
        try:
            s = os.statvfs(mnt)
        except OSError:
            continue
        total = s.f_blocks * s.f_frsize
        out.append({"mount": mnt, "total": total, "used": total - s.f_bfree * s.f_frsize})
    return out


def net_io():
    io = {}
    for line in read("/proc/net/dev").splitlines()[2:]:
        name, rest = line.split(":", 1)
        name = name.strip()
        if name.startswith(SKIP_NET):
            continue
        f = rest.split()
        io[name] = (int(f[0]), int(f[8]))
    return io


_users = {}


def user(uid):
    if uid not in _users:
        try:
            _users[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _users[uid] = str(uid)
    return _users[uid]


def proc_times():
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        stat = read(f"/proc/{pid}/stat")
        if not stat:
            continue
        r = stat.rfind(")")
        f = stat[r + 2:].split()
        try:
            uid = os.stat(f"/proc/{pid}").st_uid
            out[int(pid)] = (int(f[11]) + int(f[12]), int(f[21]) * PAGE, stat[stat.find("(") + 1:r], uid)
        except (OSError, ValueError, IndexError):  # process exited mid-read, or an odd entry
            continue
    return out


def cmdline(pid, fallback):
    c = read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()
    return (c or fallback)[:160]


def top_procs(prev, cur, dt, ncpu, n=10):
    rows = []
    for pid, (t, rss, comm, uid) in cur.items():
        cpu = 100.0 * (t - prev[pid][0]) / CLK / dt if pid in prev else 0.0
        rows.append((cpu, rss, pid, comm, uid))
    by_cpu = sorted(rows, key=lambda r: -r[0])[:n]
    by_mem = sorted(rows, key=lambda r: -r[1])[:n]
    fmt = lambda r: {"pid": r[2], "name": r[3], "user": user(r[4]), "cpu": round(r[0], 1),
                     "rss": r[1], "cmd": cmdline(r[2], r[3])}
    return [fmt(r) for r in by_cpu], [fmt(r) for r in by_mem]


def rates(prev, cur, dt):
    return {k: [round((c - p) / dt) for p, c in zip(prev[k], v)] for k, v in cur.items() if k in prev}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    host = socket.gethostname()
    ncpu = os.cpu_count() or 1
    model = next((l.split(":", 1)[1].strip() for l in read("/proc/cpuinfo").splitlines()
                  if l.startswith("model name")), "")
    if not model:  # aarch64 has no "model name"; DMI product name is the best label
        model = read("/sys/devices/virtual/dmi/id/product_name").strip() or os.uname().machine
    c0, d0, n0, p0, t0 = cpu_times(), disk_io(), net_io(), proc_times(), time.monotonic()
    tick = 0
    while True:
        time.sleep(args.interval)
        c1, d1, n1, p1, t1 = cpu_times(), disk_io(), net_io(), proc_times(), time.monotonic()
        dt = t1 - t0
        cp = pct(c0, c1)
        by_cpu, by_mem = top_procs(p0, p1, dt, ncpu)
        load = read("/proc/loadavg").split()[:3]
        sample = {
            "host": host, "ts": time.time(), "cpu_model": model, "ncpu": ncpu,
            "uptime": float(read("/proc/uptime").split()[0]),
            "cpu": cp[0], "cores": cp[1:], "load": [float(x) for x in load],
            "mem": meminfo(), "temp": temps(), "gpus": gpus(),
            "disk_io": rates(d0, d1, dt), "net_io": rates(n0, n1, dt),
            "procs_cpu": by_cpu, "procs_mem": by_mem, "nproc": len(p1),
        }
        if tick % 15 == 0:  # statvfs on a busy HDD is cheap but needn't run every tick
            dk = disks()
        sample["disks"] = dk
        sys.stdout.write(json.dumps(sample, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        c0, d0, n0, p0, t0 = c1, d1, n1, p1, t1
        tick += 1


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
