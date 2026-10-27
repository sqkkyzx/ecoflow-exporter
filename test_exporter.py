import base64
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from prometheus_client.parser import text_string_to_metric_families

from exporter import (CloudAPI, CloudError, Collector, Config, ConfigError, ServerConfig,
                      SessionPool, config_fingerprint, http_server)
from metrics import render_metrics
from recovery import missing_or_stale
import protocol
import ef_river3_pb2 as pb


def metric_values(raw):
    return {sample.name + str(sorted(sample.labels.items())): sample.value
            for family in text_string_to_metric_families(raw.decode()) for sample in family.samples}


def packet(**values):
    display = pb.River3DisplayPropertyUpload(**values)
    message = pb.River3HeaderMessage()
    h = message.header.add(cmd_func=254, cmd_id=21)
    h.pdata = display.SerializeToString()
    return message.SerializeToString()


class ConfigTests(unittest.TestCase):
    def env(self):
        return {"ECOFLOW_ACCOUNT": "+86 13800000000", "ECOFLOW_PASSWORD": " password $ ' ", "ECOFLOW_SN": "r655test1234"}

    def test_environment_and_repr(self):
        config = Config.from_env(self.env())
        self.assertEqual(config.account, "13800000000")
        self.assertEqual(config.password, " password $ ' ")
        self.assertEqual(config.sn, "R655TEST1234")
        self.assertEqual(config.port, 9098)
        for secret in (config.account, config.password, config.sn):
            self.assertNotIn(secret, repr(config))

    def test_secret_file_preserves_password_and_excludes_conflicting_env(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password"
            path.write_text(" password\n")
            env = self.env()
            del env["ECOFLOW_PASSWORD"]
            env["ECOFLOW_PASSWORD_FILE"] = str(path)
            self.assertEqual(Config.from_env(env).password, " password\n")
            env["ECOFLOW_PASSWORD"] = "conflict"
            with self.assertRaises(ConfigError):
                Config.from_env(env)

    def test_invalid_settings_fail_without_echoing_secrets(self):
        for key, value in [("EXPORTER_PORT", "invalid"), ("ECOFLOW_API_HOST", "evil.example"),
                           ("ECOFLOW_ACCOUNT", "PRIVATE_INVALID_ACCOUNT"), ("ECOFLOW_SN", "PRIVATE-INVALID-SN")]:
            with self.subTest(key=key), self.assertRaises(ConfigError) as error:
                Config.from_env({**self.env(), key: value})
            self.assertNotIn(value, str(error.exception))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.collector = Collector(Config("13800000000", "PRIVATE_PASSWORD", "R655TEST1234"), clock=lambda: self.now[0])
        self.collector.connected = self.collector.subscribed = True

    def test_live_zero_values_become_ready_and_cache_cannot_refresh_them(self):
        raw = packet(cms_batt_soc=0, pow_get_ac_in=0, plug_in_info_ac_charger_flag=0)
        self.collector.receive(raw)
        self.assertTrue(self.collector.ready())
        timestamp = self.collector.values["cms_batt_soc"]["timestamp"]
        self.now[0] += 121
        self.collector.receive(raw, reply=True)
        self.collector.receive(raw, retained=True)
        self.assertEqual(self.collector.values["cms_batt_soc"]["timestamp"], timestamp)
        self.assertFalse(self.collector.ready())
        raw_metrics = render_metrics(self.collector.snapshot()).decode()
        self.assertNotIn('ecoflow_ac_connected{device="river3"}', raw_metrics)
        self.assertIn('ecoflow_metric_available{device="river3",metric="ac_connected"} 0.0', raw_metrics)

    def test_disconnect_invalidates_old_session_values(self):
        self.collector.receive(packet(cms_batt_soc=79, plug_in_info_ac_charger_flag=1))
        last = self.collector.last_live_timestamp
        self.collector._reset_connection()
        self.assertFalse(self.collector.ready())
        self.assertFalse(self.collector.values)
        self.assertEqual(self.collector.last_live_timestamp, last)
        self.collector.connected = self.collector.subscribed = True
        self.assertFalse(self.collector.ready())

    def test_malformed_payload_is_counted_without_escaping_collector(self):
        self.collector.receive(b"\xff\xff\xff")
        self.assertEqual(self.collector.counters["decode_errors"], 1)
        self.assertFalse(self.collector.values)

    def test_missing_ac_flag_is_recovered_even_when_soc_and_power_are_fresh(self):
        self.collector.receive(packet(cms_batt_soc=79, pow_get_ac_in=103))
        self.assertFalse(self.collector.ready())
        self.assertEqual(missing_or_stale(self.collector.recovery_snapshot()), "no_data")
        self.collector.receive(packet(plug_in_info_ac_charger_flag=1))
        self.assertIsNone(missing_or_stale(self.collector.recovery_snapshot()))

    def test_clock_rollback_hides_future_values_and_triggers_recovery(self):
        self.collector.receive(packet(cms_batt_soc=79, plug_in_info_ac_charger_flag=1))
        self.now[0] -= 60
        self.assertFalse(self.collector.ready())
        self.assertEqual(missing_or_stale(self.collector.recovery_snapshot()), "stale_data")
        self.assertNotIn('ecoflow_ac_connected{device="river3"}', render_metrics(self.collector.snapshot()).decode())

    def test_invalid_soc_cannot_make_ready_endpoint_healthy(self):
        self.collector.receive(packet(cms_batt_soc=200, plug_in_info_ac_charger_flag=1))
        self.assertFalse(self.collector.ready())


class AuthTests(unittest.TestCase):
    def config(self):
        return Config("13800000000", " PRIVATE_PASSWORD ", "R655TEST1234")

    def test_login_phone_and_password_contract(self):
        api = CloudAPI(self.config())
        with patch.object(api, "request", return_value={"token": "TOKEN", "user": {"userId": "123"}}) as request:
            self.assertEqual(api.login(), ("TOKEN", "123"))
            payload = request.call_args.kwargs["data"]
        self.assertEqual(payload["phone"], "13800000000")
        self.assertNotIn("email", payload)
        self.assertEqual(base64.b64decode(payload["password"]), b" PRIVATE_PASSWORD ")

    def test_certification_host_is_validated(self):
        api = CloudAPI(self.config())
        for host in ("mqtt.ecoflow.com.evil.example", "http://mqtt.ecoflow.com", "127.0.0.1"):
            with patch.object(api, "request", return_value={"url": host, "port": 8883,
                             "certificateAccount": "USER", "certificatePassword": "SECRET"}):
                with self.assertRaises(CloudError) as error:
                    api.certification("TOKEN", "123")
                self.assertNotIn("SECRET", str(error.exception))


class HTTPTests(unittest.TestCase):
    def test_http_scrapes_do_not_trigger_login_and_remain_healthy_during_cloud_failure(self):
        class OfflineAPI:
            calls = 0
            def login(self):
                self.calls += 1
                raise CloudError("login", "network")
        api = OfflineAPI()
        collector = Collector(Config("13800000000", "PASSWORD", "R655TEST1234"), api=api)
        server = http_server(collector, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        collector.start()
        thread.start()
        address = f"http://127.0.0.1:{server.server_port}"
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            for _ in range(3):
                with local.open(address + "/metrics", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue(list(text_string_to_metric_families(response.read().decode())))
            with local.open(address + "/healthz", timeout=2) as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(urllib.error.HTTPError) as error:
                local.open(address + "/readyz", timeout=2)
            self.assertEqual(error.exception.code, 503)
            error.exception.close()
            self.assertEqual(api.calls, 1)
            collector.connected = collector.subscribed = True
            collector.receive(packet(cms_batt_soc=80, plug_in_info_ac_charger_flag=1))
            with local.open(address + "/readyz", timeout=2) as response:
                self.assertEqual(response.status, 200)
        finally:
            collector.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ProbeSessionTests(unittest.TestCase):
    class FakeCollector:
        created = []

        def __init__(self, config):
            self.config = config
            self.started = self.stopped = 0
            self.created.append(self)

        def start(self):
            self.started += 1

        def stop(self):
            self.stopped += 1

        def ready(self):
            return False

        def snapshot(self):
            return {"device": self.config.device_name, "connected": False, "subscribed": False,
                    "login_ok": False, "now": 1000, "stale_seconds": 120,
                    "last_live_timestamp": None, "values": {}, "counters": {}}

    def setUp(self):
        self.FakeCollector.created = []
        self.now = [1000.0]
        settings = ServerConfig(session_ttl_seconds=60, max_sessions=4)
        self.pool = SessionPool(settings, collector_factory=self.FakeCollector, clock=lambda: self.now[0])

    def config(self, password="PASSWORD", sn="R655TEST1234", name="river3"):
        return Config.from_probe({"account": "+86 13800000000", "password": password,
                                  "sn": sn, "device_name": name}, self.pool.settings)

    def test_same_configuration_reuses_collector_and_refreshes_lease(self):
        config = self.config()
        self.pool.snapshot(config)
        self.now[0] += 30
        self.pool.snapshot(self.config(name="ups"))
        self.assertEqual(len(self.FakeCollector.created), 1)
        self.assertEqual(self.FakeCollector.created[0].started, 1)
        self.assertEqual(self.FakeCollector.created[0].config.device_name, "river3")

    def test_changed_secret_or_sn_gets_a_distinct_collector(self):
        self.pool.snapshot(self.config())
        self.pool.snapshot(self.config(password="OTHER"))
        self.pool.snapshot(self.config(sn="R654TEST5678"))
        self.assertEqual(len(self.FakeCollector.created), 3)

    def test_simultaneous_first_scrapes_create_only_one_session(self):
        config = self.config()
        barrier = threading.Barrier(12)
        results = []
        def request():
            barrier.wait()
            results.append(self.pool.get(config))
        workers = [threading.Thread(target=request) for _ in range(12)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
        self.assertEqual(len(self.FakeCollector.created), 1)
        self.assertTrue(all(item is self.FakeCollector.created[0] for item in results))

    def test_fingerprint_is_not_a_plaintext_credential_and_ignores_display_name(self):
        first = self.config(name="ups-one")
        second = self.config(name="ups-two")
        fingerprint = config_fingerprint(first)
        self.assertEqual(fingerprint, config_fingerprint(second))
        for secret in (first.account, first.password, first.sn):
            self.assertNotIn(secret, fingerprint)

    def test_idle_session_is_stopped_and_recreated(self):
        config = self.config()
        self.pool.snapshot(config)
        self.now[0] += 61
        self.assertEqual(self.pool.reap(), 1)
        self.assertEqual(self.FakeCollector.created[0].stopped, 1)
        self.pool.snapshot(config)
        self.assertEqual(len(self.FakeCollector.created), 2)

    def test_probe_http_reuses_session_and_does_not_return_credentials(self):
        server = http_server(self.pool, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        query = urllib.parse.urlencode({"account": "13800000000", "password": "PRIVATE_PASSWORD",
                                        "sn": "R655TEST1234", "device_name": "ups"})
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            for _ in range(2):
                with local.open(f"http://127.0.0.1:{server.server_port}/probe?{query}", timeout=2) as response:
                    body = response.read()
                    self.assertEqual(response.status, 200)
                    self.assertNotIn(b"PRIVATE_PASSWORD", body)
                    self.assertNotIn(b"R655TEST1234", body)
                    self.assertIn(b'probe_success{device="ups"} 0.0', body)
            self.assertEqual(len(self.FakeCollector.created), 1)
        finally:
            self.pool.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_probe_rejects_missing_credentials_without_creating_session(self):
        server = http_server(self.pool, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                local.open(f"http://127.0.0.1:{server.server_port}/probe?sn=R655TEST1234", timeout=2)
            self.assertEqual(error.exception.code, 400)
            self.assertEqual(self.FakeCollector.created, [])
        finally:
            self.pool.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class RecoveryTests(unittest.TestCase):
    def run_supervisor(self, outcomes):
        class FakeAPI:
            logins = 0
            certs = 0
            def login(self):
                self.logins += 1
                return "TOKEN", "123"
            def certification(self, token, uid):
                self.certs += 1
                return "mqtt.ecoflow.com", 8883, "USER", "PASSWORD"
        class BoundedStop:
            stopped = False
            def __init__(self): self.waits = []
            def is_set(self): return self.stopped
            def wait(self, delay):
                self.waits.append(delay)
                self.stopped = len(self.waits) >= len(outcomes)
                return self.stopped
        api = FakeAPI()
        collector = Collector(Config("13800000000", "PASSWORD", "R655TEST1234"), api=api, monotonic=lambda: 1000)
        collector.stop_event = BoundedStop()
        with patch.object(collector, "_session", side_effect=outcomes):
            collector._run()
        return collector, api

    def test_network_reconnect_reuses_login_and_certification_with_backoff(self):
        collector, api = self.run_supervisor([("network", False), ("network", False), ("network", False)])
        self.assertEqual((api.logins, api.certs), (1, 1))
        waits = collector.stop_event.waits
        self.assertTrue(waits[0] < waits[1] < waits[2])
        self.assertEqual(collector.counters["reconnects"], 2)

    def test_mqtt_authorization_rejection_refreshes_cert_without_repeating_password_login(self):
        collector, api = self.run_supervisor([("auth", False), ("network", False)])
        self.assertEqual((api.logins, api.certs), (1, 2))
        self.assertEqual(collector.stop_event.waits[0], 300)
        self.assertEqual(collector.counters["auth_errors"], 1)

    def test_mqtt_session_requests_only_when_missing_or_stale_and_only_uses_get(self):
        class Reason:
            is_failure = False
            value = 0
        class Clock:
            now = 1000.0
        clock = Clock()
        created, published = threading.Event(), threading.Event()
        clients = []
        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs, self.sent = kwargs, []
                clients.append(self)
                created.set()
            def username_pw_set(self, *args): pass
            def tls_set_context(self, *args): pass
            def connect(self, *args, **kwargs): pass
            def loop_start(self): self.on_connect(self, None, None, Reason(), None)
            def subscribe(self, topics): self.on_subscribe(self, None, 1, [Reason(), Reason()], None)
            def publish(self, topic, payload, qos, retain):
                self.sent.append((topic, payload, qos, retain))
                published.set()
                return type("Published", (), {"rc": 0})()
            def disconnect(self): self.on_disconnect(self, None, None, Reason(), None)
            def loop_stop(self): pass
        collector = Collector(Config("13800000000", "PASSWORD", "R655TEST1234"),
                              clock=lambda: clock.now, monotonic=lambda: clock.now, mqtt_factory=FakeClient)
        worker = threading.Thread(target=collector._session, args=("123", ("mqtt.ecoflow.com", 8883, "U", "P")), daemon=True)
        worker.start()
        try:
            self.assertTrue(created.wait(2))
            self.assertTrue(published.wait(2))
            collector.receive(packet(cms_batt_soc=80, pow_get_ac_in=100, plug_in_info_ac_charger_flag=1))
            clock.now += 60
            time.sleep(0.6)
            self.assertEqual(len(clients[0].sent), 1)
            published.clear()
            clock.now += 121
            self.assertTrue(published.wait(2))
            self.assertEqual(len(clients[0].sent), 2)
            self.assertEqual(collector.counters["reads_no_data"], 1)
            self.assertEqual(collector.counters["reads_stale"], 1)
            for topic, payload, qos, retain in clients[0].sent:
                self.assertTrue(topic.endswith("/thing/property/get"))
                self.assertEqual((qos, retain), (1, False))
                header = pb.River3HeaderMessage.FromString(payload).header[0]
                self.assertFalse(header.HasField("pdata"))
                self.assertFalse(header.HasField("cmd_func"))
        finally:
            collector.stop_event.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
