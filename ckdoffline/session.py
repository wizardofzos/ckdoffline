"""Session: opens an image (recovering a leftover journal), keeps one backup per session, and
offers the high-level read and write operations the HTTP API wraps. Every write operation is one
undo-journal transaction: begin, operation, commit; any exception rolls back and is re-raised."""

import getpass
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from ckdoffline import ebcdic
from ckdoffline.dataset.extents import DatasetIO
from ckdoffline.dataset.pds import Pds, parse_ispf, validate_member_name
from ckdoffline.dataset.sequential import (patch_sequential_blocks, read_sequential,
                                           write_sequential)
from ckdoffline.errors import (CkdError, ImageChangedError, NotFoundError, ReadOnlyError,
                               UnsupportedError, ValidationError)
from ckdoffline.image.base import open_image
from ckdoffline.image.rawfile import RawFile, _journal_path
from ckdoffline.vtoc.space import free_extents
from ckdoffline.vtoc.vtoc import Volume, dsorg_string, recfm_string

OPENED_WARNING = "CCKD 'opened' flag set: image may be in use by an emulator, or the emulator crashed"
CHANGED_MESSAGE = "image changed on disk since it was opened; reopen it"


# DS1DSIND bits (only the ones with a well-known meaning are named)
DSIND_BITS = {0x80: "last volume", 0x40: "RACF defined", 0x20: "block length multiple of 8",
              0x10: "password protected", 0x04: "write protected (password)",
              0x02: "changed since backup", 0x01: "checkpointed"}
# DS1SMSFG bits
SMSFG_BITS = {0x80: "SMS managed", 0x40: "uncataloged", 0x20: "reblockable",
              0x10: "system-determined BLKSIZE", 0x08: "PDSE", 0x04: "extended format",
              0x02: "HFS", 0x01: "extended attributes"}
# DS1SCALO byte 0, bits 0-1
SCALO_UNITS = ("ABSTR", "BLK", "TRK", "CYL")


def _flags(value: int, names: dict) -> list[str]:
    return [names.get(bit, f"X'{bit:02X}'") for bit in (0x80, 0x40, 0x20, 0x10, 8, 4, 2, 1)
            if value & bit]


def _ydd_date(ydd) -> str | None:
    """YDD (years since 1900, day of year) as an ISO date; None when zero; 'YYYY.DDD' when
    the day does not exist in that year (e.g. the 99.366 never-expires convention)."""
    y, d = ydd
    if not y and not d:
        return None
    year = 1900 + y
    if 1 <= d <= (366 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 365):
        return (date(year, 1, 1) + timedelta(days=d - 1)).isoformat()
    return f"{year}.{d:03d}"


def default_userid() -> str:
    name = os.environ.get("USER") or ""
    return name.upper()[:8] or "CKDOFF"


