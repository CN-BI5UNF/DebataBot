"""ImageAssetStore：真实图片、安全边界、规范化、缓存与 v1 兼容。"""

from __future__ import annotations

import hashlib
import io
import json
import os
from copy import deepcopy

import pytest
from PIL import Image

from features.vision import image_assets as image_assets_module
from features.vision.image_assets import (
    ImageAssetError,
    ImageAssetStore,
    prepare_image_data_url,
)
from utils.image_contracts import (
    GIF_TRANSFORM_VERSION,
    TRANSFORM_VERSION_V1,
    ImageLimits,
    asset_relative_path,
    validate_image_asset_ref,
)


def _png_bytes(width: int = 64, height: int = 48, color=(255, 0, 0)) -> bytes:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_bytes(width: int = 64, height: int = 48, color=(0, 128, 0)) -> bytes:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def _decode_data_url(url: str) -> bytes:
    import base64

    prefix, encoded = url.split(",", 1)
    assert prefix.startswith("data:image/")
    return base64.b64decode(encoded)


@pytest.mark.asyncio
async def test_ingest_real_png_and_jpeg_uses_v2_content_addressed_path(tmp_path):
    store = ImageAssetStore(tmp_path / "workspace")
    png = await store.ingest(_png_bytes(), message_id="m1", index=0)
    jpeg = await store.ingest(_jpeg_bytes(), message_id="m2", index=1)

    assert png["mime"] == "image/png"
    assert jpeg["mime"] == "image/jpeg"
    assert png["path"].startswith("incoming/images/pillow-first-frame-max2048-v2/")
    assert jpeg["path"].endswith(".jpg")
    assert png["transform_version"] == store.transform_version
    validate_image_asset_ref(png)
    validate_image_asset_ref(jpeg)
    assert (tmp_path / "workspace" / png["path"]).is_file()


@pytest.mark.asyncio
async def test_extension_mismatch_uses_actual_content(tmp_path):
    source = tmp_path / "actually-png.jpg"
    source.write_bytes(_png_bytes())
    store = ImageAssetStore(tmp_path / "workspace")

    ref = await store.ingest(source, trusted_local=True)

    assert ref["mime"] == "image/png"
    assert ref["path"].endswith(".png")


@pytest.mark.asyncio
async def test_exif_orientation_is_applied(tmp_path):
    image = Image.new("RGB", (100, 50), (0, 0, 255))
    exif = Image.Exif()
    exif[274] = 6
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    store = ImageAssetStore(tmp_path / "workspace")

    ref = await store.ingest(buffer.getvalue())

    assert (ref["width"], ref["height"]) == (50, 100)


def _animated_gif_bytes() -> bytes:
    first = Image.new("RGB", (32, 16), (255, 0, 0))
    second = Image.new("RGB", (32, 16), (0, 255, 0))
    buffer = io.BytesIO()
    first.save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
    )
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_animated_gif_is_preserved_complete(tmp_path):
    source = _animated_gif_bytes()
    store = ImageAssetStore(tmp_path / "workspace")

    ref = await store.ingest(source)
    data = store._read_asset(ref)

    assert ref["mime"] == "image/gif"
    assert ref["transform_version"] == GIF_TRANSFORM_VERSION
    assert ref["path"].endswith(".gif")
    assert data == source
    validate_image_asset_ref(ref)
    block = await store.image_content(ref)
    assert block["image_url"]["url"].startswith("data:image/gif;base64,")
    with Image.open(io.BytesIO(data)) as output:
        assert output.n_frames == 2


@pytest.mark.asyncio
async def test_animated_gif_can_still_use_first_frame_mode(tmp_path):
    first = Image.new("RGB", (32, 16), (255, 0, 0))
    second = Image.new("RGB", (32, 16), (0, 255, 0))
    buffer = io.BytesIO()
    first.save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
    )
    store = ImageAssetStore(tmp_path / "workspace", gif_mode="first_frame")

    ref = await store.ingest(buffer.getvalue())
    data = store._read_asset(ref)

    assert ref["mime"] == "image/png"
    assert ref["transform_version"] == store.transform_version
    with Image.open(io.BytesIO(data)) as output:
        assert output.size == (32, 16)
        assert output.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


