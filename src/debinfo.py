"""Read the control file out of a .deb without dpkg."""
import bz2
import gzip
import io
import lzma
import tarfile


class DebError(Exception):
    pass


def ar_members(data):
    """Return (name, payload) pairs from a Debian ar archive."""
    if not data.startswith(b"!<arch>\n"):
        raise DebError("not a debian package")
    out = []
    i = 8
    while i + 60 <= len(data):
        header = data[i : i + 60]
        if header[58:60] != b"`\n":
            break
        name = header[0:16].decode("ascii", "replace").strip()
        try:
            size = int(header[48:58].decode("ascii").strip() or "0")
        except ValueError as exc:
            raise DebError("bad ar header") from exc
        i += 60
        payload = data[i : i + size]
        if len(payload) != size:
            raise DebError("truncated debian package")
        i += size
        if size % 2 == 1:
            i += 1
        if name.startswith("#1/"):
            name_len = int(name[3:])
            name = payload[:name_len].decode("utf-8", "replace").strip("\x00")
            payload = payload[name_len:]
        name = name.rstrip("/")
        out.append((name, payload))
    if not out:
        raise DebError("empty debian package")
    return out


def _decompress_control(name, blob):
    if name.endswith(".gz"):
        return gzip.decompress(blob)
    if name.endswith(".xz"):
        return lzma.decompress(blob)
    if name.endswith(".bz2"):
        return bz2.decompress(blob)
    if name.endswith(".zst"):
        raise DebError("zstd control.tar is not supported")
    return blob


def _control_text(members):
    for name, blob in members:
        if not name.startswith("control.tar"):
            continue
        raw = _decompress_control(name, blob)
        try:
            tar = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
        except tarfile.TarError as exc:
            raise DebError("unreadable control.tar") from exc
        for member in tar.getmembers():
            base = member.name.rsplit("/", 1)[-1]
            if base == "control" and member.isfile():
                handle = tar.extractfile(member)
                if handle is None:
                    break
                return handle.read().decode("utf-8", "replace")
        raise DebError("control file missing")
    raise DebError("control.tar missing")


def parse_control(text):
    fields = {}
    key = None
    for line in text.splitlines():
        if line.strip() == "":
            break
        if line.startswith((" ", "\t")):
            if key:
                fields[key] += " " + line.strip()
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        fields[key] = value.strip()
    return fields


def read_control(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise DebError(str(exc)) from exc
    fields = parse_control(_control_text(ar_members(data)))
    if "Package" not in fields:
        raise DebError("Package field missing")
    return fields
