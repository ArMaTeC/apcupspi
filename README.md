<!-- markdownlint-disable MD033 MD041 MD036 -->
<div align="center">

# 🔌 APC-UPS-PI

<!-- markdownlint-disable-next-line MD001 -->
### Turn a Raspberry Pi into an APC Network Management Card

*Give any NUT-monitored UPS a real **AP9631 (NMC2)** web UI, SNMP agent and
management card presence — for the price of a Pi.*

[![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-3B%2B%20%7C%204%20%7C%205-A22846?logo=raspberrypi&logoColor=white)](https://www.raspberrypi.com/)
[![OS](https://img.shields.io/badge/OS-Raspberry%20Pi%20OS%20%7C%20Debian%2064--bit-1793D1?logo=debian&logoColor=white)](https://www.raspberrypi.com/software/)
[![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![NUT](https://img.shields.io/badge/NUT-2.8.x-E95420)](https://networkupstools.org/)
[![GitHub last commit](https://img.shields.io/github/last-commit/ArMaTeC/apcupspi)](https://github.com/ArMaTeC/apcupspi/commits)
[![Repo size](https://img.shields.io/github/repo-size/ArMaTeC/apcupspi)](https://github.com/ArMaTeC/apcupspi)

</div>

---

**Contents** · [What is this?](#-what-is-this) · [Features](#-features) ·
[Screenshots](#-screenshots) · [Install](#-install) ·
[Wizard](#-configuration-wizard) · [Alerts](#-alerts) ·
[API](#-api--integration) · [Security](#-security) · [FAQ](#-faq)

## ⚡ What is this?

`APC-UPS-PI` transforms a fresh Raspberry Pi OS install into a complete
**emulated APC Network Management Card** — the kind of management interface
you'd normally need an expensive AP9631 card for:

- Your UPS is monitored locally by **NUT** (USB/serial)
- The Pi serves an **HTTPS web UI** that looks and behaves like a real NMC2
- **SNMP** answers PowerNet-MIB queries exactly like APC hardware, so your
  NMS (Zabbix, LibreNMS, PRTG, UPS-aware shutdown tools, hypervisors…)
  can't tell the difference
- It's reachable as **`hostname.local`** out of the box (mDNS via Avahi)

```mermaid
flowchart LR
    UPS[("🔋 UPS")] -- "USB / serial" --> NUT["upsd :3493"]
    subgraph PI["Raspberry Pi"]
        NUT --> WEB["apc-nmc-web :443"]
        NUT --> SNMP["snmpd + pass_persist :161<br>PowerNet-MIB .1.3.6.1.4.1.318"]
        NUT --> MON["upsmon<br>graceful shutdown"]
    end
    WEB -->|"events"| AL["🔔 Discord · Teams · webhook · email · syslog"]
    NMS["📡 NMS / monitoring tools"] -. "SNMP v1/v2c/v3" .-> SNMP
    YOU["🧑 You"] -. "HTTPS" .-> WEB
```

## ✨ Features

| | |
| --- | --- |
| 🌐 **NMC2 Web UI** | Port `443` (`:80` redirects) — status, logs, control, tests and configuration, backed live by NUT |
| 📡 **SNMP Agent** | Port `161` — APC PowerNet-MIB (`.1.3.6.1.4.1.318`) via `pass_persist`; v1/v2c communities + v3 read-only user; GET/GETNEXT/SET + traps |
| 🔋 **NUT Backend** | `upsd` on `3493` + `upsmon` shutdown controller; `apc_modbus` **built from vendored NUT 2.8.5 source** on the target (prebuilt binary as fallback); auto-detects the UPS USB VID/PID at driver start |
| 🛡️ **Resilience** | Kernel watchdog + `RuntimeWatchdogSec`, panic-reboot sysctl, per-service OOM tuning, `net-watchdog` connectivity recovery (layered ICMP/routed/ARP liveness probes, Wi-Fi firmware wedge recovery, bounded reboots) |
| 🔔 **Alerts** | UPS events pushed to Discord, Teams, HTTP webhook, email (SMTP), syslog or **secure syslog (TLS)** — per-event routing matrix + severity filter + test buttons |
| 🧙 **Setup Wizard** | Interactive first-boot wizard (hostname, credentials, network, UPS, SNMP, alerts, certs) — re-runnable anytime |
| 🔄 **Safe Updates** | Update to the newest GitHub release from the UI — sha256/signature-verified payload, live streaming build console, per-file backups + one-click rollback, global banner when an update exists |
| 📈 **Metrics** | Prometheus `/metrics` endpoint + JSON API + raw `events.txt`/`data.txt` like a real card; optional read-only bearer token for monitoring clients |
| 🏭 **Modbus TCP** | BMS-facing server on `:502` — discrete inputs for status flags, input registers for live readings, optional write access for UPS commands ([register map](docs/modbus-registers.md)) |
| � **MQTT Telemetry** | Periodic JSON publish of the key UPS readings to a broker (`<topic>/state`) for Home Assistant / Node-RED — TLS, auth and a test-publish button |
| ⏱️ **Scheduling** | Weekly self-test and scheduled UPS power-off (`shutdown.return`/`stayoff`) from the Diagnostics page |
| 🧰 **Support Bundle** | One-click `support.tgz` download — sanitized config, events, service states and recent journals, secrets redacted |
| �👥 **User Roles** | 4-tier access (admin / device / network / readonly) with per-page and per-action enforcement, plus **RADIUS** fallback auth for web logins |
| 🛡️ **Network Security** | nftables firewall + wired **802.1X** (EAPoL) configured from the UI, optional chrooted **FTP server**, secure-boot status reporting |
| ❓ **In-App Help** | Full built-in documentation (16 topics — every page, roles, API, glossary, troubleshooting) plus `?` tooltips on form fields |

## 📸 Screenshots

<div align="center">

<img src="docs/img/home.png" width="88%" alt="Home dashboard — battery, runtime, load, state, recent events">

*Home — live charge, runtime, load, input/output and the rolling event log*

<img src="docs/img/data-log.png" width="88%" alt="Data Log — energy stats, power-quality trend, samples">

*Data Log — energy usage, power-quality trend and per-minute samples*

<img src="docs/img/config.png" width="88%" alt="Configuration — thresholds, notifications, sessions, backup">

*Configuration — thresholds, the full alert-channel setup with per-trigger
routing, sessions and backup/restore*

<img src="docs/img/login.png" width="55%" alt="Log On — NMC2-style login">

</div>

<details>
<summary><b>Every page</b> — click to expand the full tour</summary>

<div align="center">

| | |
| --- | --- |
| ![UPS Status](docs/img/ups-status.png) | ![Diagnostics](docs/img/diagnostics.png) |
| *Status — battery, input, output, timers* | *Tests — self-test & runtime calibration* |
| ![UPS Control](docs/img/ups-control.png) | ![Outlet Groups](docs/img/outlet-groups.png) |
| *Control — safe shutdown, audible alarm on/off/mute, PCNS signaling* | *Outlet groups — per-group power control* |
| ![Event Log](docs/img/event-log.png) | ![System](docs/img/about-system.png) |
| *Event log — severity-filtered history* | *About — NMC identity & attached UPS* |
| ![Network](docs/img/about-network.png) | ![Host](docs/img/about-host.png) |
| *Network — addressing, DNS, NTP* | *Host — Pi SoC temp, memory, services* |
| ![UPS Variables](docs/img/ups-variables.png) | |
| *Raw NUT variables behind every page* | |

</div>
</details>

## 🚀 Install

**Requirements:** Raspberry Pi 3B+, 4 or 5 · Raspberry Pi OS (or Debian) —
64-bit recommended; 32-bit works too because the vendored NUT/libmodbus are
compiled on the target (the shipped prebuilt driver is aarch64-only) ·
~2 GB free disk · a UPS supported by NUT.

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

Runs at the end of the install, and can be re-run any time via
`sudo firstboot-wizard.sh`. It collects:

- 🏷️ Hostname + new root password
- 🔑 SSH public key for root (optional)
- 🌍 Network — DHCP or static IP/gateway/DNS
- 🔐 Web-interface admin credentials
- 🔋 UPS name, NUT driver & port, NUT monitor/admin passwords
- 📡 SNMP communities, device location & contact
- 🔔 Alert channels — Discord / Teams / generic webhook, email (SMTP), syslog + minimum severity (optional, with live test sends)
- 🔒 Fresh self-signed HTTPS certificate (auto-generated)

## 🔌 Ports & services

| Port | Service | Purpose |
| ------ | --------- | --------- |
| `443/tcp` | `apc-nmc-web` | HTTPS management UI (`80` redirects) |
| `3493/tcp` | `nut-server` (`upsd`) | NUT daemon — loopback-only by default |
| `161/udp` | `snmpd` | SNMP — PowerNet-MIB subtree |
| `22/tcp` | `sshd` | Admin access |
| `502/tcp` | `apc-nmc-modbus` | Modbus TCP (opt-in — enable in Configuration → System) |
| `21/tcp` | `vsftpd` | FTP (opt-in at install, `nmcftp` chrooted account) |

## 🛠️ Install-time options

| Prompt | What it does |
| -------- | -------------- |
| Hardware hardening | `enable_uart`, hardware watchdog, disable onboard WiFi/BT — radios can be re-enabled later under **System → WiFi Setup → Wireless Hardware** (reboot required) |
| Pi 4 overclock | `arm_freq=1750`, `over_voltage=2`, `gpu_freq=600` (Pi 5 ignores it) |
| `/var/swap` | Creates a dedicated 10G swapfile |
| Argon One | Fan/button daemon + i2c, if you run the Argon One case |

## 🔄 Updating

The card checks this repo once a day (`apc-nmc-update-check.timer`) and
shows the result under **About → System → Software Update** — when an
update exists a banner also appears under the top bar on every page.
**Update Now** applies it: the landing page streams `update.log` —
including the full libmodbus/NUT build — to a live console, and the
in-progress status on the card links back to it. A reboot mid-update
can't leave it hanging: a stale "running" status is detected via the
updater's lock and reported as *interrupted* instead of waiting forever.

From SSH:

```bash
apc-nmc-update.sh --check     # see if an update is available
apc-nmc-update.sh --apply     # download + deploy the latest version
apc-nmc-update.sh --rollback  # restore the newest update backup
apc-nmc-update.sh --status    # last check / last apply result
```

Updates deploy code, web assets and systemd units, and rebuild the
`apc_modbus` driver from the vendored NUT sources exactly like
`install.sh` (falling back to the shipped aarch64 binary when the build
can't run — other architectures are always source-built) — **never**
your `nmc.conf`, TLS keys, NUT or SNMP credentials (which `install.sh`
would regenerate). Replaced files **and** synced directories are backed
up under `/var/lib/apc-nmc/update-backup-*/` (kept: 3) — `--rollback`
or the card's **Roll Back** button restores the newest one.

**Update settings from the UI.** The *Update Settings* card edits the
`[update]` section of `nmc.conf` directly — repository, branch, channel
(`releases`|`branch`), verify mode (`checksum`|`strict`|`0`) and the
daily auto-check. The System page also shows which config keys the
newest defaults ship that your live `nmc.conf` doesn't set (*New Config
Options*), and hosts **API token** management: **Generate** creates a
read-only bearer token for monitoring, **Clear** removes it.

**Release channel.** By default the updater tracks the repo's newest
GitHub release/tag rather than `main` — pushing a tag like `1.1.0`
triggers the `release` workflow, which publishes a reproducible tarball
with a `.sha256` checksum (and a `.sig` when signing is set up, below).
Release notes
appear in the update card. If no releases exist it falls back to
tracking the `VERSION` file on `[update] branch`.

**Signed updates (optional).** Generate a keypair once:

```bash
openssl genpkey -algorithm RSA -out update-signing.key
openssl rsa -pubout -in update-signing.key -out etc/apc-nmc/update-signing.pub
```

Commit the `.pub` (ships to `/etc/apc-nmc/update-signing.pub`), keep
the private key out of the repo and store it as the
`UPDATE_SIGNING_KEY` GitHub secret — the release workflow then attaches
a signature every update verifies with `openssl`. A bad signature
always fails the update; no pubkey shipped = signature checks skipped.

## 🔔 Alerts

UPS events — power loss/restore, low battery, overload, comms loss,
self-test results, alarms — can be pushed to **Discord**, **Teams**, any
**HTTP webhook**, **email (SMTP)** or **syslog**.

Configure channels in the wizard or later in the web UI (*Configuration →
UPS → Notifications*): per-channel test buttons, a minimum-severity filter
and a per-trigger routing matrix (e.g. Discord for power events, email for
everything). Criticals can ping a Discord mention. At boot the service
waits for DNS before dispatching, so early alerts aren't dropped.

## 🧩 API & integration

JSON endpoints on the management UI (behind the web login unless noted):

- `GET /api/v1/vars` — live UPS variables (status, load, battery…)
- `GET /api/v1/commands` — instant commands the UPS exposes
- `GET /datalog.json` — logged power-quality samples
- `GET /events.txt`, `GET /data.txt` — raw event/data logs, same as a real NMC
- `GET /metrics` — Prometheus exposition (localhost or logged-in)
- `GET /healthz` — liveness probe for uptime checks (unauthenticated)
- `GET /ca.crt` — download the generated CA to trust the self-signed cert
- `GET /config/backup` — download `apc-nmc-config.tgz` (config + state + events)
- `POST /config/restore` — restore a saved config bundle

`/api/v1/*` and `/metrics` also accept a read-only bearer token —
generate one from **About → System → Update Settings → API Token** (or
set `api_token` under `[web]` in `nmc.conf`) and send
`Authorization: Bearer <token>` (Nagios/Prometheus-friendly; no web
session needed, changes apply without a restart).

## 🔐 Security

- Web password is stored as **PBKDF2-SHA256** (200k iterations); login is
  rate-limited with lockout, sessions are `HttpOnly`+`Secure`+`SameSite=Lax`
  cookies with CSRF tokens on every POST
- All config/secrets (`upsd.users`, `nmc.conf`, state) are generated at
  install/wizard time — nothing secret ships in this repo
- `upsd` binds loopback-only by default — its auth is plaintext and the
  configured users can issue power-off commands. For remote upsmon
  monitoring, add a LAN `LISTEN` to `upsd.conf` **and** a dedicated
  `upsmon secondary` user — never expose the write-capable users off-box
- The web UI is self-signed; grab `/ca.crt` to pin trust instead of
  clicking through warnings
- Updates verify a `sha256` checksum of the release tarball before
  deploying, and `verify = strict` + a shipped signing pubkey make the
  updater refuse unsigned or tampered payloads entirely
- The API bearer token is read-only — it can query `/metrics` and
  `/api/v1/*` but can't change anything or log in to the UI
- **Roles**: `[users]` accounts in `nmc.conf` carry a role —
  `admin` (full), `device` (UPS ops only), `network` (network config),
  `readonly` (view only). Managed from *Configuration → System → User
  Accounts*; `[radius]` adds PAP fallback auth against a RADIUS server.
- **Firewall**: `[firewall]` drives an nftables input chain
  (`apc-nmc-firewall.service`); SSH/HTTPS/SNMP/mDNS stay open by
  default. **802.1X**: `[dot1x]` runs wpa_supplicant's wired driver.
- **Modbus TCP** (`apc-nmc-modbus.service`, off by default) is
  read-only unless `allow_write = 1` — which exposes real power
  commands, so pair it with the firewall's `allow_from` restriction.
- **FTP** (optional install) uses a chrooted `nmcftp` account confined
  to `/var/lib/apc-nmc/ftp` — no system-user or anonymous access.

## 📁 Repo layout

The repo mirrors filesystem layout — files land exactly where they sit:

```text
install.sh                  installer (payload = this repo)
├── docs/img/               README screenshots
├── usr/local/bin/          apc-nmc-web.py · apc-nmc-agent.py · apc-nmc-modbus.py · web UI assets
├── usr/local/sbin/         apc-nmc-update.sh · apc-nmc-firewall.sh · apc-nmc-dot1x.sh · firstboot-wizard.sh · watchdog/backup tools
├── usr/lib/nut/apc_modbus  fallback prebuilt NUT driver (aarch64)
├── usr/local/lib/          fallback libmodbus.so.5 (runtime dep of apc_modbus)
├── vendor/                 NUT 2.8.5 + NUT libmodbus fork sources (built on target)
├── etc/nut/                NUT config templates (secrets generated, never shipped)
├── etc/apc-nmc/            nmc.conf.default + optional update-signing pubkey
├── etc/systemd/system/     units, timers, hardening & OOM drop-ins
├── etc/logrotate.d/        agent/state log rotation
├── etc/sysctl.d/           panic-reboot + OOM tuning
├── etc/argon/              optional Argon One fan daemon
├── tests/                  pytest unit layer (version conf parsers, config writer)
├── .github/workflows/      CI (lint + tests + real NUT build) · release automation
└── .vscode/                ready-made lint/test/deploy/release tasks
```

## ❓ FAQ

**Which UPSes work?**
Anything NUT supports — `apc_modbus` is the default (APC MODBUS-over-USB),
but `usbhid-ups`, `apcsmart`, `blazer_usb` and friends all work. The wizard
asks which driver/port to use, and `nut-usb-detect` fills in the VID/PID
automatically at driver start.

**Pi 3, 4 or 5?**
All three. `config.txt` uses per-model sections; only the Pi 4 gets the
optional overclock. Pi 5 is 64-bit only; on Pi 3/4 use the 64-bit OS image
for the prebuilt driver path, or 32-bit works via the on-target source
build. The Argon One case prompt only applies to Pi 4/5-era Argon cases —
skip it on Pi 3.

**What are the default web credentials?**
The wizard sets them during install — there is no shipped password. If you
skip the wizard the UI falls back to `apc`/`apc`; change it on the
Configuration page (*Change Web Password*).

**Does it work headless?**
Yes — it's designed as a network appliance. Console is only needed if you
disable WiFi *and* can't reach it via Ethernet.

**Where's my data?**
Runtime state and secrets live outside the repo: `/etc/apc-nmc/nmc.conf`,
`/var/lib/apc-nmc/{state,events,datalog,sessions}.json`, agent log in the
same dir (logrotated). Config backup/restore is built into the UI.

---

<div align="center">

*Not affiliated with or endorsed by APC / Schneider Electric. "APC" and
"PowerNet" are trademarks of their respective owners — used here to
describe emulation compatibility.*

</div>
