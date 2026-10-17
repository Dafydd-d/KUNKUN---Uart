import serial
import serial.tools.list_ports
import threading
import time
import queue
from collections import deque

MAX_RECV_FRAME = 64 * 1024       # 普通模式单帧上限：持续流数据时强制拆包，防止缓冲无限增长
LOOPBACK_MAX_AGE = 2.0           # 回环帧匹配窗口（秒）：超时未回显即丢弃，防止队列无限增长
LOOPBACK_MAX_PENDING = 256       # 回环帧队列上限：防止设备不回显时长期挂机内存膨胀


def parse_hex_bytes(text):
    """HEX 文本转字节：忽略空白，奇数位左补零（111 → 01 11）。非法字符抛 ValueError。"""
    hex_str = "".join(text.split())
    if len(hex_str) % 2 != 0:
        hex_str = "0" + hex_str
    try:
        return bytes(int(hex_str[i:i + 2], 16) for i in range(0, len(hex_str), 2))
    except ValueError:
        raise ValueError("invalid hex text")


def checksum_add8(data_bytes):
    return sum(data_bytes) & 0xFF


def checksum_crc16(data_bytes):
    """CRC16/MODBUS：多项式 0xA001，初值 0xFFFF。"""
    crc = 0xFFFF
    for byte in data_bytes:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def build_payload(text, hex_mode, add_crlf=False, checksum=None, encoding="gbk"):
    """统一的发送数据准备：HEX/文本编码 + 可选回车换行 + 可选校验。

    校验覆盖回车换行在内；CRC16 按 MODBUS 惯例低字节在前。
    文本模式按 encoding 编码（默认 GBK，可选 UTF-8）。格式非法抛 ValueError。
    """
    data = parse_hex_bytes(text) if hex_mode else text.encode(encoding)
    if add_crlf:
        data += b"\r\n"
    if checksum == "ADD8":
        data += bytes([checksum_add8(data)])
    elif checksum == "CRC16":
        crc = checksum_crc16(data)
        data += bytes([crc & 0xFF, crc >> 8])
    return data


def pop_fixed_frames(buf, frame_len):
    """从缓冲区按固定长度切出完整帧，就地删除已切出的字节。"""
    frames = []
    if frame_len > 0:
        while len(buf) >= frame_len:
            frames.append(bytes(buf[:frame_len]))
            del buf[:frame_len]
    return frames


