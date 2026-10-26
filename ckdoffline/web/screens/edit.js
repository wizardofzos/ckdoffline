// ISPF EDIT for FB/VB text: prefix (line command) area + data area, rendering only
// the visible window. Editing semantics live in EditModel; this module maps the
// screen to it: data changes first, then line commands, then the primary command.

import { EditModel } from "../editor-model.js";
import {
  row, field, lineCommand, displayText, mergeEdited, fitFixed, vhex, scrollAmount,
  isScrollSpec, pad,
} from "../panel.js";

const PREFIX = 6;
const GUTTER = PREFIX + 1; // prefix area and the blank after it

/** "****** Top of Data ******" across `width` columns. */
export function markerText(label, width) {
  const mid = ` ${label} `;
  const left = Math.max(0, Math.floor((width - mid.length) / 2));
  return ("*".repeat(left) + mid).padEnd(width, "*").slice(0, Math.max(width, mid.length));
}

/** What the data field of line v shows: the window slice, its display text, the full line. */
export function screenLine(model, v, left, winW) {
  const line = model.getLine(v);
  const slice = line.slice(left, left + winW);
  return { line, slice, shown: displayText(slice).padEnd(winW) };
}

/** Panel geometry shared by EDIT and BROWSE: total columns and the data window width. */
export function geometry(app, dataWidth, gutter) {
  const cols = Math.max(80, Math.min(app.maxCols, dataWidth + gutter + 2));
  return { cols, winW: Math.min(dataWidth, cols - gutter - 1) };
}

/** Lines to scroll vertically: CSR uses the line under the cursor. */
export function verticalAmount(spec, page, dir, cursorV, top, lastV) {
  let offset = null;
  if (cursorV !== null && cursorV !== undefined) offset = dir > 0 ? cursorV - top : lastV - cursorV;
  return scrollAmount(spec, page, offset);
}

export class EditScreen {
  constructor(app, { ds, member = null, isNew = false }) {
    this.app = app;
    this.ds = ds;
    this.dsn = ds.dsname;
    this.member = member;
    this.isNew = isNew;
    this.top = -1; // virtual line at the top: -1 = Top of Data
    this.left = 0; // first data column shown (0-based)
    this.scroll = "CSR";
    this.typedPrefix = null;
    this.focus = "first";
    this.version = "";
  }

  get label() {
    return this.member ? `${this.dsn}(${this.member})` : this.dsn;
  }

  async load() {
    if (this.isNew) {
      this.model = new EditModel({ recfm: this.ds.recfm_str, lrecl: this.ds.lrecl, lines: [], raw: [] });
      // ISPF shows a new member as a screen of empty input lines
      this.model.applyLineCommands({ "-1": `I${Math.max(1, this.app.bodyRows - 2)}` });
      this.initialMessage = ["New member", `${this.member} is created in ${this.dsn} when you save it (PF3 or SAVE)`];
      return;
    }
    await this.fetch();
  }

  /** (Re)load the text from the server: raw bytes stay current after a save. */
  async fetch() {
    const a = this.app;
    const t = await a.get("/api/text", { dsn: this.dsn, member: this.member });
    const old = this.model;
    this.model = new EditModel({ recfm: t.recfm, lrecl: t.lrecl, lines: t.lines, raw: t.raw_lines });
    if (old) {
      this.model.caps = old.caps;
      this.model.hex = old.hex;
    }
    this.version = "";
    if (this.member) {
      try {
        const ms = await a.get("/api/members", { dsn: this.dsn });
        this.version = ms.find((m) => m.name === this.member.toUpperCase())?.version || "";
      } catch {
        this.version = "";
      }
    }
  }

  dirty() {
    return !!this.model?.modified;
  }

  get panelName() {
    return this.app.readOnly ? "VIEW" : "EDIT";
  }

  // ---- rendering ---------------------------------------------------------------------

