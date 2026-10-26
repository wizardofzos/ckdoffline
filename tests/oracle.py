"""Wrappers around the Hyperion utilities built in .oracle/hyperion."""
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORACLE_BIN = ROOT / ".oracle" / "hyperion"


def _run(args, check=True):
    p = subprocess.run([str(a) for a in args], capture_output=True, cwd=ORACLE_BIN)
    if check and p.returncode != 0:
        raise RuntimeError(f"{args[0]} failed rc={p.returncode}: {p.stderr.decode(errors='replace')}"
                           f"{p.stdout.decode(errors='replace')}")
    return p


_MSG = re.compile(rb"^HHC\d{5}[A-Z] ")


def _strip_messages(data: bytes) -> bytes:
    """Remove Hercules HHCnnnnnX message lines that the utilities mix into stdout."""
    return b"".join(l for l in data.splitlines(keepends=True) if not _MSG.match(l))


def dasdls(image) -> str:
    return _run([ORACLE_BIN / "dasdls", Path(image).resolve()]).stdout.decode(errors="replace")


def dasdcat(image, dsn, member=None, ascii=True) -> bytes:
    """Concatenated member data (dasdcat's `dsn/member[:a]` syntax). ascii=False gives the raw
    EBCDIC block bytes. With member=None every member is dumped, each preceded by a
    '> Member NAME' line. A single named member makes dasdcat rewind and ftruncate stdout, which
    fails on a pipe, so stdout is a temp file (the file is truncated, so HHC lines vanish too)."""
    spec = f"{dsn}/{member if member else '*'}"
    if ascii:
        spec += ":a"
    with tempfile.TemporaryFile() as out:
        p = subprocess.run([str(ORACLE_BIN / "dasdcat"), "-i", str(Path(image).resolve()), spec],
                           stdout=out, stderr=subprocess.PIPE, cwd=ORACLE_BIN)
        out.seek(0)
        data = out.read()
    if p.returncode != 0 or b"HHC02406E" in p.stderr + data:
        raise RuntimeError(f"dasdcat failed rc={p.returncode}: {p.stderr.decode(errors='replace')}")
    return _strip_messages(data)


_MEMINFO = re.compile(rb"^HHC02407I \S+/\S+/(\S+)\s+(\d+[KMG]?) bytes from ([0-9A-F]{4})([0-9A-F]{2})([0-9A-F]{2})",
                      re.M)


def dasdcat_members(image, dsn) -> list[tuple[str, tuple[int, int, int], int | None]]:
    """Members of a PDS in directory order: (name, (cyl, head, rec) of the first data block,
    total data bytes, or None when dasdcat rounds it to 95K etc.). Aliases are listed like any other entry. Members with no data are not
    reported by dasdcat (it prints a zero start address)."""
    p = _run([ORACLE_BIN / "dasdcat", "-i", Path(image).resolve(), f"{dsn}/*:?"])
    return [(m[1].decode(), (int(m[3], 16), int(m[4], 16), int(m[5], 16)),
             int(m[2]) if m[2].isdigit() else None)
            for m in _MEMINFO.finditer(p.stdout)]


def dasdseq(image, dsn) -> bytes:
    """Unload a PS dataset as raw (EBCDIC) record bytes, no separators. dasdseq writes a file
    named after the dataset in its cwd (-ascii only logs to the terminal), so run it in a
    scratch directory."""
    with tempfile.TemporaryDirectory() as d:
        p = subprocess.run([str(ORACLE_BIN / "dasdseq"), str(Path(image).resolve()), dsn],
                           capture_output=True, cwd=d)
        out = Path(d) / dsn
        if p.returncode != 0 or not out.exists():
            raise RuntimeError(f"dasdseq failed rc={p.returncode}: {p.stdout.decode(errors='replace')}"
                               f"{p.stderr.decode(errors='replace')}")
        return out.read_bytes()


def cckdcdsk(image, level=3) -> tuple[int, str]:
    """Run the consistency check read-only; picks cckdcdsk64 for CCKD64 images."""
    image = Path(image).resolve()
    with open(image, "rb") as f:
        sig = f.read(8)
    tool = "cckdcdsk64" if sig == b"CKD_C064" else "cckdcdsk"
    p = _run([ORACLE_BIN / tool, "-ro", f"-{level}", image], check=False)
    return p.returncode, (p.stdout + p.stderr).decode(errors="replace")
