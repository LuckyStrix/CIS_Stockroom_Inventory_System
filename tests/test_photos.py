"""Item photos, and the CSRF path they put under strain.

The upload feature itself is small. The risk is where it lands: the CSRF
middleware reads the raw request body by hand to find the token, and its
comment used to say "No file uploads in this application". These tests pin
down what that code now has to survive -- binary content in the body, a body
large enough to matter, and the requirement that the route still receives the
file intact afterwards.
"""

from __future__ import annotations

import io
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from stockroom import accounts, config, db, photos, service
from stockroom.photos import PhotoError
from stockroom.service import Actor, ConflictError

SETUP = Actor("cli:test")
STAFF_PASSWORD = "glass onion tuesday lamp"


def jpeg(size=(80, 60), colour="red", quality=90) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# processing
# ---------------------------------------------------------------------------


def test_a_large_photo_is_downscaled(temp_env):
    stored = photos.store(jpeg((4000, 3000)))
    assert (stored.width, stored.height) == (1600, 1200)
    assert stored.bytes < 400_000


def test_a_small_photo_is_not_upscaled(temp_env):
    stored = photos.store(jpeg((120, 90)))
    assert (stored.width, stored.height) == (120, 90)


def test_the_stored_file_is_a_real_jpeg(temp_env):
    stored = photos.store(jpeg())
    with Image.open(photos.photo_path(stored.filename)) as image:
        assert image.format == "JPEG"


def test_exif_is_stripped(temp_env):
    """Phone photos carry GPS coordinates and a device serial."""
    source = Image.new("RGB", (400, 300), "blue")
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[0x010F] = "SomeCameraMaker"      # Make
    exif[0x0112] = 1                      # Orientation
    source.save(buffer, "JPEG", exif=exif)

    stored = photos.store(buffer.getvalue())
    with Image.open(photos.photo_path(stored.filename)) as image:
        assert not dict(image.getexif())
    assert b"SomeCameraMaker" not in photos.photo_path(stored.filename).read_bytes()


def test_the_uploaders_filename_is_never_used(temp_env):
    """Generated names remove a whole category of problem, rather than
    trying to sanitise one."""
    stored = photos.store(jpeg())
    assert re.fullmatch(r"[0-9a-f]{32}\.jpg", stored.filename)


def test_two_uploads_never_collide(temp_env):
    names = {photos.store(jpeg()).filename for _ in range(5)}
    assert len(names) == 5


def test_a_file_that_is_not_an_image_is_refused(temp_env):
    with pytest.raises(PhotoError, match="not an image"):
        photos.store(b"%PDF-1.4\nnot really a photo at all")


def test_an_empty_upload_is_refused(temp_env):
    with pytest.raises(PhotoError, match="empty"):
        photos.store(b"")


def test_an_oversized_upload_is_refused_before_decoding(temp_env, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1024)
    with pytest.raises(PhotoError, match="limit is"):
        photos.store(jpeg((2000, 1500)))


@pytest.mark.parametrize(
    "name", ["../../etc/passwd", "sub/dir.jpg", "..\\\\windows", "", ".hidden"],
)
def test_a_path_is_never_built_from_an_arbitrary_name(temp_env, name):
    with pytest.raises(PhotoError):
        photos.photo_path(name)


# ---------------------------------------------------------------------------
# the service layer
# ---------------------------------------------------------------------------


def test_the_first_photo_becomes_the_main_one(conn, actor, item):
    photo = service.add_photo(conn, actor=actor, item_id=item.id, data=jpeg())
    assert photo.is_primary


def test_the_main_photo_can_be_changed(conn, actor, item):
    first = service.add_photo(conn, actor=actor, item_id=item.id, data=jpeg())
    second = service.add_photo(conn, actor=actor, item_id=item.id, data=jpeg())
    assert not second.is_primary

    service.set_primary_photo(conn, actor=actor, photo_id=second.id)

    assert service.get_photo(conn, second.id).is_primary
    assert not service.get_photo(conn, first.id).is_primary


