# 中國區 RIVER 3 UPS：Mac Wi-Fi／雲端唯讀測試

本工具用於確認「能否採集」，尚不是正式告警服務。中國區登入＋certification＋這台設備的即時推送，仍需本機帳號實測。它不會修改 AC/DC 開關、充放電上限、韌體、設備網路或 DNS，也不會發送通知。

## 在這台 Mac 啟動

在 Finder 雙擊 `start.command`；或在這個目錄的 Terminal 執行：

```zsh
./start.command
```

啟動器使用 Python 3.10+，在專案本機的 `work/ecoflow-probe-runtime/` 建立獨立環境，安裝鎖定版本的 paho-mqtt／protobuf；不安裝全域套件。首次執行需要下載依賴；`work/` 不納入版本控制。

這台 Mac 的 Python 預設 CA 路徑不完整；啟動器會將 macOS 系統根憑證匯出到專案 `config/mac-system-roots.pem`，僅供這個進程驗證 TLS，不修改系統信任設定，也不停用驗證。直接執行 Python 時，先在同一個 Terminal 執行：

```zsh
export SSL_CERT_FILE="../../config/mac-system-roots.pem"
```

選項：

1. **中國區 App 雲端 MQTT：建議先試。**首次輸入完整設備 SN、中國區手機號和 App 密碼。登入後，帳號和 SN 保存至本機 `config/profile.json`（0600），密碼保存至 macOS 鑰匙圈，服務名稱為 `ecoflow-wifi-cloud-probe`；下次選 1 直接重用。此處是帳號密碼，不能用短信驗證碼；不要把帳密貼到聊天。先向 `api-cn.ecoflow.com/auth/login` 登入，再向同主機 `/iot-auth/app/certification` 取得 MQTT 憑證，使用回傳的 EcoFlow broker。預設只訂閱目標設備，持續 10 分鐘，不發布訊息。
2. **區網連通性。**輸入從路由器 DHCP 清單取得的設備 IP，只建立一次 TCP 8055 連線，不掃網段、不發設備指令。端口開啟也不代表 RIVER 3 支援早期協議；端口不通也可能由網路隔離造成。
3. **官方 API。**僅已有 AccessKey／SecretKey 時使用；與 App 帳密模式分開。預設測中國區，可能回 503。官方設備清單有此 SN，也不代表 quota 權限已開放。結果只列白名單指標，不列出設備清單與 SN。
4. **更換帳號／密碼／SN。**重新輸入，成功登入後更新保存設定。不會發出設備控制指令。
5. **查看最近一次採集中文摘要。**只讀既有結果檔，不登入、不建立第二條 MQTT 連線。也可雙擊 `view-current.command`。
6. **不開 App 的按需讀取測試。**連線並訂閱成功後，若尚未取得有效指標，立即向同一設備 `/get` 發送最小 Protobuf GET（只有 src、dest、seq、from，無設定 payload）；之後僅在重要資料過期時補讀，不按固定週期發送。一次嘗試後等待 30 秒；若仍無資料／仍過期，才再嘗試。開始前完全退出手機 App，測試中不要開設備頁；觀察能否獨立取得並持續更新。此路徑有 RIVER 3 開源實作依據，但這台國行機器能否藉此維持推送仍待實測。讀取回覆與非快取設備推送分開顯示，不自動把回覆當作新採樣。

macOS 可能要求解鎖或允許 Python 存取鑰匙圈。若取消／拒絕，工具會明確顯示密碼未保存或未能讀取，不會改存明文。現有舊版採集程序不會自動升級；它先前輸入的密碼沒有保存，下次仍需輸入一次才可存入鑰匙圈。

新版預設每 10 秒顯示繁體中文摘要：雲端連線、電量、AC 輸入／輸出、AC 接入回報與各欄位接收時間。詳細 JSON 仍保存在結果檔；若需要在 Terminal 顯示 JSON，可加 `--verbose`。

## 雲端測試順序

1. 確認 RIVER 3 已在官方 App 綁定並連接 Wi-Fi，在 App 能看到在線資料。
2. 選 1，等 `mqtt_connect` 和 `mqtt_subscribe` 的 `success` 都為 `true`。
3. 退出手機 App，觀察 `message` 是否持續出現，`decoded_live_messages` 是否增加。
4. 查看 `cms_batt_soc`／`bms_batt_soc`、`pow_get_ac_in`、`pow_get_ac_out`、`plug_in_info_ac_charger_flag` 和 `plug_in_info_ac_in_flag`。不同韌體可能不提供全部欄位。
5. 若要測掉電，請自行選擇不影響重要負載的時機，只切斷設備的市電輸入，保持採集與網路供電；觀察旗標，再恢復市電。工具不會自動操作設備。

**0 W 不能單獨判停電。AC 旗標的 0／1 語義仍需用你的實機拔插市電對照。**部分 RIVER 3 的 AC 電壓可能一直為 0，因此這個試驗不自動推導「市電正常／中斷」。

每個欄位都有獨立 `age_seconds` 和 `stale`；其他欄位更新不會刷新 AC 旗標。`ac_charger_flag_state` 的 `flag_on`／`flag_off` 僅指協議旗標，`unknown` 表示未取得、斷線或過期。MQTT 接收時間不等於設備採樣時間；`device_time_raw` 不擅自解釋成 Unix 時間。retained 訊息和 get_reply 一律標記 cached，不刷新即時旗標。

## 進階執行

以下在 Terminal 先切換到此工具目錄。若只訂閱沒資料，可再啟用按需唯讀 `latestQuotas` 請求：

