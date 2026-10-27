# 驗證記錄

日期：2026-10-24（Asia/Shanghai）。版本：0.1.0。

## 自動化測試

25 項測試通過，包含：

- 環境變數及 `_FILE`、密碼空白保留、設定錯誤不回顯敏感內容。
- 中國區手機號登入格式、MQTT broker 網域檢查。
- HTTP `/metrics`、`/healthz`、`/readyz`；重複 scrape 不觸發重複登入。
- 快取／retained／get_reply 不刷新即時資料；缺失與過期數值不補 0。
- 原生 AC 旗標、最新 SOC 候選、AC 輸出功率絕對值。
- 無資料、必要 AC 旗標缺失、資料過期才 GET；正常資料不固定輪詢。
- 斷線清除舊 session 的有效數值；網路重連復用登入／MQTT 憑證並退避。
- MQTT 授權拒絕後刷新憑證，避免反覆密碼登入。
- 主機時鐘回撥後不把未來時間戳當成有效當前讀數。

執行：

```sh
python -m unittest discover -v
```

## 真實設備、原生 HTTP 端到端測試

使用既有已授權的中國區 RIVER 3 UPS 設定，從 Mac 鑰匙圈在測試進程記憶體中載入密碼；帳密、SN、token 不寫入交付包。

| 項目 | 結果 |
|---|---|
| 本機 HTTP 監聽 | `127.0.0.1:9098` |
| 測試時長 | 150 秒 |
| `/metrics` 抓取 | 15 次，HTTP 200，可解析 Prometheus exposition |
| 結束時 SOC／AC 旗標 | 均有效且未過期 |
| MQTT 訊息 | 110 筆 |
| 含即時指標的訊息 | 85 筆 |
| 啟動無資料 GET | 1 次 |
| 過期 GET | 0 次 |
| App 登入／MQTT 憑證取得 | 各 1 次 |
| 解碼／授權／讀取錯誤 | 0 次 |
| 帳密、完整 SN 出現在 metrics | 未出現，逐次抓取驗證 |

測試後已正常停止本機 exporter，未留下背景服務。指標樣本只保留在本機的忽略路徑；斷線與授權失敗情境由離線測試覆蓋，沒有對真實設備或網路做斷線操作。

## Docker 與 vmagent 設定

- `compose.yaml`、`compose.secrets.yaml`、`vmagent.yml` 的 YAML 解析及關鍵配置一致性檢查通過。
- Dockerfile 的 COPY 來源均存在，映像 build context 使用白名單，排除 `.env`、secrets、Mac 設定及採集日誌。
- Python 原始碼符合 Python 3.12 語法；原生測試使用 Mac Python 3.13。
- 本機沒有 Docker CLI／daemon，**尚未實際 build 或運行 Linux 容器**。
- 沒有部署、改動任何 VM／CT，也未建立或提交 PR。
