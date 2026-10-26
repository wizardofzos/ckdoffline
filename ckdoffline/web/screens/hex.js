// Hex view/editor for RECFM U: per block, rows of offset / 32 hex digits / 16 EBCDIC
// characters. Overtype only (block lengths never change); SAVE or END sends every changed
// block in one /api/patch-blocks call. Works for a load library member and for a whole
// sequential RECFM U data set (member null).

import {
  row, field, el, scrollAmount, isScrollSpec, pad, hexDigitsOk, hexToBytes, bytesToHex, dumpChars,
} from "../panel.js";

const PER_ROW = 16;
const GROUP = 4; // bytes per hex field

function hex4(n) {
  return n.toString(16).toUpperCase().padStart(4, "0");
}

export class HexScreen {
  constructor(app, { ds, member = null, readOnly = false }) {
    this.app = app;
    this.ds = ds;
    this.dsn = ds.dsname;
    this.member = member;
    this.wantReadOnly = readOnly;
    this.top = 0;
    this.scroll = "PAGE";
    this.bad = new Set(); // field keys holding invalid hex
    this.first = true; // focus the first hex field on the first rendering
  }

  get readOnly() {
    return this.wantReadOnly || this.app.readOnly;
  }

  async load() {
    await this.fetch();
    if (!this.member && this.ds.dsorg_str !== "PS") this.wantReadOnly = true;
  }

  async fetch() {
    const list = await this.app.get("/api/blocks", { dsn: this.dsn, member: this.member });
    this.blocks = list.map(hexToBytes);
    this.orig = this.blocks.map((b) => b.slice());
    this.flat = [];
    this.blocks.forEach((b, i) => {
      this.flat.push({ head: true, b: i });
      for (let off = 0; off < b.length; off += PER_ROW) this.flat.push({ b: i, off });
    });
  }

  changed(i) {
    const a = this.blocks[i];
    const o = this.orig[i];
    for (let k = 0; k < a.length; k++) if (a[k] !== o[k]) return true;
    return false;
  }

  dirty() {
    return !!this.blocks && this.blocks.some((_, i) => this.changed(i));
  }

  get pageRows() {
    return this.app.bodyRows;
  }

  render() {
    const a = this.app;
    if (this.initialMessage) {
      a.message(...this.initialMessage);
      this.initialMessage = null;
    }
    const cp = a.codepage;
    const rows = [];
    this.top = Math.max(0, Math.min(this.top, Math.max(0, this.flat.length - 1)));
    const slice = this.flat.slice(this.top, this.top + this.pageRows);
    this.inputs = [];
    for (const r of slice) {
      const blk = this.blocks[r.b];
      if (r.head) {
        const mark = this.changed(r.b) ? "  (changed)" : "";
        rows.push(row([`Block ${pad(r.b + 1, 5)}   Length ${pad(blk.length, 5)} (X'${hex4(blk.length)}')${mark}`, "head"]));
        continue;
      }
      const bytes = blk.subarray(r.off, Math.min(blk.length, r.off + PER_ROW));
      const chars = el("span", "out", `*${dumpChars(bytes, cp).padEnd(PER_ROW)}*`);
      const parts = [[`  +${hex4(r.off)}  `, "prompt"]];
      for (let g = 0; g < PER_ROW; g += GROUP) {
        const gb = bytes.subarray(g, g + GROUP);
        const text = bytesToHex(gb);
        if (!text) {
          parts.push(" ".repeat(GROUP * 2 + 1));
          continue;
        }
        if (this.readOnly) {
          parts.push([text.padEnd(GROUP * 2), "out"], " ");
          continue;
        }
        const key = `h:${r.b}:${r.off + g}`;
        const f = field(text, text.length, { key, kind: "hex", cls: "hexfld" });
        f.dataset.b = r.b;
        f.dataset.at = r.off + g;
        f.addEventListener("input", () => this.sync(f, bytes, chars));
        this.inputs.push(f);
        parts.push(f, " ".repeat(1 + GROUP * 2 - text.length));
      }
      parts.push(" ", chars);
      rows.push(row(...parts));
    }
    if (this.top + this.pageRows >= this.flat.length) {
      rows.push(row(["*".repeat(29) + (this.flat.length ? " Bottom of Data " : " Member has no data ") + "*".repeat(29), "marker"]));
    }
    const label = this.member ? `${this.dsn}(${this.member})` : this.dsn;
    const cur = this.flat[this.top];
    const right = this.flat.length ? `Block ${pad((cur?.b ?? 0) + 1, 5)} of ${pad(this.blocks.length, 5)}` : "";
    a.frame({
      panel: this.readOnly ? "BROWSE" : "EDIT", title: `${label} - Hex`, right, rows, cols: 80,
      scroll: this.scroll, legend: "hex", cmdValue: this.keepCmd || "",
    });
    this.keepCmd = "";
    if (!a.top.long) {
      a.root.querySelector(".long").textContent = this.readOnly
        ? `RECFM ${this.ds.recfm_str}  ${this.blocks.length} blocks  read-only   Commands: TOP  BOTTOM  L block`
        : `RECFM ${this.ds.recfm_str}  ${this.blocks.length} blocks  overtype only   Commands: SAVE  CANCEL  TOP  BOTTOM  L block`;
    }
    const f = this.focusKey && this.inputs.find((i) => i.dataset.k === this.focusKey);
    this.focusKey = null;
    if (f) {
      f.focus();
      f.setSelectionRange(0, 0);
    } else if (this.first && this.inputs[0]) {
      this.inputs[0].focus();
      this.inputs[0].setSelectionRange(0, 0);
    } else {
      a.focusCommand();
    }
    this.first = false;
  }