  render() {
    const a = this.app;
    const m = this.model;
    if (this.initialMessage) {
      a.message(...this.initialMessage);
      this.initialMessage = null;
    }
    const dw = m.dataWidth;
    const { cols, winW } = geometry(a, dw, GUTTER);
    this.winW = winW;
    this.left = Math.max(0, Math.min(this.left, Math.max(0, dw - winW)));
    this.top = Math.max(-1, Math.min(this.top, m.lineCount));
    const budget = a.bodyRows;
    const rows = [];
    this.prefixes = [];
    this.datas = [];
    const marks = m.pendingMarks;
    const cp = a.codepage;
    let v = this.top;
    for (; v <= m.lineCount; v++) {
      const need = v >= 0 && v < m.lineCount && m.hex ? 4 : 1;
      if (rows.length + need > budget) break;
      if (v === -1 || v === m.lineCount) {
        const label = v === -1 ? "Top of Data" : "Bottom of Data";
        if (v === -1) {
          const pf = field(this.typedPrefix?.[v] ?? "******", PREFIX, { key: "p:-1", cls: "prefix", data: { v } });
          this.prefixes.push({ f: pf, v, orig: "******", isMark: false });
          rows.push(row(pf, " ", [markerText(label, winW), "marker"]));
        } else {
          rows.push(row(["******", "marker"], " ", [markerText(label, winW), "marker"]));
        }
        continue;
      }
      const mark = marks[v];
      const tail = m.numOn ? m.getTail(v) : "";
      const num = m.isInputLine(v) ? "''''''" : /^\d{6}/.test(tail) ? tail.slice(0, PREFIX) : pad(v + 1, PREFIX);
      const orig = mark ?? num;
      const pf = field(this.typedPrefix?.[v] ?? orig, PREFIX, {
        key: `p:${v}`, cls: mark ? "prefix mark" : "prefix", data: { v },
      });
      this.prefixes.push({ f: pf, v, orig, isMark: !!mark });
      const { line, slice, shown } = screenLine(m, v, this.left, winW);
      const df = field(shown, winW, {
        key: `d:${v}:${this.left}:${winW}`, cls: "data", fixed: true, data: { v },
        readOnly: false,
      });
      this.datas.push({ f: df, v, slice, shown, line });
      rows.push(row(pf, " ", df));
      if (m.hex) {
        const [hi, lo] = vhex(slice, cp);
        const g = " ".repeat(GUTTER);
        rows.push(row(g, [hi, "hex"]), row(g, [lo, "hex"]), row(g, ["-".repeat(winW), "dim"]));
      }
    }
    this.lastV = Math.min(v - 1, m.lineCount);
    this.page = Math.max(1, this.lastV - this.top + 1);
    const right = `Columns ${pad(this.left + 1, 5)} ${pad(this.left + winW, 5)}`;
    const title = `${this.label}${this.version ? ` - ${this.version}` : ""}`;
    a.frame({ panel: this.panelName, title, right, rows, cols, scroll: this.scroll, legend: "edit", cmdValue: this.keepCmd || "" });
    this.keepCmd = "";
    if (!a.top.long) {
      const on = (b) => (b ? "ON " : "OFF");
      a.root.querySelector(".long").textContent =
        `CAPS ${on(m.caps)}  NUM ${on(m.numOn)}  HEX ${on(m.hex)}  RECFM ${m.recfm}  LRECL ${m.lrecl}  ${m.lineCount} lines`;
    }
    this.placeCursor();
  }

  placeCursor() {
    const f = this.focus;
    this.focus = null;
    const a = this.app;
    if (f === "first") {
      const d = this.datas[0];
      if (d) {
        d.f.focus();
        d.f.setSelectionRange(0, 0);
      } else a.focusCommand();
      return;
    }
    if (f && typeof f === "object") {
      if (f.prefix) {
        const p = this.prefixes.find((x) => x.v === f.v);
        if (p) { p.f.focus(); p.f.setSelectionRange(0, 0); return; }
      }
      const d = this.datas.find((x) => x.v === f.v);
      if (d) {
        const s = Math.max(0, Math.min(this.winW, (f.col ?? 0) - this.left));
        const e = Math.min(this.winW, s + (f.len ?? 0));
        d.f.focus();
        d.f.setSelectionRange(s, e);
        return;
      }
    }
    a.focusCommand();
  }

