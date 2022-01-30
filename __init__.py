"""The Tesla Powerwall integration."""
from datetime import timedelta
import logging

import requests
from tesla_powerwall import (
    AccessDeniedError,
    APIError,
    MissingAttributeError,
    Powerwall,
    PowerwallUnreachableError,
)

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_IP_ADDRESS, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    POWERWALL_API_CHANGED,
    POWERWALL_API_CHARGE,
    POWERWALL_API_DEVICE_TYPE,
    POWERWALL_API_GRID_SERVICES_ACTIVE,
    POWERWALL_API_GRID_STATUS,
    POWERWALL_API_METERS,
    POWERWALL_API_SERIAL_NUMBERS,
    POWERWALL_API_SITE_INFO,
    POWERWALL_API_SITEMASTER,
    POWERWALL_API_STATUS,
    POWERWALL_COORDINATOR,
    POWERWALL_HTTP_SESSION,
    POWERWALL_OBJECT,
    UPDATE_INTERVAL,
)

CONFIG_SCHEMA = cv.removed(DOMAIN, raise_if_present=False)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)

MAX_LOGIN_FAILURES = 5


async def _migrate_old_unique_ids(hass, entry_id, powerwall_data):
    serial_numbers = powerwall_data[POWERWALL_API_SERIAL_NUMBERS]
    site_info = powerwall_data[POWERWALL_API_SITE_INFO]

    @callback
    def _async_migrator(entity_entry: entity_registry.RegistryEntry):
        parts = entity_entry.unique_id.split("_")
        # Check if the unique_id starts with the serial_numbers of the powerwalls
        if parts[0 : len(serial_numbers)] != serial_numbers:
            # The old unique_id ended with the nomianal_system_engery_kWh so we can use that
            # to find the old base unique_id and extract the device_suffix.
            normalized_energy_index = (
                len(parts) - 1 - parts[::-1].index(str(site_info.nominal_system_energy))
            )
            device_suffix = parts[normalized_energy_index + 1 :]

            new_unique_id = "_".join([*serial_numbers, *device_suffix])
            _LOGGER.info(
                "Migrating unique_id from [%s] to [%s]",
                entity_entry.unique_id,
                new_unique_id,
            )
            return {"new_unique_id": new_unique_id}
        return None

    await entity_registry.async_migrate_entries(hass, entry_id, _async_migrator)


