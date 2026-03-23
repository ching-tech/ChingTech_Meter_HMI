# -*- coding: utf-8 -*-
"""
擎添耳溫槍探頭套檢測系統 - 主程式
"""
import sys
import os
import socket
import ctypes
import multiprocessing

# 設定 Windows 工作列獨立圖示（不跟 python.exe 共用）
ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('com.chingtech.meter-hmi')
import datetime
import threading
import asyncio
import time

# --- CMD 輸出同時寫入 log 檔 ---
class _TeeWriter:
    """同時輸出到 console 和 log 檔案"""
    def __init__(self, original, log_file):
        self.original = original
        self.log_file = log_file
    def write(self, text):
        self.original.write(text)
        try:
            self.log_file.write(text)
            self.log_file.flush()
        except Exception:
            pass
    def flush(self):
        self.original.flush()
    def isatty(self):
        return self.original.isatty()
    def fileno(self):
        return self.original.fileno()
    def __getattr__(self, name):
        return getattr(self.original, name)

if multiprocessing.current_process().name == 'MainProcess':
    os.makedirs('logs', exist_ok=True)
    _log_filename = f'logs/debug_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.txt'
    _log_file = open(_log_filename, 'w', encoding='utf-8')
    sys.stdout = _TeeWriter(sys.stdout, _log_file)
    sys.stderr = _TeeWriter(sys.stderr, _log_file)

from nicegui import ui, app

from config import config, save_config, CHANNEL_DISPLAY_NAMES, get_channel_display_name
from bluetooth_comm import BluetoothManager, ConnectionState, ThermometerData
from plc_comm import PLCManager, PLCConnectionState
from network_comm import NetworkManager, NetworkRole, NetworkState, MeterDataPacket
from measurement import MeasurementManager, MeasurementState, JudgeResult, JudgeMode, ChannelData

# --- 全域變數 ---
is_shutting_down = False
prev_bt_states = {}
slave_bt_connecting_since = {}  # Slave 通道進入 CONNECTING 的時間戳 {ch: timestamp}
ear_cover_statuses = {}  # 儲存各通道最新的耳套狀態 (1111/0000)
no_cover_consecutive = {}  # 各通道連續無套計數 {ch: int}
temp_anomaly_active = False  # 溫度異常狀態
no_cover_anomaly_active = False  # 連續無套異常狀態
empty_out_of_range_count = 0    # 暖槍時空槍超限累計次數
_pending_bt_sync = set()        # Slave: 待補送藍芽狀態的通道
_d500_triggered_at = 0.0        # D500=1 觸發時間戳 (0=未觸發)
_d515_triggered_at = 0.0        # D515=1 觸發時間戳 (0=未觸發)
_TRIGGER_TIMEOUT = 15.0         # 觸發信號超時門檻 (秒)
managers_initialized = False
meters_ui = {}          
log_console = None      
bt_manager = None       
plc_manager = None      
net_manager = None      
measure_manager = None  

# --- UI 狀態元件 ---
plc_status_icon = None
network_status_icon = None
system_status_label = None  
measure_status_label = None 
current_batch_label = None  # 顯示目前批次檔名
plc_monitor_ui = {}         
system_running = False
slave_channel_enabled = {}  # Slave 回報的通道啟用狀態 {ch: bool}

# --- 設定面板元件 ---
SETTINGS_PASSWORD = "36274806"  # 進階設定密碼
settings_logged_in = False
settings_drawer = None
protected_sections = []  # 需要密碼保護的 UI 區塊
timing_inputs = {}
plc_inputs = {}
bt_inputs = {}
bt_mac_inputs = {}
net_inputs = {}              
channel_switches = {}       
mode_select = None          
tolerance_upper_input = None
tolerance_lower_input = None
empty_upper_input = None
empty_lower_input = None
temp_anomaly_switch = None
temp_anomaly_upper_input = None
temp_anomaly_lower_input = None
temp_anomaly_fields = None
no_cover_anomaly_switch = None
no_cover_anomaly_count_input = None
no_cover_anomaly_fields = None

# --- 異常警告元件 ---
alert_container = None
alert_message_label = None
alert_flash_timer = None
is_alert_visible = True  

def init_managers():
    """初始化各管理器並自動啟動服務"""
    global bt_manager, plc_manager, net_manager, measure_manager, system_running, managers_initialized

    if managers_initialized:
        return
    managers_initialized = True

    # 藍芽管理器
    bt_manager = BluetoothManager(
        simulation_mode=config.simulation_mode,
        connect_timeout=config.bluetooth.timeout,
        reconnect_interval=config.bluetooth.reconnect_interval
    )
    bt_manager.set_callbacks(on_data=on_bluetooth_data, on_state=on_bluetooth_state, is_channel_enabled=is_channel_enabled, get_channel_name=get_channel_display_name)

    if config.network.mode == "master":
        for i in range(1, 7):
            addr = config.bluetooth.device_addresses[i-1] if i <= len(config.bluetooth.device_addresses) else ""
            bt_manager.add_device(i, addr)
    else:
        for i in range(7, 13):
            idx = i - 7
            addr = config.bluetooth.device_addresses[idx] if idx < len(config.bluetooth.device_addresses) else ""
            bt_manager.add_device(i, addr)

    # PLC 管理器
    if config.network.mode == "master":
        plc_manager = PLCManager(
            ip_address=config.plc.ip_address,
            port=config.plc.port,
            simulation_mode=config.simulation_mode
        )
        plc_manager.set_callbacks(
            on_empty=on_plc_empty_trigger,
            on_measure=on_plc_measure_trigger,
            on_state=on_plc_state,
            on_reset=on_plc_reset
        )

    # 網路管理器
    role = NetworkRole.MASTER if config.network.mode == "master" else NetworkRole.SLAVE
    net_manager = NetworkManager(role=role, port=config.network.port, master_ip=config.network.master_ip)
    net_manager.set_callbacks(on_data=on_network_data, on_state=on_network_state, on_command=on_network_command, on_channel_enabled=on_slave_channel_enabled)

    # 量測管理器 (Slave 不需要寫入 CSV)
    is_master = config.network.mode == "master"
    measure_manager = MeasurementManager(
        channel_count=config.measurement.meter_count,
        tolerance_upper=config.measurement.tolerance_upper,
        tolerance_lower=config.measurement.tolerance_lower,
        log_dir=config.log_dir,
        enable_logging=is_master
    )
    measure_manager.set_callbacks(
        on_state=on_measurement_state,
        on_channel=on_channel_update,
        on_complete=on_measurement_complete
    )

    # 啟動服務
    log_message("系統服務啟動中...")
    bt_manager.start()
    if plc_manager and config.plc.enabled:
        plc_manager.start_monitoring()
    net_manager.start()
    system_running = True
    
    if system_status_label:
        system_status_label.set_text('運行中')
        system_status_label.classes('text-green-400', remove='text-red-400')
    log_message("系統服務已全面啟動")
    
    # 沿用 config 中的批次，或建立新批次 (僅 Master 需要寫入 CSV)
    if measure_manager and config.network.mode == "master":
        resumed = False
        if config.current_batch:
            filepath = measure_manager.resume_batch(config.current_batch)
            if filepath:
                filename = os.path.basename(filepath)
                if current_batch_label:
                    current_batch_label.set_text(f"目前批次: {filename}")
                log_message(f"沿用既有批次檔案: {filename}")
                resumed = True
        if not resumed:
            filepath = measure_manager.start_new_batch()
            if filepath:
                filename = os.path.basename(filepath)
                config.current_batch = filename
                save_config(config)
                if current_batch_label:
                    current_batch_label.set_text(f"目前批次: {filename}")
                log_message(f"已建立初始批次檔案: {filename}")

def is_channel_enabled(channel: int) -> bool:
    if 1 <= channel <= 12:
        return config.measurement.channel_enabled[channel - 1]
    return False

def on_bluetooth_data(channel: int, data: ThermometerData):
    global ear_cover_statuses
    display_name = get_channel_display_name(channel)
    ear_cover = "有耳溫套" if data.trans_temp_raw == "1111" else "無耳溫套"
    log_message(f"[BT] {display_name}: {data.temperature}°C ({ear_cover})")
    update_meter_ear_cover(channel, data.trans_temp_raw)

    # 儲存耳套狀態供 Log 使用
    ear_cover_statuses[channel] = data.trans_temp_raw

    # 異常檢測在 collect_measure_values 統一處理 (所有通道資料收齊後)

    # Slave 模式即時顯示數值 (依量測狀態決定顯示在空槍值或溫度值)
    if config.network.mode == "slave" and channel in meters_ui:
        meter = meters_ui[channel]
        try:
            state = measure_manager.state if measure_manager else None
            is_empty = state in (MeasurementState.WAITING_EMPTY, MeasurementState.EMPTY_DONE)
            with meter['temp_display'].client:
                if is_empty:
                    meter['empty_display'].set_value(data.temperature)
                else:
                    meter['temp_display'].set_value(data.temperature)
        except Exception as e:
            print(f"[!] Slave UI 更新失敗 (通道 {channel}): {e}")

    if config.network.mode == "slave" and net_manager:
        packet = MeterDataPacket(
            channel=channel, meter_id=data.meter_id,
            temperature=data.temperature, timestamp=data.timestamp,
            ear_cover=data.trans_temp_raw,
            bt_state=bt_manager.get_device_state(channel).value if bt_manager else ""
        )
        net_manager.send_data(packet)

def on_bluetooth_state(channel: int, state: ConnectionState):
    global prev_bt_states
    update_meter_bt_status(channel, state)
    old_state = prev_bt_states.get(channel)
    prev_bt_states[channel] = state

    if is_channel_enabled(channel) and state != old_state:
        display_name = get_channel_display_name(channel)
        try:
            logical_num = int(display_name.replace('CH', ''))
        except:
            logical_num = channel

        if state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR):
            log_message(f"[警告] {display_name} 藍芽已斷線!")
            show_bt_disconnect_alert(channel)
            if plc_manager: plc_manager.set_bt_error(logical_num, True)
        elif state == ConnectionState.CONNECTING:
            if plc_manager: plc_manager.set_bt_error(logical_num, True)
        elif state == ConnectionState.CONNECTED:
            log_message(f"[恢復] {display_name} 藍芽已連線")
            stop_alert_flash()
            if plc_manager: plc_manager.set_bt_error(logical_num, False)

    # Slave 模式：藍芽狀態變更時通知 Master (失敗時標記待補送)
    if config.network.mode == "slave" and net_manager:
        import time as _time
        packet = MeterDataPacket(
            channel=channel, meter_id="",
            temperature=0.0, timestamp=_time.time(),
            bt_state=state.value
        )
        if not net_manager.send_data(packet):
            _pending_bt_sync.add(channel)

def log_message(msg: str):
    """執行緒安全的 Log 寫入"""
    if is_shutting_down:
        print(f"[SHUTDOWN] {msg}")
        return
    current_time = datetime.datetime.now().strftime("%H:%M:%S")
    formatted = f"[{current_time}] {msg}"
    print(formatted)
    try:
        if log_console and log_console.client:
            with log_console.client:
                log_console.push(formatted)
    except: pass

