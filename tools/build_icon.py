"""Build a multi-size Windows .ico from the supplied PNG using PySide6."""

from pathlib import Path
import struct

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "icon" / "icon.png"
OUTPUT = ROOT / "icon" / "netfusion.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("Qt could not encode a PNG icon frame")
    return bytes(buffer.data())


def main() -> None:
    source = QImage(str(SOURCE))
    if source.isNull():
        raise SystemExit(f"Could not read icon image: {SOURCE}")

    frames = [
        (size, png_bytes(source.scaled(
            size,
            size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )))
        for size in SIZES
    ]

    directory_size = 6 + 16 * len(frames)
    offset = directory_size
    entries = []
    for size, payload in frames:
        entries.append(struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0,
            0,
            1,
            32,
            len(payload),
            offset,
        ))
        offset += len(payload)

    OUTPUT.write_bytes(
        struct.pack("<HHH", 0, 1, len(frames)) + b"".join(entries)
        + b"".join(payload for _, payload in frames)
    )
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