class Session:
    def __init__(self, path: Path, image, mode: str, backup: bool, codepage: str,
                 userid: str, recovered: bool):
        self.path = Path(path)
        self.image = image
        self.volume = Volume(image)
        self.mode = mode
        self.backup = backup
        self.codepage = codepage
        self.userid = userid
        self.recovered = recovered
        self.backup_path: Path | None = None
        self._confirmed = not (mode == "rw" and image.opened_flag)
        self._opened_seen = image.opened_flag     # the in-use flag was set when last looked at
        # File identity and change stamp at open and after each of our own commits: anything
        # else that writes the image (Hercules, another CKD-Offline) changes it, and then the
        # cached tracks, L1/L2 tables and VTOC copies are stale.
        self._stamp = self._disk_stamp()
        self._seen = self._stamp                # last stamp the read caches were valid for

    @classmethod
    def open(cls, path: Path, mode: str = "rw", backup: bool = True, codepage: str = "1047",
             userid: str | None = None) -> "Session":
        path = Path(path)
        if mode not in ("rw", "ro"):
            raise ValueError(f"mode must be 'rw' or 'ro', not {mode!r}")
        if codepage not in ebcdic.CODEPAGES:
            raise ValidationError(f"unknown code page {codepage!r}")
        recovered = False
        if mode == "rw":
            recovered = RawFile.recover(path)       # CkdError if the journal is corrupt
        elif _journal_path(path).exists():
            raise CkdError(f"{path.name} has an undo journal from an interrupted write; "
                           f"open it read-write once to recover it")
        image = open_image(path, mode == "rw")
        try:
            return cls(path, image, mode, backup, codepage,
                       (userid or default_userid()).upper()[:8], recovered)
        except BaseException:
            image.close()
            raise

    def close(self) -> None:
        self.image.close()

    def _disk_stamp(self):
        """(device, inode, size, mtime) of the image path now; None if it cannot be read.
        By path, not by our descriptor, so a replaced file counts as a change too."""
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns

    def _refresh_if_changed(self) -> None:
        """Before a read: if someone else changed the file, drop every cached copy so the
        read sees what is on disk now. Writes stay refused (see _check_fresh)."""
        now = self._disk_stamp()
        if now == self._seen:
            return
        self._seen = now
        self.image.drop_caches()
        try:
            self.volume = Volume(self.image)
        except CkdError:
            pass                                # keep the old view; the read reports the error

    def _check_fresh(self) -> None:
        """Refuse to write from caches that predate somebody else's change to the file, or
        when the in-use flag has been set since it was last confirmed."""
        if self._disk_stamp() != self._stamp:
            raise ImageChangedError(CHANGED_MESSAGE)
        if self.image.disk_opened_flag():
            if not self._opened_seen:
                self._opened_seen = True
                self._confirmed = False
        else:
            self._opened_seen = False

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- state -----------------------------------------------------------------------------
    @property
    def info(self) -> dict:
        self._refresh_if_changed()
        # The free space is informational: a volume whose free-space map disagrees with the
        # extents (or with DS4DEVSZ, ...) still opens and can be edited in place; only
        # extend() insists on a consistent map.
        try:
            free, free_error = sum(e - s for s, e in free_extents(self.volume)), None
        except CkdError as e:
            free, free_error = None, str(e)
        return {
            "volser": self.volume.volser,
            "device": f"{self.image.devtype:04X}",
            "fmt": self.image.fmt,
            "cyls": self.image.cyls,
            "free_tracks": free,
            "free_space_error": free_error,
            "mode": self.mode,
            "codepage": self.codepage,
            "warning": OPENED_WARNING if self.mode == "rw" and not self._confirmed else None,
            "recovered": self.recovered,
            "backup_path": str(self.backup_path) if self.backup_path else None,
        }

    def confirm_rw(self) -> None:
        """The user accepted the 'opened' flag warning: writes are allowed from now on."""
        self._confirmed = True

    # -- reads -----------------------------------------------------------------------------
    def datasets(self):
        self._refresh_if_changed()
        return self.volume.datasets()

    def _ds(self, dsn: str):
        """(DSCB, DSORG string, RECFM string) of a dataset; NotFoundError if absent."""
        self._refresh_if_changed()
        _, f1 = self.volume.f1(dsn)
        return f1, dsorg_string(f1), recfm_string(f1.recfm)

    def _po(self, dsn: str) -> Pds:
        _, dsorg, _ = self._ds(dsn)
        if dsorg != "PO":
            raise ValidationError(f"{dsn} is not a partitioned dataset")
        return Pds(self.volume, dsn)

    def members(self, dsn: str) -> list[dict]:
        out = []
        for e in self._po(dsn).directory():
            s = parse_ispf(e.userdata)
            out.append({
                "name": e.name, "alias": e.alias, "ttr": f"{e.ttr[0]:04X}{e.ttr[1]:02X}",
                "version": f"{s.version:02d}.{s.mod:02d}" if s else None,
                "created": s.created.isoformat() if s else None,
                "changed": s.changed.isoformat(timespec="seconds") if s else None,
                "lines": s.lines if s else None,
                "init_lines": s.init_lines if s else None,
                "mod_lines": s.mod_lines if s else None,
                "userid": s.userid if s else None,
            })
        return out

    def _target(self, dsn: str, member: str | None):
        """(f1, dsorg, recfm, normalised member) with the PS/PO member rules applied."""
        f1, dsorg, recfm = self._ds(dsn)
        if dsorg == "PO":
            if not member:
                raise ValidationError(f"{dsn} is partitioned: a member name is required")
            return f1, dsorg, recfm, validate_member_name(member)
        if dsorg == "PS":
            if member:
                raise ValidationError(f"{dsn} is sequential: it has no members")
            return f1, dsorg, recfm, None
        raise UnsupportedError(f"{dsn} has DSORG {dsorg}; only PS and PO can be opened")

    def _records(self, dsn: str, member: str | None) -> tuple[list[bytes], str, int]:
        f1, dsorg, recfm, member = self._target(dsn, member)
        if dsorg == "PO":
            return Pds(self.volume, dsn).read_member(member), recfm, f1.lrecl
        return read_sequential(self.volume, dsn), recfm, f1.lrecl

    def read_text(self, dsn: str, member: str | None = None) -> dict:
        _, _, recfm, member = self._target(dsn, member)
        if recfm.startswith("U"):
            raise UnsupportedError(f"{dsn} is RECFM U: use the hex view")
        recs, recfm, lrecl = self._records(dsn, member)
        return {"recfm": recfm, "lrecl": lrecl,
                "lines": [ebcdic.to_text(r, self.codepage) for r in recs],
                "raw_lines": [r.hex() for r in recs]}

    def read_blocks(self, dsn: str, member: str | None = None) -> list[str]:
        _, dsorg, _, member = self._target(dsn, member)
        if dsorg == "PO":
            return [b.hex() for _, b in Pds(self.volume, dsn).read_member_blocks(member)]
        return [r.data.hex() for _, r in DatasetIO(self.volume, dsn).blocks()]

    # -- writes ----------------------------------------------------------------------------
    def _check_writable(self) -> None:
        if self.mode != "rw":
            raise ReadOnlyError("session is read-only")
        self._check_fresh()
        if not self._confirmed:
            raise CkdError(OPENED_WARNING + "; confirm to allow writing")

    def _make_backup(self) -> None:
        if not self.backup or self.backup_path is not None:
            return
        base = f"{self.path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        n = 0
        while True:                         # reserve a name atomically: never overwrite a backup
            dst = self.path.with_name(base if n == 0 else f"{base}-{n}")
            try:
                open(dst, "xb").close()
                break
            except FileExistsError:
                n += 1
            except OSError as e:
                raise CkdError(f"backup to {dst.name} failed ({e}); nothing was written") from e
        # dst is now ours (created by this call), so it is safe to remove on failure.
        try:
            ok = False
            if sys.platform == "darwin":  # APFS clone: instant, no extra space
                try:
                    ok = subprocess.run(["cp", "-c", str(self.path), str(dst)],
                                        capture_output=True).returncode == 0
                except OSError:
                    pass
            if not ok:
                shutil.copyfile(self.path, dst)
        except OSError as e:
            dst.unlink(missing_ok=True)
            raise CkdError(f"backup to {dst.name} failed ({e}); nothing was written") from e
        self.backup_path = dst

    def _txn(self, op):
        """Run op() as one transaction (after the once-per-session backup)."""
        self._check_writable()
        self._make_backup()
        self.image.begin()
        try:
            result = op()
            self.image.commit()
            return result
        except BaseException:
            try:
                self.image.rollback()
            except BaseException:
                pass                    # the original error is the useful one
            raise
        finally:
            # Our own commit (or the restore of a failed one) changed the file: that is not
            # an external change. The freshness check ran just before begin().
            self._stamp = self._seen = self._disk_stamp()

    def _encode(self, lines: list, recfm: str, lrecl: int) -> list[bytes]:
        fixed = recfm.startswith("F")
        blank = ebcdic.from_text(" ", self.codepage)
        out = []
        for n, line in enumerate(lines, 1):
            if isinstance(line, dict):
                try:
                    out.append(bytes.fromhex(line["raw"]))
                except (KeyError, ValueError, TypeError):
                    e = ValidationError(f"line {n}: invalid raw hex")
                    e.line_no = n
                    raise e from None
                continue
            try:
                rec = ebcdic.from_text(line, self.codepage)
            except (KeyError, TypeError):
                e = ValidationError(f"line {n}: contains a character the code page "
                                    f"{self.codepage} cannot encode")
                e.line_no = n
                raise e from None
            if not fixed and not rec:
                rec = blank                     # ISPF saves an emptied V line as one blank
            if fixed:
                if len(rec) > lrecl:
                    e = ValidationError(f"line {n}: {len(rec)} characters, LRECL is {lrecl}")
                    e.line_no = n
                    raise e
                rec = rec + blank * (lrecl - len(rec))
            out.append(rec)
        return out

    def save_text(self, dsn: str, member: str | None, lines: list) -> None:
        self._check_writable()
        f1, dsorg, recfm, member = self._target(dsn, member)
        if recfm.startswith("U"):
            raise UnsupportedError(f"{dsn} is RECFM U: use the hex view")
        recs = self._encode(lines, recfm, f1.lrecl)
        if dsorg == "PO":
            self._txn(lambda: Pds(self.volume, dsn).save_member(member, recs, self.userid))
        else:
            self._txn(lambda: write_sequential(self.volume, dsn, recs))

    def new_member(self, dsn: str, name: str) -> None:
        self._check_writable()
        pds = self._po(dsn)
        name = validate_member_name(name)
        if pds.recfm.startswith("U"):
            raise UnsupportedError(f"{dsn} is RECFM U: members cannot be created")
        if any(e.name == name for e in pds.directory()):
            raise ValidationError(f"Member {name} already exists in {dsn}")
        self._txn(lambda: Pds(self.volume, dsn).save_member(name, [], self.userid))

    def delete_member(self, dsn: str, name: str) -> None:
        self._check_writable()
        self._po(dsn)
        self._txn(lambda: Pds(self.volume, dsn).delete_member(name))

    def rename_member(self, dsn: str, old: str, new: str) -> None:
        self._check_writable()
        self._po(dsn)
        self._txn(lambda: Pds(self.volume, dsn).rename_member(old, new))

    def patch_block(self, dsn: str, member: str | None, index: int, hexdata: str) -> None:
        self.patch_blocks(dsn, member, [{"index": index, "hex": hexdata}])

    def patch_blocks(self, dsn: str, member: str | None, patches: list) -> None:
        """Hex overtype of several blocks of a member, or of a sequential RECFM U dataset
        (member None), as ONE transaction: every patch is validated (index, hex, length) before
        anything is written; all or nothing. Indexes are 0-based; messages count from 1."""
        self._check_writable()
        _, dsorg, recfm, member = self._target(dsn, member)
        if dsorg == "PS" and not recfm.startswith("U"):
            raise UnsupportedError(f"{dsn} is RECFM {recfm}: edit it as text")
        if not patches:
            raise ValidationError("no blocks to patch")
        data: dict[int, bytes] = {}
        for p in patches:
            index = p.get("index") if isinstance(p, dict) else None
            if not isinstance(index, int) or isinstance(index, bool):
                raise ValidationError("each patch needs an integer index")
            if index in data:
                raise ValidationError(f"block {index + 1} is patched twice")
            try:
                data[index] = bytes.fromhex(p.get("hex"))
            except (ValueError, TypeError):
                raise ValidationError(f"block {index + 1}: data is not valid hex") from None
        if dsorg == "PS":
            self._txn(lambda: patch_sequential_blocks(self.volume, dsn, data))
        else:
            self._txn(lambda: Pds(self.volume, dsn).patch_member_blocks(member, data,
                                                                        self.userid))

    def dscb(self, dsn: str) -> dict:
        """The decoded format-1 DSCB (plus format-3 extents) of a dataset, for the I panel."""
        self._refresh_if_changed()
        cchhr, f1 = self.volume.f1(dsn)
        f1_exts = list(f1.extents)
        exts = self.volume.extents_of(dsn)
        heads = self.image.heads
        units, qty = f1.scalo
        return {
            "dsname": f1.dsname,
            "f1_cchhr": list(cchhr),
            "volser": ebcdic.to_text(f1.volser_raw, "1047").rstrip(),
            "volsq": f1.volsq,
            "credt": _ydd_date(f1.credt),
            "expdt": _ydd_date(f1.expdt),
            "refdt": _ydd_date(f1.refdt),
            "dsorg": f"{f1.dsorg:04X}", "dsorg_str": dsorg_string(f1),
            "recfm": f"{f1.recfm:02X}", "recfm_str": recfm_string(f1.recfm),
            "optcd": f"{f1.optcd:02X}",
            "blksize": f1.blksize, "lrecl": f1.lrecl, "keylen": f1.keylen, "rkp": f1.rkp,
            "dsind": f"{f1.dsind:02X}", "dsind_flags": _flags(f1.dsind, DSIND_BITS),
            "scalo": {"units": SCALO_UNITS[units >> 6], "flags": f"{units:02X}", "qty": qty},
            "lstar": {"tt": f1.lstar[0], "r": f1.lstar[1]},
            "tracks_used": f1.tracks_used,
            "trbal": f1.trbal,
            "noepv": f1.noepv,
            "smsfg": f"{f1.smsfg:02X}", "smsfg_flags": _flags(f1.smsfg, SMSFG_BITS),
            "extents": [{"source": "F1" if k < len(f1_exts) else "F3", "type": e.type,
                         "seq": e.seq, "bcyl": e.bcyl, "bhead": e.bhead, "ecyl": e.ecyl,
                         "ehead": e.ehead, "tracks": e.tracks(heads)}
                        for k, e in enumerate(exts)],
        }

    def compress(self, dsn: str) -> dict:
        self._check_writable()
        self._po(dsn)
        return self._txn(lambda: Pds(self.volume, dsn).compress())
