import unittest

from console_view import format_event


class ConsoleViewTests(unittest.TestCase):
    def snapshot(self, fields=None, **extra):
        data = {"broker_connected": True, "last_telemetry_receive_age_seconds": 2,
                "fields": fields or {}}
        data.update(extra)
        return format_event("snapshot", data)

    def field(self, value, age=2, stale=False, **extra):
        return {"value": value, "age_seconds": age, "stale": stale, **extra}

    def test_connection_lifecycle_uses_chinese_and_safe_codes(self):
        self.assertIn("已連上", format_event("mqtt_connect", {"success": True}))
        self.assertIn("訂閱成功", format_event("mqtt_subscribe", {"success": True}))
        self.assertIn("訂閱失敗", format_event("mqtt_subscribe", {"success": False}))
        failure = format_event("mqtt_connect", {"success": False, "code": 135,
                                               "reason": "secret-user-id"})
        self.assertIn("135：未獲授權", failure)
        self.assertNotIn("secret-user-id", failure)
        self.assertIn("連線已中斷", format_event("mqtt_disconnect", {"code": 128}))

    def test_each_metric_keeps_its_own_age_and_zero(self):
        text = self.snapshot({"cms_batt_soc": self.field(76.25, 1),
                              "pow_get_ac_in": self.field(0, 9),
                              "pow_get_ac_out": self.field(35, 130, True),
                              "plug_in_info_ac_charger_flag": self.field(0, 4)})
        self.assertIn("電量：76.2%（1 秒前收到）", text)
        self.assertIn("AC 輸入：0 W（9 秒前收到）", text)
        self.assertIn("AC 輸出：35 W（130 秒前收到；已過期）", text)
        self.assertIn("0／設備回報未連接（4 秒前收到）", text)
        self.assertIn("尚未實機斷電驗證，不能保證市電正常", text)
        self.assertNotIn("停電", text)

    def test_watts_never_determine_grid_state(self):
        for power in (0, 300):
            text = self.snapshot({"pow_get_ac_in": self.field(power)})
            self.assertIn("AC 連接旗標：尚未收到有效值", text)
            self.assertNotIn("市電已", text)

    def test_disconnected_and_stale_flag_are_not_current(self):
        text = self.snapshot({"plug_in_info_ac_charger_flag": self.field(1, 140, True)},
                             broker_connected=False)
        self.assertIn("雲端未連線", text)
        self.assertIn("已過期", text)
        self.assertIn("不能保證市電正常", text)

    def test_cached_retained_messages_do_not_spam_console(self):
        for cached, retained in [(False, False), (True, False), (False, True)]:
            self.assertIsNone(format_event("message", {"cached": cached, "retained": retained,
                                                       "fields": {"cms_batt_soc": 80}}))

    def test_snapshot_never_labels_cache_as_live(self):
        for marker in ("cached", "retained"):
            text = self.snapshot({"cms_batt_soc": self.field(80, **{marker: True}),
                                  "plug_in_info_ac_charger_flag": self.field(1, **{marker: True})})
            self.assertIn("快取，非即時", text)
            self.assertIn("不能保證市電正常", text)
            self.assertIn("快取資料，非即時", self.snapshot(**{marker: True}))

    def test_fresh_fallback_metrics_and_missing_data(self):
        text = self.snapshot({"cms_batt_soc": self.field(80, 200, True),
                              "bms_batt_soc": self.field(79, 2), "pow_get_ac": self.field(12)})
        self.assertIn("電量：79%", text)
        self.assertIn("AC 輸出：12 W", text)
        empty = self.snapshot(last_telemetry_receive_age_seconds=None)
        self.assertIn("尚未收到非快取指標", empty)
        self.assertIn("電量：尚未收到", empty)

    def test_newest_soc_wins_and_signed_output_is_displayed_as_absolute(self):
        fields = {"cms_batt_soc": self.field(79, 98),
                  "bms_heartbeat_display_soc": self.field(79.27, 4),
                  "pow_get_ac_in": self.field(103, 3),
                  "pow_get_ac_out": self.field(-103, 3),
                  "plug_in_info_ac_charger_flag": self.field(1, 198, True)}
        text = self.snapshot(fields)
        self.assertIn("電量：79.3%（4 秒前收到）", text)
        self.assertIn("AC 輸入：103 W（3 秒前收到）", text)
        self.assertIn("AC 輸出：103 W（3 秒前收到）", text)
        self.assertIn("198 秒前收到；已過期", text)
        self.assertIn("最後回報值，目前狀態未確認", text)
        self.assertEqual(fields["pow_get_ac_out"]["value"], -103)

    def test_only_numeric_whitelisted_values_can_be_printed(self):
        secret = "DO-NOT-PRINT-SN-USER-PASSWORD"
        text = self.snapshot({"cms_batt_soc": self.field(secret),
                              "pow_get_ac_in": self.field(float("nan")),
                              "unknown": self.field(secret)},
                             reason=secret, user_id=secret, sn=secret, password=secret)
        self.assertNotIn(secret, text)
        self.assertNotIn("nan", text)
        failure = format_event("mqtt_connect", {"code": secret, "reason": secret})
        self.assertNotIn(secret, failure)

    def test_summary_reports_counts_without_claiming_sample_freshness(self):
        text = format_event("summary", {"messages": 120, "decoded_live_messages": 75,
                                        "note": "secret"})
        self.assertIn("120 筆訊息", text)
        self.assertIn("75 筆含非快取指標", text)
        self.assertIn("接收時間不代表設備採樣時間", text)
        self.assertNotIn("secret", text)

    def test_invalid_events_and_data_are_ignored(self):
        self.assertIsNone(format_event("unknown", {"value": "secret"}))
        self.assertIsNone(format_event("snapshot", None))


if __name__ == "__main__":
    unittest.main()
