#!/usr/bin/env bash
# Package the add-on as a Blender extension zip.
#   ./build.sh   ->  dist/studio_render_presets-<version>.zip
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(grep -E '^version *= *"' blender_manifest.toml | head -1 | cut -d'"' -f2)
OUT="dist/studio_render_presets-${VERSION}.zip"

mkdir -p dist
rm -f "$OUT"
zip -q "$OUT" blender_manifest.toml __init__.py LICENSE README.md
echo "built $OUT"
unzip -l "$OUT"
