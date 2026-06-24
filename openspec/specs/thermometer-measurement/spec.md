# Thermometer Measurement

### Requirement: Bluetooth SPP Thermometer Connection
系統 SHALL 透過經典藍芽 SPP 協定連接耳溫槍設備，每台電腦支援最多 6 支耳溫槍同時連線。

#### Protocol Details
- **Connection:** Bluetooth RFCOMM (SPP), Channel 1.
- **Packet Structure:** `STX(0x02) + Command + Data + Checksum(XOR) + ETX(0x03) + EOT(0x04)`
- **Commands:**
  - `CD`: Command Request (Host to Device) - 要求量測
  - `CB`: Command Back (Host to Device) - 回應 ACK
  - `DB`: Data Back (Device to Host) - 回傳量測資料
- **Data Parsing:** Includes `MeterID`, `Temperature`, `TransTemperature`, `EarCoverStatus`.

#### Scenario: Successful connection to thermometer
- **WHEN** 使用者啟動系統且耳溫槍已開機配對
- **THEN** 系統自動建立藍芽 SPP 連線
- **AND** UI 顯示該通道連線狀態為藍燈 (Connected)

#### Scenario: Connection lost and auto-reconnect
- **WHEN** 藍芽連線中斷
- **THEN** 系統自動嘗試重新連線
- **AND** UI 顯示該通道連線狀態為黃燈 (Connecting) 或紅燈 (Error)
- **AND** PLC D513 暫存器對應位元設為 1 (異常)

### Requirement: Ear Cover Detection
系統 SHALL 解析耳溫槍回傳資料中的耳套狀態。

#### Scenario: Ear cover detected
- **WHEN** 收到量測資料且 `trans_temp_raw` 為 "1111"
- **THEN** UI 顯示 "有耳溫套" (綠色)

#### Scenario: No ear cover detected
- **WHEN** 收到量測資料且 `trans_temp_raw` 為 "0000"
- **THEN** UI 顯示 "無耳溫套" (紅色)

### Requirement: PLC Trigger Signal Integration
系統 SHALL 監聽三菱 5U PLC (3E Protocol) 透過 MC Protocol 發送的觸發訊號。

#### PLC Register Mapping
- **D500:** 溫度量測觸發 (1=Trigger, 0=Complete)
- **D515:** 空槍量測觸發 (1=Trigger, 0=Complete)
- **D514:** PC 心跳訊號 (每秒切換 0/1)
- **D541:** 異常復歸訊號

#### Scenario: Empty gun measurement trigger (D515)
- **WHEN** PLC D515 值變為 1
- **THEN** 系統以「本輪觸發後收到的新推送值」作為各啟用通道的空槍值（不發送 CD）
- **AND** 系統等待至多 `bluetooth.miss_timeout`，所有啟用通道收齊即提早結束
- **AND** UI 更新各通道的空槍值顯示
- **AND** 完成判定與寫入後，系統寫入 D515 = 0 通知 PLC 完成

#### Scenario: Temperature measurement trigger (D500)
- **WHEN** PLC D500 值變為 1
- **THEN** 系統以「本輪觸發後收到的新推送值」作為各啟用通道的量測值（不發送 CD）
- **AND** 系統等待至多 `bluetooth.miss_timeout`，所有啟用通道收齊即提早結束
- **AND** 系統計算誤差值 (量測值 - 空槍值) 並判斷 PASS/FAIL
- **AND** 完成判定與寫入後，系統寫入 D500 = 0 通知 PLC 完成

#### Scenario: Trigger completes even when a channel is missed
- **WHEN** 等待達 `bluetooth.miss_timeout` 後仍有啟用通道未收到本輪新推送
- **THEN** 系統 SHALL 停止等待並繼續完成判定（不再無限重試、不卡住流程）
- **AND** 系統 SHALL 照常寫入 PLC 結果與 LOG 後將 D500/D515 歸 0 完成握手

### Requirement: Error Tolerance PASS/FAIL Judgment
系統 SHALL 根據使用者設定的誤差上限與下限值判斷各通道 PASS/FAIL。

#### Scenario: Measurement within tolerance (PASS)
- **WHEN** 誤差值 (溫度量測值 - 空槍值) 介於誤差下限與上限之間
- **THEN** 該通道判定為 PASS
- **AND** UI 顯示綠燈與 "PASS" 文字

