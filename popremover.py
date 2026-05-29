#!/usr/bin/env python3
"""
PopRemover — Pop!_OS 용 GUI 프로그램 제거 도구

설치된 apt 패키지와 flatpak 앱을 한눈에 보고, 검색하고, 선택해서
안전하게(제거 전 영향 패키지를 미리 보여줌) 삭제할 수 있는 GTK3 앱.

의존성: python3-gi (PyGObject), GTK3, pkexec (policykit-1)
"""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, GLib, Gdk, Pango  # noqa: E402

import subprocess  # noqa: E402
import threading  # noqa: E402
import shutil  # noqa: E402
import os  # noqa: E402
import glob  # noqa: E402
import shlex  # noqa: E402


# ---------------------------------------------------------------------------
# 데이터 수집
# ---------------------------------------------------------------------------

def run(cmd, force_c_locale=False):
    """명령을 실행하고 (returncode, stdout, stderr) 반환. 실패해도 예외 안 던짐."""
    env = None
    if force_c_locale:
        import os
        env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, env=env
        )
        return p.returncode, p.stdout, p.stderr
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def human_size(kb):
    """KB(정수) → 사람이 읽는 크기 문자열."""
    try:
        kb = int(kb)
    except (ValueError, TypeError):
        return ""
    if kb < 1024:
        return f"{kb} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


def list_apt_packages():
    """설치된 모든 apt 패키지 목록.

    각 항목: dict(name, version, size_kb, summary, manual)
    """
    # 모든 설치 패키지의 이름/버전/설치크기/요약을 한 번에
    rc, out, _ = run([
        "dpkg-query", "-W",
        "-f=${Package}\t${Version}\t${Installed-Size}\t${binary:Summary}\n",
    ])
    pkgs = {}
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                parts += [""] * (4 - len(parts))
            name, version, size, summary = parts[0], parts[1], parts[2], parts[3]
            if not name:
                continue
            pkgs[name] = {
                "name": name,
                "version": version,
                "size_kb": size,
                "summary": summary,
                "manual": False,
                "kind": "apt",
            }

    # 사용자가 직접 설치한(manual) 패키지 표시
    rc, out, _ = run(["apt-mark", "showmanual"])
    if rc == 0:
        for name in out.split():
            if name in pkgs:
                pkgs[name]["manual"] = True

    return list(pkgs.values())


DESKTOP_DIRS = [
    os.path.expanduser("~/.local/share/applications"),
    "/usr/local/share/applications",
    "/usr/share/applications",
]

# flatpak이 export한 .desktop은 Flatpak 탭에서 다루므로 제외
FLATPAK_EXPORT_DIRS = [
    "/var/lib/flatpak/exports/share/applications",
    os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
]

# 절대 통째로 삭제하면 안 되는 디렉터리 (안전장치)
_BLOCKED_DIRS = {
    "/", "/usr", "/usr/bin", "/usr/sbin", "/usr/local", "/usr/local/bin",
    "/usr/local/share", "/usr/share", "/usr/lib", "/opt", "/bin", "/sbin",
    "/etc", "/var", "/lib", "/lib64", "/home", "/root", "/boot", "/tmp",
    os.path.expanduser("~"),
    os.path.expanduser("~/.local"),
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.local/share"),
    os.path.expanduser("~/.config"),
    os.path.expanduser("~/Downloads"),
    os.path.dirname(os.path.expanduser("~")),
}

# 여러 앱이 공유하는 bin의 부모 디렉터리 — 여기를 "앱 폴더"로 오인하면 안 됨
_SHARED_BIN_PARENTS = {
    "/usr", "/usr/local", "/", os.path.expanduser("~/.local"),
    os.path.expanduser("~"),
}
_SHARED_BIN_DIRS = {
    "/usr/bin", "/usr/local/bin", "/bin", "/sbin", "/usr/sbin",
    os.path.expanduser("~/.local/bin"),
}


def _parse_desktop(path):
    """.desktop 파일에서 [Desktop Entry] 주요 키를 dict로."""
    data = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            in_entry = False
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("["):
                    in_entry = line.strip() == "[Desktop Entry]"
                    continue
                if not in_entry or "=" not in line or line.startswith("#"):
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                # 지역화 키(Name[ko])는 기본 키가 없을 때만 보조로
                if k not in data:
                    data[k] = v.strip()
    except OSError:
        pass
    return data