def test_only_one_photo_is_ever_the_main_one(conn, actor, item):
    for _ in range(3):
        service.add_photo(conn, actor=actor, item_id=item.id, data=jpeg())
    primary = [p for p in service.list_photos(conn, item.id) if p.is_primary]
    assert len(primary) == 1


def test_removing_a_photo_promotes_another(conn, actor, item):
    """An item must not silently lose its thumbnail."""
    first = service.add_photo(conn, actor=actor, item_id=item.id, data=jpeg())
    second = service.add_photo(conn, actor=actor, item_id=item.id, data=jpeg())

    service.remove_photo(conn, actor=actor, photo_id=first.id)

    assert service.get_photo(conn, second.id).is_primary
    assert [p.id for p in service.list_photos(conn, item.id)] == [second.id]


def test_a_removed_photo_leaves_its_file_alone(conn, actor, item):
    """Soft delete, so a mis-click is recoverable."""
    photo = service.add_photo(conn, actor=actor, item_id=item.id, data=jpeg())
    service.remove_photo(conn, actor=actor, photo_id=photo.id)
    assert photos.photo_path(photo.filename).exists()


def test_a_photo_cannot_be_removed_twice(conn, actor, item):
    photo = service.add_photo(conn, actor=actor, item_id=item.id, data=jpeg())
    service.remove_photo(conn, actor=actor, photo_id=photo.id)
    with pytest.raises(ConflictError, match="already removed"):
        service.remove_photo(conn, actor=actor, photo_id=photo.id)


def test_a_rejected_upload_leaves_no_file_behind(conn, actor, item):
    before = set(config.PHOTO_DIR.glob("*")) if config.PHOTO_DIR.exists() else set()
    with pytest.raises(PhotoError):
        service.add_photo(conn, actor=actor, item_id=item.id, data=b"nope")
    after = set(config.PHOTO_DIR.glob("*")) if config.PHOTO_DIR.exists() else set()
    assert before == after


def test_a_failed_insert_cleans_up_its_file(conn, actor):
    """The file is written before the transaction; a rollback must undo it."""
    from stockroom.service import NotFound

    before = set(config.PHOTO_DIR.glob("*")) if config.PHOTO_DIR.exists() else set()
    with pytest.raises(NotFound):
        service.add_photo(conn, actor=actor, item_id=9999, data=jpeg())
    after = set(config.PHOTO_DIR.glob("*")) if config.PHOTO_DIR.exists() else set()
    assert before == after


def test_primary_photos_are_indexed_for_list_pages(conn, actor, item):
    photo = service.add_photo(conn, actor=actor, item_id=item.id, data=jpeg())
    assert service.primary_photos(conn) == {item.id: photo.filename}


# ---------------------------------------------------------------------------
# uploads over HTTP -- the CSRF middleware under a binary body
# ---------------------------------------------------------------------------


@pytest.fixture
def client(temp_env):
    from stockroom.web.app import app

    with TestClient(app) as test_client:
        accounts.register(
            db.connect(), first_name="Test", last_name="Operator",
            email="operator@rit.edu", password=STAFF_PASSWORD,
            role="staff", status="active", actor=SETUP,
        )
        token = re.search(r'name="_csrf" value="([^"]+)"',
                          test_client.get("/login").text).group(1)
        assert test_client.post(
            "/login",
            data={"email": "operator@rit.edu", "password": STAFF_PASSWORD,
                  "next": "/", "_csrf": token},
            follow_redirects=False,
        ).status_code == 303
        yield test_client


@pytest.fixture
def stocked(client):
    return service.create_item(db.connect(), actor=SETUP, name="USB-C cable",
                               quantity=5).id


def csrf(client, path):
    return re.search(r'name="_csrf" value="([^"]+)"', client.get(path).text).group(1)


