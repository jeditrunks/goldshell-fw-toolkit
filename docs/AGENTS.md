---
doc: agent-brief
format: ab/1
repo: jeditrunks/kdbox-ii-root
target: Goldshell Goldshell miner — mcbversion MCB_V6_3_5, firmware 2.2.7, hardware <hardware>
updated: 2026-10-26
read_order: S0, S1, S9
---

# Agent Brief — Goldshell miner root via signed OTA

Format `ab/1`: ten fixed sections, one fact per line, `key: value` where greppable.
No prose. Jump by section ID. `->` = leads to. `!` = mandatory. `x` = never.
Absence of a fact is not permission — see S9.

---

## S0 · Mission

`what:` Root SSH on a Goldshell miner through its own signed firmware updater. No case opening, no wiring, no dd.
`why:` Stock firmware kills sshd at boot; web UI is the only interface. Owners want their hardware back.
`shape:` build rootfs -> patch into firmware.ius -> sign with vendor OTA key -> POST to /mcb/uploadimage -> reboot -> ssh.
`cost:` ~3.5 min downtime per box; config partition is wiped.
`output:` docs + tooling only. No firmware, no binaries, no keys in this repo.

## S1 · Orient in 60 seconds

Run in this order. All read-only until S3.

```
git ls-files                                  # 15 files, no firmware, no keys
sed -n '1,40p' README.md                      # the human summary
python3 tools/build.py --inspect PKG    # container layout of a stock package
curl -s http://$IP/mcb/status                 # target: mcbversion + firmware  (device)
ssh -i KEY -o PubkeyAcceptedAlgorithms=+ssh-rsa root@$IP \
  'id; cat /etc/goldshell-custom-fw; grep -E "^#?(PasswordAuthentication|PermitRootLogin)" /etc/sshd_config'
```

`PKG` = stock `firmware-MCB_V6_3_5.tgz`/`.cpb` for the target unit. Never flash one you did not inventory.
`$IP` answers `/mcb/status` -> device is stock and reachable -> S3 applies.
`ssh` returns `uid=0(root)` -> already done, stop; do not re-flash.

## S2 · Ground truth

All values verified on hardware, not inferred.

**S2.1 box**
`soc: InfoTM iMAPx910, quad Cortex-A9 armv7` · `kernel: Linux 3.10.0-infotm, Buildroot 2014, uClibc 0.9.33.2`
`flash: 32MB SPI NOR` · `spiblock0: SPL+kernel` · `spiblock1: rootfs` · `spiblock2: config (3MB, writable)`
`asic: ICA585 x36, 18 chains of 2, /dev/spidev1.0, plugin 114` · `hash: blake2s only` (S8)
`sshd: OpenSSH 6.4, RSA only, no ed25519 anywhere in the binary` -> ! authorized_keys key must be **RSA**; an ed25519 key can never log in -> ! `-o PubkeyAcceptedAlgorithms=+ssh-rsa`

**S2.2 update path**
`post: /mcb/uploadimage, multipart field "file", header Authorization: Bearer <JWT>`
`jwt: GET /user/login?username=admin&password=<hex>&cipher=true` -> `{"JWT Token": …}`
`cipher: AES-128-CBC, key !!!!!!!!!!!!!!!!, zero IV, zero padding, hex`
`wrong password: HTTP 500, not 401` -> not a server fault
`filename: firmware-<mcbversion>.tgz` (daemon renames; the *contents* are what matter)
`verify: signature only` -> `openssl dgst -sha256 -verify fw_cmpt.verify.pem -signature firmware.ius.sig firmware.ius` -> no CRC, no table check
`timing: POST ~7s, box off-net ~2.5min, back ~3.5min`
`side effect: config partition wiped` -> pools gone, admin password back to 123456789

**S2.3 cpb container**
`+0x000: 512B header "firmware-MCB_V6_3_5.tgz", NUL padded`
`+0x200: gzip member0 -> tar{ firmware.ius 16630272, firmware.ius.sig 512 }, pad to 1024`
`+…:     gzip member1 -> tar{ firmware.ius 44570112, firmware.ius.sig 512 } + trailer`
! both members required -> member1 byte-identical -> located by scanning next gzip magic, not arithmetic
x single-member package -> minerd crashes/respawns (`Minerd start !!!`), no update

**S2.4 firmware.ius**
`0x00 "+ius"` · `0x04 fmt=2` · `0x10 version "2.2.7"` · `0x1c count=6` · `0x30 16B entries (tag,idx,0x40000,size)`
`0x389E00: rootfs squashfs` -> region 12,919,808B to EOF; stock image 12,918,848B -> ~960B slack
`unsquashfs -s "filesystem size" 12,917,584` -> logical, not occupied; ignore for sizing

