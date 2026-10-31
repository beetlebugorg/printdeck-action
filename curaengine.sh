#!/usr/bin/env bash
# Gets CuraEngine 5.13.0 into the folder $1: the engine and the libraries that it needs, from the
# official AppImage of Cura 5.13.0. The AppImage must have the SHA-256 below. A folder that has
# the engine already stays as it is (the cache of the Action).
set -euo pipefail

DIR="$1"
URL="https://github.com/Ultimaker/Cura/releases/download/5.13.0/UltiMaker-Cura-5.13.0-linux-X64.AppImage"
SHA256="100f068127b2598167f00ba4db0e0699ded45adf97bbab2d22ff171a1e1ecc40"

# The SHA-256 of CuraEngine of that AppImage. A copy in the cache must have it too.
ENGINE_SHA256="949df48874258ff342b5369853515a93d52fa79448aab050d28ceffe16537f4b"

if [[ -x "$DIR/CuraEngine" ]]; then
	if echo "$ENGINE_SHA256  $DIR/CuraEngine" | sha256sum -c --quiet - 2>/dev/null; then
		echo "CuraEngine is in $DIR"
		exit 0
	fi
	echo "The CuraEngine of the cache has another SHA-256: getting it again"
	rm -rf "${DIR:?}"/*
fi
mkdir -p "$DIR"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Getting Cura 5.13.0 (330 MB) for its CuraEngine"
curl -fsSL --retry 3 -o "$TMP/cura.AppImage" "$URL"
echo "$SHA256  $TMP/cura.AppImage" | sha256sum -c -
chmod +x "$TMP/cura.AppImage"
(cd "$TMP" && ./cura.AppImage --appimage-extract CuraEngine >/dev/null && ./cura.AppImage --appimage-extract '*.so*' >/dev/null)
echo "$ENGINE_SHA256  $TMP/squashfs-root/CuraEngine" | sha256sum -c -
cp "$TMP/squashfs-root/CuraEngine" "$DIR/"
for lib in libArcus.so libpolyclipping.so.22 'libtbb*.so*' libssl.so.3 libcrypto.so.3; do
	# shellcheck disable=SC2086
	cp -L $TMP/squashfs-root/$lib "$DIR/"
done
"/lib64/ld-linux-x86-64.so.2" --library-path "$DIR" "$DIR/CuraEngine" help 2>&1 | grep -m1 -i version