def write_alarm_log(message: str, alarm_type: str = "其他"):
    """寫入歷史異常紀錄 CSV (一天一個檔案)"""
    try:
        alarm_dir = r"D:\logs\Alarm"
        os.makedirs(alarm_dir, exist_ok=True)
        today = datetime.datetime.now().strftime("%Y%m%d")
        filepath = os.path.join(alarm_dir, f"alarm_{today}.csv")
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        safe_message = message.replace('"', '""')
        line = f'{timestamp},{alarm_type},"{safe_message}"\n'
        # 檔案不存在時寫入 CSV 標頭
        write_header = not os.path.exists(filepath)
        try:
            with open(filepath, "a", encoding="utf-8-sig") as f:
                if write_header:
                    f.write("日期時間,異常類型,詳細內容\n")
                f.write(line)
        except PermissionError:
            # 檔案被鎖定（如 Excel 開啟中），寫入備用檔案
            fallback = os.path.join(alarm_dir, f"alarm_{today}_1.csv")
            write_header_fb = not os.path.exists(fallback)
            with open(fallback, "a", encoding="utf-8-sig") as f:
                if write_header_fb:
                    f.write("日期時間,異常類型,詳細內容\n")
                f.write(line)
            print(f"[!] Alarm CSV 被鎖定，已寫入備用: {os.path.basename(fallback)}")
    except Exception as e:
        print(f"[!] 寫入 Alarm Log 失敗: {e}")

def show_alert(message: str, alarm_type: str = "其他"):
    """顯示通用警報"""
    write_alarm_log(message, alarm_type)
    if is_shutting_down or not alert_container or not alert_message_label: return
    with alert_container.client:
        alert_message_label.set_text(f'⚠ {message}')
        alert_container.set_visibility(True)
        start_alert_flash()

def show_bt_disconnect_alert(channel: int):
    global alert_flash_timer, is_alert_visible
    if is_shutting_down or not alert_container or not alert_message_label: return
    display_name = get_channel_display_name(channel)
    current_time = datetime.datetime.now().strftime("%H:%M:%S")
    show_alert(f'[{current_time}] {display_name} 藍芽斷線!', alarm_type="藍芽斷線")

def check_temp_anomaly_all(values: dict):
    """檢查所有通道溫度異常 (僅 Master)，彙整顯示"""
    global temp_anomaly_active
    if config.network.mode != "master":
        return
    if not config.measurement.temp_anomaly_enabled:
        return

    anomaly_list = []
    for ch, temp in values.items():
        display_name = get_channel_display_name(ch)
        if temp > config.measurement.temp_anomaly_upper or temp < config.measurement.temp_anomaly_lower:
            set_meter_highlight(ch, True)
            anomaly_list.append(f'{display_name}={temp:.2f}°C')
        else:
            set_meter_highlight(ch, False)

    if anomaly_list:
        if plc_manager:
            plc_manager.set_d513_bit(12, True)
        range_txt = f'(範圍: {config.measurement.temp_anomaly_lower}~{config.measurement.temp_anomaly_upper}°C)'
        log_message(f"[異常] 溫度異常: {', '.join(anomaly_list)} {range_txt}")
        show_alert(f'量測溫度異常: {", ".join(anomaly_list)} {range_txt}', alarm_type="溫度異常")
        temp_anomaly_active = True
    else:
        if temp_anomaly_active:
            temp_anomaly_active = False
            log_message("[恢復] 溫度異常解除")
            if plc_manager:
                plc_manager.set_d513_bit(12, False)

def check_no_cover_anomaly_all(covers: dict):
    """追蹤所有通道連續無套計數 (僅 Master)，彙整顯示"""
    global no_cover_anomaly_active

    if config.network.mode != "master":
        return

    # 逐通道更新計數與 UI
    for ch, trans_temp_raw in covers.items():
        if trans_temp_raw == "0000":
            no_cover_consecutive[ch] = no_cover_consecutive.get(ch, 0) + 1
        else:
            no_cover_consecutive[ch] = 0

        count = no_cover_consecutive[ch]

        # 更新 UI 顯示計數
        if ch in meters_ui and meters_ui[ch].get('no_cover_count'):
            label = meters_ui[ch]['no_cover_count']
            try:
                with label.client:
                    label.set_text(str(count))
                    if count > 0:
                        label.classes('text-orange-400', remove='text-gray-400')
                    else:
                        label.classes('text-gray-400', remove='text-orange-400')
            except:
                pass

    # 只有啟用異常開關時才觸發警報與 D513
    if not config.measurement.no_cover_anomaly_enabled:
        return

    threshold = config.measurement.no_cover_anomaly_count
    anomaly_list = []
    for ch in covers:
        count = no_cover_consecutive.get(ch, 0)
        if count >= threshold:
            set_meter_highlight(ch, True)
            display_name = get_channel_display_name(ch)
            anomaly_list.append(f'{display_name}({count}次)')
        else:
            set_meter_highlight(ch, False)

    if anomaly_list:
        if plc_manager:
            plc_manager.set_d513_bit(13, True)
        log_message(f"[異常] 連續無套: {', '.join(anomaly_list)}")
        show_alert(f'連續無套異常: {", ".join(anomaly_list)}', alarm_type="連續無套")
        no_cover_anomaly_active = True
    else:
        if no_cover_anomaly_active:
            no_cover_anomaly_active = False
            log_message("[恢復] 連續無套異常解除")
            if plc_manager:
                plc_manager.set_d513_bit(13, False)

prev_plc_state = None
prev_net_state = None
_plc_initialized = False  # PLC 首次連線後是否已執行初始化

def on_plc_state(state: PLCConnectionState):
    global prev_plc_state, _plc_initialized
    if plc_status_icon:
        with plc_status_icon.client:
            if state == PLCConnectionState.CONNECTED: plc_status_icon.props('color=green')
            elif state == PLCConnectionState.CONNECTING: plc_status_icon.props('color=yellow')
            else: plc_status_icon.props('color=red')

    if state != prev_plc_state:
        old = prev_plc_state
        prev_plc_state = state
        if state in (PLCConnectionState.DISCONNECTED, PLCConnectionState.ERROR):
            log_message(f"[警告] PLC 連線異常 ({state.value})")
            show_alert(f'PLC 連線異常 ({state.value})', alarm_type="PLC異常")
        elif state == PLCConnectionState.CONNECTED and old is not None:
            log_message("[恢復] PLC 連線已恢復")

    # PLC 首次連線成功：初始化 D500/D515 歸零，避免殘留觸發信號
    if state == PLCConnectionState.CONNECTED and not _plc_initialized:
        _plc_initialized = True
        if plc_manager:
            plc_manager.write_complete_signal()   # D500=0
            plc_manager.clear_empty_trigger()     # D515=0
            log_message("[PLC] 初始化完成：D500/D515 已歸零")

def on_plc_reset():
    global temp_anomaly_active, no_cover_anomaly_active, empty_out_of_range_count
    log_message("[PLC] HMI 異常復歸觸發")

    # D513 整個清為 0 (通知 PLC 解除所有異常)
    if plc_manager:
        plc_manager.clear_d513()
        log_message("[PLC] D513 已清除 (0x0000)")

    # 清除所有異常狀態
    temp_anomaly_active = False
    no_cover_anomaly_active = False
    empty_out_of_range_count = 0
    no_cover_consecutive.clear()

    # 清除 UI 上無套計數顯示
    for ch, meter in meters_ui.items():
        if meter.get('no_cover_count'):
            try:
                with meter['no_cover_count'].client:
                    meter['no_cover_count'].set_text('0')
                    meter['no_cover_count'].classes('text-gray-400', remove='text-orange-400')
            except:
                pass

    # 清除所有通道 highlight
    for ch in meters_ui:
        set_meter_highlight(ch, False)

    # 清除 UI 警報 (不含 PLC/網路異常，由 stop_alert_flash 隱藏橫幅)
    stop_alert_flash()
    log_message("[PLC] 異常狀態已全部清除")

def on_reset_button_click():
    """HMI 異常復歸按鈕 — 直接執行復歸邏輯"""
    on_plc_reset()

def on_plc_empty_trigger():
    global _d515_triggered_at
    _d515_triggered_at = time.time()
    enabled = get_enabled_channel_list()
    log_message(f"[PLC] D515=1 空槍量測觸發 (啟用通道: {len(enabled)}個 {[get_channel_display_name(c) for c in enabled]})")
    clear_meter_values(is_empty=True)
    try:
        measure_manager.start_empty_measurement()
        if config.simulation_mode: bt_manager.set_simulation_mode_empty()
        delay = config.timing.empty_collect_delay
        log_message(f"[流程] 空槍: {delay}s 後開始收集")
        threading.Timer(delay, trigger_empty_ack_and_collect).start()
    except Exception as e:
        log_message(f"[錯誤] 無法啟動空槍量測: {e}")
        import traceback; traceback.print_exc()

def _wait_for_slave_data(ts_before: dict, timeout: float = 3.0):
    """等待 Slave 所有啟用通道的資料都更新（timestamp 比請求前更新）"""
    # 找出需要等待的 Slave 通道 (CH7~12)
    expected_channels = set()
    for ch in range(7, 13):
        if is_channel_enabled(ch):
            expected_channels.add(ch)
    if not expected_channels:
        return

    deadline = time.time() + timeout
    while time.time() < deadline:
        received = net_manager.get_all_received_data()
        all_ready = True
        for ch in expected_channels:
            pkt = received.get(ch)
            if not pkt or pkt.timestamp <= ts_before.get(ch, 0):
                all_ready = False
                break
        if all_ready:
            return
        time.sleep(0.1)
    # timeout：記錄未到的通道
    received = net_manager.get_all_received_data()
    missing = [get_channel_display_name(ch) for ch in expected_channels
               if ch not in received or received[ch].timestamp <= ts_before.get(ch, 0)]
    if missing:
        log_message(f"[警告] 等待 Slave 資料逾時，未收到: {', '.join(missing)}")

def trigger_empty_ack_and_collect():
    try:
        log_message("[流程] 空槍: Timer 觸發，開始發送 BT/Slave 請求")
        # 記錄請求前 Slave 資料的時間戳
        slave_ts_before = {}
        if config.network.mode == "master" and net_manager:
            for ch, pkt in net_manager.get_all_received_data().items():
                slave_ts_before[ch] = pkt.timestamp
            net_manager.send_command("request_empty")
            log_message("[流程] 空槍: 已發送 Slave request_empty")
        # 本地藍芽量測
        bt_requested = []
        for channel in bt_manager.devices.keys():
            if is_channel_enabled(channel):
                bt_manager.request_measurement(channel)
                bt_requested.append(get_channel_display_name(channel))
        log_message(f"[流程] 空槍: 已發送 BT CD 指令 → {bt_requested}")
        # 等待 Slave 資料到齊（最多 3 秒），本地 BT 回應通常更快
        if config.network.mode == "master" and net_manager:
            log_message("[流程] 空槍: 等待 Slave 資料...")
            _wait_for_slave_data(slave_ts_before, timeout=3.0)
        time.sleep(0.2)
        log_message("[流程] 空槍: 開始收集空槍值")
        collect_empty_values()
    except Exception as e:
        log_message(f"[錯誤] trigger_empty_ack_and_collect 異常: {e}")
        import traceback; traceback.print_exc()

def on_plc_measure_trigger():
    global _d500_triggered_at
    _d500_triggered_at = time.time()
    enabled = get_enabled_channel_list()
    log_message(f"[PLC] D500=1 溫度量測觸發 (啟用通道: {len(enabled)}個 {[get_channel_display_name(c) for c in enabled]})")
    clear_meter_values(is_empty=False)
    try:
        measure_manager.start_temperature_measurement()
        if config.simulation_mode: bt_manager.set_simulation_mode_measure()
        delay = config.timing.measure_collect_delay
        log_message(f"[流程] 量測: {delay}s 後開始收集")
        threading.Timer(delay, trigger_measure_ack_and_collect).start()
    except Exception as e:
        log_message(f"[錯誤] 無法執行溫度量測: {e}")
        import traceback; traceback.print_exc()

