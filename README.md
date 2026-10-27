# EcoFlow RIVER 3 UPS Exporter

將已測通的中國區 App MQTT 採集方式提供成 Prometheus `/metrics`，供 vmagent 或 Prometheus 抓取。預設埠為 **9098**。第一版以已測試的中國區 **睿 RIVER 3 UPS／R655** 為支援目標；R654 使用相同 schema，可配置測試。其他型號、區域與韌體尚未實測，不宣稱通用支援所有 EcoFlow。

快速入門請看本文件；架構、測試方法與來源索引分別見 [`docs/FINAL_SOLUTION.md`](docs/FINAL_SOLUTION.md)、[`docs/TESTING.md`](docs/TESTING.md) 和 [`docs/SOURCE_MAP.md`](docs/SOURCE_MAP.md)。Mac 雲端採集研究工具的說明見 [`probe/ecoflow-wifi-cloud-probe/README.md`](probe/ecoflow-wifi-cloud-probe/README.md)。

```text
RIVER 3 UPS → EcoFlow 中國區雲端 MQTT → exporter :9098/metrics → vmagent → VictoriaMetrics
```

帳號、密碼和設備 SN 填在 exporter。vmagent 只需要抓取地址，不需要持有 EcoFlow 帳密。採集在背景持續運行；一次 `/metrics` 抓取只讀取記憶體，不會觸發登入或 GET。

## Docker：使用環境變數

在這個資料夾中執行：

```sh
cp .env.example .env
chmod 600 .env
```

編輯 `.env`，填入 `ECOFLOW_ACCOUNT`（中國區手機號）、`ECOFLOW_PASSWORD`（App 帳號密碼，非短信驗證碼）和 `ECOFLOW_SN`（完整設備序號）。範例未包含任何真實帳密或 SN；Mac 測試工具的鑰匙圈資料不會自動打包進容器。

```sh
docker compose pull
docker compose up -d
docker compose logs -f ecoflow-exporter
curl -fsS http://127.0.0.1:9098/metrics
```

Compose 會從公開 GHCR 拉取 `ghcr.io/sqkkyzx/ecoflow-exporter:latest`，不需要 Dockerfile 或本地原始碼。GitHub Actions 會在 `main` 更新時發布 `latest`，並在 `v*` Git tag 時發布對應版本標籤；PR 只建置驗證，不會推送映像。

容器使用 UID/GID 10001、唯讀檔案系統，移除 Linux capabilities，無需特權模式、藍牙、USB 或 host network。映像內只複製程式與協議 schema，不包含 `.env`、secrets 或測試結果。

## Docker：使用 secrets 檔案

亦可使用獨立的 `compose.secrets.yaml`。此檔完整定義服務，**不要與 `compose.yaml` 疊加**，避免同時設定直接值與 `_FILE`。在 Bash／zsh 的互動式終端建立檔案，`printf '%s'` 不會加入換行：

```sh
umask 077
mkdir -p secrets
chmod 700 secrets
printf '中國區手機號：'
IFS= read -r ECOFLOW_ACCOUNT_VALUE
printf '%s' "$ECOFLOW_ACCOUNT_VALUE" > secrets/account
printf 'EcoFlow App 密碼：'
IFS= read -r ECOFLOW_PASSWORD_VALUE
printf '%s' "$ECOFLOW_PASSWORD_VALUE" > secrets/password
printf '完整設備 SN：'
IFS= read -r ECOFLOW_SN_VALUE
printf '%s' "$ECOFLOW_SN_VALUE" > secrets/sn
unset ECOFLOW_ACCOUNT_VALUE ECOFLOW_PASSWORD_VALUE ECOFLOW_SN_VALUE
chmod 444 secrets/account secrets/password secrets/sn
docker compose -f compose.secrets.yaml pull
docker compose -f compose.secrets.yaml up -d
```

`secrets/` 目錄的 0700 限制主機其他使用者存取；檔案 0444 讓容器內非 root 的 UID 10001 能讀取掛載內容。Compose 的檔案型 secrets 使用 bind mount，不應依賴 uid／mode 欄位重新映射主機檔案權限。這些檔案不會進入映像。

密碼檔內容完整保留，不自動移除頭尾空白或換行；若用 `echo` 建檔，多出的換行會成為密碼的一部分。使用 secrets 版本時不要與 `compose.yaml` 疊加。

## vmagent

將 `vmagent.yml` 的 `scrape_configs` 加入現有 vmagent 設定。vmagent 與 exporter 在同一 Docker network 時可使用服務名：

```yaml
scrape_configs:
  - job_name: ecoflow
    scrape_interval: 15s
    scrape_timeout: 5s
    static_configs:
      - targets: ['ecoflow-exporter:9098']
```

