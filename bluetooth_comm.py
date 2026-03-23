# -*- coding: utf-8 -*-
"""
藍芽 SPP 通訊模組 - 連接耳溫槍設備
"""
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from enum import Enum

# --- 常數定義 ---
STX = 0x02  # Start Code
ETX = 0x03  # End Code 1
EOT = 0x04  # End Code 2
DEVICE_TYPE_THERMOMETER = "REEB0001"

class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"

@dataclass
class ThermometerData:
    """耳溫槍量測資料"""
    meter_id: str           # 機器編號 (10 位)
    temperature: float      # 溫度值
    trans_temperature: float  # 估算溫度
    trans_temp_raw: str     # 原始估算溫度 ("1111"=有耳溫套, "0000"=無耳溫套)
    temp_mode: str          # 估算模式
    timestamp: float        # 時間戳記

@dataclass
class ThermometerDevice:
    """耳溫槍設備"""
    channel: int            # 通道編號 (1-6)
    mac_address: str        # 藍芽 MAC 位址
    device_id: str = ""     # 設備 ID
    state: ConnectionState = ConnectionState.DISCONNECTED
    last_data: Optional[ThermometerData] = None
    socket: Optional[object] = None


class BluetoothProtocol:
    """藍芽協定解析器"""

    @staticmethod
    def calc_checksum(data: bytes) -> int:
        """計算 XOR CheckSum"""
        result = 0
        for b in data:
            result ^= b
        return result

    @staticmethod
    def build_cb_response(success: bool) -> bytes:
        """建立 CB 回應封包 (ACK)"""
        # 0x02 CB Data CheckSum 0x03 0x04
        func_id = b'CB'
        data = b'1' if success else b'2'
        payload = bytes([STX]) + func_id + data
        checksum = BluetoothProtocol.calc_checksum(payload)
        return payload + bytes([checksum, ETX, EOT])

    @staticmethod
    def build_cd_request() -> bytes:
        """建立 CD 請求封包 (主動要求量測)"""
        # 0x02 CD 1 CheckSum 0x03 0x04
        func_id = b'CD'
        data = b'1'
        payload = bytes([STX]) + func_id + data
        checksum = BluetoothProtocol.calc_checksum(payload)
        return payload + bytes([checksum, ETX, EOT])

    @staticmethod
    def parse_db_packet(packet: bytes) -> Optional[ThermometerData]:
        """解析 DB 封包 (量測資料)"""
        try:
            # 驗證封包格式
            if len(packet) < 28 or packet[0] != STX:
                return None
            if packet[-2] != ETX or packet[-1] != EOT:
                return None

            # 解析欄位
            device_type = packet[1:9].decode('ascii')
            if device_type != DEVICE_TYPE_THERMOMETER:
                return None

            device_id = packet[9:19].decode('ascii')
            func_id = packet[19:21].decode('ascii')
            if func_id != 'DB':
                return None

            # 解析資料長度
            data_len = int(packet[21:23].hex(), 16)

            # 解析 Data 欄位
            data_start = 23
            data_end = data_start + data_len
            data = packet[data_start:data_end]

            # 驗證 CheckSum
            checksum_received = packet[data_end]
            checksum_calculated = BluetoothProtocol.calc_checksum(packet[:data_end])
            if checksum_received != checksum_calculated:
                print(f"CheckSum 錯誤: 收到 {checksum_received}, 計算 {checksum_calculated}")
                return None

            # 解析溫度資料
            # Data: MeterID(10) + TEMP(4) + TRAN_TEMP(4) + TEMPMode(1)
            meter_id = data[0:10].decode('ascii').strip()
            temp_raw = data[10:14].decode('ascii')
            trans_temp_raw = data[14:18].decode('ascii')
            temp_mode = data[18:19].decode('ascii')

            # 轉換溫度值 (例: "3650" -> 36.50)
            temperature = int(temp_raw[:2]) + int(temp_raw[2:]) / 100
            trans_temperature = int(trans_temp_raw[:2]) + int(trans_temp_raw[2:]) / 100

            return ThermometerData(
                meter_id=meter_id,
                temperature=temperature,
                trans_temperature=trans_temperature,
                trans_temp_raw=trans_temp_raw,
                temp_mode=temp_mode,
                timestamp=time.time()
            )
        except Exception as e:
            print(f"解析封包錯誤: {e}")
            return None


