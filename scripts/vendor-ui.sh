#!/usr/bin/env bash
# Rebuild the admin console's vendored files. Run only when changing a pinned version; the output is committed,
# so the gateway image needs no Node and the console makes no external requests.
#   gateway/ui/fx.js            React + ReactDOM + metal-fx (liquid-metal button ring), one ES module
#   gateway/ui/fx.csp           CSP hashes of the <style> blocks metal-fx injects (read by the gateway)
#   gateway/ui/geist*.woff2     Geist and Geist Mono variable fonts (latin)
#   gateway/ui/THIRD_PARTY.txt  licences of everything above
# Needs Node 18+ and network access to the npm registry.
set -euo pipefail
cd "$(dirname "$0")/.."
UI=gateway/ui
PKGS=(react@18.3.1 react-dom@18.3.1 metal-fx@2.0.11 @fontsource-variable/geist@5.3.0 @fontsource-variable/geist-mono@5.3.0 esbuild@0.28.2)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
(cd "$work" && npm init -y >/dev/null && npm install --no-audit --no-fund --silent "${PKGS[@]}")
cp scripts/ui-fx-entry.js "$work/entry.js"
(cd "$work" && npx esbuild entry.js --bundle --format=esm --minify --legal-comments=none \
  --define:process.env.NODE_ENV='"production"' --outfile=fx.js --log-level=warning)
cp "$work/fx.js" "$UI/fx.js"
cp "$work/node_modules/@fontsource-variable/geist/files/geist-latin-wght-normal.woff2" "$UI/geist.woff2"
cp "$work/node_modules/@fontsource-variable/geist-mono/files/geist-mono-latin-wght-normal.woff2" "$UI/geist-mono.woff2"

# The admin CSP allows no inline styles, so the exact <style> texts metal-fx injects are allowed by hash.
# One is inserted when the module loads (captured by running it against a stub DOM); the others are
# plain string literals set as textContent (read from the bundle).
node --input-type=module -e '
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
const [bundle, out] = process.argv.slice(1);
const texts = new Set();
const noop = () => {};
globalThis.document = { getElementById: () => null, addEventListener: noop, removeEventListener: noop,
  head: { appendChild: (el) => texts.add(el.textContent) }, body: { appendChild: noop },
  createElement: () => ({ setAttribute: noop, addEventListener: noop, style: {}, getContext: () => null }) };
await import(new URL("file://" + bundle));
const src = readFileSync(bundle, "utf8");
for (const m of src.matchAll(/textContent\s*=\s*("(?:[^"\\]|\\.)*")/g)) texts.add(JSON.parse(m[1]));
for (const m of src.matchAll(/=\s*("\.metal-fx-root\[[^"]*")/g)) texts.add(JSON.parse(m[1]));
if (texts.size < 2) throw new Error("expected metal-fx style blocks, found " + texts.size);
const hashes = [...texts].map((t) => `'"'"'sha256-${createHash("sha256").update(t).digest("base64")}'"'"'`);
writeFileSync(out, hashes.join(" ") + "\n");
console.log(`fx.csp: ${hashes.length} style hashes`);
' "$PWD/$UI/fx.js" "$UI/fx.csp"

{
  echo "Third-party files in this folder. Rebuilt by scripts/vendor-ui.sh."
  echo
  echo "fx.js bundles: ${PKGS[*]:0:3}"
  for p in react react-dom metal-fx; do echo; echo "==== $p"; cat "$work/node_modules/$p/LICENSE"; done
  echo; echo "==== metal-fx NOTICE"; cat "$work/node_modules/metal-fx/NOTICE"
  echo; echo "==== geist.woff2, geist-mono.woff2 (Geist, Geist Mono by Vercel; SIL Open Font License 1.1)"
  cat "$work/node_modules/@fontsource-variable/geist/LICENSE"
} > "$UI/THIRD_PARTY.txt"
ls -l "$UI"/fx.js "$UI"/fx.csp "$UI"/geist*.woff2
