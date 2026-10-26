import test from "node:test";
import assert from "node:assert/strict";
import { EditModel } from "../../ckdoffline/web/editor-model.js";

const hex = (s) => Buffer.from(s, "latin1").toString("hex");

function fb(texts, { lrecl = 80, recfm = "FB" } = {}) {
  const lines = texts.map((t) => t.padEnd(lrecl));
  return new EditModel({ recfm, lrecl, lines, raw: lines.map(hex) });
}
function vb(texts, lrecl = 84) {
  return new EditModel({ recfm: "VB", lrecl, lines: texts, raw: texts.map(hex) });
}
const L = (m) => m.lines.map((s) => s.trimEnd());
const abc = () => fb(["A", "B", "C", "D", "E", "F"]);
const numbered = (texts) =>
  fb(texts.map((t, i) => t.padEnd(72) + String((i + 1) * 100).padStart(8, "0")));

test("width: FB = LRECL, V = LRECL-4", () => {
  assert.equal(fb(["x"]).width, 80);
  assert.equal(vb(["x"], 84).width, 80);
  assert.equal(fb(["x"], { lrecl: 100 }).width, 100);
});

test("untouched lines keep raw", () => {
  const m = fb(["ONE", "TWO", "THREE"]);
  m.setLine(1, "TWO!");
  const s = m.toSave();
  assert.deepEqual(s[0], { raw: hex("ONE".padEnd(80)) });
  assert.equal(typeof s[1], "string");
  assert.equal(s[1].trimEnd(), "TWO!");
  assert.deepEqual(s[2], { raw: hex("THREE".padEnd(80)) });
});

test("setLine with identical text keeps the line raw", () => {
  const m = fb(["ONE"]);
  m.setLine(0, "ONE");
  m.setLine(0, "ONE".padEnd(80));
  assert.ok(typeof m.toSave()[0] === "object");
  assert.equal(m.modified, false);
});

test("V lines save as plain strings when touched", () => {
  const m = vb(["a", "bb"]);
  m.setLine(0, "aaa");
  assert.deepEqual(m.toSave(), ["aaa", { raw: hex("bb") }]);
});

test("toSave throws with .line when too long", () => {
  const m = fb(["a", "b"]);
  m.setLine(1, "x".repeat(81));
  assert.throws(() => m.toSave(), (e) => e.line === 2);
  const v = vb(["a"], 84);
  v.setLine(0, "x".repeat(81));
  assert.throws(() => v.toSave(), (e) => e.line === 1);
  v.setLine(0, "x".repeat(80));
  assert.deepEqual(v.toSave(), ["x".repeat(80)]);
});

test("setLine out of range throws", () => {
  assert.throws(() => abc().setLine(9, "x"), RangeError);
});

// ---- CAPS ----
test("CAPS defaults on without lowercase, off with lowercase", () => {
  assert.equal(fb(["HELLO", "WORLD"]).caps, true);
  assert.equal(fb(["HELLO", "world"]).caps, false);
  assert.equal(fb([]).caps, true);
});

test("CAPS ON uppercases only the changed characters", () => {
  const m = fb(["hello world"]);
  m.primary("CAPS ON");
  m.setLine(0, "hello brave world");
  assert.equal(m.getLine(0).trimEnd(), "hello BRAVE world");
  m.primary("CAPS OFF");
  m.setLine(0, "hello brave world!");
  assert.equal(m.getLine(0).trimEnd(), "hello brave world!"); // caps off: typed text is kept as typed
});

test("CAPS ON uppercases a fully new line", () => {
  const m = fb(["AAA"]);
  m.setLine(0, "abc");
  assert.equal(m.getLine(0).trimEnd(), "ABC");
});

// ---- I ----
test("I and In insert blank lines", () => {
  const m = abc();
  m.applyLineCommands({ 1: "I" });
  assert.deepEqual(L(m), ["A", "B", "", "C", "D", "E", "F"]);
  assert.equal(m.lines[2].length, 80);
  const n = abc();
  n.applyLineCommands({ 0: "i3" });
  assert.deepEqual(L(n), ["A", "", "", "", "B", "C", "D", "E", "F"]);
});

test("unused I lines vanish on the next Enter and never count as modified", () => {
  const m = abc();
  m.applyLineCommands({ 1: "I" });
  assert.equal(m.lineCount, 7);
  assert.equal(m.modified, false);
  m.applyLineCommands({});
  assert.equal(m.lineCount, 6);
  m.applyLineCommands({});
  assert.deepEqual(L(m), ["A", "B", "C", "D", "E", "F"]);
  assert.equal(m.modified, false);
});

