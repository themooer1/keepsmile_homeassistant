import asyncio
from homeassistant.components import bluetooth
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.components.light import (ColorMode)
from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTCharacteristic, BleakGATTServiceCollection
from bleak.exc import BleakDBusError
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    #BleakError,
    BleakNotFoundError,
    #ble_device_has_changed,
    establish_connection,
)
from cheshire.compiler.compiler import StateCompiler
from cheshire.compiler.state import LightState
from cheshire.generic.command import *
from cheshire.hal.devices import device_profile_from_ble_device
from cheshire.communication.transmitter import Transmitter
from typing import Any, TypeVar, cast, Tuple
from collections.abc import Callable
#import traceback
import logging
import colorsys


LOGGER = logging.getLogger(__name__)

# EFFECT_0x03_0x00 = "Colorloop"
# EFFECT_0x03_0x01 = "Red fade"
# EFFECT_0x03_0x02 = "Green fade"
# EFFECT_0x03_0x03 = "Blue fade"
# EFFECT_0x03_0x04 = "Yellow fade"
# EFFECT_0x03_0x05 = "Cyan fade"
# EFFECT_0x03_0x06 = "Magenta fade"
# EFFECT_0x03_0x07 = "White fade"
# EFFECT_0x03_0x08 = "Red green cross fade"
# EFFECT_0x03_0x09 = "Red blue cross fade"
# EFFECT_0x03_0x0a = "Green blue cross fade"
# EFFECT_0x03_0x0b = "effect_0x03_0x0b"
# EFFECT_0x03_0x0c = "Color strobe"
# EFFECT_0x03_0x0d = "Red strobe"
# EFFECT_0x03_0x0e = "Green strobe"
# EFFECT_0x03_0x0f = "Blue strobe"
# EFFECT_0x03_0x10 = "Yellow strobe"
# EFFECT_0x03_0x11 = "Cyan strobe"
# EFFECT_0x03_0x12 = "Magenta strobe"
# EFFECT_0x03_0x13 = "White strobe"
# EFFECT_0x03_0x14 = "Color jump"
# EFFECT_0x03_0x15 = "RGB jump"


# EFFECT_MAP = {
#     EFFECT_0x03_0x00:    (0x03,0x00),
#     EFFECT_0x03_0x01:    (0x03,0x01),
#     EFFECT_0x03_0x02:    (0x03,0x02),
#     EFFECT_0x03_0x03:    (0x03,0x03),
#     EFFECT_0x03_0x04:    (0x03,0x04),
#     EFFECT_0x03_0x05:    (0x03,0x05),
#     EFFECT_0x03_0x06:    (0x03,0x06),
#     EFFECT_0x03_0x07:    (0x03,0x07),
#     EFFECT_0x03_0x08:    (0x03,0x08),
#     EFFECT_0x03_0x09:    (0x03,0x09),
#     EFFECT_0x03_0x0a:    (0x03,0x0a),
#     EFFECT_0x03_0x0b:    (0x03,0x0b),
#     EFFECT_0x03_0x0c:    (0x03,0x0c),
#     EFFECT_0x03_0x0d:    (0x03,0x0d),
#     EFFECT_0x03_0x0e:    (0x03,0x0e),
#     EFFECT_0x03_0x0f:    (0x03,0x0f),
#     EFFECT_0x03_0x10:    (0x03,0x10),
#     EFFECT_0x03_0x11:    (0x03,0x11),
#     EFFECT_0x03_0x12:    (0x03,0x12),
#     EFFECT_0x03_0x13:    (0x03,0x13),
#     EFFECT_0x03_0x14:    (0x03,0x14),
#     EFFECT_0x03_0x15:    (0x03,0x15)
# }

EFFECT_LIST = [e.value for e in Effect]
# EFFECT_ID_NAME = {v: k for k, v in EFFECT_MAP.items()}

DEFAULT_ATTEMPTS = 3
BLEAK_BACKOFF_TIME = 0.25
RETRY_BACKOFF_EXCEPTIONS = (BleakDBusError)

WrapFuncType = TypeVar("WrapFuncType", bound=Callable[..., Any])

