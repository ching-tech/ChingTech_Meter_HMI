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
import queue

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

def _read_machine_name_early():
    """在 config 模組載入前，輕量讀取 config.json 的 machine_name (供 debug log 檔名後綴)。
    路徑邏輯與 config.py 一致 (程式資料夾外的 ChingTech_Meter_HMI_config)，並支援 --config。"""
    try:
        import json
        cfg_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "ChingTech_Meter_HMI_config", "config.json")
        for _i, _a in enumerate(sys.argv):
            if _a == "--config" and _i + 1 < len(sys.argv):
                cfg_file = sys.argv[_i + 1]
        if os.path.exists(cfg_file):
            with open(cfg_file, encoding='utf-8') as f:
                name = (json.load(f).get('machine_name') or '').strip()
                if name:
                    return name
    except Exception:
        pass
    return 'Machine'

if multiprocessing.current_process().name == 'MainProcess':
    # 優先寫 D:\debug_log；若 D 槽不存在或無權限，fallback 回程式資料夾下 logs/
    _debug_log_dir = r'D:\debug_log'
    try:
        os.makedirs(_debug_log_dir, exist_ok=True)
    except Exception as _e:
        _debug_log_dir = 'logs'
        os.makedirs(_debug_log_dir, exist_ok=True)
        print(f'[!] 無法建立 D:\\debug_log ({_e})，debug log fallback 到 {_debug_log_dir}/')
    _machine = _read_machine_name_early()
    _log_filename = os.path.join(_debug_log_dir, f'debug_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}_{_machine}.txt')
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
_net_data_recv_at = {}  # Master 收到 Slave 各通道「真資料」封包的本機時刻 {ch: master_clock_ts}
_net_data_value = {}    # Master 收到 Slave 各通道最新「真量測」溫度值 {ch: temp}；排除純狀態封包
_consumed_ts = {}       # 各通道上一輪已採用的推送時間戳 {ch: ts}；新鮮度基準 (本輪推送須超過此值)
_manual_trigger = False # 手動擷取旗標：True=本次 D515/D500 來自 UI 手動擷取 (發 CD)，False=生產 (純推送+漏壓偵測)
_bt_disconnect_timers = {}  # 藍芽斷線去抖動 {logical_num: (threading.Timer, since_ts)}
_bt_confirmed_down = {}      # 已確認斷線並回報 PLC 的通道 {logical_num: 上次「仍斷線」提醒時刻}
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
batch_no_input = None       # 批號輸入框
machine_name_input = None   # 設定頁的機台名稱輸入框
total_ok_label = None       # 頂部 TOTAL OK 大字
total_ng_label = None       # 頂部 TOTAL NG 大字
temp_anomaly_status_label = None    # 頂部「溫度異常: ON/OFF」狀態
no_cover_anomaly_status_label = None # 頂部「連續無套: ON/OFF」狀態
current_settings_labels = {}  # 「目前設定」面板的 label refs (供設定變更後即時刷新)
plc_sim_switch = None       # 設定頁的 PLC 模擬模式開關
bt_sim_switch = None        # 設定頁的藍芽模擬模式開關
remote_log_dir_input = None    # 遠端 log 路徑輸入框
remote_alarm_dir_input = None  # 遠端 alarm 路徑輸入框
_startup_reset_checked = False  # 啟動時跨日歸零檢查只執行一次
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
warmup_empty_threshold_input = None
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
        simulation_mode=config.bt_simulation_mode,
        connect_timeout=config.bluetooth.timeout,
        reconnect_interval=config.bluetooth.reconnect_interval,
        max_parallel_connects=config.bluetooth.max_parallel_connects
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
            simulation_mode=config.plc_simulation_mode
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
    
    # 確保今日 log 檔案存在 (僅 Master 需要寫入 CSV)
    if measure_manager and config.network.mode == "master":
        filepath = measure_manager.ensure_today_log_file(config.machine_name)
        if filepath:
            log_message(f"今日記錄檔: {os.path.basename(filepath)}")

def is_channel_enabled(channel: int) -> bool:
    if 1 <= channel <= 12:
        return config.measurement.channel_enabled[channel - 1]
    return False

def _is_logical_channel_enabled(logical_num: int) -> bool:
    """logical_num = 顯示通道號 (CHx)；找出對應內部通道判斷是否啟用
    (_bt_confirmed_down / D513 bit 以 logical 編號，is_channel_enabled 以內部編號)。"""
    for internal, name in CHANNEL_DISPLAY_NAMES.items():
        if name == f'CH{logical_num}':
            return is_channel_enabled(internal)
    return is_channel_enabled(logical_num)

def on_bluetooth_data(channel: int, data: ThermometerData):
    global ear_cover_statuses
    display_name = get_channel_display_name(channel)
    ear_cover = "有耳溫套" if data.trans_temp_raw == "1111" else "無耳溫套"
    source = "CD回應" if bt_manager.consume_cd_flag(channel) else "主動推送"
    log_message(f"[BT] {display_name}: {data.temperature}°C ({ear_cover}, {source})")
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

# 藍芽斷線去抖動：藍芽常瞬斷後 1~2 秒自動重連，若立即寫 PLC 斷線異常會讓機台被瞬斷誤觸暫停。
# 斷線需「持續」此秒數仍未恢復才報給 PLC + 跳警報；期間恢復連線則視為無事。
_BT_DISCONNECT_DEBOUNCE = 3.0
_BT_DISCONNECT_REMIND = 30.0   # 確認斷線後，每隔此秒數 log 一次「仍斷線」提醒

def _handle_bt_state_change(channel: int, state: ConnectionState, source: str = ""):
    """統一處理藍芽連線狀態變更 (Master 本機 + Slave 通道共用)。
    可見性與動作分開：icon 由呼叫端即時更新；瞬斷會即時寫 log，但「跳警報 + 寫 PLC」
    需斷線持續 _BT_DISCONNECT_DEBOUNCE 秒仍未恢復才執行，避免瞬斷瞬連誤觸機台暫停。
    source: 顯示用後綴，例如 ' (Slave)'。呼叫前 prev_bt_states[channel] 須已更新為最新狀態。"""
    display_name = get_channel_display_name(channel)
    try:
        logical_num = int(display_name.replace('CH', ''))
    except ValueError:
        logical_num = channel

    if state == ConnectionState.CONNECTED:
        entry = _bt_disconnect_timers.pop(logical_num, None)
        if entry:
            entry[0].cancel()
        if logical_num in _bt_confirmed_down:
            # 真正長斷線後恢復：解除 PLC 異常 + 停警報
            _bt_confirmed_down.pop(logical_num, None)
            log_message(f"[恢復] {display_name}{source} 藍芽已連線")
            stop_alert_flash()
            if plc_manager: plc_manager.set_bt_error(logical_num, False)
        elif entry:
            # 觀察期內就恢復 = 瞬斷，未觸發任何異常
            dur = time.time() - entry[1]
            log_message(f"[BT] {display_name}{source} 已恢復 (中斷 {dur:.1f}s，未觸發異常)")
        else:
            # 初次連線 (開機/啟用)
            log_message(f"[BT] {display_name}{source} 藍芽已連線")
    elif state == ConnectionState.CONNECTING:
        # 連線中：初始連線或重連過程，不視為斷線 (真正斷線由 DISCONNECTED/ERROR 認定)
        return
    else:
        # DISCONNECTED / ERROR
        if logical_num in _bt_confirmed_down or logical_num in _bt_disconnect_timers:
            return  # 已回報或已在觀察中，不重複
        log_message(f"[BT] {display_name}{source} 連線中斷，觀察 {_BT_DISCONNECT_DEBOUNCE:.0f}s…")
        def _confirm_disconnect():
            _bt_disconnect_timers.pop(logical_num, None)
            # 到期時若已恢復連線、或通道已被停用，就不報
            if prev_bt_states.get(channel) == ConnectionState.CONNECTED or not is_channel_enabled(channel):
                return
            _bt_confirmed_down[logical_num] = time.time()
            log_message(f"[警告] {display_name}{source} 藍芽斷線確認 (持續 {_BT_DISCONNECT_DEBOUNCE:.0f}s)!")
            show_bt_disconnect_alert(channel)
            if plc_manager: plc_manager.set_bt_error(logical_num, True)
        timer = threading.Timer(_BT_DISCONNECT_DEBOUNCE, _confirm_disconnect)
        _bt_disconnect_timers[logical_num] = (timer, time.time())
        timer.start()

def on_bluetooth_state(channel: int, state: ConnectionState):
    global prev_bt_states
    update_meter_bt_status(channel, state)
    old_state = prev_bt_states.get(channel)
    prev_bt_states[channel] = state

    if is_channel_enabled(channel) and state != old_state:
        _handle_bt_state_change(channel, state)

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

def _write_alarm_line(alarm_dir: str, today: str, line: str, machine_suffix: str = ""):
    """將一筆 alarm 寫入指定資料夾；檔案被鎖定時自動 fallback 到 _1 副檔。"""
    os.makedirs(alarm_dir, exist_ok=True)
    base = f"alarm_{today}{machine_suffix}"
    filepath = os.path.join(alarm_dir, f"{base}.csv")
    write_header = not os.path.exists(filepath)
    try:
        with open(filepath, "a", encoding="utf-8-sig") as f:
            if write_header:
                f.write("日期時間,異常類型,詳細內容\n")
            f.write(line)
    except PermissionError:
        fallback = os.path.join(alarm_dir, f"{base}_1.csv")
        write_header_fb = not os.path.exists(fallback)
        with open(fallback, "a", encoding="utf-8-sig") as f:
            if write_header_fb:
                f.write("日期時間,異常類型,詳細內容\n")
            f.write(line)
        print(f"[!] Alarm CSV 被鎖定，已寫入備用: {os.path.basename(fallback)}")

# --- 遠端 Alarm 非同步佇列 ---
# 本機寫入維持同步 (磁碟 ~1ms 不會卡)；遠端 SMB 改 daemon thread 避免阻塞 UI/量測流程。
# 失敗時用指數退避重試 (1, 2, 4, 8, 16, 32, 60, 60...)，永不放棄；若遠端持續離線，
# 佇列會累積 (alarm 量低，實務上不會爆)。
_alarm_remote_queue: "queue.Queue" = queue.Queue()

