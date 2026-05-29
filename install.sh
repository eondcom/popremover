#!/usr/bin/env bash
# PopRemover 설치 스크립트
# 실행 파일을 ~/.local/bin 에, 데스크톱 항목을 ~/.local/share/applications 에 설치합니다.
set -e

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"

# 의존성 확인
echo "==> 의존성 확인"
if ! python3 -c "import gi; gi.require_version('Gtk','3.0')" 2>/dev/null; then
    echo "   PyGObject(GTK3)가 필요합니다. 설치:"
    echo "   sudo apt install python3-gi gir1.2-gtk-3.0"
    exit 1
fi
command -v pkexec >/dev/null || { echo "   pkexec 필요: sudo apt install policykit-1"; exit 1; }
echo "   OK"

mkdir -p "$BIN_DIR" "$APP_DIR"

# 실행 래퍼 설치
install -m 755 "$SRC_DIR/popremover.py" "$BIN_DIR/popremover"
echo "==> 설치: $BIN_DIR/popremover"

# 데스크톱 항목 설치
sed "s|@BIN@|$BIN_DIR/popremover|g" "$SRC_DIR/popremover.desktop" \
    > "$APP_DIR/popremover.desktop"
chmod 644 "$APP_DIR/popremover.desktop"
update-desktop-database "$APP_DIR" 2>/dev/null || true
echo "==> 설치: $APP_DIR/popremover.desktop"

echo
echo "완료! 앱 목록에서 'PopRemover'를 검색하거나, 터미널에서 'popremover' 실행하세요."
echo "(~/.local/bin 이 PATH에 없다면 로그아웃 후 다시 로그인하세요.)"
