import test from "node:test";
import assert from "node:assert/strict";
import {
  lineCommand, displayText, mergeEdited, vhex, scrollAmount, memberNameError, fitFixed,
  pad, hexDigitsOk,
} from "../../ckdoffline/web/panel.js";
import { decodeByte, encodeChar } from "../../ckdoffline/web/ebcdic.js";

const SCREENS = ["open", "dslist", "memlist", "edit", "browse", "hex"];

test("every screen module imports without a DOM", async () => {
  assert.equal(typeof globalThis.document, "undefined");
  for (const name of SCREENS) {
    const mod = await import(`../../ckdoffline/web/screens/${name}.js`);
    assert.ok(Object.keys(mod).length > 0, `${name}.js exports nothing`);
  }
  await import("../../ckdoffline/web/app.js");
});

test("lineCommand: typed characters over the sequence number", () => {
  assert.equal(lineCommand("000100", "000100"), "");
  assert.equal(lineCommand("000100", "D00100"), "D");
  assert.equal(lineCommand("000100", "i50100"), "I5");
  assert.equal(lineCommand("000100", "DD0100"), "DD");
  assert.equal(lineCommand("000100", "d"), "D"); // rest erased
  assert.equal(lineCommand("000100", "  C   "), "C");
  assert.equal(lineCommand("******", "I*****"), "I");
  assert.equal(lineCommand("''''''", "I3''''"), "I3");
});

test("displayText shows unprintable characters as dots", () => {
  assert.equal(displayText("A\u0000B\u0085C­D"), "A.B.C.D");
  assert.equal(displayText("plain"), "plain");
});

test("mergeEdited keeps unprintable bytes the user did not overtype", () => {
  const orig = "A\u0000BC";
  const shown = displayText(orig);
  assert.equal(mergeEdited(orig, shown, shown), orig);
  assert.equal(mergeEdited(orig, shown, "X.BC"), "X\u0000BC");
  assert.equal(mergeEdited(orig, shown, "A_BC"), "A_BC");
  // length changed (insert/delete): the screen text wins
  assert.equal(mergeEdited(orig, shown, "A.B"), "A.B");
  // padded display beyond the original (VB): padding stays blank
  assert.equal(mergeEdited("AB", "AB  ", "AB X"), "AB X");
});

test("fitFixed drops blank overflow and keeps real overflow", () => {
  assert.equal(fitFixed("AB  ", 3), "AB ");
  assert.equal(fitFixed("ABCD", 3), "ABCD");
  assert.equal(fitFixed("A", 3), "A  ");
});

test("vhex: ISPF vertical hex of EBCDIC bytes", () => {
  const [hi, lo] = vhex("A1 ", "1047");
  assert.equal(hi, "CF4");
  assert.equal(lo, "110");
  // characters outside the code page show as ??
  const [h2, l2] = vhex("€", "1047");
  assert.equal(h2 + l2, "??");
});

test("scrollAmount: CSR / PAGE / HALF / n / MAX", () => {
  assert.equal(scrollAmount("PAGE", 20, null), 20);
  assert.equal(scrollAmount("HALF", 20, null), 10);
  assert.equal(scrollAmount("15", 20, null), 15);
  assert.equal(scrollAmount("CSR", 20, 5), 5);
  assert.equal(scrollAmount("CSR", 20, 0), 20); // cursor already at the edge: a page
  assert.equal(scrollAmount("CSR", 20, null), 20);
  assert.equal(scrollAmount("MAX", 20, null), Infinity);
  assert.equal(scrollAmount("M", 20, null), Infinity);
  assert.equal(scrollAmount("junk", 20, null), null);
});

test("memberNameError mirrors the server's rules", () => {
  assert.equal(memberNameError("NEWMEM"), null);
  assert.equal(memberNameError("@A#$9"), null);
  assert.match(memberNameError("TOOLONGXX"), /8/);
  assert.match(memberNameError("1ABC"), /digit|invalid/i);
  assert.match(memberNameError("A.B"), /invalid/i);
  assert.match(memberNameError(""), /name/i);
});

test("small formatters", () => {
  assert.equal(pad(42, 5), "00042");
  assert.equal(hexDigitsOk("00aF19"), true);
  assert.equal(hexDigitsOk("0g"), false);
  assert.equal(decodeByte(0xc1, "1047"), "A");
  assert.equal(encodeChar("[", "1047"), 0xad);
  assert.equal(encodeChar("[", "037"), 0xba);
});

test("EDIT collect: untouched lines stay raw, unchanged fields never reach setLine", async () => {
  const { EditScreen, screenLine } = await import("../../ckdoffline/web/screens/edit.js");
  const { EditModel } = await import("../../ckdoffline/web/editor-model.js");
  const hexOf = (s) => Buffer.from(s, "latin1").toString("hex");
  // line 0 has an unmappable byte (X'00' decodes to U+0000), line 1 is plain
  const lines = ["AB\u0000CD".padEnd(80), "PLAIN".padEnd(80), "EDIT\u0000ME".padEnd(80)];
  const model = new EditModel({ recfm: "FB", lrecl: 80, lines, raw: lines.map(hexOf) });
  const scr = new EditScreen({ codepage: "1047" }, { ds: { dsname: "X.Y", recfm_str: "FB", lrecl: 80 }, member: "M" });
  scr.model = model;
  scr.left = 0;
  scr.winW = 80;
  scr.prefixes = [];
  scr.datas = [0, 1, 2].map((v) => {
    const d = screenLine(model, v, 0, 80);
    return { ...d, v, f: { value: d.shown } };
  });
  assert.equal(scr.datas[0].shown.slice(0, 5), "AB.CD"); // shown with a dot
  // the user overtypes "EDIT" with "XDIT" on line 2 only (the dot stays as displayed)
  scr.datas[2].f.value = "X" + scr.datas[2].shown.slice(1);
  const calls = [];
  const orig = model.setLine.bind(model);
  model.setLine = (i, t) => { calls.push(i); orig(i, t); };
  scr.collect();
  assert.deepEqual(calls, [2]);
  const out = model.toSave();
  assert.deepEqual(out[0], { raw: hexOf(lines[0]) });
  assert.deepEqual(out[1], { raw: hexOf(lines[1]) });
  assert.equal(out[2], "XDIT\u0000ME".padEnd(80)); // the X'00' byte survived the edit
});
