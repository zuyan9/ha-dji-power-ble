"""Explicit local BlueZ connections and opt-in detachment at HA shutdown."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from importlib.metadata import version
from typing import TYPE_CHECKING, Any

from bleak.exc import BleakError

if TYPE_CHECKING:
    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.device import BLEDevice
    from bleak.backends.service import BleakGATTServiceCollection

_ADAPTER_INTERFACE = "org.bluez.Adapter1"
_DEVICE_INTERFACE = "org.bluez.Device1"
_ADAPTER_DETAIL = "dji_power_local_adapter"
_backend_type: type | None = None
_LOGGER = logging.getLogger(__name__)


async def _manager() -> Any:
    """Get BlueZ's current object view without choosing an adapter."""
    from bleak.backends.bluezdbus.manager import get_global_bluez_manager

    try:
        async with asyncio.timeout(5):
            return await get_global_bluez_manager()
    except Exception as error:
        raise BleakError("Cannot inspect local Bluetooth adapters") from error


def _powered_adapters(manager: Any) -> dict[str, tuple[str, dict[str, Any]]]:
    """Index powered local controllers by their stable hardware address."""
    properties = getattr(manager, "_properties", None)
    if not isinstance(properties, dict):
        raise BleakError("Unsupported BlueZ adapter discovery interface")
    adapters = {}
    for path, interfaces in properties.items():
        props = interfaces.get(_ADAPTER_INTERFACE)
        if not props or not props.get("Powered") or not props.get("Address"):
            continue
        if not path.startswith("/org/bluez/hci"):
            continue
        if "Roles" in props and "central" not in props["Roles"]:
            continue
        adapters[props["Address"].upper()] = (path, props)
    return adapters


async def async_local_adapters() -> dict[str, str]:
    """List powered local Linux adapters, keyed by hardware address."""
    if sys.platform != "linux":
        return {}
    adapters = _powered_adapters(await _manager())
    result = {}
    for address, (path, props) in adapters.items():
        adapter = path.rsplit("/", 1)[-1]
        name = props.get("Alias") or props.get("Name") or adapter
        result[address] = f"{name} ({adapter}, {address})"
    return result


async def async_local_device(
    adapter_address: str, station_address: str
) -> BLEDevice | None:
    """Find a known station on one local adapter, including retained links."""
    if sys.platform != "linux":
        return None
    from bleak.backends.device import BLEDevice

    manager = await _manager()
    selected = _powered_adapters(manager).get(adapter_address.upper())
    if selected is None:
        return None
    adapter_path, _ = selected
    for path, interfaces in manager._properties.items():
        props = interfaces.get(_DEVICE_INTERFACE)
        if (
            not props
            or props.get("Adapter") != adapter_path
            or not path.startswith(f"{adapter_path}/dev_")
            or props.get("Address", "").upper() != station_address.upper()
        ):
            continue
        return BLEDevice(
            props["Address"],
            props.get("Name") or props.get("Alias"),
            {
                "path": path,
                "props": dict(props),
                _ADAPTER_DETAIL: adapter_address.upper(),
            },
        )
    return None


def _local_backend_type() -> type:
    """Create an instance-scoped backend; never patch HA's shared BLE stack."""
    global _backend_type
    if _backend_type is not None:
        return _backend_type
    if sys.platform != "linux":
        raise BleakError("Explicit local Bluetooth requires Linux BlueZ")
    # Detachment uses private fields verified against HA's minimum Bleak 1.0.1
    # and current 3.0.2. Reject an unknown major instead of guessing its cleanup.
    installed = version("bleak")
    if installed.split(".", 1)[0] not in {"1", "2", "3"}:
        raise BleakError("This Bleak version does not support local session retention")
    from bleak.backends.bluezdbus.client import BleakClientBlueZDBus

    class LocalBlueZBackend(BleakClientBlueZDBus):
        """Record the upstream monitor so detachment can let it finish normally."""

        _retention_monitor_task: asyncio.Task | None = None

        async def _disconnect_monitor(self, bus, path, event) -> None:
            self._retention_monitor_task = asyncio.current_task()
            await super()._disconnect_monitor(bus, path, event)

    _backend_type = LocalBlueZBackend
    return _backend_type


