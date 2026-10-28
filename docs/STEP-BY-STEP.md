# Step by step: stock package → root shell

The whole process, in the order it actually has to happen. Every command below was run
against a real CK Box II (2.2.2 / `MCB_V6_3_5`) on 2026-10-28; expected output is what that
run produced. Placeholders: `<miner-ip>` for the box, `keys/` for your local (git-ignored)
key directory.

Budget ~20 minutes, of which ~4 are the flash itself.

---

## 0. What you need before you start

| Item | Where it comes from |
|---|---|
| Stock `.cpb` for **your** model + firmware version | vendor download page |
| `squashfs-tools`, `openssl`, `python3`, `curl` | your distro |
| An SSH keypair | `ssh-keygen -t rsa -b 3072 -f keys/id_rsa` |
| The **passphrase** for the vendor signing key | see step 3 — deliberately not published |

```sh
git clone https://github.com/jeditrunks/goldshell-fw-toolkit && cd goldshell-fw-toolkit
mkdir -p keys
ssh-keygen -t rsa -b 3072 -f keys/id_rsa -N ''      # RSA: these boxes run OpenSSH 6.4
```

`.gitignore` already excludes `keys/`, `*.cpb`, `*.ius`, `*.sqfs`.

---

## 1. Find the box and read its identity

```sh
nmap -sn -PR 10.0.0.0/24            # or whatever your LAN is
```

Look for the device serving a page titled `cloud-box`, then:

```sh
curl -s http://<miner-ip>/mcb/status
```

```json
{"hardware":"20.10.SA","model":"Goldshell-CKBoxⅡ","mcbversion":"MCB_V6_3_5","firmware":"2.2.2"}
```

**`mcbversion` is the field that matters.** The package filename must be
`firmware-<mcbversion>.tgz` or the updater deletes it on arrival and does nothing.

---

## 2. Inspect the stock package

```sh
./tools/build.py --inspect stock.cpb
```

```
   total size       39,138,304
   header name      firmware-MCB_V6_3_5.tgz
   member 0         comp 15,393,330  decomp 15,554,560  align-pad 974
   member 0 tar     [('firmware.ius', 15548928), ('firmware.ius.sig', 512)]
   member 1 + tail  23,740,416 bytes (left untouched)
   firmware.ius     15,548,928 bytes
   ius header       magic +ius  version 2.2.2  images 6
   rootfs region    0x389e00 .. end  (11,838,464 bytes)
   squashfs magic   68737173 (ok)
   sha256           16e450a204a3592b9dab9ca288df7985c09b121c70a9fd9c3a15e76564977d00
```

Check three things before continuing:

1. `squashfs magic ... (ok)` — if it says **NOT a squashfs**, the image uses a different
   rootfs offset; stop and read `docs/FINDINGS.md`.
2. `header name` matches the `mcbversion` from step 1.
3. `rootfs region` is big enough for your edits. The builder keeps 1,024 B in reserve and
   reports headroom later; a stock image with < ~1 MB spare needs the shrink step to work.

---

## 3. Extract the pieces — including the signing key

```sh
./tools/extract.py --cpb stock.cpb --out build/ --unsquashfs
```

```
container     stock.cpb  (39,138,304 B)
header name   firmware-MCB_V6_3_5.tgz
gzip members  2  at ['0x200', '0xeaf400', '0x198d6ec']
wrote         firmware.ius  (15,548,928 B)
wrote         firmware.ius.sig  (512 B)
wrote         rootfs.squashfs  (11,838,464 B from 0x389e00)
wrote         rootfs/
signing key   FOUND: build/rootfs/root/settings/fw_cmpt.pem
```

The device ships **its own OTA signing key** inside the rootfs. Copy both halves out:

```sh
cp build/rootfs/root/settings/fw_cmpt.pem        keys/
cp build/rootfs/root/settings/fw_cmpt.verify.pem keys/
```

Confirm the key is the one that signed this package:

```sh
openssl dgst -sha256 -verify keys/fw_cmpt.verify.pem \
    -signature build/firmware.ius.sig build/firmware.ius
# -> Verified OK
```

`fw_cmpt.pem` is an AES-encrypted PEM. Confirm you have the passphrase (it is the same
string across Goldshell models, and it is **not** in this repository — see
`docs/FINDINGS.md`):

