"""Partitioned dataset (PO) read: directory, members, ISPF statistics.

Directory: blocks of an 8-byte key (highest name in the block) + 256 data bytes at the start of the
dataset, followed by an EOF record and then the member data. Data bytes 0-1 are the used byte count
(including those two bytes); entries are name(8) TTR(3) C(1) userdata(2*(C & 0x1F)); the entry named
X'FF'*8 ends the directory.
"""

import difflib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ckdoffline import ebcdic
from ckdoffline.dataset.extents import DatasetIO
from ckdoffline.dataset.records import deblock_stream, reblock
from ckdoffline.errors import (DirectoryFullError, FormatError, NoSpaceError, NotFoundError,
                               UnsupportedError, ValidationError)
from ckdoffline.track import is_eof
from ckdoffline.vtoc.vtoc import Volume, dsorg_string, editability, recfm_string

DIR_DATA_LEN = 256
DIR_KEY_LEN = 8
END_NAME = b"\xff" * 8
C_ALIAS = 0x80
C_TTRN = 0x60
C_USER = 0x1F
PDS2OVLY = 0x20      # PDS2ATR1 (user data byte 8): overlay structure
PDS2SCTR = 0x04      # PDS2ATR1: scatter-load format

ISPF_STD_LEN = 30
ISPF_EXT_LEN = 40
ISPF_FLAG_EXT = 0x20
ISPF_MAX_MOD = 99
_NAME_CP = "1047"
_MEMBER_RE = re.compile(r"[A-Z@#$][A-Z0-9@#$]{0,7}")


@dataclass
class DirEntry:
    name: str
    ttr: tuple[int, int]
    alias: bool
    ttrn_count: int
    userdata: bytes


@dataclass
class IspfStats:
    """Decoded ISPF statistics. `flags` is the raw flag byte (kept so a stats block survives
    parse/build unchanged; build_ispf sets 0x20 itself for the extended layout)."""
    version: int
    mod: int
    created: date
    changed: datetime
    lines: int
    init_lines: int
    mod_lines: int
    userid: str
    flags: int = 0


def _unpack(b: bytes) -> int | None:
    """Packed decimal digits with no sign nibble (every nibble 0-9)."""
    n = 0
    for x in b:
        hi, lo = x >> 4, x & 15
        if hi > 9 or lo > 9:
            return None
        n = n * 100 + hi * 10 + lo
    return n


def _unpack_date(b: bytes) -> date | None:
    """0CYYDDDF: C = 0 for 19xx, 1 for 20xx. The sign nibble is F, or C (some tools write it)."""
    h = b.hex()
    if len(h) != 8 or h[0] != "0" or h[1] not in "01" or h[7] not in "fc" or not h[2:7].isdigit():
        return None
    year = 1900 + 100 * int(h[1]) + int(h[2:4])
    ddd = int(h[4:7])
    if not 1 <= ddd <= (366 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 365):
        return None
    return date(year, 1, 1) + timedelta(days=ddd - 1)


def _pack_date(d: date) -> bytes:
    if not 1900 <= d.year <= 2099:
        raise ValueError(f"year {d.year} cannot be stored as 0CYYDDDF")
    ddd = d.timetuple().tm_yday
    c, yy = (d.year - 1900) // 100, d.year % 100
    return bytes.fromhex(f"0{c}{yy:02d}{ddd:03d}f")


def _pack2(n: int) -> bytes:
    return bytes([(n // 10) << 4 | n % 10])


def parse_ispf(userdata: bytes) -> IspfStats | None:
    """Decode ISPF statistics, or None if the user data is not ISPF-shaped (wrong length, bad
    packed digits or an impossible date/time), e.g. load module attributes."""
    n = len(userdata)
    if n not in (ISPF_STD_LEN, ISPF_EXT_LEN):
        return None
    u = bytes(userdata)
    flags = u[2]
    if (n == ISPF_EXT_LEN) != bool(flags & ISPF_FLAG_EXT):
        return None
    sec, hh, mm = _unpack(u[3:4]), _unpack(u[12:13]), _unpack(u[13:14])
    created, changed_d = _unpack_date(u[4:8]), _unpack_date(u[8:12])
    if None in (sec, hh, mm, created, changed_d) or sec > 59 or hh > 23 or mm > 59:
        return None
    if n == ISPF_STD_LEN:
        lines, init, mod = (int.from_bytes(u[i:i + 2], "big") for i in (14, 16, 18))
    else:
        lines, init, mod = (int.from_bytes(u[i:i + 4], "big") for i in (28, 32, 36))
    return IspfStats(
        version=u[0], mod=u[1], created=created,
        changed=datetime(changed_d.year, changed_d.month, changed_d.day, hh, mm, sec),
        lines=lines, init_lines=init, mod_lines=mod,
        userid=ebcdic.to_text(u[20:28], _NAME_CP).rstrip(), flags=flags)


def build_ispf(s: IspfStats) -> bytes:
    """Encode ISPF statistics: 30 bytes, or 40 with flag 0x20 and 4-byte counts when a count
    exceeds 65535 (or the stats already carry the extended flag). In the extended layout the
    16-bit count fields hold min(count, 65535)."""
    counts = (s.lines, s.init_lines, s.mod_lines)
    extended = bool(s.flags & ISPF_FLAG_EXT) or max(counts) > 0xFFFF
    flags = s.flags | ISPF_FLAG_EXT if extended else s.flags & ~ISPF_FLAG_EXT
    c = s.changed
    out = bytearray([s.version, s.mod, flags]) + _pack2(c.second)
    out += _pack_date(s.created) + _pack_date(c.date()) + _pack2(c.hour) + _pack2(c.minute)
    for v in counts:
        out += min(v, 0xFFFF).to_bytes(2, "big")
    out += ebcdic.from_text(s.userid[:8].ljust(8), _NAME_CP)
    if extended:
        for v in counts:
            out += v.to_bytes(4, "big")
    else:
        out += b"\x40\x40"
    return bytes(out)


def validate_member_name(name: str) -> str:
    """Member name folded to uppercase: 1-8 of A-Z 0-9 @ # $, not starting with a digit."""
    n = name.strip()
    if not n.isascii() or not _MEMBER_RE.fullmatch(n.upper()):
        raise ValidationError(f"Invalid member name {name!r}: 1 to 8 characters A-Z, 0-9, "
                              f"@, # or $, not starting with a digit")
    return n.upper()


class Pds:
    def __init__(self, volume: Volume, dsname: str):
        self.volume = volume
        self.dsname = dsname
        self.io = DatasetIO(volume, dsname)
        self.recfm = recfm_string(self.io.f1.recfm)
        self.lrecl = self.io.f1.lrecl

    def _dir_blocks(self):
        """Yield ((TT, R), record) for each directory block, up to the first EOF."""
        for ttr, rec in self.io.blocks():
            if len(rec.key) != DIR_KEY_LEN or len(rec.data) != DIR_DATA_LEN:
                raise FormatError(f"{self.dsname}: directory block at TTR {ttr[0]:04X}{ttr[1]:02X} "
                                  f"has key {len(rec.key)} and data {len(rec.data)} bytes, "
                                  f"expected {DIR_KEY_LEN} and {DIR_DATA_LEN}")
            yield ttr, rec

    def _read_dir(self) -> "_Dir":
        """One pass over the directory blocks."""
        blocks = list(self._dir_blocks())
        entries: list[DirEntry] = []
        raws: list[bytes] = []
        used = 0
        for total, (_, rec) in enumerate(blocks, 1):
            data = rec.data
            nbytes = int.from_bytes(data[:2], "big")
            if not 2 <= nbytes <= DIR_DATA_LEN:
                raise FormatError(f"{self.dsname}: directory block {total} has invalid "
                                  f"byte count {nbytes}")
            pos = 2
            while pos + 12 <= nbytes:
                if data[pos:pos + 8] == END_NAME:
                    used = total
                    break
                c = data[pos + 11]
                end = pos + 12 + 2 * (c & C_USER)
                if end > nbytes:
                    raise FormatError(f"{self.dsname}: directory entry overruns block {total}")
                entries.append(DirEntry(
                    name=ebcdic.to_text(data[pos:pos + 8], _NAME_CP).rstrip(),
                    ttr=(int.from_bytes(data[pos + 8:pos + 10], "big"), data[pos + 10]),
                    alias=bool(c & C_ALIAS),
                    ttrn_count=(c & C_TTRN) >> 5,
                    userdata=bytes(data[pos + 12:end])))
                raws.append(bytes(data[pos:end]))
                pos = end
            if used:
                break
        if blocks and not used:
            raise FormatError(f"{self.dsname}: directory has no end-of-directory entry")
        return _Dir(entries, raws, blocks, used)

    def directory(self) -> list[DirEntry]:
        return self._read_dir().entries

    def dir_block_count(self) -> int:
        """Number of directory blocks allocated (formatted) at the start of the dataset."""
        return len(self._read_dir().blocks)

    def dir_blocks_used(self) -> int:
        """Directory blocks up to and including the one holding the end-of-directory entry."""
        return self._read_dir().used

    def _find(self, name: str) -> DirEntry:
        d = self._read_dir()
        return d.entries[d.index(name, self.dsname)]

    def _blocks_at(self, ttr: tuple[int, int]) -> list[tuple[tuple[int, int], bytes]]:
        if ttr == (0, 0):
            return []
        return [(t, bytes(rec.data)) for t, rec in self.io.blocks(from_ttr=ttr)]

    def read_member_blocks(self, name: str) -> list[tuple[tuple[int, int], bytes]]:
        """[((TT, R), block bytes)] of a member; its data runs from its TTR to the next EOF.
        TTR 0 (an entry with no data) has no blocks."""
        return self._blocks_at(self._find(name).ttr)

    def read_member(self, name: str) -> list[bytes]:
        return deblock_stream((b for _, b in self.read_member_blocks(name)), self.recfm, self.lrecl)

    # -- writing ---------------------------------------------------------------------------
    # Every write method must run inside a transaction. Refusals (bad name, overlength line,
    # RECFM U, directory full, no space for an extend, a TTR that cannot be encoded) are
    # raised before anything is staged; a failure after the first staged change poisons the
    # transaction (DasdImage.mutating). Each operation reads the directory once.

    validate_member_name = staticmethod(validate_member_name)

    def _check_editable(self) -> None:
        f1 = self.io.f1
        dsorg = dsorg_string(f1)
        ok, why = editability(f1, dsorg, self.recfm)
        if not ok:
            raise UnsupportedError(f"{self.dsname} cannot be edited: {why}")
        if dsorg != "PO":
            raise UnsupportedError(f"{self.dsname} is not a partitioned dataset")

    def _plan_directory(self, d: "_Dir", raws: list[bytes]) -> list[tuple[int, tuple[bytes, bytes]]]:
        """[(directory block index, (key, data))] needed to turn directory `d` into one holding
        `raws` (raw entries in any order), sorted by EBCDIC name and packed from the first
        block. Blocks whose bytes would not change are left out; blocks that held entries
        before and now come after the terminator block become key X'00'*8 + 256 zero bytes (as
        z/OS and dasdload format them). DirectoryFullError when the allocated blocks are too
        few. Stages nothing."""
        new = _pack_directory(raws)
        if len(new) > len(d.blocks):
            raise DirectoryFullError(
                f"{self.dsname}: directory full ({len(d.blocks)} blocks allocated, "
                f"{len(new)} needed for {len(raws)} entries)")
        plan = []
        for i, (_, rec) in enumerate(d.blocks):
            if i < len(new):
                key, data = new[i]
                n = int.from_bytes(data[:2], "big")
                if rec.key == key and rec.data[:n] == data[:n]:
                    continue                    # same entries: keep the old bytes, stale tail too
                plan.append((i, (key, data)))
            elif i < d.used:
                plan.append((i, (bytes(DIR_KEY_LEN), bytes(DIR_DATA_LEN))))
        return plan

    def _stage_directory(self, d: "_Dir", plan: list[tuple[int, tuple[bytes, bytes]]]) -> None:
        """Rewrite the planned blocks of directory `d` in place (same tracks and record
        numbers). Directory blocks never move, so `d` stays valid across member data writes."""
        by_track: dict[int, dict[int, tuple[bytes, bytes]]] = {}
        for i, kd in plan:
            tt, r = d.blocks[i][0]
            by_track.setdefault(tt, {})[r] = kd
        for tt, recs in by_track.items():
            self._rewrite_records(tt, recs)

    def _rewrite_records(self, tt: int, new: dict[int, tuple[bytes | None, bytes]]) -> None:
        self.io.rewrite_records(tt, new)

    @property
    def image(self):
        return self.volume.image

    def _append_start(self) -> tuple[int, int]:
        """TTR where new member data goes, as BPAM writes it: right after the EOF at DS1LSTAR
        (the EOF ending the last member, or the directory when the PDS is empty). That EOF
        stays: every member's data, like the directory, ends with its own EOF record."""
        tt, r = self.io.end_of_data()
        if (tt, r) == (0, 0):
            raise FormatError(f"{self.dsname}: DS1LSTAR is 0, no end of data to append after")
        recs = {x.rec: x for x in self.io.read_rel_track(tt)}
        if r not in recs or not is_eof(recs[r]):
            raise FormatError(f"{self.dsname}: DS1LSTAR {tt}/{r} is not an end-of-file record")
        return tt, r + 1

    def _old_records(self, ttr: tuple[int, int]) -> list[bytes]:
        return deblock_stream((b for _, b in self._blocks_at(ttr)), self.recfm, self.lrecl)

    def save_member(self, name: str, records: list[bytes], userid: str) -> None:
        """Add or replace a member: reblock the records and write them after the current end
        of data, then point the directory entry at the first block (at the member's EOF when
        it has no records). The old data (and any alias pointing at it) is left in place, as
        ISPF/BPAM do: aliases of a replaced member keep showing the old data until they are
        deleted or the PDS is compressed.

        ISPF statistics: a replaced member with stats gets mod+1 (at most 99), changed = now,
        lines = the new count, userid, and mod_lines = old mod_lines + the lines added,
        removed or changed (difflib); version, created and initial lines are kept. Counts over
        65535 switch to the extended layout (build_ispf). A new member, or one without
        (ISPF-shaped) stats, gets fresh 01.00 stats. Saving under an alias name makes that
        name a main member of the new data.

        RECFM U is refused (new members cannot be typed in, and rewriting a load module
        elsewhere would break the TTRs in its directory entry): use patch_member_block."""
        name = validate_member_name(name)
        self._check_editable()
        if self.recfm[0] == "U":
            raise UnsupportedError(f"{self.dsname} is RECFM U: members cannot be saved, "
                                   f"only patched in place")
        if self.io.f1.keylen:
            raise UnsupportedError(f"{self.dsname}: keyed datasets are not supported")
        blocks = reblock(records, self.recfm, self.lrecl, self.io.f1.blksize)
        d = self._read_dir()
        name_b = ebcdic.from_text(name.ljust(8), _NAME_CP)
        i = next((k for k, r in enumerate(d.raws) if r[:8] == name_b), None)
        now = datetime.now().replace(microsecond=0)
        stats = parse_ispf(d.raws[i][12:]) if i is not None else None
        if stats is not None:
            stats.mod = min(stats.mod + 1, ISPF_MAX_MOD)
            stats.changed = now
            stats.lines = len(records)
            stats.mod_lines += _changed_lines(self._old_records(d.entries[i].ttr), records)
            stats.userid = userid.upper()
        else:
            stats = IspfStats(version=1, mod=0, created=now.date(), changed=now,
                              lines=len(records), init_lines=len(records), mod_lines=0,
                              userid=userid.upper())
        userdata = build_ispf(stats)
        rest = d.raws[:i] + d.raws[i + 1:] if i is not None else list(d.raws)

        start = self._append_start()
        first = self.io.first_ttr(start, blocks, "PO")
        _check_ttr(first, self.dsname)
        entry = _entry(name_b, first, len(userdata) // 2, userdata)
        plan = self._plan_directory(d, rest + [entry])          # DirectoryFullError: nothing staged
        self.io.write_blocks(start, blocks, ending="PO")         # own pre-checks, own poisoning
        with self.image.mutating():
            self._stage_directory(d, plan)

    def delete_member(self, name: str) -> None:
        """Remove one directory entry. Its data stays until a compress; aliases of it (or the
        main member, when an alias is deleted) are untouched."""
        self._check_editable()
        d = self._read_dir()
        i = d.index(name, self.dsname)
        plan = self._plan_directory(d, d.raws[:i] + d.raws[i + 1:])
        with self.image.mutating():
            self._stage_directory(d, plan)
            self.io.mark_updated()

    def rename_member(self, old: str, new: str) -> None:
        """Rename one entry (alias flag, TTR and user data are kept). An existing `new` is
        refused. As with STOW's change function, nothing else is updated: the user data of
        aliases of a renamed load module still names the old main member."""
        new = validate_member_name(new)
        self._check_editable()
        d = self._read_dir()
        i = d.index(old, self.dsname)
        new_b = ebcdic.from_text(new.ljust(8), _NAME_CP)
        if any(r[:8] == new_b for r in d.raws):
            raise ValidationError(f"Member {new} already exists in {self.dsname}")
        plan = self._plan_directory(d, d.raws[:i] + [new_b + d.raws[i][8:]] + d.raws[i + 1:])
        with self.image.mutating():
            self._stage_directory(d, plan)
            self.io.mark_updated()

    def patch_member_block(self, name: str, block_index: int, new_bytes: bytes,
                           userid: str | None = None) -> None:
        """Overwrite one block of a member in place (hex overtype); see patch_member_blocks."""
        self.patch_member_blocks(name, {block_index: new_bytes}, userid)

    def patch_member_blocks(self, name: str, patches: dict[int, bytes],
                            userid: str | None = None) -> None:
        """Overwrite blocks of a member in place (hex overtype): {block index (0-based): new
        bytes}, same length only, nothing moves. Messages number blocks from 1, as the hex
        screen does. Every patch is checked before anything is staged, so
        the caller's transaction applies all of them or none. If the member has ISPF
        statistics they get mod+1 (once), changed = now and, when given, the userid; other
        user data (load module attributes) is left alone."""
        self._check_editable()
        if not patches:
            raise ValidationError("no blocks to patch")
        d = self._read_dir()
        i = d.index(name, self.dsname)
        blocks = self._blocks_at(d.entries[i].ttr)
        by_track: dict[int, dict[int, tuple[None, bytes]]] = {}
        for block_index, new_bytes in sorted(patches.items()):
            if not 0 <= block_index < len(blocks):
                raise ValidationError(f"Member {d.entries[i].name} has {len(blocks)} blocks; "
                                      f"there is no block {block_index + 1}")
            (tt, r), old = blocks[block_index]
            if len(new_bytes) != len(old):
                raise ValidationError(f"block {block_index + 1} is {len(old)} bytes; the patch "
                                      f"is {len(new_bytes)} (block lengths cannot change)")
            by_track.setdefault(tt, {})[r] = (None, bytes(new_bytes))
        plan = []
        raw = d.raws[i]
        stats = parse_ispf(raw[12:])
        if stats is not None:
            stats.mod = min(stats.mod + 1, ISPF_MAX_MOD)
            stats.changed = datetime.now().replace(microsecond=0)
            if userid:
                stats.userid = userid.upper()
            ud = build_ispf(stats)
            plan = self._plan_directory(
                d, d.raws[:i] + [_entry(raw[:8], None, len(ud) // 2, ud, prefix=raw[8:12])]
                + d.raws[i + 1:])
        with self.image.mutating():
            for tt, recs in by_track.items():
                self._rewrite_records(tt, recs)
            self._stage_directory(d, plan)
            self.io.mark_updated()

    # -- compress --------------------------------------------------------------------------
    def compress(self) -> dict:
        """Compress in place, like IEBCOPY: the directory blocks and their EOF stay; every
        distinct member data stream (by TTR, in TTR order; aliases share one copy) is copied
        block for block (same lengths and bytes) right after them, each with its own EOF, and
        DS1LSTAR becomes the last member's EOF. Freed tracks stay allocated.

        Every block's old TTR maps to its new one. Directory entries get their TTR relocated,
        and the TTR part of the first ttrn_count TTRN fields of their user data; a TTRN with
        N > 0 points to a note list, a block of N 4-byte TTRx entries, which are relocated too.
        Nothing else changes (ISPF statistics, load module attributes). A TTR of 0 is not a
        pointer and stays 0.

        Everything is read and the whole layout planned before anything is staged; a pointer
        that leads outside every member's data (or a keyed member block) is refused then. An
        already compact PDS is left untouched. Returns tracks used before and after (DS1LSTAR
        TT + 1) and the number of directory entries."""
        self._check_editable()
        d = self._read_dir()
        for e in d.entries:
            # Load module user data: PDS2TTRT(3) 0(1) PDS2TTRN(3) PDS2NL(1) PDS2ATR1(1) ...
            # An overlay or scatter-load module keeps a TTR in PDS2TTRN; when the C byte
            # does not count that field as a TTRN, nothing would relocate it.
            if (len(e.userdata) >= 9 and e.userdata[8] & (PDS2OVLY | PDS2SCTR)
                    and e.ttrn_count < 2 and any(e.userdata[4:7])):
                raise UnsupportedError(
                    f"{self.dsname}: member {e.name} is an overlay or scatter-load module whose "
                    f"TTR in the directory is not counted as a TTRN; compress refused")
        last = self.io.end_of_data()
        start = self._directory_end(d)
        names = {}
        for e in d.entries:
            names.setdefault(e.ttr, e.name)

        tracks_read: dict[int, list] = {}
        copies: list[tuple[list[tuple[int, int]], list[bytes]]] = []
        where: dict[tuple[int, int], tuple[int, int]] = {}     # old TTR -> (copy, position)
        for ttr in sorted(set(names) - {(0, 0)}):
            if ttr in where:
                continue                    # inside an earlier stream: shares its tail
            ttrs, data = self._member_stream(ttr, names[ttr], start, last, tracks_read)
            for k, t in enumerate(ttrs):
                where[t] = (len(copies), k)
            copies.append((ttrs, data))

        tracks, new = self.io.plan_members(start, [data for _, data in copies])
        moved = {t: new[c][k] for t, (c, k) in where.items()}
        for t in moved.values():
            _check_ttr(t, self.dsname)
        raws, note_lists = self._relocate_entries(d, moved)
        tt0 = start[0]
        for t, n in note_lists.items():
            c, k = where[t]
            if k >= len(copies[c][1]):
                raise FormatError(f"{self.dsname}: note list TTR {_ttr_str(t)} is an end-of-file "
                                  f"record; compress refused")
            ntt, nr = moved[t]
            tracks[ntt - tt0][nr - 1] = (b"", self._relocate_note_list(t, copies[c][1][k], n, moved))

        lstar = new[-1][-1] if new else (start[0], start[1] - 1)
        result = {"tracks_before": last[0] + 1, "tracks_after": lstar[0] + 1,
                  "members": len(d.entries)}
        if lstar == last and all(old == t for old, t in moved.items()):
            return result                   # already compact: every block stays where it is
        plan = self._plan_directory(d, raws)
        self.io.write_planned(tt0, tracks)  # NoSpaceError before staging; own poisoning
        with self.image.mutating():
            self._stage_directory(d, plan)
        return result

    def _directory_end(self, d: "_Dir") -> tuple[int, int]:
        """TTR right after the EOF that ends the directory blocks."""
        if not d.blocks:
            raise FormatError(f"{self.dsname}: no directory blocks")
        tt, r = d.blocks[-1][0]
        recs = {x.rec: x for x in self.io.read_rel_track(tt)}
        if r + 1 not in recs:
            tt, r = tt + 1, 0
            recs = {x.rec: x for x in self.io.read_rel_track(tt)}
        if r + 1 not in recs or not is_eof(recs[r + 1]):
            raise FormatError(f"{self.dsname}: the directory blocks are not followed by an "
                              f"end-of-file record")
        return tt, r + 2

    def _member_stream(self, ttr, name, start, last, cache) -> tuple[list[tuple[int, int]], list[bytes]]:
        """(TTRs of the blocks and the closing EOF, block bytes) of the data starting at ttr."""
        where = f"{self.dsname}: member {name} (TTR {_ttr_str(ttr)})"
        if ttr < start:
            raise FormatError(f"{where} points into the directory; compress refused")
        if ttr > last:
            raise FormatError(f"{where} points past the end of data; compress refused")
        ttrs, data = [], []
        tt, r = ttr
        while tt <= last[0]:
            if tt not in cache:
                cache[tt] = self.io.read_rel_track(tt)
            recs = [x for x in cache[tt] if x.rec >= r]
            if (tt, r) == ttr and (not recs or recs[0].rec != r):
                raise FormatError(f"{where}: there is no such record; compress refused")
            for x in recs:
                if (tt, x.rec) > last:
                    break
                ttrs.append((tt, x.rec))
                if is_eof(x):
                    return ttrs, data
                if x.key:
                    raise UnsupportedError(f"{where} has a keyed block at TTR "
                                           f"{_ttr_str((tt, x.rec))}; compress refused")
                data.append(bytes(x.data))
            tt, r = tt + 1, 1
        raise FormatError(f"{where}: its data has no end-of-file record before the end of "
                          f"data; compress refused")

    def _relocate_entries(self, d: "_Dir", moved) -> tuple[list[bytes], dict[tuple[int, int], int]]:
        """Raw entries with the TTR and the TTRN fields relocated, and {old note list TTR: N}."""
        raws, note_lists = [], {}
        for e, raw in zip(d.entries, d.raws):
            out = bytearray(raw)
            if e.ttr != (0, 0):
                out[8:11] = _ttr3(moved[e.ttr])
            if 4 * e.ttrn_count > len(e.userdata):
                raise FormatError(f"{self.dsname}: member {e.name} has {e.ttrn_count} TTRNs but "
                                  f"{len(e.userdata)} bytes of user data; compress refused")
            for k in range(e.ttrn_count):
                o = 12 + 4 * k
                t, n = (int.from_bytes(raw[o:o + 2], "big"), raw[o + 2]), raw[o + 3]
                if t == (0, 0):
                    continue
                if t not in moved:
                    raise FormatError(f"{self.dsname}: member {e.name}: TTRN {k + 1} points to "
                                      f"TTR {_ttr_str(t)}, which is not in any member's data; "
                                      f"compress refused")
                out[o:o + 3] = _ttr3(moved[t])
                if n:
                    if note_lists.setdefault(t, n) != n:
                        raise FormatError(f"{self.dsname}: note list at TTR {_ttr_str(t)} has "
                                          f"{note_lists[t]} and {n} entries; compress refused")
            raws.append(bytes(out))
        return raws, note_lists

    def _relocate_note_list(self, ttr, block: bytes, n: int, moved) -> bytes:
        """The note list block with the TTR part of its first n 4-byte TTRx entries relocated."""
        where = f"{self.dsname}: note list at TTR {_ttr_str(ttr)}"
        if len(block) < 4 * n:
            raise FormatError(f"{where} is {len(block)} bytes, too short for {n} entries; "
                              f"compress refused")
        out = bytearray(block)
        for i in range(n):
            t = (int.from_bytes(block[4 * i:4 * i + 2], "big"), block[4 * i + 2])
            if t == (0, 0):
                continue
            if t not in moved:
                raise FormatError(f"{where}: entry {i + 1} points to TTR {_ttr_str(t)}, which "
                                  f"is not in any member's data; compress refused")
            out[4 * i:4 * i + 3] = _ttr3(moved[t])
        return bytes(out)


@dataclass
class _Dir:
    """One read of a directory: entries, the same entries as raw bytes, every formatted block
    ((TT, R), record), and the blocks used up to the terminator."""
    entries: list[DirEntry]
    raws: list[bytes]
    blocks: list
    used: int

    def index(self, name: str, dsname: str) -> int:
        """Index of an existing member (matched as stored, so a member whose name we would
        not accept can still be deleted or renamed)."""
        key = name.strip().upper()
        for i, e in enumerate(self.entries):
            if e.name == key:
                return i
        raise NotFoundError(f"Member {key} not found in {dsname}")


def _changed_lines(old: list[bytes], new: list[bytes]) -> int:
    """Lines added, removed or changed between two versions (difflib opcodes)."""
    n = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if op != "equal":
            n += max(i2 - i1, j2 - j1)
    return n


def _check_ttr(ttr: tuple[int, int], dsname: str) -> None:
    tt, r = ttr
    if not (0 <= tt <= 0xFFFF and 1 <= r <= 0xFF):
        raise NoSpaceError(f"{dsname}: TTR {tt}/{r} cannot be stored in a directory entry")


def _ttr3(ttr: tuple[int, int]) -> bytes:
    return ttr[0].to_bytes(2, "big") + bytes([ttr[1]])


def _ttr_str(ttr: tuple[int, int]) -> str:
    return f"{ttr[0]:04X}{ttr[1]:02X}"


def _entry(name_b: bytes, ttr, halfwords: int, userdata: bytes, prefix: bytes | None = None) -> bytes:
    """name(8) TTR(3) C(1) userdata. `prefix` (TTR + C) is kept as is, apart from the user data
    halfword count, when given."""
    if prefix is not None:
        head = prefix[:3] + bytes([prefix[3] & ~C_USER & 0xFF | halfwords])
    else:
        tt, r = ttr                     # callers check with _check_ttr before staging
        head = tt.to_bytes(2, "big") + bytes([r, halfwords])
    return name_b + head + userdata


TERMINATOR = END_NAME + bytes(4)


def _pack_directory(raws: list[bytes]) -> list[tuple[bytes, bytes]]:
    """[(key, 256 data bytes)] of the used directory blocks for these raw entries."""
    out = []
    cur: list[bytes] = []
    size = 2
    for e in sorted(raws, key=lambda r: r[:8]) + [TERMINATOR]:
        if size + len(e) > DIR_DATA_LEN:
            out.append(cur)
            cur, size = [], 2
        cur.append(e)
        size += len(e)
    out.append(cur)
    blocks = []
    for es in out:
        body = b"".join(es)
        data = (len(body) + 2).to_bytes(2, "big") + body
        blocks.append((es[-1][:8], data.ljust(DIR_DATA_LEN, b"\0")))
    return blocks
