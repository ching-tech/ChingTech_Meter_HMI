# 程式流程規格

## 1. 啟動流程

```
main.py __main__
  ├─ 設定 Windows AppUserModelID
  ├─ init_managers()
  │   ├─ BluetoothManager(simulation_mode) → bt_manager
  │   │   └─ 註冊 on_data, on_state 回呼
  │   │   └─ add_device(): Master 加入 CH1-6, Slave 加入 CH7-12
  │   ├─ PLCManager(ip, port, simulation_mode) → plc_manager [僅 Master]
  │   │   └─ 註冊 on_empty, on_measure, on_state, on_reset 回呼
  │   ├─ NetworkManager(role, port, master_ip) → net_manager
  │   │   └─ 註冊 on_data, on_state 回呼
  │   ├─ MeasurementManager(channel_count, tolerance, log_dir) → measure_manager
  │   │   └─ 註冊 on_state, on_channel, on_complete 回呼
  │   ├─ bt_manager.start() → 每個設備啟動獨立連線執行緒
  │   ├─ plc_manager.start_monitoring() → 啟動 PLC 輪詢執行緒 [僅 Master]
  │   ├─ net_manager.start() → Master 監聽 / Slave 連線
  │   └─ measure_manager.start_new_batch() → 建立初始 CSV 檔案
  ├─ app.on_shutdown(handle_shutdown) → 註冊關閉處理
  └─ ui.run(native=True) → 啟動 NiceGUI 桌面視窗
      └─ Master port=8080, Slave port=8081
```

## 2. 量測主流程 (PLC 觸發)

### 2.1 空槍量測階段
```
PLC D515 上升緣 (0→1)
  └─ PLCManager._monitor_loop() 偵測
     └─ 回呼 on_plc_empty_trigger()
        ├─ measure_manager.start_empty_measurement()
        │   └─ 狀態: IDLE → WAITING_EMPTY
        ├─ [模擬模式] bt_manager.set_simulation_mode_empty()
        └─ threading.Timer(empty_collect_delay) → trigger_empty_ack_and_collect()
           ├─ 對每個啟用通道: bt_manager.request_measurement(ch)
           │   └─ 發送 CD 指令 (主動要求量測)
           │   └─ 耳溫槍回傳 DB 封包 → 解析溫度值
           │   └─ 觸發 on_bluetooth_data() 回呼
           │       ├─ 更新 UI 耳套狀態
           │       ├─ 儲存 ear_cover_statuses
           │       ├─ [Slave] 更新 empty_display (依量測狀態)
           │       └─ [Slave] net_manager.send_data() 轉發至 Master
           │                 (含溫度+耳套+BT狀態)
           ├─ time.sleep(0.2)
           └─ collect_empty_values()
              ├─ 收集本機藍芽資料 (bt_manager.get_last_data)
              ├─ [Master] 收集 Slave 網路資料 (net_manager.get_all_received_data)
              ├─ measure_manager.record_empty_values(values)
              │   └─ 狀態: WAITING_EMPTY → EMPTY_DONE
              ├─ save_cycle_log(is_empty=True) → 寫入 CSV 一列
              └─ plc_manager.clear_empty_trigger() → D515=0
```

### 2.2 溫度量測階段
```
PLC D500 上升緣 (0→1)
  └─ PLCManager._monitor_loop() 偵測
     └─ 回呼 on_plc_measure_trigger()
        ├─ measure_manager.start_temperature_measurement()
        │   └─ 狀態: EMPTY_DONE → WAITING_MEASURE
        ├─ [模擬模式] bt_manager.set_simulation_mode_measure()
        └─ threading.Timer(measure_collect_delay) → trigger_measure_ack_and_collect()
           ├─ 對每個啟用通道: bt_manager.request_measurement(ch)
           │   └─ [Slave] 更新 temp_display (依量測狀態)
           ├─ time.sleep(0.2)
           └─ collect_measure_values()
              ├─ 收集本機藍芽資料
              ├─ [Master] 收集 Slave 網路資料
              └─ measure_manager.record_measure_values(values)
                 ├─ 計算誤差: error = measure_value - empty_value
                 ├─ 判斷: -tolerance_lower ≤ error ≤ +tolerance_upper → PASS
                 ├─ 狀態: WAITING_MEASURE → MEASURING → COMPLETE
                 └─ 觸發 on_measurement_complete(result)
```

