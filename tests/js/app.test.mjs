// Final review fixes in the front end: session id header, VolumeChanged, VIEW when
// unconfirmed, the once-only backup note, PS RECFM U hex editing, CAPS fold, free space line.
import test from "node:test";
import assert from "node:assert/strict";
import { App, VOLUME_CHANGED } from "../../ckdoffline/web/app.js";
import { screenFor } from "../../ckdoffline/web/screens/memlist.js";
import { up } from "../../ckdoffline/web/editor-model.js";
import { volumeSummary } from "../../ckdoffline/web/panel.js";

// app.js touches document.body only when rendering
globalThis.document = { body: { classList: { toggle() {}, add() {} } } };

const INFO = { volser: "TEST01", fmt: "CCKD", cyls: 100, mode: "rw", codepage: "1047",
  warning: null, backup_path: null, session_id: 7, free_tracks: 42, free_space_error: null,
  device: "3390" };

function mockFetch(handler) {
  const calls = [];
  globalThis.fetch = async (path, opts) => {
    calls.push({ path, headers: { ...opts.headers } });
    const [status, body] = handler(path, opts);
    return { ok: status < 400, status, json: async () => body };
  };
  return calls;
}

function fakeScreen() {
  return { render() {}, renders: 0 };
}

test("api() sends X-CKD-Offline always and X-CKD-Session except on open/info/browse-fs", async () => {
  const app = new App(null);
  app.info = { ...INFO };
  const calls = mockFetch(() => [200, {}]);
  await app.get("/api/datasets");
  await app.get("/api/text", { dsn: "A.B" });
  await app.post("/api/save", { dsn: "A.B", lines: [] });
  await app.get("/api/browse-fs", { dir: "/" });
  await app.get("/api/info");
  await app.post("/api/open", { path: "/x" });
  assert.ok(calls.every((c) => c.headers["X-CKD-Offline"] === "1"));
  assert.deepEqual(calls.map((c) => c.headers["X-CKD-Session"]), ["7", "7", "7", undefined, undefined, undefined]);
});

test("VolumeChanged closes every screen above the open panel and says so", async () => {
  const app = new App(null);
  app.info = { ...INFO };
  const newInfo = { ...INFO, volser: "OTHER1", session_id: 8 };
  mockFetch((path) => (path === "/api/info" ? [200, newInfo]
    : [409, { kind: "VolumeChanged", error: "Volume changed in another tab" }]));
  const bottom = fakeScreen();
  app.stack.push({ screen: bottom, resolve() {}, short: "", long: "" });
  const results = [];
  for (let k = 0; k < 2; k++) {
    app.stack.push({ screen: fakeScreen(), resolve: (v) => results.push(v), short: "", long: "" });
  }
  await assert.rejects(app.post("/api/save", { dsn: "A.B", lines: [] }), (e) => e.kind === "VolumeChanged");
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(app.stack.length, 1);
  assert.equal(app.top.screen, bottom);
  assert.deepEqual(results, [{ saved: false, volumeChanged: true }, { saved: false, volumeChanged: true }]);
  assert.equal(app.info.session_id, 8);
  assert.equal(app.top.long, VOLUME_CHANGED);
  assert.match(VOLUME_CHANGED, /^Volume changed in another tab — reload/);
});

test("an unconfirmed read-write session is VIEW (read-only), a confirmed one EDIT", () => {
  const app = new App(null);
  app.info = { ...INFO, warning: "CCKD 'opened' flag set" };
  assert.equal(app.readOnly, true);
  app.info = { ...INFO };
  assert.equal(app.readOnly, false);
  app.info = { ...INFO, mode: "ro" };
  assert.equal(app.readOnly, true);
});

test("the backup note is announced once, after the first write of any kind", () => {
  const app = new App(null);
  app.stack.push({ screen: fakeScreen(), resolve() {}, short: "", long: "" });
  app.setInfo({ ...INFO });
  app.message("Opened");
  assert.equal(app.top.long, "");
  app.setInfo({ ...INFO, backup_path: "/v/img.bak-1" }); // e.g. after a member delete
  app.message("Member deleted", "A deleted from X.Y");
  assert.equal(app.top.long, "A deleted from X.Y. Image backed up to /v/img.bak-1 before the first write");
  app.setInfo({ ...INFO, backup_path: "/v/img.bak-1" });
  app.message("Member renamed");
  assert.equal(app.top.long, "");
});

test("a sequential RECFM U data set opens in hex for patching; a PO without member does not", () => {
  const app = new App(null);
  app.info = { ...INFO };
  const ps = screenFor(app, { dsname: "U.SEQ", recfm_str: "U", dsorg_str: "PS" }, null, "edit");
  assert.equal(ps.constructor.name, "HexScreen");
  assert.equal(ps.readOnly, false);
  assert.equal(screenFor(app, { dsname: "U.SEQ", recfm_str: "U", dsorg_str: "PS" }, null, "browse").readOnly, true);
  assert.equal(screenFor(app, { dsname: "U.LIB", recfm_str: "U", dsorg_str: "PO" }, "M", "edit").readOnly, false);
  assert.equal(screenFor(app, { dsname: "U.LIB", recfm_str: "U", dsorg_str: "PO" }, null, "edit").readOnly, true);
});

