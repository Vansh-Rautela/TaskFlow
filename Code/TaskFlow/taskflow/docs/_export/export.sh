#!/usr/bin/env bash
# Regenerate docs/*.pdf and docs/*.docx from docs/*.md.
#
# Run this after editing BRD.md or PRODUCT_SPECIFICATION.md — the markdown is the
# source of truth; the PDF and DOCX are build artefacts.
#
#   ./docs/_export/export.sh            # both documents
#   ./docs/_export/export.sh BRD.md     # just one
#
# Requires: node + npm, chromium at /usr/bin/chromium, libreoffice (soffice).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCS="$(dirname "$HERE")"
FILES=("$@")
[ ${#FILES[@]} -eq 0 ] && FILES=(BRD.md PRODUCT_SPECIFICATION.md)

command -v chromium >/dev/null || { echo "chromium not found — see puppeteer.json"; exit 1; }
command -v soffice  >/dev/null || { echo "libreoffice (soffice) not found"; exit 1; }

[ -d "$HERE/node_modules" ] || (cd "$HERE" && npm install --no-audit --no-fund)

echo "→ rendering diagrams and PDFs"
rm -rf "$HERE/diagrams"          # force mermaid re-render so edits take effect
(cd "$HERE" && node build.mjs "$DOCS" "$DOCS" "${FILES[@]}")

echo "→ converting to DOCX"
for f in "${FILES[@]}"; do
  slug="${f%.md}"
  (cd "$HERE" && soffice --headless --norestore \
      --infilter='HTML (StarWriter)' \
      --convert-to 'docx:MS Word 2007 XML' \
      --outdir "$HERE/docxout" "$HERE/$slug.docx.html" >/dev/null)
  mv "$HERE/docxout/$slug.docx.docx" "$DOCS/$slug.docx"
  echo "  DOCX  $slug.docx"
done

rm -rf "$HERE/docxout" "$HERE"/*.print.html "$HERE"/*.docx.html
echo "done — PDFs and DOCX files are in $DOCS"
