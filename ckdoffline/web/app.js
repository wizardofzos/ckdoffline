// CKD-Offline front end: API access, the screen stack, the ISPF panel frame and the
// keyboard (PF keys, field navigation, overtype). Screens live in screens/*.js.
// Nothing touches the DOM at import time; start() runs only in a browser.

import { el, row, field, titleLine } from "./panel.js";
import { OpenScreen } from "./screens/open.js";

const FIXED_ROWS = 6; // status, title, command, message, 2 legend rows

const LEGENDS = {
  list: [["F3/Esc", "End"], ["F7/PgUp", "Up"], ["F8/PgDn", "Down"], ["F12/^⇧X", "Cancel"],
    ["Home", "Command"], ["Tab", "Next field"], ["^I", "Insert"]],
  edit: [["F3/Esc", "End"], ["F5", "Rfind"], ["F7/PgUp", "Up"], ["F8/PgDn", "Down"],
    ["F10/⇧←", "Left"], ["F11/⇧→", "Right"], ["F12/^⇧X", "Cancel"], ["Home", "Command"],
    ["^I", "Insert"]],
  hex: [["F3/Esc", "End"], ["F7/PgUp", "Up"], ["F8/PgDn", "Down"], ["F12/^⇧X", "Cancel"],
    ["Home", "Command"], ["Tab", "Next field"]],
  confirm: [["Enter", "Reply"], ["F3/Esc", "Cancel"], ["F12/^⇧X", "Cancel"]],
};

// Calls that work without the current session id (X-CKD-Session).
const NO_SESSION_ID = new Set(["/api/open", "/api/info", "/api/browse-fs"]);

export const VOLUME_CHANGED = "Volume changed in another tab — reload: another image was opened "
  + "or the code page was changed in another browser tab. This tab's screens were closed.";

const SHORT_FOR_KIND = {
  ValidationError: "Invalid request", NotFoundError: "Not found", NotFound: "Not found",
  UnsupportedError: "Not supported", ReadOnlyError: "Read-only session",
  NoSpaceError: "No space", DirectoryFullError: "Directory full", NeedsRecovery: "Needs recovery",
  Network: "Server not responding", NoSession: "No volume open", InternalError: "Internal error",
  ImageChanged: "Image changed on disk", VolumeChanged: "Volume changed",
  PermissionDenied: "Permission denied",
};

export class ApiError extends Error {
  constructor(status, body) {
    super(body?.error || `HTTP ${status}`);
    this.status = status;
    this.kind = body?.kind || "Error";
    this.line = body?.line;
  }
}

export class App {
  constructor(root) {
    this.root = root;
    this.info = null; // /api/info of the open session, or null
    this.backupNote = null; // "Image backed up to ..." waiting to be shown once
    // Bumped when another tab replaced the volume: screens running queued line commands
    // compare it to the value they started with and stop (see volumeChanged).
    this.epoch = 0;
    this.stack = []; // [{screen, resolve, short, long, busy}]
    this.insert = false;
    this.lineHeight = 20;
    this.charWidth = 9.6;
  }

  // ---- API ---------------------------------------------------------------------------

