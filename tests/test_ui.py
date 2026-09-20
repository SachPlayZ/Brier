"""UI helpers. `image_dims` feeds the receipt image frame, so it must never raise."""
from types import SimpleNamespace

from PIL import Image

from expenses.templatetags.expenses_ui import image_dims


def _field(path, present=True):
    """Stand-in for a FieldFile: truthy when a file is attached, exposes .path."""
    class F(SimpleNamespace):
        def __bool__(self):
            return present
    return F(path=str(path))


def test_returns_size_of_a_real_image(tmp_path):
    p = tmp_path / "r.png"
    Image.new("RGB", (404, 580)).save(p)
    assert image_dims(_field(p)) == (404, 580)


def test_missing_file_gives_none_not_an_exception(tmp_path):
    assert image_dims(_field(tmp_path / "gone.png")) is None


def test_corrupt_file_gives_none(tmp_path):
    p = tmp_path / "bad.png"
    p.write_bytes(b"not an image")
    assert image_dims(_field(p)) is None


def test_empty_field_gives_none():
    assert image_dims(_field("", present=False)) is None
    assert image_dims(None) is None


def test_storage_without_a_path_gives_none():
    class Remote:            # e.g. S3 storage: .path raises NotImplementedError
        def __bool__(self):
            return True

        @property
        def path(self):
            raise NotImplementedError

    assert image_dims(Remote()) is None
