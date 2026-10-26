# VTOC index (SYS1.VTOCIX.volser) format notes

What `ckdoffline/vtoc/vtocix.py` relies on, with the documentation each point
comes from. The layout was also checked against real data: the space maps were
compared with the allocation derived from every extent on a volume, and the
DSCB map with the empty DSCBs in its VTOC.

Status tags:

- **CHECKED**: documented, and consistent with real data.
- **UNVERIFIED**: not confirmed. The code doesn't depend on it, or refuses to
  update when it would.

## Sources

- [ADV] IBM, *z/OS DFSMSdfp Advanced Services* (SC23-6861), chapter 1:
  "Format-4 DSCB" (DS4VTOCI), "The VTOC Index", "VTOC Index Records",
  "Structure of an Indexed VTOC", "VTOC Pack Space Map", "VTOC Index Map",
  "VTOC map of DSCBs", and CVAFDSM.
- [XA] IBM, *MVS/XA System-Data Administration* (GC26-4149), chapter 1: "The
  VTOC Pack Space Map (VPSM)", "Format of a VTOC Map", "Format of the VTOC
  Index Entry Record (VIER)", "Recovering from System or User Errors". The
  only edition found that gives byte offsets for the map header; current
  manuals describe the maps without them.
- [MSG] IBM, *z/OS MVS System Messages*, message IEC606I and its reason codes.

## Format-4 DSCB: DS4VTOCI (DSCB data offset 14, `raw[58]`)

[ADV], format-4 DSCB. Bit 0 is the high bit (IBM numbering):

| Mask | Name | Meaning |
|------|------|---------|
| X'80' | DS4DOSBT | VSE bit: format-5 DSCBs invalid, or indexed VTOC |
| X'40' | DS4DVTOC | Index was disabled |
| X'20' | DS4EFVLD | Extended free-space management (format-7) |
| X'10' | DS4DSTKP | VSE stacked pack |
| X'08' | DS4DOCVT | VSE converted VTOC |
| X'04' | DS4DIRF | DIRF: a VTOC change is incomplete |
| X'02' | DS4DICVT | DIRF reclaimed |
| X'01' | DS4IVTOC | Volume uses an indexed VTOC |

- **CHECKED**: `DS4IVTOC = X'01'` ([ADV], "Structure of an Indexed VTOC"). An
  indexed VTOC normally has DS4IVTOC and DS4DOSBT both on: the VSE bit says the
  format-5 DSCB and the related format-4 fields are not valid. `vtoc.py`'s
  `DS4VTOCI_INDEXED = 0x01` is correct.
