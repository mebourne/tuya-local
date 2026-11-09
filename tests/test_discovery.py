"""Tests for the active Tuya LAN rediscovery sweeper."""

import asyncio
import logging
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.tuya_local import discovery
from custom_components.tuya_local.const import (
    CONF_DEVICE_CID,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_POLL_ONLY,
    CONF_PROTOCOL_VERSION,
    CONF_TYPE,
    DATA_DISCOVERY,
    DOMAIN,
)
from custom_components.tuya_local.discovery import (
    TuyaLANRediscovery,
    async_start_discovery,
    async_stop_discovery,
)

TESTKEY = ")<jO<@)'P1|kR$Kd"
DEVID = "bf1234567890abcdef"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def device_status(hass, mocker):
    """Keep real protocol handling, but do not contact devices on the test LAN."""
    hass.data.setdefault(DOMAIN, {})
    return mocker.patch(
        "tinytuya.Device.status", autospec=True, return_value={"dps": {"1": True}}
    )


def _make_entry(hass, host="192.168.1.10", options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=13,
        minor_version=20,
        title="thermostat",
        state=ConfigEntryState.LOADED,
        data={
            CONF_DEVICE_ID: DEVID,
            CONF_HOST: host,
            CONF_LOCAL_KEY: TESTKEY,
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: "auto",
            CONF_TYPE: "polytherm_polyalpha_thermostat",
        },
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


def _set_device(hass, returned_state, device_id=DEVID):
    """Register a fake device object in hass.data under the device id."""
    device = type(
        "Dev", (), {"has_returned_state": returned_state, "async_stop": AsyncMock()}
    )()
    hass.data.setdefault(DOMAIN, {})[device_id] = {"device": device}
    return device


@pytest.mark.asyncio
async def test_sweep_updates_unreachable_changed_host(hass, caplog, mocker):
    """An unreachable device gets relocated, its host updated, and it's logged at WARNING."""
    entry = _make_entry(hass, host="192.168.1.10")
    _set_device(hass, returned_state=False)
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"ip": "192.168.1.55", "id": DEVID},
    )

    with caplog.at_level(
        logging.WARNING, logger="custom_components.tuya_local.discovery"
    ):
        await TuyaLANRediscovery(hass)._async_sweep()
        await hass.async_block_till_done()

    assert entry.data[CONF_HOST] == "192.168.1.55"
    # The IP change must be visible even when the entry runs at WARNING.
    assert "192.168.1.55" in caplog.text
    assert "192.168.1.10" in caplog.text


@pytest.mark.asyncio
async def test_sweep_skips_reachable_device(hass, mocker):
    """A device that is returning state is never scanned."""
    entry = _make_entry(hass, host="192.168.1.10")
    _set_device(hass, returned_state=True)
    find = mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"ip": "192.168.1.55"},
    )

    await TuyaLANRediscovery(hass)._async_sweep()
    await hass.async_block_till_done()

    find.assert_not_called()
    assert entry.data[CONF_HOST] == "192.168.1.10"


@pytest.mark.asyncio
async def test_sweep_no_change_when_ip_same(hass, mocker):
    """If the scan returns the current IP, no entry update happens."""
    entry = _make_entry(hass, host="192.168.1.10")
    _set_device(hass, returned_state=False)
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"ip": "192.168.1.10", "id": DEVID},
    )
    update = mocker.spy(hass.config_entries, "async_update_entry")

    await TuyaLANRediscovery(hass)._async_sweep()
    await hass.async_block_till_done()

    update.assert_not_called()
    assert entry.data[CONF_HOST] == "192.168.1.10"


@pytest.mark.asyncio
async def test_sweep_handles_not_found(hass, mocker):
    """A scan that finds nothing must not raise or change anything."""
    entry = _make_entry(hass, host="192.168.1.10")
    _set_device(hass, returned_state=False)
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"ip": None},
    )
    update = mocker.spy(hass.config_entries, "async_update_entry")

    await TuyaLANRediscovery(hass)._async_sweep()
    await hass.async_block_till_done()

    update.assert_not_called()
    assert entry.data[CONF_HOST] == "192.168.1.10"