### 2.3 結果回寫階段
```
on_measurement_complete(result)
  ├─ Log: "PASS: X, FAIL: Y"
  ├─ measure_manager.save_cycle_log(is_empty=False)
  │   └─ 寫入 CSV 一列 (量測觸發)
  └─ [Master] PLC 回寫:
     ├─ 內部通道→邏輯通道映射 (透過 CHANNEL_DISPLAY_NAMES)
     ├─ plc_manager.write_results(logical_results) → D501-D512 (0=PASS, 1=FAIL)
     ├─ plc_manager.write_complete_signal() → D500=0
     └─ update_plc_display()
     注意: OK/NG 計數由 PLC 自行累加，HMI 不寫 D517~D540
```

### 2.4 Slave 端即時顯示
```
Slave on_bluetooth_data(channel, data)
  ├─ 更新耳套狀態 UI
  ├─ 判斷量測狀態:
  │   ├─ WAITING_EMPTY / EMPTY_DONE → 寫入 empty_display (空槍值)
  │   └─ 其他狀態 → 寫入 temp_display (量測值)
  └─ 發送封包至 Master (含耳套+BT狀態)

Slave on_bluetooth_state(channel, state)
  └─ 發送純 BT 狀態封包至 Master
```

## 3. 量測狀態機

```
IDLE ──(空槍觸發)──→ WAITING_EMPTY ──(記錄完成)──→ EMPTY_DONE
                                                        │
                                           (溫度觸發)───┘
                                                        ↓
COMPLETE ←──(判斷完成)── MEASURING ←──(記錄完成)── WAITING_MEASURE
   │
   └──(重設)──→ IDLE
```

| 狀態 | UI 顯示 | 說明 |
|------|---------|------|
| IDLE | 待機中 | 等待 PLC 觸發 |
| WAITING_EMPTY | 等待空槍 | 正在收集空槍值 |
| EMPTY_DONE | 空槍完成 | 空槍值已記錄，等待溫度觸發 |
| WAITING_MEASURE | 等待量測 | 正在收集溫度值 |
| MEASURING | 計算中 | 正在計算誤差與判斷 |
| COMPLETE | 量測完成 | 結果已回寫 PLC |

## 4. 定時更新迴圈

```
ui.timer(0.5s) → update_plc_display()
  ├─ 更新系統狀態標籤 (運行中/已停止)
  ├─ 更新 PLC 連線指示燈
  ├─ 更新各通道藍芽狀態
  ├─ [Master] 更新 PLC 監控面板:
  │   ├─ D500 量測觸發
  │   ├─ D515 空槍觸發
  │   ├─ D514 PC 心跳
  │   ├─ D516 測試週期
  │   ├─ D513 BT 錯誤遮罩
  │   └─ D541 異常復歸
  └─ [Master] 更新各通道 OK/NG 計數 (從 PLC D517~D540 讀取)
```

## 5. 使用者操作

### 換批 (on_new_batch_click)
```
點擊「換批」按鈕
  ├─ measure_manager.start_new_batch() → 建立新 CSV
  ├─ [Master] plc_manager.write_ok_ng_counts([0]*12, [0]*12) → 歸零 D517-D540
  └─ 更新批次標籤
```

### 重設資料 (on_reset_click)
```
點擊「重設資料」按鈕
  ├─ measure_manager.reset() → 狀態回 IDLE, 清空通道資料
  ├─ UI 所有通道歸零 (空槍/量測/誤差/狀態)
  ├─ [Master] OK/NG 顯示歸零
  ├─ plc_manager.write_ok_ng_counts([0]*12, [0]*12)
  ├─ 停止警告閃爍
  └─ [模擬模式] bt_manager.reset_simulation()
```

### HMI 異常復歸 (on_plc_reset)
```
PLC D541 上升緣
  └─ on_plc_reset() → on_reset_click() (等同手動重設)
```

### 更新資料 (on_apply_settings)
```
點擊「更新資料 (即時套用)」按鈕
  ├─ 從 UI 收集所有設定值 → 寫入 config → 存檔
  ├─ 量測管理器: 更新誤差容許值
  ├─ 通道啟用狀態: 更新 UI 顯示
  ├─ 藍芽: 更新 MAC 位址 (不重啟，下次重連生效)
  ├─ PLC: 更新 IP/Port (不重啟，下次連線生效)
  ├─ 網路: 更新 master_ip/port
  └─ 時序參數: 直接生效 (程式讀取 config.timing.*)
  注意: 不重啟管理器，避免執行緒衝突導致連線異常
```

## 6. 關閉流程

```
app.on_shutdown → handle_shutdown()
  ├─ is_shutting_down = True
  ├─ bt_manager.stop() → 中斷所有藍芽連線
  ├─ plc_manager.stop_monitoring() → 停止 PLC 輪詢、斷開連線
  ├─ net_manager.stop() → 關閉網路 Socket
  └─ os._exit(0) → 強制結束
```
