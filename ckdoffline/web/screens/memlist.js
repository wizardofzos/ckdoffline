// Member list: members with ISPF statistics. Line commands S/E edit, B browse,
// D delete (confirmed), R rename (new name in the Prompt field); primary commands
// S name (edit, or create on save, as ISPF does) and L name.

import { row, field, ConfirmScreen, scrollAmount, memberNameError } from "../panel.js";
import { EditScreen } from "./edit.js";
import { BrowseScreen } from "./browse.js";
import { HexScreen } from "./hex.js";

/** The screen that edits or browses a data set or member, by RECFM. */
export function screenFor(app, ds, member, mode, isNew = false) {
  if (ds.recfm_str.startsWith("U")) {
    // a member of a load library, or a whole sequential RECFM U data set, is patched in place
    return new HexScreen(app, { ds, member, readOnly: mode !== "edit" || (!member && ds.dsorg_str !== "PS") });
  }
  if (mode === "edit") return new EditScreen(app, { ds, member, isNew });
  return new BrowseScreen(app, { ds, member });
}

function date(iso) {
  return iso ? iso.slice(0, 10).replaceAll("-", "/") : "";
}

function stamp(iso) {
  return iso ? `${date(iso)} ${iso.slice(11, 16)}` : "";
}

function num(v, w) {
  return v === null || v === undefined ? "".padStart(w) : String(v).padStart(w);
}

export class MemlistScreen {
  constructor(app, ds, mode) {
    this.app = app;
    this.ds = ds;
    this.mode = mode; // "edit" | "browse"
    this.top = 0;
    this.status = {}; // member -> *Edited, *Saved, *Deleted, ...
    this.typed = {}; // member -> {c, p} kept after an error
    this.scroll = "PAGE";
  }

  async load() {
    this.members = await this.app.get("/api/members", { dsn: this.ds.dsname });
  }

  get pageRows() {
    return this.app.bodyRows - 2;
  }

  get panelName() {
    if (this.mode === "browse") return "BROWSE";
    return this.app.readOnly ? "VIEW" : "EDIT";
  }

  render() {
    const a = this.app;
    const head = "  Name     Prompt   VV.MM Created    Changed           Size  Init   Mod ID";
    const rows = [row([head, "head"])];
    const n = this.pageRows;
    this.top = Math.max(0, Math.min(this.top, Math.max(0, this.members.length - 1)));
    this.cmds = [];
    for (const m of this.members.slice(this.top, this.top + n)) {
      const t = this.typed[m.name] || {};
      const c = field(t.c || "", 1, { key: `c:${m.name}` });
      const st = this.status[m.name] || (m.alias ? "*Alias" : "");
      const p = field(t.p || "", 8, { key: `p:${m.name}`, placeholder: st, cls: "prompt-fld" });
      this.cmds.push({ c, p, m });
      const stats = m.version === null
        ? "".padEnd(5)
        : m.version;
      rows.push(row(c, " ", [m.name.padEnd(8), m.alias ? "alias" : "out"], " ", p, " ",
        [`${stats} ${date(m.created).padEnd(10)} ${stamp(m.changed).padEnd(16)} `
          + `${num(m.lines, 5)} ${num(m.init_lines, 5)} ${num(m.mod_lines, 5)} ${m.userid ?? ""}`, "out"]));
    }
    if (this.top + n >= this.members.length) rows.push(row(["  **End**", "marker"]));
    const right = `Row ${pad5(this.members.length ? this.top + 1 : 0)} of ${pad5(this.members.length)}`;
    a.frame({ panel: this.panelName, title: this.ds.dsname, right, rows, scroll: this.scroll, legend: "list" });
    if (!a.top.long) {
      a.root.querySelector(".long").textContent = this.members.length
        ? "S/E Edit  B Browse  D Delete  R Rename (new name in Prompt)   Command: S name, L name"
        : "No members in data set. S name creates one.";
    }
    a.focusCommand();
  }