若 vmagent 在其他主機、VM 或 Docker network，將 target 改為部署 exporter 的主機 IP 加埠，例如 `192.0.2.10:9098`。不要使用 vmagent 自己容器內的 `localhost` 指向 exporter。vmagent 透過 `-promscrape.config=/etc/vmagent/vmagent.yml` 載入抓取設定，現有 `remoteWrite` 設定沿用即可。[vmagent 官方文件](https://docs.victoriametrics.com/victoriametrics/vmagent/)

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

所有指標以 `device` 標籤區分設備，值來自 `ECOFLOW_DEVICE_NAME`。

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
| `ecoflow_mqtt_connected` / `ecoflow_mqtt_subscribed` | MQTT 連線／訂閱狀態 |
| `ecoflow_login_success` | 最近一次帳號登入是否成功 |
| `ecoflow_telemetry_available` | 是否至少有一個有效指標，不能取代各指標可用性 |
| `ecoflow_last_live_received_timestamp_seconds` | 最後一筆解碼成功、非快取訊息的接收時間 |
| `ecoflow_read_requests_total{reason="no_data"}` | 因無資料而嘗試 GET 的累計次數 |
| `ecoflow_read_requests_total{reason="stale_data"}` | 因過期而嘗試 GET 的累計次數 |
| `ecoflow_cached_messages_total` / `ecoflow_decode_errors_total` | 快取回覆／解碼錯誤累計 |

例如在 VictoriaMetrics／Grafana 查詢低電量可用 `ecoflow_battery_soc_percent < 20`；設備回報失去 AC 接入可用 `ecoflow_ac_connected == 0`。兩者的數值 series 只在資料有效時存在，因此也應單獨監測 `ecoflow_metric_available{metric="ac_connected"} == 0`，避免資料過期時沒有告警。`up{job="ecoflow"}` 是 vmagent 自行產生的抓取狀態，只表示 HTTP target 可否抓取。vmagent 負責採集；告警規則由既有 vmalert／Prometheus 等告警服務處理。

## 設定

| 環境變數 | 預設／用途 |
|---|---|
| `ECOFLOW_ACCOUNT` / `ECOFLOW_ACCOUNT_FILE` | 必填；中國區手機號，亦可依所選區域使用 email |
| `ECOFLOW_PASSWORD` / `ECOFLOW_PASSWORD_FILE` | 必填；App 密碼，原樣保留 |
| `ECOFLOW_SN` / `ECOFLOW_SN_FILE` | 必填；完整設備 SN |
| `ECOFLOW_DEVICE_NAME` | `river3`；metrics 的設備別名，不用帳號／SN 作標籤 |
| `ECOFLOW_API_HOST` | `api-cn.ecoflow.com`；地區須與帳號一致，非中國區未實測 |
| `EXPORTER_PORT` | `9098`；程式監聽埠，Compose 同步映射宿主相同埠 |
| `EXPORTER_LISTEN` | `0.0.0.0`；直接執行時可改，Compose 內固定全介面監聽 |
| `EXPORTER_BIND_ADDRESS` | `0.0.0.0`；Compose 宿主埠綁定地址 |
| `ECOFLOW_STALE_SECONDS` | `120`；資料過期門檻 |
| `ECOFLOW_READ_COOLDOWN_SECONDS` | `30`；按需 GET 後的等待時間，不是固定輪詢週期 |
| `ECOFLOW_RECONNECT_MIN_SECONDS` | `5`；斷線重試起始等待 |
| `ECOFLOW_RECONNECT_MAX_SECONDS` | `300`；斷線重試最長等待 |
| `ECOFLOW_AUTH_RETRY_SECONDS` | `300`；授權失敗的重試等待 |
| `LOG_LEVEL` | `INFO`；日誌級別 |

更改埠後，請同步修改 vmagent 的 target。每個 exporter 實例對應一組帳號和一台設備；多設備可啟動多個實例，設定不同設備別名與宿主埠。

## HTTP 端點

| 端點 | 用途 |
|---|---|
| `/metrics` | Prometheus exposition，供 vmagent 抓取 |
| `/healthz` | 程式存活檢查；Docker healthcheck 使用此端點 |
| `/readyz` | MQTT 已連線並訂閱，且 SOC 和 AC 接入旗標都有有效、未過期讀數時回 200；否則回 503 |

預設 HTTP 服務供內網監控抓取，沒有額外 HTTP 登入層。需要限制抓取來源時，可綁定指定宿主地址或使用既有反向代理。帳號、密碼、token 與完整 SN 不會輸出到 metrics。

## 驗證與範圍

25 項自動化測試通過；本機真實設備端到端驗證 150 秒，完成 15 次 HTTP scrape，持續取得有效 SOC／AC 旗標，只在啟動時 GET 一次。帳密不出現在 metrics。本開發環境沒有 Docker CLI／daemon，因此尚未實際 build 或運行 Linux 容器。詳細證據見 [VALIDATION.md](VALIDATION.md)。

Schema 來源與授權請見 `LICENSE`、`NOTICE` 及 `vendor/`。本專案與 EcoFlow 無隸屬關係，使用的 App 私有接口可能隨服務或韌體更新改變。