test("CAPS never folds into characters outside U+0000-U+00FF", () => {
  assert.equal(up("abcäöü"), "ABCÄÖÜ");
  assert.equal(up("ÿµß"), "ÿµß"); // Ÿ, Greek Μ and "SS" would leave Latin-1 or change the length
});

test("volume summary shows device, format and free space or why it is unknown", () => {
  assert.equal(volumeSummary(INFO), "TEST01  3390 CCKD  100 cyl  42 free tracks");
  assert.equal(volumeSummary({ ...INFO, free_tracks: null, free_space_error: "F5 out of sync" }),
    "TEST01  3390 CCKD  100 cyl  free space unknown: F5 out of sync");
  assert.equal(volumeSummary(null), "No volume open");
});

// ---- residual: queued line commands must stop when the volume changes ----------------------
import { MemlistScreen } from "../../ckdoffline/web/screens/memlist.js";
import { DslistScreen } from "../../ckdoffline/web/screens/dslist.js";
import { ConfirmScreen } from "../../ckdoffline/web/panel.js";

/** A stand-in App: the first screen run (the edit of the first queued command) ends in a
 * 409 VolumeChanged, i.e. App.volumeChanged() bumped the epoch and unwound it. */
function unwindingApp() {
  const sent = [];
  const prompts = [];
  let first = true;
  const app = {
    epoch: 0, readOnly: false, codepage: "1047", info: { ...INFO },
    fields: { scroll: { value: "PAGE" }, cmd: { value: "" } },
    clearMessage() {}, message() {}, error() {}, render() {}, close() {},
    async get(path) { sent.push(["GET", path]); return []; },
    async post(path, body) { sent.push(["POST", path, body]); return { ...INFO }; },
    setInfo() {},
    async run(screen) {
      if (screen instanceof ConfirmScreen) { prompts.push(screen); return "Y"; }
      if (first) {
        first = false;
        app.epoch++; // what App.volumeChanged() does
        return { saved: false, volumeChanged: true };
      }
      return { saved: false };
    },
  };
  return { app, sent, prompts };
}

const fld = (value) => ({ value });

test("member list: a volume change during S stops the queued D (no prompt, no delete)", async () => {
  const { app, sent, prompts } = unwindingApp();
  const ds = { dsname: "A.PDS", recfm_str: "FB", dsorg_str: "PO", lrecl: 80 };
  const scr = new MemlistScreen(app, ds, "edit");
  scr.members = [{ name: "A" }, { name: "B" }];
  scr.cmds = [
    { c: fld("S"), p: fld(""), m: { name: "A" } },
    { c: fld("D"), p: fld(""), m: { name: "B" } },
  ];
  await scr.onKey("enter");
  assert.equal(prompts.length, 0, "no Delete prompt after the volume changed");
  assert.ok(!sent.some(([m, p]) => m === "POST"), `nothing written: ${JSON.stringify(sent)}`);
  assert.deepEqual(scr.typed, {}, "pending line commands are cleared");
});

test("member list: the volume changing while the Delete prompt is up stops the delete", async () => {
  const { app, sent } = unwindingApp();
  app.run = async () => { app.epoch++; return "Y"; }; // another tab acts while the prompt waits
  const scr = new MemlistScreen(app, { dsname: "A.PDS", recfm_str: "FB", dsorg_str: "PO" }, "edit");
  scr.members = [{ name: "B" }];
  scr.cmds = [{ c: fld("D"), p: fld(""), m: { name: "B" } }];
  await scr.onKey("enter");
  assert.ok(!sent.some(([m]) => m === "POST"));
});

test("DSLIST: a volume change during E stops the queued Z (no prompt, no compress)", async () => {
  const { app, sent, prompts } = unwindingApp();
  const a = { dsname: "A.SEQ", recfm_str: "FB", dsorg_str: "PS", lrecl: 80, editable: true };
  const b = { dsname: "B.PDS", recfm_str: "FB", dsorg_str: "PO", lrecl: 80, editable: true };
  const scr = new DslistScreen(app);
  scr.dss = [a, b];
  scr.cmds = [{ f: fld("E"), d: a }, { f: fld("Z"), d: b }];
  await scr.onKey("enter");
  assert.equal(prompts.length, 0);
  assert.ok(!sent.some(([m]) => m === "POST"), `nothing written: ${JSON.stringify(sent)}`);
  assert.deepEqual(scr.typed, {});
});

test("App.volumeChanged bumps the epoch at once", async () => {
  const app = new App(null);
  app.info = { ...INFO };
  mockFetch(() => [200, { ...INFO, session_id: 9 }]);
  app.stack.push({ screen: fakeScreen(), resolve() {}, short: "", long: "" });
  const e0 = app.epoch;
  const p = app.volumeChanged();
  assert.equal(app.epoch, e0 + 1); // before any await: callers resuming see it
  await p;
});
