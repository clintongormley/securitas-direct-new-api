"""Tests for CameraDevice and ThumbnailResponse dataclasses."""

from custom_components.securitas.securitas_direct_new_api.dataTypes import (
    CameraDevice,
    ThumbnailResponse,
)


class TestCameraDevice:
    """Tests for the CameraDevice dataclass."""

    def test_default_values(self):
        """Test CameraDevice has correct default values."""
        camera = CameraDevice()
        assert camera.id == ""
        assert camera.code == 0
        assert camera.zone_id == ""
        assert camera.name == ""
        assert camera.serial_number is None

    def test_with_values(self):
        """Test CameraDevice with explicit values."""
        camera = CameraDevice(
            id="CAM001",
            code=42,
            zone_id="Z3",
            name="Front Door Camera",
            serial_number="SN-123456",
        )
        assert camera.id == "CAM001"
        assert camera.code == 42
        assert camera.zone_id == "Z3"
        assert camera.name == "Front Door Camera"
        assert camera.serial_number == "SN-123456"


class TestThumbnailResponse:
    """Tests for the ThumbnailResponse dataclass."""

    def test_default_values(self):
        """Test ThumbnailResponse has correct default values."""
        thumb = ThumbnailResponse()
        assert thumb.id_signal is None
        assert thumb.device_code is None
        assert thumb.device_alias is None
        assert thumb.timestamp is None
        assert thumb.signal_type is None
        assert thumb.image is None

    def test_with_values(self):
        """Test ThumbnailResponse with explicit values."""
        thumb = ThumbnailResponse(
            id_signal="SIG-001",
            device_code="DEV-42",
            device_alias="Front Door",
            timestamp="2026-03-09T12:00:00Z",
            signal_type="MOTION",
            image="base64encodeddata==",
        )
        assert thumb.id_signal == "SIG-001"
        assert thumb.device_code == "DEV-42"
        assert thumb.device_alias == "Front Door"
        assert thumb.timestamp == "2026-03-09T12:00:00Z"
        assert thumb.signal_type == "MOTION"
        assert thumb.image == "base64encodeddata=="
