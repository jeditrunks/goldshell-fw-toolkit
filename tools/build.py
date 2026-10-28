#!/usr/bin/env python3
"""
build.py -- turn a stock Goldshell miner firmware package into a root-SSH image.

    GOLDSHELL_KEY_PASS=... ./tools/build.py --stock stock.cpb --out custom.cpb \
                     --pubkey keys/id_rsa.pub --signing-key keys/fw_cmpt.pem

    ./tools/build.py --inspect stock.cpb        # print container layout, change nothing

What it does, in order:
  1. parse the .cpb container (512-byte header + 2 gzip members, 1024-byte aligned)
  2. extract firmware.ius / firmware.ius.sig from member 0
  3. carve the rootfs squashfs out of firmware.ius at 0x389E00
  4. unsquashfs it, apply the root mods, mksquashfs it back with stock parameters
  5. patch the new squashfs into firmware.ius in place (region length unchanged)
  6. sign firmware.ius with the vendor OTA key and verify against the public half
  7. rewrap the .cpb: stock header + our member 0 + stock member 1/trailer, unchanged

Output is deterministic: two runs on the same input produce identical bytes.

The only hard inputs you cannot invent are the vendor signing key and a stock package
for the target's firmware version. See docs/PROCEDURE.md.
"""

import argparse
import gzip
import hashlib
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile

CPB_HDR = 512
ALIGN = 1024
IUS_ROOTFS_OFF = 0x389E00
ROOTFS_RESERVE = 1024             # bytes kept free at the end of the rootfs region
                                  # (per-image budget = region - this)

# files we drop to make room in the rootfs region (see docs/PROCEDURE.md)
SHRINK = ["root/yottamc/fonts/materialdesignicons-webfont.64d4cf64.eot"]

SSHD_STOP_OLD = """stop() {
\techo -n "Stopping sshd: "
\tkillall sshd
\trm -f /var/lock/sshd
\techo "OK"
}"""

SSHD_STOP_NEW = """stop() {
\t# neutralised so the management daemon cannot kill sshd at boot
\techo -n "Stopping sshd: disabled by goldshell-custom-fw "
\techo "OK"
}"""

SSHD_CONFIG_ADD = [
    "PermitRootLogin yes",
    "PasswordAuthentication no",
    "UseDNS no",
]


