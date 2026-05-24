"""Build-time helper: turn assets/icon.png into a multi-size Windows .ico.

Called by build_installer.py so the Inno Setup script can reference a real
.ico file for SetupIconFile and shortcut IconFilename. The runtime GUI uses
the PNG directly via QIcon, so this script is only needed when producing the
installer.

We use PyQt6 (already a project dependency) instead of Pillow to avoid a
build-only dependency. ICO is built as a small container around PNG-encoded
entries — Vista+ Windows understands PNG-inside-ICO, which keeps the file
small and high quality at large sizes.
"""
from __future__ import annotations

import struct
from io import BytesIO
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, QSize, Qt
from PyQt6.QtGui import QImage, QPainter

# Standard Windows shell icon sizes. 256 must be present so Explorer's "Large
# icons" view looks crisp on the desktop shortcut.
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _scale_to_square(src: QImage, size: int) -> QImage:
    """Fit src into a transparent SIZExSIZE square, preserving aspect ratio."""
    scaled = src.scaled(
        QSize(size, size),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    canvas = QImage(size, size, QImage.Format.Format_ARGB32)
    canvas.fill(0)  # transparent
    painter = QPainter(canvas)
    x = (size - scaled.width()) // 2
    y = (size - scaled.height()) // 2
    painter.drawImage(x, y, scaled)
    painter.end()
    return canvas


def _png_bytes(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def png_to_ico(src_png: Path, dst_ico: Path,
               sizes: tuple[int, ...] = ICON_SIZES) -> Path:
    """Write a Windows .ico containing PNG-encoded entries at each size."""
    src = QImage(str(src_png))
    if src.isNull():
        raise FileNotFoundError(f"Could not load {src_png}")

    payloads: list[tuple[int, bytes]] = []
    for size in sizes:
        scaled = _scale_to_square(src, size)
        payloads.append((size, _png_bytes(scaled)))

    # ICONDIR (6 bytes) + ICONDIRENTRY (16 bytes each) + image payloads
    out = BytesIO()
    out.write(struct.pack("<HHH", 0, 1, len(payloads)))
    offset = 6 + 16 * len(payloads)
    for size, data in payloads:
        # In the directory entry, dimensions >=256 are stored as 0.
        w = 0 if size >= 256 else size
        h = 0 if size >= 256 else size
        out.write(struct.pack(
            "<BBBBHHII",
            w, h,
            0,        # color count (0 = no palette)
            0,        # reserved
            1,        # color planes
            32,       # bits per pixel
            len(data),
            offset,
        ))
        offset += len(data)
    for _, data in payloads:
        out.write(data)

    dst_ico.parent.mkdir(parents=True, exist_ok=True)
    dst_ico.write_bytes(out.getvalue())
    return dst_ico


if __name__ == "__main__":
    # Allow `python build_icons.py` for ad-hoc rebuilds.
    import sys

    from PyQt6.QtGui import QGuiApplication  # noqa: F401 — ensures Qt is initialized
    _ = QGuiApplication.instance() or QGuiApplication(sys.argv)

    root = Path(__file__).parent
    src = root / "assets" / "icon.png"
    dst = root / "assets" / "icon.ico"
    png_to_ico(src, dst)
    print(f"  wrote {dst}  ({dst.stat().st_size // 1024} KB)")
