// ISPF panel building blocks: pure helpers (Node-testable) and DOM builders.
// Nothing here touches the DOM at import time.

import { encodeChar, isPrintable, decodeByte } from "./ebcdic.js";

// ---- pure helpers ----------------------------------------------------------------------

export function pad(n, width) {
  return String(n).padStart(width, "0");
}

/**
 * The line command typed into a prefix field. `orig` is what the field showed
 * (sequence number, ****** or ''''''), `now` what it holds. Characters the user
 * did not overtype are ignored, as ISPF does: "D00100" over "000100" is "D".
 */
export function lineCommand(orig, now) {
  const w = Math.max(orig.length, now.length);
  const a = orig.padEnd(w);
  const b = now.padEnd(w);
  let last = -1;
  for (let i = 0; i < w; i++) if (a[i] !== b[i]) last = i;
  return b.slice(0, last + 1).trim().toUpperCase();
}

const UNPRINTABLE = /[\u0000-\u001f\u007f-\u009f­]/g;

/** Text as shown on the screen: characters with no glyph become "." (ISPF style). */
export function displayText(s) {
  return s.replace(UNPRINTABLE, ".");
}

/**
 * Map an edited field back to real text. Where the user left a position as it was
 * displayed, the original character comes back (so an unprintable byte shown as "."
 * survives an edit elsewhere on the line). If the length changed, the screen wins.
 */
export function mergeEdited(orig, shown, now) {
  if (now === shown) return orig;
  if (now.length !== shown.length) return now;
  let out = "";
  for (let i = 0; i < now.length; i++) {
    out += now[i] === shown[i] ? (i < orig.length ? orig[i] : " ") : now[i];
  }
  return out;
}

/** FB: pad to the width; drop blank overflow from insert mode; keep real overflow. */
export function fitFixed(s, width) {
  if (s.length <= width) return s.padEnd(width);
  return s.slice(width).trim() === "" ? s.slice(0, width) : s;
}

/** ISPF HEX ON: two rows with the high and low nibble of each EBCDIC byte. */
export function vhex(text, cp) {
  let hi = "";
  let lo = "";
  for (const ch of text) {
    const b = encodeChar(ch, cp);
    if (b === undefined) { hi += "?"; lo += "?"; continue; }
    const h = b.toString(16).toUpperCase().padStart(2, "0");
    hi += h[0];
    lo += h[1];
  }
  return [hi, lo];
}

/** "*....*" style character column for a hex dump row. */
export function dumpChars(bytes, cp) {
  let s = "";
  for (const b of bytes) s += isPrintable(b, cp) ? decodeByte(b, cp) : ".";
  return s;
}

/**
 * Lines to scroll for a Scroll field value. `cursorOffset` is how far the cursor
 * row is from the edge being scrolled toward (null when the cursor is not in the
 * data). Returns Infinity for MAX, null for an invalid amount.
 */
export function scrollAmount(spec, page, cursorOffset) {
  const s = String(spec).trim().toUpperCase();
  if (s === "PAGE" || s === "P") return page;
  if (s === "HALF" || s === "H") return Math.max(1, Math.floor(page / 2));
  if (s === "MAX" || s === "M") return Infinity;
  if (s === "CSR" || s === "C") return cursorOffset ? cursorOffset : page;
  if (/^\d{1,4}$/.test(s)) return parseInt(s, 10);
  return null;
}

export function isScrollSpec(s) {
  return scrollAmount(s, 1, null) !== null;
}

