// DSLIST (like ISPF 3.4): every data set on the volume, with line commands
// E edit, B browse, M member list, I info, Z compress; primary LOCATE / L prefix.

import { row, field, ConfirmScreen, scrollAmount, pad, dotted, volumeSummary } from "../panel.js";
import { MemlistScreen, screenFor } from "./memlist.js";

export class DslistScreen {
  constructor(app) {
    this.app = app;
    this.top = 0;
    this.status = {}; // dsname -> row message (*Edited, *Compressed, ...)
    this.typed = {}; // dsname -> line command kept after an error
    this.scroll = "PAGE";
  }

  async load() {
    this.dss = await this.app.get("/api/datasets");
  }

  get pageRows() {
    return this.app.bodyRows - 3;
  }

  render() {
    const a = this.app;
    if (this.initialMessage) {
      a.message(...this.initialMessage);
      this.initialMessage = null;
    }
    const dsw = Math.max(26, ...this.dss.map((d) => d.dsname.length));
    const head = `Cmd ${"Dsname".padEnd(dsw)} Org  RECFM LRECL BLKSZ Tracks %Used XT`;
    const cols = head.length + 12;
    const rows = [row([`Volume ${volumeSummary(a.info)}`, "info"]), row([head, "head"])];
    const n = this.pageRows;
    this.top = Math.max(0, Math.min(this.top, Math.max(0, this.dss.length - 1)));
    this.cmds = [];
    for (const d of this.dss.slice(this.top, this.top + n)) {
      const f = field(this.typed[d.dsname] || "", 3, { key: `c:${d.dsname}` });
      this.cmds.push({ f, d });
      const used = d.tracks_alloc ? Math.round((100 * d.tracks_used) / d.tracks_alloc) : 0;
      const attrs = ` ${d.dsorg_str.padEnd(4)} ${d.recfm_str.padEnd(5)} ${String(d.lrecl).padStart(5)} `
        + `${String(d.blksize).padStart(5)} ${String(d.tracks_alloc).padStart(6)} `
        + `${String(used).padStart(5)} ${String(d.extents.length).padStart(2)}`;
      rows.push(row(f, " ", [d.dsname.padEnd(dsw), d.editable ? "out" : "dim"], [attrs, "out"],
        [this.status[d.dsname] ? `  ${this.status[d.dsname]}` : "", "status-msg"]));
    }
    if (this.top + n >= this.dss.length) {
      rows.push(row(["*".repeat(27) + " End of Data Set list " + "*".repeat(27), "marker"]));
    }
    const right = `Row ${this.dss.length ? this.top + 1 : 0} of ${this.dss.length}`;
    a.frame({
      panel: "DSLIST", title: `Data Sets on volume ${a.info?.volser ?? ""}`, right, rows, cols,
      scroll: this.scroll, legend: "list",
    });
    if (!a.top.long) {
      // ISPF puts the line command help where the long message goes
      a.root.querySelector(".long").textContent = "Line commands: E Edit  B Browse  M Member list  I Info  Z Compress";
    }
    a.focusCommand();
  }

  async onKey(key) {
    const a = this.app;
    this.scroll = a.fields.scroll.value.trim() || "PAGE";
    const lineCmds = this.cmds.map(({ f, d }) => ({ c: f.value.trim().toUpperCase(), d })).filter((x) => x.c);
    const cmd = a.fields.cmd.value.trim();
    a.clearMessage();
    if (key === "pf3" || key === "pf12") return a.close();
    if (key === "pf7" || key === "pf8") {
      const amt = scrollAmount(cmd || this.scroll, this.pageRows, null);
      if (amt === null) { a.message("Invalid scroll amount"); return a.render(() => true); }
      const max = Math.max(0, this.dss.length - 1);
      this.top = key === "pf8" ? Math.min(max, this.top + amt) : Math.max(0, this.top - amt);
      if (Number.isNaN(this.top)) this.top = 0;
      return a.render((k) => k.startsWith("c:"));
    }
    if (key !== "enter") return;

    if (cmd) {
      const m = /^(?:L|LOC|LOCATE)\s+(\S+)$/i.exec(cmd);
      if (!m) {
        a.message("Invalid command", "Primary commands: LOCATE prefix (L prefix)");
        return a.render(() => true);
      }
      const p = m[1].toUpperCase();
      const i = this.dss.findIndex((d) => d.dsname >= p);
      this.top = i < 0 ? Math.max(0, this.dss.length - 1) : i;
      if (i < 0) a.message("Not found", `No data set at or after ${p}`);
      return a.render();
    }

    this.typed = {};
    this.epoch = a.epoch;
    for (let k = 0; k < lineCmds.length; k++) {
      const { c, d } = lineCmds[k];
      const ok = await this.lineCommand(c, d);
      if (this.stale()) {
        // another tab replaced the volume: the rest belongs to the old one; drop it
        this.typed = {};
        return;
      }
      if (!ok) {
        for (const r of lineCmds.slice(k)) this.typed[r.d.dsname] = r.c;
        break;
      }
    }
    await this.reload();
    a.render();
  }

