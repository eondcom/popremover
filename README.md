# PopRemover

Pop!_OS / Ubuntu 용 **GUI 프로그램 제거 도구**.

Pop!_Shop(Pop!_Shop 스토어)은 자기가 관리하는 일부 앱만 보여주기 때문에,
터미널이나 `.deb` 파일로 설치한 프로그램은 거기서 제거할 수 없습니다.
PopRemover는 `dpkg`로 **시스템에 설치된 모든 apt 패키지**와 **flatpak 앱**을
직접 읽어와서, 검색하고 선택해서 안전하게 제거합니다.

## 화면 구성

- **APT 패키지 / Flatpak 앱** 탭 전환
- 상단 검색창으로 이름·설명 즉시 필터
- 체크박스로 여러 개 선택 후 한 번에 제거
- **직접 설치한 것만 보기**(기본 켜짐): 라이브러리·의존성을 숨겨 실제 "프로그램"만 표시
- **purge**: 설정 파일까지 완전 삭제
- **불필요한 의존성도 정리**: 제거 후 `apt autoremove` 자동 실행

## 안전장치

- 제거 전 `apt-get -s`(시뮬레이션)로 **함께 삭제되는 패키지를 미리 보여주고** 확인을 받습니다.
  의존성 때문에 다른 게 딸려 삭제되는 사고를 막습니다.
- root 권한이 필요한 작업은 `pkexec`로 그래픽 비밀번호 창을 띄웁니다.
- 진행 상황(로그)을 실시간으로 보여줍니다.

## 의존성

- `python3-gi` (PyGObject), GTK3 — Pop!_OS에 기본 포함
- `pkexec` (policykit-1) — 기본 포함
- (선택) `flatpak`

부족하면:
```bash
sudo apt install python3-gi gir1.2-gtk-3.0 policykit-1
```

## 설치

```bash
cd ~/popremover
./install.sh
```

앱 목록에서 **PopRemover**(또는 "프로그램 제거")를 검색하거나, 터미널에서:
```bash
popremover
```

## 그냥 한 번 실행

설치 없이 바로:
```bash
python3 ~/popremover/popremover.py
```

## 제거(이 도구 자체)

```bash
rm ~/.local/bin/popremover ~/.local/share/applications/popremover.desktop
```
