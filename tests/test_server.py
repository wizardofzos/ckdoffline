import json
import threading
import urllib.error
import urllib.request

import pytest

from ckdoffline.server import make_server
from ckdoffline.session import Session

FB = "TEST.FB80.PDS"
FBSEQ = "TEST.FB.SEQ"


class Api:
    """A client that behaves like the UI: it remembers the session_id of the last info it was
    given and sends it back as X-CKD-Session (sid= overrides it; sid=False leaves it out)."""

    def __init__(self, server):
        self.server = server
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        self.sid = None

    def call(self, method, path, body=None, raw=False, sid=None):
        data = None if body is None else json.dumps(body).encode()
        hdrs = {"X-CKD-Offline": "1"}
        sid = self.sid if sid is None else sid
        if sid is not False and sid is not None:
            hdrs["X-CKD-Session"] = str(sid)
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req) as r:
                payload = r.read()
                status, headers = r.status, r.headers
        except urllib.error.HTTPError as e:
            payload = e.read()
            status, headers = e.code, e.headers
        obj = payload if raw else json.loads(payload)
        if not raw and status == 200 and isinstance(obj, dict):
            info = obj.get("info", obj)
            if isinstance(info, dict) and "session_id" in info:
                self.sid = info["session_id"]
        return status, obj, headers

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, {} if body is None else body, **kw)