class BluetoothManager:
    """藍芽連線管理器"""

    def __init__(self, simulation_mode: bool = True,
                 connect_timeout: float = 10.0,
                 reconnect_interval: float = 5.0):
        self.simulation_mode = simulation_mode
        self.connect_timeout = connect_timeout
        self.reconnect_interval = reconnect_interval
        self.devices: Dict[int, ThermometerDevice] = {}  # channel -> device
        self._running = False
        self._threads: List[threading.Thread] = []
        self._on_data_callback: Optional[Callable[[int, ThermometerData], None]] = None
        self._on_state_callback: Optional[Callable[[int, ConnectionState], None]] = None
        self._is_channel_enabled: Optional[Callable[[int], bool]] = None
        self._get_channel_name: Optional[Callable[[int], str]] = None

        # 追蹤正在連線中的 socket（用於 stop 時中斷）
        self._connecting_sockets: Dict[int, socket.socket] = {}

        # 每個設備的接收 buffer
        self._recv_buffers: Dict[int, bytes] = {}

        # 模擬用：記錄空槍基準值
        self._sim_empty_values: Dict[int, float] = {}
        self._sim_is_empty_measure: bool = True  # 標記下次量測是空槍還是溫度量測

    def set_callbacks(self,
                      on_data: Optional[Callable[[int, ThermometerData], None]] = None,
                      on_state: Optional[Callable[[int, ConnectionState], None]] = None,
                      is_channel_enabled: Optional[Callable[[int], bool]] = None,
                      get_channel_name: Optional[Callable[[int], str]] = None):
        """設定回呼函式"""
        self._on_data_callback = on_data
        self._on_state_callback = on_state
        if is_channel_enabled is not None:
            self._is_channel_enabled = is_channel_enabled
        if get_channel_name is not None:
            self._get_channel_name = get_channel_name

    def _ch_name(self, channel: int) -> str:
        """取得通道顯示名稱"""
        if self._get_channel_name:
            return self._get_channel_name(channel)
        return f"通道{channel}"

    def add_device(self, channel: int, mac_address: str):
        """新增設備"""
        self.devices[channel] = ThermometerDevice(
            channel=channel,
            mac_address=mac_address
        )

    def start(self):
        """啟動藍芽管理器"""
        if self._running:
            return

        self._running = True

        # 每個設備啟動獨立的資料接收 thread
        for channel, device in self.devices.items():
            if device.mac_address or self.simulation_mode:
                thread = threading.Thread(
                    target=self._receive_thread,
                    args=(channel,),
                    daemon=True
                )
                thread.start()
                self._threads.append(thread)

        if not self.simulation_mode:
            # 實體模式：啟動單一連線管理 thread（依序連接，不互搶）
            conn_thread = threading.Thread(target=self._connection_manager, daemon=True)
            conn_thread.start()
            self._threads.append(conn_thread)

    def _warmup_bluetooth_stack(self):
        """暖機：用第一台設備的 MAC 觸發 Windows 藍芽堆疊初始化"""
        first_mac = None
        for device in self.devices.values():
            if device.mac_address:
                first_mac = device.mac_address
                break
        if not first_mac:
            return

        print("[*] 藍芽堆疊暖機中...")
        try:
            sock = socket.socket(
                socket.AF_BLUETOOTH,
                socket.SOCK_STREAM,
                socket.BTPROTO_RFCOMM
            )
            sock.settimeout(3.0)  # 短 timeout，只是為了觸發堆疊初始化
            sock.connect((first_mac, 1))
            # 如果意外連上了就關掉，後面正式流程會再連
            sock.close()
            print("[*] 藍芽堆疊暖機完成 (探測連線成功)")
        except Exception:
            print("[*] 藍芽堆疊暖機完成 (探測連線失敗，屬正常)")
        finally:
            try:
                sock.close()
            except:
                pass
        # 等待堆疊消化探測連線
        time.sleep(1.0)

    def _connect_parallel(self, channels: list):
        """同時對多個通道發起連線，等待全部回應，回傳各通道結果"""
        results = {}  # channel -> True/False

        def _try_connect(channel):
            device = self.devices[channel]
            self._connect_device(device)
            results[channel] = (device.state == ConnectionState.CONNECTED)

        # 每個通道一個 thread 同時連線
        threads = []
        for ch in channels:
            t = threading.Thread(target=_try_connect, args=(ch,), daemon=True)
            threads.append(t)
            t.start()

        # 等待所有連線嘗試完成
        for t in threads:
            t.join(timeout=self.connect_timeout + 5)

        return results

    def _connection_manager(self):
        """初始化：所有啟用通道同時連線；之後監控斷線重連"""
        # 首次啟動先暖機
        self._warmup_bluetooth_stack()

        # --- 初始化：所有啟用通道同時連線 ---
        enabled_channels = [
            ch for ch, dev in self.devices.items()
            if dev.mac_address and (not self._is_channel_enabled or self._is_channel_enabled(ch))
        ]

        if enabled_channels:
            names = [self._ch_name(ch) for ch in enabled_channels]
            print(f"[*] 同時連線 {len(enabled_channels)} 個通道: {', '.join(names)}")
            results = self._connect_parallel(enabled_channels)

            ok = [self._ch_name(ch) for ch, success in results.items() if success]
            ng = [self._ch_name(ch) for ch, success in results.items() if not success]
            print(f"[*] 連線結果 — 成功: {', '.join(ok) if ok else '無'} | 失敗: {', '.join(ng) if ng else '無'}")

        # --- 監控模式：偵測斷線重連 & 通道啟用狀態變更 ---
        fail_counts = {ch: 0 for ch in self.devices}
        next_retry_time = {}

        while self._running:
            # 檢查停用通道是否仍在連線中
            has_pending_disconnect = False
            for channel, device in self.devices.items():
                if self._is_channel_enabled and not self._is_channel_enabled(channel):
                    if device.state in (ConnectionState.CONNECTED, ConnectionState.CONNECTING):
                        has_pending_disconnect = True
                    fail_counts[channel] = 0
                    next_retry_time.pop(channel, None)
            if has_pending_disconnect:
                time.sleep(1.0)
                continue

            # 收集需要重連的通道
            channels_to_reconnect = []
            for channel, device in self.devices.items():
                if not self._running:
                    return
                if self._is_channel_enabled and not self._is_channel_enabled(channel):
                    continue
                if device.state == ConnectionState.CONNECTED or not device.mac_address:
                    fail_counts[channel] = 0
                    next_retry_time.pop(channel, None)
                    continue
                if channel in next_retry_time and time.time() < next_retry_time[channel]:
                    continue
                channels_to_reconnect.append(channel)

            if channels_to_reconnect:
                # 斷線重連也用同時連線
                names = [self._ch_name(ch) for ch in channels_to_reconnect]
                print(f"[*] 重連 {len(channels_to_reconnect)} 個通道: {', '.join(names)}")
                results = self._connect_parallel(channels_to_reconnect)

                for ch, success in results.items():
                    if success:
                        fail_counts[ch] = 0
                        next_retry_time.pop(ch, None)
                        print(f"[*] {self._ch_name(ch)} 已重連")
                    else:
                        fail_counts[ch] += 1
                        fc = fail_counts[ch]
                        if fc >= 3:
                            cooldown = min(fc * 5, 30)
                            next_retry_time[ch] = time.time() + cooldown
                            print(f"[!] {self._ch_name(ch)} 連續失敗 {fc} 次，{cooldown}秒後重試")

                time.sleep(self.reconnect_interval)
            else:
                time.sleep(2.0)  # 全部已連線，低頻監控

    def stop(self):
        """停止藍芽管理器"""
        self._running = False
        # 關閉正在等待 connect() 的 socket（中斷 timeout 等待）
        for sock in self._connecting_sockets.values():
            try:
                sock.close()
            except:
                pass
        self._connecting_sockets.clear()
        # 關閉已連線的 socket
        for device in self.devices.values():
            if device.socket:
                try:
                    device.socket.close()
                except:
                    pass
                device.socket = None
            device.state = ConnectionState.DISCONNECTED

    def send_ack(self, channel: int, success: bool = True) -> bool:
        """發送 CB 回應 (ACK)"""
        device = self.devices.get(channel)
        if not device:
            return False

        if self.simulation_mode:
            # 模擬模式：發送 ACK 後自動產生模擬資料
            self._simulate_measurement(channel)
            return True

        if device.state != ConnectionState.CONNECTED:
            return False

        try:
            packet = BluetoothProtocol.build_cb_response(success)
            device.socket.send(packet)
            return True
        except Exception as e:
            print(f"發送 CB 回應失敗 ({self._ch_name(channel)}): {e}")
            return False

    def request_measurement(self, channel: int) -> bool:
        """主動要求量測 (發送 CD 指令)"""
        device = self.devices.get(channel)
        if not device:
            return False

        if self.simulation_mode:
            # 模擬模式：產生模擬資料（不需要檢查連線狀態）
            self._simulate_measurement(channel)
            return True

        if device.state != ConnectionState.CONNECTED:
            return False

        try:
            packet = BluetoothProtocol.build_cd_request()
            device.socket.send(packet)
            return True
        except Exception as e:
            print(f"發送 CD 指令失敗 ({self._ch_name(channel)}): {e}")
            return False

    def request_all_measurements(self) -> int:
        """要求所有設備量測，回傳成功數量"""
        success_count = 0
        for channel in self.devices.keys():
            if self.request_measurement(channel):
                success_count += 1
        return success_count

    def get_device_state(self, channel: int) -> ConnectionState:
        """取得設備連線狀態"""
        device = self.devices.get(channel)
        return device.state if device else ConnectionState.DISCONNECTED

    def get_last_data(self, channel: int) -> Optional[ThermometerData]:
        """取得最後一筆資料"""
        device = self.devices.get(channel)
        return device.last_data if device else None

    def set_simulation_mode_empty(self):
        """設定模擬為空槍量測模式"""
        self._sim_is_empty_measure = True

    def set_simulation_mode_measure(self):
        """設定模擬為溫度量測模式"""
        self._sim_is_empty_measure = False

    def reset_simulation(self):
        """重設模擬狀態"""
        self._sim_empty_values.clear()
        self._sim_is_empty_measure = True

    def _receive_thread(self, channel: int):
        """設備資料接收 thread（只負責收資料，不負責連線）"""
        device = self.devices[channel]

        if self.simulation_mode:
            # 模擬模式：直接設為已連線
            time.sleep(0.5)
            self._update_state(device, ConnectionState.CONNECTED)

        while self._running:
            # 通道停用時：斷開連線
            if self._is_channel_enabled and not self._is_channel_enabled(channel):
                if device.state == ConnectionState.CONNECTED:
                    self._disconnect_device(device)
                time.sleep(2.0)
                continue

            if device.state == ConnectionState.CONNECTED:
                self._receive_data(device)
                time.sleep(0.1)
            else:
                time.sleep(1.0)  # 未連線時低頻等待，連線由 _connection_manager 處理

    def _connect_device(self, device: ThermometerDevice):
        """連接設備 (使用 Windows 原生藍芽 socket)"""
        self._update_state(device, ConnectionState.CONNECTING)

        if self.simulation_mode:
            # 模擬模式：直接設定為已連線
            time.sleep(0.5)
            self._update_state(device, ConnectionState.CONNECTED)
            return

        if not device.mac_address:
            self._update_state(device, ConnectionState.DISCONNECTED)
            return

        # 強制關閉舊 socket，確保 Windows 藍芽堆疊釋放連線
        if device.socket:
            try:
                device.socket.close()
            except:
                pass
            device.socket = None

        try:
            print(f"[*] 嘗試連線 {self._ch_name(device.channel)} -> {device.mac_address}")
            sock = socket.socket(
                socket.AF_BLUETOOTH,
                socket.SOCK_STREAM,
                socket.BTPROTO_RFCOMM
            )
            self._connecting_sockets[device.channel] = sock
            sock.settimeout(self.connect_timeout)
            sock.connect((device.mac_address, 1))
            self._connecting_sockets.pop(device.channel, None)
            sock.settimeout(2)
            device.socket = sock
            print(f"[+] 藍芽連線成功: {self._ch_name(device.channel)} ({device.mac_address})")
            self._update_state(device, ConnectionState.CONNECTED)
        except Exception as e:
            self._connecting_sockets.pop(device.channel, None)
            try:
                sock.close()
            except:
                pass
            print(f"[!] {self._ch_name(device.channel)} 連線失敗: {e}")
            self._update_state(device, ConnectionState.ERROR)


    def _disconnect_device(self, device: ThermometerDevice):
        """斷開設備"""
        if device.socket:
            try:
                device.socket.close()
            except:
                pass
            device.socket = None
        self._update_state(device, ConnectionState.DISCONNECTED)

    def _receive_data(self, device: ThermometerDevice):
        """接收資料 (使用 buffer 封包解析)"""
        if self.simulation_mode:
            return

        try:
            data = device.socket.recv(1024)
            if not data:
                # 對方關閉連線
                print(f"連線中斷 ({self._ch_name(device.channel)}): 對方已關閉")
                self._disconnect_device(device)
                return

            # 累積到該設備的 buffer
            channel = device.channel
            self._recv_buffers.setdefault(channel, b'')
            self._recv_buffers[channel] += data

            # 嘗試從 buffer 中切割完整封包 (以 ETX+EOT 結尾)
            while len(self._recv_buffers[channel]) >= 2:
                buf = self._recv_buffers[channel]
                end_pos = -1
                for i in range(len(buf) - 1):
                    if buf[i] == ETX and buf[i + 1] == EOT:
                        end_pos = i + 2
                        break

                if end_pos == -1:
                    break  # 尚未收到完整封包

                packet = buf[:end_pos]
                self._recv_buffers[channel] = buf[end_pos:]

                result = BluetoothProtocol.parse_db_packet(packet)
                if result:
                    device.last_data = result
                    device.device_id = result.meter_id

                    # 回呼
                    if self._on_data_callback:
                        self._on_data_callback(device.channel, result)
        except socket.timeout:
            pass
        except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
            # 連線異常斷開
            print(f"連線異常 ({self._ch_name(device.channel)}): {e}")
            self._disconnect_device(device)

    def _simulate_measurement(self, channel: int):
        """模擬量測資料"""
        import random
        device = self.devices.get(channel)
        if not device:
            return

        if self._sim_is_empty_measure:
            # 空槍量測：生成基準溫度 (36.3 ~ 36.7)，無耳溫套
            temp = round(random.uniform(36.3, 36.7), 2)
            self._sim_empty_values[channel] = temp
            trans_temp_raw = "0000"  # 無耳溫套
        else:
            # 溫度量測：基於空槍值 + 隨機誤差，有耳溫套
            base_temp = self._sim_empty_values.get(channel, 36.5)
            # 誤差範圍 -0.3 ~ +0.4，讓約 70% PASS, 30% FAIL
            error = round(random.uniform(-0.3, 0.4), 2)
            temp = round(base_temp + error, 2)
            trans_temp_raw = "1111"  # 有耳溫套

        data = ThermometerData(
            meter_id=f"SIM{channel:06d}",
            temperature=temp,
            trans_temperature=temp,
            trans_temp_raw=trans_temp_raw,
            temp_mode="1",
            timestamp=time.time()
        )
        device.last_data = data
        device.device_id = data.meter_id

        if self._on_data_callback:
            self._on_data_callback(channel, data)

    def _update_state(self, device: ThermometerDevice, state: ConnectionState):
        """更新連線狀態"""
        device.state = state
        if self._on_state_callback:
            self._on_state_callback(device.channel, state)
