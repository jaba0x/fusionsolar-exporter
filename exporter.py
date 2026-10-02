#!/usr/bin/env python3
"""Prometheus exporter for Huawei FusionSolar using the web portal session.

Logs in with a normal portal user and reads the JSON endpoints the portal
pages use. All settings come from environment variables, see .env.example.
"""

import base64
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from prometheus_client import Counter, Gauge, make_wsgi_app

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
# Alarm list and inverter status changes, served as JSON on /api/alarms and /api/events
ALARM_INTERVAL = int(os.environ.get("ALARM_INTERVAL_SECONDS", "300"))
ALARM_HISTORY_DAYS = int(os.environ.get("ALARM_HISTORY_DAYS", "3650"))
EVENT_LIMIT = 5000
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
SIG_STATUS = 10025
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


class LogStore:
    """Alarms and status changes kept in memory for the JSON endpoints."""

    def __init__(self):
        self.lock = threading.Lock()
        self.alarms = {}
        self.events = []
        self.alarms_at = 0

    def put_alarms(self, alarms, active_ids, now):
        with self.lock:
            for alarm in alarms:
                self.alarms[alarm["id"]] = alarm
            # an alarm that left the active list without a history row was cleared in between
            for alarm in self.alarms.values():
                if alarm["cleared"] is None and alarm["id"] not in active_ids:
                    alarm["cleared"] = now
            self.alarms_at = now

    def add_event(self, event):
        with self.lock:
            self.events.append(event)
            del self.events[:-EVENT_LIMIT]

    def alarms_since(self, since):
        with self.lock:
            rows = [a for a in self.alarms.values()
                    if a["cleared"] is None or a["occurred"] >= since or a["cleared"] >= since]
            return {"updated": self.alarms_at, "alarms": sorted(rows, key=lambda a: a["occurred"])}

    def events_since(self, since):
        with self.lock:
            return {"events": [e for e in self.events if e["ts"] >= since]}


STORE = LogStore()


def clean_text(value):
    text = " ".join(str(value or "").split())
    return text.replace(" :", ":")


def alarm_row(hit):
    cleared = int(hit.get("cleared") or 0) == 1 and hit.get("clearUtc")
    return {
        "id": str(hit.get("csn")),
        "station_code": hit.get("nativeMeDn") or "",
        "station_name": hit.get("meName") or "",
        "device_id": hit.get("nativeMoDn") or "",
        "device_name": hit.get("devNameStr") or "",
        "device_type": hit.get("devTypeStr") or "",
        "sn": hit.get("esn") or "",
        "alarm_id": str(hit.get("alarmId") or ""),
        "name": hit.get("alarmName") or "",
        "severity": int(hit.get("severity") or 0),
        "occurred": int((hit.get("occurUtc") or 0) / 1000),
        "cleared": int(hit["clearUtc"] / 1000) if cleared else None,
        "detail": hit.get("additionalInformation") or "",
    }


class Collector:
    def __init__(self):
        self.portal = Portal()
        self.inverters = {}   # station dn -> list of device dicts
        self.inventory_at = 0
        self.alarms_at = 0
        self.alarm_history_loaded = False
        self.status = {}      # device dn -> last status text

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
        self.track_status(station, dev, clean_text(signals.get(SIG_STATUS)) or "No data")
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

    def track_status(self, station, dev, text):
        before = self.status.get(dev["dn"])
        self.status[dev["dn"]] = text
        if before is None or before == text:
            return
        now = int(time.time())
        STORE.add_event({
            "id": f"{dev['dn']}:{now}", "ts": now,
            "station_code": station["dn"], "station_name": station.get("name") or "",
            "device_id": dev["dn"], "device_name": dev.get("name") or "",
            "from": before, "to": text,
        })

    def fetch_alarms(self, data_type, begin_ms):
        rows, page = [], 1
        while page <= 50:
            body = self.portal.post("/rest/pvms/fm/v1/query", {
                "dataType": data_type, "domainType": "OC_SOLAR", "pageNo": page, "pageSize": 100,
                "nativeMeDn": "", "nativeMoDn": [],
                "occurUTC": {"begin": begin_ms, "end": int(time.time() * 1000)},
                "sort": {"field": "occurUtc", "order": "desc"},
            })
            if not body.get("success"):
                raise RuntimeError(f"alarm query failCode={body.get('failCode')}")
            data = body.get("data") or {}
            hits = data.get("hits") or []
            rows += [alarm_row(h) for h in hits]
            if not hits or len(rows) >= int(data.get("totalCount") or 0):
                break
            page += 1
        return rows

    def poll_alarms(self):
        now = int(time.time())
        days = 14 if self.alarm_history_loaded else ALARM_HISTORY_DAYS
        active = self.fetch_alarms("CURRENT", (now - ALARM_HISTORY_DAYS * 86400) * 1000)
        for alarm in active:
            alarm["cleared"] = None
        history = self.fetch_alarms("HISTORY", (now - days * 86400) * 1000)
        STORE.put_alarms(history + active, {a["id"] for a in active}, now)
        self.alarm_history_loaded = True
        self.alarms_at = time.time()

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
        if time.time() - self.alarms_at >= ALARM_INTERVAL:
            try:
                self.poll_alarms()
            except SessionExpired:
                raise
            except Exception as exc:
                self.alarms_at = time.time()
                ERRORS.labels("alarms").inc()
                log.warning("alarm list failed: %s", exc)

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


METRICS_APP = make_wsgi_app()


def http_app(environ, start_response):
    path = environ.get("PATH_INFO", "")
    if path not in ("/api/alarms", "/api/events"):
        return METRICS_APP(environ, start_response)
    query = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    try:
        since = int(float(query.get("since", ["0"])[0]))
    except ValueError:
        since = 0
    data = STORE.alarms_since(since) if path == "/api/alarms" else STORE.events_since(since)
    body = json.dumps(data, ensure_ascii=False).encode()
    start_response("200 OK", [("Content-Type", "application/json; charset=utf-8"),
                              ("Content-Length", str(len(body)))])
    return [body]


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class ThreadingServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    if not USERNAME or not PASSWORD:
        raise SystemExit("FUSIONSOLAR_USERNAME and FUSIONSOLAR_PASSWORD must be set")
    server = make_server("", LISTEN_PORT, http_app, ThreadingServer, handler_class=QuietHandler)
    log.info("serving metrics on :%d, polling every %ds", LISTEN_PORT, POLL_INTERVAL)
    threading.Thread(target=Collector().run, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