@pytest.fixture
def api(tmp_path):
    def factory(path, mode, **kw):
        return Session.open(path, mode, **kw)
    server = make_server(factory, 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield Api(server)
    server.shutdown()
    server.server_close()
    server.close_session()


@pytest.fixture
def img(fixture_images):
    return fixture_images["cckd_zlib"]


def _open(api, img, mode="rw"):
    st, body, _ = api.post("/api/open", {"path": str(img), "mode": mode})
    assert st == 200, body
    return body


def test_open_and_list(api, img):
    info = _open(api, img)
    assert info["mode"] == "rw" and info["volser"]
    assert api.get("/api/info")[1]["volser"] == info["volser"]
    st, dss, _ = api.get("/api/datasets")
    assert st == 200
    assert {d["dsname"] for d in dss} >= {FB, FBSEQ}
    assert api.get(f"/api/members?dsn={FB}")[1] == []


def test_edit_save_roundtrip_via_api(api, img):
    _open(api, img)
    st, _, _ = api.post("/api/save", {"dsn": FB, "member": "abc", "lines": ["hello", "world"]})
    assert st == 200
    st, txt, _ = api.get(f"/api/text?dsn={FB}&member=ABC")
    assert txt["lines"] == ["hello".ljust(80), "world".ljust(80)]
    [blk] = api.get(f"/api/blocks?dsn={FB}&member=ABC")[1]
    assert len(blk) == 320
    assert api.post("/api/member/rename", {"dsn": FB, "old": "ABC", "new": "XYZ"})[0] == 200
    assert [m["name"] for m in api.get(f"/api/members?dsn={FB}")[1]] == ["XYZ"]
    assert api.post("/api/member/new", {"dsn": FB, "name": "N1"})[0] == 200
    assert api.post("/api/patch", {"dsn": FB, "member": "XYZ", "index": 0, "hex": blk})[0] == 200
    assert api.post("/api/member/delete", {"dsn": FB, "name": "N1"})[0] == 200
    assert "tracks_before" in api.post("/api/compress", {"dsn": FB})[1]
    st, _, _ = api.post("/api/save", {"dsn": FBSEQ, "lines": ["seq"]})
    assert st == 200
    assert api.get(f"/api/text?dsn={FBSEQ}")[1]["lines"] == ["seq".ljust(80)]


def test_error_shape(api, img):
    _open(api, img)
    st, body, _ = api.get(f"/api/text?dsn={FB}&member=NOPE")
    assert st == 404 and body["kind"] == "NotFoundError" and body["error"]
    st, body, _ = api.post("/api/save", {"dsn": FB, "member": "A", "lines": ["x" * 81]})
    assert st == 422 and body["kind"] == "ValidationError" and body["line"] == 1
    assert api.post("/api/save", {"dsn": FB})[0] == 400          # missing field
    assert api.get("/api/text")[0] == 400
    assert api.get("/api/nothing")[0] == 404
    req = urllib.request.Request(api.base + "/api/save", data=b"{bad", method="POST",
                                 headers={"X-CKD-Offline": "1",
                                          "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400


def test_readonly_is_409(api, img):
    _open(api, img, "ro")
    st, body, _ = api.post("/api/save", {"dsn": FB, "member": "A", "lines": ["x"]})
    assert st == 409 and body["kind"] == "ReadOnlyError"


def test_no_session_is_409(api):
    st, body, _ = api.get("/api/datasets")
    assert st == 409 and body["kind"] == "NoSession"


def test_needs_recovery_ro(api, img):
    img.with_name(img.name + ".journal").write_bytes(b"junk")
    st, body, _ = api.post("/api/open", {"path": str(img), "mode": "ro"})
    assert st == 409 and body["kind"] == "NeedsRecovery"


def test_open_missing_file(api, tmp_path):
    st, body, _ = api.post("/api/open", {"path": str(tmp_path / "nope"), "mode": "rw"})
    assert st == 404


def test_open_replaces_previous_session(api, fixture_images):
    _open(api, fixture_images["ckd"])
    first = api.server.session
    _open(api, fixture_images["cckd64"])
    assert api.server.session is not first
    assert api.server.session.path.name.endswith("cckd64")
    assert api.get("/api/info")[1]["fmt"] != "ckd"


def test_codepage(api, img):
    _open(api, img)
    api.post("/api/save", {"dsn": FBSEQ, "lines": ["a"]})
    assert api.post("/api/codepage", {"cp": "037"})[0] == 200
    assert api.server.session.codepage == "037"
    st, body, _ = api.post("/api/codepage", {"cp": "999"})
    assert st == 422


def test_confirm_rw(api, img):
    _open(api, img)
    assert api.post("/api/confirm-rw")[0] == 200


def test_browse_fs(api, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.ckd").write_bytes(b"12345")
    st, body, _ = api.get(f"/api/browse-fs?dir={tmp_path}")
    assert st == 200
    ents = {e["name"]: e for e in body["entries"]}
    assert ents["sub"]["is_dir"] is True
    assert ents["a.ckd"] == {"name": "a.ckd", "is_dir": False, "size": 5}
    assert body["dir"] == str(tmp_path)
    assert api.get("/api/browse-fs")[0] == 200                  # default dir
    assert api.get(f"/api/browse-fs?dir={tmp_path}/nope")[0] == 404


def test_static_files(api):
    st, body, h = api.get("/", raw=True)
    assert st == 200 and h["Content-Type"].startswith("text/html")
    assert api.get("/index.html", raw=True)[0] == 200
    assert api.get("/nope.js", raw=True)[0] == 404


def test_static_content_types_and_traversal(api, monkeypatch):
    import ckdoffline.server as srv
    from pathlib import Path
    web = Path(srv.WEB_DIR)
    for name, ctype in (("t_test.js", "javascript"), ("t_test.css", "text/css"),
                        ("t_test.mjs", "javascript")):
        (web / name).write_text("x")
        try:
            st, _, h = api.get("/" + name, raw=True)
            assert st == 200 and ctype in h["Content-Type"]
        finally:
            (web / name).unlink()
    import socket
    for p in ("/../session.py", "/%2e%2e/session.py", "/..%2fsession.py"):
        s = socket.create_connection(("127.0.0.1", api.server.server_address[1]))
        s.sendall(f"GET {p} HTTP/1.0\r\n\r\n".encode())
        head = s.recv(4096)
        s.close()
        assert b"200" not in head.split(b"\r\n")[0], p


def test_internal_error_500(api, img, monkeypatch, capsys):
    _open(api, img)
    monkeypatch.setattr(api.server.session, "datasets",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    st, body, _ = api.get("/api/datasets")
    assert st == 500 and body == {"error": "internal error: boom", "kind": "InternalError"}
    assert "Traceback" in capsys.readouterr().err


def test_binds_localhost_only():
    server = make_server(lambda *a, **k: None, 0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def _raw(api, request_line, headers):
    import socket
    s = socket.create_connection(("127.0.0.1", api.server.server_address[1]))
    s.sendall((request_line + "\r\n" + "".join(h + "\r\n" for h in headers) + "\r\n").encode())
    head = s.recv(4096)
    s.close()
    return int(head.split()[1])


def test_csrf_missing_header_and_content_type(api, img):
    import hashlib
    _open(api, img)
    sha = hashlib.sha256(img.read_bytes()).hexdigest()
    body = json.dumps({"dsn": FB, "member": "EVIL", "lines": ["x"]}).encode()
    for hdrs, code in (({}, 403),                                       # no custom header
                       ({"X-CKD-Offline": "1", "Content-Type": "text/plain"}, 415),
                       ({"Content-Type": "text/plain"}, 403)):
        req = urllib.request.Request(api.base + "/api/save", data=body, method="POST", headers=hdrs)
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == code
        assert "Access-Control-Allow-Origin" not in e.value.headers
    req = urllib.request.Request(api.base + "/api/info")                # GET needs the header too
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    assert hashlib.sha256(img.read_bytes()).hexdigest() == sha
    assert api.get(f"/api/members?dsn={FB}")[1] == []


def test_bad_host_rejected(api):
    port = api.server.server_address[1]
    assert _raw(api, "GET /index.html HTTP/1.0", ["Host: evil.example:%d" % port]) == 403
    assert _raw(api, "GET /api/info HTTP/1.0", ["Host: evil.example", "X-CKD-Offline: 1"]) == 403
    assert _raw(api, "GET /index.html HTTP/1.0", [f"Host: 127.0.0.1:{port + 1}"]) == 403
    assert _raw(api, "GET /index.html HTTP/1.0", [f"Host: localhost:{port}"]) == 200
    assert _raw(api, "GET /index.html HTTP/1.0", [f"Host: 127.0.0.1:{port}"]) == 200


def test_content_length_and_bad_paths(api):
    port = api.server.server_address[1]
    base = [f"Host: 127.0.0.1:{port}", "X-CKD-Offline: 1", "Content-Type: application/json"]
    assert _raw(api, "POST /api/open HTTP/1.0", base + ["Content-Length: abc"]) == 400
    assert _raw(api, "POST /api/open HTTP/1.0", base + ["Content-Length: -5"]) == 400
    assert _raw(api, "POST /api/open HTTP/1.0", base + [f"Content-Length: {70 * 1024 * 1024}"]) == 413
    assert _raw(api, "GET /a%00b HTTP/1.0", [f"Host: 127.0.0.1:{port}"]) == 400
    assert api.post("/api/open", {"path": "~nosuchuser_xyz/img"})[0] in (400, 404)
    assert api.get("/api/browse-fs?dir=~nosuchuser_xyz")[0] in (400, 404)


def _run_cli(args):
    import re
    import subprocess
    import sys
    p = subprocess.Popen([sys.executable, "-m", "ckdoffline", *args, "--port", "0",
                          "--no-browser"], stdout=subprocess.PIPE, text=True)
    m = re.search(r"http://localhost:(\d+)/", p.stdout.readline())
    assert m, "no URL line printed"
    return p, f"http://localhost:{m.group(1)}"


def _info(url):
    req = urllib.request.Request(url + "/api/info", headers={"X-CKD-Offline": "1"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


@pytest.mark.parametrize("extra,mode", [([], "rw"), (["--readonly"], "ro")])
def test_cli_opens_image_and_serves(fixture_images, extra, mode):
    p, url = _run_cli([str(fixture_images["ckd"]), *extra])
    try:
        assert _info(url)["mode"] == mode
    finally:
        p.send_signal(2)
        assert p.wait(timeout=10) == 0


def test_patch_blocks_endpoint_all_or_nothing(api, img):
    import hashlib
    _open(api, img)
    lines = [f"LINE {i:05d}" for i in range(800)]
    assert api.post("/api/save", {"dsn": FB, "member": "M", "lines": lines})[0] == 200
    blocks = api.get(f"/api/blocks?dsn={FB}&member=M")[1]
    assert len(blocks) == 3
    new0 = "c1" + blocks[0][2:]
    sha0 = hashlib.sha256(img.read_bytes()).hexdigest()
    st, body, _ = api.post("/api/patch-blocks", {"dsn": FB, "member": "M", "patches": [
        {"index": 0, "hex": new0}, {"index": 1, "hex": "00"}]})
    assert st == 422 and body["kind"] == "ValidationError"
    assert hashlib.sha256(img.read_bytes()).hexdigest() == sha0
    st, info, _ = api.post("/api/patch-blocks", {"dsn": FB, "member": "M", "patches": [
        {"index": 0, "hex": new0}, {"index": 2, "hex": blocks[2]}]})
    assert st == 200 and info["volser"]
    assert api.get(f"/api/blocks?dsn={FB}&member=M")[1] == [new0, blocks[1], blocks[2]]
    assert api.post("/api/patch-blocks", {"dsn": FB, "member": "M", "patches": "x"})[0] == 400


def test_dscb_endpoint(api, img):
    _open(api, img)
    st, d, _ = api.get(f"/api/dscb?dsn={FB}")
    assert st == 200 and d["dsname"] == FB and d["recfm_str"] == "FB"
    assert d["extents"][0]["tracks"] == 5
    assert api.get("/api/dscb?dsn=NO.SUCH")[0] == 404


# -- final review -------------------------------------------------------------------------------
def _sha(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_image_changed_on_disk_is_409(api, img):
    _open(api, img)
    assert api.get("/api/datasets")[0] == 200
    with Session.open(img, backup=False) as other:
        other.save_text(FB, "NEWMEM", ["x"])
    sha = _sha(img)
    st, body, _ = api.post("/api/save", {"dsn": FB, "member": "A", "lines": ["y"]})
    assert st == 409 and body["kind"] == "ImageChanged"
    assert "changed on disk" in body["error"]
    assert _sha(img) == sha


def test_session_id_guards_against_other_tabs(api, fixture_images):
    a, b = fixture_images["cckd_zlib"], fixture_images["cckd64"]
    tab_a = _open(api, a)
    assert isinstance(tab_a["session_id"], int)
    sid_a = api.sid
    tab_b = _open(api, b)                                   # another tab opens another image
    assert tab_b["session_id"] != sid_a
    sha_a, sha_b = _sha(a), _sha(b)
    st, body, _ = api.post("/api/save", {"dsn": FB, "member": "A", "lines": ["x"]}, sid=sid_a)
    assert st == 409 and body["kind"] == "VolumeChanged" and "another tab" in body["error"]
    assert (_sha(a), _sha(b)) == (sha_a, sha_b)
    assert api.get("/api/datasets", sid=sid_a)[0] == 409
    assert api.get("/api/datasets", sid=False)[0] == 409       # header missing
    # open, info and browse-fs need no session id
    assert api.get("/api/info", sid=False)[1]["session_id"] == api.sid
    assert api.get("/api/browse-fs", sid=False)[0] == 200
    assert api.get("/api/datasets")[0] == 200
    # a code page change in one tab invalidates the others
    sid_b = api.sid
    st, info, _ = api.post("/api/codepage", {"cp": "037"})
    assert st == 200 and info["session_id"] != sid_b
    assert api.get(f"/api/text?dsn={FBSEQ}", sid=sid_b)[1]["kind"] == "VolumeChanged"
    assert api.get(f"/api/text?dsn={FBSEQ}")[0] == 200


def test_security_headers_everywhere(api, img):
    for st, _, h in (api.get("/", raw=True), api.get("/app.js", raw=True),
                     api.get("/nope.js", raw=True), api.get("/api/info", raw=True)):
        assert h["X-Frame-Options"] == "DENY", st
        assert h["X-Content-Type-Options"] == "nosniff"
        csp = h["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp and "default-src 'self'" in csp


def test_patch_blocks_sequential_recfm_u_endpoint(api, img):
    from test_session import _patch_f1
    _patch_f1(img, FBSEQ, recfm=0xC0)
    _open(api, img)
    [b0, *_] = api.get(f"/api/blocks?dsn={FBSEQ}")[1]
    new = "C1" * (len(b0) // 2)
    st, info, _ = api.post("/api/patch-blocks", {"dsn": FBSEQ, "member": None,
                                                 "patches": [{"index": 0, "hex": new}]})
    assert st == 200 and info["backup_path"]
    assert api.get(f"/api/blocks?dsn={FBSEQ}")[1][0].upper() == new


def test_open_without_write_permission_is_clear(api, img):
    import os
    os.chmod(img, 0o444)
    try:
        st, body, _ = api.post("/api/open", {"path": str(img), "mode": "rw"})
        assert st == 400 and body["kind"] == "PermissionDenied"
        assert body["error"].startswith("cannot open image for writing")
        assert _open(api, img, "ro")["mode"] == "ro"
    finally:
        os.chmod(img, 0o644)


def test_every_write_response_carries_backup_path(api, img):
    _open(api, img)
    st, r, _ = api.post("/api/compress", {"dsn": FB})
    assert st == 200 and "members" in r and r["info"]["backup_path"]
    assert r["info"]["session_id"] == api.sid


def _cli_fail(args):
    import subprocess
    import sys
    p = subprocess.run([sys.executable, "-m", "ckdoffline", *args, "--no-browser"],
                       capture_output=True, text=True, timeout=30)
    return p.returncode, p.stderr


def test_cli_port_in_use(fixture_images):
    import socket
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        rc, err = _cli_fail(["--port", str(busy.getsockname()[1])])
    assert rc == 2 and "already in use" in err and "Traceback" not in err


def test_cli_readonly_with_journal(fixture_images):
    img = fixture_images["ckd"]
    (img.parent / (img.name + ".journal")).write_bytes(b"partial")
    rc, err = _cli_fail([str(img), "--readonly", "--port", "0"])
    assert rc == 2 and "without --readonly" in err and "Traceback" not in err