def test_a_multipart_upload_passes_csrf_and_arrives_intact(client, stocked):
    """The test this whole feature hangs on.

    The CSRF middleware reads the raw body to find the token. If it consumed
    the stream, or choked on binary content, the route below would see an
    empty form -- silently, with a redirect that looks like success.
    """
    payload = jpeg((900, 700), "green")
    response = client.post(
        f"/items/{stocked}/photos",
        data={"_csrf": csrf(client, f"/items/{stocked}"), "caption": "the USB-C end"},
        files={"photo": ("shot.jpg", payload, "image/jpeg")},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Photo added" in response.text

    stored = service.list_photos(db.connect(), stocked)
    assert len(stored) == 1, "the route received no file -- the body was eaten"
    assert stored[0].caption == "the USB-C end"
    assert stored[0].width == 900


def test_an_upload_with_no_csrf_token_is_still_refused(client, stocked):
    response = client.post(
        f"/items/{stocked}/photos",
        data={"caption": "sneaky"},
        files={"photo": ("shot.jpg", jpeg(), "image/jpeg")},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert service.list_photos(db.connect(), stocked) == []


def test_the_csrf_field_comes_first_in_every_upload_form():
    """The middleware only scans the head of a multipart body.

    Scanning megabytes of JPEG with a regex for every upload is work with no
    possible payoff, so it stops after 16 KB. That is only safe while `_csrf`
    is the first field in any form that can carry a file.
    """
    from pathlib import Path

    templates = Path(__file__).resolve().parents[1] / "src" / "stockroom" / "templates"
    for path in templates.glob("*.html"):
        body = path.read_text()
        for form in re.findall(r"<form[^>]*multipart/form-data.*?</form>", body, re.S):
            fields = re.findall(r'name="([^"]+)"', form)
            assert fields and fields[0] == "_csrf", (
                f"{path.name}: a multipart form must put _csrf first, got {fields[:3]}"
            )


def test_a_body_over_the_limit_is_refused_before_it_is_read(client, stocked,
                                                            monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 2048)
    response = client.post(
        f"/items/{stocked}/photos",
        data={"_csrf": csrf(client, f"/items/{stocked}")},
        files={"photo": ("big.jpg", jpeg((3000, 2000)), "image/jpeg")},
        follow_redirects=False,
    )
    assert response.status_code == 413


def test_uploading_something_that_is_not_an_image_is_a_message_not_a_500(client,
                                                                        stocked):
    response = client.post(
        f"/items/{stocked}/photos",
        data={"_csrf": csrf(client, f"/items/{stocked}")},
        files={"photo": ("notes.pdf", b"%PDF-1.4 hello", "application/pdf")},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "not an image" in response.text


def test_submitting_the_form_with_no_file_says_so(client, stocked):
    response = client.post(
        f"/items/{stocked}/photos",
        data={"_csrf": csrf(client, f"/items/{stocked}"), "caption": ""},
        follow_redirects=True,
    )
    assert "No photo was chosen" in response.text


# ---------------------------------------------------------------------------
# serving them
# ---------------------------------------------------------------------------


def test_a_stored_photo_can_be_fetched(client, stocked):
    photo = service.add_photo(db.connect(), actor=SETUP, item_id=stocked,
                              data=jpeg())
    response = client.get(photo.url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"


def test_photos_are_not_public(temp_env, stocked):
    """Availability is public; what the stockroom's shelves look like is not."""
    from stockroom.web.app import app

    photo = service.add_photo(db.connect(), actor=SETUP, item_id=stocked,
                              data=jpeg())
    with TestClient(app) as anonymous:
        assert anonymous.get(photo.url, follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    "path",
    ["/photos/../../../etc/passwd", "/photos/..%2f..%2fetc%2fpasswd",
     "/photos/nothing-here.jpg"],
)
def test_traversal_and_missing_files_are_404(client, path):
    assert client.get(path).status_code == 404


def test_the_item_page_shows_its_photos(client, stocked):
    service.add_photo(db.connect(), actor=SETUP, item_id=stocked, data=jpeg(),
                      caption="the USB-C end")
    body = client.get(f"/items/{stocked}").text
    assert "the USB-C end" in body
    assert 'alt="the USB-C end"' in body
