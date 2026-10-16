<div align="center">

# 🔌 apcupspi

### Turn a Raspberry Pi into an APC Network Management Card

*Give any NUT-monitored UPS a real **AP9631 (NMC2)** web UI, SNMP agent and
management card presence — for the price of a Pi.*

[![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-4%20%7C%205-A22846?logo=raspberrypi&logoColor=white)](https://www.raspberrypi.com/)
[![OS](https://img.shields.io/badge/OS-Raspberry%20Pi%20OS%20%7C%20Debian%2064--bit-1793D1?logo=debian&logoColor=white)]()
[![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)]()
[![NUT](https://img.shields.io/badge/NUT-2.8.x-E95420?logo=data:image/svg+xml;base64,)]()
[![GitHub last commit](https://img.shields.io/github/last-commit/ArMaTeC/apcupspi)](https://github.com/ArMaTeC/apcupspi/commits)
[![Repo size](https://img.shields.io/github/repo-size/ArMaTeC/apcupspi)](https://github.com/ArMaTeC/apcupspi)

</div>

---

## ⚡ What is this?

`apcupspi` transforms a fresh Raspberry Pi OS install into a complete
**emulated APC Network Management Card** — the kind of management interface
you'd normally need an expensive AP9631 card for:

- Your UPS is monitored locally by **NUT** (USB/serial)
- The Pi serves an **HTTPS web UI** that looks and behaves like a real NMC2
- **SNMP** answers PowerNet-MIB queries exactly like APC hardware, so your
  NMS (Zabbix, LibreNMS, PRTG, UPS-aware shutdown tools, hypervisors…)
  can't tell the difference

Everything is configured by an interactive installer — no hand-editing
configs.

## ✨ Features

| | |
| --- | --- |
| 🌐 **NMC2 Web UI** | Port `443` (`:80` redirects) — status, logs, control, tests and configuration, backed live by NUT |
| 📡 **SNMP Agent** | Port `161` — APC PowerNet-MIB (`.1.3.6.1.4.1.318`) via `pass_persist`; v1/v2c communities + v3 read-only user |
| 🔋 **NUT Backend** | `upsd` on `3493` + `upsmon` shutdown controller; `apc_modbus` **built from vendored NUT 2.8.5 source** on the target (prebuilt binary as fallback) |
| 🛡️ **Resilience** | Kernel watchdog + `RuntimeWatchdogSec`, panic-reboot sysctl, per-service OOM tuning, `net-watchdog` connectivity recovery |
| 💾 **Imaging Tool** | `pi-system-backup.sh` — flashable `.img` of the running system; `--clean` strips secrets for a publishable image |
| 🧙 **Setup Wizard** | Interactive first-boot wizard (hostname, credentials, network, UPS, SNMP, certs) — re-runnable anytime |

## 📸 Screenshots

<!-- Add screenshots here, e.g.:
<div align="center">
  <img src="docs/img/status.png" width="80%">
</div>
-->
*Coming soon — drop captures of the web UI into `docs/img/` and link them here.*

## 🚀 Install

Flash **Raspberry Pi OS (64-bit)** — Imager customisation (hostname, user,
SSH key, WiFi) is honoured — then either:

**Clone & run:**

```bash
git clone https://github.com/ArMaTeC/apcupspi.git
cd apcupspi
sudo ./install.sh
```

**One-liner** (always fetches the latest commit — raw.githubusercontent can
serve a cached copy for a few minutes after pushes):

```bash
curl -fsSL https://github.com/ArMaTeC/apcupspi/archive/refs/heads/main.tar.gz | tar -xz -C /tmp && sudo bash /tmp/apcupspi-main/install.sh
```

## 🧙 Configuration wizard

Runs at the end of the install — and on first boot of clean images — via
`sudo firstboot-wizard.sh`. It collects:

- 🏷️ Hostname + new root password
- 🔑 SSH public key for root (optional)
- 🌍 Network — DHCP or static IP/gateway/DNS
- 🔐 Web-interface admin credentials
- 🔋 UPS name, NUT driver & port, NUT monitor/admin passwords
- 📡 SNMP communities, device location & contact
- 🔒 Fresh self-signed HTTPS certificate (auto-generated)

## 🔌 Ports & services

| Port | Service | Purpose |
| ------ | --------- | --------- |
| `443/tcp` | `apc-nmc-web` | HTTPS management UI (`80` redirects) |
| `3493/tcp` | `nut-server` (`upsd`) | NUT network daemon — monitor remotely |
| `161/udp` | `snmpd` | SNMP — PowerNet-MIB subtree |
| `22/tcp` | `sshd` | Admin access |

## 🛠️ Install-time options

| Prompt | What it does |
| -------- | -------------- |
| Hardware hardening | `enable_uart`, hardware watchdog, disable onboard WiFi/BT |
| Pi 4 overclock | `arm_freq=1750`, `over_voltage=2`, `gpu_freq=600` (Pi 5 ignores it) |
| `/var/swap` | Creates a dedicated 10G swapfile |
| Argon One | Fan/button daemon + i2c, if you run the Argon One case |

## 💾 Backup & imaging

```bash
sudo pi-system-backup.sh           # full clone image  -> /root/pi4-backup-*.img.gz
sudo pi-system-backup.sh --clean   # publishable image -> secrets stripped,
                                   #                     first-boot wizard enabled
```

Flash the `.img.gz` straight to a new SSD/SD with Raspberry Pi Imager or
`dd` — it boots as your system (or as a clean wizard-driven install).

## 📁 Repo layout

The repo mirrors filesystem layout — files land exactly where they sit:

```
install.sh                  installer (payload = this repo)
├── usr/local/bin/          apc-nmc-web.py · apc-nmc-agent.py · web UI assets
├── usr/local/sbin/         firstboot-wizard.sh · pi-system-backup.sh · net-watchdog.sh
├── usr/lib/nut/apc_modbus  fallback prebuilt NUT driver (arm64)
├── usr/local/lib/          fallback libmodbus.so.5 (runtime dep of apc_modbus)
├── vendor/                 NUT 2.8.5 + NUT libmodbus fork sources (built on target)
├── etc/nut/                NUT config templates (secrets generated, never shipped)
├── etc/apc-nmc/            nmc.conf.default + public CA material
├── etc/systemd/system/     units, timers, hardening & OOM drop-ins
├── etc/sysctl.d/           panic-reboot + OOM tuning
└── etc/argon/              optional Argon One fan daemon
```

## ❓ FAQ

**Which UPSes work?**
Anything NUT supports — `apc_modbus` is the default (APC MODBUS-over-USB),
but `usbhid-ups`, `apcsmart`, `blazer_usb` and friends all work. The wizard
asks which driver/port to use.

**Pi 4 or Pi 5?**
Both. `config.txt` uses per-model sections; only the Pi 4 gets the optional
overclock.

**Does it work headless?**
Yes — it's designed as a network appliance. Console is only needed if you
disable WiFi *and* can't reach it via Ethernet.

---

<div align="center">

*Not affiliated with or endorsed by APC / Schneider Electric. "APC" and
"PowerNet" are trademarks of their respective owners — used here to
describe emulation compatibility.*

</div>
