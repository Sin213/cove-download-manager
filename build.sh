#!/usr/bin/env bash
# Build the Cove Download Manager AppImage into release/.
# We drive python-appimage with --no-packaging then run appimagetool
# ourselves so spaces in the .desktop's Name= don't trip up packaging.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

VERSION="$(grep -E '^__version__' cove/__init__.py | cut -d'"' -f2)"
ARCH="$(uname -m)"
OUT_NAME="Cove-Download-Manager-${VERSION}-${ARCH}.AppImage"
OUT="release/${OUT_NAME}"

# build/recipe/ is a committed, read-only template. The recipe python-appimage
# actually consumes carries per-build state (this checkout's absolute path, the
# AppImage icon), so we stage a throwaway copy outside the source tree and
# generate into that. The build must never write to tracked source.
TEMPLATE_RECIPE="build/recipe"
STAGED_RECIPE="$(mktemp -d "${TMPDIR:-/tmp}/cove-appimage-recipe.XXXXXXXX")"
cleanup_staged_recipe() {
    if [ -n "${STAGED_RECIPE:-}" ] && [ -d "${STAGED_RECIPE:-}" ]; then
        rm -rf -- "$STAGED_RECIPE"
    fi
}
trap cleanup_staged_recipe EXIT
cp -a "$TEMPLATE_RECIPE/." "$STAGED_RECIPE/"
# The template may be checked out read-only; the staged copy must be writable.
chmod -R u+w "$STAGED_RECIPE"

# Point python-appimage's requirements at this checkout by absolute path.
cat > "$STAGED_RECIPE/requirements.txt" <<EOF
PySide6>=6.5
requests>=2.31
yt-dlp>=2025.1.0
${HERE}
EOF
# Refresh bundled icon — once into the staged recipe (for python-appimage's
# .desktop integration) and once into the package itself (so the running
# app can find it via importlib's package data search).
cp -f cove_dm_icon.png "$STAGED_RECIPE/cove.png"
cp -f cove_dm_icon.png cove/cove_dm_icon.png
# The suite icon stays bundled as find_icon()'s fallback.
cp -f cove_icon.png cove/cove_icon.png

# Clear stale build artifacts.
rm -rf "Cove Download Manager-${ARCH}" Cove.AppDir "Cove-${ARCH}.AppImage" cove.egg-info build/__pycache__ AppDir

PYAPPIMG="${PYAPPIMG:-$HOME/.local/bin/python-appimage}"
"$PYAPPIMG" build app --no-packaging -p 3.13 "$STAGED_RECIPE"

# python-appimage names the AppDir from the .desktop's Name= field.
SRC_DIR="Cove Download Manager-${ARCH}"
[ -d "$SRC_DIR" ] || { echo "AppDir '$SRC_DIR' not found" >&2; exit 1; }
mv "$SRC_DIR" Cove.AppDir

# Locate appimagetool (python-appimage caches it).
APPIMAGETOOL="$HOME/.cache/python-appimage/bin/.appimagetool-continuous.appdir.${ARCH}/AppRun"
if [ ! -x "$APPIMAGETOOL" ]; then
    APPIMAGETOOL="$(command -v appimagetool || true)"
fi
[ -x "$APPIMAGETOOL" ] || { echo "appimagetool not found" >&2; exit 1; }

mkdir -p release
# Write to a temp name then atomically rename — rename(2) can replace
# even a currently-running AppImage because the running process keeps
# the original inode mmap'd while the directory entry repoints.
TMP_OUT="release/.Cove-Download-Manager.${ARCH}.$$.AppImage"
ARCH="$ARCH" "$APPIMAGETOOL" --no-appstream Cove.AppDir "$TMP_OUT"
mv -f "$TMP_OUT" "$OUT"

# Cleanup intermediate AppDir (saves ~700 MB on repeated builds).
rm -rf Cove.AppDir

(cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")

echo
echo "Built: $OUT"
ls -la "$OUT" "$OUT.sha256"