def trigger_measure_ack_and_collect():
    try:
        log_message("[流程] 量測: Timer 觸發，開始發送 BT/Slave 請求")
        # 記錄請求前 Slave 資料的時間戳
        slave_ts_before = {}
        if config.network.mode == "master" and net_manager:
            for ch, pkt in net_manager.get_all_received_data().items():
                slave_ts_before[ch] = pkt.timestamp
            net_manager.send_command("request_measure")
            log_message("[流程] 量測: 已發送 Slave request_measure")
        # 本地藍芽量測
        bt_requested = []
        for channel in bt_manager.devices.keys():
            if is_channel_enabled(channel):
                bt_manager.request_measurement(channel)
                bt_requested.append(get_channel_display_name(channel))
        log_message(f"[流程] 量測: 已發送 BT CD 指令 → {bt_requested}")
        # 等待 Slave 資料到齊
        if config.network.mode == "master" and net_manager:
            log_message("[流程] 量測: 等待 Slave 資料...")
            _wait_for_slave_data(slave_ts_before, timeout=3.0)
        time.sleep(0.2)
        log_message("[流程] 量測: 開始收集量測值")
        collect_measure_values()
    except Exception as e:
        log_message(f"[錯誤] trigger_measure_ack_and_collect 異常: {e}")
        import traceback; traceback.print_exc()
def on_network_data(packet: MeterDataPacket):
    display_name = get_channel_display_name(packet.channel)
    ch = packet.channel

    # 更新藍芽連線狀態
    if packet.bt_state:
        try:
            bt_state = ConnectionState(packet.bt_state)
            old_state = prev_bt_states.get(ch)
            prev_bt_states[ch] = bt_state
            update_meter_bt_status(ch, bt_state)

            # 追蹤 CONNECTING 超時
            if bt_state == ConnectionState.CONNECTING:
                if ch not in slave_bt_connecting_since:
                    slave_bt_connecting_since[ch] = time.time()
            else:
                slave_bt_connecting_since.pop(ch, None)

            if is_channel_enabled(ch) and bt_state != old_state:
                try:
                    logical_num = int(display_name.replace('CH', ''))
                except:
                    logical_num = ch

                if bt_state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR):
                    log_message(f"[警告] {display_name} (Slave) 藍芽已斷線!")
                    show_bt_disconnect_alert(ch)
                    if plc_manager: plc_manager.set_bt_error(logical_num, True)
                elif bt_state == ConnectionState.CONNECTED:
                    log_message(f"[恢復] {display_name} (Slave) 藍芽已連線")
                    if plc_manager: plc_manager.set_bt_error(logical_num, False)
                elif bt_state == ConnectionState.CONNECTING:
                    if plc_manager: plc_manager.set_bt_error(logical_num, True)
        except ValueError:
            pass

    # 更新耳溫套狀態
    if packet.ear_cover:
        ear_cover_statuses[ch] = packet.ear_cover
        update_meter_ear_cover(ch, packet.ear_cover)
    # 異常檢測在 collect_measure_values 統一處理

    # 溫度為 0 且無耳套資訊 = 純 BT 狀態封包，不 log 溫度
    if packet.temperature == 0.0 and not packet.ear_cover:
        log_message(f"[NET] {display_name}: BT {packet.bt_state}")
    else:
        ear_txt = "有耳溫套" if packet.ear_cover == "1111" else "無耳溫套" if packet.ear_cover == "0000" else ""
        log_message(f"[NET] {display_name}: {packet.temperature}°C {ear_txt}")

def on_network_state(state: NetworkState):
    global prev_net_state
    if network_status_icon:
        with network_status_icon.client:
            if state == NetworkState.CONNECTED: network_status_icon.props('color=green')
            elif state == NetworkState.LISTENING: network_status_icon.props('color=yellow')
            else: network_status_icon.props('color=red')

    if state != prev_net_state:
        old = prev_net_state
        prev_net_state = state
        if state in (NetworkState.DISCONNECTED, NetworkState.ERROR):
            log_message(f"[警告] 網路連線異常 ({state.value})")
            show_alert(f'網路連線異常 ({state.value})', alarm_type="網路異常")
        elif state == NetworkState.CONNECTED and old is not None:
            log_message("[恢復] 網路連線已恢復")

    # Master 模式：連線建立後，延遲請求 Slave 重送藍芽狀態 + 同步通道啟用狀態
    if state == NetworkState.CONNECTED and config.network.mode == "master" and net_manager:
        def _request_slave_sync():
            time.sleep(2.0)  # 等待 Master UI 完全載入
            if net_manager and net_manager.state == NetworkState.CONNECTED:
                net_manager.send_command("sync_bt_status")
                log_message("[NET] 已請求 Slave 重送藍芽狀態")
                _sync_channel_enabled_to_peer()
        threading.Thread(target=_request_slave_sync, daemon=True).start()

    # Slave 模式：網路連線建立後，補送所有通道的藍芽狀態給 Master
    if state == NetworkState.CONNECTED and config.network.mode == "slave" and bt_manager and net_manager:
        import time as _time
        for ch, device in bt_manager.devices.items():
            packet = MeterDataPacket(
                channel=ch, meter_id=device.device_id or "",
                temperature=0.0, timestamp=_time.time(),
                bt_state=device.state.value
            )
            net_manager.send_data(packet)
        _pending_bt_sync.clear()
        log_message("[NET] 已補送所有通道藍芽狀態至 Master")
        # 補送通道啟用狀態
        _sync_slave_channel_enabled()

def on_slave_channel_enabled(channels: dict):
    """收到對端的通道啟用狀態 → 套用、存檔、更新 UI"""
    global slave_channel_enabled
    if config.network.mode == "master":
        slave_channel_enabled = channels

    changed = []
    for ch, peer_enabled in channels.items():
        ch = int(ch)
        if 1 <= ch <= 12:
            local_enabled = is_channel_enabled(ch)
            if local_enabled != peer_enabled:
                config.measurement.channel_enabled[ch - 1] = peer_enabled
                changed.append(f"{get_channel_display_name(ch)}={'啟用' if peer_enabled else '停用'}")

    if changed:
        # 自動存檔
        save_config(config)
        log_message(f"[NET] 收到對端通道狀態變更，已套用並存檔: {changed}")
        # 更新 UI (需在 NiceGUI 執行緒)
        try:
            _apply_channel_enabled_to_ui()
        except Exception as e:
            log_message(f"[NET] 更新通道 UI 失敗: {e}")
    else:
        log_message("[NET] 收到對端通道狀態，無變更")

def _sync_channel_enabled_to_peer():
    """傳送本機所有通道啟用狀態給對端 (Master↔Slave 雙向同步)"""
    if not net_manager:
        return
    ch_state = {}
    for ch in range(1, 13):
        ch_state[ch] = is_channel_enabled(ch)
    if net_manager.send_channel_enabled(ch_state):
        log_message(f"[NET] 已同步通道啟用狀態至對端: {[get_channel_display_name(ch) for ch in range(1,13) if not is_channel_enabled(ch)]} 停用")

def _sync_slave_channel_enabled():
    """Slave 端：傳送通道啟用狀態給 Master (相容舊呼叫)"""
    _sync_channel_enabled_to_peer()

def on_network_command(command: str):
    """Slave 收到 Master 指令"""
    if command == "request_empty":
        log_message("[NET] 收到 Master 空槍量測請求")
        clear_meter_values(is_empty=True)
        # 設定量測狀態 (讓 Slave UI 顯示在正確欄位)
        if measure_manager:
            measure_manager.start_empty_measurement()
        if config.simulation_mode and bt_manager:
            bt_manager.set_simulation_mode_empty()
        if bt_manager:
            for ch in bt_manager.devices.keys():
                if is_channel_enabled(ch):
                    bt_manager.request_measurement(ch)
    elif command == "sync_bt_status":
        log_message("[NET] 收到 Master 藍芽狀態同步請求")
        if bt_manager and net_manager:
            import time as _time
            for ch, device in bt_manager.devices.items():
                packet = MeterDataPacket(
                    channel=ch, meter_id=device.device_id or "",
                    temperature=0.0, timestamp=_time.time(),
                    bt_state=device.state.value
                )
                net_manager.send_data(packet)
            _sync_slave_channel_enabled()
    elif command == "request_measure":
        log_message("[NET] 收到 Master 溫度量測請求")
        clear_meter_values(is_empty=False)
        if measure_manager:
            measure_manager.start_temperature_measurement()
        if config.simulation_mode and bt_manager:
            bt_manager.set_simulation_mode_measure()
        if bt_manager:
            for ch in bt_manager.devices.keys():
                if is_channel_enabled(ch):
                    bt_manager.request_measurement(ch)

def on_measurement_state(state: MeasurementState):
    log_message(f"[量測] 狀態: {state.value}")
    if not measure_status_label: return
    with measure_status_label.client:
        state_configs = {
            MeasurementState.IDLE: ("待機中", "text-gray-400"),
            MeasurementState.WAITING_EMPTY: ("等待空槍", "text-yellow-400"),
            MeasurementState.EMPTY_DONE: ("空槍完成", "text-blue-400"),
            MeasurementState.WAITING_MEASURE: ("等待量測", "text-yellow-400"),
            MeasurementState.MEASURING: ("計算中", "text-orange-400"),
            MeasurementState.COMPLETE: ("量測完成", "text-green-400"),
        }
        text, color_class = state_configs.get(state, (state.value, "text-white"))
        measure_status_label.set_text(text)
        measure_status_label.classes(color_class, remove="text-gray-400 text-yellow-400 text-blue-400 text-orange-400 text-green-400 text-white")

def on_channel_update(channel: int, data: ChannelData):
    update_meter_display(channel, data)

def on_measurement_complete(result):
    log_message(f"[量測完成] PASS: {result.pass_count}, FAIL: {result.fail_count}")

    # 列出各通道判定結果
    ch_results = []
    for ch_data in sorted(result.channels.values(), key=lambda c: c.channel):
        if ch_data.empty_value is not None:
            ch_results.append(f"CH{ch_data.channel}:{ch_data.result.value}(err={ch_data.error_value:.2f})" if ch_data.error_value is not None else f"CH{ch_data.channel}:{ch_data.result.value}")
    log_message(f"[量測完成] 通道結果: {ch_results}")

    # 執行 5 橫列批次 Log 紀錄 (僅 Master)
    if measure_manager and config.network.mode == "master":
        log_saved = measure_manager.save_cycle_log(
            plc_data=plc_manager.plc_data if plc_manager else None,
            ear_covers=ear_cover_statuses,
            enabled_channels=get_enabled_channel_list()
        )
        log_message(f"[流程] 量測 Log 寫入: {'成功' if log_saved else '失敗'}")

    if plc_manager:
        # 0=不使用, 1=FAIL, 2=PASS
        logical_results = [0]*12
        current_results = measure_manager.get_results()
        for internal_ch in range(1, 13):
            display_name = get_channel_display_name(internal_ch)
            logical_idx = int(display_name.replace('CH', '')) - 1
            if is_channel_enabled(internal_ch):
                logical_results[logical_idx] = 2 if current_results[internal_ch - 1] else 1
            # 未啟用的通道保持 0 (不使用)

        log_message(f"[流程] 寫入 PLC 判定結果 D501~D512: {logical_results}")
        # 寫判定結果 (D501~D512): 0=不使用, 1=FAIL, 2=PASS
        s1 = plc_manager.write_results(logical_results)
        log_message(f"[流程] write_results: {'成功' if s1 else '失敗'}")
        if s1:
            plc_manager.write_complete_signal()
            globals()['_d500_triggered_at'] = 0.0
            log_message("[流程] D500 已歸零 (write_complete_signal 成功)")
        else:
            log_message("[錯誤] write_results 失敗，D500 未歸零!")
        update_plc_display()
    else:
        log_message("[錯誤] plc_manager 不存在，無法寫入 PLC 結果!")