class LocalBleakClient:
    """The BLE operations used by the integration on one pinned BlueZ device."""

    def __init__(
        self,
        device: BLEDevice,
        *,
        disconnected_callback: Callable[[LocalBleakClient], None] | None = None,
    ) -> None:
        details = device.details
        if not isinstance(details, dict) or _ADAPTER_DETAIL not in details:
            raise BleakError("The station was not resolved on a selected local adapter")
        self._device = device
        self._disconnected_callback = disconnected_callback
        self.connected_before_attach = False
        self._backend = _local_backend_type()(
            device,
            bluez={},
            timeout=10.0,
            disconnected_callback=self._on_backend_disconnect,
        )

    def _on_backend_disconnect(self) -> None:
        """Release D-Bus on unexpected loss before the owner drops this client."""
        backend = self._backend
        self.connected_before_attach = False
        # Bleak 3 keeps the bus after its disconnect signal cleanup. An explicit
        # disconnect still needs that bus until its method reply has arrived.
        if backend._disconnecting_event is None and backend._bus is not None:
            backend._bus.disconnect()
            backend._bus = None
        if self._disconnected_callback is not None:
            self._disconnected_callback(self)

    @property
    def is_connected(self) -> bool:
        """Return whether this client currently owns an attached BLE session."""
        return self._backend.is_connected

    @property
    def services(self) -> BleakGATTServiceCollection:
        """Return the GATT table for the attached connection."""
        services = self._backend.services
        if services is None:
            raise BleakError("Local Bluetooth services have not been discovered")
        return services

    def _selected_device_matches(self, manager: Any) -> bool:
        """Require the device path to still belong to the chosen controller."""
        details = self._device.details
        path = details["path"]
        selected = _powered_adapters(manager).get(details[_ADAPTER_DETAIL])
        props = manager._properties.get(path, {}).get(_DEVICE_INTERFACE, {})
        return (
            selected is not None
            and props.get("Adapter") == selected[0]
            and path.startswith(f"{selected[0]}/dev_")
            and props.get("Address", "").upper() == self._device.address.upper()
        )

    async def connect(self) -> None:
        """Attach to the selected controller, never selecting another source."""
        self.connected_before_attach = False
        manager = await _manager()
        if not self._selected_device_matches(manager):
            raise BleakError("The selected local Bluetooth device is unavailable")
        path = self._device.details["path"]

        # Watch before yielding: a disconnect/reconnect during attachment must
        # not turn an old snapshot into permission to reuse its authentication.
        remained_connected = manager.is_connected(path)

        def connection_changed(connected: bool) -> None:
            nonlocal remained_connected
            if not connected:
                remained_connected = False

        watcher = manager.add_device_watcher(path, connection_changed, lambda *_: None)
        try:
            await self._backend.connect(pair=False)
            if not self._selected_device_matches(manager):
                await self._backend.disconnect()
                raise BleakError("The selected local Bluetooth device changed")
            self.connected_before_attach = remained_connected and self.is_connected
        finally:
            manager.remove_device_watcher(watcher)

    async def start_notify(
        self,
        characteristic: BleakGATTCharacteristic,
        callback: Callable[[BleakGATTCharacteristic, bytearray], None],
    ) -> None:
        """Subscribe using the normal Bleak callback shape."""
        await self._backend.start_notify(
            characteristic,
            lambda data: callback(characteristic, data),
            bluez={"use_start_notify": True},
        )

    async def write_gatt_char(
        self, characteristic: BleakGATTCharacteristic, data: bytes, *, response: bool
    ) -> None:
        """Write to this connection's selected characteristic."""
        await self._backend.write_gatt_char(characteristic, data, response)

    async def disconnect(self) -> None:
        """Normally release both the station link and local resources."""
        await self._backend.disconnect()

    async def detach(self) -> None:
        """Close our D-Bus session while leaving an established link in BlueZ."""
        backend = self._backend
        if not backend.is_connected:
            await backend.disconnect()
            return
        event = getattr(backend, "_disconnect_monitor_event", None)
        bus = getattr(backend, "_bus", None)
        if (
            not isinstance(event, asyncio.Event)
            or bus is None
            or getattr(backend, "_disconnecting_event", None) is not None
            or not isinstance(getattr(backend, "_notification_callbacks", None), dict)
            or getattr(backend, "_notification_fds", {})
            or not callable(getattr(bus, "disconnect", None))
            or not callable(getattr(bus, "wait_for_disconnect", None))
            or not hasattr(backend, "_remove_device_watcher")
        ):
            raise BleakError("The BlueZ client cannot safely retain this connection")

        # Everything before the first await is synchronous: disarm the upstream
        # cancellation monitor, remove callbacks, and close D-Bus before HA starts
        # cancelling background tasks. No Device1.Disconnect is sent here.
        backend.set_disconnected_callback(None)
        event.set()
        backend._disconnect_monitor_event = None
        if backend._remove_device_watcher is not None:
            backend._remove_device_watcher()
            backend._remove_device_watcher = None
        backend._notification_callbacks.clear()
        backend._is_connected = False
        backend.services = None
        backend._bus = None
        bus.disconnect()

        # A monitor created immediately before shutdown may not have started yet.
        try:
            async with asyncio.timeout(5):
                await asyncio.sleep(0)
                if (monitor := backend._retention_monitor_task) is not None:
                    await asyncio.shield(monitor)
                await bus.wait_for_disconnect()
        except (TimeoutError, OSError):
            # Detachment is already committed. An incomplete local drain must
            # not be reported as permission to disconnect the retained station.
            _LOGGER.debug("Local Bluetooth detached; D-Bus cleanup did not finish")