def _alarm_remote_worker():
    """背景 thread：消費 alarm 佇列，把 alarm 寫到遠端資料夾。"""
    while True:
        try:
            item = _alarm_remote_queue.get()
            if item is None:  # 關機 poison pill (目前未啟用)
                break
            path, today, line, machine_suffix, attempt = item
            try:
                _write_alarm_line(path, today, line, machine_suffix=machine_suffix)
                if attempt > 0:
                    print(f"[*] 遠端 alarm 寫入恢復成功 (重試第 {attempt} 次)")
            except Exception as e:
                backoff = min(60, 2 ** attempt)
                print(f"[!] 遠端 alarm 寫入失敗 (第 {attempt + 1} 次嘗試，{backoff}s 後重試): {e}")
                time.sleep(backoff)
                _alarm_remote_queue.put((path, today, line, machine_suffix, attempt + 1))
        except Exception as e:
            print(f"[!!] alarm worker 未預期錯誤: {e}")

threading.Thread(target=_alarm_remote_worker, daemon=True, name="AlarmRemoteWorker").start()


# --- 遠端 Cycle Log 非同步佇列 ---
# 同樣的設計：本機 log 維持同步寫；遠端 summary log (批號/時間/TOTAL OK/TOTAL NG) 改 daemon thread。
# 失敗指數退避重試，永不放棄；佇列累積到上限後拒絕新筆 (本機 log 仍正常寫，不影響資料完整性)。
# 上限 100000 ≈ 3 秒/筆 × 約 83 小時，遠超過單班斷線時間；本機 log 永遠是 source of truth。
_remote_log_queue: "queue.Queue" = queue.Queue(maxsize=100000)


def _write_remote_summary_line(remote_dir: str, machine_name: str,
                               batch_no: str, time_str: str,
                               total_ok: int, total_ng: int):
    """把一筆簡化版 cycle log 寫到遠端 summary CSV (檔名加機台名與日期)。"""
    os.makedirs(remote_dir, exist_ok=True)
    date_str = datetime.datetime.now().strftime("%Y%m%d")
    safe_machine = (machine_name or "Machine").strip() or "Machine"
    filename = f"summary_log_{date_str}_{safe_machine}.csv"
    filepath = os.path.join(remote_dir, filename)
    write_header = not os.path.exists(filepath)
    with open(filepath, 'a', newline='', encoding='utf-8-sig') as f:
        import csv as _csv
        writer = _csv.writer(f)
        if write_header:
            writer.writerow(['批號', '時間', 'TOTAL OK', 'TOTAL NG'])
        writer.writerow([batch_no or "", time_str, total_ok, total_ng])


def _remote_log_worker():
    """背景 thread：消費 cycle log 佇列，寫到遠端資料夾，失敗指數退避重試。"""
    while True:
        try:
            item = _remote_log_queue.get()
            if item is None:
                break
            remote_dir, machine_name, batch_no, time_str, total_ok, total_ng, attempt = item
            try:
                _write_remote_summary_line(remote_dir, machine_name, batch_no,
                                           time_str, total_ok, total_ng)
                if attempt > 0:
                    print(f"[*] 遠端 cycle log 寫入恢復成功 (重試第 {attempt} 次)")
            except Exception as e:
                backoff = min(60, 2 ** attempt)
                print(f"[!] 遠端 cycle log 寫入失敗 (第 {attempt + 1} 次嘗試，{backoff}s 後重試): {e}")
                time.sleep(backoff)
                try:
                    _remote_log_queue.put_nowait(
                        (remote_dir, machine_name, batch_no, time_str, total_ok, total_ng, attempt + 1)
                    )
                except queue.Full:
                    print(f"[!!] 遠端 cycle log 佇列已滿，丟棄重試任務以避免無限累積")
        except Exception as e:
            print(f"[!!] cycle log worker 未預期錯誤: {e}")


threading.Thread(target=_remote_log_worker, daemon=True, name="RemoteLogWorker").start()


def _enqueue_remote_cycle_log(plc_data):
    """把當下這筆 cycle 的 summary 資料丟進遠端 log 佇列；無 remote_log_dir 直接 skip"""
    if not config.remote_log_dir:
        return
    time_str = datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    total_ok = sum(plc_data.ok_counts[:12]) if plc_data else 0
    total_ng = sum(plc_data.ng_counts[:12]) if plc_data else 0
    try:
        _remote_log_queue.put_nowait((
            config.remote_log_dir,
            config.machine_name,
            config.batch_no,
            time_str,
            total_ok,
            total_ng,
            0,  # attempt
        ))
    except queue.Full:
        print("[!!] 遠端 cycle log 佇列已滿 (10000 筆)，本筆 summary 已丟棄；本機 log 不受影響")


def write_alarm_log(message: str, alarm_type: str = "其他"):
    """寫入歷史異常紀錄 CSV (一天一個檔案)：本機同步 + 遠端 async (丟佇列)"""
    today = datetime.datetime.now().strftime("%Y%m%d")
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_message = message.replace('"', '""')
    line = f'{timestamp},{alarm_type},"{safe_message}"\n'
    safe_machine = (config.machine_name or "Machine").strip() or "Machine"
    machine_suffix = f"_{safe_machine}"

    # 本機寫入 (同步)：跟 cycle log 共用 config.log_dir，放在 Alarm 子資料夾；檔名加機台後綴 (與遠端一致)
    try:
        _write_alarm_line(os.path.join(config.log_dir, "Alarm"), today, line, machine_suffix=machine_suffix)
    except Exception as e:
        err = f"[!] 寫入本機 Alarm Log 失敗: {e}"
        print(err)
        try: log_message(err)
        except: pass

    # 遠端寫入：丟進佇列由 daemon worker 處理，呼叫端不阻塞
    if config.remote_alarm_dir:
        _alarm_remote_queue.put((
            config.remote_alarm_dir,
            today,
            line,
            machine_suffix,
            0,  # attempt 計數
        ))

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
    show_alert(f'{display_name} 藍芽斷線!', alarm_type="藍芽斷線")

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
        range_txt = f'(範圍: {config.measurement.temp_anomaly_lower:.2f}~{config.measurement.temp_anomaly_upper:.2f}°C)'
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
        # 清 0 後，對「仍斷線且仍啟用」的通道重新寫回 bt_error：藍芽是持續性硬體狀態，
        # 復歸不該讓還在斷線的槍被忽略 (溫度/無套/空槍/漏壓等是逐次條件，下輪會重評，故不重寫)。
        # 已停用的通道不重寫、並從追蹤移除，避免停用後復歸又把它的 bit 點亮造成停機。
        if _bt_confirmed_down:
            now = time.time()
            reasserted = []
            for logical in list(_bt_confirmed_down.keys()):
                if not _is_logical_channel_enabled(logical):
                    _bt_confirmed_down.pop(logical, None)
                    continue
                plc_manager.set_bt_error(logical, True)
                _bt_confirmed_down[logical] = now   # 重置「仍斷線」提醒計時
                reasserted.append(logical)
            if reasserted:
                log_message(f"[PLC] 復歸後重寫仍斷線通道 D513: CH{sorted(reasserted)}")

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

# --- 量測取值說明 ---
# 量測值由「PLC 壓桿物理接觸」觸發 BT 槍主動 auto-push (cb_test 實測：閒置不推、只壓桿才推)。
# 生產不發 CD (CD 只回快取值、不觸發新量測、漏壓時會回舊值)；手動擷取無實體壓桿則發 CD。
# 兩者皆以「本輪未收到新推送」判漏壓 → 統一回報 (D513 bit15 + NG + 警報)。
# 觸發後到開始收集的延遲由 config.timing.empty_collect_delay / measure_collect_delay 控制 (UI 可調)。


def _channel_latest_ts(channel: int) -> float:
    """通道最新「真資料」的時間戳 (皆為 Master 本機時鐘)。
    本機 BT 用 last_data.timestamp；Slave 用 Master 收到真資料封包時刻 (_net_data_recv_at)。"""
    if bt_manager and channel in bt_manager.devices:
        d = bt_manager.get_last_data(channel)
        if d:
            return d.timestamp
    return _net_data_recv_at.get(channel, 0.0)


def _channel_latest_value(channel: int):
    """通道最新一筆「真量測」溫度值；本機 BT 用 last_data、Slave 用 _net_data_value。無真值回 None。
    Slave 一律走 _net_data_value (只記真資料封包)，避免純狀態封包 (temp=0) 污染成假 0.0。"""
    if bt_manager and channel in bt_manager.devices:
        d = bt_manager.get_last_data(channel)
        if d:
            return d.temperature
    return _net_data_value.get(channel)


def _wait_fresh_pushes(enabled: set, timeout: float) -> set:
    """輪詢直到所有啟用通道都收到「比上一輪基準 (_consumed_ts) 更新」的推送，或逾時。
    回傳已收到新推送的通道集合 (生產模式中，未在此集合者即漏壓)。"""
    deadline = time.time() + timeout
    def fresh():
        return {ch for ch in enabled if _channel_latest_ts(ch) > _consumed_ts.get(ch, 0.0)}
    got = fresh()
    while got < enabled and time.time() < deadline:
        time.sleep(0.1)
        got = fresh()
    return got


def _report_missed(missed: set, is_empty: bool):
    """漏壓回報：設 D513 bit15 + 跳警報。漏壓通道值留 None (start_*_measurement 已清空，不再寫入)。
    該通道 D501~D512 的 NG 由量測完成時 (on_measurement_complete) 對 None 自然判 NG 寫入。"""
    if not missed:
        return
    names = [get_channel_display_name(ch) for ch in sorted(missed)]
    label = "空槍" if is_empty else "量測"
    log_message(f"[漏壓] {label}: 未收到量測值 {names}")
    show_alert(f'未收到量測值: {", ".join(names)}（未壓到/未觸發）', alarm_type="漏壓")
    for ch in missed:
        set_meter_highlight(ch, True)   # 主畫面標紅該通道
    if plc_manager:
        plc_manager.set_d513_bit(15, True)


def _acquire_and_collect(is_empty: bool, is_manual: bool):
    """取值並收集判定。
    - 手動 (UI 手動擷取，無實體壓桿)：先發 CD + 通知 Slave，讓槍回快取值。
    - 生產：不發 CD，純等主動推送。
    之後兩者一致：以新鮮度判斷收齊、timeout 放行；未收到本輪新值的啟用通道即漏壓，
    統一走 _report_missed (D513 bit15 + NG + 警報)。"""
    label = "空槍" if is_empty else "量測"
    enabled = set(get_enabled_channel_list())
    miss_timeout = config.bluetooth.miss_timeout

    if is_manual:
        log_message(f"[流程] {label}(手動): 發送 BT CD + 通知 Slave 取快取值")
        if config.network.mode == "master" and net_manager:
            net_manager.send_command("request_empty" if is_empty else "request_measure")
        for channel in bt_manager.devices.keys():
            if is_channel_enabled(channel):
                bt_manager.request_measurement(channel)
    else:
        log_message(f"[流程] {label}: 等待主動推送 (逾時 {miss_timeout:.1f}s)…")

    # 手動/生產一致：新鮮度判斷收齊，未收到新值者即漏壓
    got = _wait_fresh_pushes(enabled, miss_timeout)
    missed = enabled - got

    # 更新基準時間戳 (只更新有收到新推送的通道，漏壓通道維持舊基準)
    for ch in got:
        _consumed_ts[ch] = _channel_latest_ts(ch)

    log_message(f"[流程] {label}: 收齊 {len(got)}/{len(enabled)} 通道，開始收集")
    if is_empty:
        collect_empty_values(got, missed)
    else:
        collect_measure_values(got, missed)


