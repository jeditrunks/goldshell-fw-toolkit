# Troubleshooting and recovery

Everything in this page was hit on real hardware. Format: **symptom → what it means → what to
do.** Work top to bottom inside the section that matches your problem.

If you are an AI agent reading this: the same content is in `docs/AGENTS.md` §S7 in brief form,
and the rules in §S9 apply — especially *verify before claiming success* and *never re-flash
blindly*.

---

## 0. First: how alive is the box?

Run this ladder and act on the first line that answers:

```sh
ping -c1 -W2 <miner-ip>                                  # kernel alive?
curl -s -m 4 http://<miner-ip>/mcb/status                # management daemon (web) alive?
curl -s -m 4 http://<miner-ip>:4068/summary              # miner alive?
ssh -i keys/id_rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@<miner-ip> 'id'   # shell alive?
```

| result | meaning | go to |
|---|---|---|
| ping fails | no power / no link / wrong box | check the obvious first |
| ping ok, web down | booting, updating, or the web daemon died | §2, §4 |
| web ok, miner down | miner not started — usually config | §4 |
| ssh refused but web ok | SSH not enabled / wrong key | §3 |
| everything answers | box is fine; your problem is elsewhere | read on anyway |

---

## 1. Flashing problems

**Clicked UPDATE, box stays up, firmware version unchanged**

The updater rejected the package without writing. Re-uploading the same file will not help.
Check, in order:

1. The log: the box's UI → *System* → logs, or `/var/log/messages` on the box.
   `Minerd start !!!` means the management daemon crashed and respawned.
2. The most common cause: a **single-member `.cpb`** — the updater requires both container
   members. Build with `tools/build.py`; it always ships both.
3. Wrong header name, or the package isn't 1024-byte aligned — let the script build it.

**Upload starts, then "Update process terminated. Please choose the officially issued update
package."**

On a *successful* flash this message is normal and misleading: the daemon kills userspace
mid-upload to write flash, and the page sees the connection die. **Do not upload again.** Wait
~4 minutes, reload, and check the firmware version.

**Every upload fails identically, from the browser and from `curl`**

Seen on units that have run this image for a while: the daemon writes the payload **split
across two files** in `/dev/shm/minerd/download/` — `firmware-<mcbversion>.tgz` at a fixed
16,445,440 bytes, plus a garbage-named file with the rest — and nothing ever flashes. It is
content-independent (we reproduced it with a deliberately corrupted package) and it is not
disk space. Re-uploading will not help.

