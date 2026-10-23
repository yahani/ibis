#!/usr/bin/env bash
# The 60-second demo (docs/DEMO.md): a fresh workspace with 10 fake invoices, one run, the Excel
# file, then the review page. Needs ANTHROPIC_API_KEY in .env; costs about $0.02.
#
#   scripts/demo.sh                 # workspace in demo-workspace/ (git-ignored)
#   scripts/demo.sh /tmp/ibis-demo  # or anywhere else
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORKSPACE="${1:-$ROOT/demo-workspace}"

# 7 clean documents (an invoice, a credit note, a packing slip, a two-page euro invoice, ...) and
# 3 that need a look: a total off by $100, the same file saved twice, and a date that could be
# 3 Sep or 9 Mar.
FILES=(
  001-inv-classic 002-inv-modern 003-inv-us_sales 004-credit-compact 006-inv-euro
  015-inv-euro 025-slip-classic 039-inv-us_sales 045-inv-copy-of-001 047-inv-classic
)

rm -rf "$WORKSPACE"
mkdir -p "$WORKSPACE/inbox"
for name in "${FILES[@]}"; do
  cp "$ROOT/demo/invoices/$name.pdf" "$WORKSPACE/inbox/"
done
printf 'input_dir: inbox\noutput_dir: output\n' > "$WORKSPACE/config.yaml"

cd "$ROOT"
echo "→ A fresh workspace (anything from an earlier demo is wiped): ${#FILES[@]} PDFs in $WORKSPACE/inbox"
uv run ibis run -c "$WORKSPACE/config.yaml"

echo
echo "→ Opening the Excel file"
if command -v open >/dev/null; then open "$WORKSPACE/output/ibis.xlsx"
elif command -v xdg-open >/dev/null; then xdg-open "$WORKSPACE/output/ibis.xlsx"
else echo "  $WORKSPACE/output/ibis.xlsx"
fi

echo
echo "→ Opening the review page (Ctrl+C to stop)"
uv run ibis review -c "$WORKSPACE/config.yaml"
