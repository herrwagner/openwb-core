import logging
import os
import re
import time
from datetime import datetime, timezone
from json import load, dump, JSONDecodeError
from pathlib import Path
from threading import Lock

from requests.exceptions import ConnectionError, HTTPError, Timeout

from modules.common.component_state import CarState
from modules.common.req import get_http_session

log = logging.getLogger(__name__)

DATA_PATH = Path(__file__).resolve().parents[4] / "data" / "modules" / "bmw_cardata"

TOKEN_URL = "https://customer.bmwgroup.com/gcdm/oauth/token"
CARDATA_BASE_URL = "https://api-cardata.bmwgroup.com/customers/vehicles"

TOKEN_EXPIRY_BUFFER = 60  # Sekunden Puffer vor Ablauf des Tokens
DEFAULT_TOKEN_LIFETIME = 3600  # Standard Token-Lebensdauer in Sekunden

MAX_RETRIES = 3  # Maximale Anzahl Wiederholungsversuche bei transienten Fehlern
RETRY_DELAY = 5  # Wartezeit in Sekunden zwischen Wiederholungsversuchen
RETRYABLE_STATUS_CODES = {408, 500, 502, 503}

DAILY_REQUEST_LIMIT = 50
DAILY_REQUEST_WARNING = 40

VIN_PATTERN = re.compile(r'^[A-HJ-NPR-Z0-9]{17}$')

# BMW CarData API Fehlercodes
ERROR_MESSAGES = {
    "CU-100": "Ungueltige Anfrage",
    "CU-101": "Ungueltige Parameter",
    "CU-102": "Token abgelaufen",
    "CU-200": "Fahrzeug nicht gefunden",
    "CU-201": "Container nicht gefunden",
    "CU-300": "Keine Telematikdaten verfuegbar",
    "CU-429": "API-Ratenlimit erreicht (max. 50 Anfragen/Tag)",
    "CU-500": "Interner Serverfehler",
    "CU-503": "Service nicht verfuegbar",
}


class CarDataError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"BMW CarData API Fehler {code}: {message}")