class SerialManager:
    def __init__(self):
        self.ser = None
        self.is_open = False
        self.running = False
        self.com_list = []
        self.com_show_list = []
        self.last_com_list = []
        self.serial_params = {
            "port": "", "baudrate": 9600, "bytesize": 8, "stopbits": 1, "parity": "N",
            "parity_text": "无校验", "rtscts": False, "xonxoff": False,
            "flow_text": "无"
        }
        self.send_count = 0
        self.recv_count = 0
        self.recv_callback = None
        self.recv_timeout = 0.1
        self._recv_thread = None
        self._send_thread = None
        self._send_queue = queue.Queue()
        self._write_lock = threading.Lock()
        self.recv_frame_length = 0
        self._recv_buffer = bytearray()
        # (时间戳, 已发送报文)：供普通模式识别串口回环并按发送长度即时分帧。
        # 带过期与容量上限，设备不回显时不会无限堆积。
        self._loopback_frames = deque()

    def refresh_com(self):
        """刷新串口列表"""
        ports = serial.tools.list_ports.comports()
        self.com_show_list = []
        self.com_list = []
        for port in ports:
            description = port.description or ""
            # 经典蓝牙串口模块配对后通常表现为系统虚拟 COM 口。
            # 在列表中明确标识，连接方式与 USB 串口完全一致，仍使用 pyserial 收发。
            lower_desc = description.lower()
            if any(key in lower_desc for key in ("bluetooth", "蓝牙", "bth", "rfcomm")):
                description = f"[蓝牙串口] {description}"
            self.com_show_list.append(f"{port.device} {description}")
            self.com_list.append(port.device)
        self.last_com_list = [p.device for p in ports]
        return self.com_show_list, self.com_list

    def open_serial(self, port, baudrate, rts_state, dtr_state):
        """打开串口，返回 (成功状态, 错误信息)"""
        try:
            self.serial_params["port"] = port
            self.serial_params["baudrate"] = int(baudrate)
            self.ser = self._make_serial(port)
            self.ser.rts = rts_state
            self.ser.dtr = dtr_state
            self.is_open = True
            self.running = True
            self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
            self._send_thread.start()
            return True, ""
        except Exception as e:
            return False, str(e)

    def _make_serial(self, port):
        return serial.Serial(
            port=port,
            baudrate=self.serial_params["baudrate"],
            bytesize=self.serial_params["bytesize"],
            stopbits=self.serial_params["stopbits"],
            parity=self.serial_params["parity"],
            rtscts=self.serial_params["rtscts"],
            xonxoff=self.serial_params["xonxoff"],
            timeout=0.001
        )

    def close_serial(self):
        """关闭串口并重置计数；等待收发线程退出，避免快速重开时新旧线程同时读串口。"""
        self.running = False
        self.is_open = False
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        for thread in (self._recv_thread, self._send_thread):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        self._recv_thread = None
        self._send_thread = None
        self._recv_buffer = bytearray()
        self._loopback_frames.clear()
        self.send_count = 0
        self.recv_count = 0

    def update_params(self, params):
        """热更新串口参数，返回 (成功状态, 错误信息)。

        端口号变更且串口已打开时热切换到新端口（收发线程与计数保持不变），
        切换失败保留原连接并回滚端口参数。
        """
        old_port = self.serial_params.get("port", "")
        self.serial_params.update(params)
        if not (self.is_open and self.ser):
            return True, ""
        new_port = self.serial_params.get("port", "")
        if new_port and new_port != getattr(self.ser, "port", old_port):
            ok, msg = self._hot_swap_port(new_port)
            if not ok:
                self.serial_params["port"] = old_port
            return ok, msg
        try:
            self.ser.baudrate = self.serial_params["baudrate"]
            self.ser.bytesize = self.serial_params["bytesize"]
            self.ser.stopbits = self.serial_params["stopbits"]
            self.ser.parity = self.serial_params["parity"]
            self.ser.rtscts = self.serial_params["rtscts"]
            self.ser.xonxoff = self.serial_params["xonxoff"]
            return True, ""
        except Exception as e:
            return False, str(e)

    def _hot_swap_port(self, port):
        """打开新端口成功后再替换底层对象并关闭旧端口，收发线程无感切换。"""
        try:
            new_ser = self._make_serial(port)
        except Exception as e:
            return False, str(e)
        try:
            new_ser.rts = self.ser.rts
            new_ser.dtr = self.ser.dtr
        except Exception:
            pass
        old = self.ser
        with self._write_lock:
            self.ser = new_ser
        # 换绑（而非 clear）接收缓冲，避免清掉接收线程正在组装的帧
        self._recv_buffer = bytearray()
        try:
            old.close()
        except Exception:
            pass
        return True, ""

    def update_rts_dtr(self, rts, dtr):
        if self.is_open and self.ser:
            try:
                self.ser.rts = rts
                self.ser.dtr = dtr
                return True
            except Exception:
                return False
        return False

    def write(self, data_bytes):
        """将发送任务加入唯一发送队列，避免循环发送和自动应答交叉写串口。

        入队即计数，界面刷新可立即看到发送量；实际写出在发送线程完成。
        """
        if not self.is_open or not self.ser:
            return False
        data_bytes = bytes(data_bytes)
        self.send_count += len(data_bytes)
        self._send_queue.put(data_bytes)
        return True

    def _send_loop(self):
        while self.running:
            try:
                data_bytes = self._send_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if not self.is_open or not self.ser or not self.ser.is_open:
                continue
            try:
                with self._write_lock:
                    self.ser.write(data_bytes)
                # 供普通模式下的串口回环使用：接收流中出现完整已发送报文时，
                # 可立即按该报文边界拆帧，而无需依赖帧间超时。
                self._note_loopback_frame(data_bytes)
            except Exception:
                # 上层会在下一次状态刷新时反映串口不可用；不能让发送线程崩溃。
                pass

    def _note_loopback_frame(self, data_bytes):
        """记录待匹配回环帧；超上限丢弃最旧的，防止设备不回显时队列膨胀。"""
        self._loopback_frames.append((time.monotonic(), data_bytes))
        while len(self._loopback_frames) > LOOPBACK_MAX_PENDING:
            self._loopback_frames.popleft()

    def start_recv(self, callback, timeout_ms=100):
        """启动后台接收线程；设置帧长后按固定长度逐帧回调。"""
        self.recv_callback = callback
        self.set_recv_timeout(timeout_ms)
        self.running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def set_recv_timeout(self, timeout_ms):
        """普通模式下的帧间合包超时（毫秒），可随时热更新。"""
        self.recv_timeout = max(1, int(timeout_ms)) / 1000

    def set_recv_frame_length(self, length=0):
        """设置固定接收帧长度；0 表示仍按串口空闲超时合包。"""
        self.recv_frame_length = max(0, int(length or 0))
        self._recv_buffer = bytearray()

    def _extract_loopback_frames(self):
        """从普通接收缓冲区中提取已发送报文的回环帧。

        队列条目带时间戳：超时未匹配（设备不回显）即丢弃；首字节不匹配时
        交给普通收包逻辑处理，避免错误丢弃真实设备数据。
        """
        frames = []
        now = time.monotonic()
        while True:
            try:
                ts, expected = self._loopback_frames[0]
            except IndexError:
                break
            if now - ts > LOOPBACK_MAX_AGE:
                self._loopback_frames.popleft()
                continue
            if len(self._recv_buffer) < len(expected):
                break
            if self._recv_buffer[:len(expected)] == expected:
                del self._recv_buffer[:len(expected)]
                self._loopback_frames.popleft()
                frames.append(expected)
            else:
                break
        return frames

    def stop_recv(self):
        self.running = False

    def _read_failure_is_swap(self, ser):
        """读失败时判断是否为端口热切换导致旧对象失效（可继续用新对象收数据）。"""
        return self.running and ser is not self.ser

    def _recv_loop(self):
        while self.running:
            ser = self.ser
            if not ser or not ser.is_open:
                break
            # 固定长度协议不依赖帧间超时；持续读取即可，凑满一帧立即回调。
            if self.recv_frame_length:
                try:
                    data = ser.read(ser.in_waiting or 1)
                except Exception:
                    if self._read_failure_is_swap(ser):
                        continue
                    break
                if not data:
                    time.sleep(0.005)
                    continue
                self._recv_buffer.extend(data)
                for frame in pop_fixed_frames(self._recv_buffer, self.recv_frame_length):
                    self.recv_count += len(frame)
                    if self.recv_callback:
                        self.recv_callback(frame)
                continue
            # 普通模式：如果是本软件刚发送的串口回环报文，按发送报文长度即时分帧。
            # 这样 3 字节的 11 12 23 连续回环也会显示为一帧一条。
            try:
                data = ser.read(ser.in_waiting or 1)
            except Exception:
                if self._read_failure_is_swap(ser):
                    continue
                break
            if not data:
                time.sleep(0.005)
                continue
            self._recv_buffer.extend(data)
            for frame in self._extract_loopback_frames():
                self.recv_count += len(frame)
                if self.recv_callback:
                    self.recv_callback(frame)
            buffer = self._recv_buffer
            self._recv_buffer = bytearray()
            # 第一批数据已在上方读取；没有回环匹配时继续按帧间超时聚合。
            start_time = time.time()
            while self.running and (time.time() - start_time) < self.recv_timeout:
                # 连续流数据不断重置计时会一直合包；到单帧上限强制拆包
                if len(buffer) >= MAX_RECV_FRAME:
                    break
                try:
                    data = ser.read(ser.in_waiting or 1)
                except Exception:
                    break
                if not data:
                    time.sleep(0.001)
                    continue
                buffer.extend(data)
                start_time = time.time()
            if buffer and self.recv_callback:
                self.recv_count += len(buffer)
                self.recv_callback(bytes(buffer))
