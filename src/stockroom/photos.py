"""Storing pictures of items.

"Is this the right cable?" is a photo question. No description of a connector
answers it as well as a picture of the connector.

    WHAT THIS MODULE IS CAREFUL ABOUT

    * **Re-encoding, not trusting.** An upload is decoded, resized and written
      out as a fresh JPEG. Nothing the uploader supplied survives: not the
      filename, not the container, not the metadata. A file that is not really
      an image fails to decode and is rejected before it is stored.
    * **EXIF is stripped.** Phone photos carry GPS coordinates and a device
      serial. There is no reason for either to end up on a stockroom server,
      and every reason not to put them where they might later be published.
    * **Orientation is applied first.** EXIF orientation is what makes photos
      appear sideways; applying it before stripping the tag is the difference
      between a rotated photo and a rotated tag nobody reads.
    * **Size.** A phone photo is 3-5 MB. Downscaled and re-encoded they land
      around 200 KB, which matters on an SD card that is already the most
      likely component here to fail.

The file lives on disk; :mod:`stockroom.service` owns the ``item_photo`` row
that indexes it, and the audit event that goes with it.
"""

from __future__ import annotations

import io
import secrets
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from . import config


class PhotoError(ValueError):
    """The upload was not usable. Shown to the operator, not logged as a bug."""


@dataclass(frozen=True, slots=True)
class StoredPhoto:
    filename: str
    width: int
    height: int
    bytes: int


def photo_path(filename: str) -> Path:
    """Where one stored photo lives.

    Rejects anything that is not a plain generated name. Callers pass a value
    that came out of the database, but a path built from a database string is
    exactly the sort of thing that becomes a traversal after a later refactor.
    """
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        raise PhotoError("That is not a photo this system stored.")
    return config.PHOTO_DIR / filename


def store(data: bytes, *, max_pixels: int | None = None) -> StoredPhoto:
    """Decode, downscale, strip metadata, and write a fresh JPEG.

    Returns the generated filename and the stored dimensions. Raises
    :class:`PhotoError` for anything that is not a decodable image, which
    covers both an honest mistake (a PDF) and a file pretending to be an image.
    """
    if not data:
        raise PhotoError("That file was empty.")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise PhotoError(
            f"That photo is {len(data) / 1024 / 1024:.1f} MB. The limit is "
            f"{config.MAX_UPLOAD_BYTES / 1024 / 1024:.0f} MB."
        )

    limit = max_pixels or config.PHOTO_MAX_PIXELS
    try:
        # A decompression bomb is a small file that declares an enormous
        # canvas: 388 KB of PNG can claim 20000x20000, which is 400 megapixels
        # and well over a gigabyte once decoded. Pillow raises
        # DecompressionBombError above 2x MAX_IMAGE_PIXELS -- and that
        # subclasses Exception, not OSError, so it sailed past the handlers
        # below and out of the route as a 500. Between 1x and 2x it only warns
        # and decodes anyway, which on a Pi is the more dangerous half.
        #
        # Promoting the warning makes both bands raise, and both are caught
        # here as an ordinary bad upload.
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                # Ask the decoder for something near the size we actually want.
                # JPEG can decode at 1/2, 1/4 or 1/8 scale, so a phone photo
                # never has to exist at full resolution to be shrunk. A no-op
                # for formats that cannot do it.
                image.draft("RGB", (limit, limit))
                # Apply EXIF orientation while the tag is still there, then
                # drop every tag by copying the pixels into a new image below.
                image = ImageOps.exif_transpose(image)
                image = image.convert("RGB")
                image.thumbnail((limit, limit), Image.LANCZOS)

                buffer = io.BytesIO()
                # No exif= argument: a fresh JPEG carries no metadata at all.
                image.save(buffer, format="JPEG", quality=config.PHOTO_QUALITY,
                           optimize=True)
                width, height = image.size
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise PhotoError(
            "That image claims to be far too large to process. If it is a "
            "real photo, open it and save a smaller copy."
        ) from exc
    except UnidentifiedImageError as exc:
        raise PhotoError(
            "That file is not an image the system can read. JPEG, PNG, HEIC "
            "and WebP all work."
        ) from exc
    except OSError as exc:
        raise PhotoError(f"That image could not be processed: {exc}") from exc

    payload = buffer.getvalue()
    # A random name, never the uploader's. Filenames are the one piece of an
    # upload that reaches a filesystem, and generating them removes an entire
    # category of problem rather than trying to sanitise one.
    filename = f"{secrets.token_hex(16)}.jpg"

    config.PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    target = config.PHOTO_DIR / filename
    # Write then rename, so a reader never sees a half-written file.
    temporary = target.with_suffix(".part")
    temporary.write_bytes(payload)
    temporary.replace(target)
    target.chmod(0o644)

    return StoredPhoto(filename, width, height, len(payload))


def delete_file(filename: str) -> None:
    """Remove a stored file. Only ever called for an already soft-deleted row."""
    try:
        photo_path(filename).unlink(missing_ok=True)
    except PhotoError:
        pass
