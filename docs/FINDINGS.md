# Findings — how the KD Box II update path was taken apart

Target: **Goldshell KD Box II**, firmware **2.2.7**, MCB **V6_3_5**, model string
`Goldshell-KDBoxⅡ`, hardware `B2.CS.SA`.
Analysis host: local copy of the official firmware plus the running device (`minerd`).

Everything below was verified on the device, not inferred from vendor prose.

---

## 1. The device ships its own OTA signing key

`/root/settings/` (in the rootfs) contains:

| file | what it is |
|---|---|
| `fw_cmpt.pem` | **4096-bit RSA private key**, AES-encrypted PEM, passphrase **`<vendor-passphrase>`** |
| `fw_cmpt.verify.pem` | its public half — the key the device verifies packages with |
| `fw.pem` | another 4096-bit private key, passphrase `<vendor-passphrase>` (not used for OTA) |
| `key.pem` / `pubkey.pem` | device identity pair (used to validate the web JWT) |
| `login.json` | `{"username":"admin","password":"123456789"}` — **plaintext** |

Proof the key is the OTA key: the official `firmware.ius.sig` validates against
`fw_cmpt.verify.pem`, and a signature produced with `fw_cmpt.pem` validates the same way.

```
openssl dgst -sha256 -verify fw_cmpt.verify.pem -signature firmware.ius.sig firmware.ius
→ Verified OK
```

Scheme: **RSA-4096, PKCS#1 v1.5, SHA-256 over `firmware.ius`**.

---

## 2. Web auth (needed to reach the upload endpoint)

Login is a GET with an obfuscated password:

```
GET /user/login?username=admin&password=<hex>&cipher=true
```

The cipher (from the UI bundle) is AES-128-CBC with key `!!!!!!!!!!!!!!!!`, a **zero IV**,
zero padding, hex output — a static key in the JS, i.e. obfuscation, not security.

Response: `{"JWT Token": "..."}`, sent on subsequent calls as

```
Authorization: Bearer <jwt>
```

A wrong password surfaces as **HTTP 500**, not 401 — which makes it look like the endpoint is
broken when it is merely rejecting you.

---

## 3. The update chain (`minerd`)

Decompiled (ARM, Ghidra via REA):

- **`/mcb/uploadimage` handler** — `FUN_0000ce64`
  - validates the `Authorization` header against `/mnt/config/minerd/settings/pubkey.pem`
  - reads multipart fields `file_filename` and the file body
  - `system("rm -rf /dev/shm//minerd/download/*")`, writes the package, then calls verify
- **verify** — `FUN_0000cd68`
  ```c
  sprintf(cmd, "tar -xvf %s/%s -C %s/ && rm -rf %s/%s", dir, pkg, dir, dir, pkg);
  system(cmd);
  sprintf(cmd, "openssl dgst -sha256 -verify %s%s -signature %s.sig %s",
          "/mnt/config//minerd/settings/", "fw_cmpt.verify.pem", ius, ius);
  system(cmd);
  ```
  **Signature only.** No CRC, no container/table validation in this path.
- **package scan** — `FUN_000127f8`: looks for `firmware-<MCB_VERSION>.tgz` in the download
  directory; every *non-matching* entry is deleted via `system("rm -rf %s%s", dir, name)`.
- on success a thread runs `/root/update_arm.sh <package>`, which kills user processes,
  `pivot_root`s into a tmpfs copy of the system and calls `upgrade <package>` then reboots.
- there is a second, cloud-driven variant (`FUN_0001cd7c`) that curls a `firmware.cpb` from a
  URL parameter and runs the same verify — unused here.

### Injection dead ends (checked, not exploitable)
The `rm -rf %s%s` and the tar/openssl command lines take filenames, but those names come from
the device's own fixed naming (`firmware-<ver>.tgz`, `firmware.ius`), not from the upload.
The client `file_filename` is only logged and prefix-checked. So there is **no shell
injection** through the upload — the signature is the real gate.

---

## 4. Container formats

### `.cpb` (what you upload)
```
[ 512-byte header: "firmware-MCB_<ver>.tgz" NUL-padded ]
[ gzip member 0: tar{ firmware.ius (16,630,272), firmware.ius.sig (512) } , padded to 1024 ]
[ gzip member 1: tar{ firmware.ius (44,570,112), firmware.ius.sig (512) } ]
[ trailer , total padded to 1024 ]
```
Both members are required. **Shipping only member 0 makes `minerd` crash and respawn**
(`Minerd start !!!` in the log, no update) — learned the hard way. Member 1 is the hashrate
board image; leave it byte-identical.

The padding between/after members is high-entropy filler, not zeros; content doesn't matter,
only the 1024-byte alignment.

### `firmware.ius` (the inner image)
```
0x00  "+ius"
0x04  format version (2)
0x10  version string, e.g. "2.2.7"
0x18  unknown word
0x1c  image count (6)
0x30  16-byte entries: fields look like [0x8200xxxx, index, 0x40000, size]
...
0x389E00  rootfs squashfs (12,917,584 bytes used, region 12,918,784)
```
Table semantics are **not fully resolved** — which is why the build patches the squashfs
**in place** (same region length, zero-filled tail) and leaves the table and offsets alone.
All differing bytes are verified to fall inside the rootfs region.

### rootfs (squashfs 4.0)
`gzip`, block size `131072`, no xattrs. Stock build parameters that reproduce it:
```
mksquashfs root out.sqfs -comp gzip -b 131072 -no-xattrs -noappend -all-root -nopad
```

---

## 5. Why there was no remote shell before

- `sshd` is installed (OpenSSH 6.4) and `/etc/init.d/S50sshd` starts it at boot, but
  `minerd` runs `/etc/init.d/S50sshd stop` → `killall sshd` a couple of seconds later.
  That single call is the entire "SSH is disabled" mechanism.
- Root password in `/etc/shadow` is a DES hash (`kRmQ3yGkGZThM` in 2.2.7) that matches none
  of the known/default values; port 22 never opens long enough to catch (polled at 80 ms
  across a full cold boot).
- The 2022 CVEs (path traversal, debug interface, hard-coded root creds) are fixed in this build.

The fix used here: neutralise `stop()` in `S50sshd`, add `authorized_keys`, and bake it into
the rootfs.

---

## 6. Recovery notes

- `spiblock0` (SPL + kernel) is never written by this process, so the SoC still boots even if
  the rootfs is bad; recovery is re-uploading the stock `.cpb`.
- If the box never reconnects, recovery needs the **J2 serial header** (3.3 V, 115200 8N1,
  behind a MOSFET level shifter — needs a push-pull GPIO such as an ESP32, a plain USB-TTL can
  read but not type) and a full 32 MB `/dev/mtd0` backup.
- Take that backup before experimenting further:
  `dd if=/dev/mtd0 of=/tmp/full.bin bs=65536` (needs root — which this firmware provides).
