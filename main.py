# -*- coding: utf-8 -*-
"""
三橋耳溫槍探頭套檢測系統 - 主程式
"""
import sys
import os
import socket
import ctypes
import multiprocessing
import datetime
import threading
import asyncio
import time

from nicegui import ui, app

from config import config, save_config, CHANNEL_DISPLAY_NAMES, get_channel_display_name
from bluetooth_comm import BluetoothManager, ConnectionState, ThermometerData
from plc_comm import PLCManager, PLCConnectionState
from network_comm import NetworkManager, NetworkRole, NetworkState, MeterDataPacket
from measurement import MeasurementManager, MeasurementState, JudgeResult, ChannelData

# --- 全域變數 ---
is_shutting_down = False
prev_bt_states = {}
ear_cover_statuses = {}  # 儲存各通道最新的耳套狀態 (1111/0000)
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

# --- 設定面板元件 ---
settings_drawer = None
timing_inputs = {}
plc_inputs = {}
bt_inputs = {}
bt_mac_inputs = {}              
channel_switches = {}       
mode_select = None          
tolerance_upper_input = None  
tolerance_lower_input = None  

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
    bt_manager = BluetoothManager(simulation_mode=config.simulation_mode)
    bt_manager.set_callbacks(on_data=on_bluetooth_data, on_state=on_bluetooth_state)

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
    net_manager.set_callbacks(on_data=on_network_data, on_state=on_network_state, on_command=on_network_command)

    # 量測管理器
    measure_manager = MeasurementManager(
        channel_count=config.measurement.meter_count,
        tolerance_upper=config.measurement.tolerance_upper,
        tolerance_lower=config.measurement.tolerance_lower,
        log_dir=config.log_dir
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
    
    # 沿用 config 中的批次，或建立新批次
    if measure_manager:
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

    # Slave 模式即時顯示數值 (依量測狀態決定顯示在空槍值或溫度值)
    if config.network.mode == "slave" and channel in meters_ui:
        meter = meters_ui[channel]
        try:
            is_empty = measure_manager and measure_manager.state in (
                MeasurementState.WAITING_EMPTY, MeasurementState.EMPTY_DONE
            )
            with meter['temp_display'].client:
                if is_empty:
                    meter['empty_display'].set_value(data.temperature)
                else:
                    meter['temp_display'].set_value(data.temperature)
        except Exception:
            pass

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
        elif state == ConnectionState.CONNECTED:
            log_message(f"[恢復] {display_name} 藍芽已連線")
            stop_alert_flash()
            if plc_manager: plc_manager.set_bt_error(logical_num, False)

    # Slave 模式：藍芽狀態變更時通知 Master
    if config.network.mode == "slave" and net_manager:
        import time as _time
        packet = MeterDataPacket(
            channel=channel, meter_id="",
            temperature=0.0, timestamp=_time.time(),
            bt_state=state.value
        )
        net_manager.send_data(packet)

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

def show_bt_disconnect_alert(channel: int):
    global alert_flash_timer, is_alert_visible
    if is_shutting_down or not alert_container or not alert_message_label: return
    display_name = get_channel_display_name(channel)
    current_time = datetime.datetime.now().strftime("%H:%M:%S")
    # 使用 client 上下文保護
    with alert_container.client:
        alert_message_label.set_text(f'⚠ [{current_time}] {display_name} 藍芽斷線!')
        alert_container.set_visibility(True)
        start_alert_flash()

def on_plc_state(state: PLCConnectionState):
    if plc_status_icon:
        with plc_status_icon.client:
            if state == PLCConnectionState.CONNECTED: plc_status_icon.props('color=green')
            elif state == PLCConnectionState.CONNECTING: plc_status_icon.props('color=yellow')
            else: plc_status_icon.props('color=red')

def on_plc_reset():
    log_message("[PLC] HMI 異常復歸觸發")
    on_reset_click()

def on_plc_empty_trigger():
    log_message("[PLC] 空槍量測觸發")
    try:
        measure_manager.start_empty_measurement()
        if config.simulation_mode: bt_manager.set_simulation_mode_empty()
        # 改用 threading.Timer 避開 slot 錯誤
        threading.Timer(config.timing.empty_collect_delay, trigger_empty_ack_and_collect).start()
    except Exception as e:
        log_message(f"[錯誤] 無法啟動空槍量測: {e}")
        if plc_manager: plc_manager.clear_empty_trigger()

def trigger_empty_ack_and_collect():
    # 通知 Slave 請求量測
    if config.network.mode == "master" and net_manager:
        net_manager.send_command("request_empty")
    for channel in bt_manager.devices.keys():
        if is_channel_enabled(channel): bt_manager.request_measurement(channel)
    # 背景延遲
    time.sleep(0.2)
    collect_empty_values()

def on_plc_measure_trigger():
    log_message("[PLC] 溫度量測觸發")
    try:
        measure_manager.start_temperature_measurement()
        if config.simulation_mode: bt_manager.set_simulation_mode_measure()
        # 改用 threading.Timer
        threading.Timer(config.timing.measure_collect_delay, trigger_measure_ack_and_collect).start()
    except Exception as e:
        log_message(f"[錯誤] 無法執行溫度量測: {e}")
        if plc_manager: plc_manager.write_complete_signal()

def trigger_measure_ack_and_collect():
    # 通知 Slave 請求量測
    if config.network.mode == "master" and net_manager:
        net_manager.send_command("request_measure")
    for channel in bt_manager.devices.keys():
        if is_channel_enabled(channel): bt_manager.request_measurement(channel)
    # 背景延遲
    time.sleep(0.2)
    collect_measure_values()
def on_network_data(packet: MeterDataPacket):
    display_name = get_channel_display_name(packet.channel)
    ch = packet.channel

    # 更新藍芽連線狀態
    if packet.bt_state:
        try:
            bt_state = ConnectionState(packet.bt_state)
            update_meter_bt_status(ch, bt_state)
            # 同步更新 PLC D513 藍芽錯誤遮罩 (停用通道不寫入)
            if plc_manager and is_channel_enabled(ch):
                try:
                    logical_num = int(get_channel_display_name(ch).replace('CH', ''))
                except:
                    logical_num = ch
                is_error = bt_state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR)
                plc_manager.set_bt_error(logical_num, is_error)
        except ValueError:
            pass

    # 更新耳溫套狀態
    if packet.ear_cover:
        ear_cover_statuses[ch] = packet.ear_cover
        update_meter_ear_cover(ch, packet.ear_cover)

    # 溫度為 0 且無耳套資訊 = 純 BT 狀態封包，不 log 溫度
    if packet.temperature == 0.0 and not packet.ear_cover:
        log_message(f"[NET] {display_name}: BT {packet.bt_state}")
    else:
        ear_txt = "有耳溫套" if packet.ear_cover == "1111" else "無耳溫套" if packet.ear_cover == "0000" else ""
        log_message(f"[NET] {display_name}: {packet.temperature}°C {ear_txt}")