def on_plc_empty_trigger():
    global _d515_triggered_at, _manual_trigger
    _d515_triggered_at = time.time()
    is_manual = _manual_trigger
    _manual_trigger = False   # 消費旗標
    enabled = get_enabled_channel_list()
    log_message(f"[PLC] D515=1 空槍量測觸發{'(手動)' if is_manual else ''} (啟用通道: {len(enabled)}個 {[get_channel_display_name(c) for c in enabled]})")
    clear_meter_values(is_empty=True)
    try:
        measure_manager.start_empty_measurement()
        if config.bt_simulation_mode: bt_manager.set_simulation_mode_empty()
        delay = max(0.0, config.timing.empty_collect_delay)
        log_message(f"[流程] 空槍: {delay}s 後開始收集")
        threading.Timer(delay, lambda: trigger_empty_ack_and_collect(is_manual)).start()
    except Exception as e:
        log_message(f"[錯誤] 無法啟動空槍量測: {e}")
        import traceback; traceback.print_exc()

def trigger_empty_ack_and_collect(is_manual: bool = False):
    try:
        _acquire_and_collect(is_empty=True, is_manual=is_manual)
    except Exception as e:
        log_message(f"[錯誤] trigger_empty_ack_and_collect 異常: {e}")
        import traceback; traceback.print_exc()

def on_plc_measure_trigger():
    global _d500_triggered_at, _manual_trigger
    _d500_triggered_at = time.time()
    is_manual = _manual_trigger
    _manual_trigger = False
    enabled = get_enabled_channel_list()
    log_message(f"[PLC] D500=1 溫度量測觸發{'(手動)' if is_manual else ''} (啟用通道: {len(enabled)}個 {[get_channel_display_name(c) for c in enabled]})")
    clear_meter_values(is_empty=False)
    try:
        measure_manager.start_temperature_measurement()
        if config.bt_simulation_mode: bt_manager.set_simulation_mode_measure()
        delay = max(0.0, config.timing.measure_collect_delay)
        log_message(f"[流程] 量測: {delay}s 後開始收集")
        threading.Timer(delay, lambda: trigger_measure_ack_and_collect(is_manual)).start()
    except Exception as e:
        log_message(f"[錯誤] 無法執行溫度量測: {e}")
        import traceback; traceback.print_exc()

def trigger_measure_ack_and_collect(is_manual: bool = False):
    try:
        _acquire_and_collect(is_empty=False, is_manual=is_manual)
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
                _handle_bt_state_change(ch, bt_state, source=" (Slave)")
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
        # 真資料封包：記錄收到時刻 (本機時鐘，供新鮮度判斷) 與真量測值 (供取值，避免狀態封包 0.0 污染)
        _net_data_recv_at[ch] = time.time()
        _net_data_value[ch] = packet.temperature
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
        if config.bt_simulation_mode and bt_manager:
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
        if config.bt_simulation_mode and bt_manager:
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
            enabled_channels=get_enabled_channel_list(),
            batch_no=config.batch_no,
            machine_name=config.machine_name,
        )
        log_message(f"[流程] 量測 Log 寫入: {'成功' if log_saved else '失敗'}")
        # 遠端簡化版 log → 丟佇列由 worker 非同步處理 (不阻塞量測流程)
        if log_saved:
            _enqueue_remote_cycle_log(plc_manager.plc_data if plc_manager else None)

    if plc_manager:
        # 0=OK, 1=NG, 2=不使用
        logical_results = [2]*12
        current_results = measure_manager.get_results()
        for internal_ch in range(1, 13):
            display_name = get_channel_display_name(internal_ch)
            logical_idx = int(display_name.replace('CH', '')) - 1
            if is_channel_enabled(internal_ch):
                logical_results[logical_idx] = 0 if current_results[internal_ch - 1] else 1
            # 未啟用的通道保持 2 (不使用)

        log_message(f"[流程] 寫入 PLC 判定結果 D501~D512: {logical_results}")
        # 寫判定結果 (D501~D512): 0=OK, 1=NG, 2=不使用
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

_batch_revert_timer = None

def _schedule_batch_revert():
    """輸入框失焦時排程還原；若 150ms 內按下 SAVE，timer 會被取消"""
    global _batch_revert_timer
    if _batch_revert_timer:
        _batch_revert_timer.cancel()

    def _revert():
        global _batch_revert_timer
        _batch_revert_timer = None
        if batch_no_input and (batch_no_input.value or "") != config.batch_no:
            batch_no_input.set_value(config.batch_no)

    _batch_revert_timer = ui.timer(0.15, _revert, once=True)

def on_batch_no_commit():
    """提交批號：驗證僅英數字，寫入 config 並持久化"""
    global _batch_revert_timer
    # 先取消還原排程，避免 SAVE 後又被還原成舊值
    if _batch_revert_timer:
        _batch_revert_timer.cancel()
        _batch_revert_timer = None
    if not batch_no_input:
        return
    raw = (batch_no_input.value or "").strip()
    import re
    if raw and not re.fullmatch(r'[A-Za-z0-9]+', raw):
        ui.notify("批號僅能為英文字母與數字", type='negative')
        batch_no_input.set_value(config.batch_no)
        return
    if raw == config.batch_no:
        return
    config.batch_no = raw
    save_config(config)
    ui.notify(f"批號已設定: {raw or '(空白)'}", type='positive')
    log_message(f"[批號] 設定為: {raw or '(空白)'}")

def on_force_clear_triggers():
    """『流程解卡』按鈕：強制將 D500 與 D515 寫 0，並重置量測狀態機"""
    def do_clear():
        if plc_manager:
            plc_manager.write_complete_signal()   # D500 = 0
            plc_manager.clear_empty_trigger()     # D515 = 0
        # 重置觸發時戳，避免馬上又跳超時警報
        globals()['_d500_triggered_at'] = 0.0
        globals()['_d515_triggered_at'] = 0.0
        # 重置量測狀態機回 IDLE
        if measure_manager:
            measure_manager.reset()
        # 清除 UI 上的量測值顯示
        for meter in meters_ui.values():
            try:
                with meter['light'].client:
                    meter['empty_display'].set_value(0.00)
                    meter['temp_display'].set_value(0.00)
                    meter['error_display'].set_value(0.00)
                    meter['light'].props('color=grey')
                    meter['text'].set_text('WAIT')
                    meter['text'].classes('text-gray-500', remove='text-green-500 text-red-500')
            except Exception:
                pass
        ui.notify("已強制將 D500 / D515 歸零，量測狀態已重置", type='warning')
        log_message("[流程] 使用者強制解卡：D500/D515 → 0，狀態機 → IDLE")
        dialog.close()

    with ui.dialog() as dialog, ui.card().classes('bg-slate-800'):
        with ui.row().classes('items-center gap-3 mb-2'):
            ui.icon('build_circle', size='md', color='red')
            ui.label('強制流程解卡').classes('text-xl text-white font-bold')
        ui.label('此操作會強制將 PLC 的 D500 (量測觸發) 與 D515 (空槍觸發) 寫 0，').classes('text-gray-300')
        ui.label('並重置 HMI 量測狀態機，使後續流程能繼續。').classes('text-gray-300')
        ui.label('僅在流程明顯卡住時使用，是否繼續？').classes('text-yellow-300 mt-1')
        with ui.row().classes('w-full justify-end gap-2 mt-3'):
            ui.button('取消', on_click=dialog.close).props('flat color=grey')
            ui.button('確認解卡', icon='build_circle', on_click=do_clear).props('color=red')
    dialog.open()

def _maybe_daily_reset_on_startup():
    """啟動連上 PLC 後執行一次跨日檢查：
       - last_reset_date 與今日相同 → 沿用既有計數 (避免同日重啟把累積數歸零)
       - 不同 → 自動歸零並更新日期
       PLC 寫入失敗時 flag 保持 False，下次 poll 看到 CONNECTED 會重試。
    """
    global _startup_reset_checked
    if _startup_reset_checked:
        return
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    last = (config.last_reset_date or "").strip()
    if last == today:
        log_message(f"[啟動] 與上次歸零同日 ({today})，沿用 PLC 既有計數")
        _startup_reset_checked = True
        return
    log_message(f"[啟動] 跨日自動歸零 (上次: {last or '無紀錄'} → 今日: {today})")
    if _do_reset_counts(reason="啟動跨日自動歸零"):
        _startup_reset_checked = True
    # 若失敗，flag 保持 False，下次 update_plc_display 看到 CONNECTED 時會重試

def _do_reset_counts(reason: str) -> bool:
    """共用歸零邏輯：寫 PLC 0 + 清 UI；只在 PLC 寫入成功時才更新 last_reset_date 並存檔。
    回傳 True 表示 PLC 寫入成功 (或無 plc_manager 時視同成功)；False 表示寫入失敗。"""
    plc_ok = True
    if plc_manager:
        plc_ok = plc_manager.write_ok_ng_counts([0] * 12, [0] * 12)
    # UI 一律更新 (給使用者即時視覺回饋；PLC 重連後若值不一致會被 poll 蓋回正確值)
    for meter in meters_ui.values():
        if meter.get('ok_display'):
            meter['ok_display'].set_value(0)
        if meter.get('ng_display'):
            meter['ng_display'].set_value(0)
    if total_ok_label is not None:
        total_ok_label.set_text('0')
    if total_ng_label is not None:
        total_ng_label.set_text('0')
    if plc_ok:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        config.last_reset_date = today
        save_config(config)
        log_message(f"[計數] 已歸零並記錄日期 {today} (原因: {reason})")
    else:
        log_message(f"[計數] PLC 寫入失敗，未更新 last_reset_date，連線恢復後會自動重試 (原因: {reason})")
    return plc_ok