def on_new_batch_click():
    """點擊『換批』按鈕"""
    if measure_manager:
        filepath = measure_manager.start_new_batch()
        if filepath:
            filename = os.path.basename(filepath)
            # 寫入 config 持久化
            config.current_batch = filename
            save_config(config)

            if current_batch_label:
                current_batch_label.set_text(f"目前批次: {filename}")

            # 換批時將 PLC 的 OK/NG 計數歸零 (D517~D540)
            if plc_manager:
                plc_manager.write_ok_ng_counts([0]*12, [0]*12)
                log_message(f"換批成功，PLC 計數已歸零: {filename}")
            else:
                log_message(f"換批成功: {filename}")

            ui.notify(f"已更換新批次並歸零計數: {filename}", type='positive')
            log_message(f"使用者手動更換批次: {filename}")

def get_enabled_channel_list():
    """取得已啟用的通道列表"""
    return [ch for ch in range(1, 13) if is_channel_enabled(ch)]

def collect_empty_values():
    # 清除停用通道的殘留資料
    disabled = [get_channel_display_name(ch) for ch in range(1, 13) if not is_channel_enabled(ch)]
    for ch in range(1, 13):
        if not is_channel_enabled(ch):
            measure_manager.clear_channel(ch)
    if disabled:
        log_message(f"[流程] 空槍收集: 已清除停用通道 {disabled}")

    values = {}
    # 本機 BT
    bt_got = []
    bt_miss = []
    for channel in bt_manager.devices.keys():
        if is_channel_enabled(channel):
            data = bt_manager.get_last_data(channel)
            if data:
                values[channel] = data.temperature
                bt_got.append(f"{get_channel_display_name(channel)}={data.temperature:.2f}")
            else:
                bt_miss.append(get_channel_display_name(channel))
    if bt_got:
        log_message(f"[流程] 空槍收集 BT: {bt_got}")
    if bt_miss:
        log_message(f"[警告] 空槍收集 BT 無資料: {bt_miss}")

    # Slave 網路
    if config.network.mode == "master" and net_manager:
        net_got = []
        net_miss = []
        for ch in range(7, 13):
            if is_channel_enabled(ch):
                pkt = net_manager.get_all_received_data().get(ch)
                if pkt:
                    values[ch] = pkt.temperature
                    net_got.append(f"{get_channel_display_name(ch)}={pkt.temperature:.2f}")
                else:
                    net_miss.append(get_channel_display_name(ch))
        if net_got:
            log_message(f"[流程] 空槍收集 Slave: {net_got}")
        if net_miss:
            log_message(f"[警告] 空槍收集 Slave 無資料: {net_miss}")

    log_message(f"[流程] 空槍收集完成: 共 {len(values)} 通道有值")
    # 檢查空槍值是否超出上下限 (此函式由 D515=1 觸發流程呼叫，資料已收齊)
    global empty_out_of_range_count
    is_warmup = plc_manager and plc_manager.plc_data and plc_manager.plc_data.warmup == 1

    # D542=0 (非暖槍) 時自動歸零暖槍累計次數
    if not is_warmup and empty_out_of_range_count > 0:
        empty_out_of_range_count = 0
        log_message("[暖槍] 暖槍結束，空槍超限累計歸零")
        if plc_manager: plc_manager.set_d513_bit(14, False)

    out_of_range = []
    out_of_range_chs = []
    for ch, val in values.items():
        if val > config.measurement.empty_upper or val < config.measurement.empty_lower:
            display_name = get_channel_display_name(ch)
            out_of_range.append(f'{display_name}={val:.2f}°C')
            out_of_range_chs.append(ch)
            set_meter_highlight(ch, True)
        else:
            set_meter_highlight(ch, False)

    range_txt = f'(範圍: {config.measurement.empty_lower:.1f}~{config.measurement.empty_upper:.1f}°C)'

    if out_of_range:
        if is_warmup:
            # 暖槍中 D542=1：累計超限次數，達 3 次才發出警報
            empty_out_of_range_count += 1
            log_message(f"[暖槍] 空槍值超限 第{empty_out_of_range_count}次: {', '.join(out_of_range)}")
            if empty_out_of_range_count >= 3:
                show_alert(f'暖槍空槍值連續{empty_out_of_range_count}次超限: {", ".join(out_of_range)} {range_txt}', alarm_type="空槍超限")
                log_message(f"[警報] 暖槍空槍值連續{empty_out_of_range_count}次超出範圍")
                if plc_manager: plc_manager.set_d513_bit(14, True)
        else:
            # 非暖槍：任一次超限即發出警報
            show_alert(f'空槍值異常: {", ".join(out_of_range)} {range_txt}', alarm_type="空槍異常")
            log_message(f"[警報] 空槍值超出範圍: {', '.join(out_of_range)}")
            if plc_manager: plc_manager.set_d513_bit(14, True)
    else:
        # 空槍值正常：重置累計次數與 D513 bit14
        if empty_out_of_range_count > 0 or (plc_manager and plc_manager._bt_error_mask & (1 << 14)):
            empty_out_of_range_count = 0
            if plc_manager: plc_manager.set_d513_bit(14, False)

    measure_manager.record_empty_values(values)

    # 空槍觸發寫入一列 Log (僅 Master)，寫入成功後才清除 D515
    log_saved = False
    if measure_manager and config.network.mode == "master":
        log_saved = measure_manager.save_cycle_log(
            is_empty=True,
            plc_data=plc_manager.plc_data if plc_manager else None,
            ear_covers=ear_cover_statuses,
            enabled_channels=get_enabled_channel_list()
        )

    update_plc_display()
    if plc_manager and log_saved:
        plc_manager.clear_empty_trigger()
        globals()['_d515_triggered_at'] = 0.0
        log_message("[PLC] 空槍值已寫入 Log，清除 D515")

def collect_measure_values():
    # 清除停用通道的殘留資料，避免 _all_measure_recorded 永遠等不到停用通道的量測值
    disabled = [get_channel_display_name(ch) for ch in range(1, 13) if not is_channel_enabled(ch)]
    for ch in range(1, 13):
        if not is_channel_enabled(ch):
            measure_manager.clear_channel(ch)
    if disabled:
        log_message(f"[流程] 量測收集: 已清除停用通道 {disabled}")

    values = {}
    # 本機 BT
    bt_got = []
    bt_miss = []
    for channel in bt_manager.devices.keys():
        if is_channel_enabled(channel):
            data = bt_manager.get_last_data(channel)
            if data:
                values[channel] = data.temperature
                bt_got.append(f"{get_channel_display_name(channel)}={data.temperature:.2f}")
            else:
                bt_miss.append(get_channel_display_name(channel))
    if bt_got:
        log_message(f"[流程] 量測收集 BT: {bt_got}")
    if bt_miss:
        log_message(f"[警告] 量測收集 BT 無資料: {bt_miss}")

    # Slave 網路
    if config.network.mode == "master" and net_manager:
        net_got = []
        net_miss = []
        for ch in range(7, 13):
            if is_channel_enabled(ch):
                pkt = net_manager.get_all_received_data().get(ch)
                if pkt:
                    values[ch] = pkt.temperature
                    net_got.append(f"{get_channel_display_name(ch)}={pkt.temperature:.2f}")
                else:
                    net_miss.append(get_channel_display_name(ch))
        if net_got:
            log_message(f"[流程] 量測收集 Slave: {net_got}")
        if net_miss:
            log_message(f"[警告] 量測收集 Slave 無資料: {net_miss}")

    log_message(f"[流程] 量測收集完成: 共 {len(values)} 通道有值")
    measure_manager.record_measure_values(values)

    # 檢查 _all_measure_recorded 結果
    all_done = measure_manager._all_measure_recorded()
    log_message(f"[流程] _all_measure_recorded = {all_done}, 狀態 = {measure_manager.state.value}")
    if not all_done:
        # 列出阻塞的通道：有 empty_value 但沒 measure_value
        blocking = []
        for ch_data in measure_manager._channels.values():
            if ch_data.empty_value is not None and ch_data.measure_value is None:
                blocking.append(f"CH{ch_data.channel}(empty={ch_data.empty_value:.2f})")
        if blocking:
            log_message(f"[警告] 阻塞通道(有空槍無量測): {blocking}")

    # D500=1 量測觸發：統一對所有通道 (含 Slave) 做異常檢測
    if config.network.mode == "master":
        check_temp_anomaly_all(values)
        covers = {ch: ear_cover_statuses[ch] for ch in values if ch in ear_cover_statuses}
        check_no_cover_anomaly_all(covers)

    update_plc_display()

def update_channel_disabled_display():
    for ch, meter in meters_ui.items():
        enabled = is_channel_enabled(ch)
        meter['disabled_badge'].set_visibility(not enabled)
        if enabled: meter['row_container'].classes(remove='opacity-40')
        else: meter['row_container'].classes('opacity-40')
    # 清除停用通道的 D513 錯誤位元
    if plc_manager:
        for ch in range(1, 13):
            if not is_channel_enabled(ch):
                try:
                    logical_num = int(get_channel_display_name(ch).replace('CH', ''))
                except:
                    logical_num = ch
                plc_manager.set_bt_error(logical_num, False)

def _refresh_ui_from_config():
    """從 config 刷新所有設定面板 UI 元件的顯示值"""
    # 量測參數
    if tolerance_upper_input: tolerance_upper_input.set_value(config.measurement.tolerance_upper)
    if tolerance_lower_input: tolerance_lower_input.set_value(config.measurement.tolerance_lower)
    if empty_upper_input: empty_upper_input.set_value(config.measurement.empty_upper)
    if empty_lower_input: empty_lower_input.set_value(config.measurement.empty_lower)
    # 溫度異常
    if temp_anomaly_switch: temp_anomaly_switch.set_value(config.measurement.temp_anomaly_enabled)
    if temp_anomaly_upper_input: temp_anomaly_upper_input.set_value(config.measurement.temp_anomaly_upper)
    if temp_anomaly_lower_input: temp_anomaly_lower_input.set_value(config.measurement.temp_anomaly_lower)
    # 連續無套異常
    if no_cover_anomaly_switch: no_cover_anomaly_switch.set_value(config.measurement.no_cover_anomaly_enabled)
    if no_cover_anomaly_count_input: no_cover_anomaly_count_input.set_value(config.measurement.no_cover_anomaly_count)
    # 網路
    if mode_select: mode_select.set_value(config.network.mode)
    if net_inputs.get('master_ip'): net_inputs['master_ip'].set_value(config.network.master_ip)
    if net_inputs.get('port'): net_inputs['port'].set_value(config.network.port)
    # 時序
    if timing_inputs.get('empty_collect_delay'): timing_inputs['empty_collect_delay'].set_value(config.timing.empty_collect_delay)
    if timing_inputs.get('measure_collect_delay'): timing_inputs['measure_collect_delay'].set_value(config.timing.measure_collect_delay)
    # PLC
    if plc_inputs.get('ip_address'): plc_inputs['ip_address'].set_value(config.plc.ip_address)
    if plc_inputs.get('port'): plc_inputs['port'].set_value(config.plc.port)
    # 藍芽
    if bt_inputs.get('reconnect_interval'): bt_inputs['reconnect_interval'].set_value(config.bluetooth.reconnect_interval)
    if bt_inputs.get('timeout'): bt_inputs['timeout'].set_value(config.bluetooth.timeout)
    for ch, mac_input in bt_mac_inputs.items():
        idx = ch - 1 if ch <= 6 else ch - 7
        if idx < len(config.bluetooth.device_addresses):
            mac_input.set_value(config.bluetooth.device_addresses[idx])
    # 通道啟用開關
    for ch, sw in channel_switches.items():
        sw.set_value(config.measurement.channel_enabled[ch - 1])

