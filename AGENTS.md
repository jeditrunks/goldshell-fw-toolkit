# AGENTS.md — machine brief

For AI agents operating this repo. Optimised for *acting*, not reading. Human-facing detail
lives in `docs/STEP-BY-STEP.md`; every claim here traces back to it.

If you only read one thing: **§2 invariants** and **§5 gates**. Violating either has bricked
or leaked hardware before.

---

## S0 · What this repo does

Turns a **stock Goldshell miner `.cpb`** into a root-SSH image and flashes it over the device's
own authenticated OTA path.

Validated: KD Box II 2.2.7, CK Box II 2.2.2 (both `MCB_V6_3_5`).
Evidence: `docs/VALIDATION.md`.

**Absent by design:** vendor signing key, stock/built firmware images, SSH keys, the key
passphrase. If a task needs any of those, they come from the operator's own device — stop and
ask, do not source them from the network.

---

## S1 · Orient in 60 seconds

| Fact | Value |
|---|---|
| rootfs offset in `firmware.ius` | `0x389E00` both validated models (`--rootfs-offset` to change) |
| squashfs budget rule | `region - 1024`, derived per image, builder refuses to overrun |
| squashfs params | gzip, block 131072, no xattrs, `-all-root` |
| signature verified by device | RSA PKCS#1 v1.5 / SHA-256 over `firmware.ius` **only** |
| container | 512-byte name header + 2 gzip members, 1024-aligned; member 1 never touched |
| package filename | must equal `firmware-<mcbversion>.tgz` (from `/mcb/status`) |
| SSH | OpenSSH 6.4, RSA only, needs `-o PubkeyAcceptedAlgorithms=+ssh-rsa` |
| marker file | `/etc/goldshell-custom-fw` (older images: `/etc/kdbox-custom-fw`) |

Read order for context: `docs/STEP-BY-STEP.md` → `docs/TROUBLESHOOTING.md` → `docs/SIGNING-KEY.md`
→ `docs/FINDINGS.md` (mechanism) → `docs/PROCEDURE.md` (build internals).

---

## S2 · Invariants — never violate

1. **Never commit** `keys/`, `*.pem`, `*.cpb`, `*.ius`, `*.sqfs`. `.gitignore` blocks them;
   do not override it.
2. **Never publish the vendor signing key or its passphrase**, in any form: code default,
   doc example, commit message, test fixture.
3. **Never rename a package to force `mcbversion`.** The updater deletes non-matching names —
   a mismatch is a harmless no-op, a rename can brick.
4. **Never touch member 1** (hashboard image). Byte-identical or abort.
5. **Patch in place** — rootfs region length unchanged, zero-filled tail.
6. **Never re-flash blindly.** If a flash did not change the firmware version, diagnose first
   (``docs/TROUBLESHOOTING.md`` §1); a second upload fixes nothing and costs a boot cycle.
7. **Ask before flashing.** Analysing and building is cheap; a flash reboots someone's miner.

---

## S3 · Command cookbook

All exits must be checked; every step prints evidence — quote it, don't summarise it.

```sh
# 1. target identity — must return JSON with mcbversion
curl -s -m 6 http://<miner-ip>/mcb/status

# 2. container layout — must show "squashfs magic ... (ok)" and region size
./tools/build.py --inspect stock.cpb

# 3. carve parts + find the key — must print "signing key FOUND"
./tools/extract.py --cpb stock.cpb --out build/ --unsquashfs
cp build/rootfs/root/settings/fw_cmpt{,.verify}.pem keys/

# 4. key sanity — must print "Verified OK"
openssl dgst -sha256 -verify keys/fw_cmpt.verify.pem \
    -signature build/firmware.ius.sig build/firmware.ius

# 5. build — must print "ALL CHECKS PASSED"; note the headroom figure
GOLDSHELL_KEY_PASS=... ./tools/build.py --stock stock.cpb --out custom.cpb \
    --pubkey keys/id_rsa.pub --signing-key keys/fw_cmpt.pem

# 6. independent verify — must print 12/12
./tools/verify.py --stock stock.cpb --new custom.cpb \
    --verify-key keys/fw_cmpt.verify.pem --pubkey keys/id_rsa.pub

# 7. dry run — must show the matching filename + "login ok"
KD_DRY_RUN=1 ./tools/flash.sh <miner-ip> custom.cpb

# 8. flash — ask the operator first
./tools/flash.sh <miner-ip> custom.cpb

# 9. confirm root — must print uid=0 and the marker
ssh -i keys/id_rsa -o IdentitiesOnly=yes -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    root@<miner-ip> 'id; cat /etc/goldshell-custom-fw'
```

