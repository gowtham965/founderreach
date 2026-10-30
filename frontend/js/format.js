// Light formatting for the assistant's chat messages: **bold**, *italic*, `code`, [text](link), bare links,
// bullet and numbered lists, simple tables, and headings shown as bold lines. Built from DOM elements with
// textContent, never innerHTML, and links only for http(s) and mailto: the AI's text can't inject markup.

const SAFE_URL = /^(https?:\/\/|mailto:)/i;
const INLINE = /\*\*([^*]+?)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s<>"')]+)|(?<![\w*])\*([^*\n]+?)\*(?![\w*])|(?<![\w])_([^_\n]+?)_(?![\w])/g;

function link(text, url) {
  if (!SAFE_URL.test(url)) return document.createTextNode(text);
  const a = document.createElement("a");
  a.href = url; a.target = "_blank"; a.rel = "noopener"; a.textContent = text;
  return a;
}

export function inline(parent, text) {
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    parent.append(text.slice(last, m.index));
    if (m[1] !== undefined) { const b = document.createElement("strong"); inline(b, m[1]); parent.append(b); }
    else if (m[2] !== undefined) { const c = document.createElement("code"); c.textContent = m[2]; parent.append(c); }
    else if (m[3] !== undefined) parent.append(link(m[3], m[4]));
    else if (m[5] !== undefined) {
      const url = m[5].replace(/[.,;:!?]+$/, "");  // don't swallow sentence punctuation
      parent.append(link(url, url), m[5].slice(url.length));
    } else { const i = document.createElement("em"); i.textContent = m[6] ?? m[7]; parent.append(i); }
    last = m.index + m[0].length;
  }
  parent.append(text.slice(last));
}

const BULLET = /^\s*[-*•]\s+(.*)$/, NUMBERED = /^\s*\d+[.)]\s+(.*)$/, HEADING = /^\s*#{1,6}\s+(.*)$/;
const isRow = l => /^\s*\|.*\|\s*$/.test(l), isRule = l => /^\s*\|?[\s:|-]+\|?\s*$/.test(l) && l.includes("-");
const cells = l => l.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());

export function render(container, text) {
  const lines = (text || "").replace(/\r/g, "").split("\n");
  let para = null;
  const endPara = () => { para = null; };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) { endPara(); continue; }
    if (isRow(line)) {  // a run of | a | b | lines becomes a small table
      endPara();
      const table = document.createElement("table"); table.className = "md-table";
      let first = true;
      for (; i < lines.length && isRow(lines[i]); i++) {
        if (isRule(lines[i])) continue;
        const tr = document.createElement("tr");
        for (const c of cells(lines[i])) { const td = document.createElement(first ? "th" : "td"); inline(td, c); tr.append(td); }
        table.append(tr); first = false;
      }
      i--;
      const wrap = document.createElement("div"); wrap.className = "md-table-wrap"; wrap.append(table);
      container.append(wrap); continue;
    }
    const kind = BULLET.test(line) ? "ul" : NUMBERED.test(line) ? "ol" : null;
    if (kind) {
      endPara();
      const list = document.createElement(kind);
      for (; i < lines.length && (kind === "ul" ? BULLET : NUMBERED).test(lines[i]); i++) {
        const li = document.createElement("li"); inline(li, lines[i].match(kind === "ul" ? BULLET : NUMBERED)[1]); list.append(li);
      }
      i--;
      container.append(list); continue;
    }
    const h = line.match(HEADING);
    if (h) { endPara(); const p = document.createElement("p"); const b = document.createElement("strong"); inline(b, h[1]); p.append(b); container.append(p); continue; }
    if (!para) { para = document.createElement("p"); container.append(para); }
    else para.append(document.createElement("br"));
    inline(para, line);
  }
}
