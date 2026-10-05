// In-page helper library, installed in every frame via an init script.
// Computes roles/accessible names, table context and adjacent labels for
// legacy table-layout pages, and resolves structural locators.
(() => {
  if (window.__cua) return;
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const TEXTBOX_TYPES = new Set(["", "text", "password", "email", "number", "tel", "search", "url"]);

  function isVisible(el) {
    if (!el.isConnected) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none";
  }

  function role(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "input") {
      const t = (el.getAttribute("type") || "").toLowerCase();
      if (["submit", "button", "reset", "image"].includes(t)) return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (TEXTBOX_TYPES.has(t)) return "textbox";
      return "input";
    }
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "td") return "cell";
    if (tag === "th") return "columnheader";
    return "text";
  }

  function labelFor(el) {
    if (el.getAttribute("aria-label")) return norm(el.getAttribute("aria-label"));
    const lb = el.getAttribute("aria-labelledby");
    if (lb) return norm(lb.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" "));
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return norm(l.innerText);
    }
    const wrap = el.closest("label");
    if (wrap) return norm(wrap.innerText);
    return null;
  }

  function accName(el) {
    const r = role(el);
    const lab = labelFor(el);
    if (lab) return lab;
    if (r === "button" && el.tagName === "INPUT") return norm(el.value);
    if (["button", "link", "heading", "cell", "columnheader", "text"].includes(r)) return norm(el.innerText);
    if (el.getAttribute("title")) return norm(el.getAttribute("title"));
    return "";
  }

  function cellOf(el) {
    return el.closest("td,th");
  }

  function nearLabel(el) {
    const cell = cellOf(el);
    if (!cell) return null;
    let prev = cell.previousElementSibling;
    while (prev && !norm(prev.innerText)) prev = prev.previousElementSibling;
    if (!prev || prev.querySelector("input,select,textarea,button")) return null;
    return norm(prev.innerText) || null;
  }

  // A first row counts as a header only if every non-empty cell is a <th> or bold:
  // key/value tables ("Name: | Dana") have plain first rows and are not data tables.
  function isBold(cell) {
    if (cell.tagName === "TH") return true;
    const inner = cell.querySelector("b,strong") || cell;
    return parseInt(getComputedStyle(inner).fontWeight, 10) >= 600;
  }
  function headerCells(table) {
    const first = table.rows[0];
    if (!first) return null;
    const cells = Array.from(first.cells);
    const texts = cells.map((c) => norm(c.innerText));
    if (texts.filter(Boolean).length < 2) return null;
    if (!cells.every((c, i) => !texts[i] || isBold(c))) return null;
    return texts;
  }

  function tableContext(el) {
    const cell = cellOf(el);
    if (!cell) return {};
    const row = cell.parentElement;
    const table = cell.closest("table");
    if (!table || !row || row === table.rows[0]) return {};
    const headers = headerCells(table);
    if (!headers) return {};
    const rowMap = {};
    Array.from(row.cells).forEach((c, i) => {
      if (headers[i]) rowMap[headers[i]] = norm(c.innerText);
    });
    return { column: headers[cell.cellIndex] || null, row: rowMap };
  }

  function cssPath(el) {
    const parts = [];
    while (el && el.nodeType === 1 && el.tagName !== "HTML") {
      const tag = el.tagName.toLowerCase();
      let i = 1;
      for (let s = el.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === el.tagName) i++;
      parts.unshift(`${tag}:nth-of-type(${i})`);
      el = el.parentElement;
    }
    return parts.join(" > ");
  }

  function emphasis(el) {
    const cs = getComputedStyle(el);
    return parseInt(cs.fontWeight, 10) >= 600 || parseFloat(cs.fontSize) >= 18;
  }

  const CONTROL_SEL = "a[href],button,input:not([type=hidden]),select,textarea";

  function describe(el, ref) {
    const r = role(el);
    const ctx = tableContext(el);
    const d = {
      ref,
      role: r,
      name: accName(el),
      tag: el.tagName.toLowerCase(),
      label: ["textbox", "combobox", "checkbox", "radio"].includes(r) ? labelFor(el) : null,
      near_label: ["textbox", "combobox", "checkbox", "radio", "cell", "text"].includes(r) ? nearLabel(el) : null,
      column: ctx.column || null,
      row: ctx.row || null,
      emphasis: r === "text" || r === "cell" ? emphasis(el) : false,
      css: cssPath(el),
    };
    if (r === "textbox") d.value = el.type === "password" ? (el.value ? "<secret>" : "") : el.value;
    if (r === "combobox") {
      d.value = el.selectedOptions[0] ? norm(el.selectedOptions[0].text) : "";
      d.options = Array.from(el.options).map((o) => norm(o.text));
    }
    if (r === "link") d.href = el.getAttribute("href");
    return d;
  }

  function observe(prefix) {
    document.querySelectorAll("[data-cua-ref]").forEach((e) => e.removeAttribute("data-cua-ref"));
    if (!document.body) return [];
    const out = [];
    const seen = new Set();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    const textOwners = [];
    for (let n = walker.currentNode; n; n = walker.nextNode()) {
      if (n.nodeType === 1 && n.matches(CONTROL_SEL)) {
        if (!seen.has(n) && isVisible(n)) {
          seen.add(n);
          out.push(n);
        }
      } else if (n.nodeType === 3 && norm(n.textContent)) {
        const p = n.parentElement;
        if (!p || p.closest(CONTROL_SEL) || p.closest("script,style,option,select")) continue;
        if (!seen.has(p) && isVisible(p)) {
          seen.add(p);
          out.push(p);
          textOwners.push(p);
        }
      }
    }
    return out.slice(0, 400).map((el, i) => {
      const ref = `${prefix}${i}`;
      el.setAttribute("data-cua-ref", ref);
      const d = describe(el, ref);
      if (d.role === "text" || d.role === "cell") {
        // Avoid huge container text: if the element contains other observed elements, keep direct text only.
        const containsOthers = out.some((o) => o !== el && el.contains(o));
        if (containsOthers) {
          d.name = norm(Array.from(el.childNodes).filter((c) => c.nodeType === 3).map((c) => c.textContent).join(" "));
        }
      }
      return d;
    });
  }

  function controlMatches(el, controlRole) {
    return isVisible(el) && role(el) === controlRole;
  }

  function resolve(spec, nonce) {
    document.querySelectorAll("[data-cua-hit]").forEach((e) => e.removeAttribute("data-cua-hit"));
    const hits = [];
    if (spec.strategy === "near_label") {
      document.querySelectorAll("td,th").forEach((cell) => {
        if (norm(cell.innerText) !== spec.label_text || cell.querySelector(CONTROL_SEL)) return;
        let next = cell.nextElementSibling;
        while (next && !next.querySelector(CONTROL_SEL) && !norm(next.innerText)) next = next.nextElementSibling;
        if (!next) return;
        if (spec.control_role) {
          next.querySelectorAll(CONTROL_SEL).forEach((c) => controlMatches(c, spec.control_role) && hits.push(c));
        } else if (isVisible(next) && !next.querySelector(CONTROL_SEL)) {
          hits.push(next); // value cell beside a label, e.g. "Reference Number: | SA-480017"
        }
      });
    } else if (spec.strategy === "table_cell") {
      document.querySelectorAll("table").forEach((table) => {
        const headers = headerCells(table);
        if (!headers) return;
        const keyIdx = headers.indexOf(spec.row_key_column);
        const colIdx = spec.column_header ? headers.indexOf(spec.column_header) : -1;
        if (keyIdx < 0 || (spec.column_header && colIdx < 0)) return;
        Array.from(table.rows).slice(1).forEach((row) => {
          const key = row.cells[keyIdx];
          if (!key || norm(key.innerText) !== spec.row_key_value) return;
          const scope = colIdx >= 0 ? row.cells[colIdx] : row;
          if (!scope) return;
          if (spec.control_role) {
            scope.querySelectorAll(CONTROL_SEL).forEach((c) => controlMatches(c, spec.control_role) && hits.push(c));
          } else if (isVisible(scope)) {
            hits.push(scope);
          }
        });
      });
    }
    hits.forEach((h) => h.setAttribute("data-cua-hit", nonce));
    return hits.length;
  }

  function linesAfter(anchor) {
    const all = Array.from(document.querySelectorAll("body *")).filter(
      (e) => norm(e.innerText).startsWith(anchor) && isVisible(e)
    );
    if (!all.length) return null;
    // Smallest element whose text starts with the anchor and has more lines after it.
    let best = all[all.length - 1];
    for (const e of all) if (e.innerText.split("\n").length > 1 && e.contains(best)) best = e;
    let block = best;
    while (block.parentElement && block.innerText.split("\n").filter((l) => norm(l)).length < 2) block = block.parentElement;
    const lines = block.innerText.split("\n").map(norm).filter(Boolean);
    const i = lines.findIndex((l) => l.startsWith(anchor));
    return lines.slice(i + 1);
  }

  function markSensitive(rules) {
    document.querySelectorAll("[data-cua-mask]").forEach((e) => e.removeAttribute("data-cua-mask"));
    let n = 0;
    const mark = (e) => { e.setAttribute("data-cua-mask", "1"); n++; };
    document.querySelectorAll("input[type=password]").forEach(mark);
    const labels = new Set(rules.labels || []);
    document.querySelectorAll("td,th").forEach((cell) => {
      if (!labels.has(norm(cell.innerText))) return;
      const next = cell.nextElementSibling;
      if (next) mark(next);
    });
    const cols = new Set(rules.columns || []);
    document.querySelectorAll("table").forEach((table) => {
      const headers = headerCells(table);
      if (!headers) return;
      headers.forEach((h, idx) => {
        if (!cols.has(h)) return;
        Array.from(table.rows).slice(1).forEach((row) => row.cells[idx] && mark(row.cells[idx]));
      });
    });
    return n;
  }

  window.__cua = { observe, resolve, describe, role, accName, labelFor, nearLabel, tableContext, linesAfter, markSensitive, norm };
})();