def retry_bluetooth_connection_error(func: WrapFuncType) -> WrapFuncType:
    async def _async_wrap_retry_bluetooth_connection_error(
        self: "BJLEDInstance", *args: Any, **kwargs: Any
    ) -> Any:
        attempts = DEFAULT_ATTEMPTS
        max_attempts = attempts - 1

        for attempt in range(attempts):
            try:
                return await func(self, *args, **kwargs)
            except BleakNotFoundError:
                # The lock cannot be found so there is no
                # point in retrying.
                raise
            except RETRY_BACKOFF_EXCEPTIONS as err:
                if attempt >= max_attempts:
                    LOGGER.debug(
                        "%s: %s error calling %s, reach max attempts (%s/%s)",
                        self.name,
                        type(err),
                        func,
                        attempt,
                        max_attempts,
                        exc_info=True,
                    )
                    raise
                LOGGER.debug(
                    "%s: %s error calling %s, backing off %ss, retrying (%s/%s)...",
                    self.name,
                    type(err),
                    func,
                    BLEAK_BACKOFF_TIME,
                    attempt,
                    max_attempts,
                    exc_info=True,
                )
                await asyncio.sleep(BLEAK_BACKOFF_TIME)
            except BLEAK_EXCEPTIONS as err:
                if attempt >= max_attempts:
                    LOGGER.debug(
                        "%s: %s error calling %s, reach max attempts (%s/%s): %s",
                        self.name,
                        type(err),
                        func,
                        attempt,
                        max_attempts,
                        err,
                        exc_info=True,
                    )
                    raise
                LOGGER.debug(
                    "%s: %s error calling %s, retrying  (%s/%s)...: %s",
                    self.name,
                    type(err),
                    func,
                    attempt,
                    max_attempts,
                    err,
                    exc_info=True,
                )

    return cast(WrapFuncType, _async_wrap_retry_bluetooth_connection_error)


