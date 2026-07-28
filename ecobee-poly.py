#!/usr/bin/env python3
# MIT License — Copyright (c) 2026 csteenwyk
"""
Ecobee (Direct) Polyglot v3 NodeServer

Talks to ecobee.com using the consumer login flow (your own username/password),
the same path Home Assistant uses via python-ecobee-api. Independent of UDI's
shared developer API key, which Ecobee has disabled.

Custom params:
    username   — ecobee.com email address
    password   — ecobee.com password
    mfa_code   — only if the account has two-factor enabled. Set this when the
                 plugin raises the "two-factor code required" notice; it is
                 consumed once to finish the login and then ignored.
    hold_type  — "nextTransition" (default) or "indefinite". Controls whether
                 setpoint and climate changes hold until the next scheduled
                 climate change or stay in effect until manually released.

Requires python-ecobee-api >= 0.4.1, which logs in with the OAuth2
authorization-code + PKCE flow and returns a real refresh_token. The token is
cached at <PG3 data dir>/.ecobee_state.json, so the username/password are used
only for the initial login, after a password change, or if the refresh token is
revoked. Earlier releases (<= 0.3.2) re-submitted the credentials on every
token refresh and scraped the response HTML for the token, which broke with an
IndexError as soon as ecobee returned any interstitial page.

Features:
    - Thermostat: temp / setpoints / mode / fan / humidity / HVAC state
    - Remote sensors: per-sensor temp / humidity / occupancy nodes
    - Climate commands: Home / Away / Sleep + dedicated "Away Indefinite"
    - Commands honor the global hold_type custom param
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

import udi_interface
from udi_interface import Custom

LOGGER = udi_interface.LOGGER

_IMPORT_ERR = None
try:
    # EcobeeAuth*Error and MfaChallenge are 0.4.0+. Importing them here means a
    # stale 0.3.x left behind by pip (which will not upgrade an already-satisfied
    # `>=` pin without --upgrade) surfaces as a clear notice instead of an
    # AttributeError deep in the auth path.
    from pyecobee import (
        Ecobee,
        ECOBEE_USERNAME,
        ECOBEE_PASSWORD,
        ECOBEE_REFRESH_TOKEN,
    )
    from pyecobee.errors import (
        EcobeeAuthFailedError,
        EcobeeAuthMfaRequiredError,
        EcobeeAuthUnknownError,
        InvalidTokenError,
    )
except ImportError as _e:
    _IMPORT_ERR = str(_e)
    Ecobee = None
    ECOBEE_USERNAME = 'EMAIL'
    ECOBEE_PASSWORD = 'PASSWORD'
    ECOBEE_REFRESH_TOKEN = 'REFRESH_TOKEN'

    # Placeholders so the except clauses below still resolve when the real
    # library is missing. Nothing raises them, so they can never match.
    class _MissingPyecobee(Exception):
        pass

    EcobeeAuthFailedError = type('EcobeeAuthFailedError', (_MissingPyecobee,), {})
    EcobeeAuthMfaRequiredError = type('EcobeeAuthMfaRequiredError', (_MissingPyecobee,), {})
    EcobeeAuthUnknownError = type('EcobeeAuthUnknownError', (_MissingPyecobee,), {})
    InvalidTokenError = type('InvalidTokenError', (_MissingPyecobee,), {})

_STATE_FILE = Path(os.environ.get('PG3_PROFILE', '.')) / '.ecobee_state.json'

# Auth retry policy. Every failed login is a credential submission against
# ecobee's Auth0 tenant, and Auth0 throttles both per-account and per-IP — so a
# failing login must never retry at the shortPoll rate.
_AUTH_RETRY_BASE = 60      # seconds, doubling per consecutive failure
_AUTH_RETRY_MAX = 3600     # cap at one hour


# --- Mapping helpers -------------------------------------------------------

_HVAC_MODE_TO_IDX = {'off': 0, 'heat': 1, 'cool': 2, 'auto': 3, 'auxHeatOnly': 4}
_IDX_TO_HVAC_MODE = {v: k for k, v in _HVAC_MODE_TO_IDX.items()}

_FAN_MODE_TO_IDX = {'auto': 0, 'on': 1}
_IDX_TO_FAN_MODE = {v: k for k, v in _FAN_MODE_TO_IDX.items()}

# Climate program refs Ecobee always exposes by default
_IDX_TO_CLIMATE = {0: 'home', 1: 'away', 2: 'sleep'}
_CLIMATE_TO_IDX = {v: k for k, v in _IDX_TO_CLIMATE.items()}

_HOLD_TYPES = {'nextTransition', 'indefinite'}

# CLISMD: active hold/event type on the thermostat
_HOLD_TYPE_TO_IDX = {
    'none': 0,
    'nextTransition': 1,
    'indefinite': 2,
    'dateTime': 3,
    'holdHours': 4,
    'vacation': 5,
    'demandResponse': 6,
    'quickSave': 7,
}

# Ecobee weather wind direction → ISY index (must match WINDDIR-* NLS subset).
_WIND_DIR_TO_IDX = {
    '0': 0, 'N': 1, 'NNE': 2, 'NE': 3, 'ENE': 4, 'E': 5,
    'ESE': 6, 'SE': 7, 'SSE': 8, 'S': 9, 'SSW': 10, 'SW': 11,
    'WSW': 12, 'W': 13, 'WNW': 14, 'NW': 15, 'NNW': 16,
}


def _hvac_state_index(equipment_status: str) -> int:
    """Derive HCSTATE (idle/heating/cooling/fan only) from equipmentStatus
    comma-separated list."""
    if not equipment_status:
        return 0
    parts = {p.strip() for p in equipment_status.split(',') if p.strip()}
    if parts & {'heatPump', 'heatPump2', 'heatPump3',
                'auxHeat1', 'auxHeat2', 'auxHeat3'}:
        return 1
    if parts & {'compCool1', 'compCool2'}:
        return 2
    if 'fan' in parts:
        return 3
    return 0


def _fan_state_index(equipment_status: str) -> int:
    """CLIFRS: actual fan running state. UOM 80: 0=off, 1=on."""
    if not equipment_status:
        return 0
    parts = {p.strip() for p in equipment_status.split(',') if p.strip()}
    if parts & {'fan', 'heatPump', 'heatPump2', 'heatPump3',
                'auxHeat1', 'auxHeat2', 'auxHeat3',
                'compCool1', 'compCool2', 'ventilator'}:
        return 1
    return 0


def _hold_type_index(events: list) -> int:
    """CLISMD: derive currently-running hold/event type from events[]."""
    if not events:
        return 0
    for ev in events:
        if ev.get('running'):
            t = ev.get('type', '')
            if t == 'hold':
                # Ecobee marks indefinite holds with the sentinel
                # endDate=2035-01-01, endTime=00:00:00. The midnight endTime
                # is the tell — matches old udi-poly-ecobee's heuristic.
                if ev.get('endTime') == '00:00:00':
                    return _HOLD_TYPE_TO_IDX['indefinite']
                return _HOLD_TYPE_TO_IDX['nextTransition']
            if t == 'vacation':
                return _HOLD_TYPE_TO_IDX['vacation']
            if t == 'quickSave':
                return _HOLD_TYPE_TO_IDX['quickSave']
            if t == 'demandResponse':
                return _HOLD_TYPE_TO_IDX['demandResponse']
            if t in _HOLD_TYPE_TO_IDX:
                return _HOLD_TYPE_TO_IDX[t]
    return _HOLD_TYPE_TO_IDX['none']


def _temp_in(value):
    """Ecobee returns temperatures in tenths of °F. Convert to whole °F."""
    if value is None:
        return None
    try:
        return round(value / 10.0, 1)
    except (TypeError, ValueError):
        return None


def _temp_out(value):
    """Convert °F to ecobee's tenths-of-°F integer."""
    return int(round(float(value) * 10))