  /** True when the volume changed (another tab) since the line commands started. */
  stale() {
    return this.epoch !== undefined && this.app.epoch !== this.epoch;
  }

  async reload() {
    try {
      this.dss = await this.app.get("/api/datasets");
    } catch (e) {
      this.app.error(e);
      return;
    }
    await this.refreshInfo(); // free tracks after the writes
  }

  /** Run one line command; false stops the rest (error shown). */
  async lineCommand(c, d) {
    const a = this.app;
    const po = d.dsorg_str === "PO";
    if (this.stale()) return false;
    try {
      switch (c) {
        case "E": case "B": {
          const mode = c === "E" ? "edit" : "browse";
          if (mode === "edit" && !d.editable) {
            a.message("Not editable", `${d.dsname}: ${d.reason || "cannot be edited"}`);
            return false;
          }
          if (po) {
            await a.run(new MemlistScreen(a, d, mode));
            return true;
          }
          if (d.dsorg_str !== "PS" && mode === "browse" && !d.editable) {
            a.message("Not supported", `${d.dsname}: ${d.reason || "cannot be browsed"}`);
            return false;
          }
          const r = await a.run(screenFor(a, d, null, mode));
          this.status[d.dsname] = r?.saved ? "*Saved" : mode === "edit" ? "*Edited" : "*Browsed";
          if (r?.message) a.message(r.message);
          return true;
        }
        case "M":
          if (!po) {
            a.message("Not partitioned", `${d.dsname} is ${d.dsorg_str}: it has no members`);
            return false;
          }
          if (!d.editable) {
            a.message("Not supported", `${d.dsname}: ${d.reason || "members cannot be listed"}`);
            return false;
          }
          await a.run(new MemlistScreen(a, d, "edit"));
          return true;
        case "I":
          await a.run(new InfoScreen(a, d));
          this.status[d.dsname] = "*Info";
          return true;
        case "Z": {
          if (!po || !d.editable) {
            a.message("Cannot compress", `${d.dsname}: only an editable partitioned data set can be compressed`);
            return false;
          }
          const ans = await a.run(new ConfirmScreen(a, {
            panel: "COMPRESS", title: "Confirm Compress",
            text: [`Compress ${d.dsname} in place?`,
              "Members are packed to the front of the data set and every TTR is updated."],
            choices: { Y: "compress the data set" },
          }));
          if (this.stale()) return false; // the volume changed while the prompt was up
          if (ans !== "Y") {
            a.message("Compress cancelled");
            return true;
          }
          const r = await a.post("/api/compress", { dsn: d.dsname });
          a.setInfo(r.info);
          this.status[d.dsname] = "*Compressed";
          a.message("Compressed", `${d.dsname}: ${r.members} directory entries; tracks used ${r.tracks_before} -> ${r.tracks_after}`);
          return true;
        }
        default:
          a.message("Invalid line command", `"${c}": use E, B, M, I or Z`);
          return false;
      }
    } catch (e) {
      a.error(e);
      this.status[d.dsname] = "*Failed";
      return false;
    }
  }

  async refreshInfo() {
    try {
      this.app.setInfo(await this.app.get("/api/info"));
    } catch {
      // the status line keeps the old values
    }
  }

