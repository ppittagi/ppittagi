"""Burned-in annotation removal: decode, paint template regions, re-encode.

Pixel data is always decoded to a numpy array and written back uncompressed
(Explicit VR Little Endian). Compressed bitstreams are never edited in place,
and lossy data is never re-compressed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image
from pydicom.dataset import Dataset
from pydicom.uid import (
    DeflatedExplicitVRLittleEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    JPEG2000,
    JPEG2000Lossless,
    JPEGBaseline8Bit,
    JPEGExtended12Bit,
    JPEGLossless,
    JPEGLosslessSV1,
    JPEGLSLossless,
    JPEGLSNearLossless,
    RLELossless,
)

from .templates import Region, TemplateSpec

SUPPORTED_TRANSFER_SYNTAXES = {
    ImplicitVRLittleEndian,
    ExplicitVRLittleEndian,
    DeflatedExplicitVRLittleEndian,
    JPEGBaseline8Bit,
    JPEGExtended12Bit,
    JPEGLossless,
    JPEGLosslessSV1,
    JPEGLSLossless,
    JPEGLSNearLossless,
    JPEG2000Lossless,
    JPEG2000,
    RLELossless,
}
LOSSY_TRANSFER_SYNTAXES = {JPEGBaseline8Bit, JPEGExtended12Bit, JPEGLSNearLossless, JPEG2000}
SUPPORTED_PHOTOMETRIC = {"MONOCHROME1", "MONOCHROME2", "RGB", "YBR_FULL", "YBR_FULL_422"}

PREVIEW_MAX_SIDE = 512


class PixelError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class PixelCheck:
    ok: bool
    code: str | None = None
    message: str = ""


def check_supported(ds: Dataset) -> PixelCheck:
    """Can this file's pixel data be redacted by this engine?"""
    ts = ds.file_meta.get("TransferSyntaxUID")
    if ts not in SUPPORTED_TRANSFER_SYNTAXES:
        return PixelCheck(False, "UNSUPPORTED_TS", f"transfer syntax {ts} is not supported")
    pi = str(ds.get("PhotometricInterpretation", ""))
    if pi not in SUPPORTED_PHOTOMETRIC:
        return PixelCheck(False, "UNSUPPORTED_PHOTOMETRIC", f"photometric interpretation {pi or '-'} is not supported")
    return PixelCheck(True)


def fill_value(ds: Dataset, photometric: str) -> int:
    """The stored value that displays as black for this image."""
    if photometric.startswith(("RGB", "YBR")):
        return 0
    bits_stored = int(ds.get("BitsStored", ds.BitsAllocated))
    signed = int(ds.get("PixelRepresentation", 0)) == 1
    lo = -(1 << (bits_stored - 1)) if signed else 0
    hi = (1 << (bits_stored - 1)) - 1 if signed else (1 << bits_stored) - 1
    return hi if photometric == "MONOCHROME1" else lo


def _frame_count(ds: Dataset) -> int:
    return int(ds.get("NumberOfFrames", 1) or 1)


def _selected_frames(spec_frames, region_frames, n_frames: int) -> list[int]:
    """0-based frame indexes a region applies to (region setting wins)."""
    frames = region_frames if region_frames != "ALL" else spec_frames
    if frames == "ALL":
        return list(range(n_frames))
    return [f - 1 for f in frames if 1 <= f <= n_frames]


def decode(ds: Dataset) -> tuple[np.ndarray, str]:
    """Decode pixel data; YBR is converted to RGB. Returns (array, photometric)."""
    arr = ds.pixel_array  # pydicom converts YBR colour data to RGB by default
    pi = str(ds.PhotometricInterpretation)
    if pi.startswith("YBR"):
        pi = "RGB"
    return np.ascontiguousarray(arr), pi


def redact(ds: Dataset, spec: TemplateSpec) -> dict:
    """Apply ``spec`` to ``ds`` in place. Returns a summary for the trace.

    For PASS templates the pixel data is left untouched (only overlays and
    icons are removed by :func:`strip_non_pixel_images`).
    """
    if spec.kind != "REDACT":
        return {"pixelsChanged": 0, "regions": 0}

    rows, cols = int(ds.Rows), int(ds.Columns)
    exp_rows, exp_cols = spec.required_equal("Rows"), spec.required_equal("Columns")
    if (rows, cols) != (int(exp_rows), int(exp_cols)):
        # Matching pins the size; a mismatch here means the template is stale.
        raise PixelError("TEMPLATE_DIMENSION_MISMATCH", f"image is {cols}x{rows}, template expects {exp_cols}x{exp_rows}")

    was_lossy = ds.file_meta.TransferSyntaxUID in LOSSY_TRANSFER_SYNTAXES
    arr, pi = decode(ds)
    n_frames = _frame_count(ds)
    multi = n_frames > 1
    value = fill_value(ds, pi)

    changed = 0
    for region in spec.regions:
        ys, xs = slice(region.y, region.y + region.h), slice(region.x, region.x + region.w)
        for f in _selected_frames(spec.frames, region.frames, n_frames):
            view = arr[f, ys, xs] if multi else arr[ys, xs]
            diff = view != value
            changed += int(np.count_nonzero(diff.any(-1) if pi == "RGB" else diff))
            view[...] = value

    bits_stored = int(ds.get("BitsStored", ds.BitsAllocated))
    ds.set_pixel_data(arr, photometric_interpretation=pi, bits_stored=bits_stored, generate_instance_uid=False)
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    if was_lossy:
        ds.LossyImageCompression = "01"
    ds.BurnedInAnnotation = "NO"
    return {"pixelsChanged": changed, "regions": len(spec.regions), "frames": n_frames, "photometric": pi}


def strip_non_pixel_images(ds: Dataset) -> list[str]:
    """Remove overlays (60xx), curves (50xx) and icon thumbnails."""
    removed = []
    for elem in list(ds):
        group = elem.tag.group
        if 0x6000 <= group <= 0x60FF and group % 2 == 0 or 0x5000 <= group <= 0x50FF and group % 2 == 0:
            removed.append(f"{elem.tag}")
            del ds[elem.tag]
    for keyword in ("IconImageSequence", "WaveformSequence"):
        if keyword in ds:
            removed.append(keyword)
            delattr(ds, keyword)
    return removed


def preview_png(ds: Dataset, path: str, frame: int = 0) -> None:
    """Write one frame as an 8-bit PNG (longest side PREVIEW_MAX_SIDE)."""
    arr, pi = decode(ds)
    if _frame_count(ds) > 1:
        arr = arr[frame]
    img = arr.astype(np.float64)
    if pi.startswith("MONOCHROME"):
        lo, hi = float(img.min()), float(img.max())
        img = (img - lo) / (hi - lo or 1) * 255
        if pi == "MONOCHROME1":
            img = 255 - img
    else:
        bits = int(ds.get("BitsStored", 8))
        img = img / ((1 << bits) - 1) * 255
    im = Image.fromarray(img.clip(0, 255).astype(np.uint8))
    im.thumbnail((PREVIEW_MAX_SIDE, PREVIEW_MAX_SIDE))
    im.save(path, format="PNG")


def preview_frames(ds: Dataset) -> list[int]:
    """First, middle and last frame (deduplicated)."""
    n = _frame_count(ds)
    return sorted({0, n // 2, n - 1})