# --- Thermostat Node -------------------------------------------------------

class ThermostatNode(udi_interface.Node):
    """One node per ecobee thermostat."""

    id = 'ecobee_thermostat'

    drivers = [
        {'driver': 'ST',     'value': 0, 'uom': 17},  # Current temp (°F)
        {'driver': 'CLISPH', 'value': 0, 'uom': 17},  # Heat setpoint
        {'driver': 'CLISPC', 'value': 0, 'uom': 17},  # Cool setpoint
        {'driver': 'CLIMD',  'value': 0, 'uom': 25},  # HVAC mode
        {'driver': 'CLIFS',  'value': 0, 'uom': 25},  # Fan mode
        {'driver': 'CLIHUM', 'value': 0, 'uom': 22},  # Humidity %
        {'driver': 'CLIHCS', 'value': 0, 'uom': 25},  # HVAC current state
        {'driver': 'CLIFRS', 'value': 0, 'uom': 80},  # Fan actually running
        {'driver': 'CLISMD', 'value': 0, 'uom': 25},  # Active hold type
        {'driver': 'GV0',    'value': 0, 'uom': 2},   # Connected
    ]

    def __init__(self, polyglot, primary, address, name, tstat_id, ctrl):
        super().__init__(polyglot, primary, address, name)
        self._tstat_id = tstat_id  # ecobee identifier
        self._ctrl = ctrl
        self._cache = {}
        self._climate_ref = None  # currently active climate program ref

    @property
    def ecobee(self) -> Ecobee:
        return self._ctrl.ecobee

    def _index_in_list(self) -> int:
        """Find this thermostat's position in ecobee.thermostats (pyecobee
        methods take an index, not an identifier)."""
        for i, t in enumerate(self.ecobee.thermostats):
            if t.get('identifier') == self._tstat_id:
                return i
        return -1

    def _set(self, driver, value):
        if value is None:
            return
        if self._cache.get(driver) != value:
            self._cache[driver] = value
            self.setDriver(driver, value)

    def apply_state(self, tstat: dict):
        """Read driver values from a thermostat dict returned by pyecobee."""
        runtime = tstat.get('runtime', {}) or {}
        settings = tstat.get('settings', {}) or {}
        equipment = tstat.get('equipmentStatus', '') or ''
        program = tstat.get('program', {}) or {}

        cur_temp = _temp_in(runtime.get('actualTemperature'))
        if cur_temp is not None:
            self._set('ST', cur_temp)

        heat_sp = _temp_in(runtime.get('desiredHeat'))
        if heat_sp is not None:
            self._set('CLISPH', heat_sp)

        cool_sp = _temp_in(runtime.get('desiredCool'))
        if cool_sp is not None:
            self._set('CLISPC', cool_sp)

        mode = settings.get('hvacMode')
        if mode in _HVAC_MODE_TO_IDX:
            self._set('CLIMD', _HVAC_MODE_TO_IDX[mode])

        fan = settings.get('fanMode') or runtime.get('desiredFanMode')
        if fan in _FAN_MODE_TO_IDX:
            self._set('CLIFS', _FAN_MODE_TO_IDX[fan])

        hum = runtime.get('actualHumidity')
        if hum is not None:
            self._set('CLIHUM', int(hum))

        self._set('CLIHCS', _hvac_state_index(equipment))
        self._set('CLIFRS', _fan_state_index(equipment))
        self._set('CLISMD', _hold_type_index(tstat.get('events') or []))
        self._set('GV0', 1)

        # Track active climate program for our own state; not a driver yet.
        self._climate_ref = program.get('currentClimateRef')

    def mark_offline(self):
        self._set('GV0', 0)

    # --- Commands ---

    def _call_with_log(self, op: str, fn) -> bool:
        idx = self._index_in_list()
        if idx < 0:
            LOGGER.warning(f'{self.name}: thermostat {self._tstat_id} not in cache')
            return False
        try:
            ok = fn(idx)
            if not ok:
                LOGGER.warning(f'{self.name}: {op} returned False')
            else:
                LOGGER.info(f'{self.name}: {op} OK')
            return bool(ok)
        except Exception as e:
            LOGGER.error(f'{self.name}: {op} failed: {e}')
            return False

    def _hold_type(self) -> str:
        return self._ctrl.hold_type

    def cmd_set_heat(self, command):
        target_f = float(command.get('value'))
        # Use a hold for the requested heat setpoint; keep current cool
        # setpoint to avoid clobbering it.
        idx = self._index_in_list()
        cur_cool = _temp_in(self.ecobee.thermostats[idx]['runtime']['desiredCool']) if idx >= 0 else target_f + 4
        ht = self._hold_type()
        self._call_with_log(
            f'set_hold heat={target_f} hold={ht}',
            lambda i: self.ecobee.set_hold_temp(i, cur_cool, target_f, hold_type=ht))
        self._set('CLISPH', target_f)

    def cmd_set_cool(self, command):
        target_f = float(command.get('value'))
        idx = self._index_in_list()
        cur_heat = _temp_in(self.ecobee.thermostats[idx]['runtime']['desiredHeat']) if idx >= 0 else target_f - 4
        ht = self._hold_type()
        self._call_with_log(
            f'set_hold cool={target_f} hold={ht}',
            lambda i: self.ecobee.set_hold_temp(i, target_f, cur_heat, hold_type=ht))
        self._set('CLISPC', target_f)

    def cmd_set_mode(self, command):
        idx_val = int(command.get('value', 0))
        mode = _IDX_TO_HVAC_MODE.get(idx_val)
        if mode is None:
            LOGGER.warning(f'{self.name}: unknown mode index {idx_val}')
            return
        self._call_with_log(
            f'set_hvac_mode {mode}',
            lambda i: self.ecobee.set_hvac_mode(i, mode))
        self._set('CLIMD', idx_val)

    def cmd_set_fan(self, command):
        idx_val = int(command.get('value', 0))
        fan = _IDX_TO_FAN_MODE.get(idx_val)
        if fan is None:
            LOGGER.warning(f'{self.name}: unknown fan index {idx_val}')
            return
        self._call_with_log(
            f'set_fan_mode {fan}',
            lambda i: self.ecobee.set_fan_mode(i, fan))
        self._set('CLIFS', idx_val)

    def cmd_set_climate(self, command):
        """Set the active climate program (home/away/sleep) using the
        controller-configured hold_type."""
        idx_val = int(command.get('value', 0))
        climate = _IDX_TO_CLIMATE.get(idx_val)
        if climate is None:
            LOGGER.warning(f'{self.name}: unknown climate index {idx_val}')
            return
        ht = self._hold_type()
        self._call_with_log(
            f'set_climate_hold {climate} hold={ht}',
            lambda i: self.ecobee.set_climate_hold(i, climate, hold_type=ht))

    def cmd_hold_away(self, command=None):
        """Convenience: set climate to 'away' with indefinite hold — meant for
        'leaving the house' automations. Ignores the configured hold_type
        because that's the whole point of this command."""
        self._call_with_log(
            'set_climate_hold away hold=indefinite',
            lambda i: self.ecobee.set_climate_hold(i, 'away', hold_type='indefinite'))

    def cmd_resume(self, command=None):
        self._call_with_log(
            'resume_program',
            lambda i: self.ecobee.resume_program(i))

    def query(self, command=None):
        # Force a controller poll, then re-report
        self._ctrl.poll_now()
        self.reportDrivers()

    commands = {
        'SET_HEAT':    cmd_set_heat,
        'SET_COOL':    cmd_set_cool,
        'SET_MODE':    cmd_set_mode,
        'SET_FAN':     cmd_set_fan,
        'SET_CLIMATE': cmd_set_climate,
        'HOLD_AWAY':   cmd_hold_away,
        'RESUME':      cmd_resume,
        'QUERY':       query,
    }


