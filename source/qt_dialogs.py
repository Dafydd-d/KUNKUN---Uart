"""Qt 版对话框与嵌入面板：串口设置、字体设置、HC 蓝牙配置、更新历史。"""
import threading

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFontDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

from update_history import UPDATE_HISTORY


class SerialSettingsDialog(QDialog):
    """串口参数设置弹窗；确定后 result 为参数字典。"""

    def __init__(self, parent, current_params, com_show_list, com_list, lang):
        super().__init__(parent)
        self.lang = lang
        self.setWindowTitle(lang["serial_dialog_title"])
        self.setModal(True)
        self.result = None
        self._com_show_list = com_show_list
        self._com_list = com_list

        form = QFormLayout()
        # 下拉框显示带描述的端口串，确认时映射回设备名（COM3）
        port_show = current_params.get("port", "")
        if port_show in com_list:
            port_show = com_show_list[com_list.index(port_show)]
        self.cb_port = QComboBox()
        self.cb_port.addItems(com_show_list)
        if port_show in com_show_list:
            self.cb_port.setCurrentText(port_show)
        form.addRow(lang["port"], self.cb_port)

        self.cb_baud = QComboBox()
        self.cb_baud.addItems(["1200", "2400", "4800", "9600", "14400", "19200",
                               "38400", "57600", "115200", "230400", "460800", "921600"])
        self.cb_baud.setCurrentText(str(current_params["baudrate"]))
        form.addRow(lang["baudrate"], self.cb_baud)

        self.cb_data = QComboBox()
        self.cb_data.addItems(["5", "6", "7", "8"])
        self.cb_data.setCurrentText(str(current_params["bytesize"]))
        form.addRow(lang["data_bit"], self.cb_data)

        self.cb_stop = QComboBox()
        self.cb_stop.addItems(["1", "1.5", "2"])
        self.cb_stop.setCurrentText(str(current_params["stopbits"]))
        form.addRow(lang["stop_bit"], self.cb_stop)

        parity_items = [lang["none_parity"], lang["odd"], lang["even"], lang["mark"], lang["space"]]
        self.cb_parity = QComboBox()
        self.cb_parity.addItems(parity_items)
        self.cb_parity.setCurrentText(current_params["parity_text"])
        form.addRow(lang["parity"], self.cb_parity)

        flow_items = [lang["none_flow"], lang["hardware_flow"], lang["software_flow"]]
        self.cb_flow = QComboBox()
        self.cb_flow.addItems(flow_items)
        self.cb_flow.setCurrentText(current_params["flow_text"])
        form.addRow(lang["flow_ctrl"], self.cb_flow)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(lang["confirm"])
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(lang["cancel"])
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_ok(self):
        parity_map = {
            self.lang["none_parity"]: "N", self.lang["odd"]: "O", self.lang["even"]: "E",
            self.lang["mark"]: "M", self.lang["space"]: "S",
        }
        flow = self.cb_flow.currentText()
        # 选中的显示串映射回设备名；未匹配时保留原值
        sel = self.cb_port.currentText()
        port = self._com_list[self._com_show_list.index(sel)] if sel in self._com_show_list else sel
        self.result = {
            "port": port,
            "baudrate": int(self.cb_baud.currentText()),
            "bytesize": int(self.cb_data.currentText()),
            "stopbits": float(self.cb_stop.currentText()),
            "parity": parity_map[self.cb_parity.currentText()],
            "parity_text": self.cb_parity.currentText(),
            "rtscts": flow == self.lang["hardware_flow"],
            "xonxoff": flow == self.lang["software_flow"],
            "flow_text": flow,
        }
        self.accept()


