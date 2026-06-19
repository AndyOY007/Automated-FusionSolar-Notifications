"""
Offline logic test using mocked HTTP responses -- validates token caching,
the 305 re-login retry path, and plant-list pagination, without needing
network access to the real FusionSolar domain (which this sandbox can't
reach anyway).

Run: python3 mock_logic_test.py
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from client import FusionSolarNBIClient, FusionSolarAPIError


def fake_response(json_body, headers=None, cookies=None, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.headers = headers or {}
    resp.cookies = cookies or {}
    resp.raw = MagicMock()
    resp.raw.headers.get_all.return_value = None
    return resp


def test_login_extracts_token_from_header_and_caches():
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "token.json"
        client = FusionSolarNBIClient(
            base_url="https://sg5.fusionsolar.huawei.com",
            username="api_user",
            system_code="secret",
            token_cache_path=str(cache_path),
        )

        login_resp = fake_response(
            {"success": True, "failCode": 0, "data": None, "message": "ok"},
            headers={"xsrf-token": "tok-abc123"},
        )

        with patch.object(client.session, "post", return_value=login_resp) as mock_post:
            token = client.login()
            assert token == "tok-abc123", token
            assert mock_post.call_count == 1

        assert cache_path.exists()
        cached = json.loads(cache_path.read_text())
        assert cached["token"] == "tok-abc123"
        print("PASS: login extracts token from header and persists cache")


def test_reuses_cached_token_without_relogin():
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "token.json"
        cache_path.write_text(json.dumps({"token": "cached-tok", "obtained_at": __import__("time").time()}))

        client = FusionSolarNBIClient(
            base_url="https://sg5.fusionsolar.huawei.com",
            username="api_user",
            system_code="secret",
            token_cache_path=str(cache_path),
        )
        assert client.token == "cached-tok"

        with patch.object(client.session, "post") as mock_post:
            token = client.login()  # should NOT call the network
            assert token == "cached-tok"
            mock_post.assert_not_called()
        print("PASS: reuses cached token without re-login")


def test_305_triggers_single_relogin_and_retry():
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "token.json"
        client = FusionSolarNBIClient(
            base_url="https://sg5.fusionsolar.huawei.com",
            username="api_user",
            system_code="secret",
            token_cache_path=str(cache_path),
        )
        client.token = "stale-token"
        client.token_obtained_at = __import__("time").time()

        stations_fail = fake_response({"success": False, "failCode": 305, "message": "not online"})
        login_ok = fake_response(
            {"success": True, "failCode": 0, "data": None, "message": "ok"},
            headers={"xsrf-token": "fresh-token"},
        )
        stations_ok = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": {
                    "list": [{"plantCode": "NE=1", "plantName": "Test Plant", "capacity": 10.0}],
                    "pageCount": 1,
                    "pageNo": 1,
                    "pageSize": 100,
                    "total": 1,
                },
                "message": "get plant list success",
            }
        )

        # First call fails with 305, then login succeeds, then retry succeeds
        with patch.object(client.session, "post", side_effect=[stations_fail, login_ok, stations_ok]) as mock_post:
            body = client.get_plant_list(page_no=1)
            assert body["data"]["list"][0]["plantCode"] == "NE=1"
            assert mock_post.call_count == 3
        assert client.token == "fresh-token"
        print("PASS: 305 triggers exactly one re-login + retry, and succeeds")


def test_get_all_plants_pagination():
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "token.json"
        client = FusionSolarNBIClient(
            base_url="https://sg5.fusionsolar.huawei.com",
            username="api_user",
            system_code="secret",
            token_cache_path=str(cache_path),
        )
        client.token = "tok"
        client.token_obtained_at = __import__("time").time()

        page1 = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": {
                    "list": [{"plantCode": "NE=1", "plantName": "A"}],
                    "pageCount": 2,
                    "pageNo": 1,
                    "pageSize": 1,
                    "total": 2,
                },
            }
        )
        page2 = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": {
                    "list": [{"plantCode": "NE=2", "plantName": "B"}],
                    "pageCount": 2,
                    "pageNo": 2,
                    "pageSize": 1,
                    "total": 2,
                },
            }
        )

        with patch.object(client.session, "post", side_effect=[page1, page2]) as mock_post:
            all_plants = client.get_all_plants()
            assert [p["plantCode"] for p in all_plants] == ["NE=1", "NE=2"]
            assert mock_post.call_count == 2
        print("PASS: get_all_plants pages through all results")


def test_unrecoverable_error_raises():
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "token.json"
        client = FusionSolarNBIClient(
            base_url="https://sg5.fusionsolar.huawei.com",
            username="api_user",
            system_code="secret",
            token_cache_path=str(cache_path),
        )
        client.token = "tok"
        client.token_obtained_at = __import__("time").time()

        bad_resp = fake_response({"success": False, "failCode": 20400, "message": "user.login.user_or_value_invalid"})
        with patch.object(client.session, "post", return_value=bad_resp):
            try:
                client.get_plant_list()
                assert False, "expected FusionSolarAPIError"
            except FusionSolarAPIError as exc:
                assert exc.fail_code == 20400
        print("PASS: unrecoverable error codes raise FusionSolarAPIError")


def test_plant_snapshot_combines_inverter_and_power_sensor():
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "token.json"
        client = FusionSolarNBIClient(
            base_url="https://sg5.fusionsolar.huawei.com",
            username="api_user",
            system_code="secret",
            token_cache_path=str(cache_path),
        )
        client.token = "tok"
        client.token_obtained_at = __import__("time").time()

        device_list_resp = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": [
                    {"id": 1, "devName": "Inverter-1", "devTypeId": 1, "stationCode": "NE=1"},
                    {"id": 2, "devName": "Power Sensor", "devTypeId": 47, "stationCode": "NE=1"},
                    {"id": 3, "devName": "Dongle-1", "devTypeId": 62, "stationCode": "NE=1"},
                ],
            }
        )
        inverter_kpi_resp = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": [
                    {
                        "devId": 1,
                        "dataItemMap": {"active_power": 12.5, "run_state": 1, "inverter_state": 512},
                    }
                ],
            }
        )
        sensor_kpi_resp = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": [{"devId": 2, "dataItemMap": {"active_power": 3000, "meter_status": 1}}],
            }
        )
        plant_health_resp = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": [{"stationCode": "NE=1", "dataItemMap": {"real_health_state": 3}}],
            }
        )
        alarms_resp = fake_response({"success": True, "failCode": 0, "data": []})

        # Order matters: get_device_list, then getDevRealKpi per type
        # (dict iteration order = insertion order = 1, 47, 62), then plant
        # real kpi, then alarms.
        with patch.object(
            client.session,
            "post",
            side_effect=[device_list_resp, inverter_kpi_resp, sensor_kpi_resp, plant_health_resp, alarms_resp],
        ):
            snapshot = client.get_plant_snapshot("NE=1")

        assert snapshot["pv_power_kw"] == 12.5
        assert snapshot["grid_power_kw"] == 3.0  # 3000 W -> 3.0 kW
        assert snapshot["load_estimate_kw"] == 9.5  # 12.5 - 3.0 - 0 (positive grid = exporting)
        assert snapshot["plant_health"] == "Healthy"
        online_map = {d["devName"]: d.get("online") for d in snapshot["devices"] if d.get("online") is not None}
        assert online_map["Inverter-1"] is True
        assert online_map["Power Sensor"] is True
        unmonitored = [d for d in snapshot["devices"] if d.get("status") == "n/a"]
        assert len(unmonitored) == 1 and unmonitored[0]["devName"] == "Dongle-1"
        print("PASS: get_plant_snapshot combines inverter + power sensor + skips unmonitored device types")


def test_active_alarms_filters_to_active_status():
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "token.json"
        client = FusionSolarNBIClient(
            base_url="https://sg5.fusionsolar.huawei.com",
            username="api_user",
            system_code="secret",
            token_cache_path=str(cache_path),
        )
        client.token = "tok"
        client.token_obtained_at = __import__("time").time()

        alarms_resp = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": [
                    {"alarmName": "Active fault", "status": 1, "lev": 2, "devName": "Inverter-1", "raiseTime": 1700000000000},
                ],
            }
        )
        with patch.object(client.session, "post", return_value=alarms_resp) as mock_post:
            resp = client.get_active_alarms(station_codes=["NE=1"], begin_time_ms=0, end_time_ms=1)
            assert resp["data"][0]["alarmName"] == "Active fault"
            # confirm payload sent matches the documented contract
            sent_payload = mock_post.call_args.kwargs["json"]
            assert sent_payload["stationCodes"] == "NE=1"
            assert sent_payload["beginTime"] == 0 and sent_payload["endTime"] == 1
            assert sent_payload["language"] == "en_US"
        print("PASS: get_active_alarms builds correct payload and returns alarm data")


def test_fleet_snapshot_batches_across_plants_in_fixed_call_count():
    """The whole point of get_fleet_snapshot: 2 plants should take the same
    ~5 calls as 50 plants would, NOT 2x or 50x the per-plant call count."""
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "token.json"
        client = FusionSolarNBIClient(
            base_url="https://sg5.fusionsolar.huawei.com",
            username="api_user",
            system_code="secret",
            token_cache_path=str(cache_path),
        )
        client.token = "tok"
        client.token_obtained_at = __import__("time").time()

        device_list_resp = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": [
                    {"id": 1, "devName": "Inv-A", "devTypeId": 1, "stationCode": "NE=1"},
                    {"id": 2, "devName": "Sensor-A", "devTypeId": 47, "stationCode": "NE=1"},
                    {"id": 3, "devName": "Inv-B", "devTypeId": 1, "stationCode": "NE=2"},
                    {"id": 4, "devName": "Sensor-B", "devTypeId": 47, "stationCode": "NE=2"},
                ],
            }
        )
        inverter_kpi_resp = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": [
                    {"devId": 1, "dataItemMap": {"active_power": 5.0, "run_state": 1, "inverter_state": 512}},
                    {"devId": 3, "dataItemMap": {"active_power": 7.0, "run_state": 1, "inverter_state": 512}},
                ],
            }
        )
        sensor_kpi_resp = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": [
                    {"devId": 2, "dataItemMap": {"active_power": -500, "meter_status": 1}},
                    {"devId": 4, "dataItemMap": {"active_power": 1000, "meter_status": 1}},
                ],
            }
        )
        plant_health_resp = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": [
                    {"stationCode": "NE=1", "dataItemMap": {"real_health_state": 3}},
                    {"stationCode": "NE=2", "dataItemMap": {"real_health_state": 3}},
                ],
            }
        )
        alarms_resp = fake_response({"success": True, "failCode": 0, "data": []})

        with patch.object(
            client.session,
            "post",
            side_effect=[device_list_resp, inverter_kpi_resp, sensor_kpi_resp, plant_health_resp, alarms_resp],
        ) as mock_post:
            snapshots = client.get_fleet_snapshot(["NE=1", "NE=2"])
            # Exactly 5 HTTP calls for 2 plants -- this is the whole point.
            assert mock_post.call_count == 5, mock_post.call_count

        assert snapshots["NE=1"]["pv_power_kw"] == 5.0
        assert snapshots["NE=1"]["grid_power_kw"] == -0.5
        assert snapshots["NE=1"]["load_estimate_kw"] == 5.5  # 5.0 - (-0.5): negative grid = importing

        assert snapshots["NE=2"]["pv_power_kw"] == 7.0
        assert snapshots["NE=2"]["grid_power_kw"] == 1.0
        assert snapshots["NE=2"]["load_estimate_kw"] == 6.0  # 7.0 - 1.0: positive grid = exporting
        print("PASS: get_fleet_snapshot batches 2 plants into 5 fixed HTTP calls")


def test_post_actually_retries_on_407_not_just_sleeps():
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "token.json"
        client = FusionSolarNBIClient(
            base_url="https://sg5.fusionsolar.huawei.com",
            username="api_user",
            system_code="secret",
            token_cache_path=str(cache_path),
        )
        client.token = "tok"
        client.token_obtained_at = __import__("time").time()

        throttled = fake_response({"success": False, "failCode": 407, "message": None})
        ok = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": {"list": [], "pageCount": 1, "pageNo": 1, "pageSize": 100, "total": 0},
            }
        )
        with patch.object(client.session, "post", side_effect=[throttled, ok]) as mock_post, patch(
            "client.time.sleep"
        ) as mock_sleep:
            body = client.get_plant_list(page_no=1)
            assert body["success"] is True
            assert mock_post.call_count == 2  # actually retried, not just raised after sleeping
            mock_sleep.assert_called_once()
        print("PASS: _post actually retries on 407 instead of sleeping then raising anyway")


def test_null_status_fields_dont_crash():
    """Regression test for the exact bug hit in production: an offline
    device returns inverter_state/run_state keys present but set to null,
    not omitted. int(None) used to blow up the whole fleet snapshot."""
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "token.json"
        client = FusionSolarNBIClient(
            base_url="https://sg5.fusionsolar.huawei.com",
            username="api_user",
            system_code="secret",
            token_cache_path=str(cache_path),
        )
        client.token = "tok"
        client.token_obtained_at = __import__("time").time()

        device_list_resp = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": [{"id": 1, "devName": "Offline-Inv", "devTypeId": 1, "stationCode": "NE=1"}],
            }
        )
        # The exact shape that broke things: keys present, values null.
        inverter_kpi_resp = fake_response(
            {
                "success": True,
                "failCode": 0,
                "data": [
                    {
                        "devId": 1,
                        "dataItemMap": {"active_power": None, "run_state": None, "inverter_state": None},
                    }
                ],
            }
        )
        plant_health_resp = fake_response(
            {"success": True, "failCode": 0, "data": [{"stationCode": "NE=1", "dataItemMap": {"real_health_state": 1}}]}
        )
        alarms_resp = fake_response({"success": True, "failCode": 0, "data": []})

        with patch.object(
            client.session,
            "post",
            side_effect=[device_list_resp, inverter_kpi_resp, plant_health_resp, alarms_resp],
        ):
            snapshots = client.get_fleet_snapshot(["NE=1"])  # must not raise

        dev = snapshots["NE=1"]["devices"][0]
        assert dev["power_kw"] is None
        assert dev["online"] is None
        assert dev["inverter_state"] is None
        assert snapshots["NE=1"]["pv_power_kw"] is None  # no usable power reading
        print("PASS: null status fields (offline device) handled without crashing")


if __name__ == "__main__":
    test_login_extracts_token_from_header_and_caches()
    test_reuses_cached_token_without_relogin()
    test_305_triggers_single_relogin_and_retry()
    test_get_all_plants_pagination()
    test_unrecoverable_error_raises()
    test_plant_snapshot_combines_inverter_and_power_sensor()
    test_active_alarms_filters_to_active_status()
    test_fleet_snapshot_batches_across_plants_in_fixed_call_count()
    test_post_actually_retries_on_407_not_just_sleeps()
    test_null_status_fields_dont_crash()
    print("\nAll offline logic tests passed.")
