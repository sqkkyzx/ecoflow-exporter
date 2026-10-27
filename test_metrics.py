import copy
import unittest

from prometheus_client.parser import text_string_to_metric_families

from metrics import render_metrics


def samples(snapshot):
    body = render_metrics(snapshot).decode()
    result = {}
    for family in text_string_to_metric_families(body):
        for sample in family.samples:
            result[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
    return body, result


def value(rows, name, **labels):
    return rows.get((name, tuple(sorted({"device": "ups", **labels}.items()))))


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.state = {"device": "ups", "connected": True, "subscribed": True, "login_ok": True,
                      "now": 1000, "stale_seconds": 120, "last_live_timestamp": 998,
                      "values": {}, "counters": {}}

    def add(self, name, number, timestamp=998):
        self.state["values"][name] = {"value": number, "timestamp": timestamp}

    def test_fresh_readings_have_units_and_latest_soc_source(self):
        self.add("cms_batt_soc", 79, 900)
        self.add("bms_heartbeat_display_soc", 79.27, 999)
        self.add("pow_get_ac_in", 103.2)
        self.add("pow_get_ac_out", -103.2)
        self.add("pow_in_sum_w", 105)
        self.add("pow_out_sum_w", -104)
        self.add("pow_get_pv", 0)
        self.add("plug_in_info_ac_charger_flag", 1)
        body, rows = samples(self.state)
        self.assertEqual(value(rows, "ecoflow_battery_soc_percent"), 79.27)
        self.assertEqual(value(rows, "ecoflow_ac_output_power_watts"), 103.2)
        self.assertEqual(value(rows, "ecoflow_total_output_power_watts"), 104)
        self.assertEqual(value(rows, "ecoflow_solar_input_power_watts"), 0)
        self.assertEqual(value(rows, "ecoflow_ac_connected"), 1)
        self.assertEqual(value(rows, "ecoflow_metric_age_seconds", metric="battery_soc_percent"), 1)
        self.assertEqual(value(rows, "ecoflow_metric_available", metric="ac_connected"), 1)
        self.assertEqual(value(rows, "ecoflow_telemetry_available"), 1)
        self.assertIn("# TYPE ecoflow_ac_input_power_watts gauge", body)
        self.assertIn("# HELP ecoflow_ac_input_power_watts", body)

    def test_individual_stale_value_disappears_even_when_other_fields_are_fresh(self):
        self.add("cms_batt_soc", 79, 999)
        self.add("plug_in_info_ac_charger_flag", 1, 800)
        _, rows = samples(self.state)
        self.assertIsNone(value(rows, "ecoflow_ac_connected"))
        self.assertEqual(value(rows, "ecoflow_metric_available", metric="ac_connected"), 0)
        self.assertEqual(value(rows, "ecoflow_metric_age_seconds", metric="ac_connected"), 200)
        self.assertEqual(value(rows, "ecoflow_battery_soc_percent"), 79)

    def test_no_values_are_exported_without_transport_and_subscription(self):
        self.add("cms_batt_soc", 79)
        for flag in ("connected", "subscribed"):
            state = copy.deepcopy(self.state)
            state[flag] = False
            _, rows = samples(state)
            self.assertIsNone(value(rows, "ecoflow_battery_soc_percent"))
            self.assertEqual(value(rows, "ecoflow_metric_available", metric="battery_soc_percent"), 0)
            self.assertEqual(value(rows, "ecoflow_telemetry_available"), 0)
            self.assertEqual(value(rows, "ecoflow_metric_age_seconds", metric="battery_soc_percent"), 2)

    def test_missing_fields_are_not_zero_but_explicit_false_and_zero_are_preserved(self):
        self.add("cms_batt_soc", 0)
        self.add("plug_in_info_ac_charger_flag", False)
        _, rows = samples(self.state)
        self.assertEqual(value(rows, "ecoflow_battery_soc_percent"), 0)
        self.assertEqual(value(rows, "ecoflow_ac_connected"), 0)
        self.assertIsNone(value(rows, "ecoflow_ac_input_power_watts"))
        self.assertEqual(value(rows, "ecoflow_metric_available", metric="ac_input_power_watts"), 0)

    def test_invalid_soc_and_future_values_do_not_hide_valid_older_candidates(self):
        self.add("cms_batt_soc", 101, 999)
        self.add("bms_batt_soc", 79, 998)
        self.add("pow_get_ac_in", 100, 1006)
        self.add("pow_get_pv", float("nan"))
        _, rows = samples(self.state)
        self.assertEqual(value(rows, "ecoflow_battery_soc_percent"), 79)
        self.assertIsNone(value(rows, "ecoflow_ac_input_power_watts"))
        self.assertIsNone(value(rows, "ecoflow_solar_input_power_watts"))

    def test_watts_and_secondary_flag_never_infer_ac_presence(self):
        self.add("pow_get_ac_in", 100)
        self.add("plug_in_info_ac_in_flag", 1)
        _, rows = samples(self.state)
        self.assertIsNone(value(rows, "ecoflow_ac_connected"))
        self.assertEqual(value(rows, "ecoflow_metric_available", metric="ac_connected"), 0)

    def test_runtime_is_intentionally_not_exposed_before_semantics_are_validated(self):
        self.add("cms_dsg_rem_time", 4039)
        body, _ = samples(self.state)
        self.assertNotIn("ecoflow_battery_runtime", body)

    def test_counters_have_counter_types_and_read_reasons_are_bounded(self):
        self.state["counters"] = {"messages": 10, "decoded_live": 8, "cached": 2,
                                  "decode_errors": 1, "reads_no_data": 3, "reads_stale": 4,
                                  "reconnects": 5, "auth_errors": 6, "read_errors": 7,
                                  "login_attempts": 8, "cert_refreshes": 9, "connection_attempts": 10}
        self.state["last_read_timestamp"] = 997
        self.state["last_read_reason"] = "stale_data"
        body, rows = samples(self.state)
        self.assertIn("# TYPE ecoflow_mqtt_messages_total counter", body)
        self.assertEqual(value(rows, "ecoflow_mqtt_messages_total"), 10)
        self.assertEqual(value(rows, "ecoflow_read_requests_total", reason="no_data"), 3)
        self.assertEqual(value(rows, "ecoflow_read_requests_total", reason="stale_data"), 4)
        self.assertEqual(value(rows, "ecoflow_last_read_timestamp_seconds"), 997)
        self.assertEqual(value(rows, "ecoflow_last_read_reason", reason="stale_data"), 1)
        self.assertEqual(value(rows, "ecoflow_read_errors_total"), 7)
        self.assertEqual(value(rows, "ecoflow_login_attempts_total"), 8)
        self.assertEqual(value(rows, "ecoflow_certificate_refreshes_total"), 9)
        self.assertEqual(value(rows, "ecoflow_mqtt_connection_attempts_total"), 10)

    def test_credentials_and_metadata_are_never_exposed(self):
        self.state.update({"phone": "PRIVATE_PHONE", "password": "PRIVATE_PASSWORD",
                           "sn": "PRIVATE_SN", "uid": "PRIVATE_UID", "token": "PRIVATE_TOKEN"})
        body = render_metrics(self.state, {"password": "PRIVATE_CONFIG_PASSWORD"}).decode()
        for secret in ("PRIVATE_PHONE", "PRIVATE_PASSWORD", "PRIVATE_SN", "PRIVATE_UID", "PRIVATE_TOKEN", "PRIVATE_CONFIG_PASSWORD"):
            self.assertNotIn(secret, body)
        for family in text_string_to_metric_families(body):
            for sample in family.samples:
                self.assertTrue(set(sample.labels) <= {"device", "metric", "reason"})

    def test_registry_is_isolated_between_scrapes(self):
        first = render_metrics(self.state)
        self.assertEqual(first, render_metrics(self.state))
        self.state["device"] = 'different"device'
        second = render_metrics(self.state).decode()
        self.assertNotIn('device="ups"', second)
        self.assertIn('device="different\\"device"', second)


if __name__ == "__main__":
    unittest.main()
