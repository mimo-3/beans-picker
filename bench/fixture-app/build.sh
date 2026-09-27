#!/bin/sh
# Builds the bench fixture app into ~/Library/Caches/beans-picker/fixture/BeansPickerFixture.app and prints its path.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out="$HOME/Library/Caches/beans-picker/fixture/BeansPickerFixture.app"
mkdir -p "$out/Contents/MacOS"
clang -fobjc-arc -O2 -framework AppKit -o "$out/Contents/MacOS/BeansPickerFixture" "$here/main.m"
cat > "$out/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.beans-picker.fixture</string>
<key>CFBundleName</key><string>BeansPickerFixture</string>
<key>CFBundleExecutable</key><string>BeansPickerFixture</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>NSPrincipalClass</key><string>NSApplication</string>
</dict></plist>
PLIST
echo "$out"