def on_network_state(state: NetworkState):
    if network_status_icon:
        if state == NetworkState.CONNECTED: network_status_icon.props('color=green')
        elif state == NetworkState.LISTENING: network_status_icon.props('color=yellow')
        else: network_status_icon.props('color=red')

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
        log_message("[NET] 已補送所有通道藍芽狀態至 Master")

def on_network_command(command: str):
    """Slave 收到 Master 指令"""
    if command == "request_empty":
        log_message("[NET] 收到 Master 空槍量測請求")
        # 設定量測狀態 (讓 Slave UI 顯示在正確欄位)
        if measure_manager:
            measure_manager.state = MeasurementState.WAITING_EMPTY
        if config.simulation_mode and bt_manager:
            bt_manager.set_simulation_mode_empty()
        if bt_manager:
            for ch in bt_manager.devices.keys():
                if is_channel_enabled(ch):
                    bt_manager.request_measurement(ch)
    elif command == "request_measure":
        log_message("[NET] 收到 Master 溫度量測請求")
        if measure_manager:
            measure_manager.state = MeasurementState.WAITING_MEASURE
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
    
    # 執行 5 橫列批次 Log 紀錄
    if measure_manager:
        measure_manager.save_cycle_log(
            plc_data=plc_manager.plc_data if plc_manager else None,
            ear_covers=ear_cover_statuses,
            enabled_channels=get_enabled_channel_list()
        )

    if plc_manager:
        logical_results = [False]*12
        current_results = measure_manager.get_results()
        for internal_ch in range(1, 13):
            display_name = get_channel_display_name(internal_ch)
            logical_idx = int(display_name.replace('CH', '')) - 1
            logical_results[logical_idx] = current_results[internal_ch - 1]

        # 只寫判定結果 (D501~D512)，OK/NG 計數由 PLC 自行處理
        s1 = plc_manager.write_results(logical_results)
        if s1: plc_manager.write_complete_signal()
        update_plc_display()

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
    values = {}
    for channel in bt_manager.devices.keys():
        if is_channel_enabled(channel):
            data = bt_manager.get_last_data(channel)
            if data: values[channel] = data.temperature
    if config.network.mode == "master" and net_manager:
        for ch, pkt in net_manager.get_all_received_data().items():
            if is_channel_enabled(ch): values[ch] = pkt.temperature
    measure_manager.record_empty_values(values)

    # 空槍觸發寫入一列 Log
    if measure_manager:
        measure_manager.save_cycle_log(
            is_empty=True,
            plc_data=plc_manager.plc_data if plc_manager else None,
            ear_covers=ear_cover_statuses,
            enabled_channels=get_enabled_channel_list()
        )

    update_plc_display()
    if plc_manager:
        plc_manager.clear_empty_trigger()
        log_message("[PLC] 空槍量測完成，清除 D515")

