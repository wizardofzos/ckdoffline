// Open volume panel: image path, read-only and code page fields, a directory picker
// (GET /api/browse-fs) and the in-use warnings.

import { row, field, ConfirmScreen, scrollAmount, dotted, volumeSummary } from "../panel.js";
import { DslistScreen } from "./dslist.js";

const HEADER_ROWS = 12; // rows above the directory listing

function remembered() {
  try {
    return localStorage.getItem("ckdoffline.path") || "";
  } catch {
    return "";
  }
}

function remember(path) {
  try {
    localStorage.setItem("ckdoffline.path", path);
  } catch {
    // private window or blocked storage: nothing to remember
  }
}

function dirname(p) {
  const i = p.lastIndexOf("/");
  return i > 0 ? p.slice(0, i) : i === 0 ? "/" : "";
}

function size(n) {
  if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(1)}G`;
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)}M`;
  if (n >= 1 << 10) return `${(n / (1 << 10)).toFixed(1)}K`;
  return String(n);
}

export class OpenScreen {
  constructor(app) {
    this.app = app;
    this.path = remembered();
    this.ro = false;
    this.cp = app.codepage;
    this.listing = null; // {dir, parent, entries}
    this.top = 0;
  }

  async load() {
    try {
      await this.browse(dirname(this.path) || null);
    } catch {
      await this.browse(null).catch(() => {});
    }
  }

  async browse(dir) {
    this.listing = await this.app.get("/api/browse-fs", { dir });
    this.top = 0;
  }

  get listRows() {
    return Math.max(3, this.app.bodyRows - HEADER_ROWS);
  }

  render() {
    const a = this.app;
    const rows = [];
    const lbl = (t) => [`  ${dotted(t, 16)} `, "prompt"];
    this.fPath = field(this.path, 64, { key: "path", max: 4096 });
    this.fRo = field(this.ro ? "/" : "", 1, { key: "ro" });
    this.fCp = field(this.cp, 4, { key: "cp" });
    rows.push(row(""));
    rows.push(row(lbl("Image file"), this.fPath));
    rows.push(row(lbl("Read only"), this.fRo, ["  / = open read-only", "info"]));
    rows.push(row(lbl("Code page"), this.fCp, ["  1047 or 037", "info"]));
    rows.push(row(""));
    rows.push(row(["  Stop the emulator (ZD&T or Hercules) or detach the device before editing:", "warn"]));
    rows.push(row(["  There is no file lock to rely on. Plain CKD images have no in-use flag at all.", "warn"]));
    if (a.info) {
      rows.push(row([`  Open: ${volumeSummary(a.info)}, ${a.info.mode === "rw" ? "R/W" : "R/O"}.`
        + " Enter with no image file returns to its data set list.", "info"]));
    } else {
      rows.push(row(""));
    }
    rows.push(row(""));
    const L = this.listing;
    rows.push(row(["  Directory: ", "prompt"], [L ? L.dir : "(unavailable)", "hi"]));
    rows.push(row(["  S  Name".padEnd(60) + "Size".padStart(10), "head"]));
    this.sel = [];
    if (L) {
      const entries = [{ name: "..", is_dir: true, size: 0, up: true }, ...L.entries];
      const n = this.listRows;
      this.top = Math.max(0, Math.min(this.top, entries.length - 1));
      for (const e of entries.slice(this.top, this.top + n)) {
        const f = field("", 1, { key: `s:${e.name}` });
        this.sel.push({ f, e });
        const name = e.is_dir ? `${e.name}/` : e.name;
        rows.push(row("  ", f, "  ", [name.padEnd(55).slice(0, 55), e.is_dir ? "dir" : "out"],
          [(e.is_dir ? "" : size(e.size)).padStart(10), "out"]));
      }
      if (this.top + n >= entries.length) rows.push(row(["  " + "*".repeat(30) + " Bottom of list " + "*".repeat(30), "marker"]));
    }
    a.frame({ panel: "OPEN", title: "Open a 3390 Volume", rows, scroll: this.scroll || "PAGE", legend: "list" });
    this.fPath.focus();
    this.fPath.setSelectionRange(0, 0);
  }