def on_reset_count_click():
    """點擊『計數歸零』按鈕：先彈出確認視窗，確認後才執行歸零"""
    def do_reset():
        ok = _do_reset_counts(reason="使用者手動歸零")
        if ok:
            ui.notify("OK/NG 計數已歸零", type='positive')
        else:
            ui.notify("PLC 寫入失敗：UI 已歸零但 PLC 未清，連線恢復後請再按一次", type='warning')
        dialog.close()

    with ui.dialog() as dialog, ui.card().classes('bg-slate-800'):
        with ui.row().classes('items-center gap-3 mb-2'):
            ui.icon('warning', size='md', color='orange')
            ui.label('確認計數歸零').classes('text-xl text-white font-bold')
        ui.label('將清除 PLC 與畫面上所有 OK/NG 計數，此動作無法復原。').classes('text-gray-300')
        ui.label('是否繼續？').classes('text-gray-300 mt-1')
        with ui.row().classes('w-full justify-end gap-2 mt-3'):
            ui.button('取消', on_click=dialog.close).props('flat color=grey')
            ui.button('確認歸零', icon='exposure_zero', on_click=do_reset).props('color=orange')
    dialog.open()

def get_enabled_channel_list():
    """取得已啟用的通道列表"""
    return [ch for ch in range(1, 13) if is_channel_enabled(ch)]

def collect_empty_values(got: set, missed: set):
    """收集空槍值。got=本輪收到新推送的通道、missed=漏壓通道。
    漏壓通道不採值 (start_empty_measurement 已清空 → 維持 None)，並回報 D513 bit15 + 警報。"""
    # 清除停用通道的殘留資料
    for ch in range(1, 13):
        if not is_channel_enabled(ch):
            measure_manager.clear_channel(ch)

    # 只採用「本輪有新推送」的通道值
    values = {}
    for ch in sorted(got):
        v = _channel_latest_value(ch)
        if v is not None:
            values[ch] = v
    if values:
        log_message(f"[流程] 空槍收集: {[f'{get_channel_display_name(ch)}={v:.2f}' for ch, v in values.items()]}")

    # 漏壓回報 (留 None、設 bit15、跳警報)
    _report_missed(missed, is_empty=True)
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

    range_txt = f'(範圍: {config.measurement.empty_lower:.2f}~{config.measurement.empty_upper:.2f}°C)'

    if out_of_range:
        if is_warmup:
            # 暖槍中 D542=1：累計超限次數，達設定次數才發出警報
            empty_out_of_range_count += 1
            threshold = config.measurement.warmup_empty_threshold
            log_message(f"[暖槍] 空槍值超限 第{empty_out_of_range_count}/{threshold}次: {', '.join(out_of_range)}")
            if empty_out_of_range_count >= threshold:
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
            enabled_channels=get_enabled_channel_list(),
            batch_no=config.batch_no,
            machine_name=config.machine_name,
        )
        # 遠端簡化版 log → 丟佇列由 worker 非同步處理
        if log_saved:
            _enqueue_remote_cycle_log(plc_manager.plc_data if plc_manager else None)

    update_plc_display()
    if plc_manager and log_saved:
        plc_manager.clear_empty_trigger()
        globals()['_d515_triggered_at'] = 0.0
        log_message("[PLC] 空槍值已寫入 Log，清除 D515")

def collect_measure_values(got: set, missed: set):
    """收集量測值。got=本輪收到新推送的通道、missed=漏壓通道。
    漏壓通道不採值 (start_temperature_measurement 已清空 measure → 維持 None →
    不算誤差、不判 PASS → 量測完成時自然寫 NG)，並回報 D513 bit15 + 警報。"""
    # 清除停用通道的殘留資料
    for ch in range(1, 13):
        if not is_channel_enabled(ch):
            measure_manager.clear_channel(ch)

    # 只採用「本輪有新推送」的通道值
    values = {}
    for ch in sorted(got):
        v = _channel_latest_value(ch)
        if v is not None:
            values[ch] = v
    if values:
        log_message(f"[流程] 量測收集: {[f'{get_channel_display_name(ch)}={v:.2f}' for ch, v in values.items()]}")

    # 漏壓回報 (留 None、設 bit15、跳警報)
    _report_missed(missed, is_empty=False)

    measure_manager.record_measure_values(values)
    # 強制結束本輪：漏壓通道 (有空槍無量測) 不阻擋 finalize → 觸發 on_measurement_complete
    # 寫入 D501~D512 (漏壓通道 None → 非 PASS → NG) 並將 D500 歸 0 完成握手
    measure_manager.force_finalize()

    # D500=1 量測觸發：對有值通道做異常檢測
    if config.network.mode == "master":
        check_temp_anomaly_all(values)
        covers = {ch: ear_cover_statuses[ch] for ch in values if ch in ear_cover_statuses}
        check_no_cover_anomaly_all(covers)

    update_plc_display()

def _on_channel_toggle(ch: int, enabled: bool):
    """通道啟用開關切換即時生效：更新 config、存檔、刷新顯示 (含清 D513/斷線追蹤)、同步對端。
    停用後 bt_manager 下個迴圈即跳過該通道不再連線；該通道 D513 bit 立即清除。"""
    if 1 <= ch <= 12:
        config.measurement.channel_enabled[ch - 1] = bool(enabled)
    save_config(config)
    update_channel_disabled_display()
    _sync_channel_enabled_to_peer()
    log_message(f"[設定] {get_channel_display_name(ch)} 已{'啟用' if enabled else '停用'}")

def update_channel_disabled_display():
    for ch, meter in meters_ui.items():
        enabled = is_channel_enabled(ch)
        meter['disabled_badge'].set_visibility(not enabled)
        if enabled: meter['row_container'].classes(remove='opacity-40')
        else: meter['row_container'].classes('opacity-40')
    # 停用通道：清掉斷線追蹤與 D513 錯誤位元，避免「仍斷線」提醒/異常復歸又把它重寫 ON
    for ch in range(1, 13):
        if not is_channel_enabled(ch):
            try:
                logical_num = int(get_channel_display_name(ch).replace('CH', ''))
            except:
                logical_num = ch
            _bt_confirmed_down.pop(logical_num, None)        # 不再追蹤 (提醒/復歸不再碰)
            t = _bt_disconnect_timers.pop(logical_num, None)  # 取消待確認的斷線計時
            if t:
                t[0].cancel()
            if plc_manager:
                plc_manager.set_bt_error(logical_num, False)  # 清 D513 該 bit

def _refresh_ui_from_config():
    """從 config 刷新所有設定面板 UI 元件的顯示值"""
    # 機台名稱
    if machine_name_input: machine_name_input.set_value(config.machine_name)
    # 量測參數 (設定面板輸入框)
    if tolerance_upper_input: tolerance_upper_input.set_value(abs(config.measurement.tolerance_upper))
    if tolerance_lower_input: tolerance_lower_input.set_value(abs(config.measurement.tolerance_lower))
    if empty_upper_input: empty_upper_input.set_value(config.measurement.empty_upper)
    if empty_lower_input: empty_lower_input.set_value(config.measurement.empty_lower)
    if warmup_empty_threshold_input: warmup_empty_threshold_input.set_value(config.measurement.warmup_empty_threshold)
    # 「目前設定」面板的顯示 label (主畫面右側)
    if current_settings_labels.get('tol_upper'):
        current_settings_labels['tol_upper'].set_text(f'+{abs(config.measurement.tolerance_upper):.2f}°C')
    if current_settings_labels.get('tol_lower'):
        current_settings_labels['tol_lower'].set_text(f'-{abs(config.measurement.tolerance_lower):.2f}°C')
    if current_settings_labels.get('empty_upper'):
        current_settings_labels['empty_upper'].set_text(f'{config.measurement.empty_upper:.2f}°C')
    if current_settings_labels.get('empty_lower'):
        current_settings_labels['empty_lower'].set_text(f'{config.measurement.empty_lower:.2f}°C')
    if current_settings_labels.get('temp_upper'):
        current_settings_labels['temp_upper'].set_text(f'{config.measurement.temp_anomaly_upper:.2f}°C')
    if current_settings_labels.get('temp_lower'):
        current_settings_labels['temp_lower'].set_text(f'{config.measurement.temp_anomaly_lower:.2f}°C')
    # 頂部 banner 的「溫度異常 / 連續無套」使用狀態
    if temp_anomaly_status_label is not None:
        on = config.measurement.temp_anomaly_enabled
        temp_anomaly_status_label.set_text('ON' if on else 'OFF')
        temp_anomaly_status_label.classes(
            ('text-green-400' if on else 'text-gray-500'),
            remove='text-green-400 text-gray-500')
    if no_cover_anomaly_status_label is not None:
        on = config.measurement.no_cover_anomaly_enabled
        no_cover_anomaly_status_label.set_text('ON' if on else 'OFF')
        no_cover_anomaly_status_label.classes(
            ('text-green-400' if on else 'text-gray-500'),
            remove='text-green-400 text-gray-500')
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
    if bt_inputs.get('max_parallel_connects'): bt_inputs['max_parallel_connects'].set_value(config.bluetooth.max_parallel_connects)
    if bt_inputs.get('miss_timeout'): bt_inputs['miss_timeout'].set_value(config.bluetooth.miss_timeout)
    for ch, mac_input in bt_mac_inputs.items():
        idx = ch - 1 if ch <= 6 else ch - 7
        if idx < len(config.bluetooth.device_addresses):
            mac_input.set_value(config.bluetooth.device_addresses[idx])
    # 通道啟用開關
    for ch, sw in channel_switches.items():
        sw.set_value(config.measurement.channel_enabled[ch - 1])
    # 模擬模式
    if plc_sim_switch is not None: plc_sim_switch.set_value(config.plc_simulation_mode)
    if bt_sim_switch is not None: bt_sim_switch.set_value(config.bt_simulation_mode)
    # 遠端記錄路徑
    if remote_log_dir_input is not None: remote_log_dir_input.set_value(config.remote_log_dir)
    if remote_alarm_dir_input is not None: remote_alarm_dir_input.set_value(config.remote_alarm_dir)

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
    if total_ok_label is not None: total_ok_label.set_text('0')
    if total_ng_label is not None: total_ng_label.set_text('0')
    if plc_manager: plc_manager.write_ok_ng_counts([0]*12, [0]*12)
    stop_alert_flash()
    if config.bt_simulation_mode: bt_manager.reset_simulation()
    log_message("資料重設")

def toggle_settings():
    if settings_drawer: settings_drawer.toggle()

