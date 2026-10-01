#!/usr/bin/env python3
"""Prometheus exporter for Huawei FusionSolar using the web portal session.

Logs in with a normal portal user and reads the JSON endpoints the portal
pages use. All settings come from environment variables, see .env.example.
"""

import base64
import logging
import os
import secrets
import threading
import time
import urllib.parse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from prometheus_client import Counter, Gauge, start_http_server

LOGIN_URL = os.environ.get("FUSIONSOLAR_LOGIN_URL", "https://eu5.fusionsolar.huawei.com").rstrip("/")
# leave empty to use the regional host the login redirects to
PORTAL_URL = os.environ.get("FUSIONSOLAR_PORTAL_URL", "").rstrip("/")
USERNAME = os.environ.get("FUSIONSOLAR_USERNAME", "")
PASSWORD = os.environ.get("FUSIONSOLAR_PASSWORD", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))
INVENTORY_TTL = int(os.environ.get("INVENTORY_TTL_SECONDS", "21600"))
LOGIN_BACKOFF = int(os.environ.get("LOGIN_BACKOFF_SECONDS", "1800"))
TZ_OFFSET = float(os.environ.get("TIMEZONE_OFFSET_HOURS", "0"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9850"))
HTTP_TIMEOUT = 30

INVERTER_MOC = 20822
DEVICE_MOC_TYPES = "20814,20815,20816,20819,20822,50017,60066,60014,60015,23037"
# Signal ids in device-realtime-data for inverters
SIG_ACTIVE_POWER = 10018
SIG_DAY_ENERGY = 10032
SIG_TOTAL_ENERGY = 10029
SIG_TEMPERATURE = 10023
SIG_RATED_POWER = 10006
SIG_FREQUENCY = 10021
SIG_PHASE_VOLTAGE = {"A": 10011, "B": 10012, "C": 10013}
SIG_PHASE_CURRENT = {"A": 10014, "B": 10015, "C": 10016}
# PV string signals in device-real-kpi: voltage 11001 + 3n, current 11002 + 3n
PV_STRINGS = int(os.environ.get("PV_STRINGS", "8"))

log = logging.getLogger("fusionsolar")

ST = ["station_code", "station_name"]
DEV = ["station_code", "station_name", "device_id", "device_name"]

UP = Gauge("fusionsolar_up", "1 if the last poll succeeded")
LAST_OK = Gauge("fusionsolar_last_success_timestamp_seconds", "Unix time of the last successful poll")
ERRORS = Counter("fusionsolar_api_errors_total", "Errors by stage", ["stage"])
ALARMS = Gauge("fusionsolar_active_alarms", "Active alarms, severity 1 critical to 4 warning", ["severity"])

STATION_CAPACITY = Gauge("fusionsolar_station_capacity_kw", "Installed string capacity (kWp)", ST)
STATION_HEALTH = Gauge("fusionsolar_station_health_state", "1 disconnected, 2 faulty, 3 healthy", ST)
STATION_POWER = Gauge("fusionsolar_station_active_power_kw", "Current power", ST)
STATION_DAY = Gauge("fusionsolar_station_day_energy_kwh", "Yield today", ST)
STATION_MONTH = Gauge("fusionsolar_station_month_energy_kwh", "Yield this month", ST)
STATION_YEAR = Gauge("fusionsolar_station_year_energy_kwh", "Yield this year", ST)
STATION_TOTAL = Gauge("fusionsolar_station_total_energy_kwh", "Lifetime yield", ST)
STATION_DAY_INCOME = Gauge("fusionsolar_station_day_income", "Revenue today", ST)
FLEET_DAY_INCOME = Gauge("fusionsolar_day_income", "Revenue today, all plants")

INV_POWER = Gauge("fusionsolar_inverter_active_power_kw", "Inverter active power", DEV)
INV_RATED = Gauge("fusionsolar_inverter_rated_power_kw", "Inverter rated power", DEV)
INV_DAY = Gauge("fusionsolar_inverter_day_energy_kwh", "Inverter yield today", DEV)
INV_TOTAL = Gauge("fusionsolar_inverter_total_energy_kwh", "Inverter lifetime yield", DEV)
INV_TEMP = Gauge("fusionsolar_inverter_temperature_celsius", "Inverter internal temperature", DEV)
INV_FREQ = Gauge("fusionsolar_inverter_grid_frequency_hz", "Grid frequency", DEV)
INV_DC_POWER = Gauge("fusionsolar_inverter_dc_input_power_kw", "Total DC input power, sum over PV strings", DEV)
INV_PV_POWER = Gauge("fusionsolar_inverter_pv_power_kw", "PV string input power", DEV + ["pv"])
INV_PV_VOLTAGE = Gauge("fusionsolar_inverter_pv_voltage_volts", "PV string input voltage", DEV + ["pv"])
INV_PV_CURRENT = Gauge("fusionsolar_inverter_pv_current_amps", "PV string input current", DEV + ["pv"])
INV_PHASE_VOLTAGE = Gauge("fusionsolar_inverter_phase_voltage_volts", "Grid phase voltage", DEV + ["phase"])
INV_PHASE_CURRENT = Gauge("fusionsolar_inverter_phase_current_amps", "Grid phase current", DEV + ["phase"])
INV_STATE = Gauge("fusionsolar_inverter_run_state", "0 disconnected, 1 connected", DEV)


class LoginError(Exception):
    pass


class SessionExpired(Exception):
    pass


def num(value):
    try:
        if value is None or value == "":
            return None
        v = float(value)
    except (TypeError, ValueError):
        return None
    # the portal uses -99999999 for "no data"
    return None if v <= -9e7 else v


def set_gauge(gauge, labels, value):
    v = num(value)
    if v is not None:
        gauge.labels(*labels).set(v)


def encrypt_password(pubkey, password):
    key = serialization.load_pem_public_key(pubkey["pubKey"].encode())
    quoted = urllib.parse.quote(password)
    oaep = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA384()), algorithm=hashes.SHA384(), label=None)
    parts = []
    for i in range(0, len(quoted), 270):
        parts.append(base64.b64encode(key.encrypt(quoted[i:i + 270].encode(), oaep)).decode())
    return "00000001".join(parts) + str(pubkey.get("version", ""))


