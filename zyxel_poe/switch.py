from datetime import timedelta
import logging
import asyncio
import aiohttp
import async_timeout
import math
from time import time
from random import random
from aiohttp import ClientResponse

import voluptuous as vol

from homeassistant.const import STATE_ON, STATE_OFF
from homeassistant.components.switch import PLATFORM_SCHEMA, SwitchEntity
from homeassistant.const import CONF_HOST, CONF_USERNAME, CONF_PASSWORD, CONF_SCAN_INTERVAL
import homeassistant.helpers.config_validation as cv
from homeassistant.util import Throttle
from homeassistant.helpers.aiohttp_client import async_create_clientsession

REQUIREMENTS = ['beautifulsoup4==4.7.1']

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=1)

CONF_DEVICES = 'devices'

DEVICES_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): cv.string,
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
})

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_DEVICES): vol.All(cv.ensure_list, [DEVICES_SCHEMA]),
})


# from: https://github.com/jonbulica99/zyxel-poe-manager
def encode(_input):
    # The python representation of the JS function with the same name.
    # This could be improved further, but I can't be bothered.
    password = ""
    possible = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    _len = lenn = len(_input)
    i = 1
    while i <= (321 - _len):
        if 0 == i % 5 and _len > 0:
            _len -= 1
            password += _input[_len]
        elif i == 123:
            if lenn < 10:
                password += "0"
            else:
                password += str(math.floor(lenn / 10))
        elif i == 289:
            password += str(lenn % 10)
        else:
            password += possible[math.floor(random() * len(possible))]
        i += 1
    return password


async def async_setup_platform(
        hass, config, async_add_entities, discovery_info=None):
    """Set up the zyxel-poe sensor platform."""

    for device_config in config[CONF_DEVICES]:
        host = device_config[CONF_HOST]
        username = device_config[CONF_USERNAME]
        password = device_config[CONF_PASSWORD]
        interval = device_config.get(CONF_SCAN_INTERVAL, SCAN_INTERVAL)

        session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar(unsafe=True))

        poe_data = ZyxelPoeData(host, username, password, interval, session)

        await poe_data.async_update()

        switches = list()
        for port, data in poe_data.ports.items():
            switches.append(ZyxelPoeSwitch(poe_data, host, port))

        async_add_entities(switches, False)

class ZyxelPoeSwitch(SwitchEntity):
    def __init__(self, poe_data, host, port):
        self._poe_data = poe_data
        self._host = host
        self._port = port

    @property
    def is_on(self):
        """Return true if switch is on."""
        return self._poe_data.ports[self._port]['state'] == STATE_ON

    async def async_turn_on(self):
        self._poe_data.ports[self._port]['state'] = STATE_ON
        return await self._poe_data.change_state(self._port, 1)

    async def async_turn_off(self):
        self._poe_data.ports[self._port]['state'] = STATE_OFF
        return await self._poe_data.change_state(self._port, 0)

    @property
    def name(self):
        name = "{} port{}".format(self._host, self._port)
        return name

    @property
    def state(self):
        return self._poe_data.ports[self._port]['state']

    @property
    def extra_state_attributes(self):
        """Return attributes for the sensor."""
        return self._poe_data.ports[self._port]

    async def async_update(self):
        await self._poe_data.async_update()

class ZyxelPoeData:
    def __init__(self, host, username, password, interval, session):
        self.devices = {}
        self.ports = {}

        self._url = "http://{}/cgi-bin/dispatcher.cgi".format(host)
        self._username = username
        self._password = password
        self._session = session

        self.async_update = Throttle(interval)(self._async_update)

    async def _login(self, is_retry=False):
        if 'HTTP_XSSID' in [c.key for c in self._session.cookie_jar]:
            return

        login_data = {
            "username": self._username,
            "password": encode(self._password),
            "login": 'true;',
        }

        login_step1 = await self._session.post(self._url, data=login_data)
        text = await login_step1.text()

        login_check_data = {
            "authId": text.strip(),
            "login_chk": 'true',
        }

        await asyncio.sleep(1) # implicitely wait for login to occur

        login_step2 = await self._session.post(self._url, data=login_check_data)
        text = await login_step2.text()

        if 'OK' not in text:
            if is_retry:
                raise Exception("Login failed: %s" % login_step2.text)
            await self._login(is_retry=True)

    async def change_state(self, port, state, is_retry=False):
        from bs4 import BeautifulSoup
        try:
            with async_timeout.timeout(10):
                await self._login()

                ret = await self._session.get(self._url, params={'cmd':'773'})
                text = await ret.text()
                if not ret.ok:
                    raise Exception("Refresh failed. Got response: %s" % text)

                soup = BeautifulSoup(text, 'html.parser')
                xssid_content = soup.find('input', {'name': 'XSSID'}).get('value')
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            _LOGGER.error("Cannot load Zyxel data: %s", e)
            return False

        command_data = {
            "XSSID": xssid_content,
            "portlist": port,
            "state": state,
            "portPriority": 2,
            "portPowerMode": 3,
            "portRangeDetection": 0,
            "portLimitMode": 0,
            "poeTimeRange": 20,
            "cmd": 775,
            "sysSubmit": "Apply"
        }

        try:
            with async_timeout.timeout(10):
                await self._login()
                res = await self._session.post(self._url, data=command_data)
                text = await res.text()
                if not 'window.location.replace' in text:
                    if is_retry:
                        _LOGGER.error("Cannot load perform action: %s", text)
                        return False
                    self._session.cookie_jar.clear()
                    await self._login(is_retry=True)
                    return await self.change_state(port, state, is_retry=True)
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            _LOGGER.error("Cannot load Zyxel data: %s", e)
            return False
        return True

    async def _async_update(self):
        from bs4 import BeautifulSoup

        try:
            with async_timeout.timeout(10):
                await self._login()

                ret = await self._session.get(self._url, params={'cmd':'773'})
                text = await ret.text()
                if not ret.ok:
                    raise Exception("Refresh failed. Got response: %s" % text)

                soup = BeautifulSoup(text, 'html.parser')
                table = soup.select("table")[2]
                for row in table.find_all('tr'):
                   cols = row.find_all('td')
                   if len(cols) != 13:
                       continue
                   _, _, port, state, pd_class, pd_priority, power_up, wide_range_detection, consuming_power_mw, max_power_mw, time_range_name, time_range_status, _ = map(lambda a: a.text.strip(), cols)
                   if state == 'Enable':
                       state = STATE_ON
                   elif state == 'Disable':
                      state = STATE_OFF
                   port_attrs = {
                     'port': port,
                     'state': state,
                     'class': pd_class,
                     'priority': pd_priority,
                     'power_up': power_up,
                     'wide_range_detection': wide_range_detection,
                     'current_power_w': int(consuming_power_mw) / 1000.,
                     'max_power_w': int(max_power_mw) / 1000.,
                     'time_range_name': time_range_name,
                     'time_range_status': time_range_status,
                   }
                   self.ports[port] = port_attrs

        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            _LOGGER.error("Cannot load Zyxel data: %s", e)
            return False
