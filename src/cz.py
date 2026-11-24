"""LCD and keyboard for the Cardputer Zero.

Screen writes a 320x170 image to /dev/fb0 as RGB565.
Keyboard reads /dev/input/cardputer-zero-internal.

CZ_FB and CZ_KBD override those paths.
"""
import os
import select
import struct

try:
    import fcntl
except ImportError:
    fcntl = None

from PIL import Image, ImageFont

W, H = 320, 170
FB_DEV = os.environ.get("CZ_FB", "/dev/fb0")
KBD_DEV = os.environ.get("CZ_KBD", "/dev/input/cardputer-zero-internal")

KEY_ESC, KEY_BACKSPACE, KEY_TAB, KEY_ENTER = 1, 14, 15, 28
KEY_D, KEY_H, KEY_N, KEY_R, KEY_M = 32, 35, 49, 19, 50
KEY_F, KEY_Z, KEY_X, KEY_C = 33, 44, 45, 46
KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT = 103, 108, 105, 106
KEY_HOME, KEY_END = 102, 107
KEY_PAGEUP, KEY_PAGEDOWN = 104, 109
KEY_DELETE = 111
KEY_KPENTER = 96

# fn+F/X/Z/C are Up/Down/Left/Right. Screens without text input also take F/X/Z/C directly.
UP_KEYS = {KEY_UP, KEY_F}
DOWN_KEYS = {KEY_DOWN, KEY_X}
LEFT_KEYS = {KEY_LEFT, KEY_Z}
RIGHT_KEYS = {KEY_RIGHT, KEY_C}
OK_KEYS = {KEY_ENTER, KEY_KPENTER}
MENU_KEYS = {KEY_TAB}
NET_KEYS = {KEY_N}

# input_event on aarch64 is 24 bytes. "ll" changes size between Windows and
# Linux, so the layout is spelled with explicit 64-bit fields.
_EVENT_FMT = "QQHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)
_EV_KEY = 1
_EVIOCGRAB = 0x40044590


def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for folder in ("/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/dejavu/"):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def text_width(draw, text, face):
    if hasattr(draw, "textlength"):
        return draw.textlength(text, font=face)
    return len(text) * 7


def fit(draw, text, face, width):
    if text_width(draw, text, face) <= width:
        return text
    ellipsis = "..."
    while text and text_width(draw, text + ellipsis, face) > width:
        text = text[:-1]
    return text + ellipsis


class Screen:
    def __init__(self, path=FB_DEV):
        self.fb = open(path, "r+b", buffering=0)

    def show(self, img):
        import numpy as np

        if img.size != (W, H):
            img = img.resize((W, H))
        a = np.asarray(img.convert("RGB"), dtype=np.uint16)
        rgb565 = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
        self.fb.seek(0)
        self.fb.write(rgb565.astype("<u2").tobytes())

    def clear(self):
        self.show(Image.new("RGB", (W, H), (0, 0, 0)))

    def close(self):
        if self.fb:
            self.fb.close()
            self.fb = None


class Keyboard:
    def __init__(self, path=KBD_DEV):
        if fcntl is None:
            raise OSError("keyboard needs Linux")
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.grabbed = False
        try:
            fcntl.ioctl(self.fd, _EVIOCGRAB, 1)
            self.grabbed = True
        except OSError:
            self.grabbed = False

    def read(self, timeout=None):
        keys = []
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return keys
        try:
            data = os.read(self.fd, _EVENT_SIZE * 64)
        except BlockingIOError:
            return keys
        for offset in range(0, len(data) - _EVENT_SIZE + 1, _EVENT_SIZE):
            _, _, etype, code, value = struct.unpack_from(_EVENT_FMT, data, offset)
            if etype == _EV_KEY and value in (0, 1, 2):
                keys.append((code, value))
        return keys

    def close(self):
        if self.grabbed:
            try:
                fcntl.ioctl(self.fd, _EVIOCGRAB, 0)
            except OSError:
                pass
        os.close(self.fd)
