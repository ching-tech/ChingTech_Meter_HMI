# -*- coding: utf-8 -*-
"""
設定管理模組 - 管理系統設定與參數
"""
import json
import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional

# --- 設定檔路徑 ---
# 預設放在程式資料夾外，避免部署時覆蓋客戶設定
# 支援命令列參數: python main.py --config slave_test/config.json
import sys
_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "ChingTech_Meter_HMI_config")
CONFIG_FILE = os.path.join(_CONFIG_DIR, "config.json")
for i, arg in enumerate(sys.argv):
    if arg == "--config" and i + 1 < len(sys.argv):
        CONFIG_FILE = sys.argv[i + 1]
        break

# --- 通道顯示名稱對應表 ---
CHANNEL_DISPLAY_NAMES = {
    1: 'CH11', 2: 'CH9', 3: 'CH7', 4: 'CH5', 5: 'CH3', 6: 'CH1',
    7: 'CH12', 8: 'CH10', 9: 'CH8', 10: 'CH6', 11: 'CH4', 12: 'CH2',
}

def get_channel_display_name(channel: int) -> str:
    """取得通道的顯示名稱"""
    return CHANNEL_DISPLAY_NAMES.get(channel, f'CH{channel}')

@dataclass
class BluetoothConfig:
    """藍芽設定"""
    enabled: bool = True
    device_addresses: List[str] = field(default_factory=lambda: [""] * 6)  # 6 支耳溫槍 MAC
    reconnect_interval: float = 5.0  # 重連間隔 (秒)
    timeout: float = 5.0  # 連線超時 (秒)
    max_parallel_connects: int = 3  # 每批最多同時連線數，降低 Windows 藍牙堆疊卡死風險

@dataclass
class PLCConfig:
    """PLC 設定 (FX5U 3E 協議, 暫存器 D500~D541 固定)"""
    enabled: bool = True
    ip_address: str = "192.168.1.10"
    port: int = 5000

@dataclass
class NetworkConfig:
    """Master-Slave 網路設定"""
    mode: str = "master"  # "master" 或 "slave"
    master_ip: str = "192.168.1.100"
    port: int = 5001
    slave_meter_offset: int = 6  # Slave 的 Meter ID 偏移 (7-12)

@dataclass
class TimingConfig:
    """時序設定"""
    empty_collect_delay: float = 0.5    # 空槍值收集延遲 (秒)
    measure_collect_delay: float = 0.5  # 溫度量測收集延遲 (秒)
    bt_request_interval: float = 0.1    # 藍芽請求間隔 (秒)
    plc_poll_interval: float = 0.1      # PLC 輪詢間隔 (秒)
    result_hold_time: float = 1.0       # 結果保持時間 (秒)

@dataclass
class MeasurementConfig:
    """量測設定"""
    tolerance_upper: float = 0.5   # 誤差上限 (°C)，正值 magnitude
    tolerance_lower: float = 0.5   # 誤差下限 (°C)，正值 magnitude；UI 顯示時自動 prefix "-"
    empty_upper: float = 40.0      # 空槍值上限 (°C)
    empty_lower: float = 20.0      # 空槍值下限 (°C)
    meter_count: int = 12          # 總通道數
    # 通道啟用狀態 (True=啟用, False=停用)
    channel_enabled: List[bool] = field(default_factory=lambda: [True] * 12)
    # 溫度異常檢測
    temp_anomaly_enabled: bool = False   # 溫度異常使用開關
    temp_anomaly_upper: float = 42.0     # 溫度異常上限 (°C)
    temp_anomaly_lower: float = 30.0     # 溫度異常下限 (°C)
    # 連續無套異常檢測
    no_cover_anomaly_enabled: bool = False  # 連續無套異常使用開關
    no_cover_anomaly_count: int = 3         # 連續無套觸發次數

