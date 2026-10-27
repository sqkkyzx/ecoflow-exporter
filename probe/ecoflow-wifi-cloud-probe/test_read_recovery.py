import unittest

from probe import Telemetry
from read_recovery import ReadRecovery, missing_or_stale


class ReadRecoveryTests(unittest.TestCase):
    def test_empty_connected_state_reads_immediately_then_waits_for_response(self):
        snapshot = Telemetry().snapshot(0, True, 120)
        reader = ReadRecovery(30)
        self.assertEqual(reader.request_reason(0, snapshot), "no_data")
        for now in (0.2, 10, 29.9):
            self.assertIsNone(reader.request_reason(now, snapshot))
        self.assertEqual(reader.request_reason(30, snapshot), "no_data")

    def test_healthy_updates_never_trigger_periodic_requests(self):
        reader, state = ReadRecovery(30), Telemetry()
        for now in (0, 60, 120, 180, 600):
            state.update({"cms_batt_soc": 79, "pow_get_ac_in": 103,
                          "pow_get_ac_out": -103, "plug_in_info_ac_charger_flag": 1}, now)
            self.assertIsNone(reader.request_reason(now, state.snapshot(now, True, 120)))
        self.assertIsNone(reader.last_attempt)

    def test_new_soc_does_not_hide_stale_ac_flag(self):
        state = Telemetry()
        state.update({"cms_batt_soc": 79, "plug_in_info_ac_charger_flag": 1}, 0)
        state.update({"bms_heartbeat_display_soc": 79.2}, 125)
        self.assertEqual(missing_or_stale(state.snapshot(126, True, 120)), "stale_data")
        state.update({"plug_in_info_ac_charger_flag": 0}, 127)
        self.assertIsNone(missing_or_stale(state.snapshot(128, True, 120)))

    def test_new_soc_alias_keeps_its_group_fresh(self):
        state = Telemetry()
        state.update({"cms_batt_soc": 79}, 0)
        state.update({"bms_heartbeat_soc": 79}, 150)
        self.assertIsNone(missing_or_stale(state.snapshot(151, True, 120)))

    def test_unreported_optional_metrics_do_not_trigger(self):
        state = Telemetry()
        state.update({"cms_batt_soc": 79, "plug_in_info_ac_charger_flag": 1}, 0)
        self.assertIsNone(missing_or_stale(state.snapshot(1, True, 120)))

    def test_cache_alone_does_not_satisfy_live_data_and_offline_never_requests(self):
        state = Telemetry()  # Cached replies never enter this store.
        self.assertEqual(missing_or_stale(state.snapshot(0, True, 120)), "no_data")
        self.assertIsNone(ReadRecovery().request_reason(0, state.snapshot(0, False, 120)))

    def test_stale_request_stops_after_fresh_data_recovers(self):
        reader, state = ReadRecovery(30), Telemetry()
        state.update({"pow_get_ac_in": 100}, 0)
        self.assertEqual(reader.request_reason(121, state.snapshot(121, True, 120)), "stale_data")
        self.assertIsNone(reader.request_reason(121.2, state.snapshot(121.2, True, 120)))
        state.update({"pow_get_ac_in": 101}, 123)
        self.assertIsNone(reader.request_reason(151, state.snapshot(151, True, 120)))


if __name__ == "__main__":
    unittest.main()
