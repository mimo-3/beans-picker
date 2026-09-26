#!/bin/sh
# Builds the bench fixture app into ~/Library/Caches/cua-jev/fixture/CuaJevFixture.app and prints its path.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out="$HOME/Library/Caches/cua-jev/fixture/CuaJevFixture.app"
mkdir -p "$out/Contents/MacOS"
clang -fobjc-arc -O2 -framework AppKit -o "$out/Contents/MacOS/CuaJevFixture" "$here/main.m"
cat > "$out/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.cua-jev.fixture</string>
<key>CFBundleName</key><string>CuaJevFixture</string>
<key>CFBundleExecutable</key><string>CuaJevFixture</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>NSPrincipalClass</key><string>NSApplication</string>
</dict></plist>
PLIST
echo "$out"