  _read() {
    this.path = this.fPath.value.trim();
    this.ro = this.fRo.value.trim() !== "";
    this.cp = this.fCp.value.trim() || "1047";
    this.scroll = this.app.fields.scroll.value.trim() || "PAGE";
  }

  async onKey(key) {
    const a = this.app;
    this._read();
    a.clearMessage();
    if (key === "pf7" || key === "pf8") {
      const amt = scrollAmount(this.scroll, this.listRows, null);
      if (amt === null) { a.message("Invalid scroll amount"); return a.render(); }
      const count = (this.listing?.entries.length ?? 0) + 1;
      this.top = key === "pf8" ? Math.min(this.top + amt, Math.max(0, count - 1)) : Math.max(0, this.top - amt);
      if (!Number.isFinite(this.top)) this.top = key === "pf8" ? Math.max(0, count - this.listRows) : 0;
      return a.render();
    }
    if (key === "pf3" || key === "pf12") {
      a.message("", "This is the first panel. Close the browser tab to leave; stop the server with Ctrl-C.");
      return a.render();
    }
    if (key !== "enter") return;

    const cmd = a.fields.cmd.value.trim();
    if (cmd) {
      const m = /^CD\s+(.+)$/i.exec(cmd);
      if (!m) { a.message("Invalid command", "Commands: CD directory"); return a.render(() => true); }
      await this.browse(m[1]);
      return a.render();
    }
    const picked = this.sel.find((s) => s.f.value.trim() !== "");
    if (picked) {
      const { e } = picked;
      if (e.is_dir) {
        await this.browse(e.up ? this.listing.parent : `${this.listing.dir}/${e.name}`);
        return a.render();
      }
      this.path = `${this.listing.dir.replace(/\/$/, "")}/${e.name}`;
    }
    if (!this.path) {
      if (a.info) return this.enterVolume();
      a.message("Enter an image file", "Type a path, or select a file below with S");
      return a.render();
    }
    if (!["1047", "037"].includes(this.cp)) {
      a.message("Invalid code page", "Code page must be 1047 or 037");
      return a.render(() => true);
    }
    await this.openImage(this.path, this.ro ? "ro" : "rw");
  }

  async openImage(path, mode) {
    const a = this.app;
    let info;
    try {
      info = await a.post("/api/open", { path, mode });
    } catch (e) {
      await this.refreshInfo(); // a failed open may have closed the previous volume
      if (e.kind !== "NeedsRecovery") throw e;
      const ans = await a.run(new ConfirmScreen(a, {
        title: "Interrupted Write Found", warn: true,
        text: [e.message, "Reopening read-write rolls the image back to its state before that write."],
        choices: { Y: "reopen read-write to recover" },
      }));
      if (ans === "Y") return this.openImage(path, "rw");
      a.message("Image not opened");
      return a.render();
    }
    a.setInfo(info);
    remember(path);
    if (info.codepage !== this.cp) a.setInfo(await a.post("/api/codepage", { cp: this.cp }));
    if (info.warning) {
      const ans = await a.run(new ConfirmScreen(a, {
        title: "Volume May Be In Use", warn: true,
        text: [`WARNING: ${info.warning}.`,
          "Writing while the emulator has the image open will corrupt it. Stop it first."],
        choices: { Y: "continue read-write (the emulator is stopped)", R: "reopen read-only" },
      }));
      if (ans === "R") return this.openImage(path, "ro");
      if (ans !== "Y") {
        a.message("Writes not confirmed", "The volume is open, but saves are refused until you confirm. Enter to open it again.");
        return a.render();
      }
      a.setInfo(await a.post("/api/confirm-rw"));
    }
    if (info.recovered) {
      this.pending = ["Image recovered", "An interrupted write was rolled back from the undo journal"];
    }
    return this.enterVolume();
  }

  async refreshInfo() {
    try {
      this.app.setInfo(await this.app.get("/api/info"));
    } catch {
      this.app.info = null;
    }
  }

  async enterVolume() {
    const a = this.app;
    const ds = new DslistScreen(a);
    if (this.pending) {
      ds.initialMessage = this.pending;
      this.pending = null;
    }
    await a.run(ds);
    a.clearMessage();
    a.render();
  }
}