/** null when valid, else the message (same rules as the server). */
export function memberNameError(name) {
  const n = String(name).trim().toUpperCase();
  if (!n) return "Enter a member name";
  if (n.length > 8) return `Member name ${n} is longer than 8 characters`;
  if (/^[0-9]/.test(n)) return `Member name ${n} is invalid: it starts with a digit`;
  if (!/^[A-Z0-9@#$]+$/.test(n)) return `Member name ${n} is invalid: use A-Z 0-9 @ # $`;
  return null;
}

export function hexDigitsOk(s) {
  return /^[0-9A-Fa-f]*$/.test(s);
}

export function hexToBytes(h) {
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.substr(i * 2, 2), 16);
  return out;
}

export function bytesToHex(b) {
  let s = "";
  for (const x of b) s += x.toString(16).toUpperCase().padStart(2, "0");
  return s;
}

/** ISPF field label with leader dots on alternate columns: "Record length . . . ". */
/** One-line volume summary: device, format, size and free space (or why it is unknown). */
export function volumeSummary(info) {
  if (!info) return "No volume open";
  const free = info.free_tracks !== null && info.free_tracks !== undefined
    ? `${info.free_tracks} free tracks`
    : `free space unknown: ${info.free_space_error || "not available"}`;
  return `${info.volser}  ${info.device || "3390"} ${info.fmt}  ${info.cyls} cyl  ${free}`;
}

export function dotted(label, width) {
  let out = `${label} `;
  for (let i = out.length; i < width; i++) out += i % 2 ? "." : " ";
  return out;
}

/** Centre `mid` between `left` and `right` on a line of `cols` characters. */
export function titleLine(left, mid, right, cols) {
  let line = left.padEnd(cols);
  const start = Math.max(left.length + 1, Math.floor((cols - mid.length) / 2));
  line = line.slice(0, start) + mid + line.slice(start + mid.length);
  const rstart = Math.max(start + mid.length + 1, cols - right.length);
  line = line.slice(0, rstart) + right + line.slice(rstart + right.length);
  return line.slice(0, Math.max(cols, rstart + right.length)).padEnd(cols);
}

// ---- DOM builders (only called at run time) ------------------------------------------

export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

/** A row of the panel: parts are strings (normal text), [text, cls] pairs, or elements. */
export function row(...parts) {
  const r = el("div", "row");
  for (const p of parts) {
    if (p === null || p === undefined || p === "") continue;
    if (typeof p === "string") r.append(el("span", "", p));
    else if (Array.isArray(p)) r.append(el("span", p[1], p[0]));
    else r.append(p);
  }
  return r;
}

/**
 * An unprotected 3270 field. opts: key (for keeping typed text across re-renders),
 * cls, kind ("hex" for hex overtype), fixed (field holds a fixed-width value),
 * placeholder, data (dataset entries).
 */
export function field(value, width, opts = {}) {
  const i = document.createElement("input");
  i.type = "text";
  i.className = "fld" + (opts.cls ? " " + opts.cls : "");
  i.value = value;
  i.spellcheck = false;
  i.autocomplete = "off";
  i.setAttribute("autocapitalize", "off");
  i.style.width = `${width}ch`;
  if (opts.max !== false) i.maxLength = opts.max ?? width;
  if (opts.key) i.dataset.k = opts.key;
  if (opts.kind) i.dataset.kind = opts.kind;
  if (opts.fixed) i.dataset.fixed = "1";
  if (opts.placeholder) i.placeholder = opts.placeholder;
  if (opts.readOnly) i.readOnly = true;
  for (const [k, v] of Object.entries(opts.data || {})) i.dataset[k] = v;
  return i;
}

// ---- generic confirm panel ---------------------------------------------------------------

/**
 * A pop-up style confirmation panel. `choices` maps a typed letter to its
 * meaning, e.g. {Y: "delete the member"}. Resolves (via app.run) to the letter, or
 * null on PF3/PF12.
 */
export class ConfirmScreen {
  constructor(app, { panel = "CONFIRM", title, text = [], choices = { Y: "confirm" }, warn = false }) {
    this.app = app;
    this.panel = panel;
    this.title = title;
    this.text = text;
    this.choices = choices;
    this.warn = warn;
  }

  render() {
    const a = this.app;
    const rows = [row("")];
    for (const t of this.text) rows.push(row([`  ${t}`, this.warn ? "warn" : "info"]));
    rows.push(row(""));
    for (const [k, v] of Object.entries(this.choices)) rows.push(row(["  Enter ", ""], [k, "hi"], [` to ${v}`, ""]));
    rows.push(row(["  Press PF3 or PF12 to cancel.", ""]));
    rows.push(row(""));
    this.answer = field("", 1, { key: "answer" });
    rows.push(row(["  Reply ===> ", "prompt"], this.answer));
    a.frame({ panel: this.panel, title: this.title, rows, command: false, legend: "confirm" });
    this.answer.focus();
  }

  onKey(key) {
    if (key === "pf3" || key === "pf12") return this.app.close(null);
    if (key !== "enter") return;
    const v = this.answer.value.trim().toUpperCase();
    if (v in this.choices) return this.app.close(v);
    this.app.message("Invalid reply", `Enter ${Object.keys(this.choices).join(" or ")}, or PF3 to cancel`);
    this.app.render();
  }
}