  dirty() {
    return false;
  }
}

/** I: the decoded format-1 DSCB (GET /api/dscb), with every extent including format-3 ones. */
export class InfoScreen {
  constructor(app, ds) {
    this.app = app;
    this.ds = ds;
    this.top = 0;
  }

  async load() {
    this.d = await this.app.get("/api/dscb", { dsn: this.ds.dsname });
  }

  lines() {
    const d = this.d;
    const hx = (v, w = 4) => pad(v.toString(16).toUpperCase(), w);
    const line = (label, value) => row([`  ${dotted(label, 26)}: `, "prompt"], [String(value ?? ""), "out"]);
    const flags = (hex, list) => `X'${hex}'${list.length ? `  ${list.join(", ")}` : ""}`;
    const [c, h, r] = d.f1_cchhr;
    const used = this.ds.tracks_alloc ? Math.round((100 * d.tracks_used) / this.ds.tracks_alloc) : 0;
    const rows = [
      line("Data set name", d.dsname),
      line("Volume serial / sequence", `${d.volser} / ${d.volsq}`),
      line("Format-1 DSCB (CCHHR)", `${hx(c)} ${hx(h)} ${hx(r, 2)}`),
      line("Creation date", d.credt ?? "(none)"),
      line("Expiration date", d.expdt ?? "(none)"),
      line("Last referenced", d.refdt ?? "(none)"),
      line("Organization", `X'${d.dsorg}'  ${d.dsorg_str}`),
      line("Record format", `X'${d.recfm}'  ${d.recfm_str}`),
      line("Option code", `X'${d.optcd}'`),
      line("Block size", d.blksize),
      line("Record length", d.lrecl),
      line("Key length / RKP", `${d.keylen} / ${d.rkp}`),
      line("Indicators (DS1DSIND)", flags(d.dsind, d.dsind_flags)),
      line("SMS flags (DS1SMSFG)", flags(d.smsfg, d.smsfg_flags)),
      line("Secondary allocation", `${d.scalo.qty} ${d.scalo.units}  (X'${d.scalo.flags}')`),
      line("Last block (DS1LSTAR)", `TT ${d.lstar.tt}  R ${d.lstar.r}`),
      line("Track balance", d.trbal),
      line("Tracks used", `${d.tracks_used} of ${this.ds.tracks_alloc} (${used}%)`),
      line("Extents (DS1NOEPV)", `${d.noepv} (${d.extents.length} listed)`),
      line("Editable", this.ds.editable ? "YES" : `NO: ${this.ds.reason || ""}`),
      row(""),
      row(["   Ext DSCB  Type  Seq  Begin CCHH  End CCHH   Tracks   Relative tracks", "head"]),
    ];
    let rel = 0;
    d.extents.forEach((x, k) => {
      rows.push(row([`   ${String(k + 1).padStart(3)}  ${x.source}  X'${hx(x.type, 2)}' ${String(x.seq).padStart(4)}  `
        + `${hx(x.bcyl)} ${hx(x.bhead)}   ${hx(x.ecyl)} ${hx(x.ehead)}  ${String(x.tracks).padStart(6)}   ${rel}-${rel + x.tracks - 1}`, "out"]));
      rel += x.tracks;
    });
    return rows;
  }

  render() {
    const all = this.lines();
    const n = this.app.bodyRows;
    this.top = Math.max(0, Math.min(this.top, Math.max(0, all.length - n)));
    this.max = Math.max(0, all.length - n);
    this.app.frame({ panel: "INFO", title: "Data Set Information", right: this.d.dsname,
      rows: all.slice(this.top, this.top + n), scroll: "PAGE", legend: "list" });
    this.app.focusCommand();
  }

  onKey(key) {
    const a = this.app;
    if (key === "pf3" || key === "pf12" || key === "enter") return a.close();
    if (key === "pf7" || key === "pf8") {
      const amt = scrollAmount(a.fields.scroll.value || "PAGE", a.bodyRows, null) ?? a.bodyRows;
      this.top = key === "pf8" ? Math.min(this.max, this.top + amt) : Math.max(0, this.top - amt);
      a.render();
    }
  }
}