# --- Remote Sensor Node ----------------------------------------------------

class SensorNode(udi_interface.Node):
    """One node per ecobee remote sensor."""

    id = 'ecobee_sensor'

    drivers = [
        {'driver': 'ST',     'value': 0, 'uom': 17},  # Temperature
        {'driver': 'CLIHUM', 'value': 0, 'uom': 22},  # Humidity %
        {'driver': 'GV0',    'value': 0, 'uom': 25},  # Occupancy
    ]

    def __init__(self, polyglot, primary, address, name, sensor_id):
        super().__init__(polyglot, primary, address, name)
        self._sensor_id = sensor_id
        self._cache = {}

    def _set(self, driver, value):
        if value is None:
            return
        if self._cache.get(driver) != value:
            self._cache[driver] = value
            self.setDriver(driver, value)

    def apply_state(self, sensor: dict):
        """Read drivers from an ecobee remote sensor dict.

        Each sensor has a `capability` list with entries of:
            {id, type: 'temperature'|'humidity'|'occupancy', value: str}
        Temperature is in tenths of °F as a string ('722' = 72.2 °F).
        Occupancy is 'true' / 'false'.
        """
        for cap in (sensor.get('capability') or []):
            cap_type = cap.get('type')
            value = cap.get('value')
            if cap_type == 'temperature' and value not in (None, '', 'unknown'):
                try:
                    self._set('ST', round(int(value) / 10.0, 1))
                except (TypeError, ValueError):
                    pass
            elif cap_type == 'humidity' and value not in (None, '', 'unknown'):
                try:
                    self._set('CLIHUM', int(value))
                except (TypeError, ValueError):
                    pass
            elif cap_type == 'occupancy':
                self._set('GV0', 1 if str(value).lower() == 'true' else 0)

    def query(self, command=None):
        self.reportDrivers()

    commands = {
        'QUERY': query,
    }


