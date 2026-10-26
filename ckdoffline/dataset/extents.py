"""DatasetIO: relative track <-> CCHH mapping, block reads and writes for one dataset.

End of data, matching how datasets are written by the operating system:
- PO: an EOF record follows the last block (same track if it fits, else R1
  of the next track); DS1LSTAR is the EOF; DS1TRBAL counts every record on
  that track, EOF included.
- PS: DS1LSTAR is the last data block; DS1TRBAL counts the data records on
  that track; the EOF is R1 of the next track, or absent when the data ends
  on the last allocated track. Empty: DS1LSTAR = 0, DS1TRBAL = a full track,
  track 0 holds only an EOF as R1.
"""

import datetime
from collections.abc import Iterator

from ckdoffline.errors import FormatError, NoSpaceError, ValidationError
from ckdoffline.track import Record, build_track, fits_on_track, is_eof, parse_track, track_balance
from ckdoffline.vtoc.dscb import Format1
from ckdoffline.vtoc.vtoc import Volume

DSIND_UPDATED = 0x02          # DS1DSIND: changed since last backup
MAX_RECORDS_PER_TRACK = 255   # R is one byte
R0_DEFAULT = b"\0" * 8
ENDINGS = ("PS", "PO")


class DatasetIO:
    def __init__(self, volume: Volume, dsname: str):
        self.volume = volume
        self.image = volume.image
        self.dsname = dsname
        self._refresh()

    def _refresh(self) -> None:
        self.f1c, self.f1 = self.volume.f1(self.dsname)
        self.extents = self.volume.extents_of(self.dsname)
        heads = self.image.heads
        self.total_tracks = sum(e.tracks(heads) for e in self.extents)

    def rel_to_cchh(self, tt: int) -> tuple[int, int]:
        """Relative track (0-based, extents in sequence order) -> (cyl, head)."""
        heads = self.image.heads
        if tt < 0:
            raise ValidationError(f"relative track {tt} is outside the dataset")
        rest = tt
        for e in self.extents:
            n = e.tracks(heads)
            if rest < n:
                abs_trk = e.bcyl * heads + e.bhead + rest
                return abs_trk // heads, abs_trk % heads
            rest -= n
        raise ValidationError(f"relative track {tt} is outside the dataset")

    def read_rel_track(self, tt: int) -> list[Record]:
        cyl, head = self.rel_to_cchh(tt)
        return parse_track(self.image.read_track(cyl, head))[1]

    def end_of_data(self) -> tuple[int, int]:
        """(TT, R) of the last used record per DS1LSTAR; (0, 0) means empty."""
        tt, r = self.f1.lstar
        return int(tt), int(r)

    def blocks(self, from_ttr: tuple[int, int] = (0, 1)) -> Iterator[tuple[tuple[int, int], Record]]:
        """Yield ((TT, R), record) from from_ttr. Stops at the first EOF record
        or after the DS1LSTAR record, whichever comes first; nothing when
        from_ttr is past DS1LSTAR."""
        last = self.end_of_data()
        if last == (0, 0) or tuple(from_ttr) > last:
            return
        tt0, r0 = from_ttr
        for tt in range(tt0, self.total_tracks):
            if tt > last[0]:
                return
            for rec in self.read_rel_track(tt):
                if tt == tt0 and rec.rec < r0:
                    continue
                if is_eof(rec):
                    return
                yield (tt, rec.rec), rec
                if (tt, rec.rec) >= last:
                    return

    # -- writing ---------------------------------------------------------------
    def _plan(self, kept: list[tuple[bytes, bytes]], blocks: list[bytes],
              ending: str) -> tuple[list[list[tuple[bytes, bytes]]], int]:
        """Pack blocks onto tracks after the (key, data) records kept on the start
        track. Returns (tracks, data_tracks): for PS the trailing EOF-only track,
        if any, is not counted in data_tracks (it is written only if allocated)."""
        tracks = [list(kept)]

        def shapes(t):
            return [(len(k), len(d)) for k, d in t]

        for b in blocks:
            cur = tracks[-1]
            if cur and (len(cur) >= MAX_RECORDS_PER_TRACK
                        or not fits_on_track(shapes(cur) + [(0, len(b))])):
                tracks.append([])
            tracks[-1].append((b"", bytes(b)))
        eof = (b"", b"")
        cur = tracks[-1]
        if ending == "PO":
            if len(cur) >= MAX_RECORDS_PER_TRACK or not fits_on_track(shapes(cur) + [(0, 0)]):
                tracks.append([])
            tracks[-1].append(eof)
            return tracks, len(tracks)
        if not cur:                       # PS, nothing at all: EOF as R1 of the start track
            cur.append(eof)
            return tracks, 1
        tracks.append([eof])
        return tracks, len(tracks) - 1

    def first_ttr(self, start_ttr: tuple[int, int], blocks: list[bytes],
                  ending: str = "PO") -> tuple[int, int]:
        """TTR that write_blocks(start_ttr, blocks, ending) gives its first new record: the
        first block, or the EOF when there are no blocks (PO). Uses the same track plan, so it
        follows a record onto the next track when it does not fit. Reads only."""
        tt0, r0 = start_ttr
        kept = [(r.key, r.data) for r in self.read_rel_track(tt0) if r.rec < r0]
        tracks, _ = self._plan(kept, blocks, ending)
        if len(tracks[0]) > len(kept):
            return tt0, len(kept) + 1
        return tt0 + 1, 1

    def plan_members(self, start_ttr: tuple[int, int], members: list[list[bytes]]
                     ) -> tuple[list[list[tuple[bytes, bytes]]], list[list[tuple[int, int]]]]:
        """Pack members back to back from start_ttr, each as write_blocks(ending="PO") would
        write it (same _plan rules), the next one starting right after the previous EOF.
        Returns (tracks, ttrs): the (key, data) records of every track from start_ttr's track
        on, the records before start_ttr kept; and per member the TTRs of its blocks followed
        by the TTR of its EOF. Reads only; write the result with write_planned."""
        tt0, _ = start_ttr
        kept = [(r.key, r.data) for r in self._kept(start_ttr)]
        done: list[list[tuple[bytes, bytes]]] = []
        ttrs = []
        for blocks in members:
            self._check_blocks(blocks)
            tracks, _ = self._plan(kept, blocks, "PO")
            base = tt0 + len(done)
            pos = [(base + j, n) for j, recs in enumerate(tracks)
                   for n in range(len(kept) + 1 if j == 0 else 1, len(recs) + 1)]
            assert len(pos) == len(blocks) + 1
            ttrs.append(pos)
            done += tracks[:-1]
            kept = tracks[-1]
        return done + [kept], ttrs

    def write_planned(self, tt0: int, tracks: list[list[tuple[bytes, bytes]]]) -> tuple[int, int]:
        """Write tracks planned by plan_members from relative track tt0 (its last record is an
        EOF: the PO end of data) and update DS1LSTAR, DS1TRBAL, DS1DSIND and DS1REFDT. Never
        extends: NoSpaceError, before anything is staged, when the tracks are not allocated.
        Must run inside a transaction; returns the new DS1LSTAR."""
        if tt0 + len(tracks) > self.total_tracks:
            raise NoSpaceError(f"{self.dsname}: {tt0 + len(tracks)} tracks needed, "
                               f"{self.total_tracks} allocated")
        last = tracks[-1]
        if not last or last[-1] != (b"", b""):
            raise ValueError("planned tracks must end with an EOF record")
        lstar = (tt0 + len(tracks) - 1, len(last))
        with self.image.mutating():
            for i, recs in enumerate(tracks):
                self._write_track(tt0 + i, recs)
            self._update_f1(lstar, track_balance([(len(k), len(d)) for k, d in last]))
        return lstar

    def _kept(self, start_ttr: tuple[int, int]) -> list[Record]:
        """The records before start_ttr on its track, which must be exactly R1..R(r-1)."""
        tt0, r0 = start_ttr
        if r0 < 1:
            raise ValueError(f"start record must be 1 or more, not {r0}")
        kept = [r for r in self.read_rel_track(tt0) if r.rec < r0]
        if [r.rec for r in kept] != list(range(1, r0)):
            raise FormatError(f"{self.dsname}: records before {tt0}/{r0} are not R1..R{r0 - 1}")
        return kept

    @staticmethod
    def _check_blocks(blocks: list[bytes]) -> None:
        for i, b in enumerate(blocks, 1):
            if not b:
                raise ValidationError(f"block {i} is empty (it would read as an end of file)")
            if not fits_on_track([(0, len(b))]):
                raise ValidationError(f"block {i} of {len(b)} bytes does not fit on a track")

    def write_blocks(self, start_ttr: tuple[int, int], blocks: list[bytes],
                     ending: str = "PS") -> tuple[int, int]:
        """Write blocks from start_ttr, keeping the records before it on that
        track and replacing everything after it. Extends the dataset when the
        allocated tracks run out, writes the end of data per ``ending`` ("PS"
        or "PO", see the module docstring), and updates DS1LSTAR, DS1TRBAL,
        DS1DSIND and DS1REFDT. Returns the TTR stored in DS1LSTAR. Must run
        inside a transaction. Refusals found by validation and the extend
        pre-check leave nothing staged; an error after the first staged
        change poisons the transaction (only rollback() is accepted)."""
        if ending not in ENDINGS:
            raise ValueError(f"ending must be one of {ENDINGS}, not {ending!r}")
        self._check_blocks(blocks)
        tt0, _ = start_ttr
        kept = [(r.key, r.data) for r in self._kept(start_ttr)]
        if ending == "PS" and tt0 > 0 and not kept and not blocks:
            raise ValueError("a PS write that ends before its start track is not supported")

        tracks, data_tracks = self._plan(kept, blocks, ending)
        short = tt0 + data_tracks - self.total_tracks
        if short > 0:
            from ckdoffline.vtoc import space     # space -> vtocix -> DatasetIO
            self._precheck_extend(space, short)

        last = tracks[data_tracks - 1]
        if ending == "PS" and not blocks and not kept:
            lstar, trbal = (0, 0), track_balance([])
        else:
            lstar = (tt0 + data_tracks - 1, len(last))
            trbal = track_balance([(len(k), len(d)) for k, d in last])

        # From the first staged change on, any error poisons the transaction.
        with self.image.mutating():
            while tt0 + data_tracks > self.total_tracks:
                space.extend(self.volume, self.dsname, 1)
                self._refresh()
            if tt0 + len(tracks) > self.total_tracks:      # PS: no room for the EOF track
                tracks = tracks[:data_tracks]
            for i, recs in enumerate(tracks):
                self._write_track(tt0 + i, recs)
            self._update_f1(lstar, trbal)
        return lstar

    def _precheck_extend(self, space, short: int) -> None:
        """Refuse before anything is staged when `short` more tracks cannot be
        had: not enough extents left for that many secondaries, or not enough
        free tracks on the volume. extend() can still fail later (free space
        too fragmented for an aligned extent); write_blocks poisons the
        transaction then."""
        heads = self.image.heads
        sec, _ = space.secondary_tracks(self.f1, heads)
        n = -(-short // sec)
        left = space.MAX_EXTENTS - len(self.extents)
        if n > left:
            raise NoSpaceError(f"{self.dsname} needs {short} more tracks: {n} extents of "
                               f"{sec} tracks, but only {left} more are allowed "
                               f"({space.MAX_EXTENTS} at most)")
        free = sum(e - s for s, e in space.free_extents(self.volume))
        if n * sec > free:
            raise NoSpaceError(f"volume full: {self.dsname} needs {n * sec} more tracks, "
                               f"{free} are free on {self.volume.volser}")

    def _write_track(self, tt: int, recs: list[tuple[bytes, bytes]]) -> None:
        cyl, head = self.rel_to_cchh(tt)
        # Null (never written) CCKD tracks read as an empty formatted track,
        # so a read error here is real damage: report it, don't paper over it.
        try:
            r0 = parse_track(self.image.read_track(cyl, head))[0]
        except FormatError as e:
            raise FormatError(f"{self.dsname}: track {cyl}/{head} cannot be read before "
                              f"it is rewritten: {e}") from e
        if len(r0) != len(R0_DEFAULT):          # keep R0 only in its standard 8-byte form
            r0 = R0_DEFAULT
        records = [Record(cyl, head, n, k, d) for n, (k, d) in enumerate(recs, 1)]
        self.image.write_track(cyl, head, build_track(cyl, head, records, r0))

    def _update_f1(self, lstar: tuple[int, int], trbal: int) -> None:
        f1c, f1 = self.volume.f1(self.dsname)        # re-read: extend() rewrote it
        tt, r = lstar
        if f1.flag1 & Format1.FLAG1_LARGE:
            f1.ttthi = tt >> 16
            tt &= 0xFFFF
        elif tt > 0xFFFF:
            raise NoSpaceError(f"{self.dsname}: relative track {tt} needs a large format dataset")
        f1.lstar = (tt, r)
        f1.trbal = trbal
        self._touch(f1c, f1)

    def rewrite_records(self, tt: int, new: dict[int, tuple[bytes | None, bytes]]) -> None:
        """Replace the key (unless None) and data of records on relative track tt, keeping R0
        and every other record byte for byte. Callers keep the lengths unchanged."""
        cyl, head = self.rel_to_cchh(tt)
        r0, recs = parse_track(self.image.read_track(cyl, head))
        found = set()
        for rec in recs:
            if rec.rec in new:
                key, rec.data = new[rec.rec]
                if key is not None:
                    rec.key = key
                found.add(rec.rec)
        if found != set(new):
            raise FormatError(f"{self.dsname}: records {sorted(set(new) - found)} not found on "
                              f"track {cyl}/{head}")
        self.image.write_track(cyl, head, build_track(cyl, head, recs, r0))

    def mark_updated(self) -> None:
        """Set DS1DSIND "changed since backup" and DS1REFDT = today, for changes made without
        write_blocks (directory updates, in-place record patches). Inside a transaction."""
        self._touch(*self.volume.f1(self.dsname))

    def _touch(self, f1c, f1: Format1) -> None:
        f1.dsind |= DSIND_UPDATED
        today = datetime.date.today()
        f1.refdt = (today.year - 1900, today.timetuple().tm_yday)
        self.volume.write_dscb(f1c, f1)
        self.f1c, self.f1 = f1c, f1
