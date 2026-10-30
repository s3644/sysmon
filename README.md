# sysmon — live resource monitor for a few Linux machines

A small web dashboard that shows live CPU, RAM, NVIDIA GPU, disk, network and
process use for one or more Linux machines side by side. It updates every 2 seconds.

- **No dependencies.** It uses only the Python 3 standard library. The page is a
  single HTML file with canvas charts and no external libraries.
- **Agentless.** For remote machines the server pipes `sampler.py` over SSH
  (`ssh host python3 - < sampler.py`), so nothing is installed on them.
- **Handles unified-memory GPUs** (e.g. NVIDIA GB10 / DGX Spark), where
  `nvidia-smi` reports no separate VRAM figures.

Tested on Ubuntu 24.04 (x86_64 and aarch64) with Python 3.12.

## Quick start

```bash
git clone https://github.com/s3644/sysmon.git
cd sysmon
python3 server.py            # then open http://127.0.0.1:8765
```

To add remote machines, list them in `hosts.conf`:

```bash
cp hosts.example.conf hosts.conf   # then edit
```

```ini
# Label=target   ("local", or an SSH destination: user@host or a ~/.ssh/config alias)
Workstation=local
GPU server=user@gpu-server.local
```

Every remote host needs:
- passwordless (key-based) SSH from the machine running the server
- `python3` (3.8 or newer)
- `nvidia-smi` on the PATH, for GPU figures (optional)

## Running it in the background

`bin/sysmon` is a small start/stop wrapper:

```bash
bin/sysmon start      # start detached; log in run/sysmon.log
bin/sysmon status
bin/sysmon log
bin/sysmon restart
bin/sysmon stop
```

To call it as plain `sysmon`, link it onto your PATH:
`ln -s "$PWD/bin/sysmon" ~/.local/bin/sysmon`.

Extra arguments (`bin/sysmon start --port 9000`) apply only to that start. To
keep them across restarts, set `SYSMON_ARGS` in `sysmon.env` (see below).

It does not start on boot. To start it on boot, use a systemd user service or
an `@reboot` crontab entry.

### Sharing on the LAN

By default the server listens on `127.0.0.1` only. To let other computers open
the page:

```bash
cp sysmon.example.env sysmon.env      # sets SYSMON_BIND=0.0.0.0; optional SYSMON_ARGS
bin/sysmon restart                    # then open http://<this-machine-ip>:8765
```

A `SYSMON_BIND=...` given on the command line overrides `sysmon.env`.

There is **no login**. Anyone who can reach the port sees host names, user names
and full process command lines, which can include file paths and any secrets
passed as arguments. Expose it only on a trusted network, or put it behind a
reverse proxy with authentication. The page is read-only and offers no way to
change anything on the machines.

## Options

```text
python3 server.py [--host 'Label=target' ...] [--hosts-file PATH]
                  [--bind ADDR] [--port 8765] [--interval 2] [--keep 60]
```

| Option | Default | Meaning |
|---|---|---|
| `--host` | — | Add a host; repeatable. Overrides `hosts.conf` |
| `--hosts-file` | `hosts.conf` next to `server.py` | Host list file; without one, only the local machine is shown |
| `--bind` | `127.0.0.1` | Listen address |
| `--port` | `8765` | Listen port |
| `--interval` | `2` | Seconds between samples |
| `--keep` | `60` | Minutes of history kept in memory |

`bin/sysmon` passes `$SYSMON_ARGS` and any extra arguments to `server.py`.

## What the page shows

For each machine:

| Section | Contents |
|---|---|
| Tiles | CPU %, RAM used/total, GPU %, VRAM, GPU power vs limit, hottest temperature (CPU / GPU / NVMe) |
| Charts | CPU, RAM, GPU %, GPU power, network in/out, disk read/write. Choose 5, 15 or 60 min; hover a chart for exact values |
| CPU cores | One cell per core, darker = busier |
| Disks | Used space per mounted filesystem. Amber ≥ 85 %, red ≥ 95 % |
| GPU processes | PID, name and GPU memory per process |
| Processes | Top 10 by CPU or by RAM (toggle at the top) |

A status pill shows whether each machine is live. If the SSH link drops, the
panel greys out and shows the error, and the server reconnects with backoff
(2 s up to 30 s). A sampler that stops sending data for 30 s (or 5 intervals,
whichever is longer) is killed and restarted. The theme follows the OS setting; the ◐ button switches
between light and dark.

On machines with several GPUs, the tiles, charts and GPU-process table show the
first GPU only.

## Notes

- **History is in memory only.** It is lost when the server restarts. An open
  page refills from the server when it reconnects.
- **Sampling cost is small:** about 1 % of one core per machine, mostly from
  reading `/proc` and calling `nvidia-smi`.
- **CPU temperature** comes from `coretemp`, `k10temp` or `zenpower`. Where none
  exists (many ARM boards), it falls back to the hottest ACPI thermal zone.
- **Disks** lists local filesystems only (ext4, xfs, btrfs, zfs, f2fs, NTFS, FAT,
  exFAT). Network mounts are skipped because `statvfs` on an unreachable NFS or
  CIFS share can hang.
- **Hidden network interfaces:** `lo`, `docker*`, `veth*`, `br-*`, `virbr*` and
  `tailscale*`.

## Files

| File | Role |
|---|---|
| `sampler.py` | Runs on each machine and prints one JSON line of metrics per interval (Linux `/proc`, `/sys`, `nvidia-smi`) |
| `server.py` | Starts one sampler per host (local or over SSH), keeps history, and serves the page plus a Server-Sent Events stream (`/api/state`, `/api/stream`) |
| `index.html` | The dashboard |
| `bin/sysmon` | Start/stop wrapper; reads `sysmon.env` and writes to `run/` |
| `hosts.example.conf`, `sysmon.example.env` | Templates for the git-ignored `hosts.conf` and `sysmon.env` |

To check a sampler on its own:

```bash
python3 sampler.py --interval 1 | head -1
ssh user@host python3 -u - --interval 1 < sampler.py | head -1
```

## License

Apache License 2.0. See [LICENSE](LICENSE).