@pytest.mark.asyncio
async def test_sweep_updates_host_stored_in_options(hass, mocker):
    """When the effective host lives in options, the update targets options."""
    entry = _make_entry(hass, host="10.0.0.1", options={CONF_HOST: "192.168.1.103"})
    _set_device(hass, returned_state=False)
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"ip": "192.168.1.55", "id": DEVID},
    )

    await TuyaLANRediscovery(hass)._async_sweep()
    await hass.async_block_till_done()

    assert entry.options[CONF_HOST] == "192.168.1.55"
    assert entry.data[CONF_HOST] == "192.168.1.55"


@pytest.mark.asyncio
async def test_sweep_scans_when_no_device_object(hass, mocker):
    """An entry with no device object yet (failed setup) is still scanned."""
    entry = _make_entry(hass, host="192.168.1.10")
    hass.data.setdefault(DOMAIN, {})  # no device bucket registered
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"ip": "192.168.1.77", "id": DEVID},
    )

    await TuyaLANRediscovery(hass)._async_sweep()
    await hass.async_block_till_done()

    assert entry.data[CONF_HOST] == "192.168.1.77"


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_cancels(hass, mocker):
    """async_start_discovery schedules the sweep + scan intervals; stop cancels both."""
    unsub_sweep = mocker.MagicMock()
    unsub_scan = mocker.MagicMock()
    track = mocker.patch(
        "custom_components.tuya_local.discovery.async_track_time_interval",
        side_effect=[unsub_sweep, unsub_scan],
    )

    await async_start_discovery(hass)
    rediscovery = hass.data[DOMAIN][DATA_DISCOVERY]
    assert isinstance(rediscovery, TuyaLANRediscovery)
    assert track.call_count == 2

    # Second call must not schedule more intervals (singleton).
    await async_start_discovery(hass)
    assert track.call_count == 2

    async_stop_discovery(hass)
    unsub_sweep.assert_called_once()
    unsub_scan.assert_called_once()
    assert DATA_DISCOVERY not in hass.data[DOMAIN]


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"devId": DEVID},
        {"dps": None},
        {"dps": {}},
        {"Error": "Network Error", "Err": "901"},
        {"Error": "Invalid key", "Err": "914"},
        {"Error": "Invalid JSON", "Err": "900"},
        {"devId": None, "dps": {"1": True}},
        {"devId": "another-device", "dps": {"1": True}},
        OSError("network unreachable"),
    ],
)
async def test_sweep_rejects_unverified_address(hass, mocker, device_status, response):
    """A matching broadcast cannot overwrite hosts without a valid device response."""
    entry = _make_entry(hass, options={CONF_HOST: "192.168.1.10"})
    original_data, original_options = dict(entry.data), dict(entry.options)
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"id": DEVID, "ip": "172.18.0.1"},
    )
    if isinstance(response, Exception):
        device_status.side_effect = response
    else:
        device_status.return_value = response

    await TuyaLANRediscovery(hass)._async_sweep()
    await hass.async_block_till_done()

    assert entry.data == original_data
    assert entry.options == original_options


@pytest.mark.parametrize("found_id", [None, "another-device"])
async def test_sweep_requires_matching_discovery_id(hass, mocker, found_id):
    """Even a responding address is ignored without the configured discovery ID."""
    entry = _make_entry(hass)
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"id": found_id, "ip": "192.168.1.55"},
    )

    await TuyaLANRediscovery(hass)._async_sweep()

    assert entry.data[CONF_HOST] == "192.168.1.10"


@pytest.mark.parametrize("host", ["192.168.1.10", "Auto", ""])
async def test_sweep_recovers_cold_entry(hass, mocker, host):
    """An offline entry needs no previous connection or known IP to recover and reload."""
    entry = _make_entry(hass, host=host)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"id": DEVID, "ip": "192.168.1.77"},
    )
    reload_entry = mocker.patch.object(
        hass.config_entries, "async_reload", return_value=True
    )

    await async_start_discovery(hass)
    # Run the periodic callback: no device has ever been cached or connected.
    await hass.data[DOMAIN][DATA_DISCOVERY]._async_sweep()
    await hass.async_block_till_done()
    async_stop_discovery(hass)

    assert entry.data[CONF_HOST] == "192.168.1.77"
    reload_entry.assert_awaited_once_with(entry.entry_id)


