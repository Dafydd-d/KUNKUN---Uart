"""Qt 版主窗口入口：python qt_app.py"""
import logging
import os
import queue
import sys
import threading
import time
from collections import deque
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

import serial.tools.list_ports
from PyQt6.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QTextCursor, QTextDocument
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QFileDialog, QFontDialog,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QStatusBar, QTextEdit,
    QVBoxLayout, QWidget,
)

from lang import LANG_DICT
from serial_core import SerialManager, build_payload
from conf_io import load_json, save_json
from app_paths import config_path, log_dir
from qt_dialogs import FontSettingsDialog, HCBluetoothPanel, SerialSettingsDialog
from qt_panels import AksonPanel, AutoAnswerPanel, MultiSendPanel
import qt_excel

SAVE_CONFIG_FILE = config_path("send_area_cache.json")
LANG_CONFIG_FILE = config_path("lang_config.json")
DISPLAY_CONFIG_FILE = config_path("display_config.json")
SERIAL_CONFIG_FILE = config_path("serial_config.json")
LOG_FILE = os.path.join(log_dir(), "usart_tool.log")
DATA_FRAMES_LIMIT = 20000  # 导出用数据帧上限，防止长挂机内存无限增长


def app_icon_path():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "app_rounded.png")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_rounded.png")


class SerialBridge(QObject):
    """后台线程 → 主线程的信号桥；Qt 队列连接保证槽函数在 UI 线程执行。"""
    frame_received = pyqtSignal(object)
    ports_ready = pyqtSignal(object)


