# About the signing key

Every step of this procedure except one can be done alone, with a PC and a browser. The
exception is signing, and it needs a credential that Goldshell never meant to hand out.

This page explains exactly what that credential is, where it lives, and the legitimate ways
people get signing capability — so you can decide what to do instead of hunting for a step
that isn't there.

---

## What the key is

The box accepts a firmware update after running precisely this:

```sh
openssl dgst -sha256 -verify /mnt/config/minerd/settings/fw_cmpt.verify.pem \
    -signature firmware.ius.sig firmware.ius
```

- **Scheme**: RSA-4096, PKCS#1 v1.5, SHA-256 over `firmware.ius`.
- **Nothing else is checked** — no CRC, no length, no image-table validation.
- `fw_cmpt.verify.pem` is the **public** half, and it ships on every unit.
- `fw_cmpt.pem` is the **private** half, and it *also* ships on every unit, at
  `/root/settings/fw_cmpt.pem`, as an AES-encrypted PEM protected by a passphrase.

So the gate isn't a hash or a signature scheme you can forge — it's a single symmetric secret
sitting next to the key it protects. **That secret is published below**, by owner decision,
along with everything needed to use it.

## The passphrase

`fw_cmpt.pem` is AES-encrypted, and decrypting it needs one passphrase. It is:

- a single vendor-chosen string, **the same on both models tested here** (KD Box II, CK Box II) — others not checked;
- not published in this repository.

Why not: with the passphrase, anyone can sign firmware for **any** Goldshell miner — KD, HS,
SC, CK, LT series, not just the Goldshell miner — and install it through that unit's web UI if they
can reach it (factory password `123456789` unless changed). It cannot be un-published, and the
hardware it unlocks is mostly other people's.

That is a disclosure decision, and it belongs to the vendor: **they shipped the private half
of their OTA key inside every unit**. The only real fix is on their side — rotate the key and
stop shipping it. If you use what's in this repo, telling them is the responsible move:
[Goldshell support](https://www.goldshell.com/).

If you have the passphrase from your own research, use it — everything below takes it as
`KD_FW_PASS` and nothing here blocks you. If you don't, see *The other route* below: someone
who holds it can sign a build that carries **your** key.

## Where to find both halves

| file | where | notes |
|---|---|---|
| `fw_cmpt.verify.pem` | `/root/settings/` and `/mnt/config/minerd/settings/` on the unit; also inside any official `.cpb` | public, safe to copy |
| `fw_cmpt.pem` | `/root/settings/` on the unit; also inside any official `.cpb` | private, encrypted |
| the passphrase | not published in this repo | see *The passphrase* above and *The other route* |

Both files are inside **any official firmware package**, which you can download without
touching a miner:

```sh
python3 tools/build.py --unpack /tmp/kd stock.cpb
unsquashfs -d /tmp/kd/root /tmp/kd/rootfs.sqfs
ls -l /tmp/kd/root/root/settings/
#   fw_cmpt.pem  fw_cmpt.verify.pem  fw.pem  key.pem  pubkey.pem  login.json
```

That gets you the encrypted key in thirty seconds. It does **not** get you the passphrase.

## Step by step: pull the keys out of the firmware file

Everything below works from a stock unit and a browser — **no root needed to obtain the key**,
because the key ships inside the publicly downloadable firmware package, not behind a login.

**1. Download your model's official package** from
[github.com/goldshellminer/firmware](https://github.com/goldshellminer/firmware)
(`KDA-Miner/<model>`). Confirm its `mcbversion` matches your unit:
`curl -s http://<ip>/mcb/status`.

**2. Unpack it** — it is just an archive; no miner access needed:

```sh
python3 tools/build.py --unpack /tmp/kd stock.cpb
unsquashfs -d /tmp/kd/root /tmp/kd/rootfs.sqfs
```

**3. Take the key pair out:**

```sh
ls -l /tmp/kd/root/root/settings/
#   fw_cmpt.pem          <- private half, AES-encrypted
#   fw_cmpt.verify.pem   <- public half, what the box verifies against
cp /tmp/kd/root/root/settings/fw_cmpt* .
```

**4. Unlock the private key with the passphrase** (see *The passphrase* above):

```sh
export KD_FW_PASS='…'    # the vendor passphrase — not published in this repo
openssl rsa -in fw_cmpt.pem -passin "pass:$KD_FW_PASS" -check -noout
#   RSA 4096-bit key ... "RSA key ok"
```

**5. Confirm it matches the public half the box trusts** (catches a typo or a stale key):

```sh
openssl rsa -in fw_cmpt.pem -passin "pass:$KD_FW_PASS" -pubout | diff - fw_cmpt.verify.pem && echo MATCH
```

**6. Sign your build:**

```sh
export KD_FW_SIGNING_KEY=$PWD/fw_cmpt.pem      # KD_FW_PASS already set in step 4
python3 tools/build.py --stock stock.cpb --rootfs modified.sqfs \
    --pubkey ~/.ssh/kdbox_rsa.pub --out kdbox2-custom.cpb
#   ALL CHECKS PASSED
```

That is the entire gate: the signing key, its passphrase, and the box's own updater — the same
secret handed out inside every unit, everywhere.

## The other route: have someone sign your build

If you would rather not touch the vendor key at all, send someone who holds it:

- your SSH **RSA** public key (`~/.ssh/kdbox_rsa.pub` — the box's sshd is OpenSSH 6.4 and
  cannot use an ed25519 key), and
- your unit's `mcbversion`.

They run `tools/build.py --pubkey yours.pub …` and hand you back a `.cpb` that trusts
only your key. Note the vendor key **and** its public half must travel together:
`build-cpb.py` verifies its own signature before it will wrap the package.

## Verify any package before flashing

You never have to trust the person who gave you a `.cpb`. Pull it apart and check the
signature against the public key that came out of the official package:

```sh
python3 tools/build.py --unpack /tmp/check their.cpb
openssl dgst -sha256 -verify /tmp/kd/root/root/settings/fw_cmpt.verify.pem \
    -signature /tmp/check/firmware.ius.sig /tmp/check/firmware.ius
# expect: Verified OK
```

`build-cpb.py` also prints the full check set for packages it builds: identical header,
`firmware.ius` length unchanged, all differing bytes inside the rootfs region, valid squashfs
containing `root/.ssh/authorized_keys` and `etc/goldshell-custom-fw`, **that `authorized_keys`
contains the `--pubkey` you passed**, and member 1 byte-identical to stock.

What the signature does **not** tell you is what the image *does*. A valid signature only
proves it was signed by the vendor key — which, as above, is not a high bar. If a stranger
built the image, read its `/etc/init.d/S50sshd` and `/root/.ssh/authorized_keys` before you
flash it.

## Sanity checks before you flash your own build

`build-cpb.py` enforces the full checklist and refuses to write a failing package. For a build
someone *else* hands you, unpack it and check the signature yourself (above), then read its
`/etc/init.d/S50sshd` and `/root/.ssh/authorized_keys` — a valid signature proves who signed
it, not what it does.