def collect_measure_values():
    values = {}
    for channel in bt_manager.devices.keys():
        if is_channel_enabled(channel):
            data = bt_manager.get_last_data(channel)
            if data: values[channel] = data.temperature
    if config.network.mode == "master" and net_manager:
        for ch, pkt in net_manager.get_all_received_data().items():
            if is_channel_enabled(ch): values[ch] = pkt.temperature
    measure_manager.record_measure_values(values)
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
    config.measurement.tolerance_upper = tolerance_upper_input.value
    config.measurement.tolerance_lower = tolerance_lower_input.value
    config.network.mode = mode_select.value
    config.timing.empty_collect_delay = timing_inputs['empty_collect_delay'].value
    config.timing.measure_collect_delay = timing_inputs['measure_collect_delay'].value
    config.timing.bt_request_interval = timing_inputs['bt_request_interval'].value
    config.timing.plc_poll_interval = timing_inputs['plc_poll_interval'].value
    config.timing.result_hold_time = timing_inputs['result_hold_time'].value
    config.plc.ip_address = plc_inputs['ip_address'].value
    config.plc.port = int(plc_inputs['port'].value)
    config.bluetooth.reconnect_interval = bt_inputs['reconnect_interval'].value
    config.bluetooth.timeout = bt_inputs['timeout'].value
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
                device.mac_address = new_addr
                log_message(f"[設定] {get_channel_display_name(ch)} MAC 已更新: {new_addr}")
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
    global settings_drawer, timing_inputs, plc_inputs, bt_inputs, bt_mac_inputs, mode_select, tolerance_upper_input, tolerance_lower_input
    with ui.right_drawer(value=False, fixed=False).props('width=320 bordered').classes('bg-slate-900') as drawer:
        settings_drawer = drawer
        with ui.row().classes('w-full items-center justify-between p-2 bg-slate-800'):
            ui.label('進階設定').classes('text-lg text-white font-bold')
            ui.button(icon='close', on_click=toggle_settings).props('flat dense round color=white')
        with ui.scroll_area().classes('w-full h-full'):
            with ui.column().classes('w-full p-3 gap-4'):
                with ui.expansion('系統設定', icon='settings').classes('w-full bg-slate-800').props('default-opened'):
                    with ui.column().classes('w-full gap-2 p-2'):
                        with ui.row().classes('items-center'):
                            ui.label('誤差上限:').classes('text-gray-300 w-28')
                            tolerance_upper_input = ui.number(value=config.measurement.tolerance_upper, format='%.2f', step=0.01).props('outlined dense suffix=°C').classes('w-24')
                        with ui.row().classes('items-center'):
                            ui.label('誤差下限:').classes('text-gray-300 w-28')
                            tolerance_lower_input = ui.number(value=config.measurement.tolerance_lower, format='%.2f', step=0.01).props('outlined dense suffix=°C').classes('w-24')
                        ui.label('運行模式:').classes('text-gray-300 w-28')
                        mode_select = ui.select(options=['master', 'slave'], value=config.network.mode).props('outlined dense').classes('w-32')
                with ui.expansion('時序設定', icon='timer').classes('w-full bg-slate-800'):
                    with ui.column().classes('w-full gap-2 p-2'):
                        for k, v in [('empty_collect_delay', '空槍收集延遲'), ('measure_collect_delay', '量測收集延遲'), ('bt_request_interval', '藍芽請求間隔'), ('plc_poll_interval', 'PLC 輪詢間隔'), ('result_hold_time', '結果保持時間')]:
                            with ui.row().classes('items-center'):
                                ui.label(v + ':').classes('text-gray-300 w-28')
                                timing_inputs[k] = ui.number(value=getattr(config.timing, k), format='%.2f').props('outlined dense suffix=秒').classes('w-24')
                with ui.expansion('PLC 設定', icon='memory').classes('w-full bg-slate-800'):
                    with ui.column().classes('w-full gap-2 p-2'):
                        with ui.row().classes('items-center'):
                            ui.label('IP 位址:').classes('text-gray-300 w-28')
                            plc_inputs['ip_address'] = ui.input(value=config.plc.ip_address).props('outlined dense').classes('w-36')
                        with ui.row().classes('items-center'):
                            ui.label('Port:').classes('text-gray-300 w-28')
                            plc_inputs['port'] = ui.number(value=config.plc.port).props('outlined dense').classes('w-24')
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
                            bt_inputs['reconnect_interval'] = ui.number(value=config.bluetooth.reconnect_interval).props('outlined dense').classes('w-24')
                        with ui.row().classes('items-center'):
                            ui.label('超時:').classes('text-gray-300 w-28')
                            bt_inputs['timeout'] = ui.number(value=config.bluetooth.timeout).props('outlined dense').classes('w-24')
                with ui.expansion('通道啟用', icon='toggle_on').classes('w-full bg-slate-800'):
                    with ui.grid(columns=6).classes('w-full gap-1'):
                        for i in range(1, 13):
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
            headers += [('OK', 'w-14'), ('NG', 'w-14')]
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
                else:
                    ok_display = None
                    ng_display = None
            meters_ui[i] = {'row_container': row_container, 'disabled_badge': disabled_badge, 'bt_icon': bt_icon, 'ear_cover': ear_cover_label, 'empty_display': empty_display, 'temp_display': temp_display, 'error_display': error_display, 'light': status_light, 'text': status_text, 'ok_display': ok_display, 'ng_display': ng_display}

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
                    current_batch_label = ui.label('目前批次: --').classes('text-blue-300 text-lg font-mono ml-4')
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
                with ui.row().classes('items-center gap-6'):
                    with ui.row().classes('items-center gap-2'):
                        ui.label('PLC:').classes('text-gray-300 text-xl'); plc_status_icon = ui.icon('circle', color='gray').classes('text-2xl')
                    with ui.row().classes('items-center gap-2'):
                        ui.label('網路:').classes('text-gray-300 text-xl'); network_status_icon = ui.icon('circle', color='gray').classes('text-2xl')
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
                        ui.button('重設資料', on_click=on_reset_click).props('color=grey icon=refresh size=lg').classes('flex-grow text-lg')
                        ui.button('清空Log', on_click=lambda: log_console.clear()).props('color=grey icon=delete size=lg').classes('text-lg')
                with ui.card().classes('w-full bg-slate-700 p-3'):
                    ui.label('手動觸發').classes('text-lg text-yellow-400 font-bold mb-2')
                    with ui.row().classes('gap-2'):
                        ui.button('空槍量測', on_click=on_simulate_empty).props('color=cyan icon=science size=lg')
                        ui.button('溫度量測', on_click=on_simulate_measure).props('color=orange icon=thermostat size=lg')
                if is_master:
                    with ui.card().classes('w-full bg-slate-800 p-3'):
                        ui.label('PLC 監控').classes('text-lg text-purple-300 font-bold mb-2')
                        with ui.column().classes('w-full gap-1'):
                            for k, n, d in [('trigger', '量測觸發 D500', True), ('empty', '空槍觸發 D515', True), ('heartbeat', 'PC 心跳 D514', True), ('cycle', '測試週期 D516', False), ('bt_error', 'BT 錯誤 D513', False), ('reset', '異常復歸 D541', True)]:
                                with ui.row().classes('w-full items-center justify-between'):
                                    ui.label(n).classes('text-gray-400 text-base')
                                    with ui.row().classes('items-center gap-2'):
                                        plc_monitor_ui[k+'_val'] = ui.label('0').classes('text-white text-base font-mono')
                                        if d: plc_monitor_ui[k+'_ind'] = ui.icon('circle', size='xs').classes('text-gray-500')
            with ui.column().classes('flex-grow gap-3').style('max-width: 400px; height: 100%'):
                with ui.card().classes('w-full h-full bg-slate-800 p-3'):
                    ui.label('系統 Log').classes('text-lg text-blue-300 font-bold mb-2')
                    log_console = ui.log(max_lines=100).classes('w-full text-base text-gray-300 font-mono').style('height: 600px')

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

if __name__ in {"__main__", "__mp_main__"}:
    if multiprocessing.current_process().name == 'MainProcess':
        try:
            import ctypes; app_id = '3bridge.meter.hmi.v1'
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except: pass
        init_managers()

    def handle_shutdown():
        global is_shutting_down
        is_shutting_down = True
        print("系統正在關閉，正在清理資源...")
        try:
            objs = globals()
            if objs.get('bt_manager'): bt_manager.stop()
            if objs.get('plc_manager'): plc_manager.stop_monitoring()
            if objs.get('net_manager'): net_manager.stop()
        except: pass
        finally:
            import os; os._exit(0)

    app.on_shutdown(handle_shutdown)
    # Master 用 port 8080, Slave 用 port 8081，同一台電腦可同時跑兩個實例
    ui_port = 8080 if config.network.mode == "master" else 8081
    ui.run(title=config.title, dark=True, native=True, port=ui_port, window_size=(config.window_width, config.window_height), favicon='meter32x32.ico', reload=False, show=False)
