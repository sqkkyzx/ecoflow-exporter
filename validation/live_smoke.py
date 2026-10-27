"""Local, bounded smoke test. Secrets remain in this process only."""
import json
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "probe/ecoflow-wifi-cloud-probe"))
from credential_store import get_password
from profile_store import load_profile
sys.path.insert(0, str(ROOT))
from exporter import Config, ServerConfig, SessionPool, http_server
from prometheus_client.parser import text_string_to_metric_families

profile = load_profile()
if not profile:
    print("No saved local profile; live test skipped.", flush=True)
    raise SystemExit(2)
password = get_password(profile["phone"])
if not password:
    print("No saved Keychain password; live test skipped.", flush=True)
    raise SystemExit(2)
settings = ServerConfig.from_env({"EXPORTER_LISTEN": "127.0.0.1", "EXPORTER_PORT": "9098"})
config = Config.from_probe({"account": profile["phone"], "password": password,
                            "sn": profile["sn"], "device_name": "river3"}, settings)
pool = SessionPool(settings)
server = http_server(pool, settings.listen, settings.port)
thread = threading.Thread(target=server.serve_forever, daemon=True)
pool.start()
thread.start()
local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
query = urllib.parse.urlencode({"account": config.account, "password": config.password,
                                "sn": config.sn, "device_name": config.device_name,
                                "api_host": config.api_host})
probe_url = f"http://127.0.0.1:9098/probe?{query}"
started, ready_seen, checks = time.monotonic(), False, 0
last_metrics = b""
print("Bounded native live test started on 127.0.0.1:9098; no credentials printed.", flush=True)
try:
    while time.monotonic() - started < 145:
        with local.open(probe_url, timeout=3) as response:
            last_metrics = response.read()
            assert response.status == 200
        samples = [sample for family in text_string_to_metric_families(last_metrics.decode()) for sample in family.samples]
        assert samples
        for secret in (config.account, config.password, config.sn):
            assert secret.encode() not in last_metrics, "credential must not appear in metrics"
        checks += 1
        snap = pool.snapshot(config)
        ready = snap["probe_success"]
        if ready and not ready_seen:
            print("Fresh SOC + AC flag available through /probe.", flush=True)
        ready_seen |= ready
        if checks % 3 == 0:
            print(json.dumps({"elapsed_seconds": round(time.monotonic()-started), "ready": ready,
                              "mqtt_connected": snap["connected"],
                              "live_messages": snap["counters"]["decoded_live"],
                              "reads_no_data": snap["counters"]["reads_no_data"],
                              "reads_stale": snap["counters"]["reads_stale"],
                              "login_attempts": snap["counters"]["login_attempts"]}), flush=True)
        time.sleep(10)
    final_snapshot = pool.snapshot(config)
    live_passed = ready_seen and final_snapshot["probe_success"] and final_snapshot["counters"]["login_attempts"] == 1
    summary = {"probe_live_passed": live_passed, "port": 9098, "http_scrapes": checks,
               "duration_seconds": round(time.monotonic()-started), "ready_at_end": final_snapshot["probe_success"],
               "counters": final_snapshot["counters"],
               "credentials_in_metrics": False, "docker_runtime_tested": False}
    output_dir = ROOT / "work/ecoflow-live-validation"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metrics.prom").write_bytes(last_metrics)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)
    if not live_passed:
        raise SystemExit(1)
finally:
    pool.stop()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    print("Native live test stopped; no background exporter left running.", flush=True)
