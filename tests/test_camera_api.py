"""Tests for CameraDevice, ThumbnailResponse dataclasses, and camera API methods."""

from unittest.mock import AsyncMock

import pytest

from custom_components.securitas.securitas_direct_new_api.dataTypes import (
    CameraDevice,
    Installation,
    ThumbnailResponse,
)
from custom_components.securitas.securitas_direct_new_api.exceptions import (
    SecuritasDirectError,
)

pytestmark = pytest.mark.asyncio


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def installation():
    return Installation(number="123456", alias="Home", panel="SDVFAST", type="PLUS")


@pytest.fixture
def authed_api(api):
    api._check_authentication_token = AsyncMock()
    api._check_capabilities_token = AsyncMock()
    api.delay_check_operation = 0
    return api


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


class TestGetDeviceList:
    DEVICE_LIST_RESPONSE = {
        "data": {
            "xSDeviceList": {
                "res": "OK",
                "devices": [
                    {"id": "1", "code": "1", "zoneId": "QR01", "name": "Cucina", "type": "QR", "isActive": True, "serialNumber": "36QYX3LE"},
                    {"id": "2", "code": "2", "zoneId": "MG02", "name": "Entrata", "type": "MG", "isActive": True, "serialNumber": None},
                    {"id": "9", "code": "9", "zoneId": "QR09", "name": "Cameretta", "type": "QR", "isActive": True, "serialNumber": "36NF2KPR"},
                    {"id": "11", "code": "10", "zoneId": "QR10", "name": "Salon", "type": "QR", "isActive": True, "serialNumber": "36NEYYER"},
                    {"id": "20", "code": "17", "zoneId": "QR17", "name": "Inactive", "type": "QR", "isActive": False, "serialNumber": None},
                ],
            }
        }
    }

    async def test_returns_only_active_qr_devices(self, authed_api, mock_execute, installation):
        mock_execute.return_value = self.DEVICE_LIST_RESPONSE
        result = await authed_api.get_device_list(installation)
        assert len(result) == 3
        assert all(isinstance(d, CameraDevice) for d in result)
        assert [d.name for d in result] == ["Cucina", "Cameretta", "Salon"]

    async def test_parses_device_fields(self, authed_api, mock_execute, installation):
        mock_execute.return_value = self.DEVICE_LIST_RESPONSE
        result = await authed_api.get_device_list(installation)
        salon = result[2]
        assert salon.id == "11"
        assert salon.code == 10
        assert salon.zone_id == "QR10"
        assert salon.name == "Salon"
        assert salon.serial_number == "36NEYYER"

    async def test_empty_device_list(self, authed_api, mock_execute, installation):
        mock_execute.return_value = {"data": {"xSDeviceList": {"res": "OK", "devices": []}}}
        result = await authed_api.get_device_list(installation)
        assert result == []

    async def test_no_cameras(self, authed_api, mock_execute, installation):
        mock_execute.return_value = {
            "data": {"xSDeviceList": {"res": "OK", "devices": [
                {"id": "2", "code": "2", "zoneId": "MG02", "name": "Entrata", "type": "MG", "isActive": True, "serialNumber": None},
            ]}}
        }
        result = await authed_api.get_device_list(installation)
        assert result == []


class TestRequestImages:
    async def test_success(self, authed_api, mock_execute, installation):
        mock_execute.return_value = {
            "data": {"xSRequestImages": {"res": "OK", "msg": "alarm-manager.processed.request", "referenceId": "4ebfe653-fa54-4805-874c-cea1c9ad927a"}}
        }
        ref_id = await authed_api.request_images(installation, device_code=10)
        assert ref_id == "4ebfe653-fa54-4805-874c-cea1c9ad927a"

    async def test_error_response(self, authed_api, mock_execute, installation):
        mock_execute.return_value = {
            "data": {"xSRequestImages": {"res": "ERROR", "msg": "some error", "referenceId": None}}
        }
        with pytest.raises(SecuritasDirectError):
            await authed_api.request_images(installation, device_code=10)


class TestGetThumbnail:
    async def test_success(self, authed_api, mock_execute, installation):
        mock_execute.return_value = {
            "data": {"xSGetThumbnail": {
                "idSignal": "15681796423", "deviceId": None, "deviceCode": "QR10",
                "deviceAlias": "Salon", "timestamp": "2026-03-09 17:47:13",
                "signalType": "16", "image": "/9j/4AAQSkZJRgABAQEAAA==",
                "type": "BINARY", "quality": "",
            }}
        }
        result = await authed_api.get_thumbnail(installation, device_name="Salon", zone_id="QR10")
        assert isinstance(result, ThumbnailResponse)
        assert result.id_signal == "15681796423"
        assert result.device_code == "QR10"
        assert result.device_alias == "Salon"
        assert result.image == "/9j/4AAQSkZJRgABAQEAAA=="

    async def test_no_image_available(self, authed_api, mock_execute, installation):
        mock_execute.return_value = {
            "data": {"xSGetThumbnail": {
                "idSignal": None, "deviceId": None, "deviceCode": None,
                "deviceAlias": None, "timestamp": None, "signalType": None,
                "image": None, "type": None, "quality": None,
            }}
        }
        result = await authed_api.get_thumbnail(installation, device_name="Salon", zone_id="QR10")
        assert result.image is None
        assert result.id_signal is None
