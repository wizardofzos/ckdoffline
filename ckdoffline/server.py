"""Stdlib HTTP JSON API and static file server. Binds 127.0.0.1 only; holds one Session at a time
and serializes every request with a lock. The browser UI talks only to this API.

Browser-attack protection (no CORS headers are ever sent):
  * every request must carry Host: 127.0.0.1:<port> or localhost:<port> (DNS rebinding);
  * every /api/* request must carry the header ``X-CKD-Offline: 1`` (front end: Task 18 must
    send it on every fetch), and POST bodies must be ``Content-Type: application/json``;
  * every response forbids framing and MIME sniffing and carries a same-origin-only CSP.

Session identity: the info object carries ``session_id``, a generation number that changes on
every open and code page change. Every /api/* call except open, info and browse-fs must send it
back as ``X-CKD-Session``; a mismatch (another browser tab opened a different image or changed
the code page) is answered 409 kind "VolumeChanged", so a stale tab never writes into the wrong
volume or with the wrong encoding."""

import json
import mimetypes
import sys
import threading
import traceback
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ckdoffline import ebcdic
from ckdoffline.errors import (CkdError, DirectoryFullError, ImageChangedError, NoSpaceError,
                               NotFoundError, ReadOnlyError, UnsupportedError, ValidationError)
from ckdoffline.image.rawfile import _journal_path

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_BODY = 64 * 1024 * 1024
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".json": "application/json", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
}
# Endpoints a tab may call without knowing the current session id.
NO_SESSION_ID = {"/api/open", "/api/info", "/api/browse-fs"}


class ApiError(Exception):
    """An error with an explicit status, kind and message."""

    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


def _status_for(exc: CkdError) -> int:
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, (ValidationError, DirectoryFullError, NoSpaceError, UnsupportedError)):
        return 422
    if isinstance(exc, (ReadOnlyError, ImageChangedError)):
        return 409
    return 400


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, session_factory, handler):
        super().__init__(address, handler)
        self.session_factory = session_factory
        self.session = None
        self.lock = threading.Lock()
        self.options: dict = {}         # forwarded to session_factory: backup, codepage, userid
        self.generation = 0             # session_id: bumped on every open and code page change

    def close_session(self) -> None:
        with self.lock:
            self._drop_session()

    def _drop_session(self) -> None:
        if self.session is not None:
            try:
                self.session.close()
            finally:
                self.session = None


def make_server(session_factory, port: int, **options) -> ApiServer:
    """session_factory(path, mode, **options) -> Session; options (backup, codepage, userid)
    are the defaults for every open."""
    server = ApiServer(("127.0.0.1", port), session_factory, Handler)
    server.options = dict(options)
    return server


def _dataset_json(d) -> dict:
    out = asdict(d)
    out["f1_cchhr"] = list(d.f1_cchhr)
    return out


