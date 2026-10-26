"""ckdoffline [IMAGE] [--port N] [--no-backup] [--codepage 1047|037] [--userid ID]

Installed as the `ckdoffline` console script; `python -m ckdoffline` works too."""

import argparse
import errno
import sys
import threading
import webbrowser
from pathlib import Path

from ckdoffline.errors import CkdError
from ckdoffline.image.rawfile import _journal_path
from ckdoffline.server import make_server
from ckdoffline.session import Session


def _fail(msg: str) -> int:
    print(f"ckdoffline: {msg}", file=sys.stderr, flush=True)
    return 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ckdoffline",
                                 description="Offline editor for datasets on 3390 volume images: "
                                             "z/OS for ZD&T, MVS 3.8j on Hercules")
    ap.add_argument("image", nargs="?", help="image to open at start")
    ap.add_argument("--port", type=int, default=3270)
    ap.add_argument("--no-backup", action="store_true", help="skip the backup before the first write")
    ap.add_argument("--codepage", choices=("1047", "037"), default="1047")
    ap.add_argument("--userid", default=None)
    ap.add_argument("--readonly", action="store_true", help="open IMAGE read-only")
    ap.add_argument("--no-browser", action="store_true", help="do not open a web browser")
    a = ap.parse_args(argv)

    try:
        server = make_server(lambda path, mode, **kw: Session.open(path, mode, **kw), a.port,
                             backup=not a.no_backup, codepage=a.codepage, userid=a.userid)
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", None)):
            return _fail(f"port {a.port} is already in use (is CKD-Offline already running?); "
                         f"choose another with --port")
        return _fail(f"cannot listen on port {a.port}: {e.strerror or e}")
    try:
        if a.image:
            image = Path(a.image)
            try:
                server.session = Session.open(image, "ro" if a.readonly else "rw",
                                              **server.options)
            except CkdError as e:
                if a.readonly and _journal_path(image).exists():
                    return _fail(f"{image.name} has an undo journal from an interrupted write; "
                                 f"start without --readonly once to recover it")
                return _fail(f"cannot open {image}: {e}")
            except OSError as e:
                what = "for reading" if a.readonly else "for writing"
                return _fail(f"cannot open image {what}: {image}: {e.strerror or e}")
        url = f"http://localhost:{server.server_address[1]}/"
        print(f"CKD-Offline running at {url}  (Ctrl-C to stop)", flush=True)
        if not a.no_browser:
            threading.Timer(0.3, webbrowser.open, args=(url,)).start()
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        server.server_close()
        server.close_session()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