def _pick_directory(input_field, title: str = '選擇資料夾'):
    """彈出系統選資料夾對話框 (tkinter)，把選到的路徑寫回 input_field。
    僅 kiosk 場景使用 (NiceGUI 與瀏覽器同機)。"""
    def _do():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            initial = (input_field.value or "").strip()
            if not initial or not os.path.isdir(initial):
                initial = os.path.expanduser('~')
            path = filedialog.askdirectory(initialdir=initial, title=title)
            root.destroy()
            if path:
                input_field.set_value(os.path.normpath(path))
        except Exception as e:
            print(f"[!] 選擇資料夾失敗: {e}")
    threading.Thread(target=_do, daemon=True).start()

def _collect_settings_from_ui():
    """從 UI 收集所有設定值寫入 config"""
    # 機台名稱 (僅允許英數字、底線、連字號，作為檔名一部分)
    # 注意: 空字串/格式錯誤皆「保留原值」，不會被改回預設 "Machine1"
    if machine_name_input:
        import re
        raw = (machine_name_input.value or "").strip()
        if not raw:
            # 空字串：保留原值，並把 UI 還原回原值
            machine_name_input.set_value(config.machine_name)
            ui.notify("機台名稱不可為空，已還原原值", type='warning')
        elif not re.fullmatch(r'[A-Za-z0-9_\-]+', raw):
            ui.notify("機台名稱僅能為英數字、底線或連字號", type='negative')
            machine_name_input.set_value(config.machine_name)
        else:
            config.machine_name = raw
    # 誤差上下限統一存正值 magnitude；abs() 防止使用者繞過 min=0 限制
    if tolerance_upper_input: config.measurement.tolerance_upper = abs(float(tolerance_upper_input.value or 0))
    if tolerance_lower_input: config.measurement.tolerance_lower = abs(float(tolerance_lower_input.value or 0))
    if empty_upper_input: config.measurement.empty_upper = empty_upper_input.value
    if empty_lower_input: config.measurement.empty_lower = empty_lower_input.value
    if warmup_empty_threshold_input: config.measurement.warmup_empty_threshold = int(warmup_empty_threshold_input.value)
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
    if bt_inputs.get('max_parallel_connects'):
        config.bluetooth.max_parallel_connects = max(1, min(6, int(bt_inputs['max_parallel_connects'].value)))
    if bt_inputs.get('miss_timeout'):
        try: config.bluetooth.miss_timeout = max(0.5, float(bt_inputs['miss_timeout'].value))
        except (TypeError, ValueError): pass
    # 同步到 bt_manager（運行中即時生效）
    if bt_manager:
        bt_manager.connect_timeout = config.bluetooth.timeout
        bt_manager.reconnect_interval = config.bluetooth.reconnect_interval
        bt_manager.max_parallel_connects = config.bluetooth.max_parallel_connects
    for ch, mac_input in bt_mac_inputs.items():
        idx = ch - 1 if ch <= 6 else ch - 7
        if idx < len(config.bluetooth.device_addresses): config.bluetooth.device_addresses[idx] = mac_input.value
    for i in range(1, 13):
        if i in channel_switches: config.measurement.channel_enabled[i-1] = channel_switches[i].value
    # 模擬模式 (PLC / 藍芽各自獨立)
    if plc_sim_switch is not None: config.plc_simulation_mode = bool(plc_sim_switch.value)
    if bt_sim_switch is not None: config.bt_simulation_mode = bool(bt_sim_switch.value)
    # 遠端記錄路徑 (log / alarm 各自獨立)
    if remote_log_dir_input is not None: config.remote_log_dir = (remote_log_dir_input.value or "").strip()
    if remote_alarm_dir_input is not None: config.remote_alarm_dir = (remote_alarm_dir_input.value or "").strip()

def on_save_advanced_settings():
    _collect_settings_from_ui()
    measure_manager.set_tolerance(config.measurement.tolerance_upper, config.measurement.tolerance_lower)
    update_channel_disabled_display()
    saved = save_config(config)
    _sync_channel_enabled_to_peer()
    _refresh_ui_from_config()
    if saved:
        ui.notify('進階設定已儲存', type='positive')
    else:
        ui.notify('儲存失敗：原始 config.json 可能損毀，已拒絕覆寫，請檢查 console 訊息', type='negative')

def on_apply_settings():
    """儲存設定並即時套用到所有執行中的管理器（不需重啟程式）"""
    global bt_manager, plc_manager, net_manager, system_running

    _collect_settings_from_ui()
    saved = save_config(config)
    if not saved:
        ui.notify('儲存失敗：原始 config.json 可能損毀，已拒絕覆寫；本次只套用到記憶體', type='warning')

    log_message("[設定] 開始套用新參數...")

    # 1. 量測管理器：更新誤差容許值
    measure_manager.set_tolerance(config.measurement.tolerance_upper, config.measurement.tolerance_lower)
    log_message(f"[設定] 誤差範圍: +{abs(config.measurement.tolerance_upper):.2f} / -{abs(config.measurement.tolerance_lower):.2f}")

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

    # 5a. 模擬模式：即時更新 manager 屬性 (下次連線生效；建議重啟程式以完全切換)
    sim_changed = False
    if bt_manager and bt_manager.simulation_mode != config.bt_simulation_mode:
        bt_manager.simulation_mode = config.bt_simulation_mode
        log_message(f"[設定] 藍芽模擬模式: {config.bt_simulation_mode} (建議重啟程式以完全生效)")
        sim_changed = True
    if plc_manager and plc_manager.simulation_mode != config.plc_simulation_mode:
        plc_manager.simulation_mode = config.plc_simulation_mode
        log_message(f"[設定] PLC 模擬模式: {config.plc_simulation_mode} (建議重啟程式以完全生效)")
        sim_changed = True
    if sim_changed:
        ui.notify('模擬模式已切換，建議重啟程式以完全生效', type='warning')

    # 6. 時序設定直接生效（量測流程讀取 config.timing.*）
    log_message(f"[設定] 時序參數已更新")

    # 7. 雙向同步通道啟用狀態
    _sync_channel_enabled_to_peer()

    # 8. 刷新 UI 顯示值（確保 UI 與 config 一致）
    _refresh_ui_from_config()

    log_message("[設定] 所有參數已套用完成")
    ui.notify('設定已即時套用', type='positive')

def on_simulate_empty():
    # 手動擷取：無實體壓桿 → 設旗標讓觸發走「發 CD 取快取值」模式，不做漏壓偵測
    globals()['_manual_trigger'] = True
    if plc_manager: plc_manager.write_empty_trigger(1)
    else: on_plc_empty_trigger()

def on_simulate_measure():
    globals()['_manual_trigger'] = True
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

    # 斷線持續提醒：已確認斷線的通道每 _BT_DISCONNECT_REMIND 秒 log 一次 (Master/Slave 皆執行)
    if _bt_confirmed_down:
        _now = time.time()
        for _ln, _last in list(_bt_confirmed_down.items()):
            if _now - _last >= _BT_DISCONNECT_REMIND:
                _bt_confirmed_down[_ln] = _now
                log_message(f"[警告] CH{_ln} 藍芽仍斷線 (持續中)")

    if not plc_mgr or not bt_mgr: return
    if plc_status_icon:
        s = plc_mgr.state
        if s == PLCConnectionState.CONNECTED: plc_status_icon.props('color=green')
        elif s == PLCConnectionState.CONNECTING: plc_status_icon.props('color=yellow')
        else: plc_status_icon.props('color=red')
        # PLC 第一次連線成功時，做跨日歸零檢查 (整個程式生命週期只跑一次)
        if s == PLCConnectionState.CONNECTED:
            _maybe_daily_reset_on_startup()
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
                val = data.results[i] if i < len(data.results) else 2
                plc_monitor_ui[key].set_text(str(val))
                if val == 0:
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
    # 更新頂部 TOTAL OK / TOTAL NG (12 通道加總)
    if total_ok_label is not None:
        total_ok_label.set_text(str(sum(data.ok_counts[:12])))
    if total_ng_label is not None:
        total_ng_label.set_text(str(sum(data.ng_counts[:12])))

