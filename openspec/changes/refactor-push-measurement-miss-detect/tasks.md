## 1. 設定與基礎

- [x] 1.1 `config.py` 的 `BluetoothConfig` 新增 `miss_timeout: float = 3.0`（漏壓判定逾時，秒；缺欄位時走預設）
- [x] 1.2 進階設定 UI 新增「漏壓逾時」輸入框（可調），並接上載入/儲存（`_collect_settings_from_ui` / `_refresh_ui_from_config`）

## 2. 手動旗標（區分生產 vs 手動）

- [x] 2.1 新增模組旗標 `_manual_trigger`（set/consume）
- [x] 2.2 `on_simulate_empty` / `on_simulate_measure` 在寫 D515/D500 前設旗標
- [x] 2.3 `on_plc_empty_trigger` / `on_plc_measure_trigger` 讀取並消費旗標，決定生產/手動模式

## 3. 取值核心：純推送 + 新鮮度 + timeout 放行

- [x] 3.1 新增「上一輪已採用推送時間戳」基準（per channel），收集完成後更新
- [x] 3.2 `_channel_latest_ts` 沿用：本機 BT 用 last_data、Slave 用 `_net_data_recv_at`（皆 Master 時鐘）
- [x] 3.3 重寫 `_acquire_and_collect`：生產模式不發 CD、等待至多 `miss_timeout`、全到齊提早結束、逾時放行
- [x] 3.4 手動模式分支：發 CD 取快取值、不套用漏壓偵測
- [x] 3.5 移除生產路徑的「無限重試」邏輯（`collect_empty_values` / `collect_measure_values` 不再每 0.5s 重排）

## 4. 漏壓偵測與回報

- [x] 4.1 收集後計算漏壓通道集合（啟用但本輪無新推送者）
- [x] 4.2 漏壓通道內部值留 None（不寫 0、不沿用舊值）
- [x] 4.3 漏壓通道寫 D501~D512 = NG(1)
- [x] 4.4 設 D513 bit15 = ON（沿用 `set_d513_bit`）；時機對齊 bit12/13/14（reset trigger 前）
- [x] 4.5 跳警報 `未收到量測值: CHx（未壓到/未觸發）`（多支列出）
- [x] 4.6 異常復歸（D513 全清）一併清除 bit15；每輪開始重新評估
- [x] 4.7 漏壓時仍照常寫 PLC/LOG 並將 D500/D515 歸 0（流程不卡）

## 5. CSV / Log

- [x] 5.1 `measurement.py:save_cycle_log` 的 `get_temperature`：漏壓（None）通道輸出空白字串；停用通道維持 0
- [x] 5.2 確認 log 格式/欄位/檔名/路徑/非同步佇列未變；正常循環內容與重構前一致

## 6. 清理 CD / CB

- [x] 6.1 `bluetooth_comm.py` 移除 CB 死碼（`send_ack` / `build_cb_response`）或標註不使用
- [x] 6.2 `request_measurement`（CD）限縮為僅手動模式呼叫；視情況簡化 `_cd_sent_at` / `consume_cd_flag` 與「主動推送/CD回應」標籤

## 7. 驗證

- [ ] 7.1 模擬/手動模式：取值正常、漏壓旗標、CSV 空白
- [ ] 7.2 實機單支：正常壓桿（主動推送、log 正常、握手正常）
- [ ] 7.3 實機刻意漏壓一支：bit15 ON、該支 NG 噴 NG port、警報、CSV 空白、流程不卡、握手照常 reset
- [ ] 7.4 回歸：正常滿載循環，log 與重構前一致
- [ ] 7.5 邊界：同一支兩階段皆漏不會假 PASS；整台全漏壓不被當手動模式