**S2.6 attack surface** (factory default, unchanged by this procedure)
`wifi: enabled` -> `hostapd` runs an OPEN AP: SSID `Goldshell-<suffix>`, no encryption, `<lan>.0.1/24` + dnsmasq -> anyone in range reaches the web UI
`fix: disable wifi if wired` -> GUI toggle or `PUT /mcb/wifisetting {"enable":false}`; stock ignores the flag (cosmetic toggle) -> our image ships `/usr/bin/wifi-ctl` (inittab respawn) which polls `wifi.json` and converges the radio -> toggle works, off included
`/mcb/wifisetting: unauthenticated` -> exposes scan list (SSID/BSSID/security mode) + wifi state; NOT a passphrase (there is none until you set one)
`admin password: 123456789` until changed; UI is plain HTTP; login "encryption" is a static key in the page source
! after flashing: wifi off, admin password changed, box off the open internet

**S2.5 rootfs**
`squashfs 4.0, gzip, block 131072, no xattrs`
`repack: mksquashfs root out.sqfs -comp gzip -b 131072 -no-xattrs -noappend -all-root -nopad -Xcompression-level 9`
! `-all-root` (else daemons cannot read config) · ! `-nopad` (else region overflow)
`mods: root/.ssh/authorized_keys 600/700 (RSA pubkey — this sshd has no ed25519) · S50sshd stop() neutralised · sshd_config PermitRootLogin yes, PasswordAuthentication no, UseDNS no · /etc/goldshell-custom-fw marker`
`fit: drop root/yottamc/fonts/materialdesignicons-webfont.64d4cf64.eot (~1MB, IE-only)` -> frees ~470KB
x drop anything minerd/xminerd/kdaminer links against

## S3 · Procedure

Each step: `do` -> `want` -> `stop-if`.

**S3.1 unpack** `do: python3 tools/build.py --unpack DIR PKG` -> `want: header.bin, firmware.ius, firmware.ius.sig, member1_and_trailer.bin, rootfs.sqfs` -> `stop-if: no squashfs found`
**S3.2 modify** `do: unsquashfs, apply S2.5 mods, mksquashfs S2.5` -> `want: <= region, uds lists root/.ssh/authorized_keys`
**S3.3 build** `do: KD_FW_SIGNING_KEY=… KD_FW_PASS=… python3 tools/build.py --stock PKG --rootfs SQFS --pubkey KEY.pub --out OUT.cpb` -> `want: ALL CHECKS PASSED` -> `stop-if: any FAIL` (no output written)
`--pubkey` is checked, not trusted: the build reads authorized_keys out of the packaged squashfs and fails if the key is absent. KEY.pub must be RSA, and fw_cmpt.verify.pem must sit beside fw_cmpt.pem.
**S3.4 flash** `do: tools/flash.sh $IP OUT.cpb` or GUI: System -> UNLOCK(123456789) -> Update -> pick file -> UPDATE -> `want: /mcb/status 200 on new firmware` -> `stop-if: wrong mcbversion warning` -> `if the S7 upload-split signature appears and root exists: dd fallback (see ota-signed-flash.md)`
`gui truth: no confirmation dialog; "Update process terminated…" is normal on success — do not re-upload`
**S3.5 verify** `do: S6` -> `want: all gates` -> `stop-if: any gate fails, escalate (S9)`

## S4 · Files

`README.md` human summary + 3-line shape · open first
`docs/STEP-BY-STEP.md` step-by-step with GUI screenshots · when a human is driving
`docs/ota-signed-flash.md` full procedure + every gate · when building/flashing
`docs/signing-key.md` what the key is, 3 routes to get signing done, how to verify a package · when the key is missing
`docs/AGENTS.md` this file · agent entry point
`tools/build.py` inspect/unpack/patch/sign/repack, gates enforced · all builds
`tools/flash.sh` version guard, login, upload, watch; KD_DRY_RUN=1 · all flashes
`docs/img/*.png` GUI screenshots · GUI instructions
`CLAUDE.md` pointer to this file · Claude-family tools

## S5 · Secrets

`vendor key: /root/settings/fw_cmpt.pem on the unit; also inside every official .cpb` (extractable, AES-encrypted)
`public half: fw_cmpt.verify.pem` — same places + /mnt/config/minerd/settings/
`passphrase: NOT in repo` (owner decision; it decrypts the key for every Goldshell model) ->
  if missing, routes in docs/signing-key.md (holder signs your build with your pubkey)
`no firmware, no binaries, no SSH private key in this repo`
`still true:` x paste key material, passphrases or tokens into commits/logs/chat