  /** Scroll so line v (0-based) is on the screen; and its column, if given. */
  reveal(v, col = null) {
    if (v < this.top || v > this.lastV || this.lastV === undefined) this.top = v;
    if (col !== null) {
      if (col < this.left || col >= this.left + this.winW) this.left = Math.max(0, col - 8);
    }
  }

  // ---- reading the screen ------------------------------------------------------------

  cursorInfo() {
    const t = document.activeElement;
    if (!t || !t.dataset || t.dataset.v === undefined) return null;
    const v = Number(t.dataset.v);
    if (t.classList.contains("data")) return { v, col: this.left + (t.selectionStart ?? 0), data: true };
    return { v, prefix: true };
  }

  /** Apply typed data to the model; return the typed line commands. */
  collect() {
    const m = this.model;
    for (const d of this.datas) {
      const now = d.f.value;
      if (now === d.shown) continue;
      const merged = mergeEdited(d.slice, d.shown, now);
      let before = d.line.slice(0, this.left);
      const after = d.line.slice(this.left + this.winW);
      if (before.length < this.left && merged.trim()) before = before.padEnd(this.left);
      let full;
      if (m.isFixed) {
        full = fitFixed(before + (merged.length < this.winW ? merged.padEnd(this.winW) : merged) + after, m.dataWidth);
      } else if (after) {
        full = before + merged.padEnd(this.winW) + after;
      } else {
        full = (before + merged).trimEnd(); // trailing blanks are not kept in V records
      }
      m.setLine(d.v, full);
    }
    const cmds = {};
    const typed = {};
    for (const p of this.prefixes) {
      const now = p.f.value;
      typed[p.v] = now;
      const c = p.isMark ? now.trim().toUpperCase() : lineCommand(p.orig, now);
      if (c) cmds[p.v] = c;
    }
    return { cmds, typed };
  }

  // ---- keys --------------------------------------------------------------------------

  async onKey(key) {
    const a = this.app;
    const m = this.model;
    if (key === "pf12") return a.close({ saved: false }); // CANCEL: discard, no prompt (ISPF)
    this.scroll = a.fields.scroll.value.trim().toUpperCase() || "CSR";
    const cmdText = a.fields.cmd.value.trim();
    const cursor = this.cursorInfo();
    a.clearMessage();

    const { cmds, typed } = this.collect();
    const count = m.lineCount;
    const res = m.applyLineCommands(cmds);
    if (res.error) {
      // keep what was typed, unless the line list shifted under it
      this.typedPrefix = m.lineCount === count ? typed : null;
      a.message(res.message);
      this.keepCmd = cmdText;
      return a.render();
    }
    this.typedPrefix = null;
    if (res.message) a.message(res.message);
    if (Object.keys(cmds).length) {
      // after I (or a new member), the cursor goes to the first input line
      for (let v = Math.max(0, this.top); v < m.lineCount && v <= this.lastV + 1; v++) {
        if (m.isInputLine(v)) { this.focus = { v, col: this.left }; break; }
      }
    }

    switch (key) {
      case "pf3": return this.end();
      case "pf5": this.showResult(m.primary("RFIND")); break;
      case "pf7": case "pf8": this.scrollV(key === "pf8" ? 1 : -1, cmdText, cursor); break;
      case "pf10": case "pf11": this.scrollH(key === "pf11" ? 1 : -1, cmdText, cursor); break;
      case "enter":
        if (cmdText) return this.primary(cmdText);
        break;
      default: break;
    }
    a.render();
  }

  showResult(r) {
    if (!r) return;
    if (r.message) this.app.message(r.message);
    if (r.cursor) {
      this.reveal(r.cursor.line, r.cursor.col);
      this.focus = { v: r.cursor.line, col: r.cursor.col, len: r.cursor.len };
    }
  }

  scrollV(dir, cmdText, cursor) {
    const a = this.app;
    let spec = this.scroll;
    if (cmdText) {
      if (!isScrollSpec(cmdText)) { a.message("Invalid scroll amount"); this.keepCmd = cmdText; return; }
      spec = cmdText;
    }
    const amt = verticalAmount(spec, this.page, dir, cursor?.v, this.top, this.lastV);
    if (amt === null) { a.message("Invalid scroll amount"); return; }
    const n = this.model.lineCount;
    this.top = dir > 0 ? Math.min(n, this.top + amt) : Math.max(-1, this.top - amt);
  }