```sh
openssl rsa -in keys/fw_cmpt.pem -passin pass:"$GOLDSHELL_KEY_PASS" -noout -text | head -1
# -> Private-Key: (4096 bit, 2 primes)
```

If that fails, you have no way to sign a build and the flash will be rejected. Stop here.

---

## 4. Build

```sh
GOLDSHELL_KEY_PASS=... ./tools/build.py \
    --stock       stock.cpb \
    --out         custom.cpb \
    --pubkey      keys/id_rsa.pub \
    --signing-key keys/fw_cmpt.pem
```

```
== unsquashfs -> /tmp/kdbox-build-xxxx/root
   641 entries extracted

== apply root mods
   authorized_keys  ssh-rsa AAAAB3NzaC1yc2EAAAADAQAB...
   S50sshd          stop() neutralised
   sshd_config      PermitRootLogin yes, PasswordAuthentication no, UseDNS no
   marker           /etc/goldshell-custom-fw
   dropped          root/yottamc/fonts/materialdesignicons-webfont.64d4cf64.eot (1,026,396 raw)

== mksquashfs (stock parameters)
   image            11,361,371 bytes (budget 11,837,440, headroom 476,069)
   patched in place, 15,548,928 bytes (unchanged length)

== sign with the vendor OTA key
   signature        Verified OK

== rewrap .cpb
   total            38,672,384 bytes

   ALL CHECKS PASSED
```

Four edits only: `authorized_keys`, the emptied `stop()` in `S50sshd`, `PermitRootLogin yes`,
and a marker file. Member 1 (hashboard image) is never touched.

**If it dies with "bytes over budget"**, the rootfs is too full. Pass `--no-shrink` off (the
default already drops one ~1 MB web font) or remove more from `SHRINK` in `docs/PROCEDURE.md`.

---

## 5. Verify before it goes anywhere near the device

`verify.py` shares no code with the builder — a builder agreeing with itself proves nothing.

```sh
./tools/verify.py --stock stock.cpb --new custom.cpb \
    --verify-key keys/fw_cmpt.verify.pem --pubkey keys/id_rsa.pub
```

```
container header identical                     PASS
both containers have >= 2 members              PASS   stock=2 new=2
member 0 tar members identical                 PASS
ius length unchanged (in-place patch)          PASS   15,548,928 B
signature verifies against vendor public key   PASS   Verified OK
all differing bytes inside the rootfs region   PASS   11,787,114 diffs, 0x389e04..0xed3e3f
member 1 + trailer byte-identical              PASS   23,740,416 B
authorized_keys matches the supplied key       PASS
authorized_keys 0600 / .ssh 0700               PASS   0o600 / 0o700
S50sshd stop() neutralised                     PASS
PermitRootLogin yes                            PASS
marker file present                            PASS
12/12 passed
```

Every line matters. `all differing bytes inside the rootfs region` is the one that catches a
builder that quietly rewrote a header field; `member 1 + trailer byte-identical` catches the
container being repacked wrong.

---

## 6. Authenticate to the web UI

`flash.sh` does this for you, but it is worth knowing what it does, because the failure mode
looks like a broken server.

```sh
# password is AES-128-CBC on the wire: static key '!!!!!!!!!!!!!!!!', zero IV, zero padding
C=$(python3 -c 'import sys;p=sys.argv[1].encode();sys.stdout.buffer.write(p+b"\x00"*(-len(p)%16))' '123456789' \
    | openssl enc -aes-128-cbc -K 21212121212121212121212121212121 -iv 00000000000000000000000000000000 -nopad \
    | xxd -p -c 256)

curl -s "http://<miner-ip>/user/login?username=admin&password=$C&cipher=true"
# -> {"JWT Token":"eyJ0eXAiOiJKV1Q..."}
```

**`HTTP 500` means wrong password.** Not a broken endpoint, not a missing route. The factory
credential is `admin` / `123456789`; if the owner changed it, the upload path is closed to you
until you know the new one (or you use the GUI).

---

## 7. Flash

Dry run first — it checks the target, the package name, and the login without uploading:

```sh
KD_DRY_RUN=1 ./tools/flash.sh <miner-ip> custom.cpb
```

```
target   10.0.0.50
package  custom.cpb (38,672,384 bytes)
filename firmware-MCB_V6_3_5.tgz
status   {"hardware":"20.10.SA",...}
login    ok (483 char token)
dry run -- not uploading.
```

