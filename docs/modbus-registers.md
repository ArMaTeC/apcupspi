# Modbus TCP register map

`apc-nmc-modbus.service` serves live UPS data on TCP **502** (config:
`[modbus]` in `nmc.conf` — `bind`, `port`, `unit`). It is **read-only**
unless `allow_write = 1`.

All values are live from NUT/`upsd`. Unsupported variables are simply
absent from the map — a read touching an unmapped address returns
exception `02` (illegal data address).

## Discrete inputs (FC 0x02)

One bit per `ups.status` flag — the same flags shown on the web UI.

| Address | Flag | Meaning |
| ------: | ---- | ------- |
| 0 | `OL` | On line |
| 1 | `OB` | On battery |
| 2 | `LB` | Low battery |
| 3 | `OVER` | Overload |
| 4 | `BYPASS` | On bypass |
| 5 | `CHRG` | Charging |
| 6 | `DISCHRG` | Battery discharged |
| 7 | `RB` | Replace battery |
| 8 | `CAL` | Runtime calibration active |
| 9 | `OFF` | UPS off |
| 10 | `TRIM` | AVR trim (buck) |
| 11 | `BOOST` | AVR boost |
| 12 | `FSD` | Forced shutdown |
| 13 | `ALARM` | Alarm active |
| 14 | `SD` | Shutdown in progress |
| 15 | `HB` | Heartbeat / comms |

## Input + holding registers (FC 0x03 / 0x04)

Identical map on both function codes for BMS compatibility.

| Address | NUT variable | Scale | Notes |
| ------: | ------------ | ----- | ----- |
| 0 | `ups.status` | bitmap | same bits as discrete inputs 0-15 |
| 1 | `battery.charge` | ×1 | % |
| 2 | `battery.runtime` | ×1 | seconds, low word |
| 3 | `battery.runtime` | ×1 | seconds, high word |
| 4 | `input.voltage` | ×10 | V |
| 5 | `output.voltage` | ×10 | V |
| 6 | `input.frequency` | ×100 | Hz |
| 7 | `output.frequency` | ×100 | Hz |
| 8 | `ups.load` | ×1 | % |
| 9 | `ups.realpower` | ×1 | W |
| 10 | `ups.power` | ×1 | VA |
| 11 | `battery.voltage` | ×100 | V |
| 12 | `battery.temperature` | ×10 | °C |
| 13 | `output.current` | ×10 | A |
| 14 | `input.current` | ×10 | A |
| 15 | `battery.charge.low` | ×1 | % threshold |
| 16 | `ups.temperature` | ×10 | °C |
| 17 | `input.voltage.nominal` | ×10 | V |
| 18 | `output.voltage.nominal` | ×10 | V |
| 20-29 | `ups.model` | ASCII | 2 chars/reg, space padded |
| 30-34 | `ups.serial` | ASCII | 2 chars/reg, space padded |
| 35-39 | `ups.firmware` | ASCII | 2 chars/reg, space padded |
| 40-44 | `ups.mfr` | ASCII | 2 chars/reg, space padded |
| 100 | command register | — | write only (see below) |

## Commands (requires `allow_write = 1`)

Write one of these values to **register 100** (FC 0x06 / 0x10), or set
**coil N-1** (FC 0x05, coils 0-9) to fire the matching NUT instant
command:

| Value | Coil | NUT instant command |
| ----: | ---: | ------------------- |
| 1 | 0 | `test.battery.start` — self test |
| 2 | 1 | `calibrate.start` — runtime calibration |
| 3 | 2 | `calibrate.stop` |
| 4 | 3 | `beeper.mute` |
| 5 | 4 | `beeper.enable` |
| 6 | 5 | `beeper.disable` |
| 7 | 6 | `load.on` — turn outlets on |
| 8 | 7 | `shutdown.return` — off then auto-restart |
| 9 | 8 | `shutdown.stayoff` — off, stay off |
| 10 | 9 | `load.off` — immediate outlet off |

> ⚠️ `allow_write` exposes real power control over the network. Enable it
> only on a trusted management network, ideally with the firewall
> restricted to your BMS's CIDR (`[firewall] allow_from`).