  scrollH(dir, cmdText, cursor) {
    const a = this.app;
    let spec = this.scroll;
    if (cmdText) {
      if (!isScrollSpec(cmdText)) { a.message("Invalid scroll amount"); this.keepCmd = cmdText; return; }
      spec = cmdText;
    }
    let offset = null;
    if (cursor?.data) offset = dir > 0 ? cursor.col - this.left : this.left + this.winW - 1 - cursor.col;
    const amt = scrollAmount(spec, this.winW, offset);
    if (amt === null) { a.message("Invalid scroll amount"); return; }
    const max = Math.max(0, this.model.dataWidth - this.winW);
    this.left = dir > 0 ? Math.min(max, this.left + amt) : Math.max(0, this.left - amt);
  }

  async primary(cmdText) {
    const a = this.app;
    const m = this.model;
    const words = cmdText.toUpperCase().split(/\s+/);
    const verb = words[0];
    if (verb === "TOP") { this.top = -1; return a.render(); }
    if (verb === "BOT" || verb === "BOTTOM") { this.top = Math.max(-1, m.lineCount - this.page + 1); return a.render(); }
    if ((verb === "L" || verb === "LOC" || verb === "LOCATE") && /^\d+$/.test(words[1] || "")) {
      const v = Math.min(m.lineCount, Math.max(1, parseInt(words[1], 10))) - 1;
      this.top = v;
      return a.render();
    }
    if (verb === "RES" || verb === "RESET") this.typedPrefix = null;
    const r = m.primary(cmdText);
    if (r.action === "save") {
      await this.save();
      return a.render();
    }
    if (r.action === "end") return this.end();
    if (r.action === "cancel") return a.close({ saved: false });
    this.showResult(r);
    if (r.message && /^(Invalid|No previous|CHANGE needs)/.test(r.message)) this.keepCmd = cmdText;
    return a.render();
  }

  // ---- save / end --------------------------------------------------------------------

  get savedMessage() {
    return this.member ? "Member saved" : "Data set saved";
  }

  /** SAVE: write through the API, then reload. False (with a message) on failure. */
  /** leaving: END will close the screen, so the caller's screen shows the message (and the
   * backup note) instead of this one. */
  async save({ leaving = false } = {}) {
    const a = this.app;
    const m = this.model;
    let lines;
    try {
      lines = m.toSave();
    } catch (e) {
      a.error(e); // e.line is a model line number (1-based)
      if (e.line) this.gotoLine(e.line - 1);
      return false;
    }
    try {
      a.setInfo(await a.post("/api/save", { dsn: this.dsn, member: this.member, lines }));
    } catch (e) {
      a.error(e);
      if (e.kind === "ReadOnlyError") {
        a.message("Read-only session", "The volume is open read-only: CANCEL (F12) leaves without saving");
      }
      // the server numbers the lines it was sent; unused input lines were not sent
      const v = e.line ? m.modelIndex(e.line) : null;
      if (v !== null) this.gotoLine(v);
      return false;
    }
    this.isNew = false;
    try {
      await this.fetch();
    } catch (e) {
      m.markSaved(); // the data is on disk: PF3 must not save it again
      a.message("Saved; reload failed", `Saved; reload failed: ${e.message}`);
      return true;
    }
    if (!leaving) a.message(this.savedMessage); // App.message adds the backup note once
    return true;
  }

  gotoLine(v) {
    if (v < 0 || v >= this.model.lineCount) return;
    this.reveal(v);
    this.focus = { v, col: this.left };
  }

  /** END (PF3): save if changed, as ISPF does, then leave. */
  async end() {
    const a = this.app;
    if (!this.model.modified) return a.close({ saved: false });
    if (!(await this.save({ leaving: true }))) return a.render();
    return a.close({ saved: true, message: this.savedMessage });
  }
}
