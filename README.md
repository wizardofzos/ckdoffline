# CKD-Offline

Edit datasets on 3390 volumes without IPLing the system that owns them: z/OS volumes for
IBM ZD&T, or MVS 3.8j volumes on Hercules. It opens the volume's image file and gives you an ISPF-style DSLIST, member list, EDIT, BROWSE and a hex editor in
a browser tab. Saves go straight into the image, including the VTOC, the VTOC index, PDS
directories and ISPF statistics.

Typical uses: fix a PARMLIB or PROCLIB member, patch some JCL, edit a sequential file, zap a
load module, then put the volume back into service.

Supported image files:

| Format | Used by |
|---|---|
| CKD (uncompressed) | ZD&T and Hercules |
| CCKD, CCKD64 (compressed) | Hercules only |

## Requirements

- Python 3.10 or newer, on macOS, Linux or Windows.
- A web browser.
- Nothing else. CKD-Offline uses only the Python standard library. It does **not** need,
  include or call Hercules, ZD&T or any other emulator: it reads and writes the image file
  itself. (Hercules is used only by the developer test suite; see [Tests](#tests).)

## Install

```sh
pip install ckdoffline
```

To keep it in its own environment, `pipx install ckdoffline` or `uv tool install ckdoffline`
work too. From a source checkout: `pip install .` (add `-e` for development).

## Usage

1. **Stop the emulator** that uses the volume (ZD&T or Hercules), or detach the device.
2. **Make a copy** of the image to work on. Keep the original until you have checked the
   result.
3. **Start CKD-Offline** with the image:

   ```sh
   ckdoffline work.cckd
   ```

   Your default browser opens at `http://localhost:3270/` with the volume's data set list.
   Without an image argument you start on the Open panel, where you can type a path or pick a
   file.
4. **Edit**: `M` on a PDS for its member list, `E` to edit, PF3 (or Esc) to save and return.
   See [Keys](#keys).
5. **Stop CKD-Offline** with Ctrl-C in the terminal, then attach the image to the emulator
   again.

Making the copy:

| System | Command |
|---|---|
| macOS (APFS: instant clone) | `cp -c MYVOL.CCKD work.cckd` |
| Linux | `cp --reflink=auto MYVOL.CCKD work.cckd` |
| Windows (PowerShell) | `Copy-Item MYVOL.CCKD work.cckd` |

On Windows, if `ckdoffline` isn't found after installing (Python's `Scripts` folder isn't on
`PATH`), run `py -m ckdoffline work.cckd` instead. Anywhere, `python3 -m ckdoffline` works too.

Options:

| Option | Meaning |
|---|---|
| `--readonly` | open the image read-only |
| `--no-backup` | skip the `.bak-*` copy before the first write |
| `--codepage 1047\|037` | EBCDIC code page (default 1047; also on the Open panel) |
| `--userid ID` | user ID written into ISPF statistics (default: your login name) |
| `--port N` | HTTP port (default 3270) |
| `--no-browser` | don't open a browser |

The server listens on 127.0.0.1 only. It is for one person on one machine.

## What you can do

- **DSLIST**: every dataset on the volume with DSORG, RECFM, LRECL, BLKSIZE, tracks and
  extents. Line commands `E` edit, `B` browse, `M` member list, `I` info (full format-1/3
  DSCB decode), `Z` compress. Primary command `L prefix`.
- **Member list**: ISPF statistics, aliases. Line commands `S`, `E`, `B`, `D` delete,
  `R` rename. Primary commands `S name` (create a new member, as in ISPF) and `L name`.
- **EDIT** (PS and PDS members, RECFM F/FB/FBS/FBA, V/VB/VBA): line commands `I`, `D`, `R`,
  `C`/`M` with `A`/`B`, and `DD`, `CC`, `MM`, `RR`. Primary commands `SAVE`, `END`,
  `CANCEL`, `FIND`/`RFIND`, `CHANGE`/`RCHANGE`, `RESET`, `CAPS ON|OFF`, `NUM ON|OFF`,
  `HEX ON|OFF`, `L n`. Lines you don't touch are written back byte for byte, including bytes
  that have no character in the code page.
- **BROWSE**: read-only view of any PS or PO text, `HEX ON` available.
- **Hex editor** (RECFM U): overtype in place, for a member of a load library or a whole
  sequential (PS) RECFM U dataset (`E` on it in DSLIST). Block lengths never change; all
  changed blocks are written in one transaction.
- **Compress** a PDS in place, like IEBCOPY: members (and their aliases and note lists) are
  moved down, the directory TTRs are relocated, and the freed tracks stay allocated.
- **Extend**: when a save no longer fits, the dataset gets a new extent from its secondary
  allocation: format-1/3 DSCBs, format 4, and the VTOC index (or format 5 on a non-indexed
  VTOC) are updated. Up to 16 extents on the volume. Format 5 maintenance is tested on
  synthetic volumes only. If the free-space map disagrees with the extents, the volume still opens and in-place
  edits work; DSLIST shows why the free space is unknown and only extends are refused.

Every save sets DS1DSIND "changed since backup" and DS1REFDT to today.

## Keys

Laid out so that a Mac keyboard without an F-key row works too. The legend is on every
screen.

| Key | ISPF | Action |
|---|---|---|
| Enter | Enter | process the command line and line commands |
| F3 or Esc | PF3 | End (in EDIT: save and leave) |
| F5 | PF5 | Repeat find |
| F7 or PgUp | PF7 | Scroll up |
| F8 or PgDn | PF8 | Scroll down |
| F10 or Shift+← | PF10 | Scroll left |
| F11 or Shift+→ | PF11 | Scroll right |
| F12 or Ctrl+Shift+X | PF12 | Cancel (in EDIT: leave without saving) |
| Home | Home | cursor to the command line |
| Tab | Tab | next field |
| Ctrl+I or Insert | Insert | toggle insert mode |

## Safety model

- **Stop the emulator first** (ZD&T or Hercules), or detach the device. CKD-Offline can't
  rely on a file lock, and writing to an image the emulator has open will corrupt it. Don't
  run the emulator and CKD-Offline on the same image at once, and after the emulator has used
  the image, close and reopen it in CKD-Offline.
- **External changes are detected.** If anything else writes the image while it is open
  (the emulator, another CKD-Offline), the next save is refused with "image changed on disk
  since it was opened; reopen it", so nothing is written from stale copies. Reopen the image
  (Enter on the Open panel) and redo the change. An "opened" flag that appears after the
  open needs confirming again.
- **One volume per server.** Opening an image (or changing the code page) in one browser tab
  makes the other tabs' next request fail with "Volume changed in another tab": they go back
  to the Open panel instead of saving into the wrong volume.
- **Opened-flag warning.** If a CCKD/CCKD64 image has its "opened" flag set, an emulator is
  running with it, or crashed. You get a warning and choose to continue read-write (only if
  the emulator is really stopped) or reopen read-only. Plain CKD has no such flag, so the Open
  panel always reminds you.
- **Backup.** Before the first write of a session the image is copied to
  `<image>.bak-YYYYMMDD-HHMMSS` in the same folder. On macOS this is an APFS clone (instant,
  and no extra space until the files diverge); elsewhere it is a full copy, so leave room
  for one. An existing backup is never overwritten. `--no-backup` turns this off.
- **Undo journal.** Every save is one transaction. The tool works out every byte it will
  change, writes the original bytes to `<image>.journal` and fsyncs it, applies the change,
  fsyncs, and deletes the journal. A refused save (line too long, directory full, no space)
  writes nothing.
- **Recovery.** If the tool was killed mid-save (kill -9, power loss), the journal is left
  behind. The next read-write open rolls the image back to how it was before that save, and
  says so. A read-only open refuses and offers to reopen read-write to recover.
- **Read-only mode** (`--readonly` or the Open panel field) never writes the image.
- **Refusals.** It won't touch a VTOC marked as needing recovery (DIRF), an index it can't
  read, a dataset with no secondary allocation, or anything past the end of the volume.

## Out of scope

- Creating, deleting or renaming datasets (that needs catalog entries, which aren't on the
  volume).
- Anything multi-volume.
- PDSE, HFS, VSAM, DA/IS and every DSORG other than PS and PO. These are listed, not opened.
- RECFM VBS is browse only. New RECFM U members can't be created (`S name` is refused on a
  load library); existing ones can be patched, renamed and deleted.
- Devices other than 3390, FBA, EAV volumes (format 8/9 DSCBs), Hercules shadow files and
  split plain-CKD images.
- Getting data in or out other than by typing: no upload/download, XMIT or IEBCOPY unload.
- More than one user, remote access, a real 3270 data stream.

## Tests

The tests check every write with Hyperion's own utilities (`dasdls`, `dasdcat`, `dasdseq`,
`dasdload`, `dasdcopy`, `cckdcdsk`) as an independent oracle. Using the tool doesn't need
any of this. Only running the tests does.