  async onKey(key) {
    const a = this.app;
    this.scroll = a.fields.scroll.value.trim() || "PAGE";
    const lineCmds = this.cmds
      .map(({ c, p, m }) => ({ c: c.value.trim().toUpperCase(), p: p.value.trim().toUpperCase(), m }))
      .filter((x) => x.c);
    const cmd = a.fields.cmd.value.trim();
    a.clearMessage();
    if (key === "pf3" || key === "pf12") return a.close();
    if (key === "pf7" || key === "pf8") {
      const amt = scrollAmount(cmd || this.scroll, this.pageRows, null);
      if (amt === null) { a.message("Invalid scroll amount"); return a.render(() => true); }
      const max = Math.max(0, this.members.length - 1);
      this.top = key === "pf8" ? Math.min(max, this.top + amt) : Math.max(0, this.top - amt);
      return a.render((k) => k.startsWith("c:") || k.startsWith("p:"));
    }
    if (key !== "enter") return;

    if (cmd) {
      const keepCmd = await this.primary(cmd);
      if (keepCmd) return a.render((k) => k === "cmd");
      await this.reload();
      return a.render();
    }
    this.typed = {};
    this.epoch = a.epoch;
    for (let k = 0; k < lineCmds.length; k++) {
      const ok = await this.lineCommand(lineCmds[k]);
      if (this.stale()) {
        // another tab replaced the volume: the rest belongs to the old one; drop it
        this.typed = {};
        return;
      }
      if (!ok) {
        for (const r of lineCmds.slice(k)) this.typed[r.m.name] = { c: r.c, p: r.p };
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
      this.members = await this.app.get("/api/members", { dsn: this.ds.dsname });
    } catch (e) {
      this.app.error(e);
    }
  }

  /** Returns true when the command stays on the command line (error). */
  async primary(cmd) {
    const a = this.app;
    const m = /^(S|SEL|SELECT|L|LOC|LOCATE)\s+(\S+)$/i.exec(cmd);
    if (!m) {
      a.message("Invalid command", "Primary commands: S name, L name");
      return true;
    }
    const name = m[2].toUpperCase();
    if (/^L/i.test(m[1])) {
      const i = this.members.findIndex((x) => x.name >= name);
      this.top = i < 0 ? Math.max(0, this.members.length - 1) : i;
      if (i < 0) a.message("Member not found", `No member at or after ${name} in ${this.ds.dsname}`);
      return false;
    }
    const err = memberNameError(name);
    if (err) {
      a.message("Invalid member name", err);
      return true;
    }
    const existing = this.members.find((x) => x.name === name);
    if (existing) {
      await this.open(existing.name, this.mode);
      return false;
    }
    if (this.mode === "browse") {
      a.message("Member not found", `${name} is not in ${this.ds.dsname}`);
      return true;
    }
    if (this.ds.recfm_str.startsWith("U")) {
      a.message("Not supported", `${this.ds.dsname} is RECFM U: members cannot be created`);
      return true;
    }
    if (a.readOnly) {
      a.message("Read-only session", `${name} does not exist, and members cannot be created: the volume is open read-only`);
      return true;
    }
    // ISPF: the member is created when it is first saved
    await this.open(name, "edit", true);
    return false;
  }

  async open(name, mode, isNew = false) {
    const a = this.app;
    try {
      const r = await a.run(screenFor(a, this.ds, name, mode, isNew));
      this.status[name] = r?.saved ? "*Saved" : mode === "edit" ? "*Edited" : "*Browsed";
      if (r?.message) a.message(r.message);
      return true;
    } catch (e) {
      a.error(e);
      return false;
    }
  }

  async lineCommand({ c, p, m }) {
    const a = this.app;
    const dsn = this.ds.dsname;
    if (this.stale()) return false;
    try {
      switch (c) {
        case "S": return this.open(m.name, this.mode);
        case "E": return this.open(m.name, "edit");
        case "B": return this.open(m.name, "browse");
        case "D": {
          if (this.stale()) return false;
          const ans = await a.run(new ConfirmScreen(a, {
            panel: "DELETE", title: "Confirm Member Delete",
            text: [`Delete member ${m.name} from ${dsn}?`,
              m.alias ? "This is an alias: only the alias entry is removed." : "The directory entry is removed; the space is reclaimed by a compress (Z)."],
            choices: { Y: "delete the member" },
          }));
          if (this.stale()) return false; // the volume changed while the prompt was up
          if (ans !== "Y") {
            this.status[m.name] = "*Kept";
            return true;
          }
          a.setInfo(await a.post("/api/member/delete", { dsn, name: m.name }));
          a.message("Member deleted", `${m.name} deleted from ${dsn}`);
          return true;
        }
        case "R": {
          if (!p) {
            a.message("Enter new name", "Type the new name in the Prompt field next to the R");
            return false;
          }
          const err = memberNameError(p);
          if (err) {
            a.message("Invalid member name", err);
            return false;
          }
          if (this.stale()) return false;
          a.setInfo(await a.post("/api/member/rename", { dsn, old: m.name, new: p }));
          this.status[p] = "*Renamed";
          a.message("Member renamed", `${m.name} renamed to ${p}`);
          return true;
        }
        default:
          a.message("Invalid line command", `"${c}": use S, E, B, D or R`);
          return false;
      }
    } catch (e) {
      a.error(e);
      return false;
    }
  }

  dirty() {
    return false;
  }
}

function pad5(n) {
  return String(n).padStart(5, "0");
}
