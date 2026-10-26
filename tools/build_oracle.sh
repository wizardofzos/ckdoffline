#!/bin/bash
# Copy Hyperion source into .oracle/hyperion and build its DASD utilities.
# Hyperion source: $HYPERION_SRC, default .oracle/src/hyperion.
# Requires the Hercules external packages (arm64 static libs) in .oracle/extpkgs.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
E="$ROOT/.oracle"
SRC="${HYPERION_SRC:-$E/src/hyperion}"
DST="$E/hyperion"

if [ ! -e "$DST/dasdls" ] && [ ! -f "$SRC/configure" ]; then
  cat >&2 <<MSG
No Hyperion source at $SRC
Clone it there, or point HYPERION_SRC at an existing checkout:
  git clone https://github.com/SDL-Hercules-390/hyperion.git $SRC
MSG
  exit 1
fi

if ! ls "$E"/extpkgs/lib/aarch64/*.a >/dev/null 2>&1; then
  cat >&2 <<MSG
Missing $E/extpkgs/lib/aarch64/*.a
Clone crypto, decNumber, SoftFloat, telnet from github.com/SDL-Hercules-390 into $E/ext/<pkg>, then run:
E=$E; for p in crypto decNumber SoftFloat telnet; do mkdir -p \$E/work/\$p; (cd \$E/work/\$p && \$E/ext/\$p/build -d \$E/ext/\$p -n \$p -m aarch64 -a 64 -c Release -i \$E/extpkgs); done
MSG
  exit 1
fi

if [ ! -e "$DST/dasdls" ]; then
  mkdir -p "$DST"
  rsync -a --exclude .git "$SRC/" "$DST/"
  cd "$DST"
  ./configure --disable-getoptwrapper --without-included-ltdl --enable-extpkgs="$E/extpkgs" \
    CPPFLAGS=-I/opt/homebrew/include LDFLAGS=-L/opt/homebrew/lib
  make -j8 dasdls dasdcat dasdseq dasdpdsu dasdload dasdcopy dasdcopy64 cckdcdsk cckdcdsk64 || make -j8
fi
cd "$DST"
./dasdls 2>&1 | head -5 || true
