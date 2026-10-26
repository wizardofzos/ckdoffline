#!/bin/bash
# Build synthetic TEST01 3390 images (CKD, CCKD zlib/bz2, CCKD64) into .oracle/fixtures.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
H=.oracle/hyperion
OUT=.oracle/fixtures
[ -x "$H/dasdload" ] || ./tools/build_oracle.sh
rm -rf "$OUT"; mkdir -p "$OUT" tools/fixture-data
python3 - <<'PY'
with open("tools/fixture-data/fb80.bin", "wb") as f:
    for i in range(500):
        f.write(f"LINE {i+1:05d} THE QUICK BROWN FOX {chr(65 + i % 26)}".ljust(80).encode("cp037"))
PY
"$H/dasdload" tools/fixtures.ctl "$OUT/test01.ckd" >/dev/null
"$H/dasdcopy" -z   "$OUT/test01.ckd" "$OUT/test01.cckd_zlib" >/dev/null
"$H/dasdcopy" -bz2 "$OUT/test01.ckd" "$OUT/test01.cckd_bz2"  >/dev/null
"$H/dasdcopy64" -z "$OUT/test01.ckd" "$OUT/test01.cckd64"    >/dev/null
ls -l "$OUT"
