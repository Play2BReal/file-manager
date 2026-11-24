"""FTP upload jail, and an HTTP file browser that can delete and install."""
import base64
import os
import re
import secrets
import socket
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import pty
except ImportError:
    pty = None
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

MAX_UPLOAD = 256 * 1024 * 1024
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def safe_join(root, rel):
    root = Path(root).resolve()
    candidate = (root / str(rel)).resolve()
    try:
        inside = os.path.commonpath([str(root), str(candidate)]) == str(root)
    except ValueError:
        inside = False
    if not inside:
        raise PermissionError(rel)
    return candidate


def clean_name(name, *, allow_path=True):
    raw = str(name).strip()
    if not allow_path and ("/" in raw or "\\" in raw):
        return None
    base = Path(raw.replace("\\", "/")).name
    if base in ("", ".", "..") or "\x00" in base or len(base) > 200:
        return None
    return base


def lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def _take_lines(buf, lines, on_line):
    while True:
        newline = buf.find("\n")
        carriage = buf.find("\r")
        cuts = [index for index in (newline, carriage) if index >= 0]
        if not cuts:
            return buf
        cut = min(cuts)
        part = _ANSI.sub("", buf[:cut]).strip()
        buf = buf[cut + 1 :]
        if part:
            lines.append(part)
            on_line(lines)


def _pump_fd(fd, lines, on_line):
    buf = ""
    while True:
        try:
            raw = os.read(fd, 4096)
        except OSError:
            break
        if not raw:
            break
        buf += raw.decode("utf-8", "replace")
        buf = _take_lines(buf, lines, on_line)
    if buf.strip():
        lines.append(buf.strip())
        on_line(lines)


def _run(argv, on_line, stdin_text=None):
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["APT_LISTCHANGES_FRONTEND"] = "none"
    env["NEEDRESTART_MODE"] = "a"
    # A tty makes dpkg and apt flush each line. A pipe holds the text until they exit.
    master = None
    slave = None
    if pty is not None and os.name == "posix":
        master, slave = pty.openpty()
        stdout = slave
        stderr = slave
    else:
        stdout = subprocess.PIPE
        stderr = subprocess.STDOUT
    try:
        proc = subprocess.Popen(
            argv,
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            env=env,
            text=slave is None,
            encoding="utf-8" if slave is None else None,
            errors="replace" if slave is None else None,
            bufsize=1,
        )
    except OSError as exc:
        if slave is not None:
            os.close(slave)
        if master is not None:
            os.close(master)
        lines = [str(exc)]
        on_line(lines)
        return 127, lines
    if slave is not None:
        os.close(slave)
    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text if slave is None else stdin_text.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
    lines = []
    if master is not None:
        try:
            _pump_fd(master, lines, on_line)
        finally:
            os.close(master)
    else:
        assert proc.stdout is not None
        for line in proc.stdout:
            for part in line.replace("\r", "\n").split("\n"):
                part = part.strip()
                if not part:
                    continue
                lines.append(part)
                on_line(lines)
    return proc.wait(), lines


def _redact(lines, secret):
    if not secret:
        return lines
    return [line.replace(secret, "****") for line in lines]


def _password_rejected(lines):
    for line in lines:
        low = line.lower()
        if "sudo" in low and "password" in low:
            return True
        if low.startswith("sorry, try again"):
            return True
    return False


def _ask_stop(lines, sudo_password, show):
    if not _password_rejected(lines):
        return None
    if sudo_password:
        lines = lines + ["sudo rejected that password."]
    else:
        lines = lines + ["Enter the sudo password and try again."]
    show(lines)
    return lines


def _sudo(password):
    if password:
        return ["/usr/bin/sudo", "-S", "-p", ""], password + "\n"
    return ["/usr/bin/sudo", "-n"], None