@pytest.mark.asyncio
async def test_max_side_scales_down_without_upscaling(tmp_path):
    limits = ImageLimits(max_image_side=64)
    store = ImageAssetStore(tmp_path / "workspace", limits=limits)

    large = await store.ingest(_png_bytes(200, 100))
    small = await store.ingest(_png_bytes(20, 10))

    assert max(large["width"], large["height"]) == 64
    assert large["transform_version"] == "pillow-first-frame-max64-v2"
    assert (small["width"], small["height"]) == (20, 10)


@pytest.mark.asyncio
async def test_empty_corrupt_and_over_byte_pixel_limits_are_rejected(tmp_path):
    store = ImageAssetStore(tmp_path / "workspace")
    with pytest.raises(ImageAssetError):
        await store.ingest(b"")
    with pytest.raises(ImageAssetError):
        await store.ingest(b"not an image")

    tiny_limits = ImageLimits(max_image_bytes=100)
    tiny_store = ImageAssetStore(tmp_path / "workspace2", limits=tiny_limits)
    with pytest.raises(ImageAssetError):
        await tiny_store.ingest(_png_bytes(64, 48))

    pixel_limits = ImageLimits(max_image_pixels=100)
    pixel_store = ImageAssetStore(tmp_path / "workspace3", limits=pixel_limits)
    with pytest.raises(ImageAssetError):
        await pixel_store.ingest(_png_bytes(64, 48))


def test_local_source_requires_explicit_trust(tmp_path):
    source = tmp_path / "local.png"
    source.write_bytes(_png_bytes())

    async def run():
        store = ImageAssetStore(tmp_path / "workspace")
        with pytest.raises(ImageAssetError):
            await store.ingest(str(source), trusted_local=False)
        ref = await store.ingest(str(source), trusted_local=True)
        assert ref["mime"] == "image/png"

    import asyncio

    asyncio.run(run())


@pytest.mark.asyncio
async def test_fifo_and_symlink_escape_are_rejected(tmp_path):
    store = ImageAssetStore(tmp_path / "workspace")
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        with pytest.raises(ImageAssetError):
            await store.ingest(str(fifo), trusted_local=True)

    outside = tmp_path / "outside.png"
    outside.write_bytes(_png_bytes())
    ref = await store.ingest(_png_bytes(), message_id="sym", index=0)
    managed = tmp_path / "workspace" / ref["path"]
    managed.unlink()
    try:
        managed.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台/权限不能创建符号链接")
    with pytest.raises(ImageAssetError):
        await store.image_content(ref)


@pytest.mark.asyncio
async def test_cache_hits_evictions_and_zero_cache(tmp_path):
    store = ImageAssetStore(tmp_path / "workspace")
    ref1 = await store.ingest(_png_bytes(32, 32, (255, 0, 0)), message_id="a", index=0)
    ref2 = await store.ingest(_png_bytes(32, 32, (0, 255, 0)), message_id="b", index=0)

    first = await store.image_content(ref1)
    again = await store.image_content(ref1)
    assert first == again
    info = store.cache_info()
    assert info["misses"] == 1
    assert info["hits"] == 1
    assert info["entries"] == 1

    one_entry_limit = len(first["image_url"]["url"].encode("ascii")) + 10
    small_store = ImageAssetStore(
        tmp_path / "workspace",
        cache_max_bytes=one_entry_limit,
    )
    await small_store.image_content(ref1)
    await small_store.image_content(ref2)
    info = small_store.cache_info()
    assert info["entries"] == 1
    assert info["evictions"] >= 1
    assert info["current_bytes"] <= one_entry_limit

    zero_store = ImageAssetStore(tmp_path / "workspace", cache_max_bytes=0)
    await zero_store.image_content(ref1)
    info = zero_store.cache_info()
    assert info["entries"] == 0
    assert info["misses"] == 1


@pytest.mark.asyncio
async def test_corrupt_file_fails_even_after_cache_hit(tmp_path):
    store = ImageAssetStore(tmp_path / "workspace")
    ref = await store.ingest(_png_bytes(), message_id="m", index=0)
    await store.image_content(ref)

    managed = tmp_path / "workspace" / ref["path"]
    managed.write_bytes(_png_bytes(8, 8, (1, 2, 3)))

    with pytest.raises(ImageAssetError):
        await store.image_content(ref)


