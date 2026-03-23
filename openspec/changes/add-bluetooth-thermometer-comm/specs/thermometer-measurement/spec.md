## ADDED Requirements

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
- **THEN** 系統發送指令要求 12 支耳溫槍量測 (Master + Slave)
- **AND** 系統擷取回傳溫度作為 "空槍值"
- **AND** UI 更新各通道的空槍值顯示
- **AND** 系統寫入 D515 = 0 通知 PLC 完成

#### Scenario: Temperature measurement trigger (D500)
- **WHEN** PLC D500 值變為 1
- **THEN** 系統發送指令要求 12 支耳溫槍量測
- **AND** 系統擷取回傳溫度作為 "量測值"
- **AND** 系統計算誤差值 (量測值 - 空槍值) 並判斷 PASS/FAIL
- **AND** 系統寫入 D500 = 0 通知 PLC 完成

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
系統 SHALL 將 12 通道的 PASS/FAIL 結果及統計數據寫入 PLC 暫存器。

#### PLC Result Registers
- **D501-D512:** 通道 1-12 判定結果 (0=PASS, 1=FAIL)
- **D517-D528:** 通道 1-12 OK 計數
- **D529-D540:** 通道 1-12 NG 計數

#### Scenario: Send results to PLC
- **WHEN** 系統完成 12 通道的 PASS/FAIL 判斷
- **THEN** 系統將各通道結果 (PASS=0, FAIL=1) 寫入 D501-D512
- **AND** 系統更新累計 OK/NG 計數至對應暫存器

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
