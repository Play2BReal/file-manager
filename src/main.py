"""File browser, FTP, web upload, and .deb install for the Cardputer Zero."""
import os
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

import cz
from debinfo import DebError, read_control
from servers import FtpServer, HttpServer, install_deb, lan_ip, make_folder, remove_path, rename_path

BG = (14, 16, 24)
HEADER = (28, 32, 48)
FG = (232, 234, 240)
DIM = (140, 146, 166)
ACCENT = (255, 154, 60)
DIRC = (130, 176, 255)
SEL = (54, 46, 32)
RED = (230, 96, 96)
GREEN = (130, 200, 150)
LINE = (50, 54, 72)

FACE = cz.font(12)
FACE_B = cz.font(13, bold=True)
FACE_S = cz.font(11)

VISIBLE = 7
ROW_H = 16
LIST_Y = 38
FOOT_Y = 154
ESC_HOLD = 3.0

SUDO = "/usr/bin/sudo"


def human(size):
    size = int(size)
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} K"
    return f"{size / 1024 / 1024:.1f} M"


def load_config():
    defaults = {"user": "card", "password": "cardputer", "ftp_port": 2121, "http_port": 8081}
    path = Path.home() / ".config" / "cz-files" / "config"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            save_config(defaults)
        text = path.read_text(encoding="utf-8")
    except OSError:
        return dict(defaults)
    cfg = dict(defaults)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        cfg[key.strip()] = value.strip()
    for key in ("ftp_port", "http_port"):
        try:
            port = int(cfg[key])
        except (TypeError, ValueError):
            port = defaults[key]
        if not 1 <= port <= 65535:
            port = defaults[key]
        cfg[key] = port
    # 8080 is raspyjack. The first build saved that port, so move it.
    if cfg["http_port"] == 8080:
        cfg["http_port"] = 8081
        try:
            save_config(cfg)
        except OSError:
            pass
    return cfg


def save_config(cfg):
    path = Path.home() / ".config" / "cz-files" / "config"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "user={user}\npassword={password}\nftp_port={ftp_port}\nhttp_port={http_port}\n".format(**cfg),
        encoding="utf-8",
    )


def apply_field(cfg, key, raw):
    if key in ("ftp_port", "http_port"):
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return None, "port must be a number"
        if not 1 <= value <= 65535:
            return None, "port must be 1-65535"
        if key == "http_port" and value == 8080:
            return None, "8080 is used by raspyjack"
        other = cfg["http_port"] if key == "ftp_port" else cfg["ftp_port"]
        if value == other:
            return None, "ftp and web need different ports"
    elif key in ("user", "password"):
        value = str(raw)
        if not value or any(ch in value for ch in "\n\r"):
            return None, "user and password cannot be empty"
    else:
        return None, "unknown field"
    updated = dict(cfg)
    updated[key] = value
    return updated, ""


# Linux evdev codes. Shift uses the US symbols on the number row.
_KEY_CHAR = {
    2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8", 10: "9", 11: "0",
    12: "-", 13: "=", 16: "q", 17: "w", 18: "e", 19: "r", 20: "t", 21: "y", 22: "u", 23: "i",
    24: "o", 25: "p", 26: "[", 27: "]", 30: "a", 31: "s", 32: "d", 33: "f", 34: "g", 35: "h",
    36: "j", 37: "k", 38: "l", 39: ";", 40: "'", 41: "`", 43: "\\", 44: "z", 45: "x", 46: "c",
    47: "v", 48: "b", 49: "n", 50: "m", 51: ",", 52: ".", 53: "/", 57: " ",
}
_KEY_SHIFT = {
    "1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^", "7": "&", "8": "*",
    "9": "(", "0": ")", "-": "_", "=": "+", "[": "{", "]": "}", ";": ":", "'": '"',
    "`": "~", "\\": "|", ",": "<", ".": ">", "/": "?",
}
_SHIFT_KEYS = {42, 54}
NET_FIELDS = (("user", "user"), ("password", "pass"), ("ftp_port", "ftp"), ("http_port", "web"))


def key_char(code, shift):
    ch = _KEY_CHAR.get(code)
    if not ch:
        return None
    if shift and ch.isalpha():
        return ch.upper()
    if shift and ch in _KEY_SHIFT:
        return _KEY_SHIFT[ch]
    return ch