test("I3, typing into one keeps only that line", () => {
  const m = abc();
  m.applyLineCommands({ 0: "I3" });
  m.setLine(2, "new");
  m.applyLineCommands({});
  assert.deepEqual(L(m), ["A", "NEW", "B", "C", "D", "E", "F"]);
  assert.equal(m.modified, true);
});

test("toSave drops unused input lines; typing keeps a line", () => {
  const m = abc();
  m.applyLineCommands({ 0: "I2" });
  m.setLine(1, "x");
  const s = m.toSave();
  assert.equal(s.length, 7);
  assert.equal(s[1].trimEnd(), "X");
  assert.equal(m.lineCount, 8); // toSave does not mutate
});

test("a command on an unused input line keeps it for that Enter", () => {
  const m = abc();
  m.applyLineCommands({ 0: "I" });
  m.applyLineCommands({ 1: "R" });
  assert.deepEqual(L(m), ["A", "", "", "B", "C", "D", "E", "F"]);
});

test("markSaved clears modified", () => {
  const m = abc();
  m.setLine(0, "Z");
  assert.equal(m.modified, true);
  m.markSaved();
  assert.equal(m.modified, false);
});

test("I on TOP OF DATA (-1) inserts at the top; V inserts empty lines", () => {
  const m = vb(["a"]);
  m.applyLineCommands({ "-1": "I2" });
  assert.deepEqual(m.lines, ["", "", "a"]);
});

test("inserted lines are sent as strings", () => {
  const m = fb(["A"]);
  m.applyLineCommands({ 0: "I" });
  m.setLine(1, "typed");
  const s = m.toSave();
  assert.deepEqual(s[0], { raw: hex("A".padEnd(80)) });
  assert.equal(s[1], "TYPED".padEnd(80));
});

// ---- D ----
test("D and Dn delete", () => {
  const m = abc();
  m.applyLineCommands({ 1: "D" });
  assert.deepEqual(L(m), ["A", "C", "D", "E", "F"]);
  m.applyLineCommands({ 1: "D2" });
  assert.deepEqual(L(m), ["A", "E", "F"]);
  m.applyLineCommands({ 1: "D9" });
  assert.deepEqual(L(m), ["A"]);
});

test("DD block across Enter", () => {
  const m = abc();
  let r = m.applyLineCommands({ 1: "DD" });
  assert.equal(r.pending, "DD");
  assert.match(r.message, /Block command incomplete/);
  assert.deepEqual(L(m), ["A", "B", "C", "D", "E", "F"]);
  assert.deepEqual(m.pendingMarks, { 1: "DD" });
  r = m.applyLineCommands({ 3: "DD" });
  assert.equal(r.pending, undefined);
  assert.deepEqual(L(m), ["A", "E", "F"]);
});

test("DD..DD in one Enter, and in either order", () => {
  const m = abc();
  m.applyLineCommands({ 4: "DD", 2: "DD" });
  assert.deepEqual(L(m), ["A", "B", "F"]);
});

test("pending block survives other line commands and re-submitted marks", () => {
  const m = abc();
  m.applyLineCommands({ 1: "DD" });
  m.applyLineCommands({ 5: "I" });
  m.setLine(6, "x");
  assert.equal(m.pendingMarks[1], "DD");
  m.applyLineCommands({ 1: "DD" }); // screen re-submits the pending mark
  assert.ok(m.pendingMarks[1]);
  m.applyLineCommands({ 2: "DD" });
  assert.deepEqual(L(m), ["A", "D", "E", "F", "X"]);
});

test("RESET clears pending commands", () => {
  const m = abc();
  m.applyLineCommands({ 1: "DD" });
  m.primary("RESET");
  assert.deepEqual(m.pendingMarks, {});
  m.applyLineCommands({ 3: "DD" });
  assert.deepEqual(L(m), ["A", "B", "C", "D", "E", "F"]);
});

// ---- R ----
test("R, Rn and RR repeat", () => {
  const m = abc();
  m.applyLineCommands({ 0: "R" });
  assert.deepEqual(L(m), ["A", "A", "B", "C", "D", "E", "F"]);
  m.applyLineCommands({ 2: "R2" });
  assert.deepEqual(L(m), ["A", "A", "B", "B", "B", "C", "D", "E", "F"]);
});

