import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import probe
import ef_river3_pb2 as pb


class ProbeTests(unittest.TestCase):
    def packet(self, message, command=21, xor=False):
        packet = pb.River3HeaderMessage()
        header = packet.header.add()
        header.cmd_func, header.cmd_id, header.src = 254, command, 2
        header.seq, header.enc_type = 301, int(xor)
        raw = message.SerializeToString()
        header.pdata = bytes((value ^ 301) & 255 for value in raw) if xor else raw
        return packet.SerializeToString()

    def test_zero_and_false_preserved_in_binary_and_base64(self):
        message = pb.River3DisplayPropertyUpload()
        message.cms_batt_soc = 75.5
        message.pow_get_ac_in = 0
        message.plug_in_info_ac_charger_flag = 0
        for raw in [self.packet(message), base64.b64encode(self.packet(message, xor=True))]:
            data, _, encoding = probe.decode_message(raw)
            self.assertEqual(encoding, "protobuf")
            self.assertEqual(data["cms_batt_soc"], 75.5)
            self.assertEqual(data["pow_get_ac_in"], 0)
            self.assertEqual(data["plug_in_info_ac_charger_flag"], 0)
            self.assertNotIn("plug_in_info_ac_in_vol", data)

    def test_runtime_voltage_and_multi_header(self):
        runtime = pb.River3RuntimePropertyUpload()
        runtime.plug_in_info_ac_in_vol = 221.5
        display = pb.River3DisplayPropertyUpload()
        display.plug_in_info_ac_charger_flag = 1
        packet = pb.River3HeaderMessage()
        for raw in [self.packet(runtime, command=22), self.packet(display)]:
            other = pb.River3HeaderMessage.FromString(raw)
            packet.header.extend(other.header)
        values, headers, _ = probe.decode_message(packet.SerializeToString())
        self.assertEqual(values["plug_in_info_ac_in_vol"], 221.5)
        self.assertEqual(len(headers), 2)

    def test_partial_updates_do_not_refresh_ac_flag(self):
        state = probe.Telemetry()
        state.update({"plug_in_info_ac_charger_flag": 1}, 100)
        state.update({"cms_batt_soc": 72}, 230)
        result = state.snapshot(240, True, 120)
        self.assertEqual(result["ac_charger_flag_state"], "unknown")
        self.assertTrue(result["fields"]["plug_in_info_ac_charger_flag"]["stale"])
        self.assertFalse(result["fields"]["cms_batt_soc"]["stale"])
        state.update({"plug_in_info_ac_charger_flag": 0}, 245)
        self.assertEqual(state.snapshot(246, True, 120)["ac_charger_flag_state"], "flag_off")
        self.assertEqual(state.snapshot(246, False, 120)["ac_charger_flag_state"], "unknown")

    def test_no_power_based_grid_inference(self):
        state = probe.Telemetry()
        state.update({"pow_get_ac_in": 300, "pow_get_pv": 100}, 0)
        self.assertEqual(state.snapshot(1, True, 120)["ac_charger_flag_state"], "unknown")

    def test_cache_and_nonfinite_filtering(self):
        data, _, encoding = probe.decode_message(json.dumps({"operateType": "latestQuotas", "data": {"quotaMap": {"cms_batt_soc": 70}}}).encode())
        self.assertEqual(encoding, "cached_snapshot")
        self.assertEqual(data, {"cms_batt_soc": 70})
        self.assertEqual(probe.filtered({"token": "secret", "cms_batt_soc": float("nan")}), {})

    def test_cn_phone_payload(self):
        result = probe.cn_login_payload("+86 13800000000", "secret")
        self.assertEqual(result["phone"], "13800000000")
        self.assertNotIn("email", result)
        self.assertEqual(base64.b64decode(result["password"]), b"secret")
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value=" secret "):
            self.assertEqual(probe.visible_input(""), " secret ")

    def test_bms_heartbeat_separate_schema_and_string_metrics(self):
        msg = pb.River3BMSHeartBeatReport()
        msg.soc, msg.f32_show_soc = 0, 0
        packet = pb.River3HeaderMessage()
        h = packet.header.add()
        h.cmd_func, h.cmd_id, h.pdata = 3, 1, msg.SerializeToString()
        values, _, _ = probe.decode_message(packet.SerializeToString())
        self.assertEqual(values["bms_heartbeat_soc"], 0)
        self.assertEqual(values["bms_heartbeat_display_soc"], 0)
        self.assertEqual(probe.filtered({"cmsBattSoc": "75.5", "pow_get_ac_in": "0"}),
                         {"cms_batt_soc": 75.5, "pow_get_ac_in": 0})

    def test_tls_broker_validation(self):
        valid = {"url": "mqtt-cn.ecoflow.com", "port": 8883, "certificateAccount": "user", "certificatePassword": "secret"}
        self.assertEqual(probe.broker_settings(valid)[0], "mqtt-cn.ecoflow.com")
        for url in ["mqtt.ecoflow.com.evil.example", "http://mqtt.ecoflow.com", "127.0.0.1"]:
            with self.assertRaises(probe.ProbeError):
                probe.broker_settings({**valid, "url": url})

    def test_signature_empty_and_sn(self):
        for params, plain in [({}, "accessKey=ak&nonce=123456&timestamp=1000"), ({"sn": "R655TEST"}, "sn=R655TEST&accessKey=ak&nonce=123456&timestamp=1000")]:
            headers = probe.official_signature("ak", "sk", params, "123456", "1000")
            self.assertEqual(headers["sign"], hmac.new(b"sk", plain.encode(), hashlib.sha256).hexdigest())

    def test_app_client_id_matches_active_upstream_login_path(self):
        first = probe.app_client_id("123456")
        self.assertRegex(first, r"^ANDROID_[A-F0-9]{32}_123456$")
        self.assertNotEqual(first, probe.app_client_id("123456"))
        self.assertEqual(len(first.split("_")), 3)

    def test_protobuf_get_has_only_read_envelope_and_no_setting_payload(self):
        packet = pb.River3HeaderMessage.FromString(probe.read_request("protobuf"))
        self.assertEqual(len(packet.header), 1)
        header = packet.header[0]
        self.assertEqual((header.src, header.dest), (32, 32))
        self.assertEqual(getattr(header, "from"), "Android")
        self.assertEqual({field.name for field, _ in header.ListFields()}, {"src", "dest", "seq", "from"})
        self.assertFalse(header.HasField("pdata"))
        self.assertFalse(header.HasField("cmd_func"))
        self.assertFalse(header.HasField("cmd_id"))

    def test_read_request_is_fixed_and_no_credentials_logged(self):
        request = json.loads(probe.read_request())
        self.assertEqual(request["operateType"], "latestQuotas")
        self.assertEqual(request["params"], {})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.jsonl"
            with contextlib.redirect_stdout(io.StringIO()):
                recorder = probe.Recorder(path)
                recorder.emit("snapshot", **probe.Telemetry().snapshot(0, False, 120))
                recorder.close()
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertNotIn("password", path.read_text())
            with self.assertRaises(FileExistsError):
                probe.Recorder(path)

    def test_input_requires_interactive_terminal(self):
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(probe.ProbeError):
                probe.visible_input("Password: ")

    def test_saved_identifiers_only_need_password_once(self):
        profile = {"phone": "13800000000", "sn": "R655TEST1234"}
        with patch.object(probe, "load_profile", return_value=profile), \
             patch.object(probe, "get_password", return_value=None), \
             patch.object(probe, "visible_input", return_value=" password ") as prompt, \
             contextlib.redirect_stdout(io.StringIO()):
            result = probe.cloud_inputs()
        self.assertEqual(result, ("R655TEST1234", "13800000000", " password ", True))
        self.assertEqual(prompt.call_count, 1)
        with patch.object(probe, "load_profile", return_value=profile), \
             patch.object(probe, "get_password", return_value="saved password"), \
             patch.object(probe, "visible_input") as prompt, \
             contextlib.redirect_stdout(io.StringIO()):
            result = probe.cloud_inputs()
        self.assertEqual(result[-2:], ("saved password", False))
        prompt.assert_not_called()

    def test_credentials_saved_only_after_successful_login_and_not_logged(self):
        credentials = {"token": "PRIVATE_TOKEN", "user": {"userId": "123"}}
        cert = {"url": "iot-mqtt-cn.ecoflow.com", "port": 8883,
                "certificateAccount": "PRIVATE_MQTT_USER", "certificatePassword": "PRIVATE_MQTT_PASS"}
        console = io.StringIO()
        with patch.object(probe, "request_json", side_effect=[credentials, cert]), \
             patch.object(probe, "save_profile") as profile, \
             patch.object(probe, "set_password") as save, contextlib.redirect_stdout(console):
            probe.cloud_credentials("13800000000", "PRIVATE_PASSWORD", "R655TEST1234", True)
        save.assert_called_once_with("13800000000", "PRIVATE_PASSWORD")
        profile.assert_called_once_with("13800000000", "R655TEST1234")
        for secret in ("PRIVATE_PASSWORD", "PRIVATE_TOKEN", "PRIVATE_MQTT_PASS"):
            self.assertNotIn(secret, console.getvalue())
        with patch.object(probe, "request_json", side_effect=probe.ProbeError("rejected")), \
             patch.object(probe, "save_profile") as profile, \
             patch.object(probe, "set_password") as save, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(probe.ProbeError):
                probe.cloud_credentials("13800000000", "wrong", "R655TEST1234", True)
        save.assert_not_called()
        profile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