#### Scenario: Measurement outside tolerance (FAIL)
- **WHEN** 誤差值超出誤差上限或低於誤差下限
- **THEN** 該通道判定為 FAIL
- **AND** UI 顯示紅燈與 "FAIL" 文字
- **AND** 異常訊息寫入 Log

### Requirement: PLC Result Feedback
系統 SHALL 將 12 通道的 PASS/FAIL 結果及統計數據寫入 PLC 暫存器。漏壓（未收到本輪新推送）的通道 SHALL 以 NG 回報，並 SHALL NOT 沿用任何舊值。

#### PLC Result Registers
- **D501-D512:** 通道 1-12 判定結果 (0=OK, 1=NG, 2=不使用)
- **D517-D528:** 通道 1-12 OK 計數
- **D529-D540:** 通道 1-12 NG 計數
- **D513 bit15:** 漏壓旗標（任一通道漏壓即 ON）

#### Scenario: Send results to PLC
- **WHEN** 系統完成 12 通道的 PASS/FAIL 判斷
- **THEN** 系統將各通道結果 (OK=0, NG=1, 不使用=2) 寫入 D501-D512
- **AND** 系統更新累計 OK/NG 計數至對應暫存器

#### Scenario: Missed channel reported as NG
- **WHEN** 某啟用通道於本輪未收到新推送（漏壓）
- **THEN** 系統 SHALL 將該通道 D501-D512 寫入 NG(1)（PLC 既有邏輯將該耳溫套噴至 NG port）
- **AND** 系統 SHALL 將該通道內部空槍值/量測值留為 None，不沿用舊值
- **AND** 系統 SHALL NOT 因該通道為 0 或舊值而誤判為 PASS

### Requirement: Master-Slave Data Aggregation
電腦 A (Master) SHALL 透過 TCP 網路接收電腦 B (Slave) 的 6 通道溫度數據，彙整成完整 12 通道數據。

#### Channel Mapping
- **Master (PC A):** 連接實體通道 1-6，對應 UI 顯示 CH11, CH9, CH7, CH5, CH3, CH1
- **Slave (PC B):** 連接實體通道 7-12，對應 UI 顯示 CH12, CH10, CH8, CH6, CH4, CH2

#### Scenario: Slave sends data to Master
- **WHEN** 電腦 B 收到藍芽量測數據
- **THEN** 電腦 B 透過 TCP 傳送數據封包 (含 MeterID, 溫度, 時間戳) 至電腦 A
- **AND** 電腦 A 彙整 12 通道數據後進行統一判斷與 PLC 溝通

### Requirement: User Configurable Tolerance Settings
使用者 SHALL 能夠在 UI 設定誤差上限值與誤差下限值及其他系統參數。

#### Scenario: Set tolerance limits
- **WHEN** 使用者在 "進階設定" 面板輸入誤差上限與下限數值
- **THEN** 系統儲存設定 (`config.json`) 並套用於後續判斷
- **AND** 設定值在系統重啟後保留

### Requirement: Missed Press Detection (漏壓偵測)
系統 SHALL 偵測「啟用通道於本輪 D515/D500 觸發後未收到新推送」之漏壓情況，並以與既有 D513 異常位元（bit12 溫度異常、bit13 連續無套、bit14 空槍異常）一致的時機回報——皆於收集判定階段、寫入 PLC 結果與 reset D500/D515 之前完成。判定「新推送」SHALL 以「該通道資料時間戳是否在本輪觸發之後更新」為準，且 SHALL 使用可與觸發時間在同一時鐘比較的基準（本機 BT 用 last_data 時間戳、Slave 通道用 Master 收到封包時刻），以避免跨機器時鐘偏移。

#### Scenario: Single channel missed
- **WHEN** 啟用通道 CHx 於本輪觸發後達 `bluetooth.miss_timeout` 仍未收到新推送
- **THEN** 系統 SHALL 設定 PLC D513 bit15 = ON
- **AND** 系統 SHALL 跳出警報訊息 `未收到量測值: CHx（未壓到/未觸發）`
- **AND** 其他正常收到推送的通道 SHALL 照常判定與寫入