test("RR..RR repeats a block, RRn repeats it n times", () => {
  const m = abc();
  m.applyLineCommands({ 0: "RR", 1: "RR" });
  assert.deepEqual(L(m), ["A", "B", "A", "B", "C", "D", "E", "F"]);
  const n = abc();
  n.applyLineCommands({ 1: "RR2", 2: "RR" });
  assert.deepEqual(L(n), ["A", "B", "C", "B", "C", "B", "C", "D", "E", "F"]);
});

test("repeated lines keep raw but are independent objects", () => {
  const m = fb(["A"]);
  m.applyLineCommands({ 0: "R" });
  m.setLine(1, "Z");
  const s = m.toSave();
  assert.deepEqual(s[0], { raw: hex("A".padEnd(80)) });
  assert.equal(s[1].trimEnd(), "Z");
});

// ---- C / M ----
test("C with A target", () => {
  const m = abc();
  const r = m.applyLineCommands({ 0: "C", 3: "A" });
  assert.deepEqual(L(m), ["A", "B", "C", "D", "A", "E", "F"]);
  assert.equal(r.pending, undefined);
});

test("C with B target, across Enter (copy pending)", () => {
  const m = abc();
  let r = m.applyLineCommands({ 0: "C" });
  assert.equal(r.pending, "C");
  assert.match(r.message, /MOVE\/COPY is pending/);
  r = m.applyLineCommands({ 3: "B" });
  assert.deepEqual(L(m), ["A", "B", "C", "A", "D", "E", "F"]);
  assert.equal(r.pending, undefined);
  assert.deepEqual(m.pendingMarks, {});
});

test("Cn copies n lines; M with A moves", () => {
  const m = abc();
  m.applyLineCommands({ 0: "C2", 5: "A" });
  assert.deepEqual(L(m), ["A", "B", "C", "D", "E", "F", "A", "B"]);
  const n = abc();
  n.applyLineCommands({ 0: "M2", 5: "A" });
  assert.deepEqual(L(n), ["C", "D", "E", "F", "A", "B"]);
});

test("M to top with B on first line or A on TOP", () => {
  const m = abc();
  m.applyLineCommands({ 4: "M", 0: "B" });
  assert.deepEqual(L(m), ["E", "A", "B", "C", "D", "F"]);
  const n = abc();
  n.applyLineCommands({ 4: "M", "-1": "A" });
  assert.deepEqual(L(n), ["E", "A", "B", "C", "D", "F"]);
});

test("CC..CC and MM..MM with targets, block pending across Enter", () => {
  const m = abc();
  m.applyLineCommands({ 1: "CC" });
  m.applyLineCommands({ 2: "CC" });
  assert.equal(m.pendingMarks[1], "CC");
  m.applyLineCommands({ 5: "A" });
  assert.deepEqual(L(m), ["A", "B", "C", "D", "E", "F", "B", "C"]);
  const n = abc();
  n.applyLineCommands({ 0: "MM", 1: "MM", 4: "B" });
  assert.deepEqual(L(n), ["C", "D", "A", "B", "E", "F"]);
});

test("moved lines keep raw", () => {
  const m = fb(["A", "B"]);
  m.applyLineCommands({ 0: "M", 1: "A" });
  assert.deepEqual(m.toSave(), [{ raw: hex("B".padEnd(80)) }, { raw: hex("A".padEnd(80)) }]);
});

test("target without source is an error and changes nothing", () => {
  const m = abc();
  const r = m.applyLineCommands({ 2: "A" });
  assert.equal(r.error, true);
  assert.deepEqual(L(m), ["A", "B", "C", "D", "E", "F"]);
});

test("conflicting commands apply nothing", () => {
  const m = abc();
  for (const cmds of [
    { 0: "C", 1: "M", 5: "A" },
    { 0: "D3", 1: "I" },
    { 0: "DD", 2: "R", 3: "DD" },
    { 0: "C", 3: "A", 4: "B" },
    { 0: "C2", 1: "A" },
    { 0: "CC", 1: "MM" },
  ]) {
    const r = m.applyLineCommands(cmds);
    assert.equal(r.error, true, JSON.stringify(cmds));
    assert.deepEqual(L(m), ["A", "B", "C", "D", "E", "F"]);
    assert.deepEqual(m.pendingMarks, {});
  }
});