## S6 · Verify gates

`g1` build prints `ALL CHECKS PASSED`
`g2` header byte-identical to stock
`g3` member0 decompresses to exactly 16,640,000B
`g4` firmware.ius length unchanged; every differing byte inside [0x389E00, +12,919,808)
`g5` `openssl … verify` -> `Verified OK` against vendor public key
`g6` packaged squashfs lists `root/.ssh/authorized_keys` + `etc/goldshell-custom-fw`, and authorized_keys contains the `--pubkey` you passed
`g7` member1+trailer byte-identical to stock; member1 starts 1024-aligned
`g8` device: `/mcb/status` returns 200 on the new firmware
`g9` device: ssh -> `uid=0(root)` + marker present
`g10` device: `sync; reboot -f` -> g8+g9 again  (permanence; g9 alone proves nothing)

Report gate IDs and raw output. `g9` without `g10` is not success.
Verifying a third-party .cpb: `--unpack` it, run g5 yourself, then read its `S50sshd` and `authorized_keys` before flashing.

## S7 · Failure signatures

Human-facing version (with recovery procedures): `docs/troubleshooting.md`.

`Minerd start !!!` in log, box stays on old fw -> malformed package, almost always single-member -> rebuild via S3.3
`upload splits into 2 files (16,445,440B + rest, second has garbage name), daemon restarts, no flash, any client` -> box-side uploader regression (fixed offset 0xFAF000, content-independent; not disk space) -> if root available: dd the verified rootfs to spiblock1 with `trap "" HUP`, sync x2, read-back verify, reboot -> else re-uploading will not help
`POST 200, nothing happens, box up` -> updater rejected without writing -> check header name, alignment (S2.3), signature (g5)
`openssl` says `Verification failure` -> wrong key/passphrase, or signed before patching -> sign the patched .ius
`box never returns after flash` -> rootfs did not mount -> physical console access required; do not retry uploads
`ssh: connection refused` right after boot -> sshd not started or S50sshd stop() not neutralised -> check g6 image + reboot
`ssh: no matching host key type` -> missing legacy flags (S2.1)
`ssh: Permission denied (publickey)` with an ed25519 key -> this sshd has no ed25519 support at all; rebuild the image with an RSA key, no flag fixes it
`0 h/s` + `Auto addressing failed` -> wrong algorithm for this silicon (S8)
`Update process terminated…` on a *successful* flash -> socket died, userspace killed -> wait, reload, check fw version

## S8 · Limits

`ICA585 = blake2s only.` Verified by attempting alternatives on hardware:
`blake2s (KDA) kdaminer` -> works ~3.4 TH/s
`blake2b(SC) intminer` -> `SCBOX2 device init… Set TSENSOR_MODE Failed` -> `Auto addressing failed` x5 -> exits
`blake2b xvgminer` -> `unexpected echo for cmd 0x0200` -> `Auto addressing failed`
`blake2s(XVG) xvgminer, real blake2s pool` -> chip hashes: 395 shares submitted, 0 HW errors, all rejected `(Low difficulty share.)` because that port expected blake2b -> nonce->blake2s over a rebuilt 80-byte header unproven, work format open
`blake2s(XVG) xvgminer, mock pool on fw 2.2.7` -> clock walk-down 30->20->10->0 MHz, 0 h/s -> contradicts the real-pool run; unreconciled
`blake2b parts: ICT580 (SC line, SCBox II) and ICA590/ICC590 (HS line, blake2b-sha3)` -> different silicon, not firmware. ICA585/ICA586 (Goldshell miner / KD5, KD5 Pro, KD Box) are all blake2s.
x promise a blake2b conversion on this model, with or without root
x cite ICA586 as blake2b silicon — it is the KD5's blake2s die

## S9 · Rules

`r1` ! verify before flash (S6 g1–g7). Failed gate -> stop.
`r2` ! match mcbversion. Different -> rebuild from that unit's stock package. x rename.
`r3` ! never assume offsets across versions. `0x389E00` is MCB_V6_3_5 only; build-cpb.py finds the superblock by validation.
`r4` ! keep the stock package on disk before flashing. It is the only undo without physical access.
`r5` ! prefer read-only inspection, then a reversible step, then the irreversible one — with human confirmation before the last.
`r6` x open the case, solder, or ask for wiring unless the human asks for it explicitly.
`r7` x write secrets to disk, commits, logs, or chat.
`r8` x claim success from a log line. Success = g8 + g9 + g10.
`r9` ! flash only a device the human named and confirmed is theirs.
`r10` escalate before: flashing a mining box · publishing the key or passphrase · contacting the vendor · deleting a backup or stock package.