- **CHECKED**: on an indexed VTOC, DADSM doesn't maintain **DS4DSREC** (the
  number of format-0 DSCBs) or **DS4HPCHR** ([ADV] and [XA], "Structure of an
  Indexed VTOC"). DS4DSREC can therefore disagree with the real number of empty
  DSCBs. The code never uses DS4DSREC to judge free DSCBs on an indexed volume;
  the VMDS is the authority.
- An indexed VTOC keeps a single empty format-5 DSCB ([ADV], "Format-5 DSCB").
  The code leaves it alone.
- If DS4DIRF is on, the system runs VTOC recovery on the next DADSM call ([XA],
  "Recovering from System or User Errors"). `VtocIndex` refuses updates while
  DIRF is set. Our own edits never set DIRF, because the undo journal makes
  them atomic.

## The index dataset and VIRs

- 2048-byte physical blocks called VIRs; 8192 on an extended address volume
  ([ADV], "The VTOC Index"). The index dataset is RECFM F, BLKSIZE 2048,
  keyless. The code refuses any other block size (EAV isn't supported anyway).
- A VIR's RBA is (record number in dataset order) x 2048. IEC606I reason code
  138 is the system's check for this ([MSG]); the code checks it on load.
- Order: VIXM, VPSM, VMDS, then VIERs ([ADV]; [XA]). The VIXM's VIMVIR field
  gives the number of map records (VIXM + VPSMs + VMDS). **CHECKED**.

## Map VIR header (VIXM, VPSM, VMDS): 44 bytes

[XA], "Format of a VTOC Map". Big-endian.

| Off | Len | Name | Meaning |
|-----|-----|------|---------|
| 0 | 4 | VIMID | EBCDIC 'VIXM' / 'VPSM' / 'VMDS' |
| 4 | 4 | VIMRBA | RBA of this VIR |
| 8 | 4 | VIMHZPTR | RBA of the next VIR of this map, 0 = last |
| 12 | 4 | VIMORG | Sequence number of this VIR's first entry (small units) |
| 16 | 2 | VIMVRFDA | Offset of current/next VRF data (first VIXM only) |
| 18 | 2 | VIMVRFO | Offset of the VRF area (first VIXM only) |
| 20 | 1 | VIMFLG1 | X'80' VIMVRFSW: VRF data exists |
| 21 | 1 | VIMLUFL | Large-unit flag byte |
| 22 | 2 | VIMLUOF | Offset of the large-unit map (0 = none) |
| 24 | 4 | VIMLUSZ | Large-unit map size in bits |
| 28 | 1 | VIMSUFL | Small-unit flag byte |
| 29 | 1 | VIMSUBIT | Small units per large unit (VPSM: heads; VMDS: DSCBs per track) |
| 30 | 2 | VIMSUOF | Offset of the small-unit map |
| 32 | 4 | VIMSUSZ | Small-unit map size in bits |
| 36 | 3 | reserved | |
| 39 | 1 | VIMVIR | Number of map records (VIXM only) |
| 40 | 4 | VIMFHLV | RBA of the first high-level VIER (VIXM only) |
| 44 | - | maps | large-unit map, small-unit map, (VIXM) VRF area |

The code reads every offset and size from these fields rather than assuming
them. **CHECKED**.

## Bit order and granularity

- **CHECKED**: bit 0 of a map is the high-order bit (X'80') of its first byte;
  entry *i* is in byte `off + i // 8` at mask `0x80 >> (i % 8)`. A 1 bit means
  allocated, a 0 bit free ([ADV], "VTOC Pack Space Map", "VTOC Index Map",
  "VTOC map of DSCBs"). With this order, a volume's track map matched the
  allocation derived from its extents exactly; any other order would not.
- **VPSM small units = tracks.** The absolute track (cyl x heads + head) is
  `VIMORG + i`.
- **VPSM large units = cylinders.** The cylinder is `VIMORG / VIMSUBIT + i`.
  Per [XA]: the cylinder bit is zero if no track of the cylinder is allocated,
  and one if any track is. **CHECKED**. On update the code sets each cylinder
  bit to "any track of the cylinder allocated".
- Each VPSM VIR covers whole cylinders. The code checks that `VIMORG` is a
  multiple of the head count, that `VIMSUSZ = VIMLUSZ x heads`, and that the
  chain covers cylinders 0..cyls-1 contiguously.
- Pad bits after the end of a map are 1 in the VPSM (so nonexistent space looks
  allocated) and 0 in the VMDS. The code never changes them.
- "Allocated" in the VPSM = every dataset extent (format-1 plus the format-3
  chain, which includes the index dataset itself) + the VTOC extent (format-4
  DS4VTOCE) + track 0 (IPL records and VOL1). **CHECKED**. The invariant in
  `tests/invariants.py` uses exactly this.
- **VMDS**: one bit per DSCB, in relative DSCB order within the VTOC extent
  (track by track, records 1..VIMSUBIT, where VIMSUBIT = DS4DEVDT). **CHECKED**:
  its zero bits are exactly the all-zero (format-0) DSCBs.
- Cylinder-managed space (EAV, more than 65520 cylinders) uses
  multicylinder-unit VPSMs and an enlarged VIXM header with free-space
  statistics ([ADV]). **UNVERIFIED** and unsupported: the code refuses volumes
  with more than 65520 cylinders.

## Counters

- A non-EAV map header has **no free-space or free-DSCB counters**: CVAFDSM
  derives free counts by reading the maps ([ADV], CVAFDSM). So allocating a
  track or a DSCB changes **only map bits**: the track bit and its cylinder bit
  in the VPSM, or the DSCB bit in the VMDS. DS4DSREC and DS4HPCHR aren't
  maintained (see above), and adding a format-3 DSCB doesn't move the highest
  format-1 anyway.
- The VIXM changes only when VIRs are allocated or freed (a new VIER), which an
  extension never does. Level-1 VIER entries point to format-1 and format-4
  DSCBs only, never format-3, so adding an extent or a format-3 DSCB needs no
  VIER or VIXM change. **CHECKED**. `VtocIndex` never writes the VIXM or a
  VIER.

## VRF and other system checks

- The VTOC recording facility (VRF) area is in the first VIXM (offset
  VIMVRFO). It holds live data only while an update is in flight, flagged by
  VIMVRFSW (VIMFLG1 X'80') ([XA]; [ADV], "VTOC Index Map"); otherwise it may
  hold stale data from an earlier update. `VtocIndex` refuses updates if
  VIMVRFSW is on, because the system would replay or back out that data.
- **UNVERIFIED**: the meaning of VIXM VIMFLG1 X'40', of VIMLUSZ in the VIXM
  (which has no large-unit map), and of the reserved bytes. The tool never
  writes the VIXM, so whatever they hold is preserved.
- [MSG] lists every structural check that disables the index (IEC606I reason
  codes 129-159): VIER pointers, levels, keys, IDs, RBAs, map placement, the
  index and VSE bits in the format-4 DSCB, and the dataset name. None involves
  a timestamp or a change counter, and none compares map contents with the
  DSCBs. Bit-only map updates touch none of these fields.
- A map bit that disagrees with the DSCBs is *not* detected by the system. It
  causes double allocation (a 0 bit on a used track) or lost space (a 1 bit on
  a free track). That's why the tests check the exact invariant
  (`tests/invariants.py: assert_vtocix_consistent`) after every update.
- If in doubt after an update, ICKDSF `BUILDIX` (or `REFORMAT REFVTOC`)
  rebuilds the index from the VTOC.

## What `VtocIndex` refuses (UnsupportedError on `mark_*`)

- The VTOC index isn't active (`Volume.indexed` is false): the constructor
  raises.
- VIR size isn't 2048, or the volume is larger than 65520 cylinders (EAV).
- VIMVRFSW is on (VRF recovery pending), or DS4DIRF is on (incomplete VTOC
  change).
- On load, the cylinder map disagrees with the track map. That would mean our
  model of the format is wrong for this volume.

Structural surprises (a bad ID, an RBA mismatch, a chain that doesn't cover the
volume, a VMDS that doesn't cover the VTOC) raise `FormatError` when the index
is opened.