test("invalid line commands apply nothing", () => {
  const m = abc();
  for (const cmds of [{ 0: "X9" }, { 1: "A2" }, { 0: "D0" }, { 99: "D" }, { "-1": "D" }]) {
    assert.equal(m.applyLineCommands(cmds).error, true);
  }
  assert.equal(m.lineCount, 6);
});

test("an error leaves earlier pending state intact", () => {
  const m = abc();
  m.applyLineCommands({ 0: "C" });
  const r = m.applyLineCommands({ 1: "D", 2: "X" });
  assert.equal(r.error, true);
  assert.equal(r.pending, "C");
  m.applyLineCommands({ 5: "A" });
  assert.deepEqual(L(m), ["A", "B", "C", "D", "E", "F", "A"]);
});

test("target inside the source is an error", () => {
  const m = abc();
  assert.equal(m.applyLineCommands({ 0: "CC", 3: "CC", 2: "A" }).error, true);
  assert.deepEqual(L(m), ["A", "B", "C", "D", "E", "F"]);
});

test("empty and blank commands are ignored", () => {
  const m = abc();
  assert.deepEqual(m.applyLineCommands({ 0: "", 1: "  " }), {});
});

// ---- FIND ----
test("FIND is case-insensitive, quoted strings with spaces, NEXT/RFIND", () => {
  const m = fb(["alpha", "Beta gamma", "gamma beta"]);
  let r = m.primary("FIND 'beta gamma'");
  assert.deepEqual(r.cursor, { line: 1, col: 0, len: 10 });
  r = m.primary("FIND gamma");
  assert.deepEqual(m.cursor, { line: 2, col: 0, len: 5 });
  r = m.primary("RFIND");
  assert.match(r.message, /Bottom of data reached/);
  r = m.primary("RFIND");
  assert.deepEqual(m.cursor, { line: 1, col: 5, len: 5 });
});

test("FIND finds several hits in a line, then wraps", () => {
  const m = fb(["abab"]);
  m.primary("F ab");
  assert.equal(m.cursor.col, 0);
  m.primary("RFIND");
  assert.equal(m.cursor.col, 2);
  assert.match(m.primary("RFIND").message, /Bottom/);
  m.primary("RFIND");
  assert.equal(m.cursor.col, 0);
});

test("FIND not found, case-sensitive C'' and FIRST/LAST/PREV/ALL", () => {
  const m = fb(["one", "Two", "two", "three two"]);
  assert.match(m.primary("FIND zzz").message, /not found/);
  assert.match(m.primary("FIND c'two'").message, /found/);
  assert.equal(m.cursor.line, 2);
  m.primary("FIND two FIRST");
  assert.equal(m.cursor.line, 1);
  m.primary("FIND two LAST");
  assert.deepEqual(m.cursor, { line: 3, col: 6, len: 3 });
  m.primary("FIND two PREV");
  assert.equal(m.cursor.line, 2);
  const r = m.primary("FIND two ALL");
  assert.match(r.message, /found 3 time/);
  assert.equal(m.cursor.line, 1);
  assert.match(m.primary("FIND two PREV").message, /Top of data reached/);
});

test("FIND WORD does not match inside a word", () => {
  const m = fb(["cathedral cat"]);
  m.primary("FIND cat WORD");
  assert.equal(m.cursor.col, 10);
});

test("FIND searches only the data area under NUM ON", () => {
  const m = numbered(["abc"]);
  assert.equal(m.numOn, true);
  assert.match(m.primary("FIND 00000100").message, /not found/);
});

test("RFIND without a previous FIND", () => {
  assert.match(abc().primary("RFIND").message, /No previous/);
});

// ---- CHANGE ----
test("CHANGE ALL counts", () => {
  const m = fb(["foo bar foo", "no", "FOO"]);
  const r = m.primary("CHANGE foo 'x y' ALL");
  assert.equal(r.message, "CHG 3");
  assert.deepEqual(L(m), ["x y bar x y", "no", "x y"]);
  assert.equal(m.lines[0].length, 80);
  const s = m.toSave();
  assert.equal(typeof s[0], "string");
  assert.deepEqual(s[1], { raw: hex("no".padEnd(80)) });
});

