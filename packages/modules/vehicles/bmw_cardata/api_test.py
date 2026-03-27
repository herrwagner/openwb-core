import datetime as dt_module
import logging
import threading
import time
from unittest.mock import MagicMock

import pytest
import requests.exceptions
import requests_mock as rm

from modules.vehicles.bmw_cardata import api as api_module
from modules.vehicles.bmw_cardata.api import (
    Api, CarDataError, TOKEN_URL, CARDATA_BASE_URL,
    DAILY_REQUEST_LIMIT, DAILY_REQUEST_WARNING,
)

CLIENT_ID = "test-client-id"
REFRESH_TOKEN = "test-refresh-token"
VIN = "WBA12345678901234"
CONTAINER_ID = "test-container-id"

TOKEN_RESPONSE = {
    "access_token": "new-access-token",
    "refresh_token": "new-refresh-token",
    "expires_in": 3600,
}

TELEMATIC_RESPONSE = {
    "telematicData": {
        "vehicle.drivetrain.batteryManagement.header": {
            "value": "72",
            "unit": "PERCENT",
            "timestamp": "2024-01-15T10:30:00Z",
        },
        "vehicle.drivetrain.electricEngine.kombiRemainingElectricRange": {
            "value": "285.5",
            "unit": "KM",
            "timestamp": "2024-01-15T10:30:00Z",
        },
    }
}

TELEMATIC_RESPONSE_SOC_ONLY = {
    "telematicData": {
        "vehicle.drivetrain.batteryManagement.header": {
            "value": "45",
            "unit": "PERCENT",
            "timestamp": "2024-01-15T08:00:00Z",
        },
    }
}

TELEMATIC_RESPONSE_NO_SOC = {
    "telematicData": {}
}

API_ERROR_RATE_LIMIT = {
    "error": {
        "code": "CU-429",
        "message": "Rate limit exceeded",
    }
}

API_ERROR_TOKEN_EXPIRED = {
    "error": {
        "code": "CU-102",
        "message": "Token expired",
    }
}


@pytest.fixture(autouse=True)
def reset_api_singleton(monkeypatch, tmp_path):
    monkeypatch.setattr(api_module, "DATA_PATH", tmp_path)
    Api._instance = None
    Api._tokens = {}
    Api._request_counts = {}
    yield
    Api._instance = None
    Api._tokens = {}
    Api._request_counts = {}


def _setup_valid_token(api_instance):
    api_instance._tokens[CLIENT_ID] = {
        "access_token": "valid-token",
        "refresh_token": REFRESH_TOKEN,
        "expires_at": time.time() + 3600,
    }


class TestTokenRefresh:
    def test_refresh_token_on_expired(self, requests_mock: rm.Mocker):
        # setup: kein gespeicherter Token, muss refreshen
        requests_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        # execution
        result = Api().fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation
        assert requests_mock.call_count == 2
        token_request = requests_mock.request_history[0]
        assert token_request.path == "/gcdm/oauth/token"
        assert "grant_type=refresh_token" in token_request.text
        assert f"client_id={CLIENT_ID}" in token_request.text
        assert result.soc == 72.0
        assert result.range == 285.5

    def test_use_cached_token_if_valid(self, requests_mock: rm.Mocker):
        # setup: gueltiger Token im Cache
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        # execution
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation: kein Token-Refresh noetig
        assert requests_mock.call_count == 1
        assert result.soc == 72.0

    def test_refresh_when_token_about_to_expire(self, requests_mock: rm.Mocker):
        # setup: Token laeuft in 30 Sekunden ab (< 60s Puffer)
        api = Api()
        api._tokens[CLIENT_ID] = {
            "access_token": "expiring-token",
            "refresh_token": REFRESH_TOKEN,
            "expires_at": time.time() + 30,
        }
        requests_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        # execution
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation: Token wurde erneuert
        assert requests_mock.call_count == 2
        assert result.soc == 72.0

    def test_updated_refresh_token_is_stored(self, requests_mock: rm.Mocker):
        # setup
        requests_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        # execution
        api = Api()
        api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation: neuer Refresh-Token gespeichert
        assert api._tokens[CLIENT_ID]["refresh_token"] == "new-refresh-token"
        assert api._tokens[CLIENT_ID]["access_token"] == "new-access-token"

    def test_token_refresh_missing_access_token(self, requests_mock: rm.Mocker):
        # setup: Token-Endpunkt liefert keinen access_token
        requests_mock.post(TOKEN_URL, json={"refresh_token": "new-rt"})

        # execution & evaluation
        with pytest.raises(CarDataError, match="Kein access_token in Antwort"):
            Api().fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

    def test_token_refresh_malformed_json(self, requests_mock: rm.Mocker):
        # setup: Token-Endpunkt liefert ungueltige Antwort
        requests_mock.post(TOKEN_URL, text="not json at all")

        # execution & evaluation
        with pytest.raises(CarDataError, match="Unerwartetes Antwortformat"):
            Api().fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)