Then the real thing:

```sh
./tools/flash.sh <miner-ip> custom.cpb
```

The box drops off the network within a second of the POST and reboots. Expect **~3.5 minutes**.

Two things worth knowing:

- **Interrupting your ssh/terminal does not stop the flash.** The package is already on the
  device by then; it verifies and reboots on its own. If you kill the client, check the box
  rather than re-uploading.
- During the window `ping` may keep answering while `http` returns `000` and port `4028` is
  `connection refused`. That is the expected mid-update state.

`flash.sh` polls until `/mcb/status` answers again and prints the status document.

---

## 8. Confirm root

```sh
ssh -i keys/id_rsa -o IdentitiesOnly=yes -o PubkeyAcceptedAlgorithms=+ssh-rsa root@<miner-ip> \
    'id; cat /etc/goldshell-custom-fw; uname -a'
```

```
uid=0(root) gid=0(root) groups=0(root),10(wheel)
ssh-rootfs-2.2.2 (goldshell-custom-fw)
Linux qsdk 3.10.0-infotm #1 SMP PREEMPT armv7l GNU/Linux
```

The `PubkeyAcceptedAlgorithms=+ssh-rsa` flag is mandatory — the box's OpenSSH 6.4 predates
ed25519 and the newer signature schemes.

**First thing to do once you have root:** back up the flash.

```sh
dd if=/dev/mtd0 of=/tmp/full.bin bs=65536     # keep a copy off the device
```

---

## 9. Confirm it still mines

This is where a good flash can look like a bad one.

**If the box has no pool configured, `ckbminer` does not start at all.** No pool means no
hashing and no API on 4028 — by design, not breakage. Add a pool first.

With a pool:

```sh
curl -s -X POST http://<miner-ip>/mcb/status      # still answers
```

```python
# 4028 is the cgminer-style API
{"command": "devs"}       # Status, Device Elapsed, MHS av, temps, fans
{"command": "pools"}      # URL, Status, Accepted
{"command": "summary"}    # Accepted / Rejected / Hardware Errors
```

A healthy box a few minutes after the flash:

```
Status            Alive
MHS av            ~2.07 GH/s
fans              2340 / 2280 rpm
temps             84 / 84 °C
Hardware Errors   0
first accepted share at t+242 s
```

Shares take minutes at CKB difficulty — `Accepted 0` in the first minute is normal. Hardware
errors climbing is not.

---

## 10. If it goes wrong

- **Box never comes back.** `spiblock0` (SPL + kernel) is never written by this process, so
  the SoC still boots. Re-upload the **stock** `.cpb` the same way.
- **Still nothing.** The J2 serial header is the fallback: 3.3 V, 115200 8N1, and it needs a
  push-pull GPIO level shifter — a plain USB-TTL can read but not type.
- **Wrong model flashed.** The updater deletes packages whose name does not match
  `mcbversion`, so a mismatched package is normally a no-op rather than a brick. Do not rename
  to force it; rebuild from that unit's own stock package.

---

## 11. Rollback

Keep the stock package. Flashing it back through the same path restores the vendor image —
you lose root, which is the point.

```sh
./tools/flash.sh <miner-ip> stock.cpb
```

---

## Appendix: the order, compressed

```sh
curl -s http://<ip>/mcb/status                          # 1. identity + mcbversion
./tools/build.py --inspect stock.cpb                    # 2. container layout
./tools/extract.py --cpb stock.cpb --out build/ --unsquashfs   # 3. key + rootfs
openssl dgst -sha256 -verify keys/fw_cmpt.verify.pem -signature build/firmware.ius.sig build/firmware.ius
GOLDSHELL_KEY_PASS=... ./tools/build.py --stock stock.cpb --out custom.cpb \
    --pubkey keys/id_rsa.pub --signing-key keys/fw_cmpt.pem    # 4. build
./tools/verify.py --stock stock.cpb --new custom.cpb \
    --verify-key keys/fw_cmpt.verify.pem --pubkey keys/id_rsa.pub   # 5. verify
KD_DRY_RUN=1 ./tools/flash.sh <ip> custom.cpb           # 6. dry run
./tools/flash.sh <ip> custom.cpb                        # 7. flash
ssh -i keys/id_rsa -o IdentitiesOnly=yes -o PubkeyAcceptedAlgorithms=+ssh-rsa root@<ip> 'id'
```
