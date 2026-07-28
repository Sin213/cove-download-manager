#!/usr/bin/env bash
# Build the Cove Download Manager .deb for amd64 from a PyInstaller bundle.
#
# Output: release/cove-download-manager_<version>_amd64.deb
#
# Notes:
# - Declares aria2 and yt-dlp dependencies so the package manager installs
#   the command-line tools Cove launches from PATH.
# - Builds the .deb manually with `ar` + `tar.xz`, no dpkg-deb dependency,
#   so it works on Arch / non-Debian build hosts.
#
# Env flags:
#   VERSION=X.Y.Z     override the version (defaults to cove/__init__.py)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP_NAME="cove-download-manager"
DISPLAY_NAME="Cove Download Manager"
VERSION="${VERSION:-$(grep -E '^__version__' cove/__init__.py | cut -d'"' -f2)}"
DEB_ARCH="amd64"
RELEASE_DIR="$ROOT/release"
DIST_DIR="$ROOT/dist"
DEB_BUILD="$ROOT/build/deb"
BUILD_ENV="$ROOT/.buildenv"
ICON_SRC="$ROOT/cove_icon.png"

mkdir -p "$RELEASE_DIR"
rm -rf "$DIST_DIR" "$ROOT/build/deb" "$ROOT/build/work"

# ----------------------------------------------------------------------
# 0. Build venv (only what we need to ship + PyInstaller).
# ----------------------------------------------------------------------
if [ ! -x "$BUILD_ENV/bin/pyinstaller" ]; then
    echo "==> Creating build venv at $BUILD_ENV"
    rm -rf "$BUILD_ENV"
    python3 -m venv "$BUILD_ENV"
    "$BUILD_ENV/bin/pip" install --quiet --upgrade pip
    "$BUILD_ENV/bin/pip" install --quiet -r requirements.txt pyinstaller
fi

# ----------------------------------------------------------------------
# 1. PyInstaller — one-dir bundle that we'll copy into /usr/lib/$APP_NAME.
# ----------------------------------------------------------------------
echo "==> PyInstaller bundle"
"$BUILD_ENV/bin/pyinstaller" \
    --noconfirm --clean --log-level WARN \
    --windowed \
    --name "$APP_NAME" \
    --paths . \
    --add-data "cove_icon.png:cove" \
    --hidden-import cove \
    --hidden-import cove.app \
    --exclude-module PySide6.QtWebEngineCore \
    --exclude-module PySide6.QtWebEngineWidgets \
    --exclude-module PySide6.QtQml \
    --exclude-module PySide6.QtQuick \
    --exclude-module PySide6.QtPdf \
    --exclude-module PySide6.Qt3DCore \
    --exclude-module PySide6.QtCharts \
    --exclude-module PySide6.QtDataVisualization \
    --exclude-module PySide6.QtMultimedia \
    --exclude-module PySide6.QtMultimediaWidgets \
    --exclude-module tkinter \
    packaging/launcher.py

BUNDLE="$DIST_DIR/$APP_NAME"
[ -d "$BUNDLE" ] || { echo "PyInstaller bundle missing: $BUNDLE"; exit 1; }

# ----------------------------------------------------------------------
# 2. Lay out the .deb tree.
# ----------------------------------------------------------------------
echo "==> Assembling .deb tree"
PKG_ROOT="$DEB_BUILD/${APP_NAME}_${VERSION}_${DEB_ARCH}"
rm -rf "$PKG_ROOT"
mkdir -p "$PKG_ROOT/DEBIAN" \
         "$PKG_ROOT/usr/bin" \
         "$PKG_ROOT/usr/lib/$APP_NAME" \
         "$PKG_ROOT/usr/share/applications" \
         "$PKG_ROOT/usr/share/icons/hicolor/256x256/apps" \
         "$PKG_ROOT/usr/share/icons/hicolor/512x512/apps" \
         "$PKG_ROOT/usr/share/doc/$APP_NAME"