def _apply_channel_enabled_to_ui():
    """對端通道狀態變更後，同步更新本機 UI (開關 + 通道列外觀)"""
    # 更新設定頁面的開關
    for ch, sw in channel_switches.items():
        try:
            with sw.client:
                sw.set_value(config.measurement.channel_enabled[ch - 1])
        except Exception:
            pass
    # 更新通道列外觀 (停用灰化 + badge)
    update_channel_disabled_display()

def clear_meter_values(is_empty: bool):
    """收到量測訊號時，先將 UI 對應欄位清 0，避免顯示舊數據"""
    for ch, meter in meters_ui.items():
        if not is_channel_enabled(ch):
            continue
        try:
            with meter['light'].client:
                if is_empty:
                    meter['empty_display'].set_value(0.00)
                else:
                    meter['temp_display'].set_value(0.00)
                    meter['error_display'].set_value(0.00)
                    meter['light'].props('color=grey')
                    meter['text'].set_text('WAIT')
                    meter['text'].classes('text-gray-500', remove='text-green-500 text-red-500')
        except Exception:
            pass

def update_meter_display(channel: int, data: ChannelData):
    if is_shutting_down or channel not in meters_ui: return
    meter = meters_ui[channel]
    # 執行緒安全保護
    with meter['light'].client:
        if data.empty_value is not None: meter['empty_display'].set_value(data.empty_value)
        if data.measure_value is not None: meter['temp_display'].set_value(data.measure_value)
        if data.error_value is not None: meter['error_display'].set_value(data.error_value)
        if data.result == JudgeResult.PASS:
            meter['light'].props('color=green'); meter['text'].set_text('PASS'); meter['text'].classes('text-green-500', remove='text-red-500 text-gray-500')
        elif data.result == JudgeResult.FAIL:
            meter['light'].props('color=red'); meter['text'].set_text('FAIL'); meter['text'].classes('text-red-500', remove='text-green-500 text-gray-500')
            log_message(f"[異常] {get_channel_display_name(channel)} 誤差超限: {data.error_value:.2f}°C")

def update_meter_bt_status(channel: int, state: ConnectionState):
    if is_shutting_down or channel not in meters_ui: return
    meter = meters_ui[channel]
    # 執行緒安全保護
    with meter['bt_icon'].client:
        if state == ConnectionState.CONNECTED: meter['bt_icon'].props('color=blue')
        elif state == ConnectionState.CONNECTING: meter['bt_icon'].props('color=yellow')
        elif state == ConnectionState.ERROR: meter['bt_icon'].props('color=red')
        else: meter['bt_icon'].props('color=gray')

def update_meter_ear_cover(channel: int, trans_temp_raw: str):
    if channel not in meters_ui: return
    meter = meters_ui[channel]
    # 執行緒安全保護
    with meter['ear_cover'].client:
        if trans_temp_raw == "1111": meter['ear_cover'].set_text("有"); meter['ear_cover'].classes("text-green-400", remove="text-red-400 text-gray-500")
        elif trans_temp_raw == "0000": meter['ear_cover'].set_text("無"); meter['ear_cover'].classes("text-red-400", remove="text-green-400 text-gray-500")

def set_meter_highlight(channel: int, anomaly: bool):
    """設定通道列 highlight (異常時紅色邊框閃爍)"""
    if channel not in meters_ui: return
    meter = meters_ui[channel]
    try:
        with meter['row_container'].client:
            if anomaly:
                meter['row_container'].classes('bg-red-900/40 border border-red-500 rounded', remove='')
            else:
                meter['row_container'].classes(remove='bg-red-900/40 border border-red-500 rounded')
    except:
        pass

def start_alert_flash():
    global alert_flash_timer, is_alert_visible
    if alert_flash_timer: alert_flash_timer.deactivate()
    is_alert_visible = True
    async def flash():
        global is_alert_visible
        if alert_container:
            is_alert_visible = not is_alert_visible
            if is_alert_visible: alert_container.classes('bg-red-600', remove='bg-red-900')
            else: alert_container.classes('bg-red-900', remove='bg-red-600')
    alert_flash_timer = ui.timer(0.5, flash)

def stop_alert_flash():
    global alert_flash_timer
    if alert_flash_timer: alert_flash_timer.deactivate(); alert_flash_timer = None
    if alert_container: alert_container.set_visibility(False)

def on_reset_click():
    measure_manager.reset()
    for meter in meters_ui.values():
        meter['empty_display'].set_value(0.00); meter['temp_display'].set_value(0.00); meter['error_display'].set_value(0.00)
        meter['light'].props('color=grey'); meter['text'].set_text('WAIT'); meter['text'].classes('text-gray-500', remove='text-green-500 text-red-500')
        meter['ear_cover'].set_text('--'); meter['ear_cover'].classes('text-gray-500', remove='text-green-400 text-red-400')
        if meter['ok_display']: meter['ok_display'].set_value(0)
        if meter['ng_display']: meter['ng_display'].set_value(0)
    if plc_manager: plc_manager.write_ok_ng_counts([0]*12, [0]*12)
    stop_alert_flash()
    if config.simulation_mode: bt_manager.reset_simulation()
    log_message("資料重設")

def toggle_settings():
    if settings_drawer: settings_drawer.toggle()

def _collect_settings_from_ui():
    """從 UI 收集所有設定值寫入 config"""
    if tolerance_upper_input: config.measurement.tolerance_upper = tolerance_upper_input.value
    if tolerance_lower_input: config.measurement.tolerance_lower = tolerance_lower_input.value
    if empty_upper_input: config.measurement.empty_upper = empty_upper_input.value
    if empty_lower_input: config.measurement.empty_lower = empty_lower_input.value
    # 溫度異常設定
    if temp_anomaly_switch: config.measurement.temp_anomaly_enabled = temp_anomaly_switch.value
    if temp_anomaly_upper_input: config.measurement.temp_anomaly_upper = temp_anomaly_upper_input.value
    if temp_anomaly_lower_input: config.measurement.temp_anomaly_lower = temp_anomaly_lower_input.value
    # 連續無套異常設定
    if no_cover_anomaly_switch: config.measurement.no_cover_anomaly_enabled = no_cover_anomaly_switch.value
    if no_cover_anomaly_count_input: config.measurement.no_cover_anomaly_count = int(no_cover_anomaly_count_input.value)
    config.network.mode = mode_select.value
    if config.network.mode == "slave":
        config.plc.enabled = False
    if net_inputs.get('master_ip'): config.network.master_ip = net_inputs['master_ip'].value
    if net_inputs.get('port'): config.network.port = int(net_inputs['port'].value)
    if timing_inputs.get('empty_collect_delay'): config.timing.empty_collect_delay = timing_inputs['empty_collect_delay'].value
    if timing_inputs.get('measure_collect_delay'): config.timing.measure_collect_delay = timing_inputs['measure_collect_delay'].value
    if plc_inputs.get('ip_address'): config.plc.ip_address = plc_inputs['ip_address'].value
    if plc_inputs.get('port'): config.plc.port = int(plc_inputs['port'].value)
    config.bluetooth.reconnect_interval = bt_inputs['reconnect_interval'].value
    config.bluetooth.timeout = bt_inputs['timeout'].value
    # 同步到 bt_manager（運行中即時生效）
    if bt_manager:
        bt_manager.connect_timeout = config.bluetooth.timeout
        bt_manager.reconnect_interval = config.bluetooth.reconnect_interval
    for ch, mac_input in bt_mac_inputs.items():
        idx = ch - 1 if ch <= 6 else ch - 7
        if idx < len(config.bluetooth.device_addresses): config.bluetooth.device_addresses[idx] = mac_input.value
    for i in range(1, 13):
        if i in channel_switches: config.measurement.channel_enabled[i-1] = channel_switches[i].value

def on_save_advanced_settings():
    _collect_settings_from_ui()
    measure_manager.set_tolerance(config.measurement.tolerance_upper, config.measurement.tolerance_lower)
    update_channel_disabled_display()
    save_config(config)
    _sync_channel_enabled_to_peer()
    _refresh_ui_from_config()
    ui.notify('進階設定已儲存', type='positive')

def on_apply_settings():
    """儲存設定並即時套用到所有執行中的管理器（不需重啟程式）"""
    global bt_manager, plc_manager, net_manager, system_running

    _collect_settings_from_ui()
    save_config(config)

    log_message("[設定] 開始套用新參數...")

    # 1. 量測管理器：更新誤差容許值
    measure_manager.set_tolerance(config.measurement.tolerance_upper, config.measurement.tolerance_lower)
    log_message(f"[設定] 誤差範圍: +{config.measurement.tolerance_upper:.2f} / -{config.measurement.tolerance_lower:.2f}")

    # 2. 通道啟用狀態
    update_channel_disabled_display()

    # 3. 藍芽：只更新已有裝置的 MAC 位址（不重啟管理器）
    if bt_manager:
        for ch, device in bt_manager.devices.items():
            if ch in bt_mac_inputs:
                new_addr = bt_mac_inputs[ch].value
            else:
                # 從 config 取
                if config.network.mode == "master":
                    idx = ch - 1
                else:
                    idx = ch - 7
                new_addr = config.bluetooth.device_addresses[idx] if 0 <= idx < len(config.bluetooth.device_addresses) else ""
            if device.mac_address != new_addr:
                old_connected = device.state == ConnectionState.CONNECTED
                if old_connected:
                    bt_manager._disconnect_device(device)
                device.mac_address = new_addr
                log_message(f"[設定] {get_channel_display_name(ch)} MAC 已更新: {new_addr}" + (" (已斷開舊連線，將自動重連)" if old_connected else ""))
        log_message("[設定] 藍芽參數已更新")

    # 4. PLC：更新 IP/Port（不中斷監控，由監控迴圈自動重連）
    if plc_manager:
        old_ip, old_port = plc_manager.ip_address, plc_manager.port
        plc_manager.ip_address = config.plc.ip_address
        plc_manager.port = config.plc.port
        if old_ip != config.plc.ip_address or old_port != config.plc.port:
            log_message(f"[設定] PLC 位址已更新: {config.plc.ip_address}:{config.plc.port} (下次連線生效)")

    # 5. 網路：更新參數（不重啟，維持現有連線）
    if net_manager:
        net_manager.master_ip = config.network.master_ip
        net_manager.port = config.network.port
        log_message(f"[設定] 網路參數已更新 (模式: {config.network.mode})")

    # 6. 時序設定直接生效（量測流程讀取 config.timing.*）
    log_message(f"[設定] 時序參數已更新")

    # 7. 雙向同步通道啟用狀態
    _sync_channel_enabled_to_peer()

    # 8. 刷新 UI 顯示值（確保 UI 與 config 一致）
    _refresh_ui_from_config()

    log_message("[設定] 所有參數已套用完成")
    ui.notify('設定已即時套用', type='positive')