If you already have a root shell on that unit, flash directly instead —
see [Recovery](#5-recovery) below.

**UPDATE button greyed out**

The web UI is still locked, or no file is selected. Click **UNLOCK** (password is the admin
password — factory `123456789`), then choose the `.cpb` again, and confirm the name *and size*
in the field: the file chooser remembers your last file and will happily offer the stock
package instead of your build.

**Login to the web UI fails, and it looks like a server error**

A wrong password returns **HTTP 500**, not 401. The UI is not broken; the password is wrong.

---

## 2. The box never comes back after a flash

This is the one this page cannot fully rescue: it means the rootfs did not mount.

- The bootloader and kernel live on a different partition and are untouched by this process,
  so the box will still *start* (ping answers) — it just can't mount a rootfs, so no web, no
  SSH.
- A **power cycle** is worth one try: if the write was interrupted rather than corrupt, the
  old rootfs may boot after all. One cycle, not twenty.
- If it still won't come back, the fix needs **physical access to the board's console
  header**. That procedure is out of scope for this repo; do not keep flashing at a box that
  answers only to ping.

This is the risk you accept when flashing. It is why every step before the flash is verified
and why you keep the stock package.

---

## 3. SSH problems

**`Permission denied (publickey)`**

- The image trusts exactly the key that was baked in at build time (`authorized_keys`).
  A different key on your side is a different key, no matter the account.
- Try `-v` and check which key is offered. Then rebuild with the right public key, or ask
  whoever signed your build which key it carries.

**`no matching host key type` / `Unable to negotiate`**

The box runs OpenSSH 6.4: RSA host keys, no ed25519, no modern key exchange. Both legacy
flags are required:

```sh
ssh -i keys/id_rsa \
    -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    root@<miner-ip>
```

**`connection refused` right after boot**

sshd starts at boot and is then killed by the management daemon on *stock* firmware. If your
image still shows this after a reboot, the `S50sshd` patch didn't take — check the marker
file (`cat /etc/goldshell-custom-fw`) to see which image actually booted.

**`connection refused` on port 4068 (the miner API)**

That is the *miner's* API, not SSH — it only listens while the miner runs. See §4.

---

## 4. Mining problems

**Web UI says "Oops! Miner pool is not ready yet" but the box answers**

Reload the page first — it is often a stale view. Then check the miner's own API:

```sh
curl -s http://<miner-ip>:4068/summary | python3 -m json.tool | head -20
```

`accepted_shares` climbing means it is mining; the dashboard lags.

**Miner not running after a flash**

The flash wipes the config partition: pools are gone. Re-add your pool in the web UI
(*Miner* → pool settings), or:

```sh
curl -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"url":"stratum+tcp://kda.antpool.com:8026","user":"<worker>","pass":"x","pool-priority":0}' \
    http://<miner-ip>/mcb/newpool
```

**`0 h/s` with `Auto addressing failed` in the log**

Wrong algorithm for this silicon. The Goldshell miner's `ICA585` chips are blake2s only — blake2b
(Sia/Handshake) needs different parts. See [STEP-BY-STEP.md](STEP-BY-STEP.md),
*What this does not buy you*.

**`0 h/s` but no chip errors, and the log shows jobs arriving**

Check the pool actually speaks this firmware's dialect: one pool's `mining.notify` was
rejected wholesale by one of the vendor binaries while another pool worked. Try the pool the
box was configured for, then the other way around.

**Mining won't start even though the pool is saved**

The pool list on disk can disagree with what the supervisor has in memory:

```sh
cat /mnt/config/minerd/settings/pools.json     # needs "active": true, one entry is enough
kill $(pidof xminerd)                          # init respawns it; it re-reads the file
```

*(Requires root, i.e. this image.)* The web API's add-pool call tends to **append** a
duplicate rather than replace — check for two entries and clean up if so.

---

## 5. Recovery

**Roll back to stock firmware** — flash the stock `.cpb` through the same updater (web UI or
`tools/flash.sh`). You lose root (the stock image kills sshd at boot and doesn't carry
your key); the box returns to exactly what Goldshell shipped. Keep that package on hand
*before* you start — it is the only rollback that needs no hardware access.

**The uploader is broken on this box** (§1, "every upload fails identically") — with a root
shell, write the repacked rootfs directly to the rootfs partition instead:

```sh
# on your PC: verify what you're sending
md5sum modified.sqfs
# copy it over, then flash detached so a dropped SSH session can't kill the write
scp modified.sqfs root@<miner-ip>:/dev/shm/
ssh root@<miner-ip> 'sh -c "trap \"\" HUP; \
    dd if=/dev/shm/modified.sqfs of=/dev/spiblock1 bs=65536; \
    sync; sync; reboot"'
```

- `/dev/spiblock1` **is** the rootfs partition (18,874,368 bytes; `/dev/root` symlinks to it).
- Read it back and compare before rebooting if you want certainty:
  `dd if=/dev/spiblock1 bs=65536 count=$(($(stat -c%s /dev/shm/modified.sqfs)/65536)) | md5sum`.
- This is the same write the vendor's updater performs, minus the pivot it does for safety —
  the brief window while it runs is the risk.

**Box answers ping and nothing else, even after a power cycle** — the rootfs didn't mount.
That needs physical access to the board's console header, which this repo does not cover.

---

## 6. Model differences seen in the wild

The same toolkit drives more than one model, and the *platform* is nearly identical while the
*surface* is not. What was observed on a CK Box II (2.2.2 / `MCB_V6_3_5`) next to a KD Box II
(2.2.7 / `MCB_V6_3_5`):

| | KD Box II | CK Box II |
|---|---|---|
| miner binary | `kdaminer` | `ckbminer` |
| miner API port | 4068 | **4028** |
| rootfs region | 12,918,784 B | 11,838,464 B |
| stock root DES hash | `kRmQ3yGkGZThM` | `kRmQ3yGkGZThM` (identical) |

Check `--inspect` for your own unit rather than trusting this table.

**`ckbminer` never starts, port 4028 refused, no hashing**

Not a flash failure. With **no pool configured** the management daemon never spawns the miner.
`/mnt/config/bfgminer/bfgminer.json` being empty is the tell. Add a pool and it starts within
a few seconds.

**`I2C0 ... not acknowledged by any slave` every few seconds**

`minerd` polling the board while idle. Seen on a freshly flashed, pool-less box. Treat as
noise unless hashing is also failing.

**A package the tooling cannot read at all: MiniDOGE 2.2.7**

Inspected as a control case, not a supported target. `firmware-Goldshell-MiniDOGE-2.2.7.cpb`
(22,893,056 B, header `firmware-MCB_V5_4.tgz`) differs from the Box II family on three counts:

| | Box II family (KD/CK) | MiniDOGE 2.2.7 |
|---|---|---|
| gzip members | 2 (rootfs + hashboard) | **1** |
| rootfs filesystem | squashfs at `0x389E00` | **big-endian cramfs**, different offset |
| `build.py --inspect` | reports the layout | **"no second gzip member found"** |

So `--inspect` refusing a package is the tooling telling you the truth, not a bug — this is
exactly why the supported list is two models and not "all Goldshell". Its chip is `ICT560`
(scrypt); the image contains no `blake`/`kadena`/`eaglesong` strings anywhere.

**Shares stay at `Accepted 0` for the first minutes**

Normal at CKB difficulty. In the reference run the first accepted share landed at
t+242 s. Watch `Hardware Errors` instead — that climbing is a real signal.
