# Validation

What was actually run, on which hardware, and what the box did afterwards. No claims beyond
observed output.

---

## CK Box II — firmware 2.2.2, MCB `MCB_V6_3_5` (2026-10-28)

The interesting case: the tactic was developed on the KD Box II and applied to a *different*
model to prove it generalises.

### Stock input

```
firmware-Goldshell-CKBoxⅡ-2.2.2.cpb     39,138,304 B
sha256 16e450a204a3592b9dab9ca288df7985c09b121c70a9fd9c3a15e76564977d00
```

### Container

```
header name      firmware-MCB_V6_3_5.tgz
gzip members     2  (member 0 = rootfs image, member 1 = hashboard image)
firmware.ius     15,548,928 B, magic +ius, version 2.2.2, images 6
rootfs region    0x389e00 .. end  (11,838,464 B)
squashfs         4.0, gzip, block 131072, no xattrs
```

Same rootfs offset as the KD Box II, so only the budget differed — which is why the builder
derives the budget per image now.

### Build

```
dropped          root/yottamc/fonts/materialdesignicons-webfont.64d4cf64.eot (1,026,396 raw)
image            11,361,371 B  (budget 11,837,440, headroom 476,069)
patched in place 15,548,928 B (length unchanged)
signature        Verified OK
```

### Independent verification (`tools/verify.py`, shares no code with the builder)

```
container header identical                     PASS
both containers have >= 2 members              PASS   stock=2 new=2
member 0 tar members identical                 PASS
ius length unchanged (in-place patch)           PASS   15,548,928 B
signature verifies against vendor public key    PASS   Verified OK
all differing bytes inside the rootfs region    PASS   11,787,114 diffs, 0x389e04..0xed3e3f
member 1 + trailer byte-identical               PASS   23,740,416 B
authorized_keys matches the supplied key        PASS
authorized_keys 0600 / .ssh 0700                PASS   0o600 / 0o700
S50sshd stop() neutralised                      PASS
PermitRootLogin yes                             PASS
marker file present                             PASS
12/12 passed
```

### Flash

Uploaded over the authenticated OTA path (`/mcb/uploadimage`). The box dropped off the
network almost immediately and returned ~3 minutes later.

```json
{"hardware":"20.10.SA","model":"Goldshell-CKBoxⅡ","mcbversion":"MCB_V6_3_5","firmware":"2.2.2"}
```

The version string still reads 2.2.2 — the image preserves it deliberately.

### Result

```
uid=0(root) gid=0(root) groups=0(root),10(wheel)
marker: ssh-rootfs-2.2.2 (kdbox-custom-fw)
Linux qsdk 3.10.0-infotm #1 SMP PREEMPT armv7l GNU/Linux
```

(The marker reads `kdbox-custom-fw` because this image was built before the toolkit was
generalised; current builds write `/etc/goldshell-custom-fw`. `tools/verify.py` accepts
either.)

### Mining afterwards

The miner was left with no pool configured, so `ckbminer` did not start — expected: the
management daemon only spawns it once a pool exists. After a pool was added:

```
ckbminer 5.4.2-unknown | API 3.1
Status            Alive
MHS av            ~2.07 GH/s
clock             715 MHz
fans              2340 / 2280 rpm
temps             84 / 84 °C
Hardware Errors   0
first accepted share at t+242 s
```

So: root, hashing, sharing, zero hardware errors.

---

## KD Box II — firmware 2.2.7, MCB `MCB_V6_3_5` (2026-10-26/28)

