# Secure Boot on the card

The web UI reports the card's secure-boot state under
**Configuration → System → Secure Boot**.

## What it is

Raspberry Pi 5 / CM4 (and Pi 4 with a custom eeprom build) support
**signed boot**: the bootloader verifies `boot.img` against an RSA key
burned into OTP before the OS loads. This is a one-way hardware
provisioning step — it can only be done manually, at the console, and
**it cannot be enabled or undone from this interface or remotely**.

On Pi 3 and stock Pi 4 firmware there is no secure-boot path; the UI
reports *not supported on this platform*.

## Enabling it (Pi 5 / CM4, at your own risk)

> ⚠️ Burning OTP is irreversible. Read the official
> [Raspberry Pi secure-boot documentation](https://pip.raspberrypi.com/categories/secure-whitepaper-app-notes)
> before touching `program_secure_boot`.

Outline:

1. Generate a signing key: `openssl genrsa -out boot-signing.key 2048`
2. Build a signed `boot.img` containing kernel + initramfs + cmdline
   (`rpi-make-boot-image` / `rpi-sign-bootcode` from the
   [usbboot](https://github.com/raspberrypi/usbboot) tooling).
3. Provision the public key hash into OTP via a signed
   `boot.img`/`recovery.bin` recovery boot.
4. Set eeprom config `SIGNED_BOOT=1` (`rpi-eeprom-config --edit`).

Once enabled, the card detects the bootloader property and reports
*enabled*.

## What the card does regardless

Even without signed boot the appliance hardens the runtime layer:
systemd unit sandboxing, read-only root filesystem for the web/modbus
services, OOM tuning, kernel watchdog, and signed/checksum-verified
software updates (`verify = strict` + a shipped signing pubkey) — see
the README's Security section.