@pytest.fixture
def cold_entry(hass, mocker, device_status):
    """A never-connected plug responds only at its new address."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=13,
        minor_version=24,
        title="Cold plug",
        data={
            CONF_DEVICE_ID: DEVID,
            CONF_HOST: "192.168.1.10",
            CONF_LOCAL_KEY: TESTKEY,
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: 3.5,
            CONF_TYPE: "smartplugv2",
        },
    )
    entry.add_to_hass(hass)
    # Keep entity setup, but do not start the persistent socket receive loop.
    mocker.patch("custom_components.tuya_local.device.TuyaLocalDevice.start")

    def status(api):
        if api.address != "192.168.1.77":
            return {"Error": "Network Error", "Err": "901"}
        return {"dps": {"1": False, "9": 0, "18": 0, "19": 0, "20": 2400, "26": 0}}

    device_status.side_effect = status
    return entry


@pytest.mark.parametrize("host", ["192.168.1.10", "Auto", ""])
async def test_setup_immediately_discovers_and_retries(hass, mocker, cold_entry, host):
    """Setup recovers a stale or unknown host immediately, preserving the entry and settings."""
    entry = cold_entry
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: host})
    original_data = dict(entry.data)
    # The initial constructor may also try discovery for Auto/blank hosts.
    # Simulate that failing before the explicit setup-recovery scan succeeds.
    mocker.patch("tinytuya.core.XenonDevice.find_device", return_value={"ip": None})
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"id": DEVID, "ip": "192.168.1.77"},
    )

    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
        assert entry.data == {**original_data, CONF_HOST: "192.168.1.77"}
        assert hass.states.get("switch.cold_plug").state == "off"
    finally:
        async_stop_discovery(hass)


async def test_cold_start_recovers_after_failed_setup(
    hass, mocker, cold_entry, freezer
):
    """The periodic sweep starts despite all setups failing and reloads a working entry."""
    entry = cold_entry
    discovery_available = False

    def find_device(device_id):
        return {"id": device_id, "ip": "192.168.1.77" if discovery_available else None}

    mocker.patch(
        "custom_components.tuya_local.discovery._find_device", side_effect=find_device
    )

    try:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert DEVID not in hass.data[DOMAIN]

        now = dt_util.utcnow()
        # Advance chronologically so HA's shorter setup retries do not all
        # fire at the same instant as the first discovery sweep.
        for seconds in range(1, 62):
            if seconds == 60:
                discovery_available = True
            freezer.move_to(now + timedelta(seconds=seconds))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done(wait_background_tasks=True)

        assert entry.state is ConfigEntryState.LOADED
        assert entry.data[CONF_HOST] == "192.168.1.77"
        assert hass.states.get("switch.cold_plug").state == "off"
    finally:
        async_stop_discovery(hass)


@pytest.mark.parametrize(
    ("protocol", "wire_version"), [("auto", 3.5), (3.3, 3.3), (3.5, 3.5), (3.52, 3.5)]
)
@pytest.mark.parametrize("cid", [None, "child-id"])
async def test_sweep_preserves_settings_and_uses_effective_credentials(
    hass, mocker, device_status, protocol, wire_version, cid
):
    """Standalone and child devices use saved options without replacing identities or settings."""
    options = {
        CONF_HOST: "192.168.1.20",
        CONF_LOCAL_KEY: "abcdefghijklmnop",
        CONF_PROTOCOL_VERSION: protocol,
        CONF_POLL_ONLY: True,
    }
    entry = _make_entry(hass, options=options)
    if cid:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICE_CID: cid}
        )
    original_data = dict(entry.data)
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"id": DEVID, "ip": "192.168.1.55"},
    )

    def status(api):
        wifi_api = api.parent or api
        if (
            wifi_api.id == DEVID
            and wifi_api.address == "192.168.1.55"
            and wifi_api.real_local_key == b"abcdefghijklmnop"
            and wifi_api.version == wire_version
            and api.cid == cid
        ):
            return {"dps": {"1": False}}
        return {"Error": "Invalid key or protocol", "Err": "914"}

    device_status.side_effect = status

    await TuyaLANRediscovery(hass)._async_sweep()

    assert entry.data == {**original_data, CONF_HOST: "192.168.1.55"}
    assert entry.options == {**options, CONF_HOST: "192.168.1.55"}
    assert not hass.data[DOMAIN]  # The temporary probe is not registered.


@pytest.mark.parametrize("when", ["discovery", "verification"])
@pytest.mark.parametrize("change", ["recovered", "edited", "removed", "disabled"])
async def test_sweep_discards_stale_result(hass, mocker, device_status, when, change):
    """Discovery and verification cannot overwrite recovered, edited, removed or disabled entries."""
    entry = _make_entry(hass)
    device = _set_device(hass, returned_state=False)

    def change_entry():
        if change == "recovered":
            device.has_returned_state = True
        elif change == "edited":
            hass.config_entries.async_update_entry(
                entry, options={CONF_HOST: "192.168.1.99"}
            )
        elif change == "disabled":
            object.__setattr__(entry, "disabled_by", ConfigEntryDisabler.USER)
        else:
            hass.config_entries._entries.pop(entry.entry_id)

    def find_device(*args):
        if when == "discovery":
            hass.loop.call_soon_threadsafe(change_entry)
        return {"id": DEVID, "ip": "192.168.1.55"}

    def status(api):
        if when == "verification":
            hass.loop.call_soon_threadsafe(change_entry)
        return {"dps": {"1": True}}

    mocker.patch(
        "custom_components.tuya_local.discovery._find_device", side_effect=find_device
    )
    device_status.side_effect = status
    original_job = hass.async_add_executor_job

    async def executor_job(func, *args):
        result = await original_job(func, *args)
        # Let the competing lifecycle change run even if the mocked I/O
        # completes without suspending the sweep task.
        await asyncio.sleep(0)
        return result

    mocker.patch.object(hass, "async_add_executor_job", side_effect=executor_job)

    await TuyaLANRediscovery(hass)._async_sweep()

    assert entry.data[CONF_HOST] == "192.168.1.10"
    assert entry.options.get(CONF_HOST) == (
        "192.168.1.99" if change == "edited" else None
    )


def _fake_config(matches):
    """Minimal stand-in for a device config with a matches_product() method."""
    return type("Cfg", (), {"matches_product": lambda self, pid: matches})()


def _scan_result(gwid=DEVID, product="keyabc123", ip="192.168.1.10"):
    """A tinytuya.deviceScan-style result: keyed by IP, carrying gwId/productKey."""
    info = {"gwId": gwid, "ip": ip, "version": "3.5"}
    if product is not None:
        info["productKey"] = product
    return {ip: info}


def _patch_flow_init(hass, mocker):
    """Patch the config-entries flow init with an awaitable mock."""
    return mocker.patch.object(
        hass.config_entries.flow, "async_init", new_callable=AsyncMock
    )


@pytest.mark.asyncio
async def test_product_scan_warns_once_on_unmatched_product(hass, caplog, mocker):
    """An unmatched product id is logged at WARNING, once per device per run."""
    _make_entry(hass, host="192.168.1.10")
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(),
    )
    mocker.patch(
        "custom_components.tuya_local.discovery.get_config",
        return_value=_fake_config(False),
    )
    _patch_flow_init(hass, mocker)
    disc = TuyaLANRediscovery(hass)

    with caplog.at_level(
        logging.WARNING, logger="custom_components.tuya_local.discovery"
    ):
        await disc._async_discovery_scan()
        await hass.async_block_till_done()
        assert caplog.text.count("keyabc123") == 1
        # A second scan must not warn again for the same device.
        caplog.clear()
        await disc._async_discovery_scan()
        await hass.async_block_till_done()
        assert "keyabc123" not in caplog.text


@pytest.mark.asyncio
async def test_product_scan_silent_when_product_matches(hass, caplog, mocker):
    """No warning when the product id is listed in the config."""
    _make_entry(hass, host="192.168.1.10")
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(),
    )
    mocker.patch(
        "custom_components.tuya_local.discovery.get_config",
        return_value=_fake_config(True),
    )
    _patch_flow_init(hass, mocker)
    with caplog.at_level(
        logging.WARNING, logger="custom_components.tuya_local.discovery"
    ):
        await TuyaLANRediscovery(hass)._async_discovery_scan()
        await hass.async_block_till_done()
    assert "is not listed" not in caplog.text


@pytest.mark.asyncio
async def test_product_scan_skips_when_no_product_id(hass, mocker):
    """If the scan reports no product id, the config is not even looked up."""
    _make_entry(hass, host="192.168.1.10")
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(product=None),
    )
    get_config = mocker.patch(
        "custom_components.tuya_local.discovery.get_config",
    )
    _patch_flow_init(hass, mocker)
    await TuyaLANRediscovery(hass)._async_discovery_scan()
    await hass.async_block_till_done()
    get_config.assert_not_called()


@pytest.mark.asyncio
async def test_product_scan_handles_missing_config(hass, caplog, mocker):
    """A missing config file must not warn or raise."""
    _make_entry(hass, host="192.168.1.10")
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(),
    )
    mocker.patch(
        "custom_components.tuya_local.discovery.get_config",
        return_value=None,
    )
    _patch_flow_init(hass, mocker)
    with caplog.at_level(
        logging.WARNING, logger="custom_components.tuya_local.discovery"
    ):
        await TuyaLANRediscovery(hass)._async_discovery_scan()
        await hass.async_block_till_done()
    assert "keyabc123" not in caplog.text


@pytest.mark.asyncio
async def test_discovery_raises_flow_for_unknown_device(hass, mocker):
    """An unconfigured device on the LAN starts an integration_discovery flow."""
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(gwid="bfunknown000000000", ip="192.168.1.99"),
    )
    init = _patch_flow_init(hass, mocker)

    await TuyaLANRediscovery(hass)._async_discovery_scan()
    await hass.async_block_till_done()

    init.assert_awaited_once()
    args, kwargs = init.call_args
    assert args[0] == DOMAIN
    assert kwargs["context"]["source"] == "integration_discovery"
    assert kwargs["data"][CONF_DEVICE_ID] == "bfunknown000000000"
    assert kwargs["data"][CONF_HOST] == "192.168.1.99"


@pytest.mark.asyncio
async def test_discovery_skips_configured_device(hass, mocker):
    """A device already configured is not offered for discovery again."""
    _make_entry(hass, host="192.168.1.10")  # DEVID is configured
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(gwid=DEVID),
    )
    mocker.patch(
        "custom_components.tuya_local.discovery.get_config",
        return_value=_fake_config(True),
    )
    init = _patch_flow_init(hass, mocker)

    await TuyaLANRediscovery(hass)._async_discovery_scan()
    await hass.async_block_till_done()

    init.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_raises_flow_only_once_per_device(hass, mocker):
    """Repeated scans do not spawn duplicate flows for the same new device."""
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(gwid="bfunknown000000000", ip="192.168.1.99"),
    )
    init = _patch_flow_init(hass, mocker)
    disc = TuyaLANRediscovery(hass)

    await disc._async_discovery_scan()
    await hass.async_block_till_done()
    await disc._async_discovery_scan()
    await hass.async_block_till_done()

    assert init.await_count == 1


@pytest.mark.asyncio
async def test_discovery_scan_handles_empty_result(hass, mocker):
    """An empty scan (e.g. socket error) does nothing and does not raise."""
    _make_entry(hass, host="192.168.1.10")
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value={},
    )
    init = _patch_flow_init(hass, mocker)
    await TuyaLANRediscovery(hass)._async_discovery_scan()
    await hass.async_block_till_done()
    init.assert_not_awaited()


def test_module_exposes_expected_intervals():
    """Guard the cadences against accidental change."""
    assert discovery.SWEEP_INTERVAL.total_seconds() == 60
    assert discovery.SCAN_INTERVAL.total_seconds() == 600