cp -r "$BUNDLE"/. "$PKG_ROOT/usr/lib/$APP_NAME/"
cp "$ICON_SRC" "$PKG_ROOT/usr/share/icons/hicolor/256x256/apps/$APP_NAME.png"
cp "$ICON_SRC" "$PKG_ROOT/usr/share/icons/hicolor/512x512/apps/$APP_NAME.png"

cat > "$PKG_ROOT/usr/bin/$APP_NAME" <<EOF
#!/usr/bin/env bash
exec /usr/lib/$APP_NAME/$APP_NAME "\$@"
EOF
chmod +x "$PKG_ROOT/usr/bin/$APP_NAME"

cat > "$PKG_ROOT/usr/share/applications/$APP_NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$DISPLAY_NAME
GenericName=Download Manager
Comment=Multi-connection downloads with a queue, schedule, and global speed cap
Exec=$APP_NAME %u
Icon=$APP_NAME
Terminal=false
Categories=Network;FileTransfer;Qt;
Keywords=download;manager;aria2;http;ftp;
MimeType=x-scheme-handler/magnet;
StartupNotify=true
StartupWMClass=Cove
EOF

# Maintainer scripts refresh the desktop/MIME caches so the magnet handler
# shows up as a choice. They never set a default -- picking a handler stays
# the user's decision.
install -m 0755 "$ROOT/packaging/debian/postinst" "$PKG_ROOT/DEBIAN/postinst"
install -m 0755 "$ROOT/packaging/debian/postrm" "$PKG_ROOT/DEBIAN/postrm"

if [ -f "$ROOT/LICENSE" ]; then
    cp "$ROOT/LICENSE" "$PKG_ROOT/usr/share/doc/$APP_NAME/copyright"
else
    cat > "$PKG_ROOT/usr/share/doc/$APP_NAME/copyright" <<'EOF'
Cove Download Manager
Copyright (c) Cove
Released under the MIT License.
EOF
fi

INSTALLED_SIZE=$(du -sk "$PKG_ROOT/usr" | awk '{print $1}')

cat > "$PKG_ROOT/DEBIAN/control" <<EOF
Package: $APP_NAME
Version: $VERSION
Architecture: $DEB_ARCH
Maintainer: Cove <noreply@cove.local>
Installed-Size: $INSTALLED_SIZE
Depends: aria2, yt-dlp
Section: net
Priority: optional
Homepage: https://github.com/Sin213/cove-download-manager
Description: Multi-connection download manager
 Cove Download Manager is a Qt6 download manager built on top of aria2.
 It supports up to 16 connections per file, a configurable concurrent
 queue, a daily schedule window, a global speed cap, and resumable
 downloads. Part of the Cove tooling family.
EOF

# ----------------------------------------------------------------------
# 3. Pack the .deb (ar + tar.xz, no dpkg-deb dependency).
# ----------------------------------------------------------------------
echo "==> Building .deb archive"
DEB_OUT="$RELEASE_DIR/${APP_NAME}_${VERSION}_${DEB_ARCH}.deb"
WORK="$DEB_BUILD/work"
rm -rf "$WORK"
mkdir -p "$WORK"

(cd "$PKG_ROOT" && tar --xz --owner=0 --group=0 -cf "$WORK/control.tar.xz" -C DEBIAN .)
(cd "$PKG_ROOT" && tar --xz --owner=0 --group=0 -cf "$WORK/data.tar.xz" \
    --transform 's,^\./,,' \
    --exclude=./DEBIAN \
    .)
echo -n "2.0" > "$WORK/debian-binary"
echo "" >> "$WORK/debian-binary"

(cd "$WORK" && ar -rc "$DEB_OUT" debian-binary control.tar.xz data.tar.xz)

(cd "$(dirname "$DEB_OUT")" && sha256sum "$(basename "$DEB_OUT")" > "$(basename "$DEB_OUT").sha256")

echo
echo "Built: $DEB_OUT"
ls -lh "$DEB_OUT" "$DEB_OUT.sha256"