def _sensor_address(sensor_id: str) -> str:
    """ISY addresses must be lowercase alphanumeric, max 14 chars."""
    import re
    cleaned = re.sub(r'[^a-z0-9]', '', (sensor_id or '').lower())
    return cleaned[:14] or 'sensor'


# --- Weather / Forecast Nodes ---------------------------------------------

class _WeatherBase(udi_interface.Node):
    """Shared logic for current-weather and forecast nodes. Both pick a single
    entry out of `thermostat['weather']['forecasts']`; the index decides
    which (0 = current, 1 = tomorrow)."""

    _forecast_index = 0

    drivers = [
        {'driver': 'ST',  'value': 0, 'uom': 17},  # Temperature
        {'driver': 'GV1', 'value': 0, 'uom': 22},  # Humidity %
        {'driver': 'GV2', 'value': 0, 'uom': 22},  # POP %
        {'driver': 'GV3', 'value': 0, 'uom': 17},  # High temp
        {'driver': 'GV4', 'value': 0, 'uom': 17},  # Low temp
        {'driver': 'GV5', 'value': 0, 'uom': 48},  # Wind speed (mph)
        {'driver': 'GV6', 'value': 0, 'uom': 25},  # Wind direction
        {'driver': 'GV7', 'value': 0, 'uom': 25},  # Sky
        {'driver': 'GV8', 'value': 0, 'uom': 25},  # Weather symbol
    ]

    def __init__(self, polyglot, primary, address, name, tstat_id):
        super().__init__(polyglot, primary, address, name)
        self._tstat_id = tstat_id
        self._cache = {}

    def _set(self, driver, value):
        if value is None:
            return
        if self._cache.get(driver) != value:
            self._cache[driver] = value
            self.setDriver(driver, value)

    def apply_state(self, tstat: dict):
        weather = tstat.get('weather') or {}
        forecasts = weather.get('forecasts') or []
        if len(forecasts) <= self._forecast_index:
            return
        fc = forecasts[self._forecast_index]

        self._set('ST', _temp_in(fc.get('temperature')))
        self._set('GV1', fc.get('relativeHumidity'))
        self._set('GV2', fc.get('pop'))
        self._set('GV3', _temp_in(fc.get('tempHigh')))
        self._set('GV4', _temp_in(fc.get('tempLow')))
        self._set('GV5', fc.get('windSpeed'))
        wd = fc.get('windDirection')
        if wd in _WIND_DIR_TO_IDX:
            self._set('GV6', _WIND_DIR_TO_IDX[wd])
        # Ecobee returns -5002 for "unavailable"; clamp negatives to 0.
        sky = fc.get('sky')
        if isinstance(sky, int) and sky >= 0:
            self._set('GV7', sky)
        sym = fc.get('weatherSymbol')
        if isinstance(sym, int) and sym >= 0:
            self._set('GV8', sym)

    def query(self, command=None):
        self.reportDrivers()

    commands = {'QUERY': query}


