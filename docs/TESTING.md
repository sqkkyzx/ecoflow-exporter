# 測試與證據索引

## Mac 採集工具

位於 `probe/ecoflow-wifi-cloud-probe/`。先在專案根目錄安裝 `requirements.txt`，再按工具自己的依賴說明執行。

- `probe.py`：中國區 App login、MQTT certification、RIVER 3 protobuf 解碼和按需 GET。
- `credential_store.py`：macOS Keychain 密碼保存。
- `profile_store.py`：手機號／SN 本機設定保存。
- `console_view.py`：繁體中文摘要。
- `read_recovery.py`：無資料／重要欄位過期才觸發 GET。
- `test_*.py`：解碼、Keychain mock、profile、摘要、新鮮度和 recovery 測試。
- `results/`：歷次採集 JSONL。

執行：

```sh
python -m unittest discover -s probe/ecoflow-wifi-cloud-probe -p 'test_*.py' -v
```

## Exporter 自動化測試

根目錄的 `test_exporter.py`、`test_metrics.py` 共覆蓋：

- 32 項 exporter／metrics／HTTP／MQTT mock 測試通過，包含 `/probe` 配置解析、session 復用、並發去重和 idle TTL 回收。
- secrets 不進 metrics、日誌或設定 repr。
- 快取／retained 不刷新即時值。
- 必要 SOC／AC flag 缺失會觸發 recovery。
- 過期資料不輸出物理數值。
- 斷線重連復用 login/cert，授權拒絕才刷新 cert。
- Docker healthz 與 readyz 行為。

執行：

```sh
python -m unittest discover -v
```

## 真實設備端到端測試

`validation/live_smoke.py` 使用本機 Keychain 密碼，只在本機進程記憶體中運行；摘要與 metrics 輸出寫入 `work/ecoflow-live-validation/`，不會改動追蹤中的檔案。執行前需在 macOS 設定本機 Keychain profile。

既有真實設備測試完成 150 秒、15 次 HTTP scrape、110 MQTT 訊息和 85 筆即時欄位；這份歷史測試早於 `/probe` session pool。2026-10-27 以新 `/probe` 重跑時，請求共 15 次但 EcoFlow login 一直回 network 錯誤，未建立 MQTT session，因此新 session pool 的真實設備驗證未通過。離線測試已驗證 `/probe` session 復用且不回傳 query 中的密碼或 SN。

Docker image build/run 尚未驗證，因本機沒有 Docker CLI/daemon。
