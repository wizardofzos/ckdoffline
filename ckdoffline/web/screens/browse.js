// BROWSE: read-only view of a text data set or member, with FIND/RFIND, HEX ON/OFF,
// TOP/BOTTOM/LOCATE and PF7/8/10/11 scrolling. (RECFM U browses in the hex screen.)

import { EditModel } from "../editor-model.js";
import { row, displayText, vhex, scrollAmount, isScrollSpec, pad } from "../panel.js";
import { markerText, geometry, verticalAmount } from "./edit.js";

const ALLOWED = new Set(["FIND", "F", "RFIND", "RF", "HEX", "RESET", "RES"]);

export class BrowseScreen {
  constructor(app, { ds, member = null }) {
    this.app = app;
    this.ds = ds;
    this.dsn = ds.dsname;
    this.member = member;
    this.top = -1;
    this.left = 0;
    this.scroll = "PAGE";
  }

  async load() {
    const t = await this.app.get("/api/text", { dsn: this.dsn, member: this.member });
    this.model = new EditModel({ recfm: t.recfm, lrecl: t.lrecl, lines: t.lines, raw: t.raw_lines });
    if (this.model.numOn) this.model.primary("NUM OFF"); // browse shows every column
  }

  dirty() {
    return false;
  }

  render() {
    const a = this.app;
    const m = this.model;
    const dw = m.dataWidth;
    const { cols, winW } = geometry(a, dw, 0);
    this.winW = winW;
    this.left = Math.max(0, Math.min(this.left, Math.max(0, dw - winW)));
    this.top = Math.max(-1, Math.min(this.top, m.lineCount));
    const rows = [];
    const budget = a.bodyRows;
    const hit = m.cursor && m.cursor.len ? m.cursor : null;
    let v = this.top;
    for (; v <= m.lineCount; v++) {
      const need = v >= 0 && v < m.lineCount && m.hex ? 4 : 1;
      if (rows.length + need > budget) break;
      if (v === -1 || v === m.lineCount) {
        rows.push(row([markerText(v === -1 ? "Top of Data" : "Bottom of Data", winW), "marker"]));
        continue;
      }
      const slice = m.getLine(v).slice(this.left, this.left + winW);
      const text = displayText(slice);
      if (hit && hit.line === v && hit.col + hit.len > this.left && hit.col < this.left + winW) {
        const s = Math.max(0, hit.col - this.left);
        const e = Math.min(winW, hit.col + hit.len - this.left);
        rows.push(row([text.slice(0, s), "out"], [text.slice(s, e), "found"], [text.slice(e), "out"]));
      } else {
        rows.push(row([text, "out"]));
      }
      if (m.hex) {
        const [hi, lo] = vhex(slice, a.codepage);
        rows.push(row([hi, "hex"]), row([lo, "hex"]), row(["-".repeat(winW), "dim"]));
      }
    }
    this.lastV = Math.min(v - 1, m.lineCount);
    this.page = Math.max(1, this.lastV - this.top + 1);
    const label = this.member ? `${this.dsn}(${this.member})` : this.dsn;
    const right = `Line ${pad(Math.max(0, this.top), 8)} Col ${pad(this.left + 1, 3)} ${pad(this.left + winW, 3)}`;
    a.frame({ panel: "BROWSE", title: label, right, rows, cols, scroll: this.scroll, legend: "edit", cmdValue: this.keepCmd || "" });
    this.keepCmd = "";
    if (!a.top.long) {
      a.root.querySelector(".long").textContent =
        `HEX ${m.hex ? "ON " : "OFF"}  RECFM ${m.recfm}  LRECL ${m.lrecl}  ${m.lineCount} lines   Commands: FIND  RFIND  HEX ON/OFF  TOP  BOTTOM  L n`;
    }
    a.focusCommand();
  }

  reveal(v, col) {
    if (v < this.top || v > this.lastV) this.top = v;
    if (col < this.left || col >= this.left + this.winW) this.left = Math.max(0, col - 8);
  }

  async onKey(key) {
    const a = this.app;
    const m = this.model;
    if (key === "pf3" || key === "pf12") return a.close({ saved: false });
    this.scroll = a.fields.scroll.value.trim().toUpperCase() || "PAGE";
    const cmdText = a.fields.cmd.value.trim();
    a.clearMessage();
    if (key === "pf5") this.result(m.primary("RFIND"));
    if (key === "pf7" || key === "pf8") {
      const spec = cmdText && isScrollSpec(cmdText) ? cmdText : this.scroll;
      const amt = verticalAmount(spec, this.page, key === "pf8" ? 1 : -1, null, this.top, this.lastV);
      if (amt === null) a.message("Invalid scroll amount");
      else this.top = key === "pf8" ? Math.min(m.lineCount, this.top + amt) : Math.max(-1, this.top - amt);
    }
    if (key === "pf10" || key === "pf11") {
      const spec = cmdText && isScrollSpec(cmdText) ? cmdText : this.scroll;
      const amt = scrollAmount(spec, this.winW, null);
      const max = Math.max(0, m.dataWidth - this.winW);
      if (amt === null) a.message("Invalid scroll amount");
      else this.left = key === "pf11" ? Math.min(max, this.left + amt) : Math.max(0, this.left - amt);
    }
    if (key === "enter" && cmdText) this.primary(cmdText);
    a.render();
  }

  primary(cmdText) {
    const a = this.app;
    const m = this.model;
    const words = cmdText.toUpperCase().split(/\s+/);
    const verb = words[0];
    if (verb === "TOP") { this.top = -1; return; }
    if (verb === "BOT" || verb === "BOTTOM") { this.top = Math.max(-1, m.lineCount - this.page + 1); return; }
    if ((verb === "L" || verb === "LOC" || verb === "LOCATE") && /^\d+$/.test(words[1] || "")) {
      this.top = Math.min(m.lineCount, Math.max(1, parseInt(words[1], 10))) - 1;
      return;
    }
    if (!ALLOWED.has(verb)) {
      a.message("Invalid command", "BROWSE commands: FIND, RFIND, HEX ON/OFF, RESET, TOP, BOTTOM, L n");
      this.keepCmd = cmdText;
      return;
    }
    if (verb === "RESET" || verb === "RES") m.cursor = null;
    this.result(m.primary(cmdText));
  }

  result(r) {
    if (r.message) this.app.message(r.message);
    if (r.cursor) this.reveal(r.cursor.line, r.cursor.col);
  }
}