test("CHANGE without ALL changes one occurrence, RCHANGE continues", () => {
  const m = fb(["a a", "a"]);
  assert.equal(m.primary("C a b").message, "CHG 1");
  assert.deepEqual(L(m), ["b a", "a"]);
  m.primary("RCHANGE");
  assert.deepEqual(L(m), ["b b", "a"]);
  m.primary("RCHANGE");
  assert.deepEqual(L(m), ["b b", "b"]);
  assert.match(m.primary("RCHANGE").message, /Bottom of data reached/);
  assert.match(m.primary("RCHANGE").message, /not found/);
});

test("CHANGE to empty string deletes text; replacement containing the search does not loop", () => {
  const m = fb(["a-b"]);
  m.primary("CHANGE '-' ''");
  assert.equal(m.getLine(0).trimEnd(), "ab");
  const n = fb(["a a"]);
  assert.equal(n.primary("CHANGE a aa ALL").message, "CHG 2");
  assert.equal(n.getLine(0).trimEnd(), "aa aa");
});

test("CHANGE that would truncate a line is skipped and reported", () => {
  const m = fb(["x".repeat(80), "x"]);
  const r = m.primary("CHANGE x yy ALL");
  assert.equal(r.message, "CHG 1; 80 not changed, line too long");
  assert.equal(m.getLine(0), "x".repeat(80));
  assert.equal(m.getLine(1).trimEnd(), "yy");
});

test("CHANGE may eat trailing blanks on FB", () => {
  const m = fb(["a" + " ".repeat(78) + "b"]);
  assert.match(m.primary("C a aa").message, /not changed/);
  const n = fb(["abc"]);
  assert.equal(n.primary("C a aa").message, "CHG 1");
  assert.equal(n.getLine(0), "aabc".padEnd(80));
});

test("CHANGE case-insensitive search, C'' case-sensitive", () => {
  const m = fb(["Abc abc"]);
  m.primary("CHANGE c'abc' X ALL");
  assert.equal(m.getLine(0).trimEnd(), "Abc X");
  m.primary("CHANGE ABC Y ALL");
  assert.equal(m.getLine(0).trimEnd(), "Y X");
});

test("CHANGE on V respects width", () => {
  const m = vb(["a".repeat(79)], 84);
  assert.match(m.primary("C a bb ALL").message, /not changed/);
  assert.equal(m.getLine(0), "a".repeat(79));
});

test("CHANGE needs two operands", () => {
  assert.match(abc().primary("CHANGE a").message, /CHANGE needs/);
});

// ---- NUM ----
test("NUM ON renumbers 73-80", () => {
  const m = numbered(["one", "two", "three"]);
  assert.equal(m.numOn, true);
  assert.equal(m.dataWidth, 72);
  assert.equal(m.getLine(0).length, 72);
  // untouched, numbers unchanged: raw
  assert.ok(m.toSave().every((x) => typeof x === "object"));
  m.applyLineCommands({ 0: "I" });
  m.setLine(1, "ins");
  const s = m.toSave();
  assert.deepEqual(s[0], { raw: hex("one".padEnd(72) + "00000100") });
  assert.equal(s[1], "ins".padEnd(72) + "00000200");
  // renumber-only lines go out as raw with columns 73-80 patched
  assert.equal(typeof s[2], "object");
  assert.ok(s[2].raw.endsWith("f0f0f0f0f0f3f0f0"));
  assert.equal(s[2].raw.length, 160);
});

test("NUM ON: deleting a line renumbers only the later lines", () => {
  const m = numbered(["a", "b", "c", "d"]);
  m.applyLineCommands({ 1: "D" });
  const s = m.toSave();
  assert.deepEqual(s[0], { raw: hex("a".padEnd(72) + "00000100") });
  assert.deepEqual(s[1], { raw: hex("c".padEnd(72)) + "f0f0f0f0f0f2f0f0" });
  assert.deepEqual(s[2], { raw: hex("d".padEnd(72)) + "f0f0f0f0f0f3f0f0" });
});

test("NUM detection: only when every line has numeric 73-80", () => {
  const good = numbered(["a", "b"]);
  assert.equal(good.numOn, true);
  const bad = fb(["a".padEnd(72) + "00000100", "b"]);
  assert.equal(bad.numOn, false);
  assert.equal(fb([]).numOn, false);
  assert.equal(fb(["a".padEnd(72) + "00000100"], { lrecl: 100 }).numOn, false);
});

