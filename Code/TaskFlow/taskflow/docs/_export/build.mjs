import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { marked } from "marked";
import puppeteer from "puppeteer";

const SP = import.meta.dirname;
const DOCS = process.argv[2];
const OUT = process.argv[3];
const CHROME = "/usr/bin/chromium";
const files = process.argv.slice(4);

const css = fs.readFileSync(path.join(SP, "style.css"), "utf8");
fs.mkdirSync(path.join(SP, "diagrams"), { recursive: true });

// mermaid theme config, tuned for print on white paper
fs.writeFileSync(path.join(SP, "mermaid.json"), JSON.stringify({
  theme: "base",
  themeVariables: {
    fontFamily: "Inter, Noto Sans, Liberation Sans, Arial, sans-serif",
    fontSize: "15px",
    primaryColor: "#eef3f9", primaryTextColor: "#0d1b2a", primaryBorderColor: "#1d3557",
    lineColor: "#37506b", textColor: "#16191d",
    secondaryColor: "#fff3d6", tertiaryColor: "#e8f7f1",
    clusterBkg: "#f6f8fa", clusterBorder: "#c3ccd6",
    edgeLabelBackground: "#ffffff",
    nodeBorder: "#1d3557", mainBkg: "#eef3f9"
  },
  flowchart: { curve: "basis", htmlLabels: true, useMaxWidth: true, padding: 12 },
  stateDiagram: { useMaxWidth: true }
}, null, 2));


// Scale a mermaid diagram to 1.35x its natural size, but never wider than the text
// column (640px) nor taller than one page's usable height (700px).
const MAX_W = 640, MAX_H = 700;
function fitWidth(svg, naturalW) {
  const vb = svg.match(/viewBox="[\d.\-]+ [\d.\-]+ ([\d.]+) ([\d.]+)"/);
  let w = Math.round(naturalW * 1.35);
  if (vb) {
    const ratio = parseFloat(vb[2]) / parseFloat(vb[1]);
    if (w * ratio > MAX_H) w = Math.round(MAX_H / ratio);
  }
  return Math.min(w, MAX_W);
}

function renderMermaid(code, id, fmt) {
  const src = path.join(SP, "diagrams", `${id}.mmd`);
  const dst = path.join(SP, "diagrams", `${id}.${fmt}`);
  if (fs.existsSync(dst)) return dst;
  fs.writeFileSync(src, code);
  execFileSync(path.join(SP, "node_modules/.bin/mmdc"), [
    "-i", src, "-o", dst,
    "-p", path.join(SP, "puppeteer.json"),
    "-c", path.join(SP, "mermaid.json"),
    "-b", "white",
    ...(fmt === "png" ? ["-s", "3"] : []),
  ], { stdio: ["ignore", "ignore", "inherit"] });
  return dst;
}

function prepare(md, slug, fmt) {
  const diagrams = [];
  let n = 0;
  // pull mermaid fences out before markdown parsing
  md = md.replace(/```mermaid\n([\s\S]*?)```/g, (_, code) => {
    const id = `${slug}-${++n}`;
    const file = renderMermaid(code, id, fmt);
    const token = `@@DIAGRAM_${id}@@`;
    diagrams.push({ token, file, fmt });
    return `\n${token}\n`;
  });
  // GFM task boxes -> glyphs, so they survive both PDF and DOCX
  md = md.replace(/^(\s*)- \[x\] /gim, "$1- ☑ ").replace(/^(\s*)- \[ \] /gim, "$1- ☐ ");
  return { md, diagrams };
}

marked.setOptions({ gfm: true, breaks: false, mangle: false, headerIds: true });

async function build(file) {
  const slug = path.basename(file, ".md");
  const mdRaw = fs.readFileSync(path.join(DOCS, file), "utf8");
  // the footer shows each document's own version, taken from its control table
  const vm = mdRaw.match(/\|\s*\*\*(?:Document )?Version\*\*\s*\|\s*([^|]+?)\s*\|/);
  const version = vm ? `v${vm[1].replace(/^v/i, "")}` : "";

  for (const fmt of ["svg", "png"]) {
    const { md, diagrams } = prepare(mdRaw, slug, fmt);
    let html = marked.parse(md);

    for (const d of diagrams) {
      let node;
      if (d.fmt === "svg") {
        let svg = fs.readFileSync(d.file, "utf8").replace(/<\?xml[^>]*\?>/, "");
        // mermaid emits width="100%" + style max-width; convert to an intrinsic size so a
        // small diagram stays small and only an oversized one is scaled down to the margin
        const m = svg.match(/max-width:\s*([\d.]+)px/);
        if (m) {
          svg = svg.replace(/max-width:\s*[\d.]+px;?/, "");
          svg = svg.replace('width="100%"', `width="${fitWidth(svg, parseFloat(m[1]))}"`);
        }
        node = `<figure class="diagram">${svg}</figure>`;
      } else {
        const b64 = fs.readFileSync(d.file).toString("base64");
        const sv = fs.readFileSync(d.file.replace(/\.png$/, ".svg"), "utf8");
        const mm = sv.match(/max-width:\s*([\d.]+)px/);
        const w = mm ? fitWidth(sv, parseFloat(mm[1])) : 620;
        node = `<figure class="diagram"><img width="${w}" src="data:image/png;base64,${b64}"></figure>`;
      }
      html = html.replace(new RegExp(`<p>${d.token}</p>`), node).replace(d.token, node);
    }

    // first fenced block of each doc is the status block -> highlight it
    html = html.replace("<pre><code>", '<pre class="statusblock"><code>');
    // ASCII box diagrams need a smaller monospace size so they don't wrap
    html = html.replace(/<pre><code>([\s\S]*?)<\/code><\/pre>/g, (m, body) => {
      const longest = Math.max(...body.split("\n").map(l => l.length));
      return longest > 74 ? m.replace("<pre>", '<pre class="wide">') : m;
    });

    // LibreOffice's HTML import honours legacy table attributes far more reliably than
    // CSS borders, so the DOCX variant carries them explicitly
    if (fmt === "png") {
      html = html.replace(/<table>/g, '<table border="1" cellspacing="0" cellpadding="4">');
    }
    const doc = `<!doctype html><html><head><meta charset="utf-8">
<title>${slug}</title><style>${css}</style></head><body>${html}</body></html>`;
    fs.writeFileSync(path.join(SP, `${slug}.${fmt === "svg" ? "print" : "docx"}.html`), doc);
  }

  // ---- PDF via headless chromium ----
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
           "--font-render-hinting=none"],
  });
  const page = await browser.newPage();
  await page.goto("file://" + path.join(SP, `${slug}.print.html`), { waitUntil: "networkidle0" });
  await page.pdf({
    path: path.join(OUT, `${slug}.pdf`),
    format: "A4",
    printBackground: true,
    margin: { top: "17mm", bottom: "18mm", left: "15mm", right: "15mm" },
    displayHeaderFooter: true,
    headerTemplate: `<div style="font-family:Inter,Arial,sans-serif;font-size:7pt;color:#8899aa;
      width:100%;padding:0 15mm;display:flex;justify-content:space-between;">
      <span>TaskFlow AI Support Agent</span><span>${slug.replace(/_/g, " ")}</span></div>`,
    footerTemplate: `<div style="font-family:Inter,Arial,sans-serif;font-size:7pt;color:#8899aa;
      width:100%;padding:0 15mm;display:flex;justify-content:space-between;">
      <span>${version}</span>
      <span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>`,
  });
  await browser.close();
  console.log(`  PDF   ${slug}.pdf`);
}

for (const f of files) { console.log(`Building ${f}`); await build(f); }
