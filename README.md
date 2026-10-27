# EcoFlow RIVER 3 UPS Exporter

將已測通的中國區 App MQTT 採集方式提供成 Prometheus `/probe`，供 vmagent 或 Prometheus 按設備抓取。預設埠為 **9098**。第一版以已測試的中國區 **睿 RIVER 3 UPS／R655** 為支援目標；R654 使用相同 schema，可配置測試。其他型號、區域與韌體尚未實測，不宣稱通用支援所有 EcoFlow。

快速入門請看本文件；架構、測試方法與來源索引分別見 [`docs/FINAL_SOLUTION.md`](docs/FINAL_SOLUTION.md)、[`docs/TESTING.md`](docs/TESTING.md) 和 [`docs/SOURCE_MAP.md`](docs/SOURCE_MAP.md)。Mac 雲端採集研究工具的說明見 [`probe/ecoflow-wifi-cloud-probe/README.md`](probe/ecoflow-wifi-cloud-probe/README.md)。

```text
RIVER 3 UPS → EcoFlow 中國區雲端 MQTT → exporter :9098/probe → vmagent → VictoriaMetrics
```

帳號、密碼和設備 SN 由 vmagent 的 target/relabel 配置傳入 `/probe`。Exporter 按配置指紋在進程內復用 MQTT 連接與最近緩存；相同配置的後續 scrape 不會重新登入或新開連接。閒置超過 TTL 後連接才會關閉。

## Docker

在這個資料夾中執行：

```sh
cp .env.example .env
chmod 600 .env
```

```sh
docker compose pull
docker compose up -d
docker compose logs -f ecoflow-exporter
curl -fsS http://127.0.0.1:9098/metrics
```

Compose 會從公開 GHCR 拉取 `ghcr.io/sqkkyzx/ecoflow-exporter:latest`，不需要 Dockerfile 或本地原始碼。GitHub Actions 會在 `main` 更新時發布 `latest`，並在 `v*` Git tag 時發布對應版本標籤；PR 只建置驗證，不會推送映像。

映像以非 root 使用者執行，不需要特權模式、藍牙、USB 或 host network。映像內只複製程式與協議 schema，不包含 `.env`、secrets 或測試結果。

## vmagent

將 `vmagent.yml` 的 `scrape_configs` 加入現有 vmagent 設定。每個 target 的 account、password 和 SN 放在 target labels，再用 `__param_*` 送到 `/probe`；`password` 在 target relabel 階段移除，不會成為輸出 metric label：

```yaml
scrape_configs:
  - job_name: ecoflow
    metrics_path: /probe
    static_configs:
      - targets: ['ecoflow-exporter:9098']
        labels:
          account: 'YOUR_CN_PHONE_NUMBER'
          password: 'YOUR_ECOFLOW_APP_PASSWORD'
          sn: R655YOURDEVICE
          device_name: river3
          api_host: api-cn.ecoflow.com
    relabel_configs:
      - source_labels: [account]
        target_label: __param_account
      - source_labels: [password]
        target_label: __param_password
      - source_labels: [sn]
        target_label: __param_sn
      - source_labels: [device_name]
        target_label: __param_device_name
      - source_labels: [api_host]
        target_label: __param_api_host
      - source_labels: [sn]
        target_label: instance
      - action: labeldrop
        regex: password|sn|device_name|api_host
```

