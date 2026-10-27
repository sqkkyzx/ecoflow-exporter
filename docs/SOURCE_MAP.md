# 專案來源與資料索引

所有路徑都相對於專案根目錄；本文件不依賴開發機的絕對路徑。

| 內容 | 路徑 |
|---|---|
| Exporter 程式 | `exporter.py`, `metrics.py`, `protocol.py`, `recovery.py` |
| Docker 部署 | `Dockerfile`, `compose.yaml` |
| vmagent 抓取範例 | `vmagent.yml` |
| Mac 採集工具 | `probe/ecoflow-wifi-cloud-probe/` |
| 協議 schema | `vendor/`, `probe/ecoflow-wifi-cloud-probe/vendor/` |
| 自動化測試 | `test_exporter.py`, `test_metrics.py`, `probe/ecoflow-wifi-cloud-probe/test_*.py` |
| 驗證記錄 | `VALIDATION.md`, `validation/` |
| 本機 profile、憑證快取與採集結果 | `config/`、`probe/.../results/`；已由 `.gitignore` 排除 |
| 本機虛擬環境與舊交付包 | `work/`、`artifacts/`；已由 `.gitignore` 排除 |

本專案不應提交真實帳號、密碼、設備 SN、token、採集原始資料或本機憑證。正式部署時，帳號、密碼和 SN 應放在 Prometheus/vmagent 的受限 target 配置中。