test("NUM ON command: FB 80 only; V and other LRECL are ignored with a message", () => {
  const v = vb(["a"]);
  assert.match(v.primary("NUM ON").message, /only supported/);
  assert.equal(v.numOn, false);
  const w = fb(["a"], { lrecl: 100 });
  assert.match(w.primary("NUM ON").message, /only supported/);
  const m = fb(["a", "b"]);
  assert.equal(m.primary("NUM ON").message, "Columns 73-80 will be renumbered on save");
  assert.equal(m.numOn, true);
  const s = m.toSave();
  assert.deepEqual(s[0], { raw: hex("a".padEnd(72)) + "f0f0f0f0f0f1f0f0" });
  assert.deepEqual(s[1], { raw: hex("b".padEnd(72)) + "f0f0f0f0f0f2f0f0" });
});

test("NUM OFF folds columns 73-80 back into the data", () => {
  const m = numbered(["a"]);
  m.primary("NUM OFF");
  assert.equal(m.numOn, false);
  assert.equal(m.getLine(0), "a".padEnd(72) + "00000100");
  assert.deepEqual(m.toSave(), [{ raw: hex("a".padEnd(72) + "00000100") }]);
});

test("NUM ON: typing past column 72 is rejected by toSave", () => {
  const m = numbered(["a"]);
  m.setLine(0, "x".repeat(73));
  assert.throws(() => m.toSave(), (e) => e.line === 1);
});

test("NUM ON: typed line keeps new sequence number", () => {
  const m = numbered(["a", "b"]);
  m.setLine(1, "bb");
  assert.equal(m.toSave()[1], "bb".padEnd(72) + "00000200");
});

// ---- primary misc ----
test("SAVE / END / CANCEL return actions", () => {
  const m = abc();
  assert.equal(m.primary("SAVE").action, "save");
  assert.equal(m.primary("end").action, "end");
  assert.equal(m.primary("CANCEL").action, "cancel");
});

test("HEX and CAPS toggles; bad operands and unknown commands report", () => {
  const m = abc();
  m.primary("HEX ON");
  assert.equal(m.hex, true);
  m.primary("HEX OFF");
  assert.equal(m.hex, false);
  m.primary("CAPS OFF");
  assert.equal(m.caps, false);
  assert.match(m.primary("CAPS MAYBE").message, /Invalid/);
  assert.match(m.primary("FROB").message, /Invalid primary command/);
  assert.deepEqual(m.primary(""), {});
});

test("modified flag", () => {
  const m = abc();
  assert.equal(m.modified, false);
  m.applyLineCommands({ 0: "D" });
  assert.equal(m.modified, true);
});

test("NUM ON: renumbered line with unmappable byte round-trips via patched raw", () => {
  // EBCDIC record: byte 5 (col 5) is 0x00, blanks 0x40, old seq 00000100
  const rec = "c1c2c3c400" + "40".repeat(67) + "f0f0f0f0f0f1f0f0";
  const rec2 = "c5".repeat(72) + "f0f0f0f0f0f2f0f0";
  const text = (n) => "ABCD\u0000".padEnd(72) + n;
  const m = new EditModel({
    recfm: "FB", lrecl: 80,
    lines: [text("00000100"), "E".repeat(72) + "00000200"],
    raw: [rec, rec2],
  });
  assert.equal(m.numOn, true);
  m.applyLineCommands({ "-1": "I" }); // both lines shift down one number
  m.setLine(0, "top");
  const s = m.toSave();
  assert.equal(s[0], "TOP".padEnd(72) + "00000100");
  assert.equal(s[1].raw, "c1c2c3c400" + "40".repeat(67) + "f0f0f0f0f0f2f0f0");
  assert.equal(s[1].raw.slice(8, 10), "00"); // byte 5 unchanged
  assert.equal(s[2].raw, rec2.slice(0, 144) + "f0f0f0f0f0f3f0f0");
});

test("NUM ON: an edited line is sent as str even if renumbered", () => {
  const m = numbered(["a", "b"]);
  m.applyLineCommands({ 0: "I" });
  m.setLine(1, "ins");
  m.setLine(2, "bb");
  assert.equal(m.toSave()[2], "bb".padEnd(72) + "00000300");
});

