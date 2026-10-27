"""Local, bounded smoke test. Secrets remain in this process only."""
import json
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "probe/ecoflow-wifi-cloud-probe"))
from credential_store import get_password
from profile_store import load_profile
sys.path.insert(0, str(ROOT))
from exporter import Config, Collector, http_server
from prometheus_client.parser import text_string_to_metric_families

profile = load_profile()
if not profile:
    print("No saved local profile; live test skipped.", flush=True)
    raise SystemExit(2)
password = get_password(profile["phone"])
if not password:
    print("No saved Keychain password; live test skipped.", flush=True)
    raise SystemExit(2)
config = Config.from_env({"ECOFLOW_ACCOUNT": profile["phone"], "ECOFLOW_PASSWORD": password,
                          "ECOFLOW_SN": profile["sn"], "EXPORTER_LISTEN": "127.0.0.1", "EXPORTER_PORT": "9098"})
collector = Collector(config)
server = http_server(collector, config.listen, config.port)
thread = threading.Thread(target=server.serve_forever, daemon=True)
collector.start()
thread.start()
local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
started, ready_seen, checks = time.monotonic(), False, 0
last_metrics = b""
print("Bounded native live test started on 127.0.0.1:9098; no credentials printed.", flush=True)
try:
    while time.monotonic() - started < 145:
        with local.open("http://127.0.0.1:9098/metrics", timeout=3) as response:
            last_metrics = response.read()
            assert response.status == 200
        samples = [sample for family in text_string_to_metric_families(last_metrics.decode()) for sample in family.samples]
        assert samples
        for secret in (config.account, config.password, config.sn):
            assert secret.encode() not in last_metrics, "credential must not appear in metrics"
        checks += 1
        ready = collector.ready()
        if ready and not ready_seen:
            print("Fresh SOC + AC flag available through /metrics.", flush=True)
        ready_seen |= ready
        if checks % 3 == 0:
            snap = collector.snapshot()
            print(json.dumps({"elapsed_seconds": round(time.monotonic()-started), "ready": ready,
                              "mqtt_connected": snap["connected"],
                              "live_messages": snap["counters"]["decoded_live"],
                              "reads_no_data": snap["counters"]["reads_no_data"],
                              "reads_stale": snap["counters"]["reads_stale"],
                              "login_attempts": snap["counters"]["login_attempts"]}), flush=True)
        time.sleep(10)
    assert ready_seen, "no fresh core telemetry received"
    assert collector.ready(), "core telemetry was not fresh at end of live test"
    assert collector.snapshot()["counters"]["login_attempts"] == 1
    summary = {"native_live_passed": True, "port": 9098, "http_scrapes": checks,
               "duration_seconds": round(time.monotonic()-started), "ready_at_end": True,
               "counters": collector.snapshot()["counters"],
               "credentials_in_metrics": False, "docker_runtime_tested": False}
    output_dir = ROOT / "work/ecoflow-live-validation"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metrics.prom").write_bytes(last_metrics)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)
finally:
    collector.stop()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    print("Native live test stopped; no background exporter left running.", flush=True)
