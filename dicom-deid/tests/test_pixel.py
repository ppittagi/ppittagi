import numpy as np
import pydicom
import pytest
from pydicom.uid import ExplicitVRLittleEndian, RLELossless

from deid import pixel
from deid.templates import parse_spec

from conftest import BAR_H, BAR_W, make_dataset, write


def spec(rows=720, cols=960, regions=None, frames="ALL"):
    return parse_spec({
        "kind": "REDACT",
        "match": {"all": [{"tag": "Rows", "op": "eq", "value": rows}, {"tag": "Columns", "op": "eq", "value": cols}]},
        "regions": regions or [{"x": 0, "y": 0, "w": BAR_W, "h": BAR_H}],
        "frames": frames,
    })


@pytest.mark.parametrize("photometric", ["RGB", "MONOCHROME2"])
def test_region_is_black_and_everything_else_unchanged(photometric):
    ds = make_dataset(photometric=photometric)
    before = ds.pixel_array.copy()
    summary = pixel.redact(ds, spec())
    after = ds.pixel_array
    assert (after[:BAR_H, :BAR_W] == 0).all()
    mask = np.ones(before.shape[:2], bool)
    mask[:BAR_H, :BAR_W] = False
    assert (after[mask] == before[mask]).all()
    assert summary["pixelsChanged"] > 0
    assert ds.BurnedInAnnotation == "NO"
    assert ds.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian


def test_monochrome1_black_is_the_maximum():
    ds = make_dataset(photometric="MONOCHROME1")
    pixel.redact(ds, spec())
    assert (ds.pixel_array[:BAR_H, :BAR_W] == 255).all()


def test_signed_monochrome2_black_is_the_minimum():
    ds = make_dataset(photometric="MONOCHROME2", bits=16, signed=True)
    pixel.redact(ds, spec())
    assert (ds.pixel_array[:BAR_H, :BAR_W] == -(1 << 11)).all()  # BitsStored = 12


def test_unsigned_16_bit_black_is_zero():
    ds = make_dataset(photometric="MONOCHROME2", bits=16)
    pixel.redact(ds, spec())
    assert (ds.pixel_array[:BAR_H, :BAR_W] == 0).all()


def test_rle_input_is_decoded_and_written_uncompressed(tmp_path):
    ds = make_dataset(photometric="RGB", rows=64, cols=96)
    ds.compress(RLELossless)
    path = write(ds, tmp_path / "rle.dcm")
    ds = pydicom.dcmread(path)
    assert pixel.check_supported(ds).ok
    pixel.redact(ds, spec(64, 96, [{"x": 0, "y": 0, "w": 50, "h": 20}]))
    out = write(ds, tmp_path / "out.dcm")
    reread = pydicom.dcmread(out)
    assert reread.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
    assert (reread.pixel_array[:20, :50] == 0).all()
    assert reread.PlanarConfiguration == 0


def test_lossy_input_is_flagged_not_recompressed():
    ds = make_dataset(rows=32, cols=32)
    ds.file_meta.TransferSyntaxUID = "1.2.840.10008.1.2.4.50"
    assert ds.file_meta.TransferSyntaxUID in pixel.LOSSY_TRANSFER_SYNTAXES


def test_multiframe_selected_frames_only():
    ds = make_dataset(photometric="MONOCHROME2", rows=64, cols=64, frames=5)
    before = ds.pixel_array.copy()
    pixel.redact(ds, spec(64, 64, [{"x": 0, "y": 0, "w": 10, "h": 10}], frames=[2, 4]))
    after = ds.pixel_array
    for f in range(5):
        redacted = (after[f, :10, :10] == 0).all()
        assert redacted == (f in (1, 3))
    assert (after[0] == before[0]).all()


def test_region_frames_override_template_frames():
    ds = make_dataset(photometric="RGB", rows=32, cols=32, frames=3)
    pixel.redact(ds, spec(32, 32, [{"x": 0, "y": 0, "w": 8, "h": 8, "frames": [3]}]))
    a = ds.pixel_array
    assert not (a[0, :8, :8] == 0).all() and (a[2, :8, :8] == 0).all()


def test_dimension_mismatch_is_refused():
    ds = make_dataset(rows=64, cols=64)
    with pytest.raises(pixel.PixelError) as exc:
        pixel.redact(ds, spec())
    assert exc.value.code == "TEMPLATE_DIMENSION_MISMATCH"


@pytest.mark.parametrize("ts, ok", [
    ("1.2.840.10008.1.2.4.100", False),  # MPEG-2
    ("1.2.840.10008.1.2.2", False),      # explicit big endian (retired)
    ("1.2.840.10008.1.2.4.70", True),    # JPEG lossless SV1 (the GE sample format)
])
def test_transfer_syntax_support(ts, ok):
    ds = make_dataset(rows=8, cols=8)
    ds.file_meta.TransferSyntaxUID = ts
    check = pixel.check_supported(ds)
    assert check.ok is ok and (ok or check.code == "UNSUPPORTED_TS")


def test_palette_color_is_not_supported():
    ds = make_dataset(rows=8, cols=8, photometric="MONOCHROME2")
    ds.PhotometricInterpretation = "PALETTE COLOR"
    assert pixel.check_supported(ds).code == "UNSUPPORTED_PHOTOMETRIC"


def test_overlays_and_icons_are_removed():
    ds = make_dataset(rows=16, cols=16)
    ds.IconImageSequence = [pydicom.Dataset()]
    removed = pixel.strip_non_pixel_images(ds)
    assert not any(e.tag.group == 0x6000 for e in ds)
    assert "IconImageSequence" in removed and "IconImageSequence" not in ds


def test_pass_template_leaves_pixels():
    ds = make_dataset(rows=16, cols=16)
    before = ds.pixel_array.copy()
    pass_spec = parse_spec({"kind": "PASS", "match": {"all": [{"tag": "Modality", "op": "eq", "value": "CT"}]}})
    assert pixel.redact(ds, pass_spec)["pixelsChanged"] == 0
    assert (ds.pixel_array == before).all()