def _exec_binary(exec_str):
    """Exec= 문자열에서 실제 실행 바이너리 경로(또는 이름)를 추출."""
    if not exec_str:
        return ""
    try:
        toks = shlex.split(exec_str)
    except ValueError:
        toks = exec_str.split()
    i = 0
    # 'env VAR=val ...' 접두 처리
    while i < len(toks) and (toks[i] == "env" or ("=" in toks[i] and "/" not in toks[i].split("=")[0])):
        i += 1
    return toks[i] if i < len(toks) else ""


def _derive_install_dir(binpath):
    """바이너리 경로 → 앱 전용 설치 루트 디렉터리 추정.

    공용 bin(/usr/bin, ~/.local/bin 등)에 있는 바이너리는 전용 폴더가
    없으므로 "" 반환(런처만 삭제, 폴더는 건드리지 않음).
    """
    if not binpath or not binpath.startswith("/"):
        return ""
    if "/bin/" in binpath:
        root = binpath.split("/bin/")[0]
        if root in _SHARED_BIN_PARENTS:   # 예: /home/dell/.local/bin/x → /home/dell/.local
            return ""
        return root
    parent = os.path.dirname(binpath)
    if parent in _SHARED_BIN_DIRS:        # 예: /usr/bin/x
        return ""
    return parent


def _is_safe_delete_dir(d):
    """이 디렉터리를 통째로 rm -rf 해도 되는지(최상위/홈 등 차단)."""
    if not d or not d.startswith("/"):
        return False
    d = os.path.normpath(d)
    if d in _BLOCKED_DIRS:
        return False
    # 너무 얕은 경로(/foo) 차단: 최소 2단계 (/opt/idea)
    if d.count("/") < 2:
        return False
    # 홈의 직속 상위거나 홈 자체 차단
    home = os.path.expanduser("~")
    if d == home or home.startswith(d + "/"):
        return False
    return True


def _dir_size_kb(path):
    """디렉터리 크기(KB). 실패 시 빈 문자열."""
    if not path or not os.path.isdir(path):
        return ""
    rc, out, _ = run(["du", "-sk", path])
    if rc == 0 and out:
        try:
            return out.split()[0]
        except IndexError:
            return ""
    return ""


def list_desktop_apps():
    """apt·flatpak이 관리하지 않는, .desktop으로 등록된 수동 설치 앱.

    (Toolbox/tarball/AppImage/스크립트 설치 등 — IntelliJ 등이 여기 잡힘)
    """
    seen = {}  # desktop 파일 ID(basename) → 파싱 결과 (상위 우선)
    for d in DESKTOP_DIRS:
        if not os.path.isdir(d):
            continue
        for path in sorted(glob.glob(os.path.join(d, "*.desktop"))):
            bid = os.path.basename(path)
            if bid in seen:
                continue  # 상위 우선순위 디렉터리가 이미 차지 → 중복 제거
            ent = _parse_desktop(path)
            if ent.get("Type", "Application") != "Application":
                continue
            if ent.get("NoDisplay", "").lower() == "true":
                continue
            if ent.get("Hidden", "").lower() == "true":
                continue
            ent["_path"] = path
            seen[bid] = ent

    entries = list(seen.values())

    # apt 소유 여부를 한 번의 dpkg -S 로 일괄 판정
    query_paths = set()
    for ent in entries:
        query_paths.add(ent["_path"])
        binpath = _exec_binary(ent.get("Exec", ""))
        if binpath and not binpath.startswith("/"):
            binpath = shutil.which(binpath) or binpath
        ent["_bin"] = binpath
        if binpath.startswith("/"):
            query_paths.add(binpath)

    owned = set()
    if query_paths:
        rc, out, _ = run(["dpkg", "-S"] + sorted(query_paths))
        for line in out.splitlines():
            # 형식: "패키지[, 패키지]: /경로"
            if ": " in line:
                owned.add(line.rsplit(": ", 1)[1].strip())

    apps = []
    for ent in entries:
        path = ent["_path"]
        binpath = ent.get("_bin", "")
        # flatpak export 제외
        if any(path.startswith(fd) for fd in FLATPAK_EXPORT_DIRS):
            continue
        # apt 소유(.desktop이든 바이너리든) 제외 → APT 탭에서 처리
        if path in owned or (binpath and binpath in owned):
            continue
        name = ent.get("Name") or os.path.splitext(os.path.basename(path))[0]
        install_dir = _derive_install_dir(binpath)
        is_jb = "jetbrains" in (path + binpath).lower() or "/idea" in binpath.lower()
        summary = ent.get("Comment", "")
        loc = install_dir or binpath
        apps.append({
            "name": name,
            "version": "",
            "size_kb": "",       # 나중에 du로 채움
            "summary": f"{loc}  —  {summary}".strip(" —"),
            "kind": "desktop",
            "manual": True,
            "desktop_file": path,
            "bin": binpath,
            "install_dir": install_dir,
            "is_jetbrains": is_jb,
            "location": loc,
        })
    return apps


