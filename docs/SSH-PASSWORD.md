# Using a password instead of (or alongside) a key

Short version: **a password has to be baked into the image at build time.** You cannot set one
on a running miner, because the rootfs is a read-only squashfs.

Everything below was verified on a CK Box II (2.2.2 / `MCB_V6_3_5`).

---

## Why `passwd` does not work on the device

```
# passwd root
Retype password:
passwd: /etc/passwd: Read-only file system
passwd: can't update password file /etc/passwd
```

`/` is `/dev/root` mounted `squashfs (ro)`. `/etc/shadow` is inside that image. There is no
overlay, so `passwd`, `chpasswd`, and hand-editing `/etc/shadow` all fail the same way. This is
not a permissions problem you can `chmod` away.

The stock firmware has the same property — which is why the vendor's own password is a
hash baked into the image, not something you can rotate.

---

## The second constraint: DES only, 8 characters

The box's libc is **uClibc with DES-only `crypt`** — no MD5 (`$1$`), no SHA (`$5$`/`$6$`).
Consequences:

- the hash in `/etc/shadow` must be exactly **13 characters** (2-char salt + 11-char hash);
- the password is **truncated to 8 characters** by the algorithm, silently:

```
crypt("password",   "ab") -> abJnggxhB/yWI
crypt("passwordXYZ","ab") -> abJnggxhB/yWI     # identical: everything after 8 is ignored
```

A hash in a modern format will be written fine and then **rejected at login**, with no useful
error. If password auth fails and the hash starts with `$`, that is why.

---

## Build with a password

```sh
# let the builder hash it (perl / python-crypt / mkpasswd, whichever exists)
GOLDSHELL_KEY_PASS=... ./tools/build.py \
    --stock stock.cpb --out custom.cpb \
    --pubkey keys/id_rsa.pub --signing-key keys/fw_cmpt.pem \
    --root-password 'letmein1'
```

```
   root password    DES hash 40o.n4IEYYeRQ
   sshd_config      PermitRootLogin yes, PasswordAuthentication yes, UseDNS no
   shadow           root hash set (40o.n4IEYYeRQ, 13 chars)
   ALL CHECKS PASSED
```

Or supply a hash you generated elsewhere (useful when the build host has no `crypt`):

```sh
perl -e 'print crypt($ARGV[0], $ARGV[1]), "\n"' 'letmein1' ab     # -> abP/cckPJaUqA
./tools/build.py ... --root-hash abP/cckPJaUqA
```

Setting either flag flips `PasswordAuthentication` to `yes`. Without a flag the image is built
key-only (`PasswordAuthentication no`) — the safer default.

Verify what you built before flashing:

```sh
./tools/extract.py --cpb custom.cpb --out build/ --unsquashfs
grep '^root:' build/rootfs/etc/shadow | cut -d: -f2      # must be 13 chars, no leading $
grep -E '^(PermitRootLogin|PasswordAuthentication)' build/rootfs/etc/sshd_config
```

The hash is verifiable offline — this is what the box will do:

```sh
H=40o.n4IEYYeRQ
perl -e 'print crypt($ARGV[0],$ARGV[1])' 'letmein1' "${H:0:2}"   # == $H
```

Then flash as usual (`docs/STEP-BY-STEP.md` §7).

---

## Connecting with a regular SSH client

The box's host key is **ECDSA P-256**, so ordinary modern clients negotiate fine — unlike the
user-key side, no legacy flag is needed for password auth:

```sh
ssh root@<miner-ip>
# root@<miner-ip>'s password:
```

`PubkeyAcceptedAlgorithms=+ssh-rsa` is only needed when authenticating with an **RSA key**;
it is irrelevant to password auth. Password auth is unaffected by that change.

Automation (the way this repo tests it):

```sh
sshpass -p 'letmein1' ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no \
    root@<miner-ip> 'id'
# uid=0(root) gid=0(root) groups=0(root),10(wheel)
```

Windows:

- **PuTTY**: host `<miner-ip>`, port 22, connection type SSH. If the version is old enough to
  complain about the key exchange, set *Connection → SSH → Kex* to include
  `diffie-hellman-group14-sha1`.
- **Windows OpenSSH** (`ssh.exe`): same command as Linux; no flags needed.

`scp` to an OpenSSH 6.4 server may need the legacy protocol flag: `scp -O ...`.

---

## Security notes — read before enabling this

- **DES is weak and the password is capped at 8 characters.** This is hardware from 2023 whose
  libc never learned anything else; you cannot upgrade the constraint without replacing the
  crypto library in the image.
- **Keys are strictly better.** The image already trusts your key; a password adds a second,
  weaker door. Enable it when a GUI client or a colleague needs in, not by default.
- **Never expose port 22 to the internet.** LAN/VPN only. An 8-char DES password on a public
  port is a matter of hours at worst.
- The password can only be changed by **rebuilding and reflashing**. There is no runtime path.

## Turning password auth back off

Rebuild without `--root-password` / `--root-hash` and reflash. The resulting image keeps your
key, sets `PasswordAuthentication no`, and the old hash in `shadow` is irrelevant again.

---

## What was verified, and what was not

| Claim | How |
|---|---|
| rootfs is read-only, `passwd` fails | run on the device, error captured above |
| DES-only, 8-char truncation | `crypt()` comparisons on the build host |
| `--root-password` bakes a working hash | built an image, hash verified with `crypt()`; wrong password rejected |
| `sshd_config` flips correctly | `grep` on the built rootfs |
| image still passes all gates | `tools/verify.py` — 12/12 |
| **password login over the network** | **not yet exercised on hardware** — would need a flash of a password-enabled build |