def read_battery():
    root = Path("/sys/class/power_supply")
    try:
        names = [entry.name for entry in root.iterdir()]
    except OSError:
        return None
    names.sort(key=lambda name: (0 if "bq27220" in name.lower() else 1, name))
    for name in names:
        node = root / name
        try:
            pct = int((node / "capacity").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        status = ""
        try:
            status = (node / "status").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if pct < 0:
            pct = 0
        if pct > 100:
            pct = 100
        return pct, status
    return None


def stop_server(server):
    stop = getattr(server, "stop", None)
    if stop is not None:
        stop()
    if getattr(server, "ident", None) is not None:
        server.join(timeout=1)


def passwordless_sudo():
    if not os.path.exists(SUDO):
        return False, "sudo is not installed"
    try:
        result = subprocess.run(
            [SUDO, "-n", "true"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout or "sudo rejected the command").strip()
    return False, err.splitlines()[-1] if err else "sudo rejected the command"


class FilesApp:
    def __init__(self, here, incoming, ftp, http, activity, sudo_ok=False, sudo_reason="", cfg=None):
        self.here = Path(here)
        self.incoming = Path(incoming)
        self.ftp = ftp
        self.http = http
        self.activity = activity
        self.cfg = cfg if cfg is not None else load_config()
        self.sudo_ok = sudo_ok
        self.sudo_reason = sudo_reason
        self.running = True
        self.busy = False
        self.mode = "browse"
        self.cursor = 0
        self.scroll = 0
        self.entries = []
        self.list_error = ""
        self.pending_action = ""
        self.pending_path = None
        self.deb_fields = None
        self.deb_error = ""
        self.log_title = ""
        self.log_ok = None
        self.log_lines = []
        self.menu = []
        self.menu_cursor = 0
        self.esc_at = None
        self.esc_fired = False
        self.net_row = 0
        self.editing = False
        self.edit_buf = ""
        self.shift = False
        self.net_note = ""
        self.sudo_password = ""
        self.name_action = ""
        self.name_path = None
        self._ip = ""
        self._ip_at = 0.0
        self._batt = None
        self._batt_at = 0.0

    def selected(self):
        if not self.entries or self.cursor >= len(self.entries):
            return None
        return self.entries[self.cursor]

    def reload(self):
        previous = None
        if self.entries and self.cursor < len(self.entries):
            previous = self.entries[self.cursor][0]
        try:
            items = []
            for path in self.here.iterdir():
                try:
                    is_dir = path.is_dir()
                    size = 0 if is_dir else path.stat().st_size
                except OSError:
                    is_dir = False
                    size = 0
                is_deb = (not is_dir) and path.name.lower().endswith(".deb")
                items.append((path.name, is_dir, size, is_deb))
            items.sort(key=lambda item: (not item[1], item[0].lower()))
            self.entries = items
            self.list_error = ""
        except OSError as exc:
            self.entries = []
            self.list_error = str(exc)
        self.cursor = 0
        if previous:
            for index, item in enumerate(self.entries):
                if item[0] == previous:
                    self.cursor = index
                    break
        self.clamp()

    def clamp(self):
        if self.entries and self.cursor >= len(self.entries):
            self.cursor = len(self.entries) - 1
        if self.cursor < 0:
            self.cursor = 0
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        if self.cursor >= self.scroll + VISIBLE:
            self.scroll = self.cursor - VISIBLE + 1

    def ip(self):
        now = time.time()
        if now - self._ip_at > 3:
            self._ip = lan_ip()
            self._ip_at = now
        return self._ip

    def on_event(self, code, value, screen):
        if self.busy:
            return
        if code in _SHIFT_KEYS:
            self.shift = value != 0
            return
        if code == cz.KEY_ESC:
            self.on_esc(value)
            return
        if value not in (1, 2):
            return
        if self.editing:
            if code in cz.OK_KEYS and value != 1:
                return
            self.edit_key(code)
            return
        if self.mode == "net" and code in cz.OK_KEYS and value != 1:
            return
        if self.mode == "sudo":
            if code in cz.OK_KEYS and value != 1:
                return
            self.key_sudo(code, screen)
            return
        if self.mode == "name":
            if code in cz.OK_KEYS and value != 1:
                return
            self.key_name(code)
            return
        if code == cz.KEY_BACKSPACE and value != 1:
            return
        if code == cz.KEY_BACKSPACE:
            self.short_back()
            return
        self.on_key(code, screen)

    def on_esc(self, value):
        if value == 1:
            self.esc_at = time.time()
            self.esc_fired = False
            return
        if value == 0:
            held = self.esc_at is not None and time.time() - self.esc_at >= ESC_HOLD
            if held or self.esc_fired:
                self.running = False
            elif self.editing:
                self.editing = False
                self.edit_buf = ""
            else:
                self.short_back()
            self.esc_at = None
            self.esc_fired = False

    def poll_esc(self):
        # This keyboard does not autorepeat Esc, so a hold has to be timed here.
        if self.busy or self.esc_fired or self.esc_at is None:
            return
        if time.time() - self.esc_at >= ESC_HOLD:
            self.esc_fired = True
            self.running = False

    def short_back(self):
        if self.mode == "sudo":
            self.sudo_password = ""
            self.mode = "confirm"
            return
        if self.mode == "name":
            self.edit_buf = ""
            self.name_action = ""
            self.name_path = None
            self.mode = "browse"
            return
        if self.mode != "browse":
            self.mode = "browse"
            return
        if self.here.resolve() != Path("/").resolve():
            self.here = self.here.parent
            self.cursor = 0
            self.scroll = 0

    def on_key(self, code, screen):
        if self.mode == "browse":
            self.key_browse(code, screen)
        elif self.mode == "menu":
            self.key_menu(code, screen)
        elif self.mode == "confirm":
            self.key_confirm(code, screen)
        elif self.mode == "log":
            if code in cz.OK_KEYS:
                self.mode = "browse"
        elif self.mode == "net":
            self.key_net(code)

    def begin_name(self, action, path=None, start=""):
        self.name_action = action
        self.name_path = path
        self.edit_buf = start
        self.editing = False
        self.mode = "name"

    def key_browse(self, code, screen):
        if code in cz.MENU_KEYS:
            self.open_menu()
            return
        if code in cz.NET_KEYS:
            self.mode = "net"
            return
        if code == cz.KEY_M:
            self.begin_name("mkdir")
            return
        if code == cz.KEY_R:
            item = self.selected()
            if item:
                self.begin_name("rename", self.here / item[0], item[0])
            return
        if code in cz.UP_KEYS and self.entries:
            self.cursor = (self.cursor - 1) % len(self.entries)
            self.clamp()
            return
        if code in cz.DOWN_KEYS and self.entries:
            self.cursor = (self.cursor + 1) % len(self.entries)
            self.clamp()
            return
        if code == cz.KEY_PAGEUP:
            self.step(-VISIBLE)
            return
        if code == cz.KEY_PAGEDOWN:
            self.step(VISIBLE)
            return
        if code == cz.KEY_HOME and self.entries:
            self.cursor = 0
            self.clamp()
            return
        if code == cz.KEY_END and self.entries:
            self.cursor = len(self.entries) - 1
            self.clamp()
            return
        if code in cz.LEFT_KEYS:
            self.short_back()
            return
        if code == cz.KEY_H:
            self.here = self.incoming
            self.cursor = 0
            self.scroll = 0
            return
        item = self.selected()
        if item is None:
            return
        path = self.here / item[0]
        if code in (cz.KEY_D, cz.KEY_DELETE):
            self.pending_action = "delete"
            self.pending_path = path
            self.deb_error = ""
            self.mode = "confirm"
            return
        if code not in cz.OK_KEYS and code not in cz.RIGHT_KEYS:
            return
        if item[1]:
            self.here = path
            self.cursor = 0
            self.scroll = 0
            return
        if item[3]:
            self.begin_install(path)

    def step(self, delta):
        if not self.entries:
            return
        self.cursor = max(0, min(len(self.entries) - 1, self.cursor + delta))
        self.clamp()

    def open_menu(self):
        item = self.selected()
        options = []
        if item and item[1]:
            options.append(("Open", "open"))
        if item and item[3]:
            options.append(("Install", "install"))
        if item:
            options.append(("Rename", "rename"))
            options.append(("Delete", "delete"))
        options.append(("New folder", "mkdir"))
        options.append(("Network", "net"))
        options.append(("Uploads", "uploads"))
        self.menu = options
        self.menu_cursor = 0
        self.mode = "menu"

    def key_menu(self, code, screen):
        if code in cz.LEFT_KEYS:
            self.mode = "browse"
            return
        if code in cz.UP_KEYS and self.menu:
            self.menu_cursor = (self.menu_cursor - 1) % len(self.menu)
            return
        if code in cz.DOWN_KEYS and self.menu:
            self.menu_cursor = (self.menu_cursor + 1) % len(self.menu)
            return
        if code in cz.OK_KEYS or code in cz.RIGHT_KEYS:
            self.run_menu()

    def run_menu(self):
        if not self.menu:
            self.mode = "browse"
            return
        action = self.menu[self.menu_cursor][1]
        item = self.selected()
        path = self.here / item[0] if item else None
        if action == "open" and path is not None:
            self.here = path
            self.cursor = 0
            self.scroll = 0
            self.mode = "browse"
        elif action == "install" and path is not None:
            self.begin_install(path)
        elif action == "rename" and path is not None:
            self.begin_name("rename", path, path.name)
        elif action == "delete" and path is not None:
            self.pending_action = "delete"
            self.pending_path = path
            self.deb_error = ""
            self.mode = "confirm"
        elif action == "mkdir":
            self.begin_name("mkdir")
        elif action == "net":
            self.mode = "net"
        elif action == "uploads":
            self.here = self.incoming
            self.cursor = 0
            self.scroll = 0
            self.mode = "browse"

    def begin_install(self, path):
        self.pending_action = "install"
        self.pending_path = path
        self.deb_fields = None
        self.deb_error = ""
        try:
            self.deb_fields = read_control(path)
        except DebError as exc:
            self.deb_error = str(exc)
        self.mode = "confirm"

    def key_confirm(self, code, screen):
        if code in cz.LEFT_KEYS:
            self.mode = "browse"
            return
        if code not in cz.OK_KEYS:
            return
        if self.pending_action == "delete":
            self.do_delete()
            return
        if self.deb_error:
            return
        self.editing = False
        self.sudo_password = ""
        self.do_install(screen)

    def do_delete(self):
        path = self.pending_path
        try:
            remove_path(path)
            self.log_title = "Deleted"
            self.log_ok = True
            self.log_lines = [path.name]
        except OSError as exc:
            self.log_title = "Failed"
            self.log_ok = False
            self.log_lines = [str(exc)]
        self.mode = "log"

    def do_install(self, screen):
        path = self.pending_path
        self.busy = True
        self.mode = "log"
        self.log_ok = None
        self.log_title = "Working"
        self.log_lines = ["Installing " + path.name, "Do not power off"]
        screen.show(self.draw())
        log_path = Path.home() / ".cache" / "cz-files" / "last-install.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(log_path, "w", encoding="utf-8")
        except OSError:
            log_handle = None

        def show(lines):
            if log_handle is not None:
                log_handle.seek(0)
                log_handle.truncate()
                log_handle.write("\n".join(lines) + "\n")
                log_handle.flush()
            self.log_lines = lines
            screen.show(self.draw())

        password = self.sudo_password
        self.sudo_password = ""
        try:
            ok, lines, needs_password = install_deb(path, show, password)
            if needs_password:
                self.sudo_password = ""
                self.log_lines = lines
                self.log_ok = False
                self.log_title = "Failed"
                self.mode = "sudo"
                screen.show(self.draw())
                return
            self.log_ok = ok
            self.log_title = "Installed" if ok else "Failed"
            self.log_lines = lines
            screen.show(self.draw())
        finally:
            password = ""
            if log_handle is not None:
                log_handle.close()
            self.busy = False

    def draw(self):
        img = Image.new("RGB", (cz.W, cz.H), BG)
        draw = ImageDraw.Draw(img)
        if self.mode == "browse":
            self.draw_browse(draw)
        elif self.mode == "menu":
            self.draw_menu(draw)
        elif self.mode == "confirm":
            self.draw_confirm(draw)
        elif self.mode == "sudo":
            self.draw_sudo(draw)
        elif self.mode == "name":
            self.draw_name(draw)
        elif self.mode == "log":
            self.draw_log(draw)
        else:
            self.draw_net(draw)
        self.hold_bar(draw)
        return img

    def hold_bar(self, draw):
        if self.busy or self.esc_at is None:
            return
        filled = (time.time() - self.esc_at) / ESC_HOLD
        if filled < 0:
            filled = 0
        if filled > 1:
            filled = 1
        y = cz.H - 5
        draw.rectangle((0, y, cz.W, cz.H), fill=(36, 38, 50))
        width = int(cz.W * filled)
        if width:
            draw.rectangle((0, y, width, cz.H), fill=ACCENT)

    def header(self, draw, title, right="", right_color=DIM):
        draw.rectangle((0, 0, cz.W, 20), fill=HEADER)
        draw.text((6, 3), title, font=FACE_B, fill=ACCENT)
        batt, batt_color = self.battery_mark()
        batt_w = cz.text_width(draw, batt, FACE_S)
        draw.text((cz.W - 6 - batt_w, 4), batt, font=FACE_S, fill=batt_color)
        if not right:
            return
        room = cz.W - 18 - batt_w - cz.text_width(draw, title, FACE_B)
        shown = cz.fit(draw, right, FACE_S, max(room, 20))
        width = cz.text_width(draw, shown, FACE_S)
        x = cz.W - 12 - batt_w - width
        if x > 10 + cz.text_width(draw, title, FACE_B):
            draw.text((x, 4), shown, font=FACE_S, fill=right_color)

    def battery_mark(self):
        now = time.time()
        if now >= self._batt_at:
            self._batt = read_battery()
            self._batt_at = now + 5
        if not self._batt:
            return "--", DIM
        pct, status = self._batt
        label = f"{pct}%"
        if str(status).lower() == "charging":
            label += "+"
        if pct <= 15:
            color = RED
        elif pct <= 40:
            color = ACCENT
        else:
            color = GREEN
        return label, color

    def footer(self, draw, text, color=DIM):
        draw.line((0, FOOT_Y - 2, cz.W, FOOT_Y - 2), fill=LINE)
        draw.text((6, FOOT_Y), cz.fit(draw, text, FACE_S, cz.W - 12), font=FACE_S, fill=color)

    def net_ok(self):
        return not self.ftp.error and not self.http.error

    def draw_browse(self, draw):
        self.reload()
        address = self.ip() or "no network"
        if not self.net_ok():
            address = "net err"
        self.header(draw, "Loader", address, GREEN if self.net_ok() and self.ip() else RED)
        shown = self.list_error or str(self.here)
        draw.text((6, 22), cz.fit(draw, shown, FACE_S, cz.W - 12), font=FACE_S, fill=RED if self.list_error else DIM)
        if not self.entries and not self.list_error:
            draw.text((6, LIST_Y), "(empty)", font=FACE, fill=DIM)
        for row, item in enumerate(self.entries[self.scroll : self.scroll + VISIBLE]):
            name, is_dir, size, is_deb = item
            y = LIST_Y + row * ROW_H
            index = self.scroll + row
            if index == self.cursor:
                draw.rectangle((0, y, cz.W, y + ROW_H - 1), fill=SEL)
            if is_deb:
                tag, color = "DEB", ACCENT
            elif is_dir:
                tag, color = "DIR", DIRC
            else:
                tag, color = "FIL", FG
            draw.text((4, y + 1), tag, font=FACE_S, fill=color)
            size_text = "" if is_dir else human(size)
            size_w = cz.text_width(draw, size_text, FACE_S) if size_text else 0
            name_w = cz.W - 40 - size_w - 8
            draw.text((36, y + 1), cz.fit(draw, name, FACE, name_w), font=FACE, fill=FG)
            if size_text:
                draw.text((cz.W - 6 - size_w, y + 2), size_text, font=FACE_S, fill=DIM)
        self.footer(draw, self.browse_hint(), ACCENT if self.fresh_upload() else DIM)

    def fresh_upload(self):
        return bool(self.activity.get("text")) and time.time() - self.activity.get("at", 0) < 8

    def browse_hint(self):
        if self.fresh_upload():
            return self.activity["text"]
        item = self.selected()
        if item and item[3]:
            return "OK install  r rename  d del  Tab"
        if item and item[1]:
            return "OK open  r rename  m folder  Tab"
        if item:
            return "r rename  d del  m folder  Tab"
        return "m folder   F/X move   Tab menu"

    def draw_confirm(self, draw):
        path = self.pending_path
        if self.pending_action == "delete":
            self.header(draw, "Delete", "")
            kind = "folder" if path.is_dir() and not path.is_symlink() else "file"
            draw.text((8, 32), f"Delete this {kind}?", font=FACE_B, fill=FG)
            draw.text((8, 56), cz.fit(draw, path.name, FACE, cz.W - 16), font=FACE, fill=ACCENT)
            try:
                size = human(path.stat().st_size)
            except OSError:
                size = ""
            draw.text((8, 76), size, font=FACE_S, fill=DIM)
            self.footer(draw, "OK delete    Esc cancel")
            return
        self.header(draw, "Install", "")
        draw.text((8, 28), "Install this package?", font=FACE_B, fill=FG)
        if self.deb_error:
            draw.text((8, 54), cz.fit(draw, self.deb_error, FACE, cz.W - 16), font=FACE, fill=RED)
            self.footer(draw, "Esc back")
            return
        fields = self.deb_fields or {}
        package = fields.get("Package", path.name)
        version = fields.get("Version", "")
        arch = fields.get("Architecture", "")
        draw.text((8, 50), cz.fit(draw, package, FACE, cz.W - 16), font=FACE, fill=FG)
        draw.text((8, 68), cz.fit(draw, version, FACE_S, cz.W - 16), font=FACE_S, fill=DIM)
        try:
            size = human(path.stat().st_size)
        except OSError:
            size = ""
        arch_text = f"{arch}   {size}".strip()
        bad_arch = arch not in ("", "all", "arm64", "aarch64", "any")
        draw.text((8, 86), arch_text, font=FACE_S, fill=RED if bad_arch else DIM)
        y = 106
        if bad_arch:
            draw.text((8, y), "This device is arm64", font=FACE_S, fill=RED)
        self.footer(draw, "OK install    Esc cancel")

    def draw_sudo(self, draw):
        self.header(draw, "sudo", "")
        draw.text((8, 36), "sudo asked for a password", font=FACE_B, fill=FG)
        shown = ("*" * len(self.sudo_password)) + "_"
        draw.text((8, 64), cz.fit(draw, shown, FACE, cz.W - 16), font=FACE, fill=ACCENT)
        name = self.pending_path.name if self.pending_path else ""
        draw.text((8, 92), cz.fit(draw, name, FACE_S, cz.W - 16), font=FACE_S, fill=DIM)
        self.footer(draw, "OK install   Esc cancel")

    def key_sudo(self, code, screen):
        if code in cz.OK_KEYS:
            if not self.sudo_password:
                return
            self.do_install(screen)
            return
        if code == cz.KEY_BACKSPACE:
            self.sudo_password = self.sudo_password[:-1]
            return
        ch = key_char(code, self.shift)
        if not ch or len(self.sudo_password) >= 128:
            return
        self.sudo_password += ch

    def draw_name(self, draw):
        title = "New folder" if self.name_action == "mkdir" else "Rename"
        self.header(draw, title, "")
        prompt = "Folder name" if self.name_action == "mkdir" else "New name"
        draw.text((8, 36), prompt, font=FACE_B, fill=FG)
        shown = self.edit_buf + "_"
        draw.text((8, 64), cz.fit(draw, shown, FACE, cz.W - 16), font=FACE, fill=ACCENT)
        if self.name_action == "rename" and self.name_path is not None:
            draw.text((8, 92), cz.fit(draw, self.name_path.name, FACE_S, cz.W - 16), font=FACE_S, fill=DIM)
        else:
            draw.text((8, 92), cz.fit(draw, str(self.here), FACE_S, cz.W - 16), font=FACE_S, fill=DIM)
        self.footer(draw, "OK save   Esc cancel")

    def key_name(self, code):
        if code in cz.OK_KEYS:
            self.commit_name()
            return
        if code == cz.KEY_BACKSPACE:
            self.edit_buf = self.edit_buf[:-1]
            return
        ch = key_char(code, self.shift)
        if not ch or len(self.edit_buf) >= 200:
            return
        self.edit_buf += ch

    def commit_name(self):
        action = self.name_action
        path = self.name_path
        name = self.edit_buf
        self.name_action = ""
        self.name_path = None
        self.edit_buf = ""
        try:
            if action == "mkdir":
                dest = make_folder(self.here, name)
                self.log_title = "Created"
                self.log_ok = True
                self.log_lines = [dest.name]
            elif action == "rename" and path is not None:
                dest = rename_path(path, name)
                self.log_title = "Renamed"
                self.log_ok = True
                self.log_lines = [dest.name]
            else:
                self.mode = "browse"
                return
        except (OSError, ValueError, FileExistsError) as exc:
            self.log_title = "Failed"
            self.log_ok = False
            self.log_lines = [str(exc)]
        self.mode = "log"

    def draw_log(self, draw):
        color = DIM if self.log_ok is None else GREEN if self.log_ok else RED
        self.header(draw, self.log_title or "Working", "")
        visual = wrap_lines(draw, self.log_lines, FACE_S, cz.W - 12, 8)
        for index, line in enumerate(visual):
            draw.text((6, 26 + index * 15), line, font=FACE_S, fill=FG)
        if self.log_ok is None:
            self.footer(draw, "Working. Do not power off.", ACCENT)
        else:
            self.footer(draw, "Esc back", color)

    def draw_menu(self, draw):
        self.header(draw, "Menu", "")
        for row, (label, _action) in enumerate(self.menu[:VISIBLE]):
            y = LIST_Y + row * ROW_H
            if row == self.menu_cursor:
                draw.rectangle((0, y, cz.W, y + ROW_H - 1), fill=SEL)
            draw.text((8, y + 1), label, font=FACE, fill=FG)
        self.footer(draw, "F/X move   OK select   Esc back")

    def key_net(self, code):
        if code in cz.UP_KEYS:
            self.net_row = (self.net_row - 1) % len(NET_FIELDS)
            return
        if code in cz.DOWN_KEYS:
            self.net_row = (self.net_row + 1) % len(NET_FIELDS)
            return
        if code in cz.OK_KEYS:
            key = NET_FIELDS[self.net_row][0]
            self.edit_buf = str(self.cfg[key])
            self.editing = True
            self.net_note = ""
            return
        if code in cz.NET_KEYS or code in cz.MENU_KEYS or code in cz.LEFT_KEYS:
            self.mode = "browse"

    def edit_key(self, code):
        if code in cz.OK_KEYS:
            self.commit_edit()
            return
        if code == cz.KEY_BACKSPACE:
            self.edit_buf = self.edit_buf[:-1]
            return
        ch = key_char(code, self.shift)
        if not ch:
            return
        key = NET_FIELDS[self.net_row][0]
        if key.endswith("_port") and not ch.isdigit():
            return
        limit = 5 if key.endswith("_port") else 32
        if len(self.edit_buf) >= limit:
            return
        self.edit_buf += ch

    def commit_edit(self):
        key = NET_FIELDS[self.net_row][0]
        updated, error = apply_field(self.cfg, key, self.edit_buf)
        self.editing = False
        self.edit_buf = ""
        if error:
            self.net_note = error
            return
        if updated[key] == self.cfg[key]:
            self.net_note = ""
            return
        self.cfg = updated
        try:
            save_config(self.cfg)
        except OSError as exc:
            self.net_note = str(exc)
            return
        self.restart_servers()
        if self.ftp.error or self.http.error:
            self.net_note = self.ftp.error or self.http.error
        else:
            self.net_note = "saved"

    def restart_servers(self):
        stop_server(self.ftp)
        stop_server(self.http)
        user = str(self.cfg["user"])
        password = str(self.cfg["password"])
        self.ftp = FtpServer("0.0.0.0", int(self.cfg["ftp_port"]), self.incoming, user, password, self.activity)
        self.http = HttpServer(
            "0.0.0.0", int(self.cfg["http_port"]), "/", user, password, self.activity, landing=self.incoming
        )
        self.ftp.start()
        self.http.start()
        self.ftp.ready.wait(2)
        self.http.ready.wait(2)

    def draw_net(self, draw):
        self.header(draw, "Network", "")
        y = 24
        for index, (key, label) in enumerate(NET_FIELDS):
            if index == self.net_row:
                draw.rectangle((0, y - 1, cz.W, y + 16), fill=SEL)
            value = self.edit_buf if self.editing and index == self.net_row else str(self.cfg[key])
            if self.editing and index == self.net_row:
                value += "_"
            draw.text((6, y), label, font=FACE_S, fill=ACCENT)
            draw.text((46, y), cz.fit(draw, value, FACE, cz.W - 54), font=FACE, fill=FG)
            y += 18
        ip = self.ip() or "no network"
        color = FG if self.net_ok() else RED
        draw.text((6, y + 2), cz.fit(draw, f"{ip}  ftp {self.ftp.port}  web {self.http.port}", FACE_S, cz.W - 12), font=FACE_S, fill=color)
        note = self.net_note or self.ftp.error or self.http.error
        if note:
            note_color = GREEN if note == "saved" and self.net_ok() else RED
            draw.text((6, y + 16), cz.fit(draw, note, FACE_S, cz.W - 12), font=FACE_S, fill=note_color)
        if self.editing:
            self.footer(draw, "type   OK save   Esc cancel")
        else:
            self.footer(draw, "F/X field   OK edit   Esc back")


def wrap_lines(draw, lines, face, width, limit):
    visual = []
    for line in lines:
        visual.extend(wrap_one(draw, line, face, width))
    return visual[-limit:]


def wrap_one(draw, line, face, width):
    if cz.text_width(draw, line, face) <= width:
        return [line]
    out = []
    rest = line
    while rest:
        take = len(rest)
        while take > 1 and cz.text_width(draw, rest[:take], face) > width:
            take -= 1
        out.append(rest[:take])
        rest = rest[take:]
        if len(out) > 12:
            break
    return out


def open_servers(cfg, incoming, activity):
    user = str(cfg["user"])
    password = str(cfg["password"])
    ftp = FtpServer("0.0.0.0", cfg["ftp_port"], incoming, user, password, activity)
    http = HttpServer("0.0.0.0", cfg["http_port"], "/", user, password, activity, landing=incoming)
    if not user or not password:
        ftp.error = "set user and password in ~/.config/cz-files/config"
        http.error = ftp.error
        ftp.ready.set()
        http.ready.set()
        return ftp, http
    ftp.start()
    http.start()
    ftp.ready.wait(2)
    http.ready.wait(2)
    return ftp, http


def write_previews(folder):
    """Render UI states from the source tree. Used on a PC, not on the device."""
    folder = Path(folder)
    sample = folder / "sample"
    if sample.exists():
        import shutil

        shutil.rmtree(sample)
    sample.mkdir(parents=True)
    notes = sample / "notes"
    notes.mkdir()
    (notes / "readme.txt").write_text("hello\n", encoding="utf-8")
    (sample / "photo.bin").write_bytes(b"1234")
    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project))
    import build_deb

    built = Path(build_deb.build())
    deb_path = sample / built.name
    deb_path.write_bytes(built.read_bytes())

    class _Up:
        def __init__(self, port):
            self.port = port
            self.error = ""

    activity = {"text": "", "at": 0.0}
    cfg = {"user": "card", "password": "cardputer", "ftp_port": 2121, "http_port": 8081}
    app = FilesApp(sample, sample, _Up(2121), _Up(8081), activity, sudo_ok=True, sudo_reason="", cfg=cfg)
    app._ip = "192.168.1.20"
    app._ip_at = time.time() + 3600
    app._batt = (76, "Discharging")
    app._batt_at = time.time() + 3600
    folder.mkdir(parents=True, exist_ok=True)
    app.draw().save(folder / "browse.png")
    app.esc_at = time.time() - (ESC_HOLD * 0.55)
    app.draw().save(folder / "hold.png")
    app.esc_at = None
    for index, item in enumerate(app.entries):
        if item[3]:
            app.cursor = index
            break
    app.draw().save(folder / "selected.png")
    app.begin_install(deb_path)
    app.draw().save(folder / "confirm.png")
    app.deb_fields = {"Package": "demo", "Version": "1.0", "Architecture": "amd64"}
    app.draw().save(folder / "arch.png")
    app.mode = "net"
    app.draw().save(folder / "net.png")
    app.mode = "log"
    app.log_title = "Installed"
    app.log_ok = True
    app.log_lines = ["Preparing to unpack demo_1.0_arm64.deb", "Unpacking demo", "Setting up demo", "Quit to the launcher to see a new app."]
    app.draw().save(folder / "log.png")
    print(folder)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--screenshot":
        write_previews(sys.argv[2])
        return
    if not Path("/dev/fb0").exists() and not os.environ.get("CZ_FB"):
        print("Loader runs on the Cardputer Zero.")
        print("On Windows, build the package with: python build_deb.py")
        return
    cfg = load_config()
    incoming = Path.home() / "incoming"
    try:
        incoming.mkdir(parents=True, exist_ok=True)
    except OSError:
        incoming = Path.home()
    activity = {"text": "", "at": 0.0}
    ftp, http = open_servers(cfg, incoming, activity)
    sudo_ok, sudo_reason = passwordless_sudo()
    app = FilesApp(incoming, incoming, ftp, http, activity, sudo_ok, sudo_reason, cfg)
    try:
        screen = cz.Screen()
        keyboard = cz.Keyboard()
    except OSError as exc:
        ftp.stop()
        http.stop()
        print(exc)
        return
    try:
        while app.running:
            screen.show(app.draw())
            timeout = 0.05 if app.esc_at else 0.2
            for code, value in keyboard.read(timeout):
                app.on_event(code, value, screen)
                if not app.running:
                    break
            app.poll_esc()
    finally:
        stop_server(app.ftp)
        stop_server(app.http)
        keyboard.close()
        screen.clear()
        screen.close()


if __name__ == "__main__":
    main()