def list_flatpak_apps():
    """설치된 flatpak 앱 목록."""
    if not shutil.which("flatpak"):
        return []
    rc, out, _ = run([
        "flatpak", "list", "--app",
        "--columns=name,application,version,size,installation",
    ])
    apps = []
    if rc == 0:
        for line in out.splitlines():
            cols = line.split("\t")
            if len(cols) < 5:
                continue
            name, appid, version, size, installation = cols[:5]
            apps.append({
                "name": name or appid,
                "version": version,
                "size_kb": "",
                "size_str": size,
                "summary": appid,
                "appid": appid,
                "installation": installation.strip(),
                "manual": True,
                "kind": "flatpak",
            })
    return apps


# ---------------------------------------------------------------------------
# 제거 작업 (확인 + 실행)
# ---------------------------------------------------------------------------

def apt_simulate_remove(names, purge):
    """apt 제거 시뮬레이션. (성공여부, 제거될 패키지 목록, 원문) 반환."""
    op = "purge" if purge else "remove"
    # 로케일과 무관한 'Remv <패키지> [...]' 줄을 파싱 (한국어 등 비영어 로케일 안전)
    rc, out, err = run(["apt-get", "-s", op] + names, force_c_locale=True)
    removed = []
    for line in (out + "\n" + err).splitlines():
        s = line.strip()
        if s.startswith("Remv ") or s.startswith("Purg "):
            parts = s.split()
            if len(parts) >= 2:
                removed.append(parts[1])
    return rc == 0, removed, (out + err)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class OutputDialog(Gtk.Dialog):
    """제거 명령 실행 중 출력을 실시간으로 보여주는 창."""

    def __init__(self, parent, title):
        super().__init__(title=title, transient_for=parent, modal=True)
        self.set_default_size(620, 420)
        self.add_button("닫기", Gtk.ResponseType.CLOSE)
        self.close_btn = self.get_widget_for_response(Gtk.ResponseType.CLOSE)
        self.close_btn.set_sensitive(False)

        box = self.get_content_area()
        box.set_border_width(8)

        self.spinner = Gtk.Spinner()
        self.spinner.start()
        self.status = Gtk.Label(label="제거 중…", xalign=0)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.pack_start(self.spinner, False, False, 0)
        head.pack_start(self.status, True, True, 0)
        box.pack_start(head, False, False, 4)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.view = Gtk.TextView(editable=False, cursor_visible=False)
        self.view.set_monospace(True)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.buffer = self.view.get_buffer()
        sw.add(self.view)
        box.pack_start(sw, True, True, 0)
        self.show_all()

    def append(self, text):
        end = self.buffer.get_end_iter()
        self.buffer.insert(end, text)
        # 자동 스크롤
        mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), False)
        self.view.scroll_to_mark(mark, 0, False, 0, 0)

    def finish(self, ok):
        self.spinner.stop()
        self.spinner.hide()
        self.status.set_text("완료되었습니다." if ok else "오류가 발생했습니다.")
        self.close_btn.set_sensitive(True)