class FontSettingsDialog(QDialog):
    """字体/颜色设置：字体按钮 + 颜色按钮 + 实时预览。"""

    def __init__(self, parent, cur_font, cur_color, lang):
        super().__init__(parent)
        self.lang = lang
        self.setWindowTitle(lang.get("font_win_title", "字体"))
        self.setModal(True)
        self.result_font = None
        self.result_color = None
        self._font = QFont(cur_font)
        self._color = QColor(cur_color)

        self.preview = QLabel(lang.get("font_sample_text", "微软中文软件"))
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(260, 90)

        btn_font = QPushButton(lang.get("font_label", "字体(F):"))
        btn_font.clicked.connect(self._pick_font)
        btn_color = QPushButton(lang.get("font_color_label", "颜色(C):"))
        btn_color.clicked.connect(self._pick_color)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(lang.get("confirm", "确定"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(lang.get("cancel", "取消"))
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(btn_font)
        row.addWidget(btn_color)
        layout.addLayout(row)
        layout.addWidget(self.preview)
        layout.addWidget(buttons)
        self._refresh_preview()

    def _refresh_preview(self):
        self.preview.setFont(self._font)
        self.preview.setStyleSheet(
            f"color: {self._color.name()}; background-color: white; border: 1px solid #888;")

    def _pick_font(self):
        font, ok = QFontDialog.getFont(self._font, self, self.lang.get("font_win_title", "字体"))
        if ok:
            self._font = font
            self._refresh_preview()

    def _pick_color(self):
        color = QColorDialog.getColor(self._color, self, self.lang.get("font_color_title", "选择文字颜色"))
        if color.isValid():
            self._color = color
            self._refresh_preview()

    def _on_ok(self):
        self.result_font = QFont(self._font)
        self.result_color = QColor(self._color)
        self.accept()


class UpdateHistoryDialog(QDialog):
    """更新历史查看窗口。"""

    def __init__(self, parent, lang):
        super().__init__(parent)
        self.setWindowTitle(lang.get("update_history", "更新历史"))
        self.resize(620, 420)
        self.setModal(True)
        layout = QVBoxLayout(self)
        browser = QTextBrowser()
        browser.setPlainText(UPDATE_HISTORY)
        layout.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class HCBluetoothPanel(QGroupBox):
    """HC 系列蓝牙模块 AT 配置面板：嵌入主窗口右侧面板，复用主窗口串口收发。"""

    BAUDRATES = ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"]

    # 后台搜索线程 → 主线程的信号
    _search_done = pyqtSignal(object, object)  # (found_rate or None, original_rate)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.lang = app.lang
        self._t = self.lang.get
        self.led_on = True
        self._searching = False
        self._response_event = threading.Event()
        self._last_response = b""
        self._text_refs = []  # (widget, lang_key, 默认值) 供语言切换批量刷新
        self._search_done.connect(self._apply_search_result)
        self._build()
        self.refresh_lang()

    def _reg(self, widget, key, default):
        """登记需要随语言切换刷新文案的控件。"""
        self._text_refs.append((widget, key, default))
        return widget

    def refresh_lang(self):
        self.lang = self.app.lang
        self._t = self.lang.get
        self.setTitle(self._t("hc_title", "HC 蓝牙配置"))
        for widget, key, default in self._text_refs:
            widget.setText(self._t(key, default))
        self.status_label.setText(self._t("hc_ready", "就绪"))

    def _build(self):
        layout = QVBoxLayout(self)
        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: #185a9d;")
        layout.addWidget(self.status_label)
        self.response_label = QLabel("")
        self.response_label.setWordWrap(True)
        self.response_label.setStyleSheet("color: #555;")
        layout.addWidget(self.response_label)

        grid = QGridLayout()
        rows = [
            ("hc_baud_query", "波特率查询", self.query_baud,
             "hc_test_cmd", "测试指令", lambda: self.send_at("AT")),
            ("hc_firmware", "固件版本", lambda: self.send_at("AT+VERSION"),
             "hc_factory_reset", "恢复出厂设置", lambda: self.confirm_send("AT+DEFAULT")),
            ("hc_query_led", "查询 LED", lambda: self.send_at("AT+LED=?"),
             "hc_led_off", "关闭 LED", lambda: self.set_led(False)),
            ("hc_set_spp_master", "设置 SPP 主机", lambda: self.send_at("AT+ROLE=M"),
             "hc_set_ble_master", "设置 BLE 主机", lambda: self.send_at("AT+ROLE=BM")),
            ("hc_set_slave", "设置从机", lambda: self.send_at("AT+ROLE=S"),
             "hc_query_role", "查询主从模式", lambda: self.send_at("AT+ROLE=?")),
        ]
        for r, (lk, ld, lc, rk, rd, rc) in enumerate(rows):
            b1 = self._reg(QPushButton(), lk, ld)
            b1.clicked.connect(lc)
            b2 = self._reg(QPushButton(), rk, rd)
            b2.clicked.connect(rc)
            grid.addWidget(b1, r, 0)
            grid.addWidget(b2, r, 1)
        layout.addLayout(grid)

        form = QFormLayout()
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(self.BAUDRATES)
        self.baud_combo.setCurrentText(str(self.app.serial_manager.serial_params.get("baudrate", 9600)))
        baud_row = QHBoxLayout()
        baud_row.addWidget(self.baud_combo)
        b_set_baud = self._reg(QPushButton(), "hc_set", "设置")
        b_set_baud.clicked.connect(self.set_baud)
        baud_row.addWidget(b_set_baud)
        form.addRow(self._reg(QLabel(), "hc_baud_setting", "波特率设置"), baud_row)

        self.name_20_edit = QLineEdit()
        form.addRow(self._reg(QLabel(), "hc_name_20", "2.0 蓝牙名称"),
                    self._entry_with_button(self.name_20_edit,
                                            lambda: self.set_name(self.name_20_edit.text(), "AT+NAME=")))
        self.name_ble_edit = QLineEdit()
        form.addRow(self._reg(QLabel(), "hc_name_ble", "BLE 蓝牙名称"),
                    self._entry_with_button(self.name_ble_edit,
                                            lambda: self.set_name(self.name_ble_edit.text(), "AT+BNAME=")))
        self.pin_edit = QLineEdit()
        form.addRow(self._reg(QLabel(), "hc_pin", "2.0 配对密码"),
                    self._entry_with_button(self.pin_edit,
                                            lambda: self.set_name(self.pin_edit.text(), "AT+PIN=")))
        layout.addLayout(form)

        tip = self._reg(QLabel(), "hc_send_format_tip",
                        "发送格式：ASCII + 回车换行（CRLF）。危险操作会先弹出确认。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #666;")
        layout.addWidget(tip)
        layout.addStretch(1)

    def _entry_with_button(self, entry, callback):
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(entry)
        btn = self._reg(QPushButton(), "hc_set", "设置")
        btn.clicked.connect(callback)
        row.addWidget(btn)
        return w

    def _ensure_open(self):
        if not self.app.serial_manager.is_open:
            QMessageBox.warning(self, self._t("hc_port_not_open", "串口未打开"),
                                self._t("hc_open_port_first", "请先在主窗口打开串口。"))
            return False
        return True

    def send_at(self, command):
        if not self._ensure_open():
            return False
        data = (command.rstrip("\r\n") + "\r\n").encode("ascii", errors="replace")
        if not self.app.serial_manager.write(data):
            QMessageBox.critical(self, self._t("send_fail", "发送失败"), command)
            return False
        self.status_label.setText(self._t("hc_sent_prefix", "已发送：{}").format(command))
        self.app.update_status()
        self.app.log_data(self._t("dir_send", "发送"), data)
        return True

    def confirm_send(self, command):
        if QMessageBox.question(self, self._t("hc_confirm_title", "确认"),
                                self._t("hc_confirm_factory", "确定发送恢复出厂设置指令？")) \
                == QMessageBox.StandardButton.Yes:
            self.send_at(command)

    def set_led(self, on):
        self.led_on = bool(on)
        self.send_at("AT+LED=1" if on else "AT+LED=0")

    def query_baud(self):
        if self._searching or not self._ensure_open():
            return
        self._searching = True
        self.status_label.setText(self._t("hc_searching_baud", "正在自动搜索波特率…"))
        threading.Thread(target=self._baud_search_worker, daemon=True).start()

    def _baud_search_worker(self):
        original = self.app.serial_manager.serial_params.get("baudrate", 9600)
        found = None
        try:
            for value in self.BAUDRATES:
                if not self.app.serial_manager.is_open:
                    break
                rate = int(value)
                ok, _msg = self.app.serial_manager.update_params({"baudrate": rate})
                if not ok:
                    continue
                self._last_response = b""
                self._response_event.clear()
                self.app.serial_manager.write(b"AT\r\n")
                if self._response_event.wait(0.25):
                    if b"OK" in self._last_response.upper():
                        found = rate
                        break
        finally:
            self._searching = False
            self._search_done.emit(found, original)

    def _apply_search_result(self, found, original):
        if found:
            self.baud_combo.setCurrentText(str(found))
            self.app.baudrate.setCurrentText(str(found))
            self.status_label.setText(self._t("hc_baud_found", "已找到波特率：{}").format(found))
        else:
            # 搜索失败时恢复搜索前的波特率，避免串口停留在最后尝试值与界面不一致
            self.app.serial_manager.update_params({"baudrate": original})
            self.app.baudrate.setCurrentText(str(original))
            self.status_label.setText(self._t("hc_baud_not_found", "未收到响应，请检查模块模式和串口参数"))
        self.app.update_status_param()

    def set_baud(self):
        value = self.baud_combo.currentText().strip()
        if value.isdigit() and self.send_at("AT+BAUD=" + value):
            def _apply():
                self.app.serial_manager.update_params({"baudrate": int(value)})
                self.app.baudrate.setCurrentText(value)
                self.app.update_status_param()
            QTimer.singleShot(250, _apply)

    def set_name(self, value, prefix):
        value = value.strip()
        if not value:
            QMessageBox.warning(self, self._t("hc_input_empty", "输入为空"),
                                self._t("hc_input_empty_tip", "请输入要设置的内容。"))
            return
        if len(value) > 32:
            QMessageBox.warning(self, self._t("hc_input_too_long", "输入过长"),
                                self._t("hc_input_too_long_tip", "名称或密码不能超过 32 个字符。"))
            return
        self.send_at(prefix + value)

    def handle_response(self, data_bytes):
        data = bytes(data_bytes)
        self._last_response = data
        self._response_event.set()
        text = data.decode("ascii", errors="replace").replace("\r", "\\r").replace("\n", "\\n")
        self.response_label.setText(self._t("hc_response_prefix", "响应：{}").format(text))
        upper = data.upper()
        if b"OK+LED=0" in upper:
            self.led_on = False
        elif b"OK+LED=1" in upper:
            self.led_on = True
        if b"OK+BAUD=" in upper:
            try:
                rate = int(upper.split(b"OK+BAUD=", 1)[1].split()[0])
                self.baud_combo.setCurrentText(str(rate))
            except (ValueError, IndexError):
                pass