class TestTelematicDataParsing:
    def test_parse_soc_and_range(self, requests_mock: rm.Mocker):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        # execution
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation
        assert result.soc == 72.0
        assert result.range == 285.5
        assert result.soc_timestamp is not None

    def test_parse_soc_without_range(self, requests_mock: rm.Mocker):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE_SOC_ONLY)

        # execution
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation
        assert result.soc == 45.0
        assert result.range is None

    def test_missing_soc_key_raises_error(self, requests_mock: rm.Mocker):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE_NO_SOC)

        # execution & evaluation
        with pytest.raises(CarDataError, match="SoC-Daten nicht verfuegbar"):
            api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

    def test_non_numeric_soc_value_raises_error(self, requests_mock: rm.Mocker):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json={"telematicData": {
            "vehicle.drivetrain.batteryManagement.header": {
                "value": "N/A",
                "unit": "PERCENT",
            },
        }})

        # execution & evaluation
        with pytest.raises(CarDataError, match="SoC-Wert ungueltig"):
            api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

    def test_missing_value_key_in_soc_raises_error(self, requests_mock: rm.Mocker):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json={"telematicData": {
            "vehicle.drivetrain.batteryManagement.header": {
                "unit": "PERCENT",
            },
        }})

        # execution & evaluation
        with pytest.raises(CarDataError, match="SoC-Wert ungueltig"):
            api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

    def test_malformed_telematic_json_raises_error(self, requests_mock: rm.Mocker):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, text="this is not json")

        # execution & evaluation
        with pytest.raises(CarDataError, match="Unerwartetes Antwortformat"):
            api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

    def test_request_includes_correct_headers(self, requests_mock: rm.Mocker):
        # setup
        api = Api()
        api._tokens[CLIENT_ID] = {
            "access_token": "my-access-token",
            "refresh_token": REFRESH_TOKEN,
            "expires_at": time.time() + 3600,
        }
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        # execution
        api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation
        request = requests_mock.request_history[0]
        assert request.headers["Authorization"] == "Bearer my-access-token"
        assert request.headers["x-version"] == "v1"
        assert f"containerId={CONTAINER_ID}" in request.url