def install_deb(path, on_line, sudo_password=""):
    """Install a .deb. sudo_password is passed to sudo -S and not kept."""
    path = Path(path)
    sudo_password = sudo_password or ""
    if any(ch in sudo_password for ch in "\n\r\x00"):
        lines = ["Password cannot contain a newline."]
        on_line(lines)
        return False, lines, False
    prefix, stdin_text = _sudo(sudo_password)

    def show(lines):
        on_line(_redact(lines, sudo_password))

    code, lines = _run(
        prefix + ["/usr/bin/dpkg", "--force-confdef", "--force-confold", "-i", str(path)],
        show,
        stdin_text,
    )
    lines = _redact(lines, sudo_password)
    if code != 0:
        stopped = _ask_stop(lines, sudo_password, show)
        if stopped is not None:
            return False, stopped, True
    if code != 0:
        show(lines + ["dpkg failed. Running apt-get install -f"])
        code, lines = _run(
            prefix
            + [
                "/usr/bin/apt-get",
                "install",
                "-f",
                "-y",
                "-o",
                "Dpkg::Options::=--force-confdef",
                "-o",
                "Dpkg::Options::=--force-confold",
            ],
            show,
            stdin_text,
        )
        lines = _redact(lines, sudo_password)
    if code != 0:
        stopped = _ask_stop(lines, sudo_password, show)
        if stopped is not None:
            return False, stopped, True
    if code == 0:
        lines = lines + ["Quit to the launcher to see a new app."]
        show(lines)
    return code == 0, lines, False


def remove_path(path):
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    path.rmdir()


def rename_path(path, new_name):
    path = Path(path)
    name = clean_name(new_name, allow_path=False)
    if name is None:
        raise ValueError("bad name")
    dest = path.parent / name
    if dest.resolve() == path.resolve():
        return path
    if dest.exists():
        raise FileExistsError(name)
    path.rename(dest)
    return dest


def make_folder(parent, name):
    folder = clean_name(name, allow_path=False)
    if folder is None:
        raise ValueError("bad name")
    dest = Path(parent) / folder
    dest.mkdir(exist_ok=False)
    return dest


def _note(activity, text):
    if activity is None:
        return
    activity["text"] = text
    activity["at"] = time.time()


def _ls_line(path):
    try:
        st = path.lstat()
    except OSError:
        return None
    when = time.localtime(st.st_mtime)
    age = time.time() - st.st_mtime
    if age > 180 * 24 * 3600:
        stamp = f"{_MONTHS[when.tm_mon - 1]} {when.tm_mday:2d}  {when.tm_year}"
    else:
        stamp = f"{_MONTHS[when.tm_mon - 1]} {when.tm_mday:2d} {when.tm_hour:02d}:{when.tm_min:02d}"
    name = path.name.replace("\r", "").replace("\n", "")
    return f"{stat.filemode(st.st_mode)} 1 user user {st.st_size} {stamp} {name}"


class FtpServer(threading.Thread):
    def __init__(self, host, port, root, user, password, activity=None):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.root = Path(root)
        self.user = user
        self.password = password
        self.activity = activity
        self.error = ""
        self.ready = threading.Event()
        self._stop = False
        self._sock = None

    def stop(self):
        self._stop = True
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
            sock.listen(8)
            sock.settimeout(0.5)
        except OSError as exc:
            self.error = str(exc)
            self.ready.set()
            sock.close()
            return
        self._sock = sock
        self.port = sock.getsockname()[1]
        self.ready.set()
        while not self._stop:
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._client,
                args=(conn,),
                daemon=True,
            ).start()
        try:
            sock.close()
        except OSError:
            pass

    def _client(self, conn):
        try:
            _FtpSession(conn, self.root, self.user, self.password, self.activity).serve()
        except Exception:
            try:
                conn.close()
            except OSError:
                pass