class BJLEDInstance:
    def __init__(self, address, reset: bool, delay: int, hass) -> None:
        self.loop = asyncio.get_running_loop()
        self._mac = address
        self._reset = reset
        self._delay = delay
        self._hass = hass
        self._device: BLEDevice | None = None
        self._device = bluetooth.async_ble_device_from_address(self._hass, address)
        if not self._device:
            raise ConfigEntryNotReady(
                f"You need to add bluetooth integration (https://www.home-assistant.io/integrations/bluetooth) or couldn't find a nearby device with address: {address}"
            )
        self._connect_lock: asyncio.Lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._cached_services: BleakGATTServiceCollection | None = None
        self._expected_disconnect = False
        self._is_on = None
        self._rgb_color = None
        self._brightness = 254
        self._effect = None
        self._effect_speed = 0x1
        self._color_mode = ColorMode.RGB
        self._turn_on_cmd = None
        self._turn_off_cmd = None
        self._compiler: StateCompiler | None = None
        self._state: LightState = self._initial_state()
        self._model = self._detect_model(self._device)
        self._transmitter: Transmitter | None = None
        
        LOGGER.debug(
            "Model information for device %s : ModelNo %s. MAC: %s",
            self._device.name,
            self._model,
            self._mac,
        )

    @staticmethod
    def _initial_state():
        state = LightState()
        state.update(SwitchCommand(on=True))
        state.update(BrightnessCommand(0xfe))
        state.update(RGBCommand(0x5f, 0x0f, 0x40))

        return state

    def _detect_model(self, device: BLEDevice):
        profile = device_profile_from_ble_device(device)
        if profile is None:
            LOGGER.debug(
                "Bluetooth device has an unrecognized name: %s. MAC: %s",
                device.name,
                device.address
            )

        self._compiler = profile.compiler()
        
        if RGBCommand in profile.supported_commands:
            self._color_mode = ColorMode.RGB
        elif BrightnessCommand in profile.supported_commands:
            self._color_mode = ColorMode.BRIGHTNESS
        else:
            self._color_mode = ColorMode.ONOFF
        
        return profile
            
    async def _write_state(self):
        """Sends commands to the device so its configuration matches 
        the desired state, self._state"""
        platform_commands = self._compiler.compile(self._state)
        LOGGER.debug(f"Sending commands to {self.name}: {platform_commands}")
        await self._ensure_connected()
        await self._transmitter.send_all(platform_commands)

    @property
    def mac(self):
        return self._device.address

    @property
    def reset(self):
        return self._reset

    @property
    def name(self):
        return self._device.name

    @property
    def rssi(self):
        return self._device.rssi

    @property
    def is_on(self):
        return self._is_on

    @property
    def brightness(self):
        return self._brightness 

    @property
    def rgb_color(self):
        return self._rgb_color

    @property
    def effect_list(self) -> list[str]:
        return EFFECT_LIST

    @property
    def effect(self):
        return self._effect
    
    @property
    def color_mode(self):
        return self._color_mode

    @retry_bluetooth_connection_error
    async def set_rgb_color(self, rgb: Tuple[int, int, int], brightness: int | None = None):
        self._rgb_color = rgb
        if brightness is None:
            if self._brightness is None:
                self._brightness = 254
            else:
                brightness = self._brightness
        # RGB packet
        self._state.update(RGBCommand(*rgb))
        self._state.update(BrightnessCommand(brightness))
        await self._write_state()


    async def set_brightness_local(self, brightness: int):
        # 0 - 254, should convert automatically with the hex calls
        # call color temp or rgb functions to update
        self._brightness = brightness
        self._state.update(BrightnessCommand(brightness))
        await self._write_state()
        # await self.set_rgb_color(self._rgb_color, value)

    @retry_bluetooth_connection_error
    async def turn_on(self):
        self._state.update(SwitchCommand(on=True))
        await self._write_state()
        self._is_on = True
                
    @retry_bluetooth_connection_error
    async def turn_off(self):
        self._state.update(SwitchCommand(on=False))
        await self._write_state()
        self._is_on = False

    @retry_bluetooth_connection_error
    async def set_effect(self, effect_name: str):
        if effect_name not in EFFECT_LIST:
            LOGGER.error("Effect %s not supported", effect_name)
            return
        self._effect = effect_name
        effect = Effect(effect_name)

        LOGGER.debug('Effect: %s', effect)
        
        self._state.update(EffectCommand(effect))
        await self._write_state()

    # @retry_bluetooth_connection_error
    # async def turn_on(self):
    #     await self._write(self._turn_on_cmd)
    #     self._is_on = True

    # @retry_bluetooth_connection_error
    # async def turn_off(self):
    #     await self._write(self._turn_off_cmd)
    #     self._is_on = False

    @retry_bluetooth_connection_error
    async def update(self):
        LOGGER.debug("%s: Update in bjled called", self.name)
        # I dont think we have anything to update

    async def _ensure_connected(self) -> None:
        """Ensure connection to device is established."""
        if self._connect_lock.locked():
            LOGGER.debug(
                "%s: Connection already in progress, waiting for it to complete",
                self.name,
            )
        if self._client and self._client.is_connected:
            self._reset_disconnect_timer()
            return
        async with self._connect_lock:
            # Check again while holding the lock
            if self._client and self._client.is_connected:
                self._reset_disconnect_timer()
                return
            LOGGER.debug("%s: Connecting", self.name)
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._device,
                self.name,
                self._disconnected,
                cached_services=self._cached_services,
                ble_device_callback=lambda: self._device,
            )
            LOGGER.debug("%s: Connected", self.name)

            self._cached_services = None
            self._transmitter = None
            try:
                transmitter = self._model.get_transmitter(client)

            except ConnectionError:
                LOGGER.debug("Connection failed: failed to wrap client with transmitter", exc_info=True)

                # Try to handle services failing to load
                try:
                    services = await client.get_services()
                    LOGGER.debug(f"Tried reloading characteristrics: {services.characteristics}")
                    transmitter = self._model.get_transmitter(self._client)

                except ConnectionError:
                    LOGGER.debug("Connection failed (x2): failed to wrap client with transmitter", exc_info=True)
            self._cached_services = client.services

            self._client = client
            self._transmitter = transmitter
            self._reset_disconnect_timer()

    def _reset_disconnect_timer(self) -> None:
        """Reset disconnect timer."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._expected_disconnect = False
        if self._delay is not None and self._delay != 0:
            LOGGER.debug(
                "%s: Configured disconnect from device in %s seconds",
                self.name,
                self._delay
            )
            self._disconnect_timer = self.loop.call_later(self._delay, self._disconnect)

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Disconnected callback."""
        if self._expected_disconnect:
            LOGGER.debug("%s: Disconnected from device", self.name)
            return
        LOGGER.warning("%s: Device unexpectedly disconnected", self.name)

    def _disconnect(self) -> None:
        """Disconnect from device."""
        self._disconnect_timer = None
        asyncio.create_task(self._execute_timed_disconnect())

    async def stop(self) -> None:
        """Stop the LEDBLE."""
        LOGGER.debug("%s: Stop", self.name)
        await self._execute_disconnect()

    async def _execute_timed_disconnect(self) -> None:
        """Execute timed disconnection."""
        LOGGER.debug(
            "%s: Disconnecting after timeout of %s",
            self.name,
            self._delay
        )
        await self._execute_disconnect()

    async def _execute_disconnect(self) -> None:
        """Execute disconnection."""
        async with self._connect_lock:
            client = self._client
            transmitter = self._transmitter
            self._expected_disconnect = True
            self._client = None
            self._transmitter = None
            if client and client.is_connected:
                # Calls client.disconnect internally
                await transmitter.close()
            LOGGER.debug("%s: Disconnected", self.name)
    