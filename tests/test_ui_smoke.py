import json
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from ckdoffline import ebcdic
from ckdoffline.server import make_server
from ckdoffline.session import Session

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "ckdoffline" / "web"
SCREENS = ["open", "dslist", "memlist", "edit", "browse", "hex"]


@pytest.fixture
def base(fixture_images):
    def factory(path, mode, **kw):
        return Session.open(path, mode, **kw)
    server = make_server(factory, 0)
    server.session = Session.open(fixture_images["ckd"], "ro")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    server.close_session()


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.headers.get("Content-Type"), r.read().decode()


def test_ui_smoke(base):
    st, ctype, html = _get(base + "/")
    assert st == 200 and ctype.startswith("text/html")
    for path in ["/app.js", "/editor-model.js", "/ebcdic.js", "/panel.js", "/style.css"]:
        st, ctype, _ = _get(base + path)
        assert st == 200, path
    for name in SCREENS:
        assert f'screens/{name}.js' in html, f"index.html does not reference {name}.js"
        st, ctype, _ = _get(f"{base}/screens/{name}.js")
        assert st == 200 and ctype.startswith("text/javascript")
    assert re.search(r'<script type="module" src="app.js">', html)
    assert 'href="style.css"' in html


def test_static_js_has_no_external_urls():
    """Offline tool: no CDN, no web fonts."""
    for f in [*WEB.glob("*.js"), *WEB.glob("screens/*.js"), *WEB.glob("*.html"),
              *WEB.glob("*.css")]:
        assert not re.search(r"https?://", f.read_text()), f


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_ebcdic_tables_match_python():
    code = ("import('./ckdoffline/web/ebcdic.js').then(m => console.log(JSON.stringify("
            "{'1047': m.decodeTable('1047'), '037': m.decodeTable('037')})))")
    r = subprocess.run(["node", "-e", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    tables = json.loads(r.stdout)
    for cp in ebcdic.CODEPAGES:
        assert len(tables[cp]) == 256
        for b in range(256):
            assert tables[cp][b] == ebcdic.to_text(bytes([b]), cp), (cp, hex(b))