class SendTextEdit(QTextEdit):
    """发送输入框：光标在首行/末行时用 ↑/↓ 翻看发送历史。"""

    def __init__(self, window):
        super().__init__()
        self.window = window

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Up:
            if self.textCursor().blockNumber() == 0 and self.window.history_prev():
                return
        elif key == Qt.Key.Key_Down:
            if self.textCursor().blockNumber() == self.document().blockCount() - 1 \
                    and self.window.history_next():
                return
        super().keyPressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.lang = LANG_DICT[self.load_lang_config()]
        self.current_lang = self.load_lang_config()
        self.serial_manager = SerialManager()
        self.serial_manager.serial_params["parity_text"] = self.lang.get("none_parity", "无校验")
        self.serial_manager.serial_params["flow_text"] = self.lang.get("none_flow", "无流控")
        self.serial_manager.set_recv_frame_length(0)

        self.bridge = SerialBridge()
        self.bridge.frame_received.connect(self._on_frame)
        self.bridge.ports_ready.connect(self._apply_port_refresh)

        self._setup_logging()
        self.data_frames = deque(maxlen=DATA_FRAMES_LIMIT)
        self._recv_file = None
        self._recv_log_queue = queue.Queue()
        self._recv_log_flush_scheduled = False
        self._send_history = []
        self._send_history_idx = None
        self._send_history_draft = ""
        self._exporting = False
        self.auto_send_running = False
        self.buffer_limit = 200 * 1024

        self.load_serial_config()
        self._build_ui()
        self.build_menu()
        self.load_display_config()
        self.load_send_area_cache()

        # 端口自动刷新：查询放后台线程，结果回主线程
        self._port_timer = QTimer(self)
        self._port_timer.timeout.connect(self.auto_refresh_com)
        self._port_timer.start(2000)
        self.auto_send_timer = QTimer(self)
        self.auto_send_timer.timeout.connect(self._auto_send_once)
        if self.auto_send.isChecked():
            self.toggle_auto_send()
        if self.recv_to_file.isChecked():
            self._open_recv_file()
        self.refresh_com()

    # ========== 日志 ==========
    def _setup_logging(self):
        """日志经队列由后台线程写入并按大小轮转，避免高频接收时在 UI 线程同步写盘。"""
        handler = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self._log_queue = queue.Queue()
        self._log_listener = QueueListener(self._log_queue, handler)
        logging.getLogger().setLevel(logging.INFO)
        logging.getLogger().addHandler(QueueHandler(self._log_queue))
        self._log_listener.start()

    @staticmethod
    def get_ms_timestamp():
        t = time.time()
        sec = int(t)
        ms = int((t - sec) * 1000)
        return f"{time.strftime('%H:%M:%S', time.localtime(sec))}.{ms:03d}"

    def log_data(self, dir_name, data_bytes, fixed_ts=None):
        time_str, line, hex_str = self._make_log_line(dir_name, data_bytes, fixed_ts)
        self._append_log_lines([(time_str, dir_name, data_bytes, line, hex_str)])

    def _make_log_line(self, dir_name, data_bytes, fixed_ts=None):
        # HEX 字符串只格式化一次：显示与文件日志共用
        hex_str = " ".join(f"{b:02X}" for b in data_bytes)
        if self.hex_show.isChecked():
            content = hex_str
        else:
            content = "".join(
                chr(b) if 32 <= b <= 126 or b in (10, 13) else "." for b in data_bytes)
        time_str = fixed_ts if fixed_ts else self.get_ms_timestamp()
        if self.show_timestamp.isChecked():
            line = f"[{time_str}] {dir_name} ◆ {content}\n"
        else:
            line = f"{dir_name} ◆ {content}\n"
        return time_str, line, hex_str

    def _append_log_lines(self, records):
        if not records:
            return
        sb = self.log_text.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 2
        text = "".join(r[3] for r in records)
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self._trim_log_buffer()
        if at_bottom:
            sb.setValue(sb.maximum())
        for time_str, dir_name, data_bytes, _line, hex_str in records:
            self.data_frames.append((time_str, dir_name, bytes(data_bytes)))
            logging.info("%s len=%d hex=%s", dir_name, len(data_bytes), hex_str)

    def _trim_log_buffer(self):
        doc = self.log_text.document()
        overflow = doc.characterCount() - 1 - self.buffer_limit
        if overflow > 0:
            cursor = QTextCursor(doc)
            cursor.setPosition(0)
            cursor.setPosition(overflow, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()

    def _on_frame(self, data_bytes):
        self.hc_panel.handle_response(data_bytes)
        ts = self.get_ms_timestamp()
        self._recv_log_queue.put((ts, bytes(data_bytes)))
        if not self._recv_log_flush_scheduled:
            self._recv_log_flush_scheduled = True
            QTimer.singleShot(50, self._flush_recv_log_queue)

    def _flush_recv_log_queue(self):
        self._recv_log_flush_scheduled = False
        records = []
        recv_name = self.lang.get("dir_recv", "接收")
        while True:
            try:
                ts, data_bytes = self._recv_log_queue.get_nowait()
            except queue.Empty:
                break
            _, line, hex_str = self._make_log_line(recv_name, data_bytes, ts)
            records.append((ts, recv_name, data_bytes, line, hex_str))
            if self.auto_answer.answer_enabled:
                self.auto_answer.match_and_answer(data_bytes)
        self._append_log_lines(records)
        self._write_recv_file(records)
        self.update_status()
        if not self._recv_log_queue.empty() and not self._recv_log_flush_scheduled:
            self._recv_log_flush_scheduled = True
            QTimer.singleShot(50, self._flush_recv_log_queue)

    # ========== 界面构建 ==========
    def _build_ui(self):
        self.setWindowTitle(self.lang.get("app_title", "坤坤串口助手 V1.5"))
        icon = QIcon(app_icon_path())
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.resize(1000, 650)
        self.setMinimumSize(900, 580)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 2)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        # 左：收发日志 + 查找栏
        left_box = QGroupBox()
        self.recv_box = left_box
        left_layout = QVBoxLayout(left_box)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        left_layout.addWidget(self.log_text)

        self.find_bar = QWidget()
        find_row = QHBoxLayout(self.find_bar)
        find_row.setContentsMargins(0, 2, 0, 0)
        self.find_label = QLabel()
        find_row.addWidget(self.find_label)
        self.find_edit = QLineEdit()
        self.find_edit.setMaximumWidth(220)
        self.find_edit.returnPressed.connect(self.find_next)
        find_row.addWidget(self.find_edit)
        self.btn_find_prev = QPushButton()
        self.btn_find_prev.clicked.connect(self.find_prev)
        find_row.addWidget(self.btn_find_prev)
        self.btn_find_next = QPushButton()
        self.btn_find_next.clicked.connect(self.find_next)
        find_row.addWidget(self.btn_find_next)
        self.find_highlight = QCheckBox()
        self.find_highlight.toggled.connect(lambda _=False: self.find_highlight_all())
        find_row.addWidget(self.find_highlight)
        self.btn_find_close = QPushButton()
        self.btn_find_close.clicked.connect(self.close_find_bar)
        find_row.addWidget(self.btn_find_close)
        find_row.addStretch(1)
        left_layout.addWidget(self.find_bar)
        self.find_bar.hide()

        # 右：功能面板（懒加载，切换时只显示其一）
        self.right_container = QWidget()
        right_layout = QVBoxLayout(self.right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.akson_scroll = QScrollArea()
        self.akson_scroll.setWidgetResizable(True)
        self.akson = AksonPanel(self)
        self.akson_scroll.setWidget(self.akson)
        self.akson_scroll.hide()
        self.multi_panel = MultiSendPanel(self)
        self.multi_panel.hide()
        self.auto_answer = AutoAnswerPanel(self)
        self.auto_answer.hide()
        self.hc_panel = HCBluetoothPanel(self)
        self.hc_panel.hide()
        right_layout.addWidget(self.akson_scroll)
        right_layout.addWidget(self.multi_panel)
        right_layout.addWidget(self.auto_answer)
        right_layout.addWidget(self.hc_panel)

        self.splitter.addWidget(left_box)
        self.splitter.addWidget(self.right_container)
        self.right_container.hide()
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        # 初始按 日志:功能面板 ≈ 56:44 分栏，窗口拉宽时日志优先（stretch 3:1）；
        # 否则右侧面板的 sizeHint 会抢占大部分宽度，日志框被挤得很小
        self.splitter.setSizes([560, 440])
        self.splitter.setCollapsible(0, False)
        root.addWidget(self.splitter, 1)

        # 中部：串口配置 + 发送设置
        middle = QHBoxLayout()
        serial_box = QGroupBox()
        self.serial_box = serial_box
        sgrid = QVBoxLayout(serial_box)
        port_row = QHBoxLayout()
        self.port_label = QLabel()
        self.port_label.setMinimumWidth(64)
        port_row.addWidget(self.port_label)
        self.cb_com = QComboBox()
        self.cb_com.setMinimumWidth(180)
        port_row.addWidget(self.cb_com, 1)
        sgrid.addLayout(port_row)
        baud_row = QHBoxLayout()
        self.baudrate_label = QLabel()
        self.baudrate_label.setMinimumWidth(64)
        baud_row.addWidget(self.baudrate_label)
        self.baudrate = QComboBox()
        self.baudrate.addItems(["1200", "2400", "4800", "9600", "19200", "38400",
                                "57600", "115200", "230400", "460800", "921600"])
        self.baudrate.currentTextChanged.connect(self.apply_baudrate)
        baud_row.addWidget(self.baudrate, 1)
        sgrid.addLayout(baud_row)
        btn_row = QHBoxLayout()
        self.btn_open = QPushButton()
        self.btn_open.clicked.connect(self.toggle_serial)
        btn_row.addWidget(self.btn_open)
        self.btn_refresh = QPushButton()
        self.btn_refresh.clicked.connect(self.refresh_com)
        btn_row.addWidget(self.btn_refresh)
        self.btn_clear_win = QPushButton()
        self.btn_clear_win.clicked.connect(self.clear_recv)
        btn_row.addWidget(self.btn_clear_win)
        sgrid.addLayout(btn_row)
        ctrl_row = QHBoxLayout()
        self.rts_state = QCheckBox("RTS")
        self.rts_state.toggled.connect(self.update_rts_dtr)
        ctrl_row.addWidget(self.rts_state)
        self.dtr_state = QCheckBox("DTR")
        self.dtr_state.setChecked(True)
        self.dtr_state.toggled.connect(self.update_rts_dtr)
        ctrl_row.addWidget(self.dtr_state)
        ctrl_row.addStretch(1)
        sgrid.addLayout(ctrl_row)
        self.btn_more = QPushButton()
        self.btn_more.clicked.connect(self.open_more_settings)
        sgrid.addWidget(self.btn_more)
        middle.addWidget(serial_box)

        send_box = QGroupBox()
        self.send_box = send_box
        sv = QVBoxLayout(send_box)
        opt1 = QHBoxLayout()
        self.hex_show = QCheckBox()
        self.hex_show.setChecked(True)
        opt1.addWidget(self.hex_show)
        self.btn_save_data = QPushButton()
        self.btn_save_data.clicked.connect(self.save_data)
        opt1.addWidget(self.btn_save_data)
        self.recv_to_file = QCheckBox()
        self.recv_to_file.toggled.connect(self.toggle_recv_to_file)
        opt1.addWidget(self.recv_to_file)
        self.hex_send = QCheckBox()
        self.hex_send.setChecked(True)
        opt1.addWidget(self.hex_send)
        self.auto_send = QCheckBox()
        self.auto_send.toggled.connect(self.toggle_auto_send)
        opt1.addWidget(self.auto_send)
        self.auto_send_interval = QLineEdit("1000")
        self.auto_send_interval.setMaximumWidth(70)
        opt1.addWidget(self.auto_send_interval)
        self.ms_label = QLabel()
        opt1.addWidget(self.ms_label)
        self.add_crlf = QCheckBox()
        opt1.addWidget(self.add_crlf)
        opt1.addStretch(1)
        sv.addLayout(opt1)
        opt2 = QHBoxLayout()
        self.show_timestamp = QCheckBox()
        self.show_timestamp.setChecked(True)
        opt2.addWidget(self.show_timestamp)
        self.timeout_label = QLabel()
        opt2.addWidget(self.timeout_label)
        self.timeout_ms = QLineEdit("100")
        self.timeout_ms.setMaximumWidth(60)
        self.timeout_ms.editingFinished.connect(self.apply_timeout)
        opt2.addWidget(self.timeout_ms)
        opt2.addWidget(QLabel("ms"))
        self.add_checksum = QCheckBox()
        opt2.addWidget(self.add_checksum)
        self.checksum_type = QComboBox()
        self.checksum_type.addItems(["ADD8", "CRC16"])
        self.checksum_type.setMaximumWidth(80)
        opt2.addWidget(self.checksum_type)
        self.enc_label = QLabel()
        opt2.addWidget(self.enc_label)
        self.text_encoding = QComboBox()
        self.text_encoding.addItems(["GBK", "UTF-8"])
        self.text_encoding.setMaximumWidth(80)
        opt2.addWidget(self.text_encoding)
        opt2.addStretch(1)
        sv.addLayout(opt2)

        self.txt_manual_send = SendTextEdit(self)
        self.txt_manual_send.setAcceptRichText(False)
        self.txt_manual_send.setFixedHeight(96)
        sv.addWidget(self.txt_manual_send, 1)
        btn_row2 = QHBoxLayout()
        btn_row2.addStretch(1)
        self.btn_clear_send = QPushButton()
        self.btn_clear_send.clicked.connect(lambda: self.txt_manual_send.clear())
        btn_row2.addWidget(self.btn_clear_send)
        self.btn_send_manual = QPushButton()
        self.btn_send_manual.clicked.connect(self.send_manual_data)
        btn_row2.addWidget(self.btn_send_manual)
        sv.addLayout(btn_row2)
        middle.addWidget(send_box, 1)
        root.addLayout(middle)

        self.setCentralWidget(central)
        status = QStatusBar()
        self.setStatusBar(status)
        self.lbl_send = QLabel("S: 0")
        self.lbl_recv = QLabel("R: 0")
        self.lbl_status = QLabel()
        self.lbl_param = QLabel("9600bps,8,1,无校验")
        for w in (self.lbl_send, self.lbl_recv, self.lbl_status, self.lbl_param):
            status.addWidget(w)

        self.refresh_ui_text()

    # ========== 菜单 ==========
    def build_menu(self):
        mb = self.menuBar()
        mb.clear()
        lg = self.lang

        m_file = mb.addMenu(lg.get("file", "文件"))
        m_file.addAction(lg.get("clear_recv", "清空接收"), self.clear_recv)
        m_file.addAction(lg.get("save_data", "保存数据"), self.save_data)
        m_file.addSeparator()
        m_file.addAction(lg.get("exit", "退出"), self.close)

        m_serial = mb.addMenu(lg.get("serial_setting", "串口设置"))
        m_serial.addAction(lg.get("open_close_serial", "打开/关闭串口"), self.toggle_serial)
        m_serial.addAction(lg.get("refresh_com", "刷新端口"), self.refresh_com)
        m_serial.addAction(lg.get("more_serial_setting", "更多串口设置"), self.open_more_settings)

        m_display = mb.addMenu(lg.get("display", "显示"))
        m_display.addAction(lg.get("font_setting", "字体设置"), self.open_font_settings)
        m_display.addAction(lg.get("bg_color", "背景颜色"), self.set_background_color)
        m_buffer = m_display.addMenu(lg.get("buffer_limit", "缓冲上限"))
        self._buffer_group = []
        for name, size in [("200K", 200 * 1024), ("500K", 500 * 1024), ("1M", 1024 * 1024),
                           ("2M", 2 * 1024 * 1024), ("5M", 5 * 1024 * 1024)]:
            act = QAction(name, self, checkable=True)
            act.triggered.connect(lambda _, s=size: self.set_buffer_limit(s))
            m_buffer.addAction(act)
            self._buffer_group.append((act, size))
        m_lang = m_display.addMenu(lg.get("lang", "语言"))
        for name, key in [("中文", "zh"), ("English", "en")]:
            act = QAction(name, self, checkable=True)
            act.setChecked(self.current_lang == key)
            act.triggered.connect(lambda _, k=key: self.switch_lang(k))
            m_lang.addAction(act)
        m_display.addSeparator()
        m_display.addAction(lg.get("help", "帮助"), self.show_display_help)

        m_tool = mb.addMenu(lg.get("tool", "工具"))
        m_tool.addAction(lg.get("multi_send", "多路发送"), self.toggle_multi_panel)
        m_tool.addAction(lg.get("auto_answer", "自动应答"), self.toggle_auto_answer_panel)
        m_tool.addAction(lg.get("save_excel", "导出Excel"), self.export_excel)

        m_extend = mb.addMenu(lg.get("extend_tool", "扩展工具"))
        m_extend.addAction(lg.get("akson", "埃克森"), self.toggle_akson_panel)
        m_extend.addAction(lg.get("hc_bluetooth", "HC 蓝牙配置"), self.toggle_hc_panel)

        m_help = mb.addMenu(lg.get("help", "帮助"))
        m_help.addAction(lg.get("update_history", "更新历史"), self.show_update_history)
        m_help.addAction(lg.get("about", "关于"), self.show_about)

    def switch_lang(self, lang_key):
        self.current_lang = lang_key
        self.lang = LANG_DICT[lang_key]
        self.serial_manager.serial_params["parity_text"] = self.lang.get("none_parity", "无校验")
        self.serial_manager.serial_params["flow_text"] = self.lang.get("none_flow", "无流控")
        self.build_menu()
        self.refresh_ui_text()
        self.multi_panel.refresh_lang()
        self.auto_answer.refresh_lang()
        self.akson.refresh_lang()
        self.akson.reset_custom_tip()
        self.hc_panel.refresh_lang()
        self.save_lang_config()

    def refresh_ui_text(self):
        lg = self.lang
        self.setWindowTitle(lg.get("app_title", "坤坤串口助手 V1.5"))
        self.recv_box.setTitle(lg.get("recv_log", "收发日志"))
        self.akson.setTitle(lg.get("akson_panel_title", "埃克森协议"))
        self.find_label.setText(lg.get("find", "查找"))
        self.btn_find_prev.setText(lg.get("find_prev", "上一个"))
        self.btn_find_next.setText(lg.get("find_next", "下一个"))
        self.find_highlight.setText(lg.get("find_highlight_all", "全部高亮"))
        self.btn_find_close.setText(lg.get("close", "关闭"))
        self.serial_box.setTitle(lg.get("serial_config", "串口配置"))
        self.port_label.setText(lg.get("port_num", "端口号:"))
        self.baudrate_label.setText(lg.get("baudrate_label", "波特率:"))
        self.btn_open.setText(lg.get("close_serial", "关闭串口") if self.serial_manager.is_open
                              else lg.get("open_serial", "打开串口"))
        self.btn_refresh.setText(lg.get("refresh", "刷新"))
        self.btn_clear_win.setText(lg.get("clear_window", "清空窗口"))
        self.btn_more.setText(lg.get("more_serial_setting", "更多串口设置"))
        self.send_box.setTitle(lg.get("send_setting", "发送设置"))
        self.hex_show.setText(lg.get("hex_show", "HEX显示"))
        self.btn_save_data.setText(lg.get("save_data", "保存数据"))
        self.recv_to_file.setText(lg.get("recv_to_file", "接收数据到文件"))
        self.hex_send.setText(lg.get("hex_send", "HEX发送"))
        self.auto_send.setText(lg.get("timing_send", "定时发送"))
        self.ms_label.setText(lg.get("ms_once", "ms/次"))
        self.add_crlf.setText(lg.get("add_crlf", "加回车换行"))
        self.show_timestamp.setText(lg.get("add_timestamp", "加时间戳和分包显示"))
        self.timeout_label.setText(lg.get("timeout", "超时时间:"))
        self.add_checksum.setText(lg.get("add_checksum", "加校验"))
        self.enc_label.setText(lg.get("encoding_label", "编码:"))
        self.btn_clear_send.setText(lg.get("clear_send_area", "清发送区"))
        self.btn_send_manual.setText(lg.get("send", "发送"))
        self.lbl_status.setText(lg.get("serial_opened", "串口已打开") if self.serial_manager.is_open
                                else lg.get("serial_closed", "串口已关闭"))
        self.update_status_param()

    # ========== 面板切换 ==========
    def _hide_all_panels(self):
        self.akson_scroll.hide()
        self.multi_panel.hide()
        self.auto_answer.hide()
        self.hc_panel.hide()

    def _show_right(self):
        self.right_container.show()

    def toggle_akson_panel(self):
        visible = self.akson_scroll.isVisible()
        self._hide_all_panels()
        if visible:
            self.serial_manager.set_recv_frame_length(0)
            self.right_container.hide()
        else:
            self.akson_scroll.show()
            self.serial_manager.set_recv_frame_length(33)
            self._show_right()

    def toggle_multi_panel(self):
        visible = self.multi_panel.isVisible()
        self._hide_all_panels()
        if visible:
            self.multi_panel.stop_cycle_send()
            self.right_container.hide()
        else:
            self.serial_manager.set_recv_frame_length(0)
            self.multi_panel.show()
            self._show_right()

    def toggle_auto_answer_panel(self):
        visible = self.auto_answer.isVisible()
        self._hide_all_panels()
        if visible:
            self.right_container.hide()
        else:
            self.serial_manager.set_recv_frame_length(0)
            self.auto_answer.show()
            self._show_right()

    def toggle_hc_panel(self):
        visible = self.hc_panel.isVisible()
        self._hide_all_panels()
        if visible:
            self.right_container.hide()
        else:
            self.serial_manager.set_recv_frame_length(0)
            self.hc_panel.show()
            self._show_right()

    # ========== 发送 ==========
    def process_send_data(self, input_str, show_error=True):
        try:
            return build_payload(
                input_str,
                hex_mode=self.hex_send.isChecked(),
                add_crlf=self.add_crlf.isChecked(),
                checksum=self.checksum_type.currentText() if self.add_checksum.isChecked() else None,
                encoding=self.text_encoding.currentText(),
            )
        except ValueError:
            if show_error:
                QMessageBox.critical(self, self.lang.get("format_error", "格式错误"),
                                     self.lang.get("hex_char_tip", "HEX模式下请输入合法的十六进制字符(0-9,A-F)"))
            return None

    def send_manual_data(self, show_error=True):
        if not self.serial_manager.is_open:
            QMessageBox.warning(self, self.lang.get("warn", "警告"),
                                self.lang.get("open_serial_first", "请先打开串口"))
            return
        data_str = self.txt_manual_send.toPlainText().strip()
        if not data_str:
            return
        send_bytes = self.process_send_data(data_str, show_error=show_error)
        if not send_bytes:
            return
        if self.serial_manager.write(send_bytes):
            self._push_send_history(data_str)
            self.update_status()
            self.log_data(self.lang.get("dir_send", "发送"), send_bytes)
        else:
            fail = self.lang.get("send_fail", "发送失败")
            QMessageBox.critical(self, fail, fail)

    def _push_send_history(self, text):
        if text and (not self._send_history or self._send_history[-1] != text):
            self._send_history.append(text)
            if len(self._send_history) > 100:
                self._send_history.pop(0)
        self._send_history_idx = None

    def _show_history_entry(self):
        self.txt_manual_send.setPlainText(self._send_history[self._send_history_idx])
        cursor = self.txt_manual_send.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.txt_manual_send.setTextCursor(cursor)

    def history_prev(self):
        if not self._send_history:
            return False
        if self._send_history_idx is None:
            self._send_history_draft = self.txt_manual_send.toPlainText()
            self._send_history_idx = len(self._send_history) - 1
        elif self._send_history_idx > 0:
            self._send_history_idx -= 1
        self._show_history_entry()
        return True

    def history_next(self):
        if self._send_history_idx is None:
            return False
        self._send_history_idx += 1
        if self._send_history_idx >= len(self._send_history):
            self._send_history_idx = None
            self.txt_manual_send.setPlainText(self._send_history_draft)
            cursor = self.txt_manual_send.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.txt_manual_send.setTextCursor(cursor)
        else:
            self._show_history_entry()
        return True

    def toggle_auto_send(self):
        if self.auto_send.isChecked():
            if not self.serial_manager.is_open:
                QMessageBox.warning(self, self.lang.get("warn", "警告"),
                                    self.lang.get("open_serial_first", "请先打开串口"))
                self.auto_send.setChecked(False)
                return
            try:
                interval = max(10, int(self.auto_send_interval.text()))
            except ValueError:
                interval = 1000
            self.auto_send_running = True
            self.auto_send_timer.start(interval)
        else:
            self.auto_send_running = False
            self.auto_send_timer.stop()

    def _auto_send_once(self):
        if not (self.auto_send_running and self.serial_manager.is_open):
            self.auto_send.setChecked(False)
            self.auto_send_timer.stop()
            return
        data_str = self.txt_manual_send.toPlainText().strip()
        if not self.process_send_data(data_str, show_error=False):
            self.auto_send.setChecked(False)
            self.auto_send_timer.stop()
            QMessageBox.critical(self, self.lang.get("format_error", "格式错误"),
                                 self.lang.get("auto_send_stopped_tip", "定时发送已停止：请输入合法的十六进制字符"))
            return
        self.send_manual_data(show_error=False)

    # ========== 串口 ==========
    def update_rts_dtr(self):
        self.serial_manager.update_rts_dtr(self.rts_state.isChecked(), self.dtr_state.isChecked())

    def refresh_com(self):
        com_show_list, com_list = self.serial_manager.refresh_com()
        self.cb_com.blockSignals(True)
        self.cb_com.clear()
        self.cb_com.addItems(com_show_list)
        self.cb_com.blockSignals(False)
        if self.serial_manager.is_open or not com_show_list:
            return
        preferred = self.serial_manager.serial_params.get("port") or self.last_port
        if preferred in com_list:
            idx = com_list.index(preferred)
        else:
            idx = -1
            keywords = ["USB Serial Port", "USB-SERIAL CH340", "Bluetooth", "蓝牙", "BTH", "RFCOMM"]
            for i, desc in enumerate(com_show_list):
                if any(k in desc for k in keywords):
                    idx = i
                    break
        if idx < 0:
            idx = 0
        self.cb_com.setCurrentIndex(idx)
        self.serial_manager.serial_params["port"] = com_list[idx]

    def auto_refresh_com(self):
        threading.Thread(target=self._auto_refresh_worker, daemon=True).start()

    def _auto_refresh_worker(self):
        try:
            ports = [p.device for p in serial.tools.list_ports.comports()]
        except Exception:
            ports = []
        self.bridge.ports_ready.emit(ports)

    def _apply_port_refresh(self, current_ports):
        if current_ports != self.serial_manager.last_com_list:
            self.refresh_com()
        if self.serial_manager.is_open and self.serial_manager.ser:
            if self.serial_manager.ser.port not in current_ports:
                self.serial_manager.close_serial()
                self.auto_send.setChecked(False)
                self.auto_send_timer.stop()
                self.refresh_ui_text()
                self.update_status()

    def apply_baudrate(self, text):
        try:
            rate = int(text)
        except ValueError:
            return
        ok, msg = self.serial_manager.update_params({"baudrate": rate})
        self.update_status_param()
        if not ok:
            QMessageBox.critical(self, self.lang.get("format_error", "格式错误"), msg)

    def apply_timeout(self):
        try:
            value = max(1, int(self.timeout_ms.text()))
        except ValueError:
            value = 100
        self.timeout_ms.setText(str(value))
        self.serial_manager.set_recv_timeout(value)

    def update_status_param(self):
        p = self.serial_manager.serial_params
        self.lbl_param.setText(f"{p['baudrate']}bps,{p['bytesize']},{p['stopbits']},{p['parity_text']}")

    def update_status(self):
        self.lbl_send.setText(f"S: {self.serial_manager.send_count}")
        self.lbl_recv.setText(f"R: {self.serial_manager.recv_count}")

    def toggle_serial(self):
        if not self.serial_manager.is_open:
            idx = self.cb_com.currentIndex()
            if idx < 0 or idx >= len(self.serial_manager.com_list):
                QMessageBox.warning(self, self.lang.get("warn", "警告"),
                                    self.lang.get("select_valid_port", "请选择有效端口"))
                return
            port = self.serial_manager.com_list[idx]
            success, msg = self.serial_manager.open_serial(
                port, self.baudrate.currentText(),
                self.rts_state.isChecked(), self.dtr_state.isChecked())
            if success:
                try:
                    recv_timeout = max(1, int(self.timeout_ms.text()))
                except ValueError:
                    recv_timeout = 100
                    self.timeout_ms.setText("100")
                self.serial_manager.start_recv(
                    lambda data: self.bridge.frame_received.emit(bytes(data)), recv_timeout)
                if self.auto_send.isChecked() and not self.auto_send_running:
                    self.toggle_auto_send()
                logging.info("Serial opened: %s, %s bps", port, self.baudrate.currentText())
            else:
                logging.error("Open serial failed: %s", msg)
                QMessageBox.critical(self, self.lang.get("open_fail", "打开失败"), msg)
        else:
            self.save_send_area_cache()
            self.save_serial_config()
            self.auto_send_running = False
            self.auto_send_timer.stop()
            self.multi_panel.stop_cycle_send()
            self.serial_manager.close_serial()
            logging.info("Serial closed")
        self.refresh_ui_text()

    def open_more_settings(self):
        dialog = SerialSettingsDialog(self, self.serial_manager.serial_params,
                                      self.serial_manager.com_show_list,
                                      self.serial_manager.com_list, self.lang)
        if dialog.exec():
            ok, msg = self.serial_manager.update_params(dialog.result)
            self.baudrate.blockSignals(True)
            self.baudrate.setCurrentText(str(self.serial_manager.serial_params["baudrate"]))
            self.baudrate.blockSignals(False)
            self.update_status_param()
            if not ok:
                QMessageBox.critical(self, self.lang.get("format_error", "格式错误"), msg)

    # ========== 查找 ==========
    def open_find_bar(self):
        self.find_bar.show()
        self.find_edit.setFocus()
        self.find_edit.selectAll()

    def close_find_bar(self):
        self.find_bar.hide()
        self.log_text.setExtraSelections([])

    def find_next(self):
        self._find_move(backwards=False)

    def find_prev(self):
        self._find_move(backwards=True)

    def _find_move(self, backwards=False):
        needle = self.find_edit.text()
        if not needle:
            return
        if self.find_highlight.isChecked():
            self.find_highlight_all()
        flags = QTextDocument.FindFlag.FindBackward if backwards else QTextDocument.FindFlag(0)
        if not self.log_text.find(needle, flags):
            cursor = self.log_text.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End if backwards
                                else QTextCursor.MoveOperation.Start)
            self.log_text.setTextCursor(cursor)
            self.log_text.find(needle, flags)

    def find_highlight_all(self):
        self.log_text.setExtraSelections([])
        needle = self.find_edit.text()
        if not needle or not self.find_highlight.isChecked():
            return
        doc = self.log_text.document()
        fmt_bg = QColor(255, 224, 102)
        selections = []
        cursor = QTextCursor(doc)
        for _ in range(1000):
            cursor = doc.find(needle, cursor)
            if cursor.isNull():
                break
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format.setBackground(fmt_bg)
            selections.append(sel)
        self.log_text.setExtraSelections(selections)

    # ========== 日志保存 / 导出 ==========
    def clear_recv(self):
        self.log_text.clear()
        self.serial_manager.send_count = 0
        self.serial_manager.recv_count = 0
        self.data_frames.clear()
        self.update_status()

    def save_data(self):
        if not self.data_frames:
            QMessageBox.warning(self, self.lang.get("warn", "警告"),
                                self.lang.get("empty_log_tip", "日志为空，无需保存"))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, self.lang.get("save_data", "保存数据"),
            f"{time.strftime('%Y%m%d_%H%M%S')}.txt", "Text Files (*.txt);;All Files (*.*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for ts, dir_name, data in self.data_frames:
                    hex_str = " ".join(f"{b:02X}" for b in data)
                    f.write(f"[{ts}] {dir_name} ◆ {hex_str}\n")
            QMessageBox.information(self, self.lang.get("success", "成功"),
                                    self.lang.get("save_success_tip", "已保存至：{}").format(path))
        except Exception as e:
            QMessageBox.critical(self, self.lang.get("error", "错误"),
                                 self.lang.get("save_fail_tip", "保存失败：{}").format(str(e)))

    def export_excel(self):
        qt_excel.start_export(self, list(self.data_frames))

    def toggle_recv_to_file(self):
        if self.recv_to_file.isChecked():
            self._open_recv_file()
        else:
            self._close_recv_file()

    def _open_recv_file(self):
        if self._recv_file:
            return
        file_path = os.path.join(log_dir(), f"recv_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        try:
            self._recv_file = open(file_path, "a", encoding="utf-8")
            self._recv_file.write(f"# 接收数据记录起始 {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            self._recv_file.flush()
            logging.info("Recv-to-file started: %s", file_path)
        except Exception as e:
            self._recv_file = None
            self.recv_to_file.setChecked(False)
            QMessageBox.critical(self, self.lang.get("error", "错误"),
                                 self.lang.get("save_fail_tip", "保存失败：{}").format(str(e)))

    def _close_recv_file(self):
        if not self._recv_file:
            return
        try:
            self._recv_file.close()
        except Exception:
            pass
        self._recv_file = None
        logging.info("Recv-to-file stopped")

    def _write_recv_file(self, records):
        f = self._recv_file
        if not f or not records:
            return
        try:
            for ts, _dir, _data, _line, hex_str in records:
                f.write(f"[{ts}] {hex_str}\n")
            f.flush()
        except Exception:
            self._close_recv_file()
            self.recv_to_file.setChecked(False)

    # ========== 显示设置 ==========
    def open_font_settings(self):
        dialog = FontSettingsDialog(self, self.log_text.font(),
                                    self.log_text.palette().color(self.log_text.foregroundRole()), self.lang)
        if dialog.exec():
            self.log_text.setFont(dialog.result_font)
            palette = self.log_text.palette()
            palette.setColor(self.log_text.foregroundRole(), dialog.result_color)
            self.log_text.setPalette(palette)
            self.save_display_config()

    def set_background_color(self):
        current = self.log_text.palette().color(self.log_text.backgroundRole())
        color = QColorDialog.getColor(current, self, self.lang.get("bg_color_title", "选择背景颜色"))
        if color.isValid():
            palette = self.log_text.palette()
            palette.setColor(self.log_text.backgroundRole(), color)
            self.log_text.setPalette(palette)
            self.save_display_config()

    def set_buffer_limit(self, size):
        self.buffer_limit = size
        self._trim_log_buffer()
        self.save_display_config()

    def show_display_help(self):
        QMessageBox.information(self, self.lang.get("help", "帮助"),
                                self.lang.get("display_help_info", "支持自定义字体、背景色、缓冲区大小。"))

    def show_about(self):
        repo_url = "https://github.com/Dafydd-d/KUNKUN---Uart"
        content = self.lang.get("about_content", "坤坤串口助手 V1.5").replace("\n", "<br>")
        box = QMessageBox(QMessageBox.Icon.Information, self.lang.get("about", "关于"),
                          f'{content}<br><br><a href="{repo_url}">{repo_url}</a>',
                          QMessageBox.StandardButton.Ok, self)
        box.setTextFormat(Qt.TextFormat.RichText)
        label = box.findChild(QLabel, "qt_msgbox_label")
        if label is not None:
            label.linkActivated.connect(lambda u: QDesktopServices.openUrl(QUrl(u)))
        box.exec()

    def show_update_history(self):
        from qt_dialogs import UpdateHistoryDialog
        UpdateHistoryDialog(self, self.lang).exec()

    # ========== 配置持久化 ==========
    def load_lang_config(self):
        lang = load_json(LANG_CONFIG_FILE, {}).get("current_lang", "zh")
        return lang if lang in LANG_DICT else "zh"

    def save_lang_config(self):
        save_json(LANG_CONFIG_FILE, {"current_lang": self.current_lang})

    def load_serial_config(self):
        cfg = load_json(SERIAL_CONFIG_FILE)
        self.last_port = ""
        if not cfg:
            return
        self.last_port = cfg.get("port", "")
        self._serial_cfg = cfg

    def _apply_serial_cfg(self):
        cfg = getattr(self, "_serial_cfg", None) or {}
        self.baudrate.setCurrentText(str(cfg.get("baudrate", "9600")))
        self.rts_state.setChecked(cfg.get("rts", False))
        self.dtr_state.setChecked(cfg.get("dtr", True))
        self.hex_show.setChecked(cfg.get("hex_show", True))
        self.hex_send.setChecked(cfg.get("hex_send", True))
        self.show_timestamp.setChecked(cfg.get("show_timestamp", True))
        self.auto_send.setChecked(cfg.get("auto_send", False))
        self.auto_send_interval.setText(str(cfg.get("auto_send_interval", 1000)))
        self.add_crlf.setChecked(cfg.get("add_crlf", False))
        self.add_checksum.setChecked(cfg.get("add_checksum", False))
        self.checksum_type.setCurrentText(cfg.get("checksum_type", "ADD8"))
        self.timeout_ms.setText(str(cfg.get("timeout_ms", 100)))
        self.recv_to_file.setChecked(cfg.get("recv_to_file", False))
        enc = cfg.get("text_encoding", "GBK")
        self.text_encoding.setCurrentText(enc if enc in ("GBK", "UTF-8") else "GBK")
        self.serial_manager.update_rts_dtr(self.rts_state.isChecked(), self.dtr_state.isChecked())

    def save_serial_config(self):
        cfg = {
            "port": self.serial_manager.serial_params.get("port", ""),
            "baudrate": self.baudrate.currentText(),
            "rts": self.rts_state.isChecked(),
            "dtr": self.dtr_state.isChecked(),
            "hex_show": self.hex_show.isChecked(),
            "hex_send": self.hex_send.isChecked(),
            "show_timestamp": self.show_timestamp.isChecked(),
            "auto_send": self.auto_send.isChecked(),
            "auto_send_interval": self._int_or(self.auto_send_interval.text(), 1000),
            "add_crlf": self.add_crlf.isChecked(),
            "add_checksum": self.add_checksum.isChecked(),
            "checksum_type": self.checksum_type.currentText(),
            "timeout_ms": self._int_or(self.timeout_ms.text(), 100),
            "recv_to_file": self.recv_to_file.isChecked(),
            "text_encoding": self.text_encoding.currentText(),
        }
        save_json(SERIAL_CONFIG_FILE, cfg)

    @staticmethod
    def _int_or(text, default):
        try:
            return int(str(text).strip())
        except ValueError:
            return default

    def load_display_config(self):
        cfg = load_json(DISPLAY_CONFIG_FILE)
        if not cfg:
            self._apply_serial_cfg()
            return
        font = QFont(cfg.get("font_family", "Consolas"), int(cfg.get("font_size", 10)))
        font.setBold(cfg.get("font_weight", "normal") == "bold")
        font.setItalic(cfg.get("font_slant", "roman") == "italic")
        font.setUnderline(bool(cfg.get("font_underline", False)))
        font.setStrikeOut(bool(cfg.get("font_overstrike", False)))
        self.log_text.setFont(font)
        palette = self.log_text.palette()
        fg = QColor(cfg.get("fg_color", "#000000"))
        bg = QColor(cfg.get("bg_color", "#ffffff"))
        if fg.isValid():
            palette.setColor(self.log_text.foregroundRole(), fg)
        if bg.isValid():
            palette.setColor(self.log_text.backgroundRole(), bg)
        self.log_text.setPalette(palette)
        self.buffer_limit = int(cfg.get("buffer_limit", 200 * 1024))
        for act, size in self._buffer_group:
            act.setChecked(size == self.buffer_limit)
        geometry = cfg.get("qt_geometry")
        if geometry:
            try:
                import base64
                self.restoreGeometry(base64.b64decode(geometry))
            except Exception:
                pass
        # layout_version 门控：旧版保存的分栏宽度不继承，按新默认比例重建
        state = cfg.get("qt_state") if cfg.get("layout_version") == 2 else None
        if state:
            try:
                import base64
                self.splitter.restoreState(base64.b64decode(state))
            except Exception:
                pass
        self._apply_serial_cfg()

    def save_display_config(self):
        import base64
        palette = self.log_text.palette()
        cfg = {
            "font_family": self.log_text.font().family(),
            "font_size": self.log_text.font().pointSize(),
            "font_weight": "bold" if self.log_text.font().bold() else "normal",
            "font_slant": "italic" if self.log_text.font().italic() else "roman",
            "font_underline": self.log_text.font().underline(),
            "font_overstrike": self.log_text.font().strikeOut(),
            "fg_color": palette.color(self.log_text.foregroundRole()).name(),
            "bg_color": palette.color(self.log_text.backgroundRole()).name(),
            "buffer_limit": self.buffer_limit,
            "qt_geometry": base64.b64encode(self.saveGeometry()).decode("ascii"),
            "qt_state": base64.b64encode(self.splitter.saveState()).decode("ascii"),
            "layout_version": 2,
        }
        save_json(DISPLAY_CONFIG_FILE, cfg)

    def load_send_area_cache(self):
        text = load_json(SAVE_CONFIG_FILE, {}).get("send_text", "")
        if text:
            self.txt_manual_send.setPlainText(text)

    def save_send_area_cache(self):
        save_json(SAVE_CONFIG_FILE, {"send_text": self.txt_manual_send.toPlainText()})

    def closeEvent(self, event):
        self.save_send_area_cache()
        self.save_display_config()
        self.save_serial_config()
        self.multi_panel.save_cache()
        self.auto_answer.save_cache()
        self.auto_send_running = False
        self.auto_send_timer.stop()
        self.multi_panel.stop_cycle_send()
        self.serial_manager.close_serial()
        self._close_recv_file()
        self._log_listener.stop()
        event.accept()


def main():
    if sys.platform == "win32":
        try:
            # Per-Monitor V2 DPI 感知：打包 exe 未内嵌 dpiAware 清单时，
            # 否则 125%/150% 缩放下窗口被系统位图拉伸，细边框会糊掉
            import ctypes
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("坤坤串口助手")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
