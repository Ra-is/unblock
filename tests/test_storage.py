import pytest

from unblock.storage import MAX_BYTES, validate_file


@pytest.mark.parametrize(
    "content,kind",
    [
        (b"", "text/plain"),
        (b"not pdf", "application/pdf"),
        (b"<script>bad</script>", "text/html"),
        (b"x" * (MAX_BYTES + 1), "text/plain"),
        (b"fake", "image/png"),
    ],
)
def test_reject_invalid_upload(content, kind):
    with pytest.raises(ValueError):
        validate_file(content, kind)