def die(msg):
    print(f"\n  ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def step(msg):
    print(f"\n== {msg}")


def ok(msg):
    print(f"   {msg}")


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        die(f"{' '.join(cmd)} failed:\n{(r.stderr or r.stdout).strip()[:800]}")
    return r


def sha256(data):
    return hashlib.sha256(data if isinstance(data, (bytes, bytearray)) else data.read()).hexdigest()


# --------------------------------------------------------------------------- container

def parse_cpb(data):
    """Return (header, name, member0_raw, member0_comp_len, rest).

    `rest` starts at member 1's gzip magic, i.e. after member 0's filler padding, and runs
    to EOF (member 1 + trailer). Stock packages pad with extra filler beyond the minimum
    alignment, so member 1 is located by scanning for the next gzip magic rather than
    computed -- that keeps it byte-identical when rewrapped.
    """
    if len(data) < CPB_HDR + 2 or data[CPB_HDR:CPB_HDR + 2] != b"\x1f\x8b":
        die("not a .cpb: no gzip magic at offset 512")
    d = zlib_decompressobj()
    raw0 = d.decompress(data[CPB_HDR:])
    if not d.eof:
        die("member 0 is truncated")
    consumed = len(data) - CPB_HDR - len(d.unused_data)
    m1 = data.find(b"\x1f\x8b\x08", CPB_HDR + consumed)
    if m1 < 0:
        die("no second gzip member found -- this package has only member 0, "
            "which minerd rejects (see docs/FINDINGS.md)")
    if m1 % ALIGN:
        die(f"member 1 starts at {m1:,}, which is not {ALIGN}-byte aligned")
    hdr = data[:CPB_HDR]
    name = hdr.split(b"\x00")[0].decode("ascii", "replace")
    return hdr, name, raw0, consumed, data[m1:]


def zlib_decompressobj():
    import zlib
    return zlib.decompressobj(16 + zlib.MAX_WBITS)


def tar_extract(raw_tar, wanted):
    out = {}
    with tarfile.open(fileobj=io.BytesIO(raw_tar)) as t:
        names = t.getnames()
        for w in wanted:
            if w not in names:
                die(f"member 0 tar has no {w!r} (found {names})")
            out[w] = t.extractfile(w).read()
        nt = t.getmembers()
    return out, [(m.name, m.size) for m in nt]


def inspect(path):
    data = open(path, "rb").read()
    hdr, name, raw0, comp0, rest = parse_cpb(data)
    step(f"{path}")
    ok(f"total size       {len(data):,}")
    ok(f"header name      {name}")
    ok(f"member 0         comp {comp0:,}  decomp {len(raw0):,}  align-pad {(-(CPB_HDR + comp0)) % ALIGN}")
    files, members = tar_extract(raw0, [])
    ok(f"member 0 tar     {members}")
    ok(f"member 1 + tail  {len(rest):,} bytes (left untouched)")
    ius = tar_extract(raw0, ["firmware.ius"])[0]["firmware.ius"]
    ok(f"firmware.ius     {len(ius):,} bytes")
    if ius[:4] == b"+ius":
        ver = ius[0x10:0x18].split(b"\x00")[0].decode("ascii", "replace")
        count = struct.unpack("<I", ius[0x1c:0x20])[0]
        region = len(ius) - IUS_ROOTFS_OFF
        ok(f"ius header       magic +ius  version {ver or '?'}  images {count}")
        ok(f"rootfs region    {IUS_ROOTFS_OFF:#x} .. end  ({region:,} bytes)")
        sq = ius[IUS_ROOTFS_OFF:IUS_ROOTFS_OFF + 4]
        ok(f"squashfs magic   {sq.hex()} {'(ok)' if sq == b'hsqs' else '(NOT a squashfs!)'}")
    ok(f"sha256           {sha256(data)}")


# --------------------------------------------------------------------------- rootfs

def build_rootfs(ius, pubkey, work, shrink=True):
    version = ius[0x10:0x18].split(b"\x00")[0].decode("ascii", "replace")
    sq_off = IUS_ROOTFS_OFF
    region = len(ius) - sq_off
    budget = region - ROOTFS_RESERVE
    stock_sqfs = ius[sq_off:]

    src = os.path.join(work, "stock.sqfs")
    with open(src, "wb") as f:
        f.write(stock_sqfs)

    root = os.path.join(work, "root")
    step(f"unsquashfs -> {root}")
    shutil.rmtree(root, ignore_errors=True)
    run(["unsquashfs", "-no-progress", "-no-exit-code", "-d", root, src])
    ok(f"{sum(len(f) for _, _, f in os.walk(root))} entries extracted")

    step("apply root mods")
    sshdir = os.path.join(root, "root/.ssh")
    os.makedirs(sshdir, mode=0o700, exist_ok=True)
    os.chmod(sshdir, 0o700)
    ak = os.path.join(sshdir, "authorized_keys")
    shutil.copyfile(pubkey, ak)
    os.chmod(ak, 0o600)
    pub = open(pubkey).read().strip()
    ok(f"authorized_keys  {pub[:32]}...")

    s50 = os.path.join(root, "etc/init.d/S50sshd")
    text = open(s50).read()
    if SSHD_STOP_OLD in text:
        open(s50, "w").write(text.replace(SSHD_STOP_OLD, SSHD_STOP_NEW))
        ok("S50sshd          stop() neutralised")
    elif "disabled by goldshell-custom-fw" in text:
        ok("S50sshd          already neutralised")
    else:
        die(f"could not find the stock stop() body in {s50} -- firmware layout changed")

    cfg = os.path.join(root, "etc/sshd_config")
    ctext = open(cfg).read()
    add = [l for l in SSHD_CONFIG_ADD
           if not re.search(rf"(?m)^\s*{l.split()[0]}\s", ctext)]
    if add:
        open(cfg, "w").write(ctext.rstrip() + "\n" + "\n".join(add) + "\n")
    ok(f"sshd_config      {', '.join(add) if add else 'already set'}")

    marker = os.path.join(root, "etc/goldshell-custom-fw")
    with open(marker, "w") as f:
        f.write(f"ssh-rootfs-{version} (goldshell-custom-fw)\n")
    ok("marker           /etc/goldshell-custom-fw")

    if shrink:
        for rel in SHRINK:
            p = os.path.join(root, rel)
            if os.path.exists(p):
                freed = os.path.getsize(p)
                os.remove(p)
                ok(f"dropped          {rel} ({freed:,} raw)")
            else:
                ok(f"drop skipped     {rel} (absent)")

    step("mksquashfs (stock parameters)")
    out = os.path.join(work, "new.sqfs")
    if os.path.exists(out):
        os.remove(out)
    run(["mksquashfs", root, out,
         "-comp", "gzip", "-b", "131072", "-no-xattrs", "-noappend",
         "-all-root", "-nopad", "-Xcompression-level", "9", "-no-progress"])
    size = os.path.getsize(out)
    ok(f"image            {size:,} bytes (budget {budget:,}, headroom {budget - size:,})")
    if size > budget:
        die(f"squashfs is {size - budget:,} bytes over budget -- "
            f"drop more from SHRINK in docs/PROCEDURE.md")
    return open(out, "rb").read(), region


# --------------------------------------------------------------------------- patch + sign

def patch_ius(ius, sqfs, region):
    d = bytearray(ius)
    off = IUS_ROOTFS_OFF
    d[off:off + region] = b"\x00" * region
    d[off:off + len(sqfs)] = sqfs
    assert len(d) == len(ius), "length changed -- must stay identical to stock"
    return bytes(d)


def sign(ius_path, sig_path, key, passphrase):
    step("sign with the vendor OTA key")
    run(["openssl", "dgst", "-sha256", "-sign", key,
         "-passin", f"pass:{passphrase}", "-out", sig_path, ius_path])
    ver = run(["openssl", "dgst", "-sha256", "-verify", key.replace(".pem", ".verify.pem"),
               "-signature", sig_path, ius_path])
    out = (ver.stdout or "").strip()
    ok(f"signature        {out}")
    if "Verified OK" not in out:
        die("signature does not verify -- wrong key or passphrase")
    return open(sig_path, "rb").read()


def wrap_cpb(hdr, ius, sig, rest):
    step("rewrap .cpb")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as t:
        for name, data in (("firmware.ius", ius), ("firmware.ius.sig", sig)):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mtime = 1663027200        # fixed: keep the build deterministic
            ti.mode = 0o644
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            t.addfile(ti, io.BytesIO(data))
    raw = buf.getvalue()
    m0 = gzip.compress(raw, 9, mtime=0)
    pad = (-(CPB_HDR + len(m0))) % ALIGN
    out = hdr + m0 + b"\x00" * pad + rest
    ok(f"member 0         comp {len(m0):,}  decomp {len(raw):,}  pad {pad}")
    ok(f"member 1 + tail  {len(rest):,} (stock, byte-identical)")
    ok(f"total            {len(out):,} bytes")
    return out, len(raw)


# --------------------------------------------------------------------------- verify

def verify(pkg, stock, key_verify, work):
    step("verify the package before it goes anywhere near a device")
    hdr, name, raw0, comp0, rest = parse_cpb(pkg)
    shdr, sname, sraw0, scomp0, srest = parse_cpb(stock)
    problems = []

    if hdr != shdr:
        problems.append("header differs from stock")
    if name != sname:
        problems.append(f"header name {name} != {sname}")
    ok(f"header           identical ({name})")

    pad = (-(CPB_HDR + comp0)) % ALIGN
    if (CPB_HDR + comp0 + pad) % ALIGN:
        problems.append("member 1 does not start on a 1024-byte boundary")
    ok(f"alignment        member 1 at {CPB_HDR + comp0 + pad:,} ({pad} bytes pad)")

    if len(raw0) != len(sraw0):
        problems.append(f"member 0 decompresses to {len(raw0):,}, stock {len(sraw0):,}")
    ok(f"member 0         decomp {len(raw0):,} bytes")

    files, members = tar_extract(raw0, ["firmware.ius", "firmware.ius.sig"])
    ius, sig = files["firmware.ius"], files["firmware.ius.sig"]
    ok(f"tar members      {members}")

    sius = tar_extract(sraw0, ["firmware.ius"])[0]["firmware.ius"]
    if len(ius) != len(sius):
        problems.append(f"firmware.ius is {len(ius):,} bytes, stock {len(sius):,}")
    diffs = [i for i in range(min(len(ius), len(sius))) if ius[i] != sius[i]]
    lo, hi = (min(diffs), max(diffs)) if diffs else (0, 0)
    region = len(sius) - IUS_ROOTFS_OFF
    if diffs and not (IUS_ROOTFS_OFF <= lo and hi < IUS_ROOTFS_OFF + region):
        problems.append(f"bytes differ outside the rootfs region ({lo:#x}..{hi:#x})")
    ok(f"ius             {len(ius):,} bytes, {len(diffs):,} differ, "
       f"all inside {IUS_ROOTFS_OFF:#x}..{IUS_ROOTFS_OFF + region:#x}"
       if diffs else f"ius             {len(ius):,} bytes, identical to stock")

    ius_p = os.path.join(work, "v.ius")
    sig_p = os.path.join(work, "v.sig")
    open(ius_p, "wb").write(ius)
    open(sig_p, "wb").write(sig)
    r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", key_verify,
                        "-signature", sig_p, ius_p], capture_output=True, text=True)
    if "Verified OK" not in (r.stdout or ""):
        problems.append("signature does not verify against the vendor public key")
    ok(f"signature        {(r.stdout or r.stderr).strip()}")

    sq_p = os.path.join(work, "v.sqfs")
    open(sq_p, "wb").write(ius[IUS_ROOTFS_OFF:])
    r = subprocess.run(["unsquashfs", "-no-exit-code", "-s", sq_p], capture_output=True, text=True)
    if "valid SQUASHFS" not in (r.stdout or "") + (r.stderr or ""):
        problems.append("rootfs is not a valid squashfs")
    l = subprocess.run(["unsquashfs", "-no-exit-code", "-l", sq_p], capture_output=True, text=True)
    listing = (l.stdout or "") + (l.stderr or "")
    for want in ("root/.ssh/authorized_keys", "etc/goldshell-custom-fw"):
        if want not in listing:
            problems.append(f"{want} missing from the image")
    ok(f"rootfs           valid squashfs, {len(listing.splitlines()):,} entries, mods present")

    if rest != srest:
        problems.append("member 1 / trailer differs from stock")
    ok("member 1 + tail  byte-identical to stock")

    if problems:
        print()
        for p in problems:
            print(f"   FAIL: {p}", file=sys.stderr)
        die("package failed verification")

    print(f"\n   ALL CHECKS PASSED   sha256 {sha256(pkg)}")


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Build a root-SSH firmware package for a Goldshell miner.")
    ap.add_argument("--stock", help="stock .cpb for the target firmware version")
    ap.add_argument("--out", help="output .cpb")
    ap.add_argument("--pubkey", default="keys/id_rsa.pub", help="SSH public key to trust")
    ap.add_argument("--signing-key", default="keys/fw_cmpt.pem",
                    help="vendor OTA private key, extracted from your own unit (NOT shipped here)")
    ap.add_argument("--passphrase", default=os.environ.get("GOLDSHELL_KEY_PASS"),
                    help="passphrase for the signing key (default: $GOLDSHELL_KEY_PASS)")
    ap.add_argument("--verify-key", default=None, help="vendor public key (default: derived)")
    ap.add_argument("--no-shrink", action="store_true", help="keep files in SHRINK")
    ap.add_argument("--rootfs-offset", default="0x389E00",
                    help="offset of the rootfs squashfs inside firmware.ius (default 0x389E00)")
    ap.add_argument("--inspect", metavar="CPB", help="print container layout and exit")
    ap.add_argument("--work", default=None, help="work dir (default: a temp dir, removed)")
    a = ap.parse_args()

    global IUS_ROOTFS_OFF
    IUS_ROOTFS_OFF = int(str(a.rootfs_offset), 0)

    if not a.passphrase and not a.inspect:
        die("no signing-key passphrase: pass --passphrase or set $GOLDSHELL_KEY_PASS")

    if a.inspect:
        inspect(a.inspect)
        return

    if not a.stock or not a.out:
        ap.error("--stock and --out are required unless --inspect is used")
    for p, what in ((a.stock, "stock package"), (a.pubkey, "public key"), (a.signing_key, "signing key")):
        if not os.path.isfile(p):
            die(f"{what} not found: {p}")

    verify_key = a.verify_key or a.signing_key.replace(".pem", ".verify.pem")
    if not os.path.isfile(verify_key):
        die(f"vendor public key not found: {verify_key}")

    tmp = None
    work = a.work
    if work:
        os.makedirs(work, exist_ok=True)
    else:
        tmp = tempfile.mkdtemp(prefix="kdbox-build-")
        work = tmp

    try:
        step(f"read {a.stock}")
        stock = open(a.stock, "rb").read()
        hdr, name, raw0, comp0, rest = parse_cpb(stock)
        ok(f"header {name}   member 0 comp {comp0:,}   member 1 + tail {len(rest):,}")

        files, _ = tar_extract(raw0, ["firmware.ius", "firmware.ius.sig"])
        ius = files["firmware.ius"]
        ok(f"firmware.ius {len(ius):,} bytes, rootfs at {IUS_ROOTFS_OFF:#x}")

        sqfs, region = build_rootfs(ius, a.pubkey, work, shrink=not a.no_shrink)
        new_ius = patch_ius(ius, sqfs, region)
        ok(f"patched in place, {len(new_ius):,} bytes (unchanged length)")

        ius_p = os.path.join(work, "firmware.ius")
        sig_p = os.path.join(work, "firmware.ius.sig")
        open(ius_p, "wb").write(new_ius)
        sig = sign(ius_p, sig_p, a.signing_key, a.passphrase)

        pkg, _ = wrap_cpb(hdr, new_ius, sig, rest)
        open(a.out, "wb").write(pkg)

        verify(pkg, stock, verify_key, work)
        print(f"\n   wrote {a.out}")
        print(f"   flash with: ./tools/flash.sh <miner-ip> {a.out} {name}")
        print("\n   keep the stock package -- it is your rollback.")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
