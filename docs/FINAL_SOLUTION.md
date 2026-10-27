# EcoFlow RIVER 3 UPS 最終落地方案

## 目標

在手機 App 不常駐、甚至完全退出時，持續取得中國區「睿 RIVER 3 UPS」的電量、AC 輸入和 AC 接入狀態，交給 vmagent，再由既有 VictoriaMetrics／Grafana／vmalert 告警。

## 最終架構

```text
RIVER 3 UPS（R655）
  → EcoFlow 中國區 App 雲端 MQTT（TLS）
  → ecoflow-exporter:9098/metrics
  → vmagent
  → VictoriaMetrics / vmalert / 通知
```

Exporter 在背景維持一條 MQTT session；HTTP `/metrics` 只讀記憶體，不會因每次 scrape 登入或請求設備。

## 按需補讀

Exporter 不採用固定每 60 秒請求。連線和訂閱成功後：

- 沒有任何有效非快取資料，發送一次空 Protobuf GET。
- SOC 或原生 AC charger flag 未收到，繼續視為缺資料並按冷卻時間補讀。
- 已收到的重要欄位過期，發送一次空 Protobuf GET。
- 資料正常時不發送。
- 每次嘗試後預設等待 30 秒；get_reply、retained 和快取回覆不刷新即時欄位時間。

這個 GET 只含 `src`、`dest`、`seq`、`from`，沒有 `pdata`、`cmd_func`、`cmd_id` 或設備設定，不包含任何控制命令。

## 核心判斷

- AC 是否接入只使用 `plug_in_info_ac_charger_flag`。
- 不用 AC 輸入功率為 0、AC 電壓為 0 或輸出功率推導停電。
- 每個欄位獨立計算新鮮度；其他欄位更新不會刷新 AC flag。
- 過期、斷線或未訂閱時省略物理數值 series，另保留 `available`、`age`、連線和錯誤指標。
- AC 輸出功率用絕對值展示；原始方向不作市電判斷。

## 部署

在專案根目錄執行以下命令。預設埠為 9098。帳密可用環境變數或 `_FILE` secrets；vmagent 只抓 `/metrics`，不持有 EcoFlow 帳密。

```sh
cp .env.example .env
chmod 600 .env
docker compose up -d --build
```

密碼亦可使用 `compose.secrets.yaml`，避免特殊字元被 `.env` 解析。部署檔未使用 privileged、host network、USB 或藍牙。

## 已知邊界

- 已實測目標是中國區 RIVER 3 UPS，SN 前綴 R655；R654 使用同一 schema，可配置嘗試。
- 官方 `iot-open` API 在中國區實測返回服務不可用；此方案使用 App 帳密登入和 App MQTT certification，不是官方 AK/SK API。
- RIVER 3 UPS 官方手冊沒有 Plus 的 USB-B／Power Manager 通訊路徑，因此沒有套用 RIVER 3 Plus 的 USB/NUT 方案。
- Docker image build、容器運行和 VM/CT 部署尚未在本機驗證；本機 Docker CLI/daemon 不存在。