@pytest.mark.asyncio
async def test_existing_content_mismatch_is_never_overwritten(tmp_path):
    store = ImageAssetStore(tmp_path / "workspace")
    data = _png_bytes()
    ref = await store.ingest(data)
    managed = tmp_path / "workspace" / ref["path"]
    managed.write_bytes(_png_bytes(8, 8, (9, 9, 9)))

    with pytest.raises(ImageAssetError):
        await store.ingest(data)


@pytest.mark.asyncio
async def test_v1_asset_is_readable_without_reencoding(tmp_path):
    data = _png_bytes(20, 10)
    sha = hashlib.sha256(data).hexdigest()
    relative = asset_relative_path(TRANSFORM_VERSION_V1, sha, "image/png")
    path = tmp_path / "workspace" / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    ref = {
        "sha256": sha,
        "mime": "image/png",
        "width": 20,
        "height": 10,
        "byte_size": len(data),
        "path": relative,
        "detail": "auto",
        "message_id": "old",
        "index": 0,
        "transform_version": TRANSFORM_VERSION_V1,
    }
    store = ImageAssetStore(tmp_path / "workspace")

    block = await store.image_content(ref)

    assert block["image_url"]["url"].startswith("data:image/png;base64,")
    assert path.read_bytes() == data


@pytest.mark.asyncio
async def test_project_record_is_copy_on_write_and_not_idempotent(tmp_path):
    store = ImageAssetStore(tmp_path / "workspace")
    ref = await store.ingest(_png_bytes(), message_id="m1", index=0)
    record = {
        "role": "user",
        "content": "请看图",
        "metadata": {"image_assets": [ref], "messages": [{"message_id": "m1"}]},
    }
    original = deepcopy(record)

    projected = await store.project_record(record)

    assert record == original
    assert projected is not record
    assert isinstance(projected["content"], list)
    assert projected["content"][0] == {"type": "text", "text": "请看图"}
    assert "data:image" in projected["content"][2]["image_url"]["url"]
    assert "data:image" not in json.dumps(record, ensure_ascii=False)
    with pytest.raises(ImageAssetError):
        await store.project_record(projected)


@pytest.mark.asyncio
async def test_project_records_keeps_multi_message_attribution(tmp_path):
    store = ImageAssetStore(tmp_path / "workspace")
    ref_a = await store.ingest(_png_bytes(16, 16, (255, 0, 0)), message_id="a", index=0)
    ref_b = await store.ingest(_png_bytes(16, 16, (0, 0, 255)), message_id="b", index=0)

    projected = await store.project_records(
        [
            {"role": "user", "content": "A", "metadata": {"image_assets": [ref_a]}},
            {"role": "user", "content": "B", "metadata": {"image_assets": [ref_b]}},
        ]
    )

    assert "msg_id=a" in projected[0]["content"][1]["text"]
    assert "msg_id=b" in projected[1]["content"][1]["text"]


@pytest.mark.asyncio
async def test_prepare_image_data_url_uses_same_validation(tmp_path):
    data_url = await prepare_image_data_url(_png_bytes(10, 10))
    assert data_url.startswith("data:image/png;base64,")
    with pytest.raises(ImageAssetError):
        await prepare_image_data_url(b"bad image")

    source = tmp_path / "local.png"
    source.write_bytes(_png_bytes(10, 10))
    with pytest.raises(ImageAssetError):
        await prepare_image_data_url(str(source), trusted_local=False)