class Portal:
    def __init__(self):
        self.session = None
        self.token = None
        self.next_login_at = 0
        self.base = PORTAL_URL

    def login(self):
        if time.time() < self.next_login_at:
            raise LoginError("waiting before next login attempt")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        )
        self.token = None
        try:
            self._login()
        except Exception:
            # repeated failures lock the account or trigger a captcha, so back off
            self.next_login_at = time.time() + LOGIN_BACKOFF
            raise
        log.info("logged in, portal %s", self.base)

    def _login(self):
        pubkey = self.session.get(f"{LOGIN_URL}/unisso/pubkey", timeout=HTTP_TIMEOUT).json()
        if pubkey.get("enableEncrypt"):
            url = f"{LOGIN_URL}/unisso/v3/validateUser.action"
            params = {"timeStamp": pubkey["timeStamp"], "nonce": secrets.token_hex(16)}
            password = encrypt_password(pubkey, PASSWORD)
        else:
            host = urllib.parse.urlparse(LOGIN_URL).netloc
            service = f"https://{host}/unisso/login.action?service=" + urllib.parse.quote(
                f"https://{host}/netecowebext/home/index.html#/LOGIN", safe=""
            )
            url = f"{LOGIN_URL}/unisso/v2/validateUser.action"
            params = {"decision": 1, "service": service}
            password = PASSWORD
        r = self.session.post(
            url,
            params=params,
            json={"organizationName": "", "username": USERNAME, "password": password},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
        code = str(body.get("errorCode") or "")
        if code == "470" and body.get("respMultiRegionName"):
            target = body["respMultiRegionName"][1]
        elif code in ("", "0") and not body.get("errorMsg"):
            target = body.get("redirectURL")
        elif code == "411":
            raise LoginError("portal asks for a captcha, log in once in a browser and retry later")
        else:
            raise LoginError(f"login rejected, errorCode={code} {body.get('errorMsg') or ''}".strip())
        if target:
            if target.startswith("/"):
                target = LOGIN_URL + target
            landed = self.session.get(target, timeout=HTTP_TIMEOUT)
            if not PORTAL_URL:
                url = urllib.parse.urlparse(landed.url)
                self.base = f"{url.scheme}://{url.netloc}"
        if not self.base:
            raise LoginError("portal host unknown, set FUSIONSOLAR_PORTAL_URL")
        self.refresh_token()

    def refresh_token(self):
        r = self.session.get(f"{self.base}/rest/dpcloud/auth/v1/keep-alive", timeout=HTTP_TIMEOUT)
        try:
            body = r.json()
        except ValueError:
            raise SessionExpired("keep-alive returned no JSON")
        if body.get("code") != 0 or not body.get("payload"):
            raise SessionExpired(f"keep-alive code={body.get('code')}")
        self.token = body["payload"]

    def ensure_session(self):
        if self.session is None:
            self.login()
            return
        try:
            self.refresh_token()
        except (SessionExpired, requests.RequestException):
            log.info("session expired, logging in again")
            self.login()

    def request(self, method, path, **kwargs):
        r = self.session.request(
            method, self.base + path, headers={"roarand": self.token}, timeout=HTTP_TIMEOUT,
            allow_redirects=False, **kwargs
        )
        if r.status_code in (301, 302, 401, 403):
            self.session = None
            raise SessionExpired(f"{path}: HTTP {r.status_code}")
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            self.session = None
            raise SessionExpired(f"{path}: no JSON in response")

    def get(self, path, params=None):
        params = dict(params or {})
        params["_"] = int(time.time() * 1000)
        return self.request("GET", path, params=params)

    def post(self, path, payload):
        return self.request("POST", path, json=payload)


def local_midnight_ms():
    offset = TZ_OFFSET * 3600
    now = time.time() + offset
    return int((now - now % 86400 - offset) * 1000)


HEALTH = {"connected": 3, "disconnected": 1}


class Collector:
    def __init__(self):
        self.portal = Portal()
        self.inverters = {}   # station dn -> list of device dicts
        self.inventory_at = 0

    def fetch_stations(self):
        stations, page = [], 1
        while True:
            body = self.portal.post("/rest/pvms/web/station/v1/station/station-list", {
                "curPage": page, "pageSize": 100, "gridConnectedTime": "",
                "queryTime": local_midnight_ms(), "timeZone": TZ_OFFSET,
                "sortId": "createTime", "sortDir": "DESC", "locale": "en_US",
            })
            if not body.get("success"):
                raise RuntimeError(f"station-list failCode={body.get('failCode')}")
            data = body.get("data") or {}
            stations += data.get("list") or []
            if len(stations) >= int(data.get("total") or 0) or not data.get("list"):
                return stations
            page += 1

    def refresh_inventory(self, stations):
        known = set(self.inverters) == {s["dn"] for s in stations}
        if known and time.time() - self.inventory_at < INVENTORY_TTL:
            return
        inverters = {}
        for s in stations:
            body = self.portal.get("/rest/neteco/web/config/device/v1/device-list", {
                "conditionParams.parentDn": s["dn"],
                "conditionParams.mocTypes": DEVICE_MOC_TYPES,
            })
            inverters[s["dn"]] = [d for d in body.get("data") or [] if d.get("mocId") == INVERTER_MOC]
        self.inverters = inverters
        self.inventory_at = time.time()
        log.info("inventory: %d plants, %d inverters", len(stations), sum(len(v) for v in inverters.values()))

    def poll_inverter(self, station, dev):
        labels = (station["dn"], station.get("name") or station["dn"], dev["dn"], dev.get("name") or "")
        INV_STATE.labels(*labels).set(1 if str(dev.get("status", "")).lower() == "connected" else 0)
        body = self.portal.get("/rest/pvms/web/device/v1/device-realtime-data", {"deviceDn": dev["dn"]})
        signals = {}
        for group in body.get("data") or []:
            for sig in group.get("signals") or []:
                signals[sig.get("id")] = sig.get("value")
        set_gauge(INV_POWER, labels, signals.get(SIG_ACTIVE_POWER))
        set_gauge(INV_RATED, labels, signals.get(SIG_RATED_POWER))
        set_gauge(INV_DAY, labels, signals.get(SIG_DAY_ENERGY))
        set_gauge(INV_TOTAL, labels, signals.get(SIG_TOTAL_ENERGY))
        set_gauge(INV_TEMP, labels, signals.get(SIG_TEMPERATURE))
        set_gauge(INV_FREQ, labels, signals.get(SIG_FREQUENCY))
        for phase, sig in SIG_PHASE_VOLTAGE.items():
            set_gauge(INV_PHASE_VOLTAGE, labels + (phase,), signals.get(sig))
        for phase, sig in SIG_PHASE_CURRENT.items():
            set_gauge(INV_PHASE_CURRENT, labels + (phase,), signals.get(sig))
        try:
            self.poll_inverter_dc(dev, labels)
        except SessionExpired:
            raise
        except Exception as exc:
            ERRORS.labels("dc").inc()
            log.warning("DC data for %s failed: %s", dev.get("name"), exc)

    def poll_inverter_dc(self, dev, labels):
        ids = []
        for n in range(PV_STRINGS):
            ids += [11001 + 3 * n, 11002 + 3 * n]
        body = self.portal.get("/rest/pvms/web/device/v1/device-real-kpi", {
            "signalIds": ids, "deviceDn": dev["dn"],
        })
        signals = (body.get("data") or {}).get("signals") or {}

        def value(sig):
            return num((signals.get(str(sig)) or {}).get("realValue"))

        total, seen = 0.0, False
        for n in range(PV_STRINGS):
            pv = str(n + 1)
            volts, amps = value(11001 + 3 * n), value(11002 + 3 * n)
            set_gauge(INV_PV_VOLTAGE, labels + (pv,), volts)
            set_gauge(INV_PV_CURRENT, labels + (pv,), amps)
            if volts is not None and amps is not None:
                total += volts * amps / 1000
                seen = True
                INV_PV_POWER.labels(*labels, pv).set(round(volts * amps / 1000, 3))
        if seen:
            INV_DC_POWER.labels(*labels).set(round(total, 3))

    def poll_once(self):
        self.portal.ensure_session()
        stations = self.fetch_stations()
        for s in stations:
            labels = (s["dn"], s.get("name") or s["dn"])
            set_gauge(STATION_CAPACITY, labels, s.get("installedCapacity"))
            STATION_HEALTH.labels(*labels).set(HEALTH.get(str(s.get("plantStatus", "")).lower(), 2))
            set_gauge(STATION_POWER, labels, s.get("currentPower"))
            set_gauge(STATION_DAY, labels, s.get("dailyEnergy"))
            set_gauge(STATION_MONTH, labels, s.get("monthEnergy"))
            set_gauge(STATION_YEAR, labels, s.get("yearEnergy"))
            set_gauge(STATION_TOTAL, labels, s.get("cumulativeEnergy"))
            set_gauge(STATION_DAY_INCOME, labels, s.get("dailyIncome"))

        kpi = self.portal.get("/rest/pvms/web/station/v1/station/total-real-kpi",
                              {"queryTime": local_midnight_ms(), "timeZone": TZ_OFFSET})
        income = num((kpi.get("data") or {}).get("dailyIncome"))
        if income is not None:
            FLEET_DAY_INCOME.set(income)

        alarms = self.portal.get("/rest/pvms/fm/v1/statistic")
        for row in alarms.get("data") or []:
            set_gauge(ALARMS, (str(row.get("severity")),), row.get("value"))

        self.refresh_inventory(stations)
        for s in stations:
            for dev in self.inverters.get(s["dn"], []):
                self.poll_inverter(s, dev)

    def run(self):
        while True:
            started = time.time()
            try:
                self.poll_once()
                UP.set(1)
                LAST_OK.set(time.time())
            except LoginError as exc:
                UP.set(0)
                ERRORS.labels("login").inc()
                log.error("%s", exc)
            except Exception as exc:
                UP.set(0)
                ERRORS.labels("poll").inc()
                log.error("poll failed: %s", exc)
            time.sleep(max(5, POLL_INTERVAL - (time.time() - started)))


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    if not USERNAME or not PASSWORD:
        raise SystemExit("FUSIONSOLAR_USERNAME and FUSIONSOLAR_PASSWORD must be set")
    start_http_server(LISTEN_PORT)
    log.info("serving metrics on :%d, polling every %ds", LISTEN_PORT, POLL_INTERVAL)
    threading.Thread(target=Collector().run, daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