class TestErrorHandling:
    def test_rate_limit_error(self, requests_mock: rm.Mocker):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=API_ERROR_RATE_LIMIT)

        # execution & evaluation
        with pytest.raises(CarDataError) as exc_info:
            api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)
        assert exc_info.value.code == "CU-429"
        assert "Ratenlimit" in exc_info.value.message

    def test_api_error_token_expired(self, requests_mock: rm.Mocker):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=API_ERROR_TOKEN_EXPIRED)

        # execution & evaluation
        with pytest.raises(CarDataError) as exc_info:
            api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)
        assert exc_info.value.code == "CU-102"

    def test_http_error_on_token_refresh_raises_cardata_error(self, requests_mock: rm.Mocker):
        # setup: Token-Refresh schlaegt mit HTTP 400 fehl
        requests_mock.post(TOKEN_URL, status_code=400)

        # execution & evaluation
        with pytest.raises(CarDataError, match="Token-Refresh fehlgeschlagen"):
            Api().fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

    def test_401_triggers_token_refresh_and_retry(self, requests_mock: rm.Mocker):
        # setup: erster Request 401, dann Token-Refresh, dann erfolgreicher Request
        api = Api()
        api._tokens[CLIENT_ID] = {
            "access_token": "expired-token",
            "refresh_token": REFRESH_TOKEN,
            "expires_at": time.time() + 3600,  # Cache sagt gueltig, aber Server sagt 401
        }
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, [
            {"status_code": 401},
            {"json": TELEMATIC_RESPONSE},
        ])
        requests_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)

        # execution
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation: GET(401) + POST(token refresh) + GET(retry ok) = 3
        assert result.soc == 72.0
        assert requests_mock.call_count == 3
        # Token wurde erneuert
        assert api._tokens[CLIENT_ID]["access_token"] == "new-access-token"

    def test_missing_config_raises_error(self):
        # execution & evaluation: client_id fehlt
        with pytest.raises(CarDataError, match="Konfiguration unvollstaendig"):
            Api().fetch_soc(None, REFRESH_TOKEN, VIN, CONTAINER_ID)

    def test_empty_config_raises_error(self):
        # execution & evaluation: leere Strings
        with pytest.raises(CarDataError, match="Konfiguration unvollstaendig"):
            Api().fetch_soc("", REFRESH_TOKEN, VIN, CONTAINER_ID)


class TestTimestampParsing:
    def test_parse_utc_z_suffix(self):
        result = Api._parse_timestamp("2024-01-15T10:30:00Z")
        assert result is not None

    def test_parse_utc_offset(self):
        result = Api._parse_timestamp("2024-01-15T10:30:00+00:00")
        assert result is not None

    def test_parse_positive_offset(self):
        result = Api._parse_timestamp("2024-01-15T10:30:00+01:00")
        assert result is not None

    def test_parse_no_timezone(self):
        # Ohne Zeitzone wird UTC angenommen
        result = Api._parse_timestamp("2024-01-15T10:30:00")
        assert result is not None

    def test_parse_empty_returns_none(self):
        assert Api._parse_timestamp("") is None
        assert Api._parse_timestamp(None) is None

    def test_parse_invalid_returns_none(self):
        assert Api._parse_timestamp("not-a-timestamp") is None


# --- Enhancement 4: Configurable Token Expiry Buffer ---

class TestTokenExpiryBuffer:
    def test_token_refreshed_when_within_custom_buffer(self, requests_mock: rm.Mocker):
        # setup: Token laeuft in 90s ab, buffer=120 → muss refreshen
        api = Api()
        api._tokens[CLIENT_ID] = {
            "access_token": "almost-expired",
            "refresh_token": REFRESH_TOKEN,
            "expires_at": time.time() + 90,
        }
        requests_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        # execution
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID, token_expiry_buffer=120)

        # evaluation: Token-Refresh + Telematik = 2 Aufrufe
        assert requests_mock.call_count == 2
        assert result.soc == 72.0

    def test_token_not_refreshed_with_default_buffer(self, requests_mock: rm.Mocker):
        # setup: Token laeuft in 90s ab, default buffer=60 → kein Refresh noetig
        api = Api()
        api._tokens[CLIENT_ID] = {
            "access_token": "still-valid",
            "refresh_token": REFRESH_TOKEN,
            "expires_at": time.time() + 90,
        }
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        # execution
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation: nur Telematik, kein Token-Refresh
        assert requests_mock.call_count == 1
        assert result.soc == 72.0


# --- Enhancement 5: Request Logging ---

class TestRequestLogging:
    def test_fetch_soc_logs_vin_and_container_id(self, requests_mock: rm.Mocker, caplog):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        # execution
        with caplog.at_level(logging.DEBUG):
            api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation: VIN und container_id in Log-Eintraegen
        assert any(VIN in r.message and CONTAINER_ID in r.message for r in caplog.records)

    def test_telematic_request_logs_vin(self, requests_mock: rm.Mocker, caplog):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        # execution
        with caplog.at_level(logging.DEBUG):
            api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation: Telematik-Anfrage und -Antwort loggen VIN
        assert any("Telematik-Anfrage" in r.message and VIN in r.message for r in caplog.records)
        assert any("Telematik-Antwort" in r.message and VIN in r.message for r in caplog.records)