@dataclass
class AppConfig:
    """應用程式設定"""
    version: str = "2.0.0"
    title: str = "擎添耳溫槍探頭套檢測系統"
    window_width: int = 1920
    window_height: int = 900
    simulation_mode: bool = True  # [舊欄位] 模擬模式總開關；保留作為向下相容，新欄位優先
    plc_simulation_mode: bool = True  # PLC 模擬 (獨立於藍芽)
    bt_simulation_mode: bool = True   # 藍芽槍模擬 (獨立於 PLC)
    log_dir: str = "logs"  # 量測記錄目錄
    remote_log_dir: str = ""    # 遠端 log 目錄 (空=不寫遠端)；寫入簡化版 cycle log
    remote_alarm_dir: str = ""  # 遠端 alarm 目錄 (空=不寫遠端)；寫入完整 alarm
    machine_name: str = "Machine1"  # 機台名稱 (用於 log 檔名與 UI 顯示)
    batch_no: str = ""  # 目前批號 (人員輸入，限定英數字，每筆 log 列首寫入此值)
    extra_password: str = "1234"  # 進階設定額外密碼 (與內建密碼並行)
    last_reset_date: str = ""  # 上次計數歸零的日期 (YYYY-MM-DD)；啟動時跨日才自動歸零

    bluetooth: BluetoothConfig = field(default_factory=BluetoothConfig)
    plc: PLCConfig = field(default_factory=PLCConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    measurement: MeasurementConfig = field(default_factory=MeasurementConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)


def _ensure_config_exists():
    """若外部設定檔不存在，從程式內附的 config.default.json 複製一份"""
    if os.path.exists(CONFIG_FILE):
        return
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    default_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.default.json")
    if os.path.exists(default_file):
        import shutil
        shutil.copy2(default_file, CONFIG_FILE)
        print(f"已從 {default_file} 建立初始設定檔: {CONFIG_FILE}")


# 模組級旗標：紀錄上一次 load_config 是否成功讀取既有檔案
# False 時 save_config 會拒絕寫入，避免把預設值覆蓋掉壞掉但有真實設定的 config.json
_load_was_successful = False


def load_config() -> AppConfig:
    """載入設定檔"""
    global _load_was_successful
    _ensure_config_exists()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            cfg = _dict_to_config(data)
            _load_was_successful = True
            return cfg
        except Exception as e:
            print(f"[!!] 載入設定檔失敗: {e}")
            print(f"[!!] 為避免覆蓋原始 config.json，本次將使用預設值執行，但不允許自動回寫")
            print(f"[!!] 請手動修復或還原: {CONFIG_FILE}")
    _load_was_successful = False
    return AppConfig()


def save_config(config: AppConfig) -> bool:
    """儲存設定檔；若先前 load 失敗則拒絕寫入，避免預設值覆蓋掉原始檔案"""
    if not _load_was_successful:
        print(f"[!!] 因先前載入 config.json 失敗，已拒絕本次儲存以避免覆蓋原始設定")
        return False
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(asdict(config), f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"儲存設定檔失敗: {e}")
        return False


def _dict_to_config(data: dict) -> AppConfig:
    """將字典轉換為設定物件"""
    config = AppConfig()

    # 更新基本欄位
    for key in ['version', 'title', 'window_width', 'window_height', 'simulation_mode',
                'plc_simulation_mode', 'bt_simulation_mode',
                'log_dir', 'remote_log_dir', 'remote_alarm_dir',
                'machine_name', 'batch_no', 'extra_password', 'last_reset_date']:
        if key in data:
            setattr(config, key, data[key])

    # 向下相容：舊版 config.json 只有 simulation_mode 時，自動同步到兩個新欄位
    if 'simulation_mode' in data:
        if 'plc_simulation_mode' not in data:
            config.plc_simulation_mode = data['simulation_mode']
        if 'bt_simulation_mode' not in data:
            config.bt_simulation_mode = data['simulation_mode']

    # 更新子設定
    if 'bluetooth' in data:
        config.bluetooth = BluetoothConfig(**data['bluetooth'])
    if 'plc' in data:
        # 過濾未知 key，相容舊 config.json 中的 M 暫存器欄位
        plc_fields = {f.name for f in PLCConfig.__dataclass_fields__.values()}
        plc_data = {k: v for k, v in data['plc'].items() if k in plc_fields}
        config.plc = PLCConfig(**plc_data)
    if 'network' in data:
        config.network = NetworkConfig(**data['network'])
    if 'measurement' in data:
        meas_data = data['measurement'].copy()
        # 過濾未知 key，相容舊版 config.json
        meas_fields = {f.name for f in MeasurementConfig.__dataclass_fields__.values()}
        meas_data = {k: v for k, v in meas_data.items() if k in meas_fields}
        # 誤差上下限：統一儲存為正值 magnitude；舊 config.json 寫成負值會自動轉正
        for k in ('tolerance_upper', 'tolerance_lower'):
            if k in meas_data:
                try:
                    meas_data[k] = abs(float(meas_data[k]))
                except Exception:
                    pass
        # 確保 channel_enabled 有 12 個元素
        if 'channel_enabled' in meas_data:
            enabled = meas_data['channel_enabled']
            # 補足不足的通道為 True
            while len(enabled) < 12:
                enabled.append(True)
            meas_data['channel_enabled'] = enabled[:12]
        config.measurement = MeasurementConfig(**meas_data)
    if 'timing' in data:
        config.timing = TimingConfig(**data['timing'])

    return config


# --- 全域設定實例 ---
config = load_config()