The original target. Flashed over the authenticated OTA path, rebooted, `sshd` stayed up
(the stock image's `S50sshd stop` call from `minerd` no longer kills it), and root SSH
survived a cold boot. Mining continued normally.

Same container layout, same `0x389E00` rootfs offset, same signature scheme, ~1 MB more
rootfs headroom than the CK Box II.

---

## Reproducing this on your own unit

```sh
./tools/build.py --inspect stock.cpb                       # confirm layout + budget
GOLDSHELL_KEY_PASS=... ./tools/build.py --stock stock.cpb --out custom.cpb \
    --pubkey keys/id_rsa.pub --signing-key keys/fw_cmpt.pem
./tools/verify.py --stock stock.cpb --new custom.cpb \
    --verify-key keys/fw_cmpt.verify.pem --pubkey keys/id_rsa.pub
./tools/flash.sh <miner-ip> custom.cpb
ssh -i keys/id_rsa -o IdentitiesOnly=yes -o PubkeyAcceptedAlgorithms=+ssh-rsa root@<miner-ip> 'id'
```

If `--inspect` reports a different rootfs offset or a budget too small for your edits, stop
there and read `docs/PROCEDURE.md` before building.

---

## Flash readback (CK Box II, after the fact)

What is on the device is the image that was built — verified by reading the partition back and
comparing hashes against the uploaded package:

```
/dev/spiblock1 body (11,361,371 B)   f7e84d1c3fca9cf84f93073ce3d2446a
package          body (11,361,371 B)  f7e84d1c3fca9cf84f93073ce3d2446a   match
/dev/spiblock1 tail (477,093 B)      0xFF (erased) — the updater writes only the fs image
```

On-device state matches the build's edits:

```
/etc/kdbox-custom-fw                ssh-rootfs-2.2.2 (kdbox-custom-fw)   <- image predates the rename
/root/.ssh/authorized_keys          the build's key, 0600
/etc/init.d/S50sshd stop()          neutralised (no killall sshd)
/etc/sshd_config                    PermitRootLogin yes / PasswordAuthentication no
```

See `docs/FINDINGS.md`, *What actually lands on flash*, for why the trailing region does not
match the package.

---

## Password-enabled build (CK Box II, flashed 2026-10-28)

Second flash on the same unit, this time with `--root-password 'CkBox123'` (DES, 8 chars) to
prove the password path works on real hardware rather than only offline:

```
package   firmware-CKBox2-pw-2.2.2.cpb   38,672,384 B
sha256    57c7609249977d022d570cb5d9b77a38210f2dd60e49709c67889d519581ce96
gates     12/12 (verify.py)
flash     down 05:42:25 .. returned 05:45:33 (~3m)
```

After reboot:

```
marker            ssh-rootfs-2.2.2 (goldshell-custom-fw)
PasswordAuthentication yes
root hash         08zPiprD.6ukA   (13-char DES, matches the build)

key login         uid=0(root)                   <- fallback intact
password login    uid=0(root)                   <- sshpass, PubkeyAuthentication=no
wrong password    Permission denied
```

`/mnt/config` was wiped as expected: no pool, `ckbminer` absent, port 4028 closed until a pool
is added. That is the documented config-wipe behaviour, not a flash failure.

### Host key stability across a reflash

The image was replaced twice on the same unit. The SSH host key did not change either time:

```
before flash   SHA256:ujkTmCjbKSMtEx+dbLVu/UFrfRXyZbusCxmPaARBCZw
after  flash   SHA256:ujkTmCjbKSMtEx+dbLVu/UFrfRXyZbusCxmPaARBCZw
file           /etc/ssh_host_ecdsa_key, dated Feb 16 2023, 668 B
```

It is baked into the stock rootfs, and `/etc` is mounted read-only, so `sshd` cannot regenerate
it. Every unit flashed from the same stock package therefore shares this host key.

### Reboot persistence (CK Box II, after the password build)

Rebooted the flashed unit to test the persistence claims rather than infer them:

```
down 05:52:22 .. back 05:53:34   (72 s)
PASSWORD OK   uid=0(root)          root hash 08zPiprD.6ukA  (unchanged)
KEY OK        root                 authorized_keys intact
pool          /mnt/config/minerd/settings/pools.json 262 B, url unchanged
ckbminer      running, 4028 Alive, 1.24 GH/s after ~40 s
host key      SHA256:ujkTmCjbKSMtEx+dbLVu/UFrfRXyZbusCxmPaARBCZw  (unchanged)
```

Nothing in the rootfs changed, because nothing can: it is a read-only squashfs. `/mnt/config`
is a separate partition, so pool settings survive a reboot — they are only lost when an image
is flashed.