# --- Enhancement 2: VIN Format Validation ---

class TestVinValidation:
    def test_vin_too_short(self):
        with pytest.raises(CarDataError, match="VIN-Format ungueltig"):
            Api().fetch_soc(CLIENT_ID, REFRESH_TOKEN, "WBA1234", CONTAINER_ID)

    def test_vin_too_long(self):
        with pytest.raises(CarDataError, match="VIN-Format ungueltig"):
            Api().fetch_soc(CLIENT_ID, REFRESH_TOKEN, "WBA123456789012345", CONTAINER_ID)

    def test_vin_with_invalid_chars(self):
        with pytest.raises(CarDataError, match="VIN-Format ungueltig"):
            Api().fetch_soc(CLIENT_ID, REFRESH_TOKEN, "WBA1234567890123!", CONTAINER_ID)

    def test_vin_with_excluded_letters(self):
        # I, O, Q sind laut ISO 3779 in VINs nicht erlaubt
        with pytest.raises(CarDataError, match="VIN-Format ungueltig"):
            Api().fetch_soc(CLIENT_ID, REFRESH_TOKEN, "WBI12345678901234", CONTAINER_ID)
        with pytest.raises(CarDataError, match="VIN-Format ungueltig"):
            Api().fetch_soc(CLIENT_ID, REFRESH_TOKEN, "WBO12345678901234", CONTAINER_ID)
        with pytest.raises(CarDataError, match="VIN-Format ungueltig"):
            Api().fetch_soc(CLIENT_ID, REFRESH_TOKEN, "WBQ12345678901234", CONTAINER_ID)

    def test_valid_vin_accepted(self, requests_mock: rm.Mocker):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        # execution & evaluation: gueltiger VIN wird akzeptiert
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)
        assert result.soc == 72.0


# --- Enhancement 1: Retry Logic ---

class TestRetryLogic:
    def test_retry_on_500_then_success(self, requests_mock: rm.Mocker, monkeypatch):
        monkeypatch.setattr(api_module, "RETRY_DELAY", 0)
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, [
            {"status_code": 500},
            {"json": TELEMATIC_RESPONSE},
        ])

        # execution
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation
        assert result.soc == 72.0
        assert requests_mock.call_count == 2

    def test_retry_on_502_then_success(self, requests_mock: rm.Mocker, monkeypatch):
        monkeypatch.setattr(api_module, "RETRY_DELAY", 0)
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, [
            {"status_code": 502},
            {"json": TELEMATIC_RESPONSE},
        ])

        # execution
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation
        assert result.soc == 72.0
        assert requests_mock.call_count == 2

    def test_retry_on_connection_error_then_success(self, requests_mock: rm.Mocker, monkeypatch):
        monkeypatch.setattr(api_module, "RETRY_DELAY", 0)
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, [
            {"exc": requests.exceptions.ConnectionError("Connection refused")},
            {"json": TELEMATIC_RESPONSE},
        ])

        # execution
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation
        assert result.soc == 72.0
        assert requests_mock.call_count == 2

    def test_retry_on_timeout_then_success(self, requests_mock: rm.Mocker, monkeypatch):
        monkeypatch.setattr(api_module, "RETRY_DELAY", 0)
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, [
            {"exc": requests.exceptions.Timeout("Request timed out")},
            {"json": TELEMATIC_RESPONSE},
        ])

        # execution
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation
        assert result.soc == 72.0
        assert requests_mock.call_count == 2

    def test_retry_exhaustion(self, requests_mock: rm.Mocker, monkeypatch):
        monkeypatch.setattr(api_module, "RETRY_DELAY", 0)
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        # MAX_RETRIES + 1 = 4 Versuche, alle mit 500
        requests_mock.get(telematic_url, [
            {"status_code": 500},
            {"status_code": 500},
            {"status_code": 500},
            {"status_code": 500},
        ])

        # execution & evaluation
        with pytest.raises(requests.exceptions.HTTPError):
            api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)
        assert requests_mock.call_count == 4

    def test_404_not_retried(self, requests_mock: rm.Mocker, monkeypatch):
        monkeypatch.setattr(api_module, "RETRY_DELAY", 0)
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, status_code=404)

        # execution & evaluation: 404 wird nicht wiederholt
        with pytest.raises(requests.exceptions.HTTPError):
            api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)
        assert requests_mock.call_count == 1

    def test_401_uses_token_refresh_not_retry(self, requests_mock: rm.Mocker, monkeypatch):
        monkeypatch.setattr(api_module, "RETRY_DELAY", 0)
        api = Api()
        api._tokens[CLIENT_ID] = {
            "access_token": "expired-token",
            "refresh_token": REFRESH_TOKEN,
            "expires_at": time.time() + 3600,
        }
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, [
            {"status_code": 401},
            {"json": TELEMATIC_RESPONSE},
        ])
        requests_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)

        # execution
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation: 401 → Token-Refresh, kein Retry-Loop
        assert result.soc == 72.0
        assert requests_mock.call_count == 3  # GET(401) + POST(refresh) + GET(ok)

    def test_token_refresh_connection_error_then_success(self, requests_mock: rm.Mocker, monkeypatch):
        monkeypatch.setattr(api_module, "RETRY_DELAY", 0)
        requests_mock.post(TOKEN_URL, [
            {"exc": requests.exceptions.ConnectionError("Connection refused")},
            {"json": TOKEN_RESPONSE},
        ])
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        # execution
        result = Api().fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation: POST(fail) + POST(ok) + GET(ok) = 3
        assert result.soc == 72.0
        assert requests_mock.call_count == 3