def on_simulate_empty():
    if plc_manager: plc_manager.write_empty_trigger(1)
    else: on_plc_empty_trigger()

def on_simulate_measure():
    if plc_manager: plc_manager.write_measure_trigger(1)
    else: on_plc_measure_trigger()

def update_plc_display():
    """每 500ms 更新 PLC 暫存器、藍芽狀態與系統狀態顯示"""
    objs = globals()
    plc_mgr, bt_mgr = objs.get('plc_manager'), objs.get('bt_manager')

    # Slave: 補送之前失敗的藍芽狀態
    if _pending_bt_sync and config.network.mode == "slave" and bt_mgr and net_manager:
        import time as _time
        for ch in list(_pending_bt_sync):
            device = bt_mgr.devices.get(ch)
            if device:
                packet = MeterDataPacket(
                    channel=ch, meter_id=device.device_id or "",
                    temperature=0.0, timestamp=_time.time(),
                    bt_state=device.state.value
                )
                if net_manager.send_data(packet):
                    _pending_bt_sync.discard(ch)
    
    # 動態更新系統狀態標籤
    if system_status_label:
        is_run = objs.get('system_running', False)
        system_status_label.set_text('運行中' if is_run else '已停止')
        system_status_label.classes('text-green-400' if is_run else 'text-red-400', 
                                    remove='text-green-400 text-red-400')

    if not plc_mgr or not bt_mgr: return
    if plc_status_icon:
        s = plc_mgr.state
        if s == PLCConnectionState.CONNECTED: plc_status_icon.props('color=green')
        elif s == PLCConnectionState.CONNECTING: plc_status_icon.props('color=yellow')
        else: plc_status_icon.props('color=red')
    for ch in bt_mgr.devices.keys(): update_meter_bt_status(ch, bt_mgr.get_device_state(ch))

    # 檢查 D500/D515 觸發超時（15 秒未歸 0 = 流程卡住）
    now = time.time()
    if _d500_triggered_at and now - _d500_triggered_at >= _TRIGGER_TIMEOUT:
        elapsed = now - _d500_triggered_at
        globals()['_d500_triggered_at'] = 0.0  # 只警報一次
        log_message(f"[異常] D500 量測觸發已 {elapsed:.1f} 秒未歸零，流程可能卡住")
        show_alert(f'D500 量測觸發超過 {_TRIGGER_TIMEOUT:.0f} 秒未完成', alarm_type="流程超時")
    if _d515_triggered_at and now - _d515_triggered_at >= _TRIGGER_TIMEOUT:
        elapsed = now - _d515_triggered_at
        globals()['_d515_triggered_at'] = 0.0
        log_message(f"[異常] D515 空槍觸發已 {elapsed:.1f} 秒未歸零，流程可能卡住")
        show_alert(f'D515 空槍觸發超過 {_TRIGGER_TIMEOUT:.0f} 秒未完成', alarm_type="流程超時")

    # 檢查 Slave 藍芽 CONNECTING 超時
    if config.network.mode == "master" and slave_bt_connecting_since:
        timeout = config.bluetooth.timeout
        now = time.time()
        for ch, since in list(slave_bt_connecting_since.items()):
            if now - since >= timeout and is_channel_enabled(ch):
                display_name = get_channel_display_name(ch)
                log_message(f"[警告] {display_name} (Slave) 藍芽連線逾時 ({timeout:.0f}s)!")
                show_bt_disconnect_alert(ch)
                slave_bt_connecting_since.pop(ch)  # 只警告一次，等狀態變化再重新追蹤

    data = plc_mgr.plc_data
    if not data: return
    if plc_monitor_ui:
        plc_monitor_ui['trigger_val'].set_text(str(data.trigger))
        plc_monitor_ui['trigger_ind'].classes('text-green-500' if data.trigger else 'text-gray-500', remove='text-green-500 text-gray-500')
        plc_monitor_ui['empty_val'].set_text(str(data.empty_trigger))
        plc_monitor_ui['empty_ind'].classes('text-green-500' if data.empty_trigger else 'text-gray-500', remove='text-green-500 text-gray-500')
        plc_monitor_ui['heartbeat_val'].set_text(str(data.heartbeat))
        plc_monitor_ui['heartbeat_ind'].classes('text-green-500' if data.heartbeat else 'text-gray-500', remove='text-green-500 text-gray-500')
        plc_monitor_ui['cycle_val'].set_text(str(data.cycle_count))
        if 'cycle_val_top' in plc_monitor_ui: plc_monitor_ui['cycle_val_top'].set_text(str(data.cycle_count))
        plc_monitor_ui['bt_error_val'].set_text(f'0x{data.bt_error:04X}')
        plc_monitor_ui['reset_val'].set_text(str(data.reset))
        plc_monitor_ui['reset_ind'].classes('text-green-500' if data.reset else 'text-gray-500', remove='text-green-500 text-gray-500')
        if 'warmup_val' in plc_monitor_ui:
            plc_monitor_ui['warmup_val'].set_text(str(data.warmup))
            plc_monitor_ui['warmup_ind'].classes('text-green-500' if data.warmup else 'text-gray-500', remove='text-green-500 text-gray-500')
        # 更新頂部暖槍狀態
        if 'warmup_label' in plc_monitor_ui:
            if data.warmup:
                plc_monitor_ui['warmup_label'].set_text('暖槍中')
                plc_monitor_ui['warmup_label'].classes('text-orange-400', remove='text-gray-400')
            else:
                plc_monitor_ui['warmup_label'].set_text('OFF')
                plc_monitor_ui['warmup_label'].classes('text-gray-400', remove='text-orange-400')
        # 更新 D501~D512 判定結果
        for i in range(12):
            key = f'result_{i}'
            if key in plc_monitor_ui:
                val = data.results[i] if i < len(data.results) else 0
                plc_monitor_ui[key].set_text(str(val))
                if val == 2:
                    plc_monitor_ui[key].classes('text-green-400', remove='text-red-400 text-white')
                elif val == 1:
                    plc_monitor_ui[key].classes('text-red-400', remove='text-green-400 text-white')
                else:
                    plc_monitor_ui[key].classes('text-white', remove='text-green-400 text-red-400')
    for internal_ch in range(1, 13):
        if internal_ch in meters_ui:
            display_name = get_channel_display_name(internal_ch)
            try:
                logical_idx = int(display_name.replace('CH', '')) - 1
                if meters_ui[internal_ch]['ok_display']:
                    meters_ui[internal_ch]['ok_display'].set_value(data.ok_counts[logical_idx])
                if meters_ui[internal_ch]['ng_display']:
                    meters_ui[internal_ch]['ng_display'].set_value(data.ng_counts[logical_idx])
            except: pass