// ---- fix round 1 ----
test("FIND then CHANGE / RCHANGE changes the found occurrence (cursor inclusive)", () => {
  const m = fb(["x one", "two x", "x"]);
  m.primary("FIND x");
  m.primary("FIND x"); // cursor on line 1 col 4
  assert.deepEqual(m.cursor, { line: 1, col: 4, len: 1 });
  assert.equal(m.primary("CHANGE x y").message, "CHG 1");
  assert.deepEqual(L(m), ["x one", "two y", "x"]);
  assert.deepEqual(m.cursor, { line: 1, col: 5, len: 0 }); // moved past replacement
  m.primary("FIND x");
  assert.equal(m.cursor.line, 2);
  m.primary("RCHANGE");
  assert.deepEqual(L(m), ["x one", "two y", "y"]);
});

test("RCHANGE after FIND replaces the highlighted hit", () => {
  const m = fb(["a1 a2 a3"]);
  m.primary("CHANGE a b");
  m.primary("FIND a");
  assert.equal(m.cursor.col, 3);
  m.primary("RCHANGE");
  assert.equal(m.getLine(0).trimEnd(), "b1 b2 a3");
});

test("CHANGE with replacement containing the search string does not re-change", () => {
  const m = fb(["a a"]);
  m.primary("C a aa");
  m.primary("RCHANGE");
  assert.equal(m.getLine(0).trimEnd(), "aa aa");
});

test("a skipped (too long) CHANGE advances the cursor so RCHANGE moves on", () => {
  const m = fb(["x".repeat(80), "x"]);
  const r = m.primary("CHANGE x yy");
  assert.match(r.message, /not changed/);
  assert.deepEqual(m.cursor, { line: 0, col: 1, len: 0 });
  assert.equal(m.getLine(0), "x".repeat(80));
});

test("CAPS: retyping identical text keeps the line raw; V padding is ignored", () => {
  const m = fb(["abc DEF"]);
  m.primary("CAPS ON");
  m.setLine(0, "abc DEF");
  assert.equal(m.modified, false);
  const v = vb(["abc"]);
  v.setLine(0, "abc   ");
  assert.equal(v.modified, false);
  assert.deepEqual(v.toSave(), [{ raw: hex("abc") }]);
});

test("B on TOP OF DATA is an error", () => {
  const m = abc();
  m.applyLineCommands({ 0: "C" });
  const r = m.applyLineCommands({ "-1": "B" });
  assert.equal(r.error, true);
  assert.match(r.message, /TOP OF DATA/);
  assert.equal(r.pending, "C");
});

test("pending M source is protected from a D covering it", () => {
  const m = abc();
  m.applyLineCommands({ 1: "M" });
  const r = m.applyLineCommands({ 0: "D3" });
  assert.equal(r.error, true);
  assert.deepEqual(L(m), ["A", "B", "C", "D", "E", "F"]);
});

test("a C-copied line keeps raw", () => {
  const m = fb(["A", "B"]);
  m.applyLineCommands({ 0: "C", 1: "A" });
  assert.deepEqual(m.toSave(), [
    { raw: hex("A".padEnd(80)) }, { raw: hex("B".padEnd(80)) }, { raw: hex("A".padEnd(80)) },
  ]);
});

test("CHANGE under NUM ON leaves 73-80 alone; lengthening at column 72 is skipped", () => {
  const m = numbered(["x".padEnd(71, " ") + "x", "x"]);
  const r = m.primary("CHANGE x yy ALL");
  assert.equal(r.message, "CHG 1; 2 not changed, line too long");
  const s = m.toSave();
  assert.deepEqual(s[0], { raw: hex("x".padEnd(71) + "x" + "00000100") });
  assert.equal(s[1], "yy".padEnd(72) + "00000200");
});

test("isInputLine: inserted lines until typed into", () => {
  const m = fb(["A", "B"]);
  m.applyLineCommands({ 0: "I2" });
  assert.deepEqual([0, 1, 2, 3].map((i) => m.isInputLine(i)), [false, true, true, false]);
  m.setLine(1, "typed");
  assert.equal(m.isInputLine(1), false);
  assert.equal(m.isInputLine(2), true);
});

test("toSave records the model index of every line it sends", () => {
  const m = fb(["A", "B", "C"]);
  m.applyLineCommands({ 0: "I2" }); // two unused input lines after A
  m.setLine(2, "typed");            // the second input line is used
  const out = m.toSave();
  assert.equal(out.length, 4);
  assert.deepEqual(m.savedIndices, [0, 2, 3, 4]);
  assert.equal(m.modelIndex(2), 2); // server line 2 (1-based) -> model index 2
  assert.equal(m.modelIndex(3), 3);
  assert.equal(m.modelIndex(99), null);
});
