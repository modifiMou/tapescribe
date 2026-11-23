#!/bin/zsh
# 生成并安装桌面快捷方式 “ASR 工作台.app”。重复执行会覆盖旧版本。
set -euo pipefail

HERE="${0:A:h}"
ROOT="${HERE:h}"
APP_NAME="ASR 工作台"
DEST="$HOME/Desktop/$APP_NAME.app"
BUILD="$HERE/build/$APP_NAME.app"

chmod +x "$HERE/launch.sh"
rm -rf "$BUILD"
mkdir -p "$HERE/build"

osacompile -o "$BUILD" -e 'on run
	set s to POSIX path of (path to resource "launch.sh")
	do shell script quoted form of s
end run'

# 把项目根目录写进包内的启动脚本：.app 会被复制到桌面，不能再靠脚本位置推断。
{
  print -r -- '#!/bin/zsh'
  print -r -- "ASRWB_ROOT=${(q)ROOT}"
  tail -n +2 "$HERE/launch.sh"
} > "$BUILD/Contents/Resources/launch.sh"
chmod +x "$BUILD/Contents/Resources/launch.sh"
cp "$HERE/AppIcon.icns" "$BUILD/Contents/Resources/applet.icns"

PLIST="$BUILD/Contents/Info.plist"
PB=/usr/libexec/PlistBuddy

set_str() { $PB -c "Set :$1 $2" "$PLIST" 2>/dev/null || $PB -c "Add :$1 string $2" "$PLIST"; }
set_bool() { $PB -c "Set :$1 $2" "$PLIST" 2>/dev/null || $PB -c "Add :$1 bool $2" "$PLIST"; }

$PB -c "Delete :CFBundleIdentifier" "$PLIST" 2>/dev/null || true
set_str CFBundleName "$APP_NAME"
$PB -c "Delete :CFBundleDisplayName" "$PLIST" 2>/dev/null || true
set_str CFBundleDisplayName "$APP_NAME"
set_str CFBundleIdentifier local.asrworkbench.launcher
set_str CFBundleIconFile applet
set_bool LSUIElement true

rm -f "$BUILD/Contents/Resources/Assets.car"
codesign --force --deep --sign - "$BUILD" >/dev/null 2>&1

rm -rf "$DEST"
ditto "$BUILD" "$DEST"
codesign --force --deep --sign - "$DEST" >/dev/null 2>&1
touch "$DEST"

echo "已安装: $DEST"
