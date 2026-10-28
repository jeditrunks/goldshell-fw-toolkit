# Goldshell firmware toolkit

Rebuild a Goldshell miner's rootfs from a **stock vendor package** and get permanent root
SSH on hardware you own.

**Status: verified on hardware.**

| Model | Firmware | MCB | Result |
|---|---|---|---|
| KD Box II | 2.2.7 | `MCB_V6_3_5` | `uid=0(root)` over SSH, survives cold boot, mining unaffected |
| CK Box II | 2.2.2 | `MCB_V6_3_5` | `uid=0(root)` over SSH, box came back and resumed hashing |

Full evidence — checksums, the 12/12 verification output, hashrate and share counters after
the flash: **[docs/VALIDATION.md](docs/VALIDATION.md)**. The KD Box II proved the approach;
the CK Box II proved it generalises.

## Documentation

| Doc | What it is |
|---|---|
| **[docs/STEP-BY-STEP.md](docs/STEP-BY-STEP.md)** | the whole process, numbered, with expected output and failure modes |
| [docs/VALIDATION.md](docs/VALIDATION.md) | what was actually observed on both models |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | symptom → what it means → what to do, plus model differences |
| [docs/SIGNING-KEY.md](docs/SIGNING-KEY.md) | the vendor key: what it is, where it lives, why the passphrase is not here |
| [docs/FINDINGS.md](docs/FINDINGS.md) | how the container and the update chain work |
| [docs/PROCEDURE.md](docs/PROCEDURE.md) | build internals: offsets, budgets, shrink list |
| [docs/WEBGUI.md](docs/WEBGUI.md) | flashing from the web UI instead of the CLI |
| **[AGENTS.md](AGENTS.md)** | machine brief for AI agents: invariants, cookbook, decision table, pre-flash gates |

---

## What this repository deliberately does **not** contain

- **No vendor signing key.** The tool needs one; you extract it from your own unit.
- **No firmware images** — not stock, not built.
- **No SSH keys.**

That is the point. The build is possible because the OTA signing key ships *inside*
each device's rootfs — but shipping that key around turns interoperability tooling into a
universal unlock. Extract it locally, keep it local.

---

## What you need

1. The **stock `.cpb`** for your model and firmware version, from the vendor.
2. The **OTA signing key**, pulled from your own unit's rootfs:
   `/root/settings/fw_cmpt.pem` (AES-encrypted PEM). Its public half,
   `fw_cmpt.verify.pem`, is what the device checks signatures against.
3. An **SSH public key** you hold the private half of.
4. `squashfs-tools`, `openssl`, `python3`, `curl`.

Put them somewhere ignored by git (`keys/` and `*.cpb` are already in `.gitignore`).

---

## Quick start

**The full procedure — every command, expected output, and the failure modes — is
[docs/STEP-BY-STEP.md](docs/STEP-BY-STEP.md).** Read that first; this is the compressed form.

```sh
# 1. identify the target and its mcbversion
curl -s http://<miner-ip>/mcb/status

# 2. look at the container without changing anything
./tools/build.py --inspect stock.cpb

# 3. carve out the rootfs and the vendor signing key it ships
./tools/extract.py --cpb stock.cpb --out build/ --unsquashfs
cp build/rootfs/root/settings/fw_cmpt{,.verify}.pem keys/

# 4. build
GOLDSHELL_KEY_PASS=... ./tools/build.py \
    --stock      stock.cpb \
    --out        custom.cpb \
    --pubkey     keys/id_rsa.pub \
    --signing-key keys/fw_cmpt.pem

# 5. verify independently before it goes near a device
./tools/verify.py --stock stock.cpb --new custom.cpb \
    --verify-key keys/fw_cmpt.verify.pem --pubkey keys/id_rsa.pub

# 6. flash over the authenticated OTA path
./tools/flash.sh <miner-ip> custom.cpb
```

Then:

```sh
ssh -i keys/id_rsa -o IdentitiesOnly=yes -o PubkeyAcceptedAlgorithms=+ssh-rsa root@<miner-ip>
```

The legacy algorithm flag is mandatory — these boxes run **OpenSSH 6.4** (RSA only).

Flashing takes the miner offline for ~3.5 minutes and reboots it. Keep the stock package:
it is your rollback.

---

## How it works

```
stock .cpb
  └─ 512-byte name header + gzip members (1024-aligned)
       └─ member 0: firmware.ius (+ .sig)      <- rootfs lives here
            └─ squashfs at 0x389E00
                 └─ unsquashfs -> 4 edits -> mksquashfs (stock params)
       └─ member 1: hashboard image            <- never touched
```

The edits are deliberately minimal:

1. `root/.ssh/authorized_keys` — your key, `0700`/`0600`.
2. `etc/init.d/S50sshd` — `stop()` emptied. The management daemon (`minerd`) calls
   `S50sshd stop` seconds after boot; that single call *is* the entire "SSH is disabled"
   mechanism on these boxes.
3. `etc/sshd_config` — `PermitRootLogin yes`.
4. `/etc/goldshell-custom-fw` — a marker so you can prove which image is running.

Then the new squashfs is patched into `firmware.ius` **in place** (region length unchanged,
zero-filled tail), the signature is regenerated with the vendor key, and the container is
rewrapped with member 1 byte-identical.

The device's update path verifies **only** the RSA signature on `firmware.ius` — no CRC, no
container/table validation. That is why an in-place patch is sufficient.

---

## Model support

`--rootfs-offset` defaults to `0x389E00`, which both verified models use. The squashfs
budget is derived per image (`region - 1024`), so a different model needs only a correct
offset and enough headroom — the builder reports the headroom and refuses to overrun.

Where room is needed, one vendor font
(`root/yottamc/fonts/materialdesignicons-webfont.*.eot`, ~1 MB) is dropped from the web UI
assets. Pass `--no-shrink` to keep it.

If a model's layout differs, `--inspect` tells you what the container actually looks like
before you change anything.

---

## Recovery

`spiblock0` (SPL + kernel) is never written by this process, so the SoC still boots even if
the rootfs is bad — recovery is re-uploading the stock `.cpb`. If the box never reconnects,
the J2 serial header is the fallback (3.3 V, 115200 8N1; it needs a push-pull GPIO level
shifter, a plain USB-TTL can read but not type).

Take a full flash backup the moment you have root:

```sh
dd if=/dev/mtd0 of=/tmp/full.bin bs=65536
```

---

## Scope and intent

This is a tool for **hardware you own**. It is not affiliated with Goldshell or Intchains,
and comes with no warranty — you are modifying a device that will reboot itself and may not
come back.

Do not ship the vendor signing key, and do not use this on hardware you do not own.

MIT licensed — see `LICENSE`.