若 vmagent 在其他主機或 VM，將 target 改為部署 exporter 的主機 IP 加埠，例如 `192.0.2.10:9098`。不要使用 vmagent 自己容器內的 `localhost` 指向 exporter。vmagent 透過 `-promscrape.config=/etc/vmagent/vmagent.yml` 載入抓取設定，現有 `remoteWrite` 設定沿用即可。[vmagent 官方文件](https://docs.victoriametrics.com/victoriametrics/vmagent/)

## 資料更新與恢復

- 初次連上且訂閱成功後，沒有有效指標才發唯讀 Protobuf GET。
- 正常推送期間不固定每 60 秒發送；重要資料過期時才補讀。
- 每次讀取後預設等待 30 秒，仍無資料或仍過期才再嘗試。
- 每個欄位各自計算接收時間；其他欄位更新不會替 AC 旗標刷新時間。
- SOC 與 AC 接入旗標是必要資料組，未收到時也會補讀；持續有功率資料不會掩蓋旗標缺失。
- retained、雲端快取與 `get_reply` 不會被當成新的設備即時採樣。
- MQTT 斷線會重連，授權失敗會重新取得憑證並限速重試；雲端中斷時 `/metrics` 仍可被 vmagent 抓取。

過期、未收到或斷線時，對應數值 series 會省略，並提供可用性與資料年齡指標；不會把缺失值填成 0。AC 接入依據設備原生旗標，不從輸入 0 W 推斷停電。MQTT 已連線也不代表所有設備資料都是最新。

## 主要指標

所有設備指標以 `device` 標籤區分設備，值來自 `/probe` 的 `device_name`；未提供時使用 `river3`。vmagent 另保留 `account` 和 `instance`（SN）target labels。

| 指標 | 意義 |
|---|---|
| `ecoflow_battery_soc_percent` | 最新有效電量百分比 |
| `ecoflow_ac_input_power_watts` | AC 輸入功率 |
| `ecoflow_ac_output_power_watts` | AC 輸出功率，以絕對值表示 |
| `ecoflow_total_input_power_watts` / `ecoflow_total_output_power_watts` | 總輸入／輸出功率 |
| `ecoflow_solar_input_power_watts` | 太陽能輸入功率 |
| `ecoflow_ac_connected` | 設備原生 AC 接入旗標：1 接入、0 未接入 |
| `ecoflow_metric_available{metric="battery_soc_percent"}` | 對應指標是否有效且未過期，1／0 |
| `ecoflow_metric_age_seconds{metric="ac_connected"}` | 對應欄位距上次接收的秒數 |
| `ecoflow_metric_last_received_timestamp_seconds` | 各指標最後接收的 Unix 時間 |
| `probe_success` | 是否已收到新鮮的 SOC 和 AC flag |
| `ecoflow_mqtt_connected` / `ecoflow_mqtt_subscribed` | MQTT 連線／訂閱狀態 |
| `ecoflow_login_success` | 最近一次帳號登入是否成功 |
| `ecoflow_telemetry_available` | 是否至少有一個有效指標，不能取代各指標可用性 |
| `ecoflow_last_live_received_timestamp_seconds` | 最後一筆解碼成功、非快取訊息的接收時間 |
| `ecoflow_read_requests_total{reason="no_data"}` | 因無資料而嘗試 GET 的累計次數 |
| `ecoflow_read_requests_total{reason="stale_data"}` | 因過期而嘗試 GET 的累計次數 |
| `ecoflow_cached_messages_total` / `ecoflow_decode_errors_total` | 快取回覆／解碼錯誤累計 |

例如在 VictoriaMetrics／Grafana 查詢低電量可用 `ecoflow_battery_soc_percent < 20`；設備回報失去 AC 接入可用 `ecoflow_ac_connected == 0`。兩者的數值 series 只在資料有效時存在，因此也應單獨監測 `ecoflow_metric_available{metric="ac_connected"} == 0`，避免資料過期時沒有告警。`up{job="ecoflow"}` 是 vmagent 自行產生的抓取狀態，只表示 HTTP target 可否抓取；`probe_success` 表示 SOC 和 AC flag 是否新鮮可用。vmagent 負責採集；告警規則由既有 vmalert／Prometheus 等告警服務處理。

## 設定

| 參數／環境變數 | 預設／用途 |
|---|---|
| `/probe?account=` / `password=` / `sn=` | 每個 target 必填；由 vmagent relabel 送入，密碼不會輸出為 metric label |
| `/probe?device_name=` | 可選；metrics 的設備別名，預設 `river3` |
| `ECOFLOW_API_HOST` | `api-cn.ecoflow.com`；所有 target 的預設區域 |
| `EXPORTER_PORT` | `9098`；程式監聽埠，Compose 同步映射宿主相同埠 |
| `EXPORTER_LISTEN` | `0.0.0.0`；直接執行時可改，Compose 內固定全介面監聽 |
| `ECOFLOW_STALE_SECONDS` | `120`；資料過期門檻 |
| `ECOFLOW_READ_COOLDOWN_SECONDS` | `30`；按需 GET 後的等待時間，不是固定輪詢週期 |
| `ECOFLOW_RECONNECT_MIN_SECONDS` | `5`；斷線重試起始等待 |
| `ECOFLOW_RECONNECT_MAX_SECONDS` | `300`；斷線重試最長等待 |
| `ECOFLOW_AUTH_RETRY_SECONDS` | `300`；授權失敗的重試等待 |
| `ECOFLOW_SESSION_TTL_SECONDS` | `300`；配置閒置多久後關閉 MQTT 連接 |
| `ECOFLOW_MAX_SESSIONS` | `16`；進程內最多保留的不同配置連接數 |
| `LOG_LEVEL` | `INFO`；日誌級別 |

更改埠後，請同步修改 vmagent 的 `replacement`。同一個 exporter 可服務多個 target；相同帳號、密碼、SN 和 API host 的 target 會復用同一條 MQTT session。

## HTTP 端點

| 端點 | 用途 |
|---|---|
| `/probe` | 按 query 配置建立或復用 MQTT session，返回該 target 的 Prometheus metrics |
| `/metrics` | Exporter 自身 session pool 統計 |
| `/healthz` | 程式存活檢查；Docker healthcheck 使用此端點 |
| `/readyz` | 至少一個 target 有有效、未過期讀數時回 200；否則回 503 |

HTTP `/probe` 目前沒有登入層，應只允許可信的 vmagent 網路訪問。密碼會以 query parameter 傳送；target relabel 的 `labeldrop` 可避免它進入採集的 metric labels，但不會從 vmagent 配置或 HTTP URL 中隱藏。若經過反向代理，請停用 query string access logging；跨主機時使用 TLS。帳號與 SN 可分別保留為 `account` 和 `instance` labels。

## 驗證與範圍

新增 session pool 和 `/probe` 的自動化測試覆蓋配置復用、並發去重與 idle TTL。既有本機真實設備驗證是直接 Collector 的 150 秒測試，尚未在真實設備上驗證 Prometheus `/probe` session pool。本開發環境沒有 Docker CLI／daemon；GHCR 映像由 GitHub Actions 建置。詳細證據見 [VALIDATION.md](VALIDATION.md)。

Schema 來源與授權請見 `LICENSE`、`NOTICE` 及 `vendor/`。本專案與 EcoFlow 無隸屬關係，使用的 App 私有接口可能隨服務或韌體更新改變。