class Handler(BaseHTTPRequestHandler):
    server: ApiServer
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):      # keep the console quiet
        pass

    # -- plumbing --------------------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _error(self, status: int, kind: str, message: str, **extra) -> None:
        self._json(status, {"error": message, "kind": kind, **extra})

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "BadRequest", "bad Content-Length") from None
        if n < 0:
            raise ApiError(400, "BadRequest", "bad Content-Length")
        if n > MAX_BODY:
            raise ApiError(413, "TooLarge", "request body too large")
        raw = self.rfile.read(n) if n else b""
        if not raw.strip():
            return {}
        try:
            obj = json.loads(raw)
        except ValueError:
            raise ApiError(400, "BadRequest", "request body is not valid JSON") from None
        if not isinstance(obj, dict):
            raise ApiError(400, "BadRequest", "request body must be a JSON object")
        return obj

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        try:
            port = self.server.server_address[1]
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                raise ApiError(403, "Forbidden", "bad Host header")
            if url.path.startswith("/api/"):
                if self.headers.get("X-CKD-Offline") != "1":
                    raise ApiError(403, "Forbidden", "missing X-CKD-Offline header")
                if method == "POST" and (self.headers.get("Content-Type") or "") \
                        .split(";")[0].strip().lower() != "application/json":
                    raise ApiError(415, "UnsupportedMediaType",
                                   "Content-Type must be application/json")
                query = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
                body = self._body() if method == "POST" else {}
                with self.server.lock:
                    result = self._route(method, url.path, query, body)
                self._json(200, result)
            elif method == "GET":
                self._static(url.path)
            else:
                raise ApiError(405, "BadRequest", "method not allowed")
        except ApiError as e:
            self._error(e.status, e.kind, e.message)
        except CkdError as e:
            extra = {"line": e.line_no} if getattr(e, "line_no", None) is not None else {}
            kind = "ImageChanged" if isinstance(e, ImageChangedError) else type(e).__name__
            self._error(_status_for(e), kind, str(e), **extra)
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            self._error(500, "InternalError", f"internal error: {e}")

    # -- static ----------------------------------------------------------------------------
    def _static(self, path: str) -> None:
        rel = unquote(path).lstrip("/") or "index.html"
        root = WEB_DIR.resolve()
        try:
            target = (root / rel).resolve()
            ok = root in target.parents and target.is_file()
        except (ValueError, OSError):
            raise ApiError(400, "BadRequest", "bad path") from None
        if not ok:
            raise ApiError(404, "NotFound", f"{path} not found")
        ctype = MIME.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] \
            or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    # -- API -------------------------------------------------------------------------------
    @staticmethod
    def _need(src: dict, *names):
        out = []
        for n in names:
            if n not in src or src[n] is None:
                raise ApiError(400, "BadRequest", f"missing field: {n}")
            out.append(src[n])
        return out

    def _session(self):
        if self.server.session is None:
            raise ApiError(409, "NoSession", "no image is open")
        return self.server.session

    def _check_session_id(self) -> None:
        if self.headers.get("X-CKD-Session") != str(self.server.generation):
            raise ApiError(409, "VolumeChanged",
                           "Volume changed in another tab (another image was opened or the code "
                           "page was changed); reload")

    def _info(self) -> dict:
        return {**self._session().info, "session_id": self.server.generation}

    def _route(self, method: str, path: str, q: dict, b: dict):
        srv = self.server
        if (method, path) == ("POST", "/api/open"):
            return self._open(b)
        if (method, path) == ("GET", "/api/browse-fs"):
            return self._browse(q.get("dir"))
        routes_get = {"/api/info", "/api/datasets", "/api/members", "/api/text", "/api/blocks",
                      "/api/dscb"}
        routes_post = {"/api/confirm-rw", "/api/save", "/api/member/new", "/api/member/delete",
                       "/api/member/rename", "/api/patch", "/api/patch-blocks", "/api/compress",
                       "/api/codepage"}
        if not ((method == "GET" and path in routes_get)
                or (method == "POST" and path in routes_post)):
            raise ApiError(404, "NotFound", f"no such endpoint: {method} {path}")
        s = self._session()
        if path not in NO_SESSION_ID:
            self._check_session_id()
        if path == "/api/info":
            return self._info()
        if path == "/api/confirm-rw":
            s.confirm_rw()
            return self._info()
        if path == "/api/datasets":
            return [_dataset_json(d) for d in s.datasets()]
        if path == "/api/members":
            return s.members(*self._need(q, "dsn"))
        if path == "/api/text":
            return s.read_text(*self._need(q, "dsn"), q.get("member") or None)
        if path == "/api/blocks":
            return s.read_blocks(*self._need(q, "dsn"), q.get("member") or None)
        if path == "/api/save":
            dsn, lines = self._need(b, "dsn", "lines")
            if not isinstance(lines, list):
                raise ApiError(400, "BadRequest", "lines must be a list")
            s.save_text(dsn, b.get("member") or None, lines)
            return self._info()
        if path == "/api/member/new":
            s.new_member(*self._need(b, "dsn", "name"))
            return self._info()
        if path == "/api/member/delete":
            s.delete_member(*self._need(b, "dsn", "name"))
            return self._info()
        if path == "/api/member/rename":
            s.rename_member(*self._need(b, "dsn", "old", "new"))
            return self._info()
        if path == "/api/patch":                # member: null for a sequential RECFM U dataset
            dsn, index, hexdata = self._need(b, "dsn", "index", "hex")
            if not isinstance(index, int) or isinstance(index, bool):
                raise ApiError(400, "BadRequest", "index must be an integer")
            s.patch_block(dsn, b.get("member") or None, index, hexdata)
            return self._info()
        if path == "/api/patch-blocks":
            dsn, patches = self._need(b, "dsn", "patches")
            if not isinstance(patches, list):
                raise ApiError(400, "BadRequest", "patches must be a list")
            s.patch_blocks(dsn, b.get("member") or None, patches)
            return self._info()
        if path == "/api/dscb":
            return s.dscb(*self._need(q, "dsn"))
        if path == "/api/compress":
            return {**s.compress(*self._need(b, "dsn")), "info": self._info()}
        if path == "/api/codepage":
            (cp,) = self._need(b, "cp")
            cp = str(cp)
            if cp not in ebcdic.CODEPAGES:
                raise ValidationError(f"unknown code page {cp!r}")
            s.codepage = cp
            srv.options["codepage"] = cp
            srv.generation += 1             # other tabs decode with the old code page
            return self._info()
        raise ApiError(404, "NotFound", f"no such endpoint: {path}")      # pragma: no cover

    def _open(self, b: dict):
        srv = self.server
        path, = self._need(b, "path")
        mode = b.get("mode", "rw")
        if mode not in ("rw", "ro"):
            raise ApiError(400, "BadRequest", "mode must be 'rw' or 'ro'")
        try:
            p = Path(str(path)).expanduser()
            found = p.is_file()
        except (RuntimeError, ValueError, OSError):
            raise ApiError(400, "BadRequest", "bad path") from None
        if not found:
            raise ApiError(404, "NotFound", f"{p} is not a file")
        srv._drop_session()
        srv.generation += 1                 # even a failed open closed the previous volume
        try:
            srv.session = srv.session_factory(p, mode, **srv.options)
        except CkdError as e:
            if mode == "ro" and _journal_path(p).exists():
                raise ApiError(409, "NeedsRecovery", str(e)) from e
            raise
        except PermissionError as e:
            what = "for writing" if mode == "rw" else "for reading"
            raise ApiError(400, "PermissionDenied",
                           f"cannot open image {what}: {e.strerror or e}") from None
        return self._info()

    @staticmethod
    def _browse(d):
        try:
            base = (Path(d).expanduser() if d else Path.home()).resolve()
            entries = []
            for e in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                try:
                    is_dir = e.is_dir()
                    entries.append({"name": e.name, "is_dir": is_dir,
                                    "size": 0 if is_dir else e.stat().st_size})
                except OSError:
                    continue        # broken link or unreadable entry
        except (RuntimeError, ValueError):
            raise ApiError(400, "BadRequest", "bad path") from None
        except FileNotFoundError:
            raise ApiError(404, "NotFound", f"{d} does not exist") from None
        except NotADirectoryError:
            raise ApiError(400, "BadRequest", f"{d} is not a directory") from None
        except OSError as e:
            raise ApiError(403, "Forbidden", str(e)) from None
        return {"dir": str(base), "parent": str(base.parent), "entries": entries}