# --- Enhancement 6: Rate Limit Tracking ---

class TestRateLimitTracking:
    @pytest.fixture(autouse=True)
    def mock_api_datetime(self, monkeypatch):
        self._mock_dt = MagicMock(wraps=dt_module.datetime)
        self._mock_dt.now.return_value = dt_module.datetime(2022, 5, 16, 10, 0, 0)
        monkeypatch.setattr(api_module, "datetime", self._mock_dt)

    def test_counter_increments_on_each_call(self, requests_mock: rm.Mocker):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        # execution
        api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)
        api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation
        assert Api._request_counts[CLIENT_ID]["count"] == 2

    def test_counter_resets_on_new_day(self, requests_mock: rm.Mocker):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        # execution: Tag 1
        api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)
        assert Api._request_counts[CLIENT_ID]["count"] == 1

        # Tag 2
        self._mock_dt.now.return_value = dt_module.datetime(2022, 5, 17, 10, 0, 0)
        api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation: Zaehler zurueckgesetzt
        assert Api._request_counts[CLIENT_ID]["count"] == 1
        assert Api._request_counts[CLIENT_ID]["date"] == "2022-05-17"

    def test_warning_logged_at_threshold(self, requests_mock: rm.Mocker, caplog):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)
        Api._request_counts[CLIENT_ID] = {"count": DAILY_REQUEST_WARNING - 1, "date": "2022-05-16"}

        # execution
        with caplog.at_level(logging.WARNING):
            api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation
        assert any("fast erreicht" in r.message for r in caplog.records)

    def test_error_logged_at_limit(self, requests_mock: rm.Mocker, caplog):
        # setup
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)
        Api._request_counts[CLIENT_ID] = {"count": DAILY_REQUEST_LIMIT - 1, "date": "2022-05-16"}

        # execution
        with caplog.at_level(logging.ERROR):
            api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation
        assert any("Tageslimit erreicht" in r.message for r in caplog.records)

    def test_request_proceeds_at_limit(self, requests_mock: rm.Mocker):
        # setup: Tageslimit erreicht, Anfrage soll trotzdem durchgehen
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)
        Api._request_counts[CLIENT_ID] = {"count": DAILY_REQUEST_LIMIT - 1, "date": "2022-05-16"}

        # execution
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation: Anfrage wird nicht blockiert
        assert result.soc == 72.0

    def test_retries_dont_double_count(self, requests_mock: rm.Mocker, monkeypatch):
        # setup: 500 dann Erfolg, Zaehler soll nur einmal inkrementieren
        monkeypatch.setattr(api_module, "RETRY_DELAY", 0)
        api = Api()
        _setup_valid_token(api)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, [
            {"status_code": 500},
            {"json": TELEMATIC_RESPONSE},
        ])

        # execution
        result = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)

        # evaluation: nur 1 Zaehlung trotz Retry
        assert result.soc == 72.0
        assert Api._request_counts[CLIENT_ID]["count"] == 1


