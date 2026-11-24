"""Build a Debian package on Windows or Linux. No dpkg required."""
import gzip
import hashlib
import io
import tarfile
import time
import zlib
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"


def load_env(path):
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def png(width, height, pixels):
    """pixels is width*height*4 RGBA bytes, top row first."""

    def chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = b"".join(b"\x00" + pixels[y * width * 4 : (y + 1) * width * 4] for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def nearest_scale(pixels, src, dst):
    out = bytearray(dst * dst * 4)
    for y in range(dst):
        sy = (y * src) // dst
        for x in range(dst):
            sx = (x * src) // dst
            i = (sy * src + sx) * 4
            j = (y * dst + x) * 4
            out[j : j + 4] = pixels[i : i + 4]
    return bytes(out)


def icon_png():
    size = 48
    buf = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            i = (y * size + x) * 4
            buf[i : i + 4] = bytes((20, 24, 36, 255))
            folder = 8 <= x <= 40 and 16 <= y <= 36
            tab = 8 <= x <= 20 and 12 <= y <= 18
            if folder or tab:
                buf[i : i + 4] = bytes((255, 154, 60, 255))
            label = 14 <= x <= 34 and 22 <= y <= 30
            if label:
                buf[i : i + 4] = bytes((28, 32, 48, 255))
    return png(256, 256, nearest_scale(bytes(buf), size, 256))


def add_bytes(tar, name, data, mode):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def add_dir(tar, name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = int(time.time())
    tar.addfile(info)


def parent_dirs(name):
    """dpkg does not create a missing folder for a file entry. List parents, shallow first."""
    parents = []
    current = name
    while True:
        current = current.rsplit("/", 1)[0]
        if current in ("", "."):
            break
        parents.append(current + "/")
    parents.reverse()
    return parents


def tar_gz(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.USTAR_FORMAT) as tar:
        seen = set()
        for name, data, mode in files:
            for directory in parent_dirs(name):
                if directory not in seen:
                    seen.add(directory)
                    add_dir(tar, directory)
            add_bytes(tar, name, data, mode)
    return buf.getvalue()


def ar_member(name, data):
    if len(name) > 16:
        raise RuntimeError(f"ar name too long: {name}")
    header = name.encode("ascii").ljust(16)
    header += str(int(time.time())).encode("ascii").ljust(12)
    header += b"0".ljust(6)
    header += b"0".ljust(6)
    header += b"100644".ljust(8)
    header += str(len(data)).encode("ascii").ljust(10)
    header += b"`\n"
    if len(header) != 60:
        raise RuntimeError("bad ar header")
    pad = b"\n" if len(data) % 2 else b""
    return header + data + pad


def build():
    env = load_env(ROOT / "app.env")
    app_id = env["APP_ID"]
    app_name = env["APP_NAME"]
    app_desc = env["APP_DESC"]
    maintainer = env.get("MAINTAINER") or "Cardputer <cardputer@localhost>"
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not app_id or not version:
        raise RuntimeError("APP_ID and VERSION are required")

    launcher = (
        "#!/bin/sh\n"
        f"cd /usr/share/APPLaunch/share/{app_id} || exit 1\n"
        "exec python3 main.py\n"
    ).encode()
    desktop = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={app_name}\n"
        f"Comment={app_desc}\n"
        f"Exec=/usr/share/APPLaunch/bin/{app_id}\n"
        "Terminal=false\n"
        f"Icon=share/images/{app_id}.png\n"
        "Sysplause=true\n"
    ).encode()
    sudoers = (
        "# cz-files: passwordless dpkg and apt-get for the on-device installer.\n"
        "%sudo ALL=(ALL) NOPASSWD: /usr/bin/dpkg, /usr/bin/apt-get\n"
    ).encode()
    icon = icon_png()
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "icon.png").write_bytes(icon)

    data_files = [
        (f"./usr/share/APPLaunch/bin/{app_id}", launcher, 0o755),
        (f"./usr/share/APPLaunch/applications/{app_id}.desktop", desktop, 0o644),
        (f"./usr/share/APPLaunch/share/images/{app_id}.png", icon, 0o644),
        ("./etc/sudoers.d/cz-files", sudoers, 0o440),
    ]
    for path in sorted(SRC.glob("*.py")):
        data_files.append(
            (f"./usr/share/APPLaunch/share/{app_id}/{path.name}", path.read_bytes(), 0o644)
        )

    sums = []
    installed = 0
    for name, data, _mode in data_files:
        installed += len(data)
        rel = name[2:] if name.startswith("./") else name
        sums.append(hashlib.md5(data).hexdigest() + "  " + rel)
    sums.sort()
    md5sums = ("\n".join(sums) + "\n").encode()

    short = "File browser with FTP, web upload, and deb install"
    long = (
        " Browse the Cardputer Zero filesystem, receive files over FTP or a\n"
        " browser, and install .deb packages from the on-device file list."
    )
    control = (
        f"Package: {app_id}\n"
        f"Version: {version}\n"
        "Section: utils\n"
        "Priority: optional\n"
        "Architecture: all\n"
        f"Maintainer: {maintainer}\n"
        "Depends: python3, python3-pil, python3-numpy\n"
        f"Installed-Size: {(installed + 1023) // 1024}\n"
        f"Description: {short}\n"
        f"{long}\n"
    ).encode()
    postinst = (
        "#!/bin/sh\n"
        "set -e\n"
        "chmod 440 /etc/sudoers.d/cz-files\n"
        "exit 0\n"
    ).encode()

    control_tar = tar_gz(
        [
            ("./control", control, 0o644),
            ("./md5sums", md5sums, 0o644),
            ("./postinst", postinst, 0o755),
        ]
    )
    data_tar = tar_gz(data_files)
    deb = b"!<arch>\n"
    deb += ar_member("debian-binary", b"2.0\n")
    deb += ar_member("control.tar.gz", control_tar)
    deb += ar_member("data.tar.gz", data_tar)

    out_dir = ROOT / "dist"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{app_id}_{version}_all.deb"
    out.write_bytes(deb)
    return out


if __name__ == "__main__":
    print(build())