def build_settings_drawer():
    global settings_drawer, timing_inputs, plc_inputs, bt_inputs, bt_mac_inputs, net_inputs, mode_select, tolerance_upper_input, tolerance_lower_input, empty_upper_input, empty_lower_input, settings_logged_in, protected_sections, temp_anomaly_switch, temp_anomaly_upper_input, temp_anomaly_lower_input, temp_anomaly_fields, no_cover_anomaly_switch, no_cover_anomaly_count_input, no_cover_anomaly_fields
    is_master = config.network.mode == "master"
    protected_sections = []

    def update_protected_visibility():
        for section in protected_sections:
            section.set_visibility(settings_logged_in)

    def on_login_click(pwd_input, login_status):
        global settings_logged_in
        if pwd_input.value == SETTINGS_PASSWORD:
            settings_logged_in = True
            update_protected_visibility()
            login_status.set_text('已登入')
            login_status.classes('text-green-400', remove='text-red-400 text-gray-400')
            log_message("[設定] 管理者已登入")
        else:
            login_status.set_text('密碼錯誤')
            login_status.classes('text-red-400', remove='text-green-400 text-gray-400')

    def on_logout_click(login_status):
        global settings_logged_in
        settings_logged_in = False
        update_protected_visibility()
        login_status.set_text('未登入')
        login_status.classes('text-gray-400', remove='text-green-400 text-red-400')

    def on_judge_mode_change(e):
        mode = e.value
        if measure_manager:
            measure_manager.judge_mode = mode
        mode_map = {JudgeMode.NORMAL: ('正常判定', 'text-green-400'), JudgeMode.FORCE_OK: ('強制OK', 'text-yellow-400'), JudgeMode.FORCE_NG: ('強制NG', 'text-red-400')}
        label_text, color = mode_map[mode]
        # 更新頂部 UI 標籤
        if 'judge_mode_label' in plc_monitor_ui:
            lbl = plc_monitor_ui['judge_mode_label']
            lbl.set_text(label_text)
            lbl.classes(color, remove='text-green-400 text-yellow-400 text-red-400')
        log_message(f"[設定] 判定模式切換: {label_text}")

    def _toggle_temp_anomaly_fields(enabled):
        if temp_anomaly_fields:
            temp_anomaly_fields.set_visibility(enabled)
        # 關閉時立即重置 D513 bit12
        if not enabled and plc_manager:
            plc_manager.set_d513_bit(12, False)
            globals()['temp_anomaly_active'] = False

    def _toggle_no_cover_anomaly_fields(enabled):
        if no_cover_anomaly_fields:
            no_cover_anomaly_fields.set_visibility(enabled)
        # 關閉時立即重置 D513 bit13
        if not enabled and plc_manager:
            plc_manager.set_d513_bit(13, False)
            globals()['no_cover_anomaly_active'] = False
            no_cover_consecutive.clear()

    with ui.right_drawer(value=False, fixed=False).props('width=320 bordered').classes('bg-slate-900') as drawer:
        settings_drawer = drawer
        with ui.row().classes('w-full items-center justify-between p-2 bg-slate-800'):
            ui.label('進階設定').classes('text-lg text-white font-bold')
            ui.button(icon='close', on_click=toggle_settings).props('flat dense round color=white')
        with ui.scroll_area().classes('w-full h-full'):
            with ui.column().classes('w-full p-3 gap-4'):
                # --- 密碼登入區 ---
                with ui.card().classes('w-full bg-slate-700 p-2'):
                    with ui.row().classes('w-full items-center gap-2'):
                        ui.icon('lock', size='sm').classes('text-gray-300')
                        pwd_input = ui.input(placeholder='輸入管理密碼').props('outlined dense type=password').classes('flex-grow')
                        login_status = ui.label('未登入').classes('text-gray-400 text-sm')
                    with ui.row().classes('w-full gap-2 mt-1'):
                        ui.button('登入', on_click=lambda: on_login_click(pwd_input, login_status)).props('color=blue dense size=sm').classes('flex-grow')
                        ui.button('登出', on_click=lambda: on_logout_click(login_status)).props('color=grey dense size=sm').classes('flex-grow')

                # === 需要密碼的區塊 ===
                # --- 系統設定 ---
                with ui.column().classes('w-full gap-4') as sys_section:
                    protected_sections.append(sys_section)
                    sys_section.set_visibility(settings_logged_in)
                    with ui.expansion('系統設定', icon='settings').classes('w-full bg-slate-800').props('default-opened'):
                        with ui.column().classes('w-full gap-2 p-2'):
                            if is_master:
                                with ui.row().classes('items-center'):
                                    ui.label('誤差上限:').classes('text-gray-300 w-28')
                                    tolerance_upper_input = ui.number(value=config.measurement.tolerance_upper, format='%.2f', step=0.01).props('outlined dense suffix=°C').classes('w-24')
                                with ui.row().classes('items-center'):
                                    ui.label('誤差下限:').classes('text-gray-300 w-28')
                                    tolerance_lower_input = ui.number(value=config.measurement.tolerance_lower, format='%.2f', step=0.01).props('outlined dense suffix=°C').classes('w-24')
                                with ui.row().classes('items-center'):
                                    ui.label('空槍上限:').classes('text-gray-300 w-28')
                                    empty_upper_input = ui.number(value=config.measurement.empty_upper, format='%.2f', step=0.1).props('outlined dense suffix=°C').classes('w-24')
                                with ui.row().classes('items-center'):
                                    ui.label('空槍下限:').classes('text-gray-300 w-28')
                                    empty_lower_input = ui.number(value=config.measurement.empty_lower, format='%.2f', step=0.1).props('outlined dense suffix=°C').classes('w-24')
                                with ui.row().classes('items-center'):
                                    ui.label('判定模式:').classes('text-gray-300 w-28')
                                    ui.toggle({JudgeMode.NORMAL: '正常', JudgeMode.FORCE_OK: '強制OK', JudgeMode.FORCE_NG: '強制NG'}, value=JudgeMode.NORMAL, on_change=on_judge_mode_change).props('dense no-caps')
                                # --- 溫度異常設定 ---
                                ui.separator().classes('my-1')
                                with ui.row().classes('items-center'):
                                    ui.label('溫度異常:').classes('text-gray-300 w-28')
                                    temp_anomaly_switch = ui.switch(value=config.measurement.temp_anomaly_enabled, on_change=lambda e: _toggle_temp_anomaly_fields(e.value)).props('dense')
                                with ui.column().classes('w-full gap-2') as ta_fields:
                                    temp_anomaly_fields = ta_fields
                                    ta_fields.set_visibility(config.measurement.temp_anomaly_enabled)
                                    with ui.row().classes('items-center'):
                                        ui.label('溫度上限:').classes('text-gray-300 w-28')
                                        temp_anomaly_upper_input = ui.number(value=config.measurement.temp_anomaly_upper, format='%.1f', step=0.1).props('outlined dense suffix=°C').classes('w-24')
                                    with ui.row().classes('items-center'):
                                        ui.label('溫度下限:').classes('text-gray-300 w-28')
                                        temp_anomaly_lower_input = ui.number(value=config.measurement.temp_anomaly_lower, format='%.1f', step=0.1).props('outlined dense suffix=°C').classes('w-24')
                                # --- 連續無套異常設定 ---
                                ui.separator().classes('my-1')
                                with ui.row().classes('items-center'):
                                    ui.label('連續無套:').classes('text-gray-300 w-28')
                                    no_cover_anomaly_switch = ui.switch(value=config.measurement.no_cover_anomaly_enabled, on_change=lambda e: _toggle_no_cover_anomaly_fields(e.value)).props('dense')
                                with ui.column().classes('w-full gap-2') as nc_fields:
                                    no_cover_anomaly_fields = nc_fields
                                    nc_fields.set_visibility(config.measurement.no_cover_anomaly_enabled)
                                    with ui.row().classes('items-center'):
                                        ui.label('連續次數:').classes('text-gray-300 w-28')
                                        no_cover_anomaly_count_input = ui.number(value=config.measurement.no_cover_anomaly_count, format='%d', step=1, min=1).props('outlined dense suffix=次').classes('w-24')
                            ui.label('運行模式:').classes('text-gray-300 w-28')
                            mode_select = ui.select(options=['master', 'slave'], value=config.network.mode).props('outlined dense').classes('w-32')
                    # --- 網路設定 ---
                    with ui.expansion('網路設定', icon='lan').classes('w-full bg-slate-800'):
                        with ui.column().classes('w-full gap-2 p-2'):
                            with ui.row().classes('items-center'):
                                ui.label('Master IP:').classes('text-gray-300 w-28')
                                net_inputs['master_ip'] = ui.input(value=config.network.master_ip).props('outlined dense').classes('w-36')
                            with ui.row().classes('items-center'):
                                ui.label('Port:').classes('text-gray-300 w-28')
                                net_inputs['port'] = ui.number(value=config.network.port).props('outlined dense').classes('w-24')
                    if is_master:
                        # --- 時序設定 ---
                        with ui.expansion('時序設定', icon='timer').classes('w-full bg-slate-800'):
                            with ui.column().classes('w-full gap-2 p-2'):
                                for k, v in [('empty_collect_delay', '空槍收集延遲'), ('measure_collect_delay', '量測收集延遲')]:
                                    with ui.row().classes('items-center'):
                                        ui.label(v + ':').classes('text-gray-300 w-28')
                                        timing_inputs[k] = ui.number(value=getattr(config.timing, k), format='%.2f', min=0, max=10, step=0.1).props('outlined dense suffix=秒').classes('w-24')
                        # --- PLC 設定 ---
                        with ui.expansion('PLC 設定', icon='memory').classes('w-full bg-slate-800'):
                            with ui.column().classes('w-full gap-2 p-2'):
                                with ui.row().classes('items-center'):
                                    ui.label('IP 位址:').classes('text-gray-300 w-28')
                                    plc_inputs['ip_address'] = ui.input(value=config.plc.ip_address).props('outlined dense').classes('w-36')
                                with ui.row().classes('items-center'):
                                    ui.label('Port:').classes('text-gray-300 w-28')
                                    plc_inputs['port'] = ui.number(value=config.plc.port).props('outlined dense').classes('w-24')

                # === 不需要密碼的區塊 ===
                with ui.expansion('藍芽設定', icon='bluetooth').classes('w-full bg-slate-800'):
                    with ui.column().classes('w-full gap-2 p-2'):
                        mac_channels = range(1, 7) if config.network.mode == "master" else range(7, 13)
                        for ch in mac_channels:
                            idx = ch - 1 if ch <= 6 else ch - 7
                            addr = config.bluetooth.device_addresses[idx] if idx < len(config.bluetooth.device_addresses) else ""
                            with ui.row().classes('items-center'):
                                ui.label(f'{get_channel_display_name(ch)}:').classes('text-gray-300 w-14')
                                bt_mac_inputs[ch] = ui.input(value=addr, placeholder='XX:XX:XX:XX:XX:XX').props('outlined dense').classes('flex-grow')
                        with ui.row().classes('items-center'):
                            ui.label('重連間隔:').classes('text-gray-300 w-28')
                            bt_inputs['reconnect_interval'] = ui.number(value=config.bluetooth.reconnect_interval).props('outlined dense suffix=秒').classes('w-24')
                        with ui.row().classes('items-center'):
                            ui.label('超時:').classes('text-gray-300 w-28')
                            bt_inputs['timeout'] = ui.number(value=config.bluetooth.timeout).props('outlined dense suffix=秒').classes('w-24')
                with ui.expansion('通道啟用', icon='toggle_on').classes('w-full bg-slate-800'):
                    ch_range = range(1, 13) if is_master else range(7, 13)
                    with ui.grid(columns=6).classes('w-full gap-1'):
                        for i in ch_range:
                            with ui.column().classes('items-center'):
                                ui.label(get_channel_display_name(i)).classes('text-gray-300 text-[10px]')
                                channel_switches[i] = ui.switch(value=config.measurement.channel_enabled[i-1]).props('dense')
                ui.button('儲存進階設定', on_click=on_save_advanced_settings).props('color=blue icon=save').classes('w-full mt-4')
                ui.button('更新資料 (即時套用)', on_click=on_apply_settings).props('color=green icon=sync').classes('w-full mt-2')

def build_meter_block(title: str, start_ch: int, end_ch: int, border_color: str = 'blue'):
    is_master = config.network.mode == "master"
    with ui.card().classes(f'bg-slate-800 p-3 border-l-4 border-{border_color}-500').style('min-width: 600px'):
        ui.label(title).classes(f'text-xl text-{border_color}-300 font-bold mb-2')
        headers = [('CH', 'w-16'), ('BT', 'w-8'), ('耳套', 'w-12'), ('空槍值', 'w-24'), ('量測值', 'w-24'), ('誤差', 'w-24'), ('狀態', 'w-24')]
        if is_master:
            headers += [('OK', 'w-14'), ('NG', 'w-14'), ('無套', 'w-10')]
        with ui.row().classes('w-full gap-x-2 items-center border-b border-gray-600 pb-1'):
            for label, w in headers:
                ui.label(label).classes(f'font-bold text-gray-400 text-base {w}')
        for i in range(start_ch, end_ch + 1):
            is_enabled = config.measurement.channel_enabled[i - 1]
            with ui.row().classes('w-full gap-x-2 items-center py-1' + ('' if is_enabled else ' opacity-40')) as row_container:
                with ui.row().classes('items-center gap-1 w-16'):
                    ui.label(get_channel_display_name(i)).classes('text-white text-xl font-mono font-bold')
                    disabled_badge = ui.badge('停用', color='red').props('dense').classes('text-xs')
                    disabled_badge.set_visibility(not is_enabled)
                bt_icon = ui.icon('bluetooth', color='gray').classes('text-xl w-8')
                ear_cover_label = ui.label('--').classes('text-gray-500 text-base font-bold w-12 text-center')
                empty_display = ui.number(value=0.00, format='%.2f').props('readonly borderless dense input-class="text-cyan-400 text-xl font-bold"').classes('w-24')
                temp_display = ui.number(value=0.00, format='%.2f').props('readonly borderless dense input-class="text-yellow-400 text-xl font-bold"').classes('w-24')
                error_display = ui.number(value=0.00, format='%.2f').props('readonly borderless dense input-class="text-white text-xl font-bold"').classes('w-24')
                with ui.row().classes('items-center gap-1 bg-slate-900 rounded px-2 py-1 w-24'):
                    status_light = ui.icon('circle').props('size=20px color=grey')
                    status_text = ui.label('WAIT').classes('font-bold text-gray-500 text-lg w-12')
                if is_master:
                    ok_display = ui.number(value=0, format='%d').props('readonly borderless dense input-class="text-green-400 text-xl font-bold"').classes('w-14')
                    ng_display = ui.number(value=0, format='%d').props('readonly borderless dense input-class="text-red-400 text-xl font-bold"').classes('w-14')
                    no_cover_count_label = ui.label('0').classes('text-gray-400 text-xl font-bold font-mono w-10 text-center')
                else:
                    ok_display = None
                    ng_display = None
                    no_cover_count_label = None
            meters_ui[i] = {'row_container': row_container, 'disabled_badge': disabled_badge, 'bt_icon': bt_icon, 'ear_cover': ear_cover_label, 'empty_display': empty_display, 'temp_display': temp_display, 'error_display': error_display, 'light': status_light, 'text': status_text, 'ok_display': ok_display, 'ng_display': ng_display, 'no_cover_count': no_cover_count_label}

