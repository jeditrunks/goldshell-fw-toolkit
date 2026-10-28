#!/usr/bin/env python3
"""extract.py -- pull the pieces out of a stock .cpb so you can inspect them.

    ./tools/extract.py --cpb stock.cpb --out build/

Writes, in order:

    build/member0.tar            the tar inside gzip member 0
    build/firmware.ius           the signed inner image (rootfs lives here)
    build/firmware.ius.sig       its signature
    build/rootfs.squashfs        the rootfs region carved at --rootfs-offset
    build/rootfs/                (with --unsquashfs) the extracted tree

Nothing is modified; this is read-only over the input .cpb.
"""
import argparse
import gzip
import io
import sys
import tarfile
import zlib
from pathlib import Path

DEFAULT_OFFSET = 0x389E00


def members(data: bytes):
    """Yield (offset, decompressed_bytes) for every gzip member in the container."""
    i = 0
    while True:
        i = data.find(b"\x1f\x8b\x08", i)
        if i < 0:
            return
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            raw = d.decompress(data[i:]) + d.flush()
        except zlib.error:
            i += 3
            continue
        yield i, raw
        i += 3


def main() -> int:
    ap = argparse.ArgumentParser(description="Carve a stock Goldshell .cpb into its parts.")
    ap.add_argument("--cpb", required=True)
    ap.add_argument("--out", required=True, help="output directory (created if missing)")
    ap.add_argument("--rootfs-offset", default=hex(DEFAULT_OFFSET))
    ap.add_argument("--unsquashfs", action="store_true", help="also extract the rootfs tree")
    ap.add_argument("--member", type=int, default=0,
                    help="which gzip member holds the rootfs image (default 0)")
    a = ap.parse_args()

    offset = int(str(a.rootfs_offset), 0)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    data = Path(a.cpb).read_bytes()
    name = data[:512].split(b"\x00")[0].decode("ascii", "replace")
    print(f"container     {a.cpb}  ({len(data):,} B)")
    print(f"header name   {name}")

    found = list(members(data))
    if not found:
        print("no gzip members found -- not a Goldshell .cpb?", file=sys.stderr)
        return 1
    print(f"gzip members  {len(found)}  at {[hex(o) for o, _ in found]}")

    if a.member >= len(found):
        print(f"member {a.member} requested but only {len(found)} present", file=sys.stderr)
        return 1
    _, raw = found[a.member]

    (out / f"member{a.member}.tar").write_bytes(raw)
    print(f"wrote         member{a.member}.tar  ({len(raw):,} B)")

    tf = tarfile.open(fileobj=io.BytesIO(raw))
    files = {}
    for m in tf.getmembers():
        if m.isfile():
            files[m.name] = tf.extractfile(m).read()
    for fname, blob in files.items():
        (out / Path(fname).name).write_bytes(blob)
        print(f"wrote         {Path(fname).name}  ({len(blob):,} B)")

    ius = files.get("firmware.ius")
    if ius is None:
        print("member does not contain firmware.ius", file=sys.stderr)
        return 1

    if ius[:4] != b"+ius":
        print(f"firmware.ius magic is {ius[:4]!r}, expected b'+ius'", file=sys.stderr)
        return 1
    ver = ius[0x10:0x20].split(b"\x00")[0].decode("ascii", "replace")
    print(f"ius version   {ver}")

    if offset >= len(ius) or ius[offset:offset + 4] != b"hsqs":
        idx = ius.find(b"hsqs")
        hint = f" (a squashfs magic exists at {hex(idx)})" if idx >= 0 else ""
        print(f"no squashfs at {hex(offset)}{hint} -- try --rootfs-offset", file=sys.stderr)
        return 1

    region = ius[offset:]
    (out / "rootfs.squashfs").write_bytes(region)
    print(f"wrote         rootfs.squashfs  ({len(region):,} B from {hex(offset)})")

    if a.unsquashfs:
        import shutil
        import subprocess
        if not shutil.which("unsquashfs"):
            print("squashfs-tools not installed -- skipping rootfs tree", file=sys.stderr)
            return 1
        root = out / "rootfs"
        shutil.rmtree(root, ignore_errors=True)
        r = subprocess.run(["unsquashfs", "-no-progress", "-no-exit-code", "-d", str(root),
                            str(out / "rootfs.squashfs")])
        if r.returncode == 0:
            print(f"wrote         rootfs/  (unsquashed)")
            k = root / "root/settings/fw_cmpt.pem"
            print(f"\nsigning key   {'FOUND' if k.exists() else 'not found'}: {k}")
            if k.exists():
                print("              that is the vendor OTA private key, AES-encrypted PEM.")
                print("              Set GOLDSHELL_KEY_PASS and keep it out of git.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