class WeatherNode(_WeatherBase):
    id = 'ecobee_weather'
    _forecast_index = 0


class ForecastNode(_WeatherBase):
    id = 'ecobee_forecast'
    _forecast_index = 1


# --- Controller ------------------------------------------------------------

class Controller(udi_interface.Node):

    id = 'ecobee_controller'

    drivers = [{'driver': 'ST', 'value': 0, 'uom': 2}]

    def __init__(self, polyglot, primary, address, name):
        super().__init__(polyglot, primary, address, name)
        self.poly = polyglot
        self._params = Custom(polyglot, 'customparams')
        self._thermostats = {}   # tstat_id → ThermostatNode
        self._sensors = {}       # sensor_id → SensorNode
        self._weather = {}       # tstat_id → WeatherNode
        self._forecast = {}      # tstat_id → ForecastNode
        self._node_events = {}   # node address → threading.Event
        self._node_events_lock = threading.Lock()
        self._controller_added = False
        self._reconcile_lock = threading.Lock()
        self._last_params = {}
        self.ecobee: Ecobee | None = None
        self._authenticated = False   # ecobee is non-None even after a failed login
        self.hold_type = 'nextTransition'  # set from custom params

        # Auth retry state. _auth_blocked is a hard stop for failures that only
        # a human can clear (rejected password, pending MFA); retrying those
        # automatically is what gets an account locked out.
        self._auth_failures = 0
        self._auth_retry_at = 0.0     # time.monotonic() deadline; 0 = retry now
        self._auth_blocked = False
        self._mfa_challenge = None
        self._saved_refresh_token = None  # avoid rewriting state on every poll

        polyglot.subscribe(polyglot.CONFIGDONE,   self._on_config_done)
        polyglot.subscribe(polyglot.START,        self.start)
        polyglot.subscribe(polyglot.CUSTOMPARAMS, self._on_params)
        polyglot.subscribe(polyglot.POLL,         self.poll)
        polyglot.subscribe(polyglot.STOP,         self.stop)
        polyglot.subscribe(polyglot.ADDNODEDONE,  self._on_node_added)
        polyglot.ready()

    # --- Node lifecycle ---

    def _on_node_added(self, data):
        addr = (data or {}).get('address')
        with self._node_events_lock:
            if addr is None:
                # Payload without an address: we can't tell who it was for,
                # so wake everyone rather than hang every waiter.
                waiters = list(self._node_events.values())
            else:
                # A known address with no waiter means a late or duplicate ack.
                # Waking someone else here is exactly the cross-wake this
                # per-address scheme exists to prevent.
                ev = self._node_events.get(addr)
                waiters = [ev] if ev else []
        for e in waiters:
            e.set()

    def _add_node_wait(self, node, timeout=15):
        # One Event per address. A single shared Event let each waiter be woken
        # by some *other* node's ADDNODEDONE, so adds were both racy and slow —
        # this plugin adds thermostats, sensors, weather and forecast nodes.
        ev = threading.Event()
        with self._node_events_lock:
            self._node_events[node.address] = ev
        try:
            self.poly.addNode(node)
            if not ev.wait(timeout=timeout):
                LOGGER.warning(f'Timeout adding node {getattr(node, "address", "?")}')
        finally:
            with self._node_events_lock:
                self._node_events.pop(node.address, None)

    def _on_config_done(self):
        if self._controller_added:
            return
        self._add_node_wait(self, timeout=3)
        self._controller_added = True
        self.setDriver('ST', 0)  # set 1 only after successful auth
        self._reconcile()

    def start(self):
        self._controller_added = True

    def stop(self):
        self.setDriver('ST', 0)

    def _on_params(self, params):
        # PG3 always publishes CUSTOMPARAMS at startup, but with a None payload
        # when it has nothing stored; load(None) would wipe the params we have.
        if not params:
            LOGGER.warning('CUSTOMPARAMS with no data — keeping existing params')
            return
        self._params.load(params)
        self._last_params = dict(params)
        ht = (self._last_params.get('hold_type') or '').strip()
        self.hold_type = ht if ht in _HOLD_TYPES else 'nextTransition'

        # An MFA code is only meaningful while a challenge is open, and the
        # challenge lives on the existing client — so consume it before the
        # reset below can discard that client.
        mfa_code = (self._last_params.get('mfa_code') or '').strip()
        if mfa_code and self._mfa_challenge is not None:
            if self._submit_mfa(mfa_code) and self._controller_added:
                self._reconcile()
            return

        # Targeted deletes, not clear() — clear() also wiped the active poll
        # notice every time params were saved.
        self.poly.Notices.delete('creds')
        # Saving params is the user's "I fixed my credentials" signal. Drop the
        # stale auth notice AND the failed client, otherwise _reconcile's
        # `if self.ecobee is None` guard never re-authenticates and the notice
        # telling them to re-save is unreachable forever. It also clears the
        # backoff and the hard block, so the retry happens now rather than at
        # the end of whatever wait was pending.
        self.poly.Notices.delete('auth')
        self.poly.Notices.delete('mfa')
        self._auth_failures = 0
        self._auth_retry_at = 0.0
        self._auth_blocked = False
        self._mfa_challenge = None
        if self.ecobee is not None and not self._authenticated:
            self.ecobee = None
        if self._controller_added:
            self._reconcile()

    # --- Auth + reconcile ---

    def _load_state(self) -> dict:
        try:
            with open(_STATE_FILE) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            LOGGER.warning(f'Failed to load state: {e}')
            return {}

    def _save_state(self, refresh_token: str, username: str):
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_STATE_FILE, 'w') as f:
                json.dump({'refresh_token': refresh_token, 'username': username}, f)
        except Exception as e:
            LOGGER.warning(f'Failed to save state: {e}')

    def _clear_state(self):
        self._saved_refresh_token = None
        try:
            _STATE_FILE.unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            LOGGER.warning(f'Failed to clear state: {e}')

    def _persist_tokens(self):
        """Save the refresh token if the library rotated it."""
        token = getattr(self.ecobee, 'refresh_token', None)
        if token and token != self._saved_refresh_token:
            self._save_state(token, getattr(self.ecobee, 'username', '') or '')
            self._saved_refresh_token = token

    # --- Auth retry policy ---

    def _auth_on_hold(self) -> bool:
        """True when a login attempt right now would just be abuse."""
        return self._auth_blocked or time.monotonic() < self._auth_retry_at

    def _auth_backoff(self, err):
        self._auth_failures += 1
        delay = min(_AUTH_RETRY_BASE * (2 ** (self._auth_failures - 1)), _AUTH_RETRY_MAX)
        self._auth_retry_at = time.monotonic() + delay
        LOGGER.warning(f'ecobee authentication failed ({err}); next attempt in {int(delay)}s')
        self.poly.Notices['auth'] = (
            f'ecobee authentication failed: {err} — retrying in {int(delay)}s.')

    def _handle_auth_error(self, err) -> None:
        """Map a pyecobee auth exception onto a notice and a retry policy."""
        self._authenticated = False
        self.setDriver('ST', 0)

        if isinstance(err, EcobeeAuthMfaRequiredError):
            self._mfa_challenge = err.args[0] if err.args else None
            self._auth_blocked = True
            LOGGER.warning('ecobee requires an MFA code; waiting for the mfa_code param')
            self.poly.Notices['mfa'] = (
                'ecobee is asking for a two-factor code. Put the current code from '
                'your authenticator app (or the SMS) in the `mfa_code` custom param '
                'and save. Codes expire in ~30s, so save promptly. This is needed '
                'once — after it succeeds the plugin refreshes with a stored token.')
        elif isinstance(err, EcobeeAuthFailedError):
            self._auth_blocked = True
            LOGGER.error(f'ecobee rejected the login: {err}')
            self.poly.Notices['auth'] = (
                f'ecobee rejected the login: {err} Fix `username`/`password` in Custom '
                f'Parameters and save to retry. Not retrying on my own — repeated '
                f'failed logins can get the account temporarily locked.')
        else:
            self._auth_backoff(err)

    def _submit_mfa(self, code: str) -> bool:
        """Finish an MFA-gated login with a user-supplied OTP."""
        challenge, self._mfa_challenge = self._mfa_challenge, None
        try:
            self.ecobee.submit_mfa_code(challenge, code)
        except EcobeeAuthFailedError as e:
            # A rejected code leaves the challenge spent; the user needs a whole
            # new login to get a fresh one.
            self._auth_blocked = True
            LOGGER.warning(f'MFA code rejected: {e}')
            self.poly.Notices['mfa'] = (
                f'ecobee rejected that two-factor code ({e}). Save `username`/`password` '
                f'again to start a new login, then enter a fresh `mfa_code`.')
            return False
        except Exception as e:
            LOGGER.error(f'MFA submission failed: {e}', exc_info=True)
            self._handle_auth_error(e)
            return False

        LOGGER.info('MFA accepted; authenticated with ecobee.com')
        self._on_auth_success()
        return True

    def _on_auth_success(self):
        self._authenticated = True
        self._auth_failures = 0
        self._auth_retry_at = 0.0
        self._auth_blocked = False
        self._mfa_challenge = None
        self._persist_tokens()
        self.setDriver('ST', 1)
        for key in ('auth', 'creds', 'import', 'mfa'):
            self.poly.Notices.delete(key)

    def _authenticate(self) -> bool:
        if Ecobee is None:
            self.poly.Notices['import'] = (
                f'python-ecobee-api import failed ({_IMPORT_ERR}) — this plugin needs '
                f'version 0.4.1 or newer. Reinstall the plugin to pull it.')
            return False

        username = (self._last_params.get('username') or '').strip()
        password = (self._last_params.get('password') or '').strip()
        if not username or not password:
            self.poly.Notices['creds'] = (
                'Set `username` and `password` in Custom Parameters to your ecobee.com credentials.')
            return False

        state = self._load_state()
        refresh_token = state.get('refresh_token')
        # refresh_tokens() prefers a stored token over the credentials, so a
        # token cached under a different account would silently keep talking to
        # that account after the username changed.
        if refresh_token and state.get('username') and state['username'] != username:
            LOGGER.info('username changed since the cached token was issued; discarding it')
            self._clear_state()
            refresh_token = None
        self._saved_refresh_token = refresh_token

        # Two passes at most: the cached token, then the credentials if that
        # token turns out to be revoked.
        while True:
            config = {
                ECOBEE_USERNAME: username,
                ECOBEE_PASSWORD: password,
            }
            if refresh_token:
                config[ECOBEE_REFRESH_TOKEN] = refresh_token

            try:
                self.ecobee = Ecobee(config=config)
                if not self.ecobee.refresh_tokens():
                    self._authenticated = False
                    self._auth_backoff('ecobee.com login returned no tokens')
                    return False
            except InvalidTokenError as e:
                if refresh_token:
                    LOGGER.warning(f'Cached refresh token rejected ({e}); logging in with credentials')
                    self._clear_state()
                    refresh_token = None
                    continue
                self._handle_auth_error(EcobeeAuthFailedError(str(e)))
                return False
            except Exception as e:
                LOGGER.error(f'Authentication error: {e}', exc_info=True)
                self._handle_auth_error(e)
                return False
            break

        LOGGER.info('Authenticated with ecobee.com')
        self._on_auth_success()
        return True

    def _reconcile(self):
        with self._reconcile_lock:
            # Checking `self.ecobee is None` alone was the bug behind the retry
            # storm: a failed _authenticate() still leaves a client assigned, so
            # this guard passed and _discover_and_poll ran against an
            # unauthenticated client, which re-triggered a full login inside
            # pyecobee on every single short poll.
            if not self._authenticated:
                if self._auth_on_hold() or not self._authenticate():
                    return
            self._discover_and_poll()

    def _discover_and_poll(self):
        try:
            self.ecobee.update()
        except InvalidTokenError as e:
            # The stored refresh token was revoked (password change, or the
            # grant was dropped from the ecobee account). Discard it and let the
            # next poll log in with the credentials.
            LOGGER.warning(f'ecobee tokens invalid ({e}); will re-authenticate')
            self._clear_state()
            self.ecobee = None
            self._authenticated = False
            self.setDriver('ST', 0)
            self._mark_all_offline()
            return
        except (EcobeeAuthFailedError, EcobeeAuthMfaRequiredError, EcobeeAuthUnknownError) as e:
            # update() refreshes tokens internally, so a login failure surfaces
            # here too. Route it through the same policy rather than letting the
            # short poll retry it 1,440 times a day.
            LOGGER.error(f'update() failed during token refresh: {e}', exc_info=True)
            self._handle_auth_error(e)
            self._mark_all_offline()
            return
        except Exception as e:
            LOGGER.error(f'update() failed: {e}', exc_info=True)
            self.poly.Notices['poll'] = f'Ecobee poll failed: {e}'
            # ST tracks whether we can actually talk to ecobee.com. It used to
            # be set to 1 at auth and never cleared, so it read "online" for
            # the whole of an outage.
            self.setDriver('ST', 0)
            self._mark_all_offline()
            return
        self.poly.Notices.delete('poll')
        self.setDriver('ST', 1)

        # Persist the refresh token if Auth0 rotated it during the call.
        self._persist_tokens()

        seen_tstats = set()
        seen_sensors = set()
        for tstat in (self.ecobee.thermostats or []):
            tid = tstat.get('identifier')
            if not tid:
                continue
            seen_tstats.add(tid)
            if tid not in self._thermostats:
                name = tstat.get('name') or f'Ecobee {tid}'
                address = f't{tid}'[:14].lower()
                node = ThermostatNode(self.poly, self.address, address, name, tid, self)
                self._add_node_wait(node)
                self._thermostats[tid] = node
            self._thermostats[tid].apply_state(tstat)

            # Weather / Forecast nodes. Only create when ecobee actually
            # returns forecast data for this thermostat — older accounts or
            # offline thermostats may lack it.
            forecasts = (tstat.get('weather') or {}).get('forecasts') or []
            tname = tstat.get('name') or f'Ecobee {tid}'
            if forecasts and tid not in self._weather:
                waddr = f'w{tid}'[:14].lower()
                wnode = WeatherNode(self.poly, self.address, waddr,
                                    f'{tname} Weather', tid)
                self._add_node_wait(wnode)
                self._weather[tid] = wnode
            if tid in self._weather:
                self._weather[tid].apply_state(tstat)
            if len(forecasts) > 1 and tid not in self._forecast:
                faddr = f'f{tid}'[:14].lower()
                fnode = ForecastNode(self.poly, self.address, faddr,
                                     f'{tname} Forecast', tid)
                self._add_node_wait(fnode)
                self._forecast[tid] = fnode
            if tid in self._forecast:
                self._forecast[tid].apply_state(tstat)

            # Discover and update remote sensors. Skip the thermostat's own
            # internal sensor (type='thermostat') — that data is already on
            # the thermostat node.
            for sensor in (tstat.get('remoteSensors') or []):
                if sensor.get('type') == 'thermostat':
                    continue
                sid = sensor.get('id')
                if not sid:
                    continue
                seen_sensors.add(sid)
                if sid not in self._sensors:
                    sname = sensor.get('name') or f'Sensor {sid}'
                    saddr = _sensor_address(sid)
                    snode = SensorNode(self.poly, self.address, saddr, sname, sid)
                    self._add_node_wait(snode)
                    self._sensors[sid] = snode
                self._sensors[sid].apply_state(sensor)

        # Mark any cached thermostat not in the latest fetch as offline (rare).
        for tid, node in self._thermostats.items():
            if tid not in seen_tstats:
                node.mark_offline()

    def _mark_all_offline(self):
        for node in self._thermostats.values():
            node.mark_offline()

    def poll_now(self):
        """Triggered by a thermostat node's QUERY command — fresh fetch."""
        with self._reconcile_lock:
            if not self._authenticated:
                if self._auth_on_hold() or not self._authenticate():
                    return
            self._discover_and_poll()

    # --- Polly hooks ---

    def poll(self, flag):
        if flag == 'shortPoll':
            self._reconcile()
        elif flag == 'longPoll':
            # Keep the access token warm. Since 0.4.1 this is an OAuth2
            # refresh_token grant — no credentials on the wire and no MFA
            # prompt — so it is cheap enough to run on a timer.
            if self.ecobee is not None and self._authenticated and not self._auth_on_hold():
                try:
                    self.ecobee.refresh_tokens()
                    self._persist_tokens()
                except Exception as e:
                    LOGGER.warning(f'Token refresh failed: {e}')
                    self._handle_auth_error(e)

    def query(self, command=None):
        self.reportDrivers()
        for node in self._thermostats.values():
            node.reportDrivers()
        for node in self._sensors.values():
            node.reportDrivers()
        for node in self._weather.values():
            node.reportDrivers()
        for node in self._forecast.values():
            node.reportDrivers()

    def cmd_discover(self, command=None):
        self._reconcile()

    commands = {
        'QUERY':    query,
        'DISCOVER': cmd_discover,
    }


# --- Main ------------------------------------------------------------------

if __name__ == '__main__':
    try:
        poly = udi_interface.Interface([])
        poly.start()
        Controller(poly, 'controller', 'controller', 'Ecobee')
        poly.runForever()
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
    except Exception as e:
        LOGGER.exception(f'Fatal error: {e}')
        sys.exit(1)