def build_ui():
    global meters_ui, log_console, plc_status_icon, network_status_icon, alert_container, alert_message_label, system_status_label, measure_status_label, plc_monitor_ui, current_batch_label
    is_master = config.network.mode == "master"
    ui.colors(primary='#5898d4', secondary='#26a69a', accent='#9c27b0', dark='#1d1d1d')
    ui.add_head_html('<style>body { user-select: text !important; -webkit-user-select: text !important; }</style>')
    ui.keyboard(on_key=lambda e: ui.run_javascript('window.location.reload()') if e.key.f5 and e.action.keydown else None)
    build_settings_drawer()
    with ui.column().classes('w-full p-2 gap-2'):
        with ui.card().classes('w-full bg-slate-900 border-b-2 border-blue-500 p-3'):
            with ui.row().classes('w-full justify-between items-center'):
                with ui.row().classes('items-center gap-3'):
                    ui.icon('medical_services', size='lg', color='blue')
                    ui.label(config.title).classes('text-3xl text-white font-bold')
                    ui.label(f'v{config.version}').classes('text-base text-gray-400')
                    ui.badge('MASTER' if is_master else 'SLAVE', color='blue' if is_master else 'orange').classes('text-lg px-3 py-1')
                    
                    # 顯示目前批次
                    if is_master:
                        batch_text = f'目前批次: {config.current_batch}' if config.current_batch else '目前批次: --'
                        current_batch_label = ui.label(batch_text).classes('text-blue-300 text-lg font-mono ml-4')
                        ui.button('換批', on_click=on_new_batch_click).props('color=blue icon=fiber_new outline').classes('ml-2')

                    if is_master:
                        ui.label('|').classes('text-gray-600 text-2xl mx-2')
                        with ui.row().classes('items-center gap-2 bg-slate-800 px-4 py-1 rounded-full border border-gray-700'):
                            ui.label('測試週期:').classes('text-gray-400 text-lg')
                            plc_monitor_ui['cycle_val_top'] = ui.label('0').classes('text-yellow-400 text-2xl font-bold font-mono')
                    ui.label('|').classes('text-gray-600 text-2xl mx-2')
                    with ui.row().classes('items-center gap-2 bg-slate-800 px-4 py-1 rounded-full border border-gray-700'):
                        ui.label('系統狀態:').classes('text-gray-400 text-lg')
                        # 根據實際運行狀態設定初始文字與顏色
                        text = '運行中' if system_running else '已停止'
                        color = 'text-green-400' if system_running else 'text-red-400'
                        system_status_label = ui.label(text).classes(f'{color} text-2xl font-bold')
                    if is_master:
                        ui.label('|').classes('text-gray-600 text-2xl mx-2')
                        with ui.row().classes('items-center gap-2 bg-slate-800 px-4 py-1 rounded-full border border-gray-700'):
                            ui.label('判定模式:').classes('text-gray-400 text-lg')
                            judge_mode_label = ui.label('正常判定').classes('text-green-400 text-2xl font-bold')
                            plc_monitor_ui['judge_mode_label'] = judge_mode_label
                        ui.label('|').classes('text-gray-600 text-2xl mx-2')
                        with ui.row().classes('items-center gap-2 bg-slate-800 px-4 py-1 rounded-full border border-gray-700'):
                            ui.label('暖槍:').classes('text-gray-400 text-lg')
                            plc_monitor_ui['warmup_label'] = ui.label('OFF').classes('text-gray-400 text-2xl font-bold')
                with ui.row().classes('items-center gap-6'):
                    if is_master:
                        with ui.row().classes('items-center gap-2'):
                            ui.label('PLC:').classes('text-gray-300 text-xl'); plc_status_icon = ui.icon('circle', color='gray').classes('text-2xl')
                    with ui.row().classes('items-center gap-2'):
                        ui.label('網路:').classes('text-gray-300 text-xl'); network_status_icon = ui.icon('circle', color='gray').classes('text-2xl')
                    if is_master:
                        ui.button('異常復歸', icon='restart_alt', on_click=on_reset_button_click) \
                            .props('color=amber text-color=black dense size=lg') \
                            .classes('px-4')
                    ui.button(icon='settings', on_click=toggle_settings).props('flat round color=white size=lg')
        with ui.card().classes('w-full bg-red-600 p-3 border-2 border-red-400') as container:
            alert_container = container; alert_container.set_visibility(False)
            with ui.row().classes('w-full items-center justify-between'):
                with ui.row().classes('items-center gap-3'):
                    ui.icon('warning', size='lg', color='white')
                    alert_message_label = ui.label('').classes('text-2xl text-white font-bold')
                ui.button('確認', on_click=stop_alert_flash).props('color=white text-color=red dense size=lg').classes('px-6')
        with ui.row().classes('w-full items-start gap-3'):
            if is_master: build_meter_block('本機通道 (CH11, 9, 7, 5, 3, 1)', 1, 6, 'blue')
            if is_master: build_meter_block('Slave 通道 (CH12, 10, 8, 6, 4, 2)', 7, 12, 'orange')
            else: build_meter_block('本機通道 (CH12, 10, 8, 6, 4, 2)', 7, 12, 'orange')
            with ui.column().classes('flex-grow gap-3').style('max-width: 450px'):
                with ui.card().classes('w-full bg-slate-800 p-3'):
                    ui.label('目前設定').classes('text-lg text-white font-bold mb-2')
                    with ui.row().classes('items-center gap-4'):
                        ui.label('上限:').classes('text-gray-400 text-base'); ui.label(f'+{config.measurement.tolerance_upper:.2f}°C').classes('text-green-400 text-xl font-bold')
                        ui.label('下限:').classes('text-gray-400 text-base'); ui.label(f'-{config.measurement.tolerance_lower:.2f}°C').classes('text-red-400 text-xl font-bold')
                    if is_master:
                        with ui.row().classes('items-center gap-4 mt-1'):
                            ui.label('空槍上限:').classes('text-gray-400 text-base'); ui.label(f'{config.measurement.empty_upper:.1f}°C').classes('text-orange-400 text-xl font-bold')
                            ui.label('空槍下限:').classes('text-gray-400 text-base'); ui.label(f'{config.measurement.empty_lower:.1f}°C').classes('text-cyan-400 text-xl font-bold')
                with ui.card().classes('w-full bg-slate-800 p-3'):
                    with ui.row().classes('w-full items-center justify-between mb-2'):
                        ui.label('量測流程').classes('text-lg text-white font-bold')
                        # 根據實際量測狀態設定初始文字
                        state_text = "待機中"
                        if measure_manager:
                            state_map = {
                                MeasurementState.IDLE: "待機中",
                                MeasurementState.WAITING_EMPTY: "等待空槍",
                                MeasurementState.EMPTY_DONE: "空槍完成",
                                MeasurementState.WAITING_MEASURE: "等待量測",
                                MeasurementState.MEASURING: "計算中",
                                MeasurementState.COMPLETE: "量測完成",
                            }
                            state_text = state_map.get(measure_manager.state, "待機中")
                        measure_status_label = ui.label(state_text).classes('text-gray-400 text-xl font-bold')
                    with ui.row().classes('w-full gap-2'):
                        ui.button('清空Log', on_click=lambda: log_console.clear()).props('color=grey icon=delete size=lg').classes('text-lg')
                if is_master:
                    with ui.card().classes('w-full bg-slate-700 p-3'):
                        ui.label('手動觸發').classes('text-lg text-yellow-400 font-bold mb-2')
                        with ui.row().classes('gap-2'):
                            ui.button('空槍量測', on_click=on_simulate_empty).props('color=cyan icon=science size=lg')
                            ui.button('溫度量測', on_click=on_simulate_measure).props('color=orange icon=thermostat size=lg')
        with ui.row().classes('w-full items-stretch gap-3'):
            if is_master:
                with ui.card().classes('bg-slate-800 p-3').style('min-width: 320px'):
                    ui.label('PLC 監控').classes('text-lg text-purple-300 font-bold mb-2')
                    with ui.column().classes('w-full gap-1'):
                        for k, n, d in [('trigger', '量測觸發 D500', True), ('empty', '空槍觸發 D515', True), ('heartbeat', 'PC 心跳 D514', True), ('cycle', '測試週期 D516', False), ('bt_error', 'BT 錯誤 D513', False), ('reset', '異常復歸 D541', True), ('warmup', '暖槍訊號 D542', True)]:
                            with ui.row().classes('w-full items-center justify-between'):
                                ui.label(n).classes('text-gray-400 text-base')
                                with ui.row().classes('items-center gap-2'):
                                    plc_monitor_ui[k+'_val'] = ui.label('0').classes('text-white text-base font-mono')
                                    if d: plc_monitor_ui[k+'_ind'] = ui.icon('circle', size='xs').classes('text-gray-500')
                    ui.separator().classes('my-2')
                    ui.label('判定結果 D501~D512').classes('text-sm text-purple-200 font-bold mb-1')
                    with ui.grid(columns=4).classes('w-full gap-1'):
                        for i in range(12):
                            with ui.row().classes('items-center gap-1'):
                                ui.label(f'CH{i+1}').classes('text-gray-400 text-xs w-8')
                                plc_monitor_ui[f'result_{i}'] = ui.label('0').classes('text-white text-sm font-mono')
            with ui.card().classes('bg-slate-800 p-3 flex-grow').style('min-width: 600px'):
                ui.label('系統 Log').classes('text-lg text-blue-300 font-bold mb-2')
                log_console = ui.log(max_lines=100).classes('w-full text-base text-gray-300 font-mono').style('height: 100%; min-height: 300px')

@ui.page('/')
def main_page():
    build_ui()
    ui.timer(0.5, update_plc_display)
    def sync():
        objs = globals()
        bt_mgr = objs.get('bt_manager')
        if bt_mgr:
            for ch in bt_mgr.devices.keys(): update_meter_bt_status(ch, bt_mgr.get_device_state(ch))
    ui.timer(1.5, sync, once=True)
    def sync_network():
        if net_manager and network_status_icon:
            on_network_state(net_manager.state)
    ui.timer(2.0, sync_network, once=True)

if __name__ in {"__main__", "__mp_main__"}:
    if multiprocessing.current_process().name == 'MainProcess':
        try:
            import ctypes; app_id = 'chingtech.meter.hmi.v1'
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except: pass
        init_managers()

    def handle_shutdown():
        global is_shutting_down
        is_shutting_down = True
        print("系統正在關閉，正在清理資源...")
        try:
            objs = globals()
            if objs.get('bt_manager'):
                bt_manager.stop()
                # 等待藍芽執行緒結束，確保 socket 真正關閉
                for t in bt_manager._threads:
                    t.join(timeout=3)
                print("[SHUTDOWN] 藍芽連線已全部關閉")
            if objs.get('plc_manager'):
                plc_manager.stop_monitoring()
                print("[SHUTDOWN] PLC 連線已關閉")
            if objs.get('net_manager'): net_manager.stop()
        except Exception as e:
            print(f"[SHUTDOWN] 清理異常: {e}")
        finally:
            import os; os._exit(0)

    app.native.window_args['confirm_close'] = True
    app.on_shutdown(handle_shutdown)
    # 注意: pywebview 的 icon 參數僅支援 GTK/QT，Windows (EdgeChromium) 不支援
    # Master 用 port 8080, Slave 用 port 8081，同一台電腦可同時跑兩個實例
    ui_port = 8080 if config.network.mode == "master" else 8081
    ui.run(title=config.title, dark=True, native=True, port=ui_port, window_size=(config.window_width, config.window_height), favicon='meter32x32.ico', reload=False, show=False)