@pytest.mark.asyncio
async def test_http_url_validation_rejects_unsafe_sources(monkeypatch):
    with pytest.raises(ImageAssetError):
        await image_assets_module._public_addresses("127.0.0.1", 80)
    with pytest.raises(ImageAssetError):
        await image_assets_module._public_addresses("localhost", 80)

    async def fake_public(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(image_assets_module, "_public_addresses", fake_public)
    url, host, port, address = await image_assets_module._checked_remote_url(
        "https://example.com/a.png"
    )
    assert host == "example.com"
    assert port == 443
    assert address == "93.184.216.34"
    assert url.host == "example.com"

    for unsafe in (
        "file:///etc/passwd",
        "http://user:pass@example.com/a.png",
    ):
        with pytest.raises(ImageAssetError):
            await image_assets_module._checked_remote_url(unsafe)


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict, body: bytes = b"") -> None:
        self.status_code = status_code
        self.headers = headers
        self._body = body

    async def aiter_raw(self, chunk_size: int = 64 * 1024):
        for offset in range(0, len(self._body), chunk_size):
            yield self._body[offset:offset + chunk_size]

    async def aclose(self) -> None:
        pass


def _fake_transport(monkeypatch, responses, seen):
    class _Transport:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            pass

        async def handle_async_request(self, request):
            seen.append(
                {
                    "host": request.url.host,
                    "host_header": request.headers.get("host"),
                    "sni": request.extensions.get("sni_hostname"),
                }
            )
            return responses.pop(0)

    monkeypatch.setattr(image_assets_module.httpx, "AsyncHTTPTransport", _Transport)


@pytest.mark.asyncio
async def test_http_download_pins_public_ip_and_preserves_host_sni(monkeypatch):
    async def fake_public(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(image_assets_module, "_public_addresses", fake_public)
    seen: list[dict] = []
    body = _png_bytes(8, 8)
    _fake_transport(
        monkeypatch,
        [_FakeResponse(200, {"content-length": str(len(body))}, body)],
        seen,
    )

    data = await image_assets_module._download_image(
        "https://example.com/a.png",
        ImageLimits(),
    )

    assert data == body
    assert seen == [
        {
            "host": "93.184.216.34",
            "host_header": "example.com",
            "sni": "example.com",
        }
    ]


@pytest.mark.asyncio
async def test_http_redirect_to_private_address_is_rejected(monkeypatch):
    async def fake_public(host: str, port: int) -> list[str]:
        if host == "127.0.0.1":
            raise ImageAssetError("private")
        return ["93.184.216.34"]

    monkeypatch.setattr(image_assets_module, "_public_addresses", fake_public)
    seen: list[dict] = []
    _fake_transport(
        monkeypatch,
        [_FakeResponse(302, {"location": "http://127.0.0.1/secret.png"})],
        seen,
    )

    with pytest.raises(ImageAssetError):
        await image_assets_module._download_image(
            "https://example.com/a.png",
            ImageLimits(),
        )


@pytest.mark.asyncio
async def test_http_content_length_and_stream_limits(monkeypatch):
    async def fake_public(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(image_assets_module, "_public_addresses", fake_public)
    limits = ImageLimits(max_image_bytes=32)

    seen: list[dict] = []
    _fake_transport(
        monkeypatch,
        [_FakeResponse(200, {"content-length": "33"}, b"x")],
        seen,
    )
    with pytest.raises(ImageAssetError):
        await image_assets_module._download_image(
            "https://example.com/a.png",
            limits,
        )

    seen = []
    _fake_transport(
        monkeypatch,
        [_FakeResponse(200, {}, b"x" * 33)],
        seen,
    )
    with pytest.raises(ImageAssetError):
        await image_assets_module._download_image(
            "https://example.com/a.png",
            limits,
        )


@pytest.mark.asyncio
async def test_public_addresses_rejects_mixed_dns_answers(monkeypatch):
    class _Loop:
        async def getaddrinfo(self, host, port, **kwargs):
            return [
                (2, 1, 6, "", ("93.184.216.34", 0)),
                (2, 1, 6, "", ("10.0.0.1", 0)),
            ]

    monkeypatch.setattr(
        image_assets_module.asyncio,
        "get_running_loop",
        lambda: _Loop(),
    )
    with pytest.raises(ImageAssetError):
        await image_assets_module._public_addresses("example.com", 443)


def test_public_ip_filter_covers_private_and_transition_addresses():
    assert image_assets_module._is_public_ip("93.184.216.34")
    for private in (
        "127.0.0.1",
        "10.0.0.1",
        "169.254.1.1",
        "::1",
        "fe80::1",
        "::ffff:10.0.0.1",
    ):
        assert not image_assets_module._is_public_ip(private)
