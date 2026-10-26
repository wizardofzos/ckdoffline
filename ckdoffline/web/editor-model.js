// ISPF EDIT model: pure data + commands, no DOM and no Node-only APIs.
//
// Lines are addressed by 0-based index. A line-command key of -1 means the
// "TOP OF DATA" line (valid for I, A and B only).
//
// Each line is {data, tail, raw, touched}:
//   data    text in the editable data area (FB: padded to the data width)
//   tail    NUM ON only: columns 73-80 (the sequence number)
//   raw     hex of the untouched record from the server, or undefined
//   touched true once the text was changed, inserted or typed

const SEQ_LEN = 8;
const SEQ_COL = 72; // data area is columns 1-72 under NUM ON
const NUM_RE = /^\d{8}$/;
const WORD_CH = /[A-Za-z0-9@#$_]/;

const TOP = -1;

export function up(s) {
  // Per-character upper-casing that never changes the length, and never produces a
  // character outside U+0000-U+00FF (no EBCDIC code page has one: "ÿ" would become "Ÿ"
  // and "µ" Greek "Μ", which the save would then refuse).
  let out = "";
  for (const c of s) {
    const u = c.toUpperCase();
    out += u.length === c.length && u.codePointAt(0) <= 0xff ? u : c;
  }
  return out;
}

export class EditModel {
  constructor({ recfm = "FB", lrecl = 80, lines = [], raw = [] } = {}) {
    this.recfm = String(recfm).toUpperCase();
    this.lrecl = lrecl;
    this.isFixed = this.recfm.startsWith("F");
    this.isVar = this.recfm.startsWith("V");
    this.width = this.isVar ? lrecl - 4 : lrecl;

    this._lines = lines.map((t, i) => ({
      data: this.isFixed ? t.padEnd(this.width) : t,
      tail: "",
      raw: raw[i],
      touched: false,
    }));

    this.numOn = false;
    if (this._numSupported() && this._lines.length > 0 &&
        this._lines.every((l) => NUM_RE.test(l.data.slice(SEQ_COL, SEQ_COL + SEQ_LEN)))) {
      this._splitTails();
      this.numOn = true;
    }

    this.caps = !this._lines.some((l) => /[a-z]/.test(l.data));
    this.hex = false;
    this.cursor = null; // {line, col, len} of the last FIND/CHANGE hit
    this._lastFind = null;
    this._lastChange = null;
    this._pendBlock = null; // {type, line, n, mark}
    this._pendMC = null; // {kind: "C"|"M", lines: [], mark}
    this._dirty = false;
  }

  // ---- basic accessors -------------------------------------------------

  get dataWidth() {
    return this.numOn ? SEQ_COL : this.width;
  }

  get lineCount() {
    return this._lines.length;
  }

  /** Displayed text of every line (data area only). */
  get lines() {
    return this._lines.map((l) => l.data);
  }

  getLine(i) {
    return this._lines[i].data;
  }

  /** Sequence number text shown in columns 73-80 under NUM ON (else ""). */
  getTail(i) {
    return this._lines[i].tail;
  }

  /** True for a line inserted by I and not typed into yet (ISPF shows ''''''). */
  isInputLine(i) {
    return this._unused(this._lines[i]);
  }

  get modified() {
    return this._dirty;
  }

  /** Pending line-command marks to redisplay: {lineIndex: "DD"}. */
  get pendingMarks() {
    const marks = {};
    if (this._pendBlock) {
      const i = this._lines.indexOf(this._pendBlock.line);
      if (i >= 0) marks[i] = this._pendBlock.mark;
    }
    if (this._pendMC) {
      const i = this._lines.indexOf(this._pendMC.lines[0]);
      if (i >= 0) marks[i] = this._pendMC.mark;
    }
    return marks;
  }

  setCursor(line, col) {
    this.cursor = { line, col, len: 0 };
  }

  // ---- NUM -------------------------------------------------------------

  _numSupported() {
    return this.isFixed && this.lrecl === 80;
  }

  _splitTails() {
    for (const l of this._lines) {
      const full = l.data.padEnd(this.width);
      l.data = full.slice(0, SEQ_COL);
      l.tail = full.slice(SEQ_COL);
    }
  }

  _joinTails() {
    for (const l of this._lines) {
      l.data = (l.data.padEnd(SEQ_COL) + l.tail).padEnd(this.width);
      l.tail = "";
    }
  }

  _setNum(on) {
    if (on === this.numOn) return "";
    if (on) {
      if (!this._numSupported()) return "NUM is only supported for FB, LRECL 80";
      const bad = this._lines.some((l) => !NUM_RE.test(l.data.padEnd(80).slice(SEQ_COL)));
      this._splitTails();
      this.numOn = true;
      return bad ? "Columns 73-80 will be renumbered on save" : "";
    }
    this._joinTails();
    this.numOn = false;
    return "";
  }

  // ---- editing ---------------------------------------------------------

  _blank() {
    return {
      data: this.isFixed ? "".padEnd(this.dataWidth) : "",
      tail: this.numOn ? "".padEnd(SEQ_LEN) : "",
      raw: undefined,
      touched: true,
      input: true, // inserted by I and never typed into: vanishes on Enter
    };
  }

  _unused(l) {
    return l.input === true && l.data.trim() === "";
  }

  _clone(l) {
    return { data: l.data, tail: l.tail, raw: l.raw, touched: l.touched, input: false };
  }

  /** Replace a line's text. Unchanged text is a no-op (the line stays raw). */
  setLine(index, text) {
    const l = this._lines[index];
    if (!l) throw new RangeError(`No line ${index}`);
    const dw = this.dataWidth;
    let next = this.isFixed && text.length <= dw ? text.padEnd(dw) : text;
    if (!this.isFixed && next.trimEnd() === l.data.trimEnd()) return;
    if (next === l.data) return;
    if (this.caps) {
      // Diff on the right-trimmed text so FB padding does not hide the common suffix.
      const old = l.data.trimEnd();
      const nt = next.trimEnd();
      let p = 0;
      const min = Math.min(old.length, nt.length);
      while (p < min && old[p] === nt[p]) p++;
      let s = 0;
      while (s < min - p && old[old.length - 1 - s] === nt[nt.length - 1 - s]) s++;
      const fixed = nt.slice(0, p) + up(nt.slice(p, nt.length - s)) + nt.slice(nt.length - s);
      next = fixed + next.slice(nt.length);
    }
    if (next === l.data) return; // compared after the CAPS fold
    l.data = next;
    l.touched = true;
    if (next.trim() !== "") l.input = false;
    this._dirty = true;
  }

  /** Call after a successful save: clears the modified flag. */
  markSaved() {
    this._dirty = false;
  }

  // ---- line commands ---------------------------------------------------

  _err(message) {
    return { message, error: true, ...this._pendingInfo() };
  }

  _pendingInfo() {
    if (this._pendBlock) return { pending: this._pendBlock.type };
    if (this._pendMC) return { pending: this._pendMC.kind };
    return {};
  }

  /**
   * cmds: {lineIndex: "text typed in the line-command area"}.
   * Returns {pending?, message?, error?}. On error nothing is applied.
   */
  applyLineCommands(cmds) {
    const entries = []; // {idx, kind, n, mark}
    for (const key of Object.keys(cmds)) {
      const idx = Number(key);
      const text = String(cmds[key]).trim().toUpperCase();
      if (text === "") continue;
      let m = /^(DD|CC|MM|RR)(\d*)$/.exec(text);
      let e;
      if (m) {
        e = { idx, kind: m[1], n: m[2] === "" ? null : parseInt(m[2], 10), block: true };
      } else if ((m = /^([IDRCMAB])(\d*)$/.exec(text))) {
        e = { idx, kind: m[1], n: m[2] === "" ? null : parseInt(m[2], 10), block: false };
        if ("AB".includes(e.kind) && e.n !== null) return this._err("Invalid line command");
      } else {
        return this._err("Invalid line command");
      }
      if (e.n === 0) return this._err("Invalid line command");
      e.mark = text;
      if (!Number.isInteger(idx) || idx < TOP || idx >= this._lines.length) return this._err("Invalid line command");
      if (idx === TOP && !"IAB".includes(e.kind)) return this._err("Invalid line command");
      if (idx === TOP && e.block) return this._err("Invalid line command");
      if (idx === TOP && e.kind === "B") return this._err("B is invalid on TOP OF DATA");
      e.line = idx === TOP ? null : this._lines[idx];
      entries.push(e);
    }

    // Enter discards unused input lines (those without a command on them),
    // then command positions are re-resolved against the shrunken list.
    const keep = new Set(entries.map((e) => e.line));
    const kept = this._lines.filter((l) => keep.has(l) || !this._unused(l));
    if (kept.length !== this._lines.length) {
      this._lines = kept;
      for (const e of entries) if (e.line) e.idx = kept.indexOf(e.line);
    }

    // A pending mark redisplayed by the screen is the pending command, not a new one.
    const pb = this._pendBlock;
    const pm = this._pendMC;
    const fresh = entries.filter((e) => {
      if (pb && e.line === pb.line && e.mark === pb.mark) return false;
      if (pm && e.line === pm.lines[0] && e.mark === pm.mark) return false;
      return true;
    });

    // Drop pending state whose lines no longer exist.
    let pendBlock = pb && this._lines.indexOf(pb.line) >= 0 ? pb : null;
    let pendMC = null;
    if (pm) {
      const alive = pm.lines.filter((l) => this._lines.indexOf(l) >= 0);
      if (alive.length) pendMC = { ...pm, lines: alive };
    }

    // Pair block markers by type, in line order.
    const byType = { DD: [], CC: [], MM: [], RR: [] };
    for (const e of fresh) if (e.block) byType[e.kind].push(e);
    if (pendBlock) {
      byType[pendBlock.type].push({
        idx: this._lines.indexOf(pendBlock.line), kind: pendBlock.type, n: pendBlock.n,
        block: true, line: pendBlock.line, mark: pendBlock.mark, pending: true,
      });
    }
    const ranges = []; // {kind, s, e, n}
    const unpaired = [];
    for (const type of Object.keys(byType)) {
      const list = byType[type].sort((a, b) => a.idx - b.idx);
      for (let i = 0; i + 1 < list.length; i += 2) {
        ranges.push({ kind: type, s: list[i].idx, e: list[i + 1].idx, n: list[i].n ?? list[i + 1].n });
      }
      if (list.length % 2) unpaired.push(list[list.length - 1]);
    }
    if (unpaired.length > 1) return this._err("Conflicting line commands");

    const targets = [];
    const points = []; // I and R
    for (const e of fresh) {
      if (e.block) continue;
      if (e.kind === "A" || e.kind === "B") targets.push(e);
      else if (e.kind === "I" || e.kind === "R") points.push(e);
      else ranges.push({ kind: e.kind, s: e.idx, e: Math.min(this._lines.length - 1, e.idx + (e.n ?? 1) - 1), n: e.n });
    }
    if (targets.length > 1) return this._err("Conflicting line commands");

    // Overlap check across ranges, points, targets and the unpaired marker.
    const spans = [
      ...ranges.map((r) => [r.s, r.e]),
      ...points.map((p) => [p.idx, p.idx]),
      ...targets.map((t) => [t.idx, t.idx]),
      ...unpaired.map((u) => [u.idx, u.idx]),
    ].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    for (let i = 1; i < spans.length; i++) {
      if (spans[i][0] <= spans[i - 1][1]) return this._err("Conflicting line commands");
    }

    if (pendMC && pendMC.kind === "M") {
      for (const r of ranges) {
        if ((r.kind === "D" || r.kind === "DD") &&
            pendMC.lines.some((l) => { const i = this._lines.indexOf(l); return i >= r.s && i <= r.e; })) {
          return this._err("Conflicting line commands");
        }
      }
    }
    const sources = ranges.filter((r) => r.kind === "C" || r.kind === "CC" || r.kind === "M" || r.kind === "MM");
    if (sources.length > 1 || (sources.length && pendMC)) return this._err("Conflicting line commands");
    const target = targets[0];
    let src = null; // {kind: "C"|"M", lines, mark}
    if (sources.length) {
      const r = sources[0];
      const mark = fresh.find((e) => e.idx === r.s && e.kind === r.kind)?.mark ?? r.kind;
      src = { kind: r.kind[0], lines: this._lines.slice(r.s, r.e + 1), mark };
    } else if (pendMC) {
      src = pendMC;
    }
    if (target && !src) return this._err("No COPY/MOVE pending for target");
    if (target && target.line && src.lines.includes(target.line)) {
      return this._err("Target is within the lines being copied or moved");
    }

    // ---- apply (validation is complete; nothing below fails) ----
    const removed = new Set();
    const after = new Map(); // line object (or TOP) -> lines inserted after it
    const add = (key, items) => {
      if (!after.has(key)) after.set(key, []);
      after.get(key).push(...items);
    };
    const lineAt = (i) => (i === TOP ? TOP : this._lines[i]);
    const clones = (arr) => arr.map((l) => this._clone(l));

    for (const p of points) {
      const n = p.n ?? 1;
      if (p.kind === "I") {
        add(lineAt(p.idx), Array.from({ length: n }, () => this._blank()));
      } else {
        const rep = [];
        for (let k = 0; k < n; k++) rep.push(this._clone(this._lines[p.idx]));
        add(this._lines[p.idx], rep);
      }
    }
    let note = "";
    for (const r of ranges) {
      if (r.kind === "D" || r.kind === "DD") {
        for (let i = r.s; i <= r.e; i++) removed.add(this._lines[i]);
      } else if (r.kind === "RR") {
        const blk = this._lines.slice(r.s, r.e + 1);
        const rep = [];
        for (let k = 0; k < (r.n ?? 1); k++) rep.push(...clones(blk));
        add(this._lines[r.e], rep);
      }
    }
    let pendingAfter = null;
    if (src && target) {
      const key = target.kind === "A" ? lineAt(target.idx)
        : (target.idx === 0 || target.idx === TOP ? TOP : this._lines[target.idx - 1]);
      if (src.kind === "M") {
        for (const l of src.lines) removed.add(l);
        add(key, src.lines);
        note = `${src.lines.length} line(s) moved`;
      } else {
        add(key, clones(src.lines));
        note = `${src.lines.length} line(s) copied`;
      }
    } else if (src) {
      pendingAfter = src;
    }

    const out = [];
    const emit = (key) => {
      const a = after.get(key);
      if (a) out.push(...a);
    };
    emit(TOP);
    for (const l of this._lines) {
      if (!removed.has(l)) out.push(l);
      emit(l);
    }
    // Unused input lines do not count as a modification.
    const before = this._lines.filter((l) => !this._unused(l));
    const now = out.filter((l) => !this._unused(l));
    if (now.length !== before.length || now.some((l, i) => l !== before[i])) this._dirty = true;
    this._lines = out;

    // New pending state
    this._pendMC = pendingAfter;
    this._pendBlock = null;
    if (unpaired.length) {
      const u = unpaired[0];
      this._pendBlock = { type: u.kind, line: u.line, n: u.n, mark: u.mark };
    }
    const result = {};
    const msgs = [];
    if (note) msgs.push(note);
    if (this._pendBlock) msgs.push("Block command incomplete");
    if (this._pendMC) msgs.push("MOVE/COPY is pending");
    if (msgs.length) result.message = msgs.join("; ");
    Object.assign(result, this._pendingInfo());
    return result;
  }

  // ---- primary commands ------------------------------------------------

  static _tokens(cmd) {
    const re = /([A-Za-z]?)'((?:[^']|'')*)'|([A-Za-z]?)"((?:[^"]|"")*)"|(\S+)/g;
    const out = [];
    let m;
    while ((m = re.exec(cmd))) {
      if (m[5] !== undefined) {
        out.push({ text: m[5], quoted: false, type: "" });
      } else if (m[2] !== undefined) {
        out.push({ text: m[2].replace(/''/g, "'"), quoted: true, type: m[1].toUpperCase() });
      } else {
        out.push({ text: m[4].replace(/""/g, '"'), quoted: true, type: m[3].toUpperCase() });
      }
    }
    return out;
  }

  /** Returns {message?, action?: "save"|"cancel"|"end", cursor?}. */
  primary(cmd) {
    const toks = EditModel._tokens(String(cmd).trim());
    if (!toks.length) return {};
    const verb = toks[0].text.toUpperCase();
    const args = toks.slice(1);
    switch (verb) {
      case "SAVE": return { action: "save" };
      case "END": return { action: "end" };
      case "CANCEL": case "CAN": return { action: "cancel" };
      case "RESET": case "RES":
        this._pendBlock = null;
        this._pendMC = null;
        return {};
      case "CAPS": return this._toggle("caps", args);
      case "HEX": return this._toggle("hex", args);
      case "NUM": case "NUMBER": {
        const v = this._onOff(args);
        if (v === null) return { message: "Invalid parameter" };
        const msg = this._setNum(v);
        return msg ? { message: msg } : {};
      }
      case "FIND": case "F": return this._find(args);
      case "RFIND": case "RF": return this._rfind();
      case "CHANGE": case "CHG": case "C": return this._change(args);
      case "RCHANGE": case "RC": return this._rchange();
      default: return { message: "Invalid primary command" };
    }
  }

  _onOff(args) {
    if (!args.length) return true;
    const a = args[0].text.toUpperCase();
    if (a === "ON") return true;
    if (a === "OFF") return false;
    return null;
  }

  _toggle(field, args) {
    const v = this._onOff(args);
    if (v === null) return { message: "Invalid parameter" };
    this[field] = v;
    return {};
  }

  _searchSpec(tok) {
    if (tok.quoted && !["", "T", "C"].includes(tok.type)) return null;
    if (tok.text === "") return null;
    return { str: tok.text, cs: tok.type === "C" };
  }

  _parseFindOpts(args, spec) {
    let dir = "NEXT";
    let all = false;
    let mode = "CHARS";
    for (const a of args) {
      const w = a.text.toUpperCase();
      if (a.quoted) return null;
      if (w === "ALL") all = true;
      else if (w === "NEXT" || w === "PREV" || w === "FIRST" || w === "LAST") dir = w;
      else if (w === "CHARS" || w === "WORD" || w === "PREFIX" || w === "SUFFIX") mode = w;
      else return null;
    }
    return { ...spec, dir, all, mode };
  }

  _matchAt(text, spec, from) {
    // First match with start >= from, honouring CHARS/WORD/PREFIX/SUFFIX.
    const hay = spec.cs ? text : up(text);
    const needle = spec.cs ? spec.str : up(spec.str);
    let i = hay.indexOf(needle, Math.max(0, from));
    while (i >= 0) {
      if (this._boundaryOk(text, i, needle.length, spec.mode)) return i;
      i = hay.indexOf(needle, i + 1);
    }
    return -1;
  }

  _boundaryOk(text, i, n, mode) {
    const before = i > 0 && WORD_CH.test(text[i - 1]);
    const afterCh = i + n < text.length && WORD_CH.test(text[i + n]);
    if (mode === "WORD") return !before && !afterCh;
    if (mode === "PREFIX") return !before;
    if (mode === "SUFFIX") return !afterCh;
    return true;
  }

  _matchBefore(text, spec, before) {
    // Last match whose start < before.
    let best = -1;
    let i = this._matchAt(text, spec, 0);
    while (i >= 0 && i < before) {
      best = i;
      i = this._matchAt(text, spec, i + 1);
    }
    return best;
  }

  _label(spec) {
    return `${spec.mode} '${spec.str}'`;
  }

  _locate(spec, dir) {
    const n = this._lines.length;
    if (dir === "FIRST") { this.cursor = null; dir = "NEXT"; }
    if (dir === "LAST") { this.cursor = null; dir = "PREV"; }
    const hadCursor = this.cursor !== null;
    if (dir === "NEXT") {
      const sl = hadCursor ? this.cursor.line : 0;
      const sc = hadCursor ? this.cursor.col + this.cursor.len : 0;
      for (let l = sl; l < n; l++) {
        const c = this._matchAt(this._lines[l].data, spec, l === sl ? sc : 0);
        if (c >= 0) return { line: l, col: c, len: spec.str.length };
      }
    } else {
      const sl = hadCursor ? this.cursor.line : n - 1;
      const sc = hadCursor ? this.cursor.col : Infinity;
      for (let l = sl; l >= 0; l--) {
        const c = this._matchBefore(this._lines[l].data, spec, l === sl ? sc : Infinity);
        if (c >= 0) return { line: l, col: c, len: spec.str.length };
      }
    }
    return { wrapped: hadCursor, dir };
  }

  _find(args) {
    if (!args.length) return this._rfind();
    const spec0 = this._searchSpec(args[0]);
    if (!spec0) return { message: "Invalid search string" };
    const spec = this._parseFindOpts(args.slice(1), spec0);
    if (!spec) return { message: "Invalid parameter" };
    this._lastFind = spec;
    return this._runFind(spec, spec.dir);
  }

  _rfind() {
    if (!this._lastFind) return { message: "No previous FIND" };
    const s = this._lastFind;
    return this._runFind(s, s.dir === "LAST" || s.dir === "PREV" ? "PREV" : "NEXT");
  }

  _runFind(spec, dir) {
    if (spec.all) {
      let count = 0;
      let first = null;
      for (let l = 0; l < this._lines.length; l++) {
        let c = this._matchAt(this._lines[l].data, spec, 0);
        while (c >= 0) {
          count++;
          if (!first) first = { line: l, col: c, len: spec.str.length };
          c = this._matchAt(this._lines[l].data, spec, c + spec.str.length);
        }
      }
      if (!count) return { message: `${this._label(spec)} not found` };
      this.cursor = first;
      return { message: `${this._label(spec)} found ${count} time(s)`, cursor: first };
    }
    const r = this._locate(spec, dir);
    if (r.line !== undefined) {
      this.cursor = r;
      return { message: `${this._label(spec)} found`, cursor: r };
    }
    if (r.wrapped) {
      this.cursor = null;
      return { message: r.dir === "NEXT" ? "Bottom of data reached" : "Top of data reached" };
    }
    return { message: `${this._label(spec)} not found` };
  }

  _change(args) {
    if (args.length < 2) return { message: "CHANGE needs a search string and a replacement" };
    const spec0 = this._searchSpec(args[0]);
    if (!spec0) return { message: "Invalid search string" };
    const t2 = args[1];
    if (t2.quoted && !["", "T", "C"].includes(t2.type)) return { message: "Invalid replacement string" };
    const spec = this._parseFindOpts(args.slice(2), spec0);
    if (!spec) return { message: "Invalid parameter" };
    const job = { spec, to: t2.text };
    this._lastChange = job;
    this._lastFind = spec;
    return this._runChange(job);
  }

  _rchange() {
    if (!this._lastChange) return { message: "No previous CHANGE" };
    return this._runChange(this._lastChange);
  }

  _fit(text, orig) {
    // Returns the fitted text for the line, or null if it would truncate data.
    const dw = this.dataWidth;
    if (text.length <= dw) return this.isFixed ? text.padEnd(dw) : text;
    if (orig.length > dw) return text; // already over-long; leave to toSave
    if (this.isFixed && text.slice(dw).trim() === "") return text.slice(0, dw);
    return null;
  }

  _runChange({ spec, to }) {
    const len = spec.str.length;
    let count = 0;
    let skipped = 0;
    let lastHit = null;
    let skipPos = null;
    const doLine = (l, fromCol, once) => {
      const line = this._lines[l];
      const text = line.data;
      let out = "";
      let pos = 0;
      let c = this._matchAt(text, spec, fromCol);
      const first = c;
      let n = 0;
      let hit = null;
      while (c >= 0) {
        out += text.slice(pos, c);
        hit = { line: l, col: out.length, len: to.length };
        out += to;
        pos = c + len;
        n++;
        if (once) break;
        c = this._matchAt(text, spec, pos);
      }
      if (!n) return 0;
      out += text.slice(pos);
      const fitted = this._fit(out, text);
      if (fitted === null) { skipped += n; skipPos = { line: l, col: first + len, len: 0 }; return -1; }
      if (fitted !== text) {
        line.data = fitted;
        line.touched = true;
        this._dirty = true;
      }
      count += n;
      lastHit = hit;
      return n;
    };

    if (spec.all) {
      for (let l = 0; l < this._lines.length; l++) doLine(l, 0, false);
    } else {
      const forward = spec.dir !== "PREV" && spec.dir !== "LAST";
      if (!forward) return { message: "CHANGE searches forward only" };
      if (spec.dir === "FIRST") this.cursor = null;
      const sl = this.cursor ? this.cursor.line : 0;
      const sc = this.cursor ? this.cursor.col : 0; // CHANGE starts AT the cursor, inclusive
      for (let l = sl; l < this._lines.length; l++) {
        const r = doLine(l, l === sl ? sc : 0, true);
        if (r !== 0) break;
      }
      if (!count && !skipped && this.cursor) {
        this.cursor = null;
        return { message: "Bottom of data reached" };
      }
    }
    // The cursor moves past the replacement (or past a skipped occurrence).
    if (lastHit) this.cursor = { line: lastHit.line, col: lastHit.col + lastHit.len, len: 0 };
    else if (skipPos && !spec.all) this.cursor = skipPos;
    if (!count && !skipped) return { message: `${this._label(spec)} not found` };
    let msg = `CHG ${count}`;
    if (skipped) msg += `; ${skipped} not changed, line too long`;
    return lastHit ? { message: msg, cursor: lastHit } : { message: msg };
  }

  // ---- save ------------------------------------------------------------

  /**
   * Model index (0-based) of the n-th (1-based) line sent by the last toSave(), e.g. for
   * a server error naming a line: unused input lines are not sent, so the two differ.
   */
  modelIndex(n) {
    return this.savedIndices?.[n - 1] ?? null;
  }

  /** Array of str | {raw} for POST /api/save (savedIndices records each line's model index). Throws Error with .line (1-based) if too long. */
  toSave() {
    const dw = this.dataWidth;
    this._lines.forEach((l, i) => {
      if (l.data.length > dw) {
        const e = new Error(`Line ${i + 1} is longer than ${dw} characters`);
        e.line = i + 1;
        throw e;
      }
    });
    const sent = [];
    this._lines.forEach((l, i) => { if (!this._unused(l)) sent.push(i); });
    this.savedIndices = sent;
    const lines = sent.map((i) => this._lines[i]);
    if (this.numOn && lines.length * 100 > 99999999) {
      const e = new Error("Too many lines to number");
      e.line = Math.floor(99999999 / 100) + 1;
      throw e;
    }
    return lines.map((l, i) => {
      if (this.numOn) {
        const seq = String((i + 1) * 100).padStart(SEQ_LEN, "0");
        if (!l.touched && l.raw !== undefined) {
          if (l.tail === seq) return { raw: l.raw };
          // Only the number changed: patch columns 73-80 of the raw record with
          // EBCDIC digits (F0-F9 in every code page) so other bytes round-trip.
          if (l.raw.length === 2 * (SEQ_COL + SEQ_LEN)) {
            const digits = Array.from(seq, (d) => "f" + d).join("");
            return { raw: l.raw.slice(0, 2 * SEQ_COL) + digits };
          }
        }
        return l.data.padEnd(SEQ_COL) + seq;
      }
      if (!l.touched && l.raw !== undefined) return { raw: l.raw };
      return l.data;
    });
  }
}
