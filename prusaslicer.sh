#!/usr/bin/env bash
# Gets PrusaSlicer 2.8.1 into the folder $1, for the resin plates: the official AppImage of
# PrusaSlicer, unpacked. The AppImage must have the SHA-256 below. Prusa makes no Linux AppImage
# of a later version. A folder that has the slicer already stays as it is (the cache of the Action).
set -euo pipefail

DIR="$1"
URL="https://github.com/prusa3d/PrusaSlicer/releases/download/version_2.8.1/PrusaSlicer-2.8.1+linux-x64-newer-distros-GTK3-202409181416.AppImage"
SHA256="565f2f4bd4dbb05904a459d54db1916b6932124709c1d17b5aacfe9f5f2f1b03"

# The AppImage uses WebKitGTK of the system (the "newer distros" build).
if ! ldconfig -p | grep -q libwebkit2gtk-4.1.so.0; then
	echo "Installing WebKitGTK 4.1"
	sudo apt-get update -qq
	sudo apt-get install -y -qq libwebkit2gtk-4.1-0 >/dev/null
fi
if [[ -x "$DIR/squashfs-root/AppRun" && -f "$DIR/sha256" && "$(cat "$DIR/sha256")" == "$SHA256" ]]; then
	echo "PrusaSlicer is in $DIR"
	exit 0
fi
rm -rf "${DIR:?}"
mkdir -p "$DIR"
echo "Getting PrusaSlicer 2.8.1 (99 MB)"
curl -fsSL --retry 3 -o "$DIR/prusaslicer.AppImage" "$URL"
echo "$SHA256  $DIR/prusaslicer.AppImage" | sha256sum -c -
chmod +x "$DIR/prusaslicer.AppImage"
(cd "$DIR" && ./prusaslicer.AppImage --appimage-extract >/dev/null)
rm "$DIR/prusaslicer.AppImage"
echo "$SHA256" >"$DIR/sha256"
"$DIR/squashfs-root/AppRun" --help 2>/dev/null | head -1 || true