  /** Live update of one hex field into the block and the character column. */
  sync(f, bytes, chars) {
    const v = f.value;
    const k = f.dataset.k;
    if (!hexDigitsOk(v) || v.length !== f.maxLength) {
      this.bad.add(k);
      f.classList.add("bad");
      return;
    }
    this.bad.delete(k);
    f.classList.remove("bad");
    const at = Number(f.dataset.at);
    const blk = this.blocks[Number(f.dataset.b)];
    hexToBytes(v).forEach((x, i) => { blk[at + i] = x; });
    chars.textContent = `*${dumpChars(bytes, this.app.codepage).padEnd(PER_ROW)}*`;
  }

  async onKey(key) {
    const a = this.app;
    if (key === "pf12") return a.close({ saved: false });
    this.scroll = a.fields.scroll.value.trim().toUpperCase() || "PAGE";
    const cmdText = a.fields.cmd.value.trim();
    a.clearMessage();
    if (this.bad.size) {
      a.message("Invalid hex", "Hex fields take the digits 0-9 and A-F only, and keep their length");
      this.focusKey = [...this.bad][0];
      this.keepCmd = cmdText;
      return a.render((k) => this.bad.has(k));
    }
    if (key === "pf3") return this.end();
    if (key === "pf7" || key === "pf8") {
      const spec = cmdText && isScrollSpec(cmdText) ? cmdText : this.scroll;
      const t = document.activeElement;
      let offset = null;
      if (t?.dataset?.kind === "hex") {
        const k = this.flat.findIndex((r) => !r.head && r.b === Number(t.dataset.b) && r.off === Number(t.dataset.at) - (Number(t.dataset.at) % PER_ROW));
        if (k >= 0) offset = key === "pf8" ? k - this.top : this.top + this.pageRows - 1 - k;
      }
      const amt = scrollAmount(spec, this.pageRows, offset);
      if (amt === null) a.message("Invalid scroll amount");
      else {
        const max = Math.max(0, this.flat.length - 1);
        this.top = key === "pf8" ? Math.min(max, this.top + amt) : Math.max(0, this.top - amt);
      }
      return a.render();
    }
    if (key === "enter" && cmdText) return this.primary(cmdText);
    return a.render();
  }

  async primary(cmdText) {
    const a = this.app;
    const words = cmdText.toUpperCase().split(/\s+/);
    switch (words[0]) {
      case "SAVE":
        await this.save();
        return a.render();
      case "END": return this.end();
      case "CANCEL": case "CAN": return a.close({ saved: false });
      case "TOP": this.top = 0; return a.render();
      case "BOT": case "BOTTOM": this.top = Math.max(0, this.flat.length - this.pageRows + 1); return a.render();
      case "L": case "LOC": case "LOCATE": {
        const n = parseInt(words[1], 10);
        const k = this.flat.findIndex((r) => r.head && r.b === n - 1);
        if (k < 0) a.message("Invalid block number", `Blocks are numbered 1 to ${this.blocks.length}`);
        else this.top = k;
        return a.render();
      }
      default:
        a.message("Invalid command", "Commands: SAVE, CANCEL, END, TOP, BOTTOM, L block");
        this.keepCmd = cmdText;
        return a.render();
    }
  }

  /** Patch every changed block in one transaction. False (with a message) on failure. */
  async save({ leaving = false } = {}) {
    const a = this.app;
    if (this.readOnly) {
      a.message("Read-only", "This view cannot be saved");
      return false;
    }
    const todo = this.blocks.map((_, i) => i).filter((i) => this.changed(i));
    if (!todo.length) {
      a.message("No changes");
      return true;
    }
    try {
      // one transaction: every changed block is written, or none is
      a.setInfo(await a.post("/api/patch-blocks", {
        dsn: this.dsn, member: this.member,
        patches: todo.map((i) => ({ index: i, hex: bytesToHex(this.blocks[i]) })),
      }));
    } catch (e) {
      a.error(e);
      return false;
    }
    for (const i of todo) this.orig[i] = this.blocks[i].slice();
    try {
      await this.fetch();
    } catch (e) {
      a.message("Saved; reload failed", `Saved; reload failed: ${e.message}`);
      return true;
    }
    if (!leaving) a.message(this.member ? "Member saved" : "Data set saved"); // + backup note
    return true;
  }

  async end() {
    const a = this.app;
    if (this.readOnly || !this.dirty()) return a.close({ saved: false });
    if (!(await this.save({ leaving: true }))) return a.render();
    return a.close({ saved: true, message: this.member ? "Member saved" : "Data set saved" });
  }
}