### The `.oracle/` directory

Everything the oracle needs lives in `.oracle/` in the repo root. It is git-ignored and
never committed: it holds source checkouts, build output and test volumes, all of which you
build locally.

| Path | What |
|---|---|
| `.oracle/src/hyperion` | SDL Hyperion source (or set `HYPERION_SRC` to an existing checkout) |
| `.oracle/ext/<pkg>` | sources of the Hercules external packages |
| `.oracle/extpkgs` | those packages, built and installed |
| `.oracle/hyperion` | the build copy of Hyperion with the utilities |
| `.oracle/fixtures` | small synthetic 3390 test volumes, made on first test run |

### Setting it up

Tested on macOS on Apple Silicon. Needs a C toolchain, CMake and Homebrew's `libtool`
(for `ltdl.h`). On Intel or Linux, drop `-m aarch64` below and adjust the library path in
`tools/build_oracle.sh`.

```sh
E=$PWD/.oracle
git clone https://github.com/SDL-Hercules-390/hyperion.git $E/src/hyperion
for p in crypto decNumber SoftFloat telnet; do
  git clone https://github.com/SDL-Hercules-390/$p.git $E/ext/$p
  mkdir -p $E/work/$p $E/extpkgs
  (cd $E/work/$p && $E/ext/$p/build -d $E/ext/$p -n $p -m aarch64 -a 64 -c Release -i $E/extpkgs)
done
tools/build_oracle.sh
```