```zsh
../../work/ecoflow-probe-runtime/bin/python probe.py cloud --seconds 600 --request-snapshot
```

針對「打開 App 才有推送」的對照測試，使用主選單 6，或：

```zsh
../../work/ecoflow-probe-runtime/bin/python probe.py cloud --seconds 600 --request-snapshot --request-format protobuf
```

既有純訂閱實測時間：17:22:35 訂閱成功，17:23:19 才開始出現指標，17:25:37 最後一次收到指標；使用者回報中途打開了手機設備頁。這支持 App 觸發暫時上報的假設，尚未證明固定租期或具體觸發指令。

工具只發布到當前 SN 的 `/thing/property/get`，不具備任意 topic 或 `set` 控制入口。讀回的 snapshot 可能是雲端快取，與即時 property 推送分開記錄；僅取得快取不能當作持續監控成功。

按需判斷依據為主要指標組：電量、AC 輸入、AC 輸出、AC 接入旗標。電量等同組候選欄位只要有一個仍新鮮，就不因另一個舊欄位觸發；從未上報的可選欄位不觸發。已收到的重要組若過期（預設 120 秒）則嘗試 GET。`--read-cooldown` 是請求後等待回覆的時間，預設 30 秒，並非固定輪詢；資料正常時完全不發送。快取回覆不會刷新即時欄位的時間。

已有官方海外區 Key 才測對應區域，不要盲目跨區反覆登入：

```zsh
../../work/ecoflow-probe-runtime/bin/python probe.py official --host api.ecoflow.com
```

## 結果與故障定位

雲端測試結果保存到 `results/cloud-*.jsonl`，權限 0600。結果檔只保存白名單指標、接收時間、封包種類與階段結果；不包含帳密、token、MQTT 密碼、完整 SN、User ID 或原始封包。結果目錄與本機 profile 已由根目錄 `.gitignore` 排除，不應上傳到公開倉庫。

- `/auth/login` 失敗：先核對手機號＋App 密碼；本工具不重試登入，不處理短信／圖形驗證。
- `/iot-auth/app/certification` 失敗：代表 App 憑證路徑尚未打通；不能據此認定設備本身不支援採集。
- MQTT 連接／訂閱失敗：核對憑證／權限／區域；工具不繞過 TLS。
- 有訊息、沒有解碼指標：可能是 schema／封包差異，先保留結果檔定位，不把缺失值填 0。
- 只有快取、App 關閉就無更新：尚不能通過不依賴 App 常駐的驗收。
- `decoded_live_messages=0` 只表示未取得可辨識的非 retained property 推送；若 `get_reply` 的 fields 有資料，代表快取讀取已成功，不能將兩者混為一談。
- 所有正常：再進行斷電、復電、斷網、重新連線驗證，之後才適合接正式告警。

本工具的第一版遇 MQTT 斷線會停止，沒有長期服務的自動重連／憑證刷新；適合短時間驗證。

2026-10-24 修正：首次國行實測已通過 App 登入與 MQTT 憑證取得，但 broker 在 CONNECT 階段回覆 Not authorized。已將 client ID 從上游未使用的長格式 helper，改為上游登入流程真正使用的 `ANDROID_<32位隨機十六進位>_<UserID>`；未改動帳號登入、broker 或 MQTT 憑證用法。是否能解決這台設備的連線拒絕，仍待重試確認。結果增加 reason 與 auth_profile，方便辨識新版測試。

## 來源與授權

- RIVER 3 cloud 協議／protobuf schema／get 請求與 client ID 格式：[`tolwi/hassio-ecoflow-cloud` de02d680](https://github.com/tolwi/hassio-ecoflow-cloud/tree/de02d680cdfe1adcec60c43047ff3ed3da15e0ee)，Apache-2.0。`vendor/` 的 `.proto` 和 `_pb2.py` 原樣保留，完整授權見 `vendor/LICENSE`。
- 中國區手機號登入參考：[`nphil/ha-ecoflow-iot` e4f14d20](https://github.com/nphil/ha-ecoflow-iot/blob/e4f14d204e44e49cf76e2e874e71d5ea22776a4f/custom_components/ecoflow_iot/eflib/login.py)；亦由 [`rabits/ha-ef-ble` v1.3.0](https://github.com/rabits/ha-ef-ble/blob/v1.3.0/custom_components/ef_ble/eflib/login.py) 交叉核對。本工具獨立實作其請求格式。
- 海外 RIVER 3 UPS App 雲端路徑使用案例：[`domhughes/ecoflow-river3-desk-ups-ha`](https://github.com/domhughes/ecoflow-river3-desk-ups-ha)。這不是國行實測證據。
- 舊型號 LAN TCP8055 專案：[`iTechMedic/ha-ecoflow-local`](https://github.com/iTechMedic/ha-ecoflow-local)，支援表沒有 RIVER 3。
- Protobuf GET 請求形狀：[`ioBroker RIVER 3 prepareProtoCmd`](https://github.com/foxthefox/ioBroker.ecoflow-mqtt/blob/9df54323e17a90e5c553ba5f10771667bd9ac4fb/lib/dict_data/ef_river3_data.js#L3225)。部分其他型號有同類 App 關閉後停更、定期 GET 恢復的[社群實測](https://github.com/tolwi/hassio-ecoflow-cloud/issues/830)，不代表已驗證國行 RIVER 3。
- 官方接口：[EcoFlow 開發文件](https://developer.ecoflow.com/us/document/generalInfo)。

本工具與 EcoFlow 無隸屬關係；App 私有接口可能變更。這些來源證明可試驗的協議路徑，並不代替你的國行設備實測。