# --- Enhancement 3: Concurrent Access Threading Tests ---

class TestConcurrentAccess:
    def test_concurrent_fetch_no_double_refresh(self, requests_mock: rm.Mocker, monkeypatch):
        # setup: kein gespeicherter Token, zwei Threads rufen gleichzeitig fetch_soc auf
        monkeypatch.setattr(api_module, "RETRY_DELAY", 0)
        requests_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        results = [None, None]
        errors = [None, None]
        barrier = threading.Barrier(2)

        def worker(i):
            barrier.wait()
            try:
                results[i] = Api().fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)
            except Exception as e:
                errors[i] = e

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # evaluation
        assert all(e is None for e in errors), f"Thread-Fehler: {errors}"
        assert all(r is not None and r.soc == 72.0 for r in results)
        # Token-Refresh soll nur einmal erfolgen
        token_requests = [h for h in requests_mock.request_history if h.path == "/gcdm/oauth/token"]
        assert len(token_requests) == 1

    def test_concurrent_401_retry_no_double_refresh(self, requests_mock: rm.Mocker, monkeypatch):
        # setup: beide Threads bekommen 401 beim ersten GET
        monkeypatch.setattr(api_module, "RETRY_DELAY", 0)
        api = Api()
        api._tokens[CLIENT_ID] = {
            "access_token": "expired-token",
            "refresh_token": REFRESH_TOKEN,
            "expires_at": time.time() + 3600,
        }
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"

        def telematic_callback(request, context):
            auth = request.headers.get("Authorization", "")
            if "expired-token" in auth:
                context.status_code = 401
                return {}
            return TELEMATIC_RESPONSE

        requests_mock.get(telematic_url, json=telematic_callback)
        requests_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)

        results = [None, None]
        errors = [None, None]
        barrier = threading.Barrier(2)

        def worker(i):
            barrier.wait()
            try:
                results[i] = api.fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)
            except Exception as e:
                errors[i] = e

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # evaluation
        assert all(e is None for e in errors), f"Thread-Fehler: {errors}"
        assert all(r is not None and r.soc == 72.0 for r in results)
        # Token-Refresh soll nur einmal erfolgen (zweiter Thread findet neuen Token)
        token_requests = [h for h in requests_mock.request_history if h.path == "/gcdm/oauth/token"]
        assert len(token_requests) == 1

    def test_concurrent_access_token_cache_consistency(self, requests_mock: rm.Mocker, monkeypatch):
        # setup: 5 Threads, abgelaufener Token, alle muessen Token erneuern
        monkeypatch.setattr(api_module, "RETRY_DELAY", 0)
        N = 5
        requests_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
        telematic_url = f"{CARDATA_BASE_URL}/{VIN}/telematicData"
        requests_mock.get(telematic_url, json=TELEMATIC_RESPONSE)

        results = [None] * N
        errors = [None] * N
        barrier = threading.Barrier(N)

        def worker(i):
            barrier.wait()
            try:
                results[i] = Api().fetch_soc(CLIENT_ID, REFRESH_TOKEN, VIN, CONTAINER_ID)
            except Exception as e:
                errors[i] = e

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # evaluation
        assert all(e is None for e in errors), f"Thread-Fehler: {errors}"
        assert all(r is not None and r.soc == 72.0 for r in results)
        # Genau 1 Token-Refresh
        token_requests = [h for h in requests_mock.request_history if h.path == "/gcdm/oauth/token"]
        assert len(token_requests) == 1
        # Konsistenter Cache-Zustand
        assert Api()._tokens[CLIENT_ID]["access_token"] == "new-access-token"