`GOLDSHELL_KEY_PASS` has no default. If it is unset, the build refuses — that is correct
behaviour, not a bug to work around.

---

## S4 · Decision table (symptom → first action)

| Symptom | Meaning | Do |
|---|---|---|
| login returns **HTTP 500** | wrong web password (not a broken endpoint) | stop; get the real password or use the GUI |
| box stops answering mid-upload | normal — userspace killed for the flash | wait ~4 min, poll `/mcb/status`, **do not re-upload** |
| ping ok, `http=000`, `4028/4068` refused | mid-update window | wait; this is expected |
| firmware version unchanged after flash | updater rejected the package | read `TROUBLESHOOTING.md` §1, check member count/name |
| box never returns after 4 min | rootfs may not have mounted | one power cycle; then serial header (out of scope) |
| SSH `Permission denied (publickey)` | image trusts a different key | check the build's `--pubkey`; rebuild |
| SSH negotiation failure | OpenSSH 6.4 | add `-o PubkeyAcceptedAlgorithms=+ssh-rsa` |
| miner API refused **but web ok** | miner not started | check pool config (`TROUBLESHOOTING.md` §4) |
| `ckbminer` absent, 4028 refused | **no pool configured** — expected | add a pool; not a flash failure |
| `Accepted 0` for the first minutes | normal at CKB difficulty | watch `Hardware Errors` instead |
| `I2C0 ... not acknowledged` every few s | `minerd` polling an idle board | ignore unless hashing also fails |
| `bytes over budget` at build | rootfs too full | shrink more, or rebuild with a smaller edit set |

Ports differ per model: the KD Box II exposes its miner API on **4068**, the CK Box II on
**4028**. Probe both before concluding the miner is down.

---

## S5 · Gates — what must pass before a flash

Do not proceed if any of these is false:

- [ ] `--inspect` shows `(ok)` squashfs magic at the expected offset
- [ ] package header name == `firmware-<mcbversion>.tgz` for the **target** unit
- [ ] stock `.sig` verifies against the shipped `fw_cmpt.verify.pem` (proves the key is the OTA key)
- [ ] build reports `ALL CHECKS PASSED` with positive headroom
- [ ] `verify.py` reports **12/12**, specifically:
  - `signature verifies against vendor public key` — the device will accept it
  - `all differing bytes inside the rootfs region` — nothing else was rewritten
  - `member 1 + trailer byte-identical` — container repacked correctly
  - `authorized_keys matches the supplied key` — you can actually log in afterwards
- [ ] dry run shows the same filename the target expects and `login ok`
- [ ] operator has explicitly approved the flash
- [ ] stock package is on hand as the rollback

What each gate catches is documented in `docs/STEP-BY-STEP.md` §5; if you cannot explain a
gate's failure, you are not ready to flash.

---

## S6 · Post-flash acceptance

```sh
ssh ... 'id; cat /etc/goldshell-custom-fw'          # uid=0 + marker
curl -s http://<miner-ip>/mcb/status                # box answers
# miner API (port per model): {"command":"devs"} / "summary"
```

Healthy = `uid=0`, marker present, `/mcb/status` answers, and once a pool exists:
`Status Alive`, hashrate climbing, `Hardware Errors` flat. First share can take minutes.

Report back: model, `mcbversion`, package sha256, gate results, post-flash `uid`/marker, and
miner counters. Never report "should work" — quote the output.

---

## S7 · File map

| Path | Purpose |
|---|---|
| `tools/build.py` | stock `.cpb` → root-SSH `.cpb` (also `--inspect`) |
| `tools/extract.py` | carve members / `firmware.ius` / rootfs; locate `fw_cmpt.pem` |
| `tools/verify.py` | independent verifier; shares no code with the builder |
| `tools/flash.sh` | authenticated OTA upload (`KD_DRY_RUN=1` for a dry run) |
| `docs/STEP-BY-STEP.md` | the procedure of record |
| `docs/TROUBLESHOOTING.md` | symptom → meaning → action, + model differences |
| `docs/SIGNING-KEY.md` | the key, where it lives, disclosure policy |
| `docs/FINDINGS.md` | container + `minerd` update-chain analysis |
| `docs/PROCEDURE.md` | build internals: offsets, budget, shrink list |
| `docs/VALIDATION.md` | observed evidence, both models |

---

## S8 · Scope

Modifying **hardware the operator owns**. No affiliation with Goldshell or Intchains, no
warranty. Do not use this on devices you were not asked to modify, and do not turn it into a
generic unlock tool for third-party hardware.
