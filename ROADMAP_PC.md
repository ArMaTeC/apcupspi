# Roadmap PC — Audit (1.1.32)

## Summary

Static analysis of apc-nmc-web.py + nmc_* parts, helpers, updater, tests.
No `findUserByUsername`-style ghost calls exist (that error is a template
example). Post-split namespace verified identical to the monolith;
42 tests + ruff + py_compile all green.

## Roadmap

| Task | File | Issue | Fix | Cat | Sev |
| --- | --- | --- | --- | --- | --- |
| Driver restart trigger | nut-driver@myups / net-watchdog / update-check | ~40-min SIGTERM+restart cycles → upsd connect noise | Instrument journal for issuing PID; likely a housekeeping unit | Logic | HIGH |
| Failed LAN logins | ops | Attempts from .215/.221 — verify source | Review nmc.conf users; confirm expected hosts | Security | HIGH |
| Battery age | ops | 11y battery | Replace hardware | Ops | MEDIUM |
| Test monkeypatch leaks | tests/test_units.py | A few tests assign `web.X=` instead of monkeypatch | Convert to monkeypatch fixture | Cleanup | MEDIUM |
| `/config/wifi/hw` dead cap | ROLE_CAPS | Covered by `/config/wifi` prefix already | Remove redundant entry or tighten ordering | Security | POLISH |
| Modbus setvar burst (Oct 23) | apc_modbus | One transient write-timeout cascade | Watch recurrence; add retry in driver? | Logic | MEDIUM |
| Inline-JS CSP | page() | 'unsafe-inline' required by inline assets | Nonce CSP when feasible | Security | POLISH |
| a11y/aria on custom controls | *.js | switches/pills lack roles | Add role/aria-label passes | UI/UX | POLISH |

Severity key: CRITICAL none · HIGH driver-restart + logins · rest MED/POLISH.