  async api(method, path, body) {
    const opts = { method, headers: { "X-CKD-Offline": "1" } };
    // Every call names the session it was made for, so a tab whose volume was replaced by
    // another tab (or whose code page changed) is refused instead of writing to the wrong one.
    const sid = this.info?.session_id;
    if (!NO_SESSION_ID.has(path.split("?")[0]) && sid !== undefined && sid !== null) {
      opts.headers["X-CKD-Session"] = String(sid);
    }
    if (method === "POST") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body ?? {});
    }
    let r;
    try {
      r = await fetch(path, opts);
    } catch (e) {
      throw new ApiError(0, { error: "The CKD-Offline server is not responding", kind: "Network" });
    }
    let data = null;
    try {
      data = await r.json();
    } catch {
      data = null;
    }
    if (!r.ok) {
      const err = new ApiError(r.status, data);
      if (err.kind === "VolumeChanged") this.volumeChanged();
      throw err;
    }
    return data;
  }

  /**
   * Another tab replaced the volume (or changed the code page): pick up the current session,
   * close every screen above the open panel and say so there.
   */
  async volumeChanged() {
    this.epoch++; // synchronously, before any caller resumes and runs its next command
    if (this.unwinding) return;
    this.unwinding = true;
    try {
      try {
        const info = await this.api("GET", "/api/info");
        this.info = null; // a different session: no backup note from it
        this.setInfo(info);
      } catch {
        this.info = null;
      }
      const popped = this.stack.splice(1).reverse();
      for (const e of popped) e.resolve({ saved: false, volumeChanged: true });
      // let the closed screens' callers finish before the message goes up
      await new Promise((res) => setTimeout(res, 0));
      if (this.top) {
        this.top.short = "Volume changed";
        this.top.long = VOLUME_CHANGED;
        this.render();
      }
    } finally {
      this.unwinding = false;
    }
  }

  get(path, params = {}) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined) q.set(k, v);
    const qs = q.toString();
    return this.api("GET", qs ? `${path}?${qs}` : path);
  }

  post(path, body) {
    return this.api("POST", path, body);
  }

  setInfo(info) {
    if (!(info && typeof info === "object" && "volser" in info)) return;
    // the first write of any kind makes the backup: announce it once, with the next message
    const bak = info.backup_path;
    if (this.info && bak && bak !== this.info.backup_path) {
      this.backupNote = `Image backed up to ${bak} before the first write`;
    }
    this.info = info;
  }

  /** Read-only, or read-write but the in-use warning not (yet) confirmed: VIEW, not EDIT. */
  get readOnly() {
    return !this.info || this.info.mode !== "rw" || !!this.info.warning;
  }

  get codepage() {
    return this.info?.codepage || "1047";
  }

  // ---- screen stack ------------------------------------------------------------------

  get top() {
    return this.stack[this.stack.length - 1];
  }

  /** Show a screen; resolves with the value it closes with. load() errors propagate. */
  async run(screen) {
    if (screen.load) await screen.load();
    return new Promise((resolve) => {
      this.stack.push({ screen, resolve, short: "", long: "" });
      this.render();
    });
  }

  close(value) {
    const top = this.stack.pop();
    if (this.stack.length) this.render();
    top.resolve(value);
  }

  /** Short message (title line, right) and optional long message (line 4) on the current screen. */
  message(short, long = "") {
    if (!this.top) return;
    if (this.backupNote) {
      long = long ? `${long}. ${this.backupNote}` : this.backupNote;
      this.backupNote = null;
    }
    this.top.short = short || "";
    this.top.long = long || "";
  }

  clearMessage() {
    this.message("", "");
  }

  /** Report an API or model error on the current screen. */
  error(e) {
    const msg = e?.message || String(e);
    if (msg.length <= 24) return this.message(msg, "");
    this.message(SHORT_FOR_KIND[e?.kind] || "Error", msg);
  }

  // ---- geometry ----------------------------------------------------------------------

  measure() {
    const probe = el("div", "panel probe");
    const r = el("div", "row", "X".repeat(100));
    probe.append(r);
    document.body.append(probe);
    const rect = r.getBoundingClientRect();
    this.charWidth = rect.width / 100 || 9.6;
    this.lineHeight = rect.height || 20;
    probe.remove();
  }

  get maxCols() {
    return Math.max(80, Math.floor((window.innerWidth - 24) / this.charWidth));
  }

  /** Rows available for the panel body. */
  get bodyRows() {
    const total = Math.floor((window.innerHeight - 16) / this.lineHeight);
    return Math.max(8, total - FIXED_ROWS);
  }

  // ---- rendering ---------------------------------------------------------------------

  /**
   * Re-render the current screen. keep(key) selects fields whose typed text is
   * carried over to the new rendering (fields are matched by their data-k key).
   */
  render(keep = null) {
    if (!this.top) return;
    const saved = {};
    let focusKey = null;
    let caret = 0;
    if (keep) {
      for (const i of this.root.querySelectorAll("input[data-k]")) {
        if (keep(i.dataset.k)) saved[i.dataset.k] = i.value;
      }
      const a = document.activeElement;
      if (a && a.dataset && a.dataset.k) {
        focusKey = a.dataset.k;
        caret = a.selectionStart ?? 0;
      }
    }
    document.body.classList.toggle("busy", !!this.top.busy);
    this.top.screen.render();
    if (keep) {
      for (const i of this.root.querySelectorAll("input[data-k]")) {
        if (i.dataset.k in saved) i.value = saved[i.dataset.k];
      }
      const f = focusKey && this.root.querySelector(`input[data-k="${CSS.escape(focusKey)}"]`);
      if (f) {
        f.focus();
        f.setSelectionRange(caret, caret);
      }
    }
  }

  /**
   * Draw the standard ISPF frame and return its fields.
   * opts: panel, title, right, rows (body rows), cols, command (bool), scroll (value or
   * null for no Scroll field), legend (key into LEGENDS), cmdLabel.
   */
  frame(opts) {
    const cols = Math.max(80, Math.min(opts.cols || 80, this.maxCols));
    const top = this.top;
    const p = el("div", "panel");
    p.style.setProperty("--cols", cols);

    // line 1: volume status, shown on every screen
    const i = this.info;
    const mode = i?.mode !== "rw" ? "R/O" : i.warning ? "R/W unconfirmed" : "R/W";
    const vol = i ? `${i.volser}  ${i.fmt}  ${i.cyls} cyl  ${mode}  CP ${i.codepage || ""}`
      : "No volume open";
    const left = "CKD-Offline";
    const ins = this.insert ? "INS  " : "";
    p.append(row([left, "status"], [(ins + vol).padStart(cols - left.length), "status"]));

    // line 2: panel name, centred title, short message (or the right-hand info)
    const short = top?.short || "";
    const right = short || opts.right || "";
    const t = titleLine(opts.panel || "", opts.title || "", "", cols);
    const rstart = Math.max(cols - right.length, 0);
    const titleRow = row([t.slice(0, rstart), "title"], [right, short ? "short" : "title"]);
    p.append(titleRow);

    // line 3: Command ===> ... Scroll ===> ...
    const out = { cols };
    if (opts.command !== false) {
      const label = opts.cmdLabel || "Command ===> ";
      const scrollPart = opts.scroll !== undefined && opts.scroll !== null ? 13 + 4 : 0;
      const w = cols - label.length - scrollPart - 1;
      out.cmd = field(opts.cmdValue || "", w, { key: "cmd", cls: "cmd", max: 1024 });
      const parts = [[label, "prompt"], out.cmd];
      if (scrollPart) {
        out.scroll = field(opts.scroll, 4, { key: "scroll", cls: "scroll" });
        parts.push([" Scroll ===> ", "prompt"], out.scroll);
      }
      p.append(row(...parts));
    } else {
      p.append(row(""));
    }
    // line 4: long message
    p.append(row([top?.long || "", "long"]));

    // body
    const body = el("div", "body");
    const rows = opts.rows || [];
    const n = this.bodyRows;
    for (let k = 0; k < n; k++) body.append(rows[k] || row(""));
    p.append(body);

    // PF key legend (two lines, as ISPF)
    const items = (LEGENDS[opts.legend || "list"]).map(([k, v]) => `${k}=${v}`);
    const lines = [""];
    for (const it of items) {
      const cur = lines[lines.length - 1];
      if (cur.length + it.length + 2 > cols && cur) lines.push("");
      lines[lines.length - 1] += (lines[lines.length - 1] ? "  " : " ") + it;
    }
    for (let k = 0; k < 2; k++) p.append(row([lines[k] || "", "legend"]));

    this.root.replaceChildren(p);
    this.fields = out;
    return out;
  }

  focusCommand() {
    const c = this.root.querySelector("input.cmd") || this.root.querySelector("input");
    if (c) {
      c.focus();
      c.setSelectionRange(0, 0);
    }
  }

  // ---- keyboard ----------------------------------------------------------------------

  static aid(e) {
    const k = e.key;
    if (k === "Enter" && !e.altKey) return "enter";
    if (k === "F3" || k === "Escape") return "pf3";
    if (k === "F5") return "pf5";
    if (k === "F7" || k === "PageUp") return "pf7";
    if (k === "F8" || k === "PageDown") return "pf8";
    if (k === "F10" || (e.shiftKey && k === "ArrowLeft")) return "pf10";
    if (k === "F11" || (e.shiftKey && k === "ArrowRight")) return "pf11";
    if (k === "F12" || (e.ctrlKey && e.shiftKey && (k === "X" || k === "x"))) return "pf12";
    return null;
  }

  /** Run fn for a stack entry with its keyboard locked; errors become messages. */
  async perform(entry, fn) {
    if (entry.busy) return; // keyboard locked while the screen works
    entry.busy = true;
    document.body.classList.add("busy");
    try {
      await fn();
    } catch (err) {
      if (this.top === entry) {
        this.error(err);
        this.render();
      }
    } finally {
      entry.busy = false;
      document.body.classList.toggle("busy", !!this.top?.busy);
    }
  }

  async onKeyDown(e) {
    const aid = App.aid(e);
    if (aid) {
      e.preventDefault();
      if (this.top) await this.perform(this.top, () => this.top.screen.onKey(aid));
      return;
    }
    if (/^F\d+$/.test(e.key)) {
      e.preventDefault(); // unassigned PF keys do nothing (and F5 must not reload)
      return;
    }
    const t = e.target;
    if (e.key === "Home" && !e.shiftKey) {
      e.preventDefault();
      this.focusCommand();
      return;
    }
    if ((e.key === "ArrowUp" || e.key === "ArrowDown") && !e.shiftKey && t instanceof HTMLInputElement) {
      e.preventDefault();
      this.moveVertical(t, e.key === "ArrowUp" ? -1 : 1);
      return;
    }
    if (e.key === "Insert" || (e.ctrlKey && !e.shiftKey && (e.key === "i" || e.key === "I"))) {
      e.preventDefault();
      this.insert = !this.insert;
      this.render(() => true);
      return;
    }
    if (t instanceof HTMLInputElement) this.fieldKey(e, t);
  }

  /** Overtype (3270 default), hex-only fields, autoskip at the end of a field. */
  fieldKey(e, t) {
    const hex = t.dataset.kind === "hex";
    if (hex && (e.key === "Backspace" || e.key === "Delete")) {
      e.preventDefault(); // overtype only: block lengths never change
      if (e.key === "Backspace") {
        const p = Math.max(0, (t.selectionStart ?? 0) - 1);
        t.setSelectionRange(p, p);
      }
      return;
    }
    if (e.key.length !== 1 || e.ctrlKey || e.metaKey || e.altKey) return;
    if (t.readOnly) {
      e.preventDefault();
      return;
    }
    let ch = e.key;
    if (hex) {
      e.preventDefault();
      if (!/^[0-9a-fA-F]$/.test(ch)) return;
      ch = ch.toUpperCase();
      this.overtype(t, ch, true);
      return;
    }
    const s = t.selectionStart ?? 0;
    if (s !== (t.selectionEnd ?? s)) return; // typing over a selection: browser default
    const max = t.maxLength > 0 ? t.maxLength : Infinity;
    if (!this.insert) {
      if (s < t.value.length || s >= max) {
        e.preventDefault();
        this.overtype(t, ch, false);
      }
      return;
    }
    // insert mode: a full fixed-width field takes the character only if it ends in a blank
    if (t.value.length >= max) {
      e.preventDefault();
      if (t.value.endsWith(" ")) {
        t.value = t.value.slice(0, s) + ch + t.value.slice(s, -1);
        t.setSelectionRange(s + 1, s + 1);
        t.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
  }

  overtype(t, ch, hexOnly) {
    let s = t.selectionStart ?? 0;
    const max = t.maxLength > 0 ? t.maxLength : Infinity;
    if (s >= Math.min(max, hexOnly ? t.value.length : Infinity)) {
      const next = this.nextField(t, hexOnly);
      if (!next) return;
      next.focus();
      next.setSelectionRange(0, 0);
      this.overtype(next, ch, hexOnly);
      return;
    }
    t.value = t.value.slice(0, s) + ch + t.value.slice(s + 1);
    s += 1;
    t.setSelectionRange(s, s);
    t.dispatchEvent(new Event("input", { bubbles: true }));
  }

  nextField(t, hexOnly) {
    const all = [...this.root.querySelectorAll("input")].filter((i) => !i.readOnly && (!hexOnly || i.dataset.kind === "hex"));
    const k = all.indexOf(t);
    return k >= 0 && k + 1 < all.length ? all[k + 1] : null;
  }

  /** Arrow up/down: the field in the row above/below, at the same screen column. */
  moveVertical(t, dir) {
    const rect = t.getBoundingClientRect();
    const x = rect.left + (t.selectionStart ?? 0) * this.charWidth;
    let r = t.closest(".row");
    const rows = [...this.root.querySelectorAll(".row")];
    let idx = rows.indexOf(r);
    for (idx += dir; idx >= 0 && idx < rows.length; idx += dir) {
      const inputs = [...rows[idx].querySelectorAll("input")];
      if (!inputs.length) continue;
      let best = null;
      let dist = Infinity;
      for (const i of inputs) {
        const b = i.getBoundingClientRect();
        const d = x < b.left ? b.left - x : x > b.right ? x - b.right : 0;
        if (d < dist) { dist = d; best = i; }
      }
      const b = best.getBoundingClientRect();
      const col = Math.max(0, Math.min(best.value.length, Math.round((x - b.left) / this.charWidth)));
      best.focus();
      best.setSelectionRange(col, col);
      return;
    }
  }

  /** True when some screen on the stack holds unsaved changes. */
  get dirty() {
    return this.stack.some((s) => s.screen.dirty && s.screen.dirty());
  }

  // ---- start -------------------------------------------------------------------------

  async start() {
    this.measure();
    document.addEventListener("keydown", (e) => this.onKeyDown(e));
    let t = null;
    window.addEventListener("resize", () => {
      clearTimeout(t);
      t = setTimeout(() => this.render(() => true), 120);
    });
    window.addEventListener("beforeunload", (e) => {
      if (this.dirty) e.preventDefault();
    });
    try {
      this.setInfo(await this.api("GET", "/api/info"));
    } catch {
      this.info = null; // no image opened from the command line
    }
    const open = new OpenScreen(this);
    await open.load();
    // the open screen is the bottom of the stack: it never closes
    this.stack.push({ screen: open, resolve: () => {}, short: "", long: "" });
    this.render();
    if (this.info) await this.perform(this.top, () => open.enterVolume());
  }
}

export function start() {
  const app = new App(document.getElementById("screen"));
  window.ckdApp = app;
  app.start();
  return app;
}

if (typeof document !== "undefined" && document.getElementById?.("screen")) start();
