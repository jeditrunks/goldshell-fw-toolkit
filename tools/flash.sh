#!/usr/bin/env bash
# flash.sh -- authenticate to a Goldshell KD Box II and push a firmware package.
#
#   ./tools/flash.sh <miner-ip> <package.cpb> [<package-filename>]
#
# The package filename MUST be firmware-<mcbversion>.tgz, e.g. firmware-MCB_V6_3_5.tgz.
# If omitted it is read from the .cpb's 512-byte header, which is where the stock
# packer puts it.
#
# Environment:
#   KD_USER     web user      (default: admin)
#   KD_PASS     web password  (default: 123456789 -- the factory default)
#   KD_DRY_RUN  set to 1 to check status + login and stop before uploading
#
# What happens on the device: the package is written to /dev/shm/minerd/download,
# the daemon verifies its RSA signature, then runs the update thread which kills
# userspace, pivots into a tmpfs and calls `upgrade <pkg>` + reboot. Expect ~3.5
# minutes of downtime, starting immediately after the POST.

set -euo pipefail

IP="${1:-}"
PKG="${2:-}"
NAME="${3:-}"

if [ -z "$IP" ] || [ -z "$PKG" ]; then
    echo "usage: $0 <miner-ip> <package.cpb> [firmware-<mcbversion>.tgz]" >&2
    exit 1
fi
[ -f "$PKG" ] || { echo "error: no such package: $PKG" >&2; exit 1; }

KD_USER="${KD_USER:-admin}"
KD_PASS="${KD_PASS:-123456789}"

# --- package filename from the .cpb header if not given ----------------------
if [ -z "$NAME" ]; then
    NAME=$(head -c 512 "$PKG" | python3 -c '
import sys
d = sys.stdin.buffer.read()
print(d.split(b"\x00")[0].decode("ascii", "replace"))
')
    case "$NAME" in
        firmware-*.tgz) ;;
        *) echo "error: could not read 'firmware-*.tgz' from the header; pass it explicitly" >&2; exit 1 ;;
    esac
fi

SIZE=$(stat -c%s "$PKG")
echo "target   $IP"
echo "package  $PKG ($SIZE bytes)"
echo "filename $NAME"
echo "sha256   $(sha256sum "$PKG" | cut -d' ' -f1)"
echo

# --- what does the box say it is? -------------------------------------------
STATUS=$(curl -s -m 6 "http://$IP/mcb/status" || true)
if [ -z "$STATUS" ]; then
    echo "error: $IP is not answering on the web UI" >&2
    exit 1
fi
case "$STATUS" in
    *'"mcbversion"'*) ;;
    *) echo "error: $IP is answering, but not with a KD Box status document:" >&2
       echo "       ${STATUS:0:160}" >&2
       exit 1 ;;
esac
echo "status   $STATUS"
MCB=$(printf '%s' "$STATUS" | sed -n 's/.*"mcbversion"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
if [ -n "$MCB" ] && [ "$NAME" != "firmware-$MCB.tgz" ]; then
    echo
    echo "WARNING: the box reports mcbversion '$MCB' but the package is '$NAME'."
    echo "         The updater deletes packages whose name does not match, so this will"
    echo "         probably do nothing. Rebuild from this unit's own stock package instead"
    echo "         of renaming (see docs/PROCEDURE.md section 11)."
    echo "         continue anyway? [y/N] "
    read -r ans 2>/dev/null </dev/tty || read -r ans || ans=""
    [ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "aborted"; exit 1; }
fi

# --- login ------------------------------------------------------------------
# The UI encrypts the password with AES-128-CBC: static key '!!!!!!!!!!!!!!!!',
# zero IV, zero padding, hex output. Obfuscation, not security.
cipher() {
    python3 - "$1" <<'PY' | openssl enc -aes-128-cbc -K 21212121212121212121212121212121 \
            -iv 00000000000000000000000000000000 -nopad 2>/dev/null | xxd -p -c 256
import sys
p = sys.argv[1].encode()
sys.stdout.buffer.write(p + b'\x00' * ((-len(p)) % 16))
PY
}

C=$(cipher "$KD_PASS")
[ -n "$C" ] || { echo "error: password encryption failed (need openssl + xxd + python3)" >&2; exit 1; }

LOGIN=$(curl -s -m 10 "http://$IP/user/login?username=$KD_USER&password=$C&cipher=true" || true)
JWT=$(printf '%s' "$LOGIN" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin)["JWT Token"])
except Exception:
    sys.exit(1)
' 2>/dev/null) || true

if [ -z "$JWT" ]; then
    echo "error: login failed. The box answers a wrong password with HTTP 500, not 401." >&2
    echo "       response: ${LOGIN:0:200}" >&2
    echo "       set KD_USER / KD_PASS if the admin password was changed." >&2
    exit 1
fi
echo "login    ok (${#JWT} char token)"
echo

if [ "${KD_DRY_RUN:-0}" = "1" ]; then
    echo "dry run -- not uploading. The real POST would be:"
    echo "  curl -X POST -H 'Authorization: Bearer <jwt>' -F 'file=@$PKG;filename=$NAME' \\"
    echo "       http://$IP/mcb/uploadimage"
    exit 0
fi

# --- upload -----------------------------------------------------------------
echo "uploading -- the box will drop off the network almost immediately"
set +e
code=$(curl -s -m 300 -X POST \
    -H "Authorization: Bearer $JWT" \
    -F "file=@$PKG;filename=$NAME" \
    -o /dev/null -w '%{http_code}' \
    "http://$IP/mcb/uploadimage")
rc=$?
set -e

# A dropped connection mid-flash is normal; a clean 200 means it was accepted
# and the update thread has taken over.
if [ "$rc" -ne 0 ] && [ "$code" != "200" ] && [ "$code" != "100" ]; then
    echo
    echo "error: upload failed (curl exit $rc, http '$code')" >&2
    exit 1
fi
echo "POST     ${code:-<connection closed>}"
echo
echo "watching for the box to come back (up to ~6 min)..."

for i in $(seq 1 36); do
    sleep 10
    w=$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://$IP/mcb/status" 2>/dev/null || true)
    printf '  %s  http=%s\n' "$(date +%H:%M:%S)" "${w:-000}"
    if [ "$w" = "200" ]; then
        echo
        echo "back up:"
        curl -s -m 6 "http://$IP/mcb/status"; echo
        echo
        echo "Now verify root (name the key and re-enable the legacy algorithm -- OpenSSH 6.4, RSA only):"
        echo "  ssh -i ~/.ssh/kdbox_rsa -o IdentitiesOnly=yes -o PubkeyAcceptedAlgorithms=+ssh-rsa \\"
        echo "      root@$IP 'id; cat /etc/kdbox-custom-fw'"
        exit 0
    fi
done

echo
echo "warning: the box has not come back after 6 minutes." >&2
echo "         If it is still running the old image it will answer eventually; if the" >&2
echo "         rootfs failed to mount you need the J2 console (docs/FINDINGS.md section 6)." >&2
exit 1