def build_settings_drawer():
    global settings_drawer, timing_inputs, plc_inputs, bt_inputs, bt_mac_inputs, net_inputs, mode_select, tolerance_upper_input, tolerance_lower_input, empty_upper_input, empty_lower_input, warmup_empty_threshold_input, settings_logged_in, protected_sections, temp_anomaly_switch, temp_anomaly_upper_input, temp_anomaly_lower_input, temp_anomaly_fields, no_cover_anomaly_switch, no_cover_anomaly_count_input, no_cover_anomaly_fields, machine_name_input, plc_sim_switch, bt_sim_switch, remote_log_dir_input, remote_alarm_dir_input
    is_master = config.network.mode == "master"
    protected_sections = []

    def update_protected_visibility():
        for section in protected_sections:
            section.set_visibility(settings_logged_in)

    def on_login_click(pwd_input, login_status):
        global settings_logged_in
        valid_passwords = {SETTINGS_PASSWORD}
        extra = (config.extra_password or "").strip()
        if extra:
            valid_passwords.add(extra)
        if pwd_input.value in valid_passwords:
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
            ui.button(icon='close', on_click=toggle_settings).props('flat dense round color=white') \
                .tooltip('關閉設定面板')
        with ui.scroll_area().classes('w-full h-full'):
            with ui.column().classes('w-full p-3 gap-4'):
                # --- 密碼登入區 ---
                with ui.card().classes('w-full bg-slate-700 p-2'):
                    with ui.row().classes('w-full items-center gap-2'):
                        ui.icon('lock', size='sm').classes('text-gray-300')
                        pwd_input = ui.input(placeholder='輸入管理密碼').props('outlined dense type=password').classes('flex-grow')
                        login_status = ui.label('未登入').classes('text-gray-400 text-sm')
                    with ui.row().classes('w-full gap-2 mt-1'):
                        ui.button('登入', on_click=lambda: on_login_click(pwd_input, login_status)).props('color=blue dense size=sm').classes('flex-grow') \
                            .tooltip('解鎖密碼保護的進階設定區塊（系統設定、網路、PLC、模擬、遠端記錄等）')
                        ui.button('登出', on_click=lambda: on_logout_click(login_status)).props('color=grey dense size=sm').classes('flex-grow') \
                            .tooltip('鎖回密碼保護區塊（進階設定隱藏，避免誤改）')

                # === 需要密碼的區塊 ===
                # --- 系統設定 ---
                with ui.column().classes('w-full gap-4') as sys_section:
                    protected_sections.append(sys_section)
                    sys_section.set_visibility(settings_logged_in)
                    with ui.expansion('系統設定', icon='settings').classes('w-full bg-slate-800').props('default-opened'):
                        with ui.column().classes('w-full gap-2 p-2'):
                            with ui.row().classes('items-center'):
                                ui.label('機台名稱:').classes('text-gray-300 w-28')
                                machine_name_input = ui.input(value=config.machine_name, placeholder='例如 Machine1') \
                                    .props('outlined dense').classes('w-36') \
                                    .tooltip('機台識別名稱，會用於 log/alarm 檔名後綴；多台共用同一遠端資料夾時用此區分。限英數字、底線、連字號')
                            if is_master:
                                with ui.row().classes('items-center'):
                                    ui.label('+ 上限:').classes('text-gray-300 w-28')
                                    tolerance_upper_input = ui.number(value=abs(config.measurement.tolerance_upper), format='%.2f', step=0.01, min=0).props('outlined dense suffix=°C').classes('w-24') \
                                        .tooltip('量測誤差容許上限（正值）。量測值高於基準值此範圍以內視為 PASS；超過判 NG')
                                with ui.row().classes('items-center'):
                                    ui.label('- 下限:').classes('text-gray-300 w-28')
                                    tolerance_lower_input = ui.number(value=abs(config.measurement.tolerance_lower), format='%.2f', step=0.01, min=0).props('outlined dense suffix=°C').classes('w-24') \
                                        .tooltip('量測誤差容許下限（正值，系統會自動加負號）。量測值低於基準值此範圍以內視為 PASS；超過判 NG')
                                with ui.row().classes('items-center'):
                                    ui.label('空槍上限:').classes('text-gray-300 w-28')
                                    empty_upper_input = ui.number(value=config.measurement.empty_upper, format='%.2f', step=0.01).props('outlined dense suffix=°C').classes('w-24') \
                                        .tooltip('空槍量測 (D515) 時可接受的環境/槍體溫度上限。空槍值若高於此會視為空槍量測異常')
                                with ui.row().classes('items-center'):
                                    ui.label('空槍下限:').classes('text-gray-300 w-28')
                                    empty_lower_input = ui.number(value=config.measurement.empty_lower, format='%.2f', step=0.01).props('outlined dense suffix=°C').classes('w-24') \
                                        .tooltip('空槍量測 (D515) 時可接受的環境/槍體溫度下限。空槍值若低於此會視為空槍量測異常')
                                with ui.row().classes('items-center'):
                                    ui.label('暖槍連續異常次數:').classes('text-gray-300 w-36')
                                    warmup_empty_threshold_input = ui.number(value=config.measurement.warmup_empty_threshold, format='%d', step=1, min=1).props('outlined dense suffix=次').classes('w-24') \
                                        .tooltip('暖槍中 (D542=1) 空槍值超限累計達此次數才跳警報（預設 3 次）。中間任一次正常即歸零。非暖槍時任一次超限即跳警報，此設定不影響')
                                with ui.row().classes('items-center'):
                                    ui.label('判定模式:').classes('text-gray-300 w-28')
                                    ui.toggle({JudgeMode.NORMAL: '正常', JudgeMode.FORCE_OK: '強制OK', JudgeMode.FORCE_NG: '強制NG'}, value=JudgeMode.NORMAL, on_change=on_judge_mode_change).props('dense no-caps') \
                                        .tooltip('正常: 依誤差判定 PASS/FAIL；強制OK: 全部視為 PASS；強制NG: 全部視為 FAIL（測試/校正用）')
                                # --- 溫度異常設定 ---
                                ui.separator().classes('my-1')
                                with ui.row().classes('items-center'):
                                    ui.label('溫度異常:').classes('text-gray-300 w-28')
                                    temp_anomaly_switch = ui.switch(value=config.measurement.temp_anomaly_enabled, on_change=lambda e: _toggle_temp_anomaly_fields(e.value)).props('dense') \
                                        .tooltip('啟用後會檢查每次量測值是否在「溫度上下限」範圍內，超出會跳警報並寫 PLC D513.bit12。常用於防呆（避免量到 5°C 或 50°C 這類異常值）')
                                with ui.column().classes('w-full gap-2') as ta_fields:
                                    temp_anomaly_fields = ta_fields
                                    ta_fields.set_visibility(config.measurement.temp_anomaly_enabled)
                                    with ui.row().classes('items-center'):
                                        ui.label('溫度上限:').classes('text-gray-300 w-28')
                                        temp_anomaly_upper_input = ui.number(value=config.measurement.temp_anomaly_upper, format='%.2f', step=0.01).props('outlined dense suffix=°C').classes('w-24') \
                                            .tooltip('溫度異常檢查的上限值。量測值高於此即觸發警報（預設 42°C，正常人體溫不會超過此值）')
                                    with ui.row().classes('items-center'):
                                        ui.label('溫度下限:').classes('text-gray-300 w-28')
                                        temp_anomaly_lower_input = ui.number(value=config.measurement.temp_anomaly_lower, format='%.2f', step=0.01).props('outlined dense suffix=°C').classes('w-24') \
                                            .tooltip('溫度異常檢查的下限值。量測值低於此即觸發警報（預設 30°C，低於此可能是耳溫槍故障或沒對到耳朵）')
                                # --- 連續無套異常設定 ---
                                ui.separator().classes('my-1')
                                with ui.row().classes('items-center'):
                                    ui.label('連續無套:').classes('text-gray-300 w-28')
                                    no_cover_anomaly_switch = ui.switch(value=config.measurement.no_cover_anomaly_enabled, on_change=lambda e: _toggle_no_cover_anomaly_fields(e.value)).props('dense') \
                                        .tooltip('啟用後追蹤同一通道連續量測到「無耳套」的次數，達到設定次數會跳警報。用於提醒操作員耳套可能漏裝或裝歪')
                                with ui.column().classes('w-full gap-2') as nc_fields:
                                    no_cover_anomaly_fields = nc_fields
                                    nc_fields.set_visibility(config.measurement.no_cover_anomaly_enabled)
                                    with ui.row().classes('items-center'):
                                        ui.label('連續次數:').classes('text-gray-300 w-28')
                                        no_cover_anomaly_count_input = ui.number(value=config.measurement.no_cover_anomaly_count, format='%d', step=1, min=1).props('outlined dense suffix=次').classes('w-24') \
                                            .tooltip('連續無套達到此次數時觸發警報（預設 3 次）。任何一次量到有套會把該通道計數歸 0')
                            ui.label('運行模式:').classes('text-gray-300 w-28')
                            mode_select = ui.select(options=['master', 'slave'], value=config.network.mode).props('outlined dense').classes('w-32') \
                                .tooltip('Master: 主機，連 PLC + 6 支耳溫槍；Slave: 副機，連 6 支並透過 TCP 回報 Master。修改後需重啟程式')
                    # --- 網路設定 ---
                    with ui.expansion('網路設定', icon='lan').classes('w-full bg-slate-800'):
                        with ui.column().classes('w-full gap-2 p-2'):
                            with ui.row().classes('items-center'):
                                ui.label('Master IP:').classes('text-gray-300 w-28')
                                net_inputs['master_ip'] = ui.input(value=config.network.master_ip).props('outlined dense').classes('w-36') \
                                    .tooltip('Master 主機的 IP 位址。Slave 模式下用此 IP 連線 Master；Master 模式下此值僅供記錄')
                            with ui.row().classes('items-center'):
                                ui.label('Port:').classes('text-gray-300 w-28')
                                net_inputs['port'] = ui.number(value=config.network.port).props('outlined dense').classes('w-24') \
                                    .tooltip('Master/Slave 通訊埠（雙方須一致），預設 5001')
                    if is_master:
                        # --- 時序設定 ---
                        with ui.expansion('時序設定', icon='timer').classes('w-full bg-slate-800'):
                            with ui.column().classes('w-full gap-2 p-2'):
                                _timing_tips = {
                                    'empty_collect_delay': 'PLC D515 觸發後，等待此時間再從藍芽抓空槍值。太短可能取到舊值；太長會讓流程變慢',
                                    'measure_collect_delay': 'PLC D500 觸發後，等待此時間再從藍芽抓量測值。太短可能取到舊值；太長會讓流程變慢',
                                }
                                for k, v in [('empty_collect_delay', '空槍收集延遲'), ('measure_collect_delay', '量測收集延遲')]:
                                    with ui.row().classes('items-center'):
                                        ui.label(v + ':').classes('text-gray-300 w-28')
                                        timing_inputs[k] = ui.number(value=getattr(config.timing, k), format='%.2f', min=0, max=10, step=0.1).props('outlined dense suffix=秒').classes('w-24') \
                                            .tooltip(_timing_tips[k])
                        # --- PLC 設定 ---
                        with ui.expansion('PLC 設定', icon='memory').classes('w-full bg-slate-800'):
                            with ui.column().classes('w-full gap-2 p-2'):
                                with ui.row().classes('items-center'):
                                    ui.label('IP 位址:').classes('text-gray-300 w-28')
                                    plc_inputs['ip_address'] = ui.input(value=config.plc.ip_address).props('outlined dense').classes('w-36') \
                                        .tooltip('PLC FX5U 的 IP 位址')
                                with ui.row().classes('items-center'):
                                    ui.label('Port:').classes('text-gray-300 w-28')
                                    plc_inputs['port'] = ui.number(value=config.plc.port).props('outlined dense').classes('w-24') \
                                        .tooltip('PLC SLMP 通訊埠（FX5U 預設 5000）')
                    # --- 模擬模式 (PLC / 藍芽各自獨立) ---
                    with ui.expansion('模擬模式', icon='science').classes('w-full bg-slate-800'):
                        with ui.column().classes('w-full gap-2 p-2'):
                            ui.label('開啟後不需實體裝置即可測試流程。修改後需按下方「更新資料 (即時套用)」才會重建管理器。').classes('text-gray-400 text-xs')
                            if is_master:
                                with ui.row().classes('items-center'):
                                    ui.label('PLC 模擬:').classes('text-gray-300 w-28')
                                    plc_sim_switch = ui.switch(value=config.plc_simulation_mode).props('dense') \
                                        .tooltip('開啟後不需實體 PLC 即可測試流程；PLC 寫入/讀取會走內建模擬資料')
                            with ui.row().classes('items-center'):
                                ui.label('藍芽模擬:').classes('text-gray-300 w-28')
                                bt_sim_switch = ui.switch(value=config.bt_simulation_mode).props('dense') \
                                    .tooltip('開啟後不需實體耳溫槍即可測試流程；藍芽會回傳模擬溫度值')
                    # --- 遠端記錄 (log / alarm 路徑各自獨立) ---
                    with ui.expansion('遠端記錄', icon='cloud_upload').classes('w-full bg-slate-800'):
                        with ui.column().classes('w-full gap-2 p-2'):
                            ui.label('指定遠端資料夾 (網路磁碟或 UNC 路徑)；留空表示不寫遠端。').classes('text-gray-400 text-xs')
                            with ui.row().classes('items-center w-full gap-2'):
                                ui.label('Log 路徑:').classes('text-gray-300 w-28')
                                remote_log_dir_input = ui.input(value=config.remote_log_dir, placeholder=r'例如 \\NAS\hmi\log') \
                                    .props('outlined dense').classes('flex-grow') \
                                    .tooltip('遠端 cycle log 寫入路徑（簡化版：批號/時間/TOTAL OK/TOTAL NG）。寫入失敗會背景重試，不影響本機 log')
                                ui.button(icon='folder_open',
                                          on_click=lambda: _pick_directory(remote_log_dir_input, '選擇遠端 Log 資料夾')) \
                                    .props('flat color=blue dense').tooltip('用系統對話框瀏覽選擇資料夾')
                            with ui.row().classes('items-center w-full gap-2'):
                                ui.label('Alarm 路徑:').classes('text-gray-300 w-28')
                                remote_alarm_dir_input = ui.input(value=config.remote_alarm_dir, placeholder=r'例如 \\NAS\hmi\alarm') \
                                    .props('outlined dense').classes('flex-grow') \
                                    .tooltip('遠端 alarm 寫入路徑（異常完整紀錄）。寫入失敗會背景重試，不影響本機 alarm')
                                ui.button(icon='folder_open',
                                          on_click=lambda: _pick_directory(remote_alarm_dir_input, '選擇遠端 Alarm 資料夾')) \
                                    .props('flat color=blue dense').tooltip('用系統對話框瀏覽選擇資料夾')

                # === 不需要密碼的區塊 ===
                with ui.expansion('藍芽設定', icon='bluetooth').classes('w-full bg-slate-800'):
                    with ui.column().classes('w-full gap-2 p-2'):
                        mac_channels = range(1, 7) if config.network.mode == "master" else range(7, 13)
                        for ch in mac_channels:
                            idx = ch - 1 if ch <= 6 else ch - 7
                            addr = config.bluetooth.device_addresses[idx] if idx < len(config.bluetooth.device_addresses) else ""
                            with ui.row().classes('items-center'):
                                ui.label(f'{get_channel_display_name(ch)}:').classes('text-gray-300 w-14')
                                bt_mac_inputs[ch] = ui.input(value=addr, placeholder='XX:XX:XX:XX:XX:XX').props('outlined dense').classes('flex-grow') \
                                    .tooltip(f'{get_channel_display_name(ch)} 對應的耳溫槍藍芽 MAC 位址。留空代表停用此通道的藍芽連線')
                        with ui.row().classes('items-center'):
                            ui.label('重連間隔:').classes('text-gray-300 w-28')
                            bt_inputs['reconnect_interval'] = ui.number(value=config.bluetooth.reconnect_interval).props('outlined dense suffix=秒').classes('w-24') \
                                .tooltip('藍芽斷線後等待多久再嘗試重連，秒')
                        with ui.row().classes('items-center'):
                            ui.label('超時:').classes('text-gray-300 w-28')
                            bt_inputs['timeout'] = ui.number(value=config.bluetooth.timeout).props('outlined dense suffix=秒').classes('w-24') \
                                .tooltip('藍芽連線等待逾時時間，秒')
                        with ui.row().classes('items-center'):
                            ui.label('批次連線數:').classes('text-gray-300 w-28')
                            bt_inputs['max_parallel_connects'] = ui.number(value=config.bluetooth.max_parallel_connects, min=1, max=6, step=1).props('outlined dense').classes('w-24') \
                                .tooltip('啟動或重連時每批最多同時連線幾支耳溫槍；建議 3，若藍芽不穩可降為 2')
                        with ui.row().classes('items-center'):
                            ui.label('漏壓逾時:').classes('text-gray-300 w-28')
                            bt_inputs['miss_timeout'] = ui.number(value=config.bluetooth.miss_timeout, min=0.5, step=0.5).props('outlined dense suffix=秒').classes('w-24') \
                                .tooltip('生產量測觸發後等待主動推送的最長秒數；逾時仍未收到的啟用通道判定為漏壓 (預設 3 秒)。設長一點較能確認真漏壓')
                with ui.expansion('通道啟用', icon='toggle_on').classes('w-full bg-slate-800'):
                    ch_range = range(1, 13) if is_master else range(7, 13)
                    with ui.grid(columns=6).classes('w-full gap-1'):
                        for i in ch_range:
                            with ui.column().classes('items-center'):
                                ui.label(get_channel_display_name(i)).classes('text-gray-300 text-[10px]')
                                channel_switches[i] = ui.switch(value=config.measurement.channel_enabled[i-1],
                                    on_change=lambda e, ch=i: _on_channel_toggle(ch, e.value)).props('dense') \
                                    .tooltip(f'啟用 {get_channel_display_name(i)}。切換即時生效：停用後立即停止連線、清除該通道 D513 異常，不需按更新')
                ui.button('儲存進階設定', on_click=on_save_advanced_settings).props('color=blue icon=save').classes('w-full mt-4') \
                    .tooltip('儲存目前所有設定到 config.json；模擬模式或 IP 變更等需重啟才會完全生效')
                ui.button('更新資料 (即時套用)', on_click=on_apply_settings).props('color=green icon=sync').classes('w-full mt-2') \
                    .tooltip('儲存設定並即時套用到執行中的管理器（不需重啟程式）')

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
    global meters_ui, log_console, plc_status_icon, network_status_icon, alert_container, alert_message_label, system_status_label, measure_status_label, plc_monitor_ui, batch_no_input, total_ok_label, total_ng_label, temp_anomaly_status_label, no_cover_anomaly_status_label
    is_master = config.network.mode == "master"
    ui.colors(primary='#5898d4', secondary='#26a69a', accent='#9c27b0', dark='#1d1d1d')
    ui.add_head_html('<style>'
                     'body { user-select: text !important; -webkit-user-select: text !important; }'
                     # 系統 Log 反向顯示：最新訊息在最上方
                     '.flip-log { display: flex !important; flex-direction: column-reverse !important; }'
                     '.flip-log .q-scrollarea__content { display: flex !important; flex-direction: column-reverse !important; }'
                     '</style>')
    ui.keyboard(on_key=lambda e: ui.run_javascript('window.location.reload()') if e.key.f5 and e.action.keydown else None)
    build_settings_drawer()
    with ui.column().classes('w-full p-2 gap-2'):
        with ui.card().classes('w-full bg-slate-900 border-b-2 border-blue-500 p-3'):
            with ui.column().classes('w-full gap-2'):
                # === 第一列: 識別資訊 (左) + 連線狀態與設定 (右) ===
                with ui.row().classes('w-full justify-between items-center'):
                    with ui.row().classes('items-center gap-3'):
                        ui.icon('medical_services', size='lg', color='blue')
                        ui.label(config.title).classes('text-3xl text-white font-bold')
                        ui.label(f'v{config.version}').classes('text-base text-gray-400')
                        ui.badge('MASTER' if is_master else 'SLAVE',
                                 color='blue' if is_master else 'orange').classes('text-lg px-3 py-1')
                        # 機台名稱 (3 台機共用程式，靠 config 區分)
                        ui.badge(config.machine_name, color='teal').classes('text-lg px-3 py-1')
                        # TOTAL OK / TOTAL NG 大字 (僅 Master 顯示，由 PLC OK/NG 計數加總)
                        if is_master:
                            with ui.row().classes('items-center gap-2 bg-slate-800 px-4 py-1 rounded-full border border-green-700 ml-2'):
                                ui.label('TOTAL OK:').classes('text-gray-300 text-lg')
                                total_ok_label = ui.label('0').classes('text-green-400 text-2xl font-bold font-mono')
                            with ui.row().classes('items-center gap-2 bg-slate-800 px-4 py-1 rounded-full border border-red-700'):
                                ui.label('TOTAL NG:').classes('text-gray-300 text-lg')
                                total_ng_label = ui.label('0').classes('text-red-400 text-2xl font-bold font-mono')
                        # 量測流程狀態 (master / slave 都顯示)
                        with ui.row().classes('items-center gap-2 bg-slate-800 px-4 py-1 rounded-full border border-gray-700 ml-2'):
                            ui.label('量測流程:').classes('text-gray-300 text-lg')
                            _initial_state_text = "待機中"
                            if measure_manager:
                                _state_map = {
                                    MeasurementState.IDLE: "待機中",
                                    MeasurementState.WAITING_EMPTY: "等待空槍",
                                    MeasurementState.EMPTY_DONE: "空槍完成",
                                    MeasurementState.WAITING_MEASURE: "等待量測",
                                    MeasurementState.MEASURING: "計算中",
                                    MeasurementState.COMPLETE: "量測完成",
                                }
                                _initial_state_text = _state_map.get(measure_manager.state, "待機中")
                            measure_status_label = ui.label(_initial_state_text).classes('text-gray-400 text-2xl font-bold')
                    with ui.row().classes('items-center gap-4'):
                        if is_master:
                            with ui.row().classes('items-center gap-2'):
                                ui.label('PLC:').classes('text-gray-300 text-xl')
                                plc_status_icon = ui.icon('circle', color='gray').classes('text-2xl')
                        with ui.row().classes('items-center gap-2'):
                            ui.label('網路:').classes('text-gray-300 text-xl')
                            network_status_icon = ui.icon('circle', color='gray').classes('text-2xl')
                        ui.button(icon='settings', on_click=toggle_settings) \
                            .props('flat round color=white size=lg') \
                            .tooltip('開啟 / 關閉進階設定面板（部分項目需密碼登入）')

                ui.separator().classes('bg-slate-700')

                # === 第二列: 批號 + 即時監控 (左) + 操作按鈕 (右) ===
                with ui.row().classes('w-full justify-between items-center'):
                    with ui.row().classes('items-center gap-3'):
                        if is_master:
                            with ui.row().classes('items-center gap-1 bg-slate-800 px-3 py-1 rounded-full border border-gray-700'):
                                ui.label('批號:').classes('text-gray-400 text-lg')
                                batch_no_input = ui.input(value=config.batch_no, placeholder='英數字') \
                                    .props('outlined dense dark maxlength=20') \
                                    .props('input-class="text-blue-300 font-mono text-lg"') \
                                    .classes('w-56') \
                                    .tooltip('輸入當前批號（限英數字、底線、連字號），改完務必按右側 SAVE 才會套用；未按 SAVE 滑鼠移開會自動還原原值')
                                # 失焦時若未按 SAVE 則還原；延遲 150ms 等 SAVE 的 click 先觸發
                                batch_no_input.on('blur', lambda e: _schedule_batch_revert())
                                ui.button(icon='save', on_click=on_batch_no_commit) \
                                    .props('color=blue dense flat round size=sm') \
                                    .tooltip('套用批號 — 之後每筆量測 log 列首會帶此批號，並寫入 config.json')
                            with ui.row().classes('items-center gap-2 bg-slate-800 px-4 py-1 rounded-full border border-gray-700'):
                                ui.label('週期:').classes('text-gray-400 text-lg')
                                plc_monitor_ui['cycle_val_top'] = ui.label('0').classes('text-yellow-400 text-2xl font-bold font-mono')
                        with ui.row().classes('items-center gap-2 bg-slate-800 px-4 py-1 rounded-full border border-gray-700'):
                            ui.label('狀態:').classes('text-gray-400 text-lg')
                            text = '運行中' if system_running else '已停止'
                            color = 'text-green-400' if system_running else 'text-red-400'
                            system_status_label = ui.label(text).classes(f'{color} text-2xl font-bold')
                        if is_master:
                            with ui.row().classes('items-center gap-2 bg-slate-800 px-4 py-1 rounded-full border border-gray-700'):
                                ui.label('判定:').classes('text-gray-400 text-lg')
                                judge_mode_label = ui.label('正常').classes('text-green-400 text-2xl font-bold')
                                plc_monitor_ui['judge_mode_label'] = judge_mode_label
                            with ui.row().classes('items-center gap-2 bg-slate-800 px-4 py-1 rounded-full border border-gray-700'):
                                ui.label('暖槍:').classes('text-gray-400 text-lg')
                                plc_monitor_ui['warmup_label'] = ui.label('OFF').classes('text-gray-400 text-2xl font-bold')
                            # 溫度異常使用狀態 (master only)
                            with ui.row().classes('items-center gap-2 bg-slate-800 px-4 py-1 rounded-full border border-gray-700') as _ta_pill:
                                ui.label('溫度異常:').classes('text-gray-400 text-lg')
                                _ta_on = config.measurement.temp_anomaly_enabled
                                temp_anomaly_status_label = ui.label('ON' if _ta_on else 'OFF') \
                                    .classes(('text-green-400' if _ta_on else 'text-gray-500') + ' text-2xl font-bold')
                            _ta_pill.tooltip('防呆檢查：量測到的絕對溫度若不在「溫度上下限」範圍內 (預設 30~42°C) 會觸發警報並寫 PLC D513 異常旗標。在進階設定中啟用/停用')
                            # 連續無套使用狀態 (master only)
                            with ui.row().classes('items-center gap-2 bg-slate-800 px-4 py-1 rounded-full border border-gray-700') as _nc_pill:
                                ui.label('連續無套:').classes('text-gray-400 text-lg')
                                _nc_on = config.measurement.no_cover_anomaly_enabled
                                no_cover_anomaly_status_label = ui.label('ON' if _nc_on else 'OFF') \
                                    .classes(('text-green-400' if _nc_on else 'text-gray-500') + ' text-2xl font-bold')
                            _nc_pill.tooltip('追蹤同一通道連續量測到「無套」的次數，達到設定次數時觸發警報。在進階設定中啟用/停用')
                    with ui.row().classes('items-center gap-2'):
                        ui.button('清空系統Log', icon='delete', on_click=lambda: log_console.clear()) \
                            .props('color=grey dense size=md').classes('px-3') \
                            .tooltip('清除畫面上的訊息歷史顯示（不影響本機 CSV log 檔案內容）')
                        if is_master:
                            ui.button('計數歸零', icon='exposure_zero', on_click=on_reset_count_click) \
                                .props('color=orange dense size=md').classes('px-3') \
                                .tooltip('把 PLC OK/NG 計數 (D517~D540) 全部清為 0，並更新 last_reset_date；按下會先彈出確認視窗')
                            ui.button('異常復歸', icon='restart_alt', on_click=on_reset_button_click) \
                                .props('color=amber text-color=black dense size=md').classes('px-3') \
                                .tooltip('清除 PLC D513 異常旗標、各通道 highlight 與警報橫幅；不影響 OK/NG 計數')
                            ui.button('流程解卡', icon='build_circle', on_click=on_force_clear_triggers) \
                                .props('color=red dense size=md outline').classes('px-3') \
                                .tooltip('強制將 D500/D515 寫 0 並重置量測狀態機，僅在流程卡住時使用')
        with ui.card().classes('w-full bg-red-600 p-3 border-2 border-red-400') as container:
            alert_container = container; alert_container.set_visibility(False)
            with ui.row().classes('w-full items-center justify-between'):
                with ui.row().classes('items-center gap-3'):
                    ui.icon('warning', size='lg', color='white')
                    alert_message_label = ui.label('').classes('text-2xl text-white font-bold')
                ui.button('確認', on_click=stop_alert_flash).props('color=white text-color=red dense size=lg').classes('px-6') \
                    .tooltip('停止警報閃爍並隱藏紅色橫幅；不會解除實際異常（請按「異常復歸」）')
        with ui.row().classes('w-full items-start gap-3'):
            if is_master: build_meter_block('Slave 通道 (CH12, 10, 8, 6, 4, 2)', 7, 12, 'orange')
            if is_master: build_meter_block('本機通道 (CH11, 9, 7, 5, 3, 1)', 1, 6, 'blue')
            if not is_master: build_meter_block('本機通道 (CH12, 10, 8, 6, 4, 2)', 7, 12, 'orange')
            # 右側：目前設定在上、手動觸發在下 (整欄只在 Master 顯示)；不撐開，維持內容寬度避免擠掉版面
            if is_master:
                with ui.column().classes('items-start gap-3'):
                    # 「目前設定」單欄、一項一列 (最窄)
                    with ui.card().classes('bg-slate-800 p-3'):
                        ui.label('目前設定').classes('text-lg text-white font-bold mb-2')
                        with ui.column().classes('gap-1'):
                            with ui.row().classes('items-center gap-2'):
                                ui.label('上限:').classes('text-gray-400 text-base w-24')
                                current_settings_labels['tol_upper'] = ui.label(f'+{abs(config.measurement.tolerance_upper):.2f}°C').classes('text-green-400 text-xl font-bold')
                            with ui.row().classes('items-center gap-2'):
                                ui.label('下限:').classes('text-gray-400 text-base w-24')
                                current_settings_labels['tol_lower'] = ui.label(f'-{abs(config.measurement.tolerance_lower):.2f}°C').classes('text-red-400 text-xl font-bold')
                            with ui.row().classes('items-center gap-2'):
                                ui.label('空槍上限:').classes('text-gray-400 text-base w-24')
                                current_settings_labels['empty_upper'] = ui.label(f'{config.measurement.empty_upper:.2f}°C').classes('text-orange-400 text-xl font-bold')
                            with ui.row().classes('items-center gap-2'):
                                ui.label('空槍下限:').classes('text-gray-400 text-base w-24')
                                current_settings_labels['empty_lower'] = ui.label(f'{config.measurement.empty_lower:.2f}°C').classes('text-cyan-400 text-xl font-bold')
                            with ui.row().classes('items-center gap-2'):
                                ui.label('溫度上限:').classes('text-gray-400 text-base w-24')
                                current_settings_labels['temp_upper'] = ui.label(f'{config.measurement.temp_anomaly_upper:.2f}°C').classes('text-amber-400 text-xl font-bold')
                            with ui.row().classes('items-center gap-2'):
                                ui.label('溫度下限:').classes('text-gray-400 text-base w-24')
                                current_settings_labels['temp_lower'] = ui.label(f'{config.measurement.temp_anomaly_lower:.2f}°C').classes('text-sky-400 text-xl font-bold')
                    # 手動觸發 (移到目前設定下方)
                    with ui.card().classes('bg-slate-700 p-3'):
                        ui.label('手動觸發').classes('text-lg text-yellow-400 font-bold mb-2')
                        with ui.column().classes('gap-2'):
                            ui.button('空槍量測', on_click=on_simulate_empty).props('color=cyan icon=science size=lg').classes('w-full') \
                                .tooltip('模擬 PLC 寫入 D515=1，啟動空槍量測流程；用於模擬模式或 PLC 未接時測試')
                            ui.button('溫度量測', on_click=on_simulate_measure).props('color=orange icon=thermostat size=lg').classes('w-full') \
                                .tooltip('模擬 PLC 寫入 D500=1，啟動溫度量測流程；用於模擬模式或 PLC 未接時測試')
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
                log_console = ui.log(max_lines=100).classes('w-full text-base text-gray-300 font-mono flip-log').style('height: 100%; min-height: 300px')

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