The external packages are needed because Hyperion's repository ships them prebuilt for
x86 only. `tools/build_oracle.sh` copies the Hyperion source into `.oracle/hyperion` and
builds the utilities there; the source checkout itself is left untouched. The synthetic test
volumes are built by `tools/make_fixtures.sh` on the first test run.

### Tests against real volumes

`tests/local/` is git-ignored and meant for tests against your own volumes, which can't be
published. Put the image (a `*.cckd`/`*.ckd` file, also git-ignored) and a
`conftest.py` providing a fixture that clones it into `tmp_path` there; the tests should
only ever touch the clone. pytest picks the directory up automatically.

During development the tool was also run end to end against a real 3390 volume: member
edits, adds, deletes and renames; a VB dataset grown by 50,000 records into four extents
with a format-3 DSCB; a load module patch; PDS compress; and saves through the HTTP API.
After each step the image passed the integrity checks and the free-space maps matched the
extents.

### Running

Needs pytest; the JS tests need Node:

```sh
pip install -e '.[dev]'
python3 -m pytest -q -m "not slow"   # fast
python3 -m pytest -q                 # full, including tests/local/ if present
node --test tests/js/                # front-end editing model
```

## Notes

- **Hyperion's DIRF constant is out of date.** `dasdblks.h` names X'40' in DS4VTOCI as
  DS4VTOCI_DIRF. In current IBM documentation DS4DIRF is X'04', and X'40' is DS4DVTOC. This
  tool uses X'04', and refuses to write if either bit is set (see the comment in
  `ckdoffline/vtoc/space.py`).

## Final check is yours

The tests prove Hyperion's DASD utilities read the result cleanly. The last word belongs to
the system that uses the volume: attach an edited copy to ZD&T for z/OS, or to Hercules for
MVS 3.8j, IPL or vary the volume online, and browse the changed members. If you like, run
`IEHLIST LISTVTOC FORMAT` against it, and on z/OS also `ICKDSF ANALYZE` or `BUILDIX`.

## Trademarks

IBM, z/OS, MVS, ZD&T, ISPF, DFSMS, and other IBM product names used here are trademarks of
International Business Machines Corporation. They are used only to describe what this tool
works with. CKD-Offline is an independent project, not affiliated with, sponsored by or
endorsed by IBM. Hercules is an open-source project unrelated to IBM.

## License

MIT. See [LICENSE](LICENSE).

CKD-Offline is an independent implementation. SDL Hyperion (Hercules) was used as a reference
for the image formats, and its utilities serve as a test oracle, but no Hercules code or
binaries are included in this repository or in the package on PyPI. The test setup clones
and builds Hyperion locally from its own upstream repositories (under Hyperion's own
licence) into the git-ignored `.oracle/` directory; nothing from it is redistributed.
