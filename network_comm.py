# -*- coding: utf-8 -*-
"""
Master-Slave 網路通訊模組 - 電腦 A/B 資料同步
"""
import json
import socket
import threading
import time
from dataclasses import dataclass, asdict
from typing import Callable, Dict, List, Optional
from enum import Enum

class NetworkRole(Enum):
    MASTER = "master"
    SLAVE = "slave"

class NetworkState(Enum):
    DISCONNECTED = "disconnected"
    LISTENING = "listening"
    CONNECTED = "connected"
    ERROR = "error"

@dataclass
class MeterDataPacket:
    """溫度資料封包"""
    channel: int          # 通道編號 (7-12 for slave)
    meter_id: str         # 設備 ID
    temperature: float    # 溫度值
    timestamp: float      # 時間戳記
    ear_cover: str = ""   # 耳溫套狀態 ("1111"/"0000")
    bt_state: str = ""    # 藍芽連線狀態 ("connected"/"disconnected"/...)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(data: str) -> Optional['MeterDataPacket']:
        try:
            d = json.loads(data)
            return MeterDataPacket(**d)
        except:
            return None


class NetworkManager:
    """網路通訊管理器"""

    def __init__(self, role: NetworkRole, port: int = 5001,
                 master_ip: str = "192.168.1.100"):
        self.role = role
        self.port = port
        self.master_ip = master_ip

        self._state = NetworkState.DISCONNECTED
        self._running = False
        self._server_socket: Optional[socket.socket] = None
        self._client_socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

        # 回呼函式
        self._on_data_received: Optional[Callable[[MeterDataPacket], None]] = None
        self._on_state_callback: Optional[Callable[[NetworkState], None]] = None
        self._on_command_callback: Optional[Callable[[str], None]] = None

        # 接收緩衝
        self._received_data: Dict[int, MeterDataPacket] = {}

    def set_callbacks(self,
                      on_data: Optional[Callable[[MeterDataPacket], None]] = None,
                      on_state: Optional[Callable[[NetworkState], None]] = None,
                      on_command: Optional[Callable[[str], None]] = None):
        """設定回呼函式"""
        self._on_data_received = on_data
        self._on_state_callback = on_state
        self._on_command_callback = on_command

    @property
    def state(self) -> NetworkState:
        return self._state

    def start(self):
        """啟動網路管理器"""
        if self._running:
            return

        self._running = True
        if self.role == NetworkRole.MASTER:
            self._thread = threading.Thread(target=self._master_loop, daemon=True)
        else:
            self._thread = threading.Thread(target=self._slave_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """停止網路管理器"""
        self._running = False
        if self._server_socket:
            try:
                self._server_socket.close()
            except:
                pass
        if self._client_socket:
            try:
                self._client_socket.close()
            except:
                pass
        self._update_state(NetworkState.DISCONNECTED)

    def send_data(self, packet: MeterDataPacket) -> bool:
        """傳送資料 (Slave 用)"""
        if self.role != NetworkRole.SLAVE:
            return False
        if self._state != NetworkState.CONNECTED:
            return False

        try:
            data = packet.to_json() + "\n"
            self._client_socket.send(data.encode('utf-8'))
            return True
        except Exception as e:
            print(f"傳送資料失敗: {e}")
            self._update_state(NetworkState.ERROR)
            return False

    def send_command(self, command: str) -> bool:
        """傳送指令至 Slave (Master 用)"""
        if self.role != NetworkRole.MASTER:
            return False
        if self._state != NetworkState.CONNECTED or not self._client_socket:
            return False

        try:
            data = json.dumps({"type": "command", "command": command}) + "\n"
            self._client_socket.send(data.encode('utf-8'))
            return True
        except Exception as e:
            print(f"傳送指令失敗: {e}")
            return False

    def get_received_data(self, channel: int) -> Optional[MeterDataPacket]:
        """取得接收到的資料 (Master 用)"""
        return self._received_data.get(channel)

    def get_all_received_data(self) -> Dict[int, MeterDataPacket]:
        """取得所有接收到的資料"""
        return self._received_data.copy()

    def clear_received_data(self):
        """清除接收緩衝"""
        self._received_data.clear()

    def _master_loop(self):
        """Master 端迴圈 (接收 Slave 資料)"""
        while self._running:
            try:
                # 建立 Server Socket
                self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._server_socket.bind(('0.0.0.0', self.port))
                self._server_socket.listen(1)
                self._server_socket.settimeout(1.0)
                self._update_state(NetworkState.LISTENING)

                while self._running:
                    try:
                        client, addr = self._server_socket.accept()
                        print(f"Slave 連線: {addr}")
                        self._client_socket = client
                        self._client_socket.settimeout(1.0)
                        self._update_state(NetworkState.CONNECTED)
                        self._receive_loop()
                    except socket.timeout:
                        continue
                    except Exception as e:
                        print(f"接受連線錯誤: {e}")
                        break

            except Exception as e:
                print(f"Master 錯誤: {e}")
                self._update_state(NetworkState.ERROR)
                time.sleep(1)

    def _slave_loop(self):
        """Slave 端迴圈 (連線至 Master，接收指令)"""
        while self._running:
            try:
                self._client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._client_socket.settimeout(5.0)
                self._client_socket.connect((self.master_ip, self.port))
                self._client_socket.settimeout(1.0)  # recv 逾時
                self._update_state(NetworkState.CONNECTED)
                print(f"已連線至 Master: {self.master_ip}:{self.port}")

                # 接收 Master 指令
                buffer = ""
                while self._running and self._state == NetworkState.CONNECTED:
                    try:
                        data = self._client_socket.recv(4096)
                        if not data:
                            print("Master 斷線")
                            break
                        buffer += data.decode('utf-8')
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            if line.strip():
                                self._handle_slave_received(line.strip())
                    except socket.timeout:
                        continue
                    except Exception as e:
                        print(f"Slave 接收錯誤: {e}")
                        break

                self._update_state(NetworkState.ERROR)

            except Exception as e:
                print(f"Slave 連線失敗: {e}")
                self._update_state(NetworkState.ERROR)
                time.sleep(3)  # 等待後重試

    def _handle_slave_received(self, line: str):
        """處理 Slave 端收到的 Master 指令"""
        try:
            d = json.loads(line)
            if d.get("type") == "command":
                cmd = d.get("command", "")
                if self._on_command_callback:
                    self._on_command_callback(cmd)
        except Exception as e:
            print(f"解析 Master 指令錯誤: {e}")

    def _receive_loop(self):
        """接收資料迴圈"""
        buffer = ""
        while self._running and self._state == NetworkState.CONNECTED:
            try:
                data = self._client_socket.recv(4096)
                if not data:
                    print("Slave 斷線")
                    break

                buffer += data.decode('utf-8')

                # 處理完整的 JSON 行
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.strip():
                        packet = MeterDataPacket.from_json(line)
                        if packet:
                            self._received_data[packet.channel] = packet
                            if self._on_data_received:
                                self._on_data_received(packet)

            except socket.timeout:
                continue
            except Exception as e:
                print(f"接收資料錯誤: {e}")
                break

        self._update_state(NetworkState.LISTENING)

    def _update_state(self, state: NetworkState):
        """更新連線狀態"""
        self._state = state
        if self._on_state_callback:
            self._on_state_callback(state)