class _FtpSession:
    def __init__(self, conn, root, user, password, activity):
        self.conn = conn
        self.root = Path(root).resolve()
        self.cwd = self.root
        self.user = user
        self.password = password
        self.activity = activity
        self.authed = False
        self.pending_user = None
        self.rest = 0
        self.rename_from = None
        self.pasv = None
        self.port_target = None
        conn.settimeout(300)

    def serve(self):
        self.send("220 cz-files ready")
        buf = b""
        try:
            while True:
                chunk = self.conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 65536:
                    self.send("500 Line too long")
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.command(line.decode("utf-8", "replace").strip("\r"))
        except (OSError, ConnectionError):
            pass
        finally:
            self._close_pasv()
            try:
                self.conn.close()
            except OSError:
                pass

    def send(self, line):
        self.conn.sendall((line + "\r\n").encode("utf-8", "replace"))

    def command(self, line):
        if not line:
            return
        verb, _, arg = line.partition(" ")
        verb = verb.upper()
        if verb == "QUIT":
            self.send("221 Bye")
            raise ConnectionError("quit")
        if verb in ("NOOP", "ALLO"):
            self.send("200 OK")
            return
        if verb == "SYST":
            self.send("215 UNIX Type: L8")
            return
        if verb == "FEAT":
            self.send("211-Features:")
            self.send(" PASV")
            self.send(" EPSV")
            self.send(" UTF8")
            self.send(" SIZE")
            self.send(" MDTM")
            self.send("211 End")
            return
        if verb == "OPTS":
            self.send("200 OK")
            return
        if verb == "USER":
            self.pending_user = arg
            self.authed = False
            self.send("331 Password required")
            return
        if verb == "PASS":
            user = self.pending_user or ""
            if secrets.compare_digest(user, self.user) and secrets.compare_digest(arg, self.password):
                self.authed = True
                self.send("230 Login successful")
            else:
                self.authed = False
                self.send("530 Login incorrect")
            return
        if not self.authed:
            self.send("530 Login required")
            return
        handler = {
            "PWD": self._pwd,
            "XPWD": self._pwd,
            "CWD": self._cwd,
            "XCWD": self._cwd,
            "CDUP": self._cdup,
            "TYPE": self._ok,
            "STRU": self._ok,
            "MODE": self._ok,
            "PASV": self._pasv,
            "EPSV": self._epsv,
            "PORT": self._port,
            "LIST": self._list,
            "NLST": self._nlst,
            "RETR": self._retr,
            "STOR": self._stor,
            "DELE": self._dele,
            "MKD": self._mkd,
            "XMKD": self._mkd,
            "RMD": self._rmd,
            "XRMD": self._rmd,
            "SIZE": self._size,
            "MDTM": self._mdtm,
            "REST": self._rest,
            "RNFR": self._rnfr,
            "RNTO": self._rnto,
            "HELP": self._ok,
        }.get(verb)
        if handler is None:
            self.send("502 Command not implemented")
            return
        handler(arg)

    def _ok(self, _arg):
        self.send("200 OK")

    def _pwd(self, _arg):
        self.send(f'257 "{self._rel(self.cwd)}" is the current directory')

    def _rel(self, path):
        path = path.resolve()
        if path == self.root:
            return "/"
        return "/" + path.relative_to(self.root).as_posix()

    def resolve(self, arg):
        raw = (arg or "").strip().replace("\\", "/")
        if raw == "":
            return self.cwd
        if raw.startswith("/"):
            return safe_join(self.root, raw.lstrip("/"))
        relative = self.cwd.resolve().relative_to(self.root)
        return safe_join(self.root, Path(relative) / raw)

    def _cwd(self, arg):
        try:
            path = self.resolve(arg)
        except PermissionError:
            self.send("550 Permission denied")
            return
        if not path.is_dir():
            self.send("550 Not a directory")
            return
        self.cwd = path
        self.send("250 Directory changed")

    def _cdup(self, _arg):
        if self.cwd.resolve() == self.root:
            self.send("250 Directory changed")
            return
        self.cwd = self.cwd.parent
        self.send("250 Directory changed")

    def _close_pasv(self):
        if self.pasv is not None:
            try:
                self.pasv.close()
            except OSError:
                pass
            self.pasv = None

    def _listen(self):
        self._close_pasv()
        self.port_target = None
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.conn.getsockname()[0], 0))
        sock.listen(1)
        sock.settimeout(15)
        self.pasv = sock
        return sock.getsockname()

    def _pasv(self, _arg):
        ip, port = self._listen()
        if ip in ("0.0.0.0", ""):
            ip = lan_ip() or "127.0.0.1"
        nums = [int(part) for part in ip.split(".")] + [port // 256, port % 256]
        self.send("227 Entering Passive Mode (" + ",".join(str(n) for n in nums) + ")")

    def _epsv(self, _arg):
        _ip, port = self._listen()
        self.send(f"229 Entering Extended Passive Mode (|||{port}|)")

    def _port(self, arg):
        parts = arg.split(",")
        if len(parts) != 6:
            self.send("501 Bad PORT")
            return
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            self.send("501 Bad PORT")
            return
        ip = ".".join(str(n) for n in nums[:4])
        peer = self.conn.getpeername()[0]
        if ip != peer:
            self.send("425 PORT to another host is refused")
            return
        self._close_pasv()
        self.port_target = (ip, nums[4] * 256 + nums[5])
        self.send("200 PORT command successful")

    def open_data(self):
        if self.pasv is not None:
            try:
                conn, _addr = self.pasv.accept()
            finally:
                self._close_pasv()
            return conn
        if self.port_target is not None:
            return socket.create_connection(self.port_target, timeout=15)
        raise OSError("no data connection")

    def _list(self, arg):
        self._dir_list(self._dir_arg(arg), long=True)

    def _nlst(self, arg):
        self._dir_list(self._dir_arg(arg), long=False)

    def _dir_arg(self, arg):
        parts = arg.split()
        while parts and parts[0].startswith("-"):
            parts.pop(0)
        return " ".join(parts)

    def _dir_list(self, arg, long):
        try:
            folder = self.cwd if arg.strip() == "" else self.resolve(arg)
        except PermissionError:
            self.send("550 Permission denied")
            return
        if not folder.is_dir():
            self.send("550 Not a directory")
            return
        lines = []
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            self.send(f"550 {exc}")
            return
        for child in children:
            if long:
                line = _ls_line(child)
                if line:
                    lines.append(line)
            else:
                lines.append(child.name.replace("\r", "").replace("\n", ""))
        payload = ("\r\n".join(lines) + ("\r\n" if lines else "")).encode("utf-8", "replace")
        try:
            data = self.open_data()
        except OSError:
            self.send("425 Can't open data connection")
            return
        self.send("150 Here comes the listing")
        try:
            data.sendall(payload)
        finally:
            data.close()
        self.send("226 Listing sent")

    def _retr(self, arg):
        try:
            path = self.resolve(arg)
        except PermissionError:
            self.send("550 Permission denied")
            return
        if not path.is_file():
            self.send("550 Not a file")
            return
        try:
            data = self.open_data()
        except OSError:
            self.send("425 Can't open data connection")
            return
        self.send("150 Sending file")
        try:
            with open(path, "rb") as handle:
                if self.rest:
                    handle.seek(self.rest)
                    self.rest = 0
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    data.sendall(chunk)
        except OSError as exc:
            self.send(f"426 {exc}")
            data.close()
            return
        finally:
            try:
                data.close()
            except OSError:
                pass
        self.send("226 Transfer complete")

    def _stor(self, arg):
        name = clean_name(arg)
        if name is None and "/" not in arg.replace("\\", "/"):
            self.send("553 Bad file name")
            return
        try:
            path = self.resolve(arg)
        except PermissionError:
            self.send("550 Permission denied")
            return
        if path.is_dir():
            self.send("550 Is a directory")
            return
        try:
            data = self.open_data()
        except OSError:
            self.send("425 Can't open data connection")
            return
        self.send("150 Ok to send data")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "r+b" if path.exists() and self.rest else "wb"
            with open(path, mode) as handle:
                if self.rest:
                    handle.seek(self.rest)
                    self.rest = 0
                while True:
                    chunk = data.recv(65536)
                    if not chunk:
                        break
                    handle.write(chunk)
        except OSError as exc:
            self.send(f"426 {exc}")
            data.close()
            return
        finally:
            try:
                data.close()
            except OSError:
                pass
        _note(self.activity, "upload " + path.name)
        self.send("226 Transfer complete")

    def _dele(self, arg):
        try:
            path = self.resolve(arg)
        except PermissionError:
            self.send("550 Permission denied")
            return
        if not path.is_file():
            self.send("550 Not a file")
            return
        try:
            path.unlink()
        except OSError as exc:
            self.send(f"550 {exc}")
            return
        self.send("250 Deleted")

    def _mkd(self, arg):
        try:
            path = self.resolve(arg)
        except PermissionError:
            self.send("550 Permission denied")
            return
        try:
            path.mkdir(parents=False, exist_ok=False)
        except OSError as exc:
            self.send(f"550 {exc}")
            return
        self.send(f'257 "{self._rel(path)}" created')

    def _rmd(self, arg):
        try:
            path = self.resolve(arg)
        except PermissionError:
            self.send("550 Permission denied")
            return
        try:
            path.rmdir()
        except OSError as exc:
            self.send(f"550 {exc}")
            return
        self.send("250 Removed")

    def _size(self, arg):
        try:
            path = self.resolve(arg)
            size = path.stat().st_size
        except (PermissionError, OSError):
            self.send("550 Unavailable")
            return
        self.send(f"213 {size}")

    def _mdtm(self, arg):
        try:
            path = self.resolve(arg)
            when = time.gmtime(path.stat().st_mtime)
        except (PermissionError, OSError):
            self.send("550 Unavailable")
            return
        self.send("213 " + time.strftime("%Y%m%d%H%M%S", when))

    def _rest(self, arg):
        try:
            self.rest = int(arg)
        except ValueError:
            self.send("501 Bad REST")
            return
        self.send(f"350 Restart at {self.rest}")

    def _rnfr(self, arg):
        try:
            path = self.resolve(arg)
        except PermissionError:
            self.send("550 Permission denied")
            return
        if not path.exists():
            self.send("550 Not found")
            return
        self.rename_from = path
        self.send("350 Ready for RNTO")

    def _rnto(self, arg):
        if self.rename_from is None:
            self.send("503 RNFR first")
            return
        try:
            dest = self.resolve(arg)
        except PermissionError:
            self.send("550 Permission denied")
            return
        try:
            self.rename_from.rename(dest)
        except OSError as exc:
            self.send(f"550 {exc}")
            return
        finally:
            self.rename_from = None
        self.send("250 Renamed")


def _handler_class(root, user, password, activity, landing):
    root = Path(root).resolve()
    landing = Path(landing).resolve() if landing else root

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):
            return

        def _authorized(self):
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                raw = base64.b64decode(header.split(" ", 1)[1], validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            got_user, sep, got_pass = raw.partition(":")
            if not sep:
                return False
            return secrets.compare_digest(got_user, user) and secrets.compare_digest(got_pass, password)

        def _deny(self):
            body = b"login required\n"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="cz-files"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _send(self, code, body, content_type="text/plain; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            if content_type.startswith("text/html"):
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _target(self):
            raw = unquote(urlparse(self.path).path)
            rel = raw.lstrip("/")
            return safe_join(root, rel)

        def do_GET(self):
            if not self._authorized():
                self._deny()
                return
            request_path = unquote(urlparse(self.path).path)
            if request_path.rstrip("/") == "/install":
                query = parse_qs(urlparse(self.path).query)
                path = self._chosen({"path": query.get("path", [])})
                if path is None:
                    return
                if not path.is_file() or not path.name.lower().endswith(".deb"):
                    self._send(400, "not a deb package\n")
                    return
                self._run_install(path, query.get("path", [""])[0], "")
                return
            if request_path in ("", "/") and landing != root:
                self._redirect(_dir_url(root, landing))
                return
            try:
                path = self._target()
            except PermissionError:
                self._send(403, "forbidden\n")
                return
            if path.is_dir():
                self._send(200, _page(root, path), "text/html; charset=utf-8")
                return
            if not path.is_file():
                self._send(404, "not found\n")
                return
            try:
                size = path.stat().st_size
                handle = open(path, "rb")
            except OSError as exc:
                self._send(500, str(exc))
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            finally:
                handle.close()

        def do_PUT(self):
            if not self._authorized():
                self._deny()
                return
            name = clean_name(unquote(urlparse(self.path).path))
            if name is None:
                self._send(400, "bad file name\n")
                return
            length = self._length()
            if length is None:
                return
            dest = root / name
            try:
                with open(dest, "wb") as handle:
                    self._copy(handle, length)
            except OSError as exc:
                self._send(500, str(exc))
                return
            _note(activity, "web " + name)
            self._send(201, "saved\n")

        def do_POST(self):
            if not self._authorized():
                self._deny()
                return
            action = urlparse(self.path).path.rstrip("/") or "/"
            if action == "/upload":
                self._upload()
            elif action == "/delete":
                self._delete()
            elif action == "/rename":
                self._rename()
            elif action == "/mkdir":
                self._mkdir()
            elif action == "/install":
                self._install()
            else:
                self._send(404, "not found\n")

        def _redirect(self, location):
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def _form(self):
            length = self._length()
            if length is None:
                return None
            raw = self.rfile.read(length).decode("utf-8", "replace")
            return parse_qs(raw)

        def _chosen(self, form):
            if not form or "path" not in form or not form["path"]:
                self._send(400, "missing path\n")
                return None
            try:
                path = safe_join(root, form["path"][0].lstrip("/"))
            except PermissionError:
                self._send(403, "forbidden\n")
                return None
            if path == root:
                self._send(400, "refusing the top folder\n")
                return None
            return path

        def _upload(self):
            query = parse_qs(urlparse(self.path).query)
            raw_dir = query.get("to", [""])[0]
            try:
                dest_dir = root if raw_dir == "" else safe_join(root, raw_dir.lstrip("/"))
            except PermissionError:
                self._send(403, "forbidden\n")
                return
            if not dest_dir.is_dir():
                self._send(400, "not a folder\n")
                return
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype or "boundary=" not in ctype:
                self._send(400, "expected a file upload\n")
                return
            boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
            length = self._length()
            if length is None:
                return
            tmp = dest_dir / f".upload-{secrets.token_hex(8)}"
            try:
                with open(tmp, "wb") as handle:
                    self._copy(handle, length)
                name = _save_multipart(tmp, boundary, dest_dir)
            except ValueError as exc:
                self._send(400, str(exc) + "\n")
                return
            except OSError as exc:
                self._send(500, str(exc))
                return
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            _note(activity, "web " + name)
            self._redirect(_dir_url(root, dest_dir))

        def _delete(self):
            form = self._form()
            if form is None:
                return
            path = self._chosen(form)
            if path is None:
                return
            parent = path.parent
            try:
                remove_path(path)
            except OSError as exc:
                self._send(200, _result_page("Delete failed", [str(exc)], _dir_url(root, parent)), "text/html; charset=utf-8")
                return
            _note(activity, "deleted " + path.name)
            self._redirect(_dir_url(root, parent))

        def _rename(self):
            form = self._form()
            if form is None:
                return
            path = self._chosen(form)
            if path is None:
                return
            name = form["name"][0] if form.get("name") else ""
            parent = path.parent
            try:
                dest = rename_path(path, name)
            except (OSError, ValueError, FileExistsError) as exc:
                self._send(
                    200,
                    _result_page("Rename failed", [str(exc)], _dir_url(root, parent)),
                    "text/html; charset=utf-8",
                )
                return
            _note(activity, "rename " + dest.name)
            self._redirect(_dir_url(root, parent))

        def _mkdir(self):
            form = self._form()
            if form is None:
                return
            if not form.get("to") or not form["to"][0]:
                folder = root
            else:
                try:
                    folder = safe_join(root, form["to"][0].lstrip("/"))
                except PermissionError:
                    self._send(403, "forbidden\n")
                    return
            if not folder.is_dir():
                self._send(400, "not a folder\n")
                return
            name = form["name"][0] if form.get("name") else ""
            try:
                dest = make_folder(folder, name)
            except (OSError, ValueError, FileExistsError) as exc:
                self._send(
                    200,
                    _result_page("New folder failed", [str(exc)], _dir_url(root, folder)),
                    "text/html; charset=utf-8",
                )
                return
            _note(activity, "mkdir " + dest.name)
            self._redirect(_dir_url(root, folder))

        def _install(self):
            form = self._form()
            if form is None:
                return
            path = self._chosen(form)
            if path is None:
                return
            if not path.is_file() or not path.name.lower().endswith(".deb"):
                self._send(400, "not a deb package\n")
                return
            secret = form["sudo"][0] if form.get("sudo") else ""
            self._run_install(path, form["path"][0], secret)

        def _run_install(self, path, rel, secret):
            back = _dir_url(root, path.parent)
            self._open_install_stream(path.name)
            shown = 0

            def collect(got):
                nonlocal shown
                for line in got[shown:]:
                    self._log_line(line)
                shown = len(got)

            try:
                ok, _lines, needs_password = install_deb(path, collect, secret)
                if needs_password:
                    status = "Password required"
                elif ok:
                    status = "Installed"
                else:
                    status = "Install failed"
                self._chunk(_install_close(status, back, rel if needs_password else ""))
            finally:
                self._chunk_end()
            _note(activity, "install " + path.name)

        def _open_install_stream(self, name):
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            self.close_connection = True
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self._chunk(_install_open(name))
            self._log_line("Starting dpkg")

        def _chunk(self, text):
            if getattr(self, "_stream_dead", False):
                return
            data = text.encode("utf-8")
            try:
                self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            except OSError:
                self._stream_dead = True

        def _chunk_end(self):
            if getattr(self, "_stream_dead", False):
                return
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                self._stream_dead = True

        def _log_line(self, line):
            self._chunk(f'<div class="line">{_html(line)}</div><script>scrollTo(0,1e9)</script>\n')

        def _length(self):
            raw = self.headers.get("Content-Length")
            if raw is None:
                self._send(411, "Content-Length required\n")
                return None
            try:
                length = int(raw)
            except ValueError:
                self._send(400, "bad Content-Length\n")
                return None
            if length < 0 or length > MAX_UPLOAD:
                self._send(413, "upload is over 256 MB\n")
                return None
            return length

        def _copy(self, handle, length):
            left = length
            while left:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                handle.write(chunk)
                left -= len(chunk)

    return Handler


def _save_multipart(tmp, boundary, root):
    marker = b"\r\n--" + boundary.encode("ascii", "replace")
    with open(tmp, "rb") as handle:
        head = handle.read(8192)
        split = head.find(b"\r\n\r\n")
        if split < 0:
            raise ValueError("incomplete upload")
        header = head[:split].decode("latin1", "replace")
        start = split + 4
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(start, size - 4096))
        tail = handle.read()
        found = tail.rfind(marker)
        if found < 0:
            raise ValueError("incomplete upload")
        end = handle.tell() - len(tail) + found
        name = _filename_from(header)
        if name is None:
            raise ValueError("missing file name")
        dest = root / name
        handle.seek(start)
        left = end - start
        with open(dest, "wb") as out:
            while left:
                chunk = handle.read(min(65536, left))
                if not chunk:
                    break
                out.write(chunk)
                left -= len(chunk)
    return name


def _filename_from(header):
    lower = header.lower()
    key = "filename*="
    if key in lower:
        raw = header[lower.index(key) + len(key) :].split("\r\n", 1)[0].strip().strip('"')
        if "''" in raw:
            raw = raw.split("''", 1)[1]
        return clean_name(unquote(raw))
    key = "filename="
    if key not in lower:
        return None
    raw = header[lower.index(key) + len(key) :].split("\r\n", 1)[0].strip()
    if raw.startswith('"'):
        raw = raw[1:].split('"', 1)[0]
    else:
        raw = raw.split(";", 1)[0].strip()
    return clean_name(raw)


def _dir_url(root, folder):
    rel = Path(folder).resolve().relative_to(Path(root).resolve())
    if str(rel) == ".":
        return "/"
    return "/" + rel.as_posix()


_PAGE_CSS = """
:root { color-scheme: dark; }
body { margin: 0; background: #12141c; color: #e8eaf0; font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; }
header { padding: 14px 18px; background: #1c2030; border-bottom: 1px solid #2c3144; }
h1 { margin: 0; font-size: 16px; font-weight: 600; word-break: break-all; }
main { padding: 14px 18px 32px; max-width: 760px; }
form.bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 0 0 12px; }
.row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; padding: 8px 0; border-bottom: 1px solid #262b3c; }
.name { flex: 1; min-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.size { color: #8b93a7; font-size: 13px; }
.actions { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.actions form { margin: 0; display: flex; gap: 6px; align-items: center; }
a { color: #ffb06a; text-decoration: none; }
a:hover { text-decoration: underline; }
button { background: #2a3144; color: #e8eaf0; border: 0; border-radius: 4px; padding: 6px 10px; }
button.install, a.go { background: #ff9a3c; color: #1a1208; border-radius: 4px; padding: 6px 10px; }
button.delete { background: transparent; color: #e8a0a0; }
a.go:hover { text-decoration: none; }
label { display: block; margin: 0 0 12px; }
input[type="password"], input[type="text"] { background: #1c2030; color: #e8eaf0; border: 1px solid #2c3144; border-radius: 4px; padding: 6px 8px; }
input[type="password"] { margin-left: 8px; }
input.rename { width: 9em; }
.note { color: #8b93a7; font-size: 13px; margin: 16px 0 0; }
pre { white-space: pre-wrap; background: #1c2030; padding: 12px; border-radius: 4px; }
.line { font: 13px/1.45 ui-monospace, monospace; white-space: pre-wrap; }
h2 { font-size: 16px; margin: 18px 0 0; }
"""


def _result_page(title, lines, back):
    body = "\n".join(_html(line) for line in lines) or _html("(no output)")
    return f"""<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html(title)}</title>
<style>{_PAGE_CSS}</style>
<header><h1>{_html(title)}</h1></header>
<main>
<pre>{body}</pre>
<p><a href="{_html(back)}">Back</a></p>
</main>
"""


def _sudo_form(rel):
    return f"""<form method="post" action="/install">
<input type="hidden" name="path" value="{_html(rel)}">
<label>sudo password <input type="password" name="sudo" autofocus></label>
<button class="install">Install</button>
</form>
<p class="note">Used once for this install. It is not saved.</p>
"""


def _install_open(name):
    pad = " " * 2048
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Installing {_html(name)}</title>
<style>{_PAGE_CSS}</style>
<!--{pad}-->
<header><h1>Installing {_html(name)}</h1></header>
<main>
<p class="note">Output from dpkg and apt appears here while the install runs.</p>
<div id="log">
"""


def _install_close(status, back, rel=""):
    form = _sudo_form(rel) if rel else ""
    return f'</div><h2>{_html(status)}</h2>{form}<p><a href="{_html(back)}">Back</a></p></main>\n'


def _human_size(size):
    size = int(size)
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _page(root, folder):
    rows = []
    try:
        children = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        children = []
        rows.append(f'<p class="note">{_html(str(exc))}</p>')
    here = _dir_url(root, folder)
    base = folder.resolve().relative_to(root.resolve())
    if folder.resolve() != root.resolve():
        parent = _dir_url(root, folder.parent)
        rows.append(f'<div class="row"><a class="name" href="{_html(parent)}">..</a></div>')
    for child in children:
        if child.name.startswith(".upload-"):
            continue
        rel = (base / child.name).as_posix()
        name = child.name
        href = "/" + rel
        try:
            is_dir = child.is_dir()
            size = "" if is_dir else _human_size(child.stat().st_size)
        except OSError:
            is_dir = False
            size = ""
        if is_dir:
            label = name + "/"
            actions = _rename_form(rel, name) + _action_form("/delete", rel, "Delete")
        else:
            label = name
            actions = _rename_form(rel, name)
            if name.lower().endswith(".deb"):
                actions += _action_form("/install", rel, "Install")
            actions += _action_form("/delete", rel, "Delete")
        rows.append(
            '<div class="row">'
            f'<a class="name" href="{_html(href)}">{_html(label)}</a>'
            f'<span class="size">{_html(size)}</span>'
            f'<span class="actions">{actions}</span>'
            "</div>"
        )
    upload_to = quote(here, safe="/")
    return f"""<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Files</title>
<style>{_PAGE_CSS}</style>
<header><h1>{_html(here)}</h1></header>
<main>
<form class="bar" method="post" action="/upload?to={upload_to}" enctype="multipart/form-data">
<input type="file" name="file">
<button>Upload</button>
</form>
<form class="bar" method="post" action="/mkdir">
<input type="hidden" name="to" value="{_html(here)}">
<input type="text" name="name" placeholder="new folder" required maxlength="200">
<button>New folder</button>
</form>
{''.join(rows)}
<p class="note">Install runs dpkg. A sudo password is asked only when sudo requires one. Delete removes that file. A folder deletes only when it is empty.</p>
</main>
"""


def _rename_form(rel, name):
    return (
        f'<form method="post" action="/rename">'
        f'<input type="hidden" name="path" value="{_html(rel)}">'
        f'<input class="rename" type="text" name="name" value="{_html(name)}" required maxlength="200">'
        f"<button>Rename</button></form>"
    )


def _action_form(action, rel, label):
    kind = "install" if action == "/install" else "delete"
    return (
        f'<form method="post" action="{action}">'
        f'<input type="hidden" name="path" value="{_html(rel)}">'
        f'<button class="{kind}">{_html(label)}</button></form>'
    )


def _html(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class HttpServer(threading.Thread):
    def __init__(self, host, port, root, user, password, activity=None, landing=None):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.root = Path(root)
        self.landing = Path(landing) if landing else self.root
        self.user = user
        self.password = password
        self.activity = activity
        self.error = ""
        self.ready = threading.Event()
        self.httpd = None

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    def run(self):
        handler = _handler_class(self.root, self.user, self.password, self.activity, self.landing)

        class Server(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

        try:
            self.httpd = Server((self.host, self.port), handler)
        except OSError as exc:
            self.error = str(exc)
            self.ready.set()
            return
        self.port = self.httpd.server_address[1]
        self.ready.set()
        self.httpd.serve_forever(poll_interval=0.3)