def _enforce_single_instance():
    """單一實例：啟動時若偵測到前一個同模式實例仍在跑，先 kill 掉 (含其子行程)，只保留本次。
    避免使用者不知已開、重複執行造成兩個 UI/CMD 與藍芽/PLC 通訊衝突。
    用 lock 檔記錄 PID；以 mode 區分 (master/slave 各自一個 lock，同台跑兩模式不互殺)。"""
    import tempfile, subprocess
    mode = config.network.mode
    lock_path = os.path.join(tempfile.gettempdir(), f'chingtech_meter_hmi_{mode}.lock')
    my_pid = os.getpid()
    old_pid = None
    try:
        if os.path.exists(lock_path):
            old_pid = int((open(lock_path, encoding='utf-8').read().strip() or '0'))
    except Exception:
        old_pid = None
    if old_pid and old_pid != my_pid:
        try:
            # 確認該 PID 仍是 python 行程 (避免 PID 被回收後誤殺其他程式)
            out = subprocess.run(['tasklist', '/FI', f'PID eq {old_pid}', '/FO', 'CSV', '/NH'],
                                 capture_output=True, text=True, timeout=5).stdout.lower()
            if 'python' in out:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(old_pid)],
                               capture_output=True, timeout=5)
                print(f'[single-instance] 已關閉前一個實例 (PID {old_pid})')
                time.sleep(1.0)   # 等舊實例釋放 port
        except Exception as e:
            print(f'[single-instance] 關閉前一實例失敗: {e}')
    try:
        with open(lock_path, 'w', encoding='utf-8') as f:
            f.write(str(my_pid))
    except Exception:
        pass


if __name__ in {"__main__", "__mp_main__"}:
    if multiprocessing.current_process().name == 'MainProcess':
        _enforce_single_instance()   # 先關掉前一個實例，再啟動本次
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