class Api:
    _instance = None
    _lock = Lock()
    _tokens = {}
    _request_counts = {}

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = object.__new__(cls)
        return cls._instance

    def _get_token_file(self, client_id: str) -> Path:
        return DATA_PATH / f"token_{client_id}.json"

    def _load_tokens(self, client_id: str) -> dict:
        if client_id in self._tokens:
            return self._tokens[client_id]
        token_file = self._get_token_file(client_id)
        if token_file.is_file():
            try:
                with open(token_file, 'r', encoding='utf-8') as f:
                    tokens = load(f)
                    self._tokens[client_id] = tokens
                    log.debug("Token aus Datei geladen fuer client_id=%s", client_id)
                    return tokens
            except Exception as e:
                log.warning("Token-Datei konnte nicht geladen werden: %s", e)
        return {}

    def _save_tokens(self, client_id: str, tokens: dict):
        self._tokens[client_id] = tokens
        try:
            DATA_PATH.mkdir(parents=True, exist_ok=True)
            token_file = self._get_token_file(client_id)
            with open(token_file, 'w', encoding='utf-8') as f:
                dump(tokens, f, indent=4)
            try:
                os.chmod(token_file, 0o600)
            except OSError as e:
                log.debug("Dateiberechtigungen konnten nicht gesetzt werden: %s", e)
            log.debug("Token gespeichert fuer client_id=%s", client_id)
        except Exception as e:
            log.error("Token-Datei konnte nicht gespeichert werden: %s", e)

    def _refresh_access_token(self, client_id: str, refresh_token: str) -> dict:
        session = get_http_session()
        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = session.post(TOKEN_URL, data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                }, timeout=10)
                break
            except HTTPError as e:
                status = e.response.status_code if e.response is not None else "unbekannt"
                raise CarDataError("AUTH", f"Token-Refresh fehlgeschlagen (HTTP {status})") from e
            except (ConnectionError, Timeout) as e:
                last_error = e
                log.warning("Token-Refresh fehlgeschlagen (Versuch %d/%d): %s",
                            attempt + 1, MAX_RETRIES + 1, e)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue
        else:
            raise CarDataError(
                "AUTH", f"Token-Refresh fehlgeschlagen nach {MAX_RETRIES + 1} Versuchen") from last_error
        try:
            token_data = response.json()
        except (JSONDecodeError, ValueError) as e:
            raise CarDataError("AUTH", "Token-Refresh: Unerwartetes Antwortformat") from e
        access_token = token_data.get("access_token")
        if not access_token:
            raise CarDataError("AUTH", "Token-Refresh: Kein access_token in Antwort erhalten")
        tokens = {
            "access_token": access_token,
            "refresh_token": token_data.get("refresh_token", refresh_token),
            "expires_at": time.time() + token_data.get("expires_in", DEFAULT_TOKEN_LIFETIME),
        }
        self._save_tokens(client_id, tokens)
        log.info("Access-Token erneuert fuer client_id=%s", client_id)
        return tokens

    def _get_valid_access_token(self, client_id: str, refresh_token: str,
                                token_expiry_buffer: int = TOKEN_EXPIRY_BUFFER) -> str:
        with self._lock:
            tokens = self._load_tokens(client_id)
            if (tokens.get("access_token")
                    and tokens.get("expires_at", 0) > time.time() + token_expiry_buffer):
                return tokens["access_token"]
            # Token abgelaufen oder nicht vorhanden, erneuern
            current_refresh = tokens.get("refresh_token", refresh_token)
            tokens = self._refresh_access_token(client_id, current_refresh)
            return tokens["access_token"]

    def _handle_api_error(self, response_json: dict):
        if "error" in response_json:
            error = response_json["error"]
            code = error.get("code", "UNKNOWN")
            message = ERROR_MESSAGES.get(code, error.get("message", "Unbekannter Fehler"))
            raise CarDataError(code, message)

    @staticmethod
    def _validate_vin(vin: str):
        if not VIN_PATTERN.match(vin.upper()):
            raise CarDataError(
                "CONFIG",
                f"VIN-Format ungueltig: '{vin}'. Erwartet werden 17 Zeichen (A-H, J-N, P-R, S-Z, 0-9)")

    @staticmethod
    def _is_retryable_error(e) -> bool:
        if isinstance(e, (ConnectionError, Timeout)):
            return True
        if isinstance(e, HTTPError) and e.response is not None:
            return e.response.status_code in RETRYABLE_STATUS_CODES
        return False

    def _track_request(self, client_id: str):
        today = datetime.now().strftime("%Y-%m-%d")
        counts = self._request_counts.get(client_id, {})
        if counts.get("date") != today:
            counts = {"count": 0, "date": today}
        counts["count"] += 1
        self._request_counts[client_id] = counts
        count = counts["count"]
        if count >= DAILY_REQUEST_LIMIT:
            log.error("BMW CarData API-Tageslimit erreicht: %d/%d Anfragen (client_id=%s)",
                      count, DAILY_REQUEST_LIMIT, client_id)
        elif count >= DAILY_REQUEST_WARNING:
            log.warning("BMW CarData API-Tageslimit fast erreicht: %d/%d Anfragen (client_id=%s)",
                        count, DAILY_REQUEST_LIMIT, client_id)
        else:
            log.debug("BMW CarData API-Anfrage %d/%d (client_id=%s)", count, DAILY_REQUEST_LIMIT, client_id)

    def fetch_soc(self, client_id: str, refresh_token: str, vin: str,
                  container_id: str, token_expiry_buffer: int = TOKEN_EXPIRY_BUFFER) -> CarState:
        if not all([client_id, refresh_token, vin, container_id]):
            raise CarDataError(
                "CONFIG",
                "Konfiguration unvollstaendig: client_id, refresh_token, vin und container_id muessen gesetzt sein")
        self._validate_vin(vin)
        log.debug("BMW CarData SoC-Abfrage fuer VIN=%s, container_id=%s", vin, container_id)
        self._track_request(client_id)
        access_token = self._get_valid_access_token(client_id, refresh_token, token_expiry_buffer)
        return self._request_telematic_data(access_token, client_id, refresh_token, vin, container_id)

    def _request_telematic_data(self, access_token: str, client_id: str, refresh_token: str,
                                vin: str, container_id: str) -> CarState:
        log.debug("Telematik-Anfrage: VIN=%s, container_id=%s", vin, container_id)
        session = get_http_session()
        url = f"{CARDATA_BASE_URL}/{vin}/telematicData"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "x-version": "v1",
        }
        params = {"containerId": container_id}
        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = session.get(url, headers=headers, params=params, timeout=10)
                break
            except HTTPError as e:
                if e.response is not None and e.response.status_code == 401:
                    log.info("Access-Token abgelaufen (client_id=%s), erneuere Token und versuche erneut",
                             client_id)
                    access_token = self._refresh_and_retry(client_id, refresh_token, access_token)
                    headers["Authorization"] = f"Bearer {access_token}"
                    response = session.get(url, headers=headers, params=params, timeout=10)
                    break
                elif self._is_retryable_error(e):
                    last_error = e
                    log.warning("Telematik-Anfrage fehlgeschlagen (Versuch %d/%d): %s",
                                attempt + 1, MAX_RETRIES + 1, e)
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY)
                    continue
                else:
                    raise
            except (ConnectionError, Timeout) as e:
                last_error = e
                log.warning("Telematik-Anfrage fehlgeschlagen (Versuch %d/%d): %s",
                            attempt + 1, MAX_RETRIES + 1, e)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue
        else:
            raise last_error
        log.debug("Telematik-Antwort erhalten fuer VIN=%s", vin)
        try:
            data = response.json()
        except (JSONDecodeError, ValueError) as e:
            raise CarDataError("PARSE", "Unerwartetes Antwortformat der Telematik-API") from e
        self._handle_api_error(data)
        return self._parse_telematic_data(data)

    def _refresh_and_retry(self, client_id: str, refresh_token: str, rejected_token: str) -> str:
        with self._lock:
            tokens = self._load_tokens(client_id)
            # Token wurde moeglicherweise bereits von einem anderen Thread erneuert
            if tokens.get("access_token") and tokens["access_token"] != rejected_token:
                return tokens["access_token"]
            current_refresh = tokens.get("refresh_token", refresh_token)
            tokens = self._refresh_access_token(client_id, current_refresh)
            return tokens["access_token"]

    def _parse_telematic_data(self, data: dict) -> CarState:
        telematic = data.get("telematicData", {})
        soc_key = "vehicle.drivetrain.batteryManagement.header"
        range_key = "vehicle.drivetrain.electricEngine.kombiRemainingElectricRange"
        soc_data = telematic.get(soc_key)
        if soc_data is None:
            raise CarDataError("CU-300", f"SoC-Daten nicht verfuegbar (Schluessel '{soc_key}' fehlt)")
        try:
            soc = float(soc_data["value"])
        except (KeyError, ValueError, TypeError) as e:
            raise CarDataError("PARSE", f"SoC-Wert ungueltig: {soc_data.get('value', 'fehlt')}") from e
        range_km = None
        range_data = telematic.get(range_key)
        if range_data is not None:
            try:
                range_km = float(range_data["value"])
            except (KeyError, ValueError, TypeError):
                log.warning("Reichweite konnte nicht geparst werden: %s", range_data.get("value"))
        soc_timestamp = self._parse_timestamp(soc_data.get("timestamp"))
        return CarState(soc=soc, range=range_km, soc_timestamp=soc_timestamp)

    @staticmethod
    def _parse_timestamp(timestamp_str):
        if not timestamp_str:
            return None
        # Z-Suffix durch +00:00 ersetzen fuer Python 3.9 Kompatibilitaet
        normalized = timestamp_str.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, AttributeError):
            pass
        # Fallback: haeufige Zeitstempelformate
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(timestamp_str, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
        log.warning("SoC-Zeitstempel konnte nicht geparst werden: %s", timestamp_str)
        return None
