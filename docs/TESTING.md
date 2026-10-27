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

- 25 項 exporter／metrics／HTTP／MQTT mock 測試通過。
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

已完成 150 秒本機 HTTP 測試：15 次 `/metrics` scrape、110 MQTT 訊息、85 筆含即時欄位、啟動 GET 1 次、login 1 次、解碼錯誤 0、metrics 未出現帳密或完整 SN。

Docker image build/run 尚未驗證，因本機沒有 Docker CLI/daemon。