class PopRemover(Gtk.Window):
    def __init__(self):
        super().__init__(title="PopRemover — 프로그램 제거")
        self.set_default_size(880, 600)
        self.set_border_width(0)

        self.all_apt = []
        self.all_flatpak = []
        self.all_other = []

        # 헤더바
        hb = Gtk.HeaderBar(show_close_button=True, title="PopRemover")
        hb.set_subtitle("설치된 프로그램 제거")
        self.set_titlebar(hb)

        self.refresh_btn = Gtk.Button()
        self.refresh_btn.set_image(
            Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON))
        self.refresh_btn.set_tooltip_text("목록 새로고침")
        self.refresh_btn.connect("clicked", lambda *_: self.reload())
        hb.pack_start(self.refresh_btn)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("프로그램 검색…")
        self.search.set_size_request(280, -1)
        self.search.connect("search-changed", lambda *_: self.refilter())
        hb.set_custom_title(self.search)

        self.remove_btn = Gtk.Button(label="선택 항목 제거")
        self.remove_btn.get_style_context().add_class("destructive-action")
        self.remove_btn.connect("clicked", self.on_remove_clicked)
        hb.pack_end(self.remove_btn)

        # 본문
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)

        # 옵션 바
        opts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        opts.set_border_width(8)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        switcher = Gtk.StackSwitcher(stack=self.stack)
        opts.pack_start(switcher, False, False, 0)

        opts.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)

        self.manual_only = Gtk.CheckButton(label="직접 설치한 것만 보기")
        self.manual_only.set_active(True)
        self.manual_only.set_tooltip_text(
            "체크 해제하면 의존성·라이브러리를 포함한 모든 패키지를 표시합니다.\n"
            "라이브러리를 함부로 지우면 시스템이 망가질 수 있으니 주의하세요.")
        self.manual_only.connect("toggled", lambda *_: self.refilter())
        opts.pack_start(self.manual_only, False, False, 0)

        self.purge = Gtk.CheckButton(label="설정 파일까지 완전 삭제(purge)")
        opts.pack_start(self.purge, False, False, 0)

        self.autoremove = Gtk.CheckButton(label="불필요한 의존성도 정리")
        self.autoremove.set_active(True)
        opts.pack_start(self.autoremove, False, False, 0)

        self.count_label = Gtk.Label(label="", xalign=1)
        self.count_label.get_style_context().add_class("dim-label")
        opts.pack_end(self.count_label, False, False, 0)

        root.pack_start(opts, False, False, 0)
        root.pack_start(Gtk.Separator(), False, False, 0)
        root.pack_start(self.stack, True, True, 0)

        # APT / Flatpak / 기타(수동 설치) 세 페이지
        self.apt_store, self.apt_filter, self.apt_view = self._make_page("apt")
        self.flat_store, self.flat_filter, self.flat_view = self._make_page("flatpak")
        self.other_store, self.other_filter, self.other_view = self._make_page("desktop")

        self.stack.add_titled(self._wrap(self.apt_view), "apt", "APT 패키지")
        self.stack.add_titled(self._wrap(self.flat_view), "flatpak", "Flatpak 앱")
        self.stack.add_titled(self._wrap(self.other_view), "desktop",
                              "기타 앱 (수동 설치)")
        self.stack.connect("notify::visible-child", lambda *_: self.update_count())

        self.show_all()
        self.reload()

    # --- 페이지(트리뷰) 생성 ------------------------------------------------

    def _wrap(self, view):
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(view)
        return sw

    def _make_page(self, kind):
        # 컬럼: [선택(bool), 이름, 버전, 크기, 설명, 내부데이터(pyobj)]
        store = Gtk.ListStore(bool, str, str, str, str, object)
        flt = store.filter_new()
        flt.set_visible_func(self._visible_func)
        view = Gtk.TreeView(model=flt)
        view.set_rubber_banding(True)

        # 체크박스
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._on_toggle, flt)
        col = Gtk.TreeViewColumn("", toggle, active=0)
        view.append_column(col)

        def text_col(title, idx, expand=False, ellipsize=False, width=None):
            r = Gtk.CellRendererText()
            if ellipsize:
                r.set_property("ellipsize", Pango.EllipsizeMode.END)
            c = Gtk.TreeViewColumn(title, r, text=idx)
            c.set_resizable(True)
            c.set_sort_column_id(idx)
            if expand:
                c.set_expand(True)
            if width:
                c.set_fixed_width(width)
                c.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            view.append_column(c)

        text_col("이름", 1, width=220)
        text_col("버전", 2, width=140)
        text_col("크기", 3, width=90)
        text_col("설명", 4, expand=True, ellipsize=True)
        return store, flt, view

    # --- 데이터 로드 --------------------------------------------------------

    def reload(self):
        self.refresh_btn.set_sensitive(False)
        self.count_label.set_text("불러오는 중…")

        def work():
            apt = list_apt_packages()
            flat = list_flatpak_apps()
            other = list_desktop_apps()
            # 설치 디렉터리 크기 계산 (앱 수가 적어 부담 없음)
            for p in other:
                p["size_kb"] = _dir_size_kb(p.get("install_dir", ""))
            GLib.idle_add(self._populate, apt, flat, other)

        threading.Thread(target=work, daemon=True).start()

    def _populate(self, apt, flat, other):
        self.all_apt = sorted(apt, key=lambda d: d["name"].lower())
        self.all_flatpak = sorted(flat, key=lambda d: d["name"].lower())
        self.all_other = sorted(other, key=lambda d: d["name"].lower())

        self.apt_store.clear()
        for p in self.all_apt:
            self.apt_store.append([
                False, p["name"], p["version"],
                human_size(p["size_kb"]), p["summary"], p])

        self.flat_store.clear()
        for p in self.all_flatpak:
            self.flat_store.append([
                False, p["name"], p["version"],
                p.get("size_str", ""), p["summary"], p])

        self.other_store.clear()
        for p in self.all_other:
            self.other_store.append([
                False, p["name"], p["version"],
                human_size(p["size_kb"]), p["summary"], p])

        self.refresh_btn.set_sensitive(True)
        self.refilter()
        return False

    # --- 필터링 -------------------------------------------------------------

    def _visible_func(self, model, it, _data):
        data = model[it][5]
        if data is None:
            return True
        # manual_only 필터는 apt에만 적용
        if data["kind"] == "apt" and self.manual_only.get_active() and not data["manual"]:
            return False
        q = self.search.get_text().strip().lower()
        if not q:
            return True
        hay = f"{data['name']} {data['summary']}".lower()
        return q in hay

    def refilter(self):
        self.apt_filter.refilter()
        self.flat_filter.refilter()
        self.other_filter.refilter()
        self.update_count()

    def _current_filter(self):
        return {
            "apt": self.apt_filter,
            "flatpak": self.flat_filter,
            "desktop": self.other_filter,
        }.get(self.stack.get_visible_child_name(), self.apt_filter)

    def update_count(self):
        flt = self._current_filter()
        shown = len(flt)
        sel = sum(1 for row in flt if row[0])
        self.count_label.set_text(f"{shown}개 표시 · {sel}개 선택")

    def _on_toggle(self, _renderer, path, flt):
        flt[path][0] = not flt[path][0]
        self.update_count()

    # --- 선택 수집 ----------------------------------------------------------

    def _selected(self, flt):
        return [row[5] for row in flt if row[0]]

    # --- 제거 ---------------------------------------------------------------

    def on_remove_clicked(self, _btn):
        name = self.stack.get_visible_child_name()
        if name == "apt":
            self._remove_apt()
        elif name == "flatpak":
            self._remove_flatpak()
        else:
            self._remove_desktop()

    def _remove_apt(self):
        items = self._selected(self.apt_filter)
        if not items:
            self._info("선택된 패키지가 없습니다.")
            return
        names = [d["name"] for d in items]
        purge = self.purge.get_active()

        self.count_label.set_text("영향 분석 중…")

        def work():
            ok, removed, raw = apt_simulate_remove(names, purge)
            GLib.idle_add(self._confirm_apt, names, removed, ok, raw, purge)

        threading.Thread(target=work, daemon=True).start()

    def _confirm_apt(self, names, removed, ok, raw, purge):
        self.update_count()
        if not ok and not removed:
            self._info("제거 시뮬레이션에 실패했습니다.\n\n" + raw[-1500:])
            return

        extra = sorted(set(removed) - set(names))
        msg = "다음 패키지를 제거합니다:\n\n  • " + "\n  • ".join(sorted(names))
        if extra:
            msg += ("\n\n함께 제거되는 의존 패키지:\n\n  • "
                    + "\n  • ".join(extra))
            msg += "\n\n⚠️ 함께 제거되는 항목이 있습니다. 꼭 확인하세요."

        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="정말 제거하시겠습니까?")
        dlg.format_secondary_text(msg)
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return

        op = "purge" if purge else "remove"
        cmd = ["pkexec", "apt-get", op, "-y"] + names
        if self.autoremove.get_active():
            # 제거 후 자동정리 — 별도 명령 대신 동일 호출에 묶기 어려워 순차 실행
            self._run_command(cmd, follow=["pkexec", "apt-get", "autoremove", "-y"])
        else:
            self._run_command(cmd)

    def _remove_flatpak(self):
        items = self._selected(self.flat_filter)
        if not items:
            self._info("선택된 앱이 없습니다.")
            return
        appids = [d["appid"] for d in items]
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="다음 Flatpak 앱을 제거하시겠습니까?")
        dlg.format_secondary_text("  • " + "\n  • ".join(appids))
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        # system 설치는 pkexec 필요, user 설치는 불필요. 안전하게 둘 다 시도하도록 -y.
        needs_root = any(d.get("installation", "") == "system" for d in items)
        base = (["pkexec"] if needs_root else []) + ["flatpak", "uninstall", "-y"]
        self._run_command(base + appids)

    def _remove_desktop(self):
        items = self._selected(self.other_filter)
        if not items:
            self._info("선택된 앱이 없습니다.")
            return

        targets = []          # 실제로 삭제할 경로
        lines = []            # 확인창에 보여줄 설명
        unsafe = []           # 설치 폴더 자동삭제가 위험해 건너뛴 항목
        jetbrains = False

        for d in items:
            lines.append(f"▸ {d['name']}")
            dfile = d.get("desktop_file")
            if dfile:
                lines.append(f"    런처: {dfile}")
                targets.append(dfile)
            idir = d.get("install_dir", "")
            if idir and _is_safe_delete_dir(idir):
                sz = human_size(d.get("size_kb")) or "?"
                lines.append(f"    폴더: {idir}  ({sz})")
                targets.append(idir)
            elif idir:
                unsafe.append(idir)
                lines.append(f"    폴더: {idir}  ← ⚠️ 자동 삭제하지 않음(위험 경로)")
            if d.get("is_jetbrains"):
                jetbrains = True

        msg = "\n".join(lines)
        msg += "\n\n위 런처/폴더를 삭제합니다. 이 작업은 되돌릴 수 없습니다."
        if jetbrains:
            msg += ("\n\n💡 JetBrains 앱은 가능하면 'JetBrains Toolbox'에서 "
                    "제거하는 것이 가장 깔끔합니다.")
        if unsafe:
            msg += ("\n\n⚠️ 일부 폴더는 위험 경로라 자동 삭제하지 않습니다. "
                    "런처만 지워집니다.")

        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="다음 항목을 삭제하시겠습니까?")
        dlg.format_secondary_text(msg)
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        if not targets:
            self._info("삭제할 안전한 대상이 없습니다.")
            return

        # 홈 밖의 경로가 하나라도 있으면 root 권한(pkexec) 필요
        home = os.path.expanduser("~")
        need_root = any(not t.startswith(home + "/") for t in targets)
        cmd = (["pkexec"] if need_root else []) + ["rm", "-rf", "--"] + targets
        # 메뉴 캐시 갱신까지 이어서 실행
        self._run_command(cmd, follow=[
            "update-desktop-database",
            os.path.expanduser("~/.local/share/applications")])

    def _run_command(self, cmd, follow=None):
        dlg = OutputDialog(self, "제거 진행")
        dlg.append("$ " + " ".join(cmd) + "\n\n")

        def work():
            ok = self._stream(cmd, dlg)
            if ok and follow:
                GLib.idle_add(dlg.append, "\n$ " + " ".join(follow) + "\n\n")
                ok = self._stream(follow, dlg)
            GLib.idle_add(dlg.finish, ok)
            GLib.idle_add(self.reload)

        threading.Thread(target=work, daemon=True).start()
        dlg.run()
        dlg.destroy()

    def _stream(self, cmd, dlg):
        try:
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
        except Exception as e:  # noqa: BLE001
            GLib.idle_add(dlg.append, f"실행 실패: {e}\n")
            return False
        for line in p.stdout:
            GLib.idle_add(dlg.append, line)
        p.wait()
        GLib.idle_add(dlg.append, f"\n[종료 코드 {p.returncode}]\n")
        return p.returncode == 0

    def _info(self, text):
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK, text=text)
        dlg.run()
        dlg.destroy()


def main():
    win = PopRemover()
    win.connect("destroy", Gtk.main_quit)

    # 약간의 스타일
    css = b"""
    .destructive-action { }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    Gtk.main()


if __name__ == "__main__":
    main()