#### Scenario: Multiple channels missed
- **WHEN** 多個啟用通道於本輪皆未收到新推送
- **THEN** 警報訊息 SHALL 列出所有漏壓通道，例如 `未收到量測值: CH3, CH5（未壓到/未觸發）`
- **AND** D513 bit15 SHALL 為 ON

#### Scenario: Missed flag cleared on reset
- **WHEN** 操作員按下異常復歸（D513 全清）
- **THEN** D513 bit15 SHALL 一併清除

#### Scenario: Both phases missed does not false-pass
- **WHEN** 同一通道在空槍與量測兩階段皆漏壓
- **THEN** 該通道空槍值與量測值皆為 None，系統 SHALL NOT 計算誤差、SHALL NOT 判為 PASS（自然落入 NG）

### Requirement: Measurement Value Acquisition Mode (取值模式)
系統 SHALL 以明確的「手動旗標」區分生產模式與手動模式，而非以「是否已有推送」推測。差異僅在於「是否發送 CD」：生產模式（真實 PLC 觸發）SHALL NOT 發送 CD，純等主動推送；手動模式（UI 手動擷取，直接寫 D515/D500、無實體壓桿）SHALL 發送 CD 取得耳溫槍快取值。發送 CD 與否之後，兩種模式 SHALL 一致地以新鮮度判斷收齊並執行漏壓偵測（未收到本輪新值的啟用通道即漏壓，統一回報 D513 bit15 + NG + 警報），以確保「未取得值」的通道在兩模式下行為一致。系統 SHALL NOT 在任何流程發送 CB(ACK)。

#### Scenario: Production trigger uses push only
- **WHEN** 觸發來自真實 PLC（未設手動旗標）
- **THEN** 系統 SHALL NOT 發送 CD
- **AND** 系統 SHALL 純依主動推送取值並執行漏壓偵測（即使全部通道皆漏壓，亦不退回手動模式）

#### Scenario: Manual capture uses CD then same miss detection
- **WHEN** 使用者按下 UI「手動擷取」（設手動旗標後寫 D515/D500）
- **THEN** 系統 SHALL 對啟用通道發送 CD 取得快取值
- **AND** 之後 SHALL 與生產一致地以新鮮度判斷收齊並執行漏壓偵測
- **AND** 未回應 CD（未收到本輪新值）的通道 SHALL 一律判漏壓並回報（D513 bit15 + NG + 警報），不論本機或 Slave 通道行為一致

#### Scenario: Slave state-only packet not treated as measurement
- **WHEN** 某 Slave 通道最新收到的是純 BT 狀態封包（temperature=0.0 且無耳套資訊）
- **THEN** 系統 SHALL NOT 將該 0.0 當作量測值（避免誤觸假空槍異常）
- **AND** 該通道於本輪若無真量測新值 SHALL 判為漏壓

#### Scenario: Collect delay is configurable
- **WHEN** 使用者於進階設定調整「空槍/溫度收集延遲」
- **THEN** D515/D500 觸發後到開始收集的延遲 SHALL 採用 `config.timing.empty_collect_delay` / `measure_collect_delay`（設 0 即無延遲）

#### Scenario: CB never sent
- **WHEN** 系統收到任何 DB 量測資料封包
- **THEN** 系統 SHALL NOT 回送 CB(ACK)（實測確認耳溫槍無需 ACK 即正常運作）

### Requirement: Missed Channel Logging (漏壓 CSV 紀錄)
漏壓通道於 CSV 與遠端 log SHALL 以空白欄位呈現，以與「停用通道寫 0」區分；log 的格式、欄位、檔名、路徑與非同步寫入機制 SHALL 維持不變。本機與遠端 alarm log 檔名 SHALL 一致加上機台名後綴。

#### Scenario: Missed channel logged as blank
- **WHEN** 寫入單次循環 log 且某通道為漏壓（值為 None）
- **THEN** 該通道欄位 SHALL 留空白（非 0）
- **AND** 其餘欄位、格式、檔名與路徑 SHALL 與既有行為相同

#### Scenario: Normal cycle logging unchanged
- **WHEN** 一個循環所有啟用通道皆正常收到推送
- **THEN** CSV 與遠端 log 內容 SHALL 與重構前完全相同