async def _async_handle_api_changed_error(
    hass: HomeAssistant, error: MissingAttributeError
):
    # The error might include some important information about what exactly changed.
    _LOGGER.error(str(error))
    persistent_notification.async_create(
        hass,
        "It seems like your powerwall uses an unsupported version. "
        "Please update the software of your powerwall or if it is "
        "already the newest consider reporting this issue.\nSee logs for more information",
        title="Unknown powerwall software version",
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tesla Powerwall from a config entry."""

    entry_id = entry.entry_id

    hass.data.setdefault(DOMAIN, {})
    http_session = requests.Session()
    ip_address = entry.data[CONF_IP_ADDRESS]

    password = entry.data.get(CONF_PASSWORD)
    power_wall = Powerwall(ip_address, http_session=http_session)
    try:
        powerwall_data = await hass.async_add_executor_job(
            _login_and_fetch_base_info, power_wall, password
        )
    except PowerwallUnreachableError as err:
        http_session.close()
        raise ConfigEntryNotReady from err
    except MissingAttributeError as err:
        http_session.close()
        await _async_handle_api_changed_error(hass, err)
        return False
    except AccessDeniedError as err:
        _LOGGER.debug("Authentication failed", exc_info=err)
        http_session.close()
        raise ConfigEntryAuthFailed from err

    await _migrate_old_unique_ids(hass, entry_id, powerwall_data)
    login_failed_count = 0

    runtime_data = hass.data[DOMAIN][entry.entry_id] = {
        POWERWALL_API_CHANGED: False,
        POWERWALL_HTTP_SESSION: http_session,
    }

    def _recreate_powerwall_login():
        nonlocal http_session
        nonlocal power_wall
        http_session.close()
        http_session = requests.Session()
        power_wall = Powerwall(ip_address, http_session=http_session)
        runtime_data[POWERWALL_OBJECT] = power_wall
        runtime_data[POWERWALL_HTTP_SESSION] = http_session
        power_wall.login(password)

    async def _async_login_and_retry_update_data():
        """Retry the update after a failed login."""
        nonlocal login_failed_count
        # If the session expired, recreate, relogin, and try again
        _LOGGER.debug("Retrying login and updating data")
        try:
            await hass.async_add_executor_job(_recreate_powerwall_login)
            data = await _async_update_powerwall_data(hass, entry, power_wall)
        except AccessDeniedError as err:
            login_failed_count += 1
            if login_failed_count == MAX_LOGIN_FAILURES:
                raise ConfigEntryAuthFailed from err
            raise UpdateFailed(
                f"Login attempt {login_failed_count}/{MAX_LOGIN_FAILURES} failed, will retry: {err}"
            ) from err
        except APIError as err:
            raise UpdateFailed(f"Updated failed due to {err}, will retry") from err
        else:
            login_failed_count = 0
            return data

    async def async_update_data():
        """Fetch data from API endpoint."""
        # Check if we had an error before
        nonlocal login_failed_count
        _LOGGER.debug("Checking if update failed")
        if runtime_data[POWERWALL_API_CHANGED]:
            return runtime_data[POWERWALL_COORDINATOR].data

        _LOGGER.debug("Updating data")
        try:
            data = await _async_update_powerwall_data(hass, entry, power_wall)
        except AccessDeniedError as err:
            if password is None:
                raise ConfigEntryAuthFailed from err
            return await _async_login_and_retry_update_data()
        except APIError as err:
            raise UpdateFailed(f"Updated failed due to {err}, will retry") from err
        else:
            login_failed_count = 0
            return data

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="Powerwall site",
        update_method=async_update_data,
        update_interval=timedelta(seconds=UPDATE_INTERVAL),
    )

    runtime_data.update(
        {
            **powerwall_data,
            POWERWALL_OBJECT: power_wall,
            POWERWALL_COORDINATOR: coordinator,
        }
    )

    await coordinator.async_config_entry_first_refresh()

    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    return True


async def _async_update_powerwall_data(
    hass: HomeAssistant, entry: ConfigEntry, power_wall: Powerwall
):
    """Fetch updated powerwall data."""
    try:
        return await hass.async_add_executor_job(_fetch_powerwall_data, power_wall)
    except PowerwallUnreachableError as err:
        raise UpdateFailed("Unable to fetch data from powerwall") from err
    except MissingAttributeError as err:
        await _async_handle_api_changed_error(hass, err)
        hass.data[DOMAIN][entry.entry_id][POWERWALL_API_CHANGED] = True
        # Returns the cached data. This data can also be None
        return hass.data[DOMAIN][entry.entry_id][POWERWALL_COORDINATOR].data


def _login_and_fetch_base_info(power_wall: Powerwall, password: str):
    """Login to the powerwall and fetch the base info."""
    if password is not None:
        power_wall.login(password)
    power_wall.detect_and_pin_version()
    return call_base_info(power_wall)


def call_base_info(power_wall):
    """Wrap powerwall properties to be a callable."""
    serial_numbers = power_wall.get_serial_numbers()
    # Make sure the serial numbers always have the same order
    serial_numbers.sort()
    return {
        POWERWALL_API_SITE_INFO: power_wall.get_site_info(),
        POWERWALL_API_STATUS: power_wall.get_status(),
        POWERWALL_API_DEVICE_TYPE: power_wall.get_device_type(),
        POWERWALL_API_SERIAL_NUMBERS: serial_numbers,
    }


def _fetch_powerwall_data(power_wall):
    """Process and update powerwall data."""
    return {
        POWERWALL_API_CHARGE: power_wall.get_charge(),
        POWERWALL_API_SITEMASTER: power_wall.get_sitemaster(),
        POWERWALL_API_METERS: power_wall.get_meters(),
        POWERWALL_API_GRID_SERVICES_ACTIVE: power_wall.is_grid_services_active(),
        POWERWALL_API_GRID_STATUS: power_wall.get_grid_status(),
    }


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    hass.data[DOMAIN][entry.entry_id][POWERWALL_HTTP_SESSION].close()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
