"""SYS1.VTOCIX.volser: read and update the VTOC index space maps.

Only the VPSM (track and cylinder bits) and the VMDS (DSCB bits) are ever
changed; the VIXM and the VIERs are left alone (an extension allocates no VIR
and adds no index entry). Format and sources:
docs/notes/vtocix.md.

A VtocIndex snapshot belongs to one transaction: build it, mark, then
stage()/write_staged() before commit. Re-create it after commit or rollback.
"""

import struct
from collections.abc import Iterable
from dataclasses import dataclass

from ckdoffline.dataset.extents import DatasetIO
from ckdoffline.errors import FormatError, UnsupportedError
from ckdoffline.track import build_track, parse_track
from ckdoffline.vtoc.vtoc import DS4VTOCI_DIRF, Volume

VIR_LEN = 2048
MAP_HDR = struct.Struct(">4sIII HH BBHI BBHI")   # VIMID .. VIMSUSZ (36 bytes)
VIMVIR_OFF = 39
VIMFHLV_OFF = 40
VIMVRFSW = 0x80
ID_VIXM, ID_VPSM, ID_VMDS = (s.encode("cp037") for s in ("VIXM", "VPSM", "VMDS"))
TRACK_MANAGED_CYLS = 65520


@dataclass
class _Map:
    """One map VIR: its small-unit map covers entries org .. org+susz-1, its
    large-unit map (VPSM only) cylinders org//subit .. +lusz-1."""
    vir: int
    org: int
    luof: int
    lusz: int
    subit: int
    suof: int
    susz: int


def _get(buf, off: int, i: int) -> bool:
    return bool(buf[off + i // 8] & (0x80 >> (i % 8)))


def _put(buf, off: int, i: int, on: bool) -> None:
    if on:
        buf[off + i // 8] |= 0x80 >> (i % 8)
    else:
        buf[off + i // 8] &= ~(0x80 >> (i % 8)) & 0xFF


class VtocIndex:
    def __init__(self, volume: Volume):
        if not volume.indexed:
            raise UnsupportedError(f"{volume.volser} has no active VTOC index")
        self.volume = volume
        self.image = volume.image
        self.heads = self.image.heads
        self.unsafe_reason: str | None = None
        if self.image.cyls > TRACK_MANAGED_CYLS:
            raise UnsupportedError("VTOC index on an extended address volume is not supported")
        dsname = f"SYS1.VTOCIX.{volume.volser}"
        dio = DatasetIO(volume, dsname)
        if dio.f1.blksize != VIR_LEN:
            raise UnsupportedError(f"VTOC index block size {dio.f1.blksize} is not supported")
        self._addr: list[tuple] = []          # VIR number -> (cyl, head, rec)
        self._orig: list[bytes] = []
        for tt in range(dio.total_tracks):
            cyl, head = dio.rel_to_cchh(tt)
            for r in parse_track(self.image.read_track(cyl, head))[1]:
                if r.key or len(r.data) != VIR_LEN:
                    raise FormatError(f"{dsname}: record {cyl}/{head}/{r.rec} is not a 2048-byte VIR")
                self._addr.append((cyl, head, r.rec))
                self._orig.append(r.data)
        self._buf = [bytearray(d) for d in self._orig]
        self._load_maps(dsname)
        self._check_state()

    # -- loading ------------------------------------------------------------
    def _hdr(self, n: int, want: bytes) -> _Map:
        if n >= len(self._buf):
            raise FormatError(f"VTOC index map pointer to VIR {n} is outside the index")
        f = MAP_HDR.unpack_from(self._buf[n])
        vimid, rba, _, org, _, _, _, _, luof, lusz, _, subit, suof, susz = f
        if vimid != want or rba != n * VIR_LEN:
            raise FormatError(f"VTOC index VIR {n} is not a valid {want.decode('cp037')} map")
        m = _Map(n, org, luof, lusz, subit, suof, susz)
        # VIMLUSZ is only meaningful with a large-unit map (the VIXM has none
        # but a nonzero VIMLUSZ; see the notes)
        for off, bits in ((luof, lusz if luof else 0), (suof, susz)):
            if bits and (off < MAP_HDR.size + 8 or off + (bits + 7) // 8 > VIR_LEN):
                raise FormatError(f"VTOC index VIR {n}: map runs outside the record")
        return m

    def _chain(self, first: int, want: bytes) -> list[_Map]:
        out, n = [], first
        while True:
            out.append(self._hdr(n, want))
            hz = struct.unpack_from(">I", self._buf[n], 8)[0]
            if not hz:
                return out
            if hz % VIR_LEN or len(out) >= self.nmaps:
                raise FormatError("VTOC index map chain is broken")
            n = hz // VIR_LEN

    def _load_maps(self, dsname: str) -> None:
        vixm = self._buf[0] if self._buf else b""
        if vixm[:4] != ID_VIXM:
            raise FormatError(f"{dsname}: first record is not the VIXM")
        self.vixm = self._hdr(0, ID_VIXM)
        self.nmaps = vixm[VIMVIR_OFF]
        self._vrf_pending = bool(vixm[20] & VIMVRFSW)
        n = len(self._chain(0, ID_VIXM))                 # VIXM, VPSM, VMDS in order
        self.vpsm = self._chain(n, ID_VPSM)
        self.vmds = self._chain(n + len(self.vpsm), ID_VMDS)
        if n + len(self.vpsm) + len(self.vmds) != self.nmaps:
            raise FormatError("VTOC index map count (VIMVIR) does not match the maps found")
        # VPSM: whole cylinders, contiguous from track 0 to the end of the volume
        nxt = 0
        for m in self.vpsm:
            if (m.subit != self.heads or m.org != nxt or m.org % self.heads
                    or m.susz != m.lusz * self.heads or not m.luof):
                raise FormatError(f"VTOC index VPSM VIR {m.vir} has an unexpected layout")
            nxt += m.susz
        if nxt != self.image.cyls * self.heads:
            raise FormatError(f"VTOC index VPSM maps {nxt} tracks; the volume has "
                              f"{self.image.cyls * self.heads}")
        # VMDS: one bit per DSCB slot of the VTOC extent
        e = self.volume.vtoc_extent
        self._vtoc_first = e.bcyl * self.heads + e.bhead
        self._vtoc_tracks = e.tracks(self.heads)
        self._per_trk = self.volume.f4.devdt
        nxt = 0
        for m in self.vmds:
            if m.subit != self._per_trk or m.org != nxt or m.lusz:
                raise FormatError(f"VTOC index VMDS VIR {m.vir} has an unexpected layout")
            nxt += m.susz
        if nxt != self._vtoc_tracks * self._per_trk:
            raise FormatError(f"VTOC index VMDS maps {nxt} DSCBs; the VTOC has "
                              f"{self._vtoc_tracks * self._per_trk}")

    def _check_state(self) -> None:
        f4 = self.volume.read_dscb(self.volume.vtoc_cchhr)
        if f4.vtoci & DS4VTOCI_DIRF:
            self.unsafe_reason = "a VTOC change is incomplete (DS4DIRF set)"
        elif self._vrf_pending:
            self.unsafe_reason = "VTOC recording facility data is pending (VIMVRFSW set)"
        else:
            bad = next((c for c in range(self.image.cyls)
                        if self.cylinder_in_use(c) != self._any_track(c)), None)
            if bad is not None:
                self.unsafe_reason = f"cylinder map disagrees with track map at cylinder {bad}"

    # -- bit access -----------------------------------------------------------
    def _vpsm_for(self, trk: int) -> _Map:
        for m in self.vpsm:
            if m.org <= trk < m.org + m.susz:
                return m
        raise FormatError(f"track {trk} is outside the volume")

    def _track_bit(self, trk: int) -> bool:
        m = self._vpsm_for(trk)
        return _get(self._buf[m.vir], m.suof, trk - m.org)

    def _any_track(self, cyl: int) -> bool:
        return any(self._track_bit(cyl * self.heads + h) for h in range(self.heads))

    def cylinder_in_use(self, cyl: int) -> bool:
        """The VPSM cylinder (large-unit) bit: on if any track is allocated."""
        m = self._vpsm_for(cyl * self.heads)
        return _get(self._buf[m.vir], m.luof, cyl - m.org // self.heads)

    def _dscb_index(self, cchhr) -> int:
        cyl, head, rec = cchhr
        t = cyl * self.heads + head - self._vtoc_first
        if not (0 <= t < self._vtoc_tracks and 1 <= rec <= self._per_trk and 0 <= head < self.heads):
            raise FormatError(f"DSCB {cyl}/{head}/{rec} is outside the VTOC")
        return t * self._per_trk + rec - 1

    def _vmds_for(self, i: int) -> _Map:
        return next(m for m in self.vmds if m.org <= i < m.org + m.susz)

    def _require_safe(self) -> None:
        if self.unsafe_reason:
            raise UnsupportedError(f"VTOC index cannot be updated: {self.unsafe_reason}")

    # -- public interface -------------------------------------------------
    def free_tracks(self) -> set[int]:
        out = set()
        for m in self.vpsm:
            buf = self._buf[m.vir]
            out.update(m.org + i for i in range(m.susz) if not _get(buf, m.suof, i))
        return out

    def mark_tracks(self, tracks: Iterable[int], used: bool) -> None:
        """Allocate (used=True) or free tracks; all or nothing. FormatError if a
        track is outside the volume or already in the requested state."""
        self._require_safe()
        tracks = sorted(set(tracks))
        total = self.image.cyls * self.heads
        for t in tracks:
            if not 0 <= t < total:
                raise FormatError(f"track {t} is outside the volume")
            if self._track_bit(t) == used:
                cyl, head = divmod(t, self.heads)
                raise FormatError(f"VTOC index out of sync: track {cyl}/{head} is already "
                                  f"{'allocated' if used else 'free'}")
        for t in tracks:
            m = self._vpsm_for(t)
            _put(self._buf[m.vir], m.suof, t - m.org, used)
        for cyl in {t // self.heads for t in tracks}:
            m = self._vpsm_for(cyl * self.heads)
            _put(self._buf[m.vir], m.luof, cyl - m.org // self.heads, self._any_track(cyl))

    def free_dscb_slots(self) -> set[tuple]:
        out = set()
        for m in self.vmds:
            buf = self._buf[m.vir]
            for i in range(m.susz):
                if not _get(buf, m.suof, i):
                    t, r = divmod(m.org + i, self._per_trk)
                    cyl, head = divmod(self._vtoc_first + t, self.heads)
                    out.add((cyl, head, r + 1))
        return out

    def mark_dscb(self, cchhr, used: bool) -> None:
        self._require_safe()
        i = self._dscb_index(cchhr)
        m = self._vmds_for(i)
        if _get(self._buf[m.vir], m.suof, i - m.org) == used:
            raise FormatError(f"VTOC index out of sync: DSCB {'/'.join(map(str, cchhr))} is "
                              f"already {'allocated' if used else 'free'}")
        _put(self._buf[m.vir], m.suof, i - m.org, used)

    def stage(self) -> list[tuple[tuple, bytes]]:
        """(cchhr of the VIR, new 2048 bytes) for every VIR that differs from
        what was read."""
        return [(self._addr[n], bytes(b)) for n, b in enumerate(self._buf) if b != self._orig[n]]

    def write_staged(self) -> None:
        """Write the staged VIRs into the image (inside the caller's
        transaction), one track rewrite per affected track."""
        by_track: dict[tuple, dict[int, bytes]] = {}
        for (cyl, head, rec), data in self.stage():
            by_track.setdefault((cyl, head), {})[rec] = data
        for (cyl, head), recs in by_track.items():
            r0, records = parse_track(self.image.read_track(cyl, head))
            for r in records:
                if r.rec in recs:
                    r.data = recs.pop(r.rec)
            if recs:
                raise FormatError(f"VTOC index VIR missing on track {cyl}/{head}")
            self.image.write_track(cyl, head, build_track(cyl, head, records, r0))
