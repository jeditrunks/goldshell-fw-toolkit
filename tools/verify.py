#!/usr/bin/env python3
"""verify.py -- independent check of a built .cpb before it goes near a device.

Deliberately shares no code with build.py: it re-parses the container from scratch,
re-verifies the signature against the *stock* public key, and inspects the rootfs.

    ./tools/verify.py --stock stock.cpb --new custom.cpb \
                      --verify-key keys/fw_cmpt.verify.pem --pubkey keys/id_rsa.pub

Exit code 0 only if every check passes.
"""
import argparse
import hashlib
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import zlib
from pathlib import Path

DEFAULT_OFFSET = 0x389E00


def gzip_members(data: bytes):
    """Locate every gzip member by magic and decompress it."""
    out, i = [], 0
    while True:
        i = data.find(b"\x1f\x8b\x08", i)
        if i < 0:
            return out
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            raw = d.decompress(data[i:]) + d.flush()
        except Exception:
            i += 3
            continue
        out.append((i, raw))
        i += 3


def tar_files(raw: bytes, dest: Path, tag: str):
    tf = tarfile.open(fileobj=io.BytesIO(raw))
    got = {}
    for m in tf.getmembers():
        if m.isfile():
            b = tf.extractfile(m).read()
            got[m.name] = b
            (dest / f"{tag}_{Path(m.name).name}").write_bytes(b)
    return got


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stock", required=True, help="stock .cpb (reference)")
    ap.add_argument("--new", required=True, help="built .cpb (under test)")
    ap.add_argument("--verify-key", required=True, help="vendor public key (fw_cmpt.verify.pem)")
    ap.add_argument("--pubkey", required=True, help="SSH public key that must be trusted")
    ap.add_argument("--rootfs-offset", default=hex(DEFAULT_OFFSET))
    a = ap.parse_args()

    offset = int(str(a.rootfs_offset), 0)
    work = Path(tempfile.mkdtemp(prefix="fw-verify-"))
    res = []

    def check(label, cond, detail=""):
        res.append((label, bool(cond), str(detail)))

    sdata = Path(a.stock).read_bytes()
    ndata = Path(a.new).read_bytes()
    check("container header identical", sdata[:512] == ndata[:512],
          ndata[:512].split(b"\x00")[0].decode("ascii", "replace"))

    sm, nm = gzip_members(sdata), gzip_members(ndata)
    check("both containers have >= 2 members", len(sm) >= 2 and len(nm) >= 2,
          f"stock={len(sm)} new={len(nm)}")
    if len(sm) < 2 or len(nm) < 2:
        print("cannot continue without member 0 and 1")
        return 1

    s0 = tar_files(sm[0][1], work, "stock")
    n0 = tar_files(nm[0][1], work, "new")
    check("member 0 tar members identical", sorted(s0) == sorted(n0), sorted(n0))

    si, ni = s0["firmware.ius"], n0["firmware.ius"]
    check("ius length unchanged (in-place patch)", len(si) == len(ni), f"{len(ni):,} B")

    (work / "sig").write_bytes(n0["firmware.ius.sig"])
    r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", a.verify_key,
                        "-signature", str(work / "sig"), str(work / "new_firmware.ius")],
                       capture_output=True, text=True)
    check("signature verifies against vendor public key", "Verified OK" in r.stdout,
          (r.stdout or r.stderr).strip()[:60])

    diffs = [i for i in range(min(len(si), len(ni))) if si[i] != ni[i]]
    inside = all(offset <= i < len(ni) for i in diffs)
    check("all differing bytes inside the rootfs region", inside,
          f"{len(diffs):,} diffs, {hex(min(diffs))}..{hex(max(diffs))}" if diffs else "identical")

    tail_s, tail_n = sdata[sm[1][0]:], ndata[nm[1][0]:]
    check("member 1 + trailer byte-identical", bool(tail_s) and tail_s == tail_n,
          f"{len(tail_n):,} B")

    sq = work / "new.squashfs"
    sq.write_bytes(ni[offset:])
    root = work / "root"
    subprocess.run(["unsquashfs", "-f", "-no-progress", "-no-exit-code", "-d", str(root), str(sq)],
                   capture_output=True)

    def read(p):
        try:
            return (root / p).read_text(errors="replace")
        except Exception:
            return ""

    ak = read("root/.ssh/authorized_keys").strip()
    pub = Path(a.pubkey).read_text().strip()
    check("authorized_keys matches the supplied key", ak == pub, ak[:40])
    if (root / "root/.ssh/authorized_keys").exists():
        mode = os.stat(root / "root/.ssh/authorized_keys").st_mode & 0o777
        dirm = os.stat(root / "root/.ssh").st_mode & 0o777
        check("authorized_keys 0600 / .ssh 0700", mode == 0o600 and dirm == 0o700,
              f"{oct(mode)} / {oct(dirm)}")

    s50 = read("etc/init.d/S50sshd")
    check("S50sshd stop() neutralised", "killall sshd" not in s50 and "stop()" in s50)
    cfg = read("etc/sshd_config")
    check("PermitRootLogin yes", any(l.strip() == "PermitRootLogin yes" for l in cfg.splitlines()))
    check("marker file present",
          any((root / "etc" / n).exists() for n in ("kdbox-custom-fw", "goldshell-custom-fw", "ckbox-custom-fw")))

    print(f"\n{'CHECK':<46} RESULT")
    print("-" * 86)
    bad = 0
    for label, okk, detail in res:
        bad += not okk
        print(f"{label:<46} {'PASS' if okk else 'FAIL':<5} {detail[:32]}")
    print("-" * 86)
    print(f"{len(res) - bad}/{len(res)} passed   sha256(new)={hashlib.sha256(ndata).hexdigest()[:16]}…")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
