"""Qt 版功能面板：埃克森协议、多字符串发送、数据自动应答。"""
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTextEdit, QVBoxLayout, QWidget,
)

from serial_core import build_payload, parse_hex_bytes
from conf_io import load_json, save_json
from app_paths import config_path
from lang import LANG_DICT

MULTI_SEND_CACHE = config_path("multi_send_cache.json")
AUTO_ANSWER_CACHE = config_path("auto_answer_cache.json")

# 仿 SSCOM：数据/延时用等宽字体，HEX 报文一眼可读
MONO_FONT = QFont("Consolas", 9)
ROW_HEIGHT = 24


class SendCell(QPushButton):
    """SSCOM 风格的注释发送格：显示注释，单击发送该行，双击编辑注释。

    单击经 200ms 消歧延迟触发（等双击），键盘 Enter/Space 立即发送。
    """

    send_requested = pyqtSignal()
    edit_requested = pyqtSignal()

    def __init__(self, comment, placeholder="发送", hint=""):
        super().__init__()
        self._comment = comment
        self._placeholder = placeholder
        self._hint = hint
        self._ignore_release = False
        # 按钮文字默认居中，超宽时开头也会被裁掉；左对齐保证注释从头显示
        self.setStyleSheet("text-align: left; padding-left: 6px;")
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self.send_requested.emit)
        self.clicked.connect(self.send_requested.emit)  # 键盘激活路径
        self.setFixedHeight(ROW_HEIGHT)
        self._update_text()

    def comment(self):
        return self._comment

    def set_comment(self, text):
        self._comment = text
        self._update_text()

    def set_hint(self, hint):
        self._hint = hint
        self._update_text()

    def _update_text(self):
        self.setText(self._comment.strip() if self._comment.strip() else self._placeholder)
        tip = self._comment.strip() if self._comment.strip() else ""
        if self._hint:
            tip = f"{tip}\n{self._hint}" if tip else self._hint
        self.setToolTip(tip)

    def mousePressEvent(self, event):
        self.setDown(True)

    def mouseReleaseEvent(self, event):
        self.setDown(False)
        if self._ignore_release:
            self._ignore_release = False
            return
        if self.rect().contains(event.position().toPoint()):
            self._timer.start()

    def mouseDoubleClickEvent(self, event):
        self._timer.stop()
        self._ignore_release = True
        self.setDown(False)
        self.edit_requested.emit()


def _is_hex_byte(value):
    try:
        return len(value) == 2 and 0 <= int(value, 16) <= 0xFF
    except (TypeError, ValueError):
        return False


class AksonPanel(QGroupBox):
    """埃克森数据配置面板：11 行粘贴解析 / 表格编辑 / 生成帧并发送。"""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.frame_bytes = []
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)

        # 自定义数据区
        custom_box = QGroupBox()
        self.custom_box = custom_box
        cv = QVBoxLayout(custom_box)
        self.txt_custom = QTextEdit()
        self.txt_custom.setMaximumHeight(90)
        self.txt_custom.setAcceptRichText(False)
        cv.addWidget(self.txt_custom)
        self.btn_parse = QPushButton()
        self.btn_parse.clicked.connect(self.parse_and_fill)
        cv.addWidget(self.btn_parse)
        outer.addWidget(custom_box)

        # 数据配置与发送（表格）
        table_box = QGroupBox()
        self.table_box = table_box
        tv = QVBoxLayout(table_box)
        count_row = QHBoxLayout()
        self.count_label = QLabel()
        count_row.addWidget(self.count_label)
        self.entry_count = QLineEdit("00")
        self.entry_count.setMaxLength(2)
        self.entry_count.setMaximumWidth(80)
        count_row.addWidget(self.entry_count)
        count_row.addStretch(1)
        tv.addLayout(count_row)

        header_row = QHBoxLayout()
        self.header_labels = []
        for _ in range(4):
            lb = QLabel()
            lb.setMinimumWidth(70)
            self.header_labels.append(lb)
            header_row.addWidget(lb)
        header_row.addStretch(1)
        tv.addLayout(header_row)

        self.entries = []
        self.group_labels = []
        for row in range(10):
            row_layout = QHBoxLayout()
            lb = QLabel()
            lb.setMinimumWidth(70)
            self.group_labels.append(lb)
            row_layout.addWidget(lb)
            row_entries = []
            for _ in range(3):
                ent = QLineEdit("00")
                ent.setMaxLength(2)
                ent.setMaximumWidth(90)
                row_entries.append(ent)
                row_layout.addWidget(ent)
            self.entries.append(row_entries)
            row_layout.addStretch(1)
            tv.addLayout(row_layout)
        outer.addWidget(table_box)
        preview_box = QGroupBox()
        self.preview_box = preview_box
        pv = QVBoxLayout(preview_box)
        self.txt_frame = QTextEdit()
        self.txt_frame.setReadOnly(True)
        self.txt_frame.setMaximumHeight(140)
        self.txt_frame.setAcceptRichText(False)
        pv.addWidget(self.txt_frame)
        btn_row = QHBoxLayout()
        self.btn_build = QPushButton()
        self.btn_build.clicked.connect(self.build_frame)
        btn_row.addWidget(self.btn_build)
        btn_row.addStretch(1)
        self.btn_send = QPushButton()
        self.btn_send.clicked.connect(self.send_frame)
        btn_row.addWidget(self.btn_send)
        pv.addLayout(btn_row)
        outer.addWidget(preview_box)
        outer.addStretch(1)
        # 建完立即填充文案与占位提示（否则标签/按钮全为空白）
        self.refresh_lang()
        self.reset_custom_tip()

    def refresh_lang(self):
        lg = self.app.lang
        self.setTitle(lg.get("akson_panel_title", "埃克森数据配置面板"))
        self.custom_box.setTitle(lg.get("custom_data_title", "自定义数据"))
        self.btn_parse.setText(lg.get("parse_fill", "解析并填充"))
        self.table_box.setTitle(lg.get("data_config_send", "数据配置与发送"))
        self.count_label.setText(lg.get("count", "数量"))
        self.preview_box.setTitle(lg.get("full_hex_frame", "完整HEX帧"))
        self.btn_build.setText(lg.get("generate_frame", "生成帧"))
        self.btn_send.setText(lg.get("send", "发送"))
        for i, key in enumerate(["seq", "soc", "volt_int", "volt_dec"]):
            self.header_labels[i].setText(lg.get(key, key))
        for row in range(10):
            self.group_labels[row].setText(lg.get("group_n", "第{}组").format(row + 1))

    def reset_custom_tip(self):
        """语言切换后重置占位提示语（保留用户粘贴的数据）。"""
        tips = {d.get("custom_data_tip", "") for d in LANG_DICT.values()}
        current = self.txt_custom.toPlainText().strip()
        if not current or current in tips:
            self.txt_custom.setPlainText(self.app.lang.get("custom_data_tip", "粘贴11行数据"))

    def parse_and_fill(self):
        lg = self.app.lang
        text = self.txt_custom.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, lg.get("warn", "警告"), lg.get("paste_first", "请先粘贴数据"))
            return
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) != 11:
            QMessageBox.critical(self, lg.get("format_error", "格式错误"),
                                 lg.get("line_count_error", "数据行数必须为11行"))
            return
        if not _is_hex_byte(lines[0]):
            QMessageBox.critical(self, lg.get("format_error", "格式错误"),
                                 lg.get("akson_count_tip", "数量必须为 00~FE 的单字节 HEX 值"))
            return
        self.entry_count.setText(lines[0])
        for row in range(10):
            parts = lines[row + 1].split()
            if len(parts) != 3:
                QMessageBox.critical(self, lg.get("format_error", "格式错误"),
                                     lg.get("line_col_error", "第{}行格式错误").format(row + 2))
                return
            if not all(_is_hex_byte(v) for v in parts):
                QMessageBox.critical(self, lg.get("format_error", "格式错误"),
                                     lg.get("akson_row_hex_tip", "第{}行必须包含 3 个 00~FF 的单字节 HEX 值").format(row + 2))
                return
            for col in range(3):
                self.entries[row][col].setText(parts[col])
        QMessageBox.information(self, lg.get("success", "成功"), lg.get("parse_success", "解析成功"))

    def build_frame(self):
        lg = self.app.lang
        try:
            count_str = self.entry_count.text().strip()
            if not _is_hex_byte(count_str) or int(count_str, 16) > 0xFE:
                raise ValueError("invalid count")
            frame = [int(count_str, 16) + 1]
            for row in range(10):
                values = [e.text().strip() for e in self.entries[row]]
                if not all(_is_hex_byte(v) for v in values):
                    raise ValueError("invalid byte")
                frame.extend(int(v, 16) for v in values)
            frame.append(0x00)
            frame.append(sum(frame) & 0xFF)
        except (TypeError, ValueError):
            QMessageBox.critical(self, lg.get("input_error", "输入错误"),
                                 lg.get("akson_build_tip", "数量须为 00~FE；每个数据字段须为 00~FF 的两位 HEX 值"))
            return
        self.frame_bytes = frame
        hex_parts = [f"{b:02X}" for b in frame]
        lines = [" ".join(hex_parts[i:i + 10]) for i in range(0, len(hex_parts), 10)]
        self.txt_frame.setPlainText("\n".join(lines))

    def send_frame(self):
        lg = self.app.lang
        if not self.app.serial_manager.is_open:
            QMessageBox.warning(self, lg.get("warn", "警告"), lg.get("open_serial_first", "请先打开串口"))
            return
        if not self.frame_bytes:
            QMessageBox.warning(self, lg.get("warn", "警告"), lg.get("no_frame_tip", "请先生成帧数据"))
            return
        send_bytes = bytes(self.frame_bytes)
        if self.app.serial_manager.write(send_bytes):
            self.app.update_status()
            self.app.log_data(lg.get("dir_send", "发送"), send_bytes)


class MultiSendPanel(QGroupBox):
    """多字符串发送：表格行式条目 + 循环发送。列：选中→HEX→内容→注释→延时→操作。"""

    COL_SEL, COL_HEX, COL_CONTENT, COL_COMMENT, COL_DELAY, COL_OP = range(6)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.cycle_running = False
        self._cycle_token = 0
        self._build()
        self.load_cache()

    def _build(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.btn_add = QPushButton()
        self.btn_add.clicked.connect(lambda: self.add_item())
        top.addWidget(self.btn_add)
        self.btn_del = QPushButton()
        self.btn_del.clicked.connect(self.del_selected)
        top.addWidget(self.btn_del)
        self.btn_cycle = QPushButton()
        self.btn_cycle.clicked.connect(self.toggle_cycle_send)
        top.addWidget(self.btn_cycle)
        self.sel_all = QCheckBox()
        self.sel_all.setChecked(False)
        self.sel_all.stateChanged.connect(self.toggle_select_all)
        top.addWidget(self.sel_all)
        self.hint_label = QLabel()
        self.hint_label.setStyleSheet("color: #555;")
        top.addWidget(self.hint_label, 1)
        layout.addLayout(top)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["循环", "HEX", "", "", "", ""])
        header = self.table.horizontalHeader()
        # 仿 SSCOM 列布局：复选框窄、发送内容占主宽（等宽字体），
        # 注释定宽，延时随内容，“点击发送”操作格醒目
        header.setSectionResizeMode(self.COL_SEL, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.COL_HEX, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.COL_CONTENT, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_COMMENT, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(self.COL_DELAY, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_OP, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(self.COL_SEL, 36)
        self.table.setColumnWidth(self.COL_HEX, 40)
        self.table.setColumnWidth(self.COL_COMMENT, 170)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT + 6)
        layout.addWidget(self.table)
        self.refresh_lang()

    def refresh_lang(self):
        lg = self.app.lang
        self.setTitle(lg.get("multi_send_title", "多字符串发送面板"))
        self.btn_add.setText(lg.get("add_item", "添加条目"))
        self.btn_del.setText(lg.get("del_selected", "删除选中"))
        self.btn_cycle.setText(lg.get("stop_cycle", "停止循环") if self.cycle_running else lg.get("cycle_send", "循环发送"))
        self.sel_all.setText("全选")
        self.hint_label.setText(lg.get("multi_send_hint", "勾选的条目会按延时顺序循环发送；单击“发送”可立即发送该行。"))
        self.table.setHorizontalHeaderLabels(
            ["循环", "HEX", lg.get("col_content", "发送内容"), lg.get("col_send_comment", "点击发送（双击编辑注释）"),
             lg.get("col_delay", "延时(ms)"), ""])
        for row in range(self.table.rowCount()):
            cell = self.table.cellWidget(row, self.COL_COMMENT)
            if cell:
                cell.set_hint(lg.get("col_send_comment", "点击发送（双击编辑注释）"))
            btn_del = self.table.cellWidget(row, self.COL_OP)
            if btn_del:
                btn_del.setToolTip(lg.get("col_del", "删除"))

    def add_item(self, selected=True, hex_val=True, content="", comment="", delay="1000"):
        row = self.table.rowCount()
        self.table.insertRow(row)
        cb_sel = QCheckBox()
        cb_sel.setChecked(selected)
        cb_sel.setStyleSheet("margin-left: 8px;")
        self.table.setCellWidget(row, self.COL_SEL, cb_sel)
        cb_hex = QCheckBox()
        cb_hex.setChecked(hex_val)
        cb_hex.setStyleSheet("margin-left: 8px;")
        self.table.setCellWidget(row, self.COL_HEX, cb_hex)

        entry_content = QLineEdit(content)
        entry_content.setFont(MONO_FONT)
        entry_content.setFixedHeight(ROW_HEIGHT)
        # 光标归零：否则长报文会被 QLineEdit 滚动到末尾显示，看不到开头字节
        entry_content.setCursorPosition(0)
        self.table.setCellWidget(row, self.COL_CONTENT, entry_content)

        # 注释格 = SSCOM 式发送大格子：单击发送，双击编辑注释
        cell_comment = SendCell(comment, self.app.lang.get("col_send", "发送"),
                                self.app.lang.get("col_send_comment", "点击发送（双击编辑注释）"))
        cell_comment.send_requested.connect(lambda r=row: self.send_single_item(r))
        cell_comment.edit_requested.connect(lambda r=row: self.edit_comment(r))
        self.table.setCellWidget(row, self.COL_COMMENT, cell_comment)

        entry_delay = QLineEdit(str(delay))
        entry_delay.setFont(MONO_FONT)
        entry_delay.setFixedHeight(ROW_HEIGHT)
        entry_delay.setMaximumWidth(90)
        entry_delay.setCursorPosition(0)
        self.table.setCellWidget(row, self.COL_DELAY, entry_delay)

        # 操作格只留小叉删除（发送已并入注释格）
        btn_del = QPushButton("✕")
        btn_del.setFixedSize(26, ROW_HEIGHT)
        btn_del.setToolTip(self.app.lang.get("col_del", "删除"))
        btn_del.clicked.connect(lambda _, r=row: self.delete_row(r))
        self.table.setCellWidget(row, self.COL_OP, btn_del)

    def edit_comment(self, row):
        cell = self.table.cellWidget(row, self.COL_COMMENT)
        if not cell:
            return
        lg = self.app.lang
        text, ok = QInputDialog.getText(self, lg.get("col_comment", "注释"),
                                        lg.get("col_comment", "注释"),
                                        QLineEdit.EchoMode.Normal, cell.comment())
        if ok:
            cell.set_comment(text)

    def _row_values(self, row):
        cb_sel = self.table.cellWidget(row, self.COL_SEL)
        cb_hex = self.table.cellWidget(row, self.COL_HEX)
        entry_content = self.table.cellWidget(row, self.COL_CONTENT)
        entry_delay = self.table.cellWidget(row, self.COL_DELAY)
        cell_comment = self.table.cellWidget(row, self.COL_COMMENT)
        if not all((cb_sel, cb_hex, entry_content, entry_delay, cell_comment)):
            return None
        return (cb_sel.isChecked(), cb_hex.isChecked(), entry_content.text(),
                entry_delay.text(), cell_comment.comment())

    def toggle_select_all(self):
        state = self.sel_all.isChecked()
        for row in range(self.table.rowCount()):
            cb = self.table.cellWidget(row, self.COL_SEL)
            if cb:
                cb.setChecked(state)

    def delete_row(self, row):
        self.table.removeRow(row)

    def del_selected(self):
        to_del = [r for r in range(self.table.rowCount())
                  if (cb := self.table.cellWidget(r, self.COL_SEL)) and cb.isChecked()]
        for r in reversed(to_del):
            self.table.removeRow(r)
        self.sel_all.setChecked(False)

    def send_single_item(self, row):
        values = self._row_values(row)
        if not values or not self.app.serial_manager.is_open:
            return
        selected, hex_val, content, _delay, _comment = values
        content = content.strip()
        if not content:
            return
        try:
            send_bytes = build_payload(
                content,
                hex_mode=hex_val,
                add_crlf=self.app.add_crlf.isChecked(),
                checksum=self.app.checksum_type.currentText() if self.app.add_checksum.isChecked() else None,
                encoding=self.app.text_encoding.currentText(),
            )
        except ValueError:
            return
        if not send_bytes:
            return
        if self.app.serial_manager.write(send_bytes):
            self.app.update_status()
            self.app.log_data(self.app.lang.get("dir_send", "发送"), send_bytes)

    def toggle_cycle_send(self):
        if self.cycle_running:
            self.stop_cycle_send()
        else:
            self.start_cycle_send()

    def start_cycle_send(self):
        if not self.app.serial_manager.is_open or self.cycle_running:
            return
        if not any((v := self._row_values(r)) and v[0] and v[2].strip()
                   for r in range(self.table.rowCount())):
            QMessageBox.warning(self, self.app.lang.get("warn", "提醒"),
                                self.app.lang.get("cycle_no_item", "请至少勾选一行非空的发送内容"))
            return
        self.cycle_running = True
        self._cycle_token += 1
        self.btn_cycle.setText(self.app.lang.get("stop_cycle", "停止循环"))
        QTimer.singleShot(0, lambda t=self._cycle_token: self._cycle_step(t, 0))

    def stop_cycle_send(self):
        self.cycle_running = False
        self._cycle_token += 1
        self.btn_cycle.setText(self.app.lang.get("cycle_send", "循环发送"))

    def _cycle_step(self, token, idx):
        if token != self._cycle_token or not self.cycle_running:
            return
        if not self.app.serial_manager.is_open:
            self.stop_cycle_send()
            return
        picked = []
        for r in range(self.table.rowCount()):
            v = self._row_values(r)
            if v and v[0] and v[2].strip():
                picked.append((r, v[3]))
        if not picked:
            QTimer.singleShot(100, lambda: self._cycle_step(token, 0))
            return
        row, delay_text = picked[idx % len(picked)]
        self.send_single_item(row)
        try:
            delay = max(10, int(delay_text))
        except ValueError:
            delay = 1000
        QTimer.singleShot(delay, lambda: self._cycle_step(token, (idx + 1) % len(picked)))

    def load_cache(self):
        data = load_json(MULTI_SEND_CACHE)
        if not isinstance(data, list) or len(data) == 0:
            self.add_item(comment="示例：循环报文 1")
            self.add_item(comment="示例：循环报文 2")
            return
        for item in data:
            self.add_item(
                selected=item.get("selected", False),
                hex_val=item.get("hex", False),
                content=item.get("content", ""),
                comment=item.get("comment", ""),
                delay=str(item.get("delay", 1000)),
            )

    def save_cache(self):
        data = []
        for row in range(self.table.rowCount()):
            v = self._row_values(row)
            if not v:
                continue
            selected, hex_val, content, delay, comment = v
            data.append({
                "selected": selected,
                "hex": hex_val,
                "content": content,
                "comment": comment,
                "delay": int(delay) if delay.isdigit() else 1000,
            })
        save_json(MULTI_SEND_CACHE, data)


class AutoAnswerPanel(QGroupBox):
    """数据自动应答：规则表 + 总开关；隐藏时仍可后台工作。"""

    COL_SEL, COL_RECV, COL_ANSWER, COL_DELAY, COL_OP = range(5)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.answer_enabled = False
        self._parse_cache = {}  # 规则文本 -> bytes/None
        self._build()
        self.load_cache()

    def _build(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.btn_add = QPushButton()
        self.btn_add.clicked.connect(lambda: self.add_item())
        top.addWidget(self.btn_add)
        self.btn_del = QPushButton()
        self.btn_del.clicked.connect(self.del_selected)
        top.addWidget(self.btn_del)
        self.btn_toggle = QPushButton()
        self.btn_toggle.clicked.connect(self.toggle_answer)
        top.addWidget(self.btn_toggle)
        self.status_label = QLabel()
        top.addWidget(self.status_label)
        top.addStretch(1)
        layout.addLayout(top)

        self.hint_label = QLabel()
        self.hint_label.setStyleSheet("color: #555;")
        layout.addWidget(self.hint_label)

        self.table = QTableWidget(0, 5)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_SEL, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.COL_RECV, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_ANSWER, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_DELAY, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_OP, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(self.COL_SEL, 36)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT + 6)
        layout.addWidget(self.table)
        self.refresh_lang()

    def refresh_lang(self):
        lg = self.app.lang
        self.setTitle(lg.get("auto_answer_title", "自动应答配置面板"))
        self.btn_add.setText(lg.get("add_answer_item", "添加应答"))
        self.btn_del.setText(lg.get("del_answer_item", "删除选中"))
        self.btn_toggle.setText(lg.get("aa_disable", "关闭应答") if self.answer_enabled else lg.get("aa_enable", "开启应答"))
        self.status_label.setText(lg.get("aa_status_on", "状态：应答中") if self.answer_enabled else lg.get("aa_status_off", "状态：未启用"))
        self.hint_label.setText(lg.get("aa_hint", "勾选规则：收到匹配数据后，按设定延时自动回发 HEX 应答。"))
        self.table.setHorizontalHeaderLabels(
            ["启用", lg.get("col_recv_data", "接收数据(HEX)"), lg.get("col_answer_data", "应答数据(HEX)"),
             lg.get("col_delay_ms", "延时(ms)"), lg.get("col_del", "删除")])

    def toggle_answer(self):
        self.answer_enabled = not self.answer_enabled
        self.refresh_lang()

    def add_item(self, selected=True, recv_data="", answer_data="", delay="100"):
        row = self.table.rowCount()
        self.table.insertRow(row)
        cb = QCheckBox()
        cb.setChecked(selected)
        cb.setStyleSheet("margin-left: 8px;")
        self.table.setCellWidget(row, self.COL_SEL, cb)
        entry_recv = QLineEdit(recv_data)
        entry_recv.setFont(MONO_FONT)
        entry_recv.setFixedHeight(ROW_HEIGHT)
        entry_recv.setCursorPosition(0)
        self.table.setCellWidget(row, self.COL_RECV, entry_recv)
        entry_answer = QLineEdit(answer_data)
        entry_answer.setFont(MONO_FONT)
        entry_answer.setFixedHeight(ROW_HEIGHT)
        entry_answer.setCursorPosition(0)
        self.table.setCellWidget(row, self.COL_ANSWER, entry_answer)
        entry_delay = QLineEdit(str(delay))
        entry_delay.setFont(MONO_FONT)
        entry_delay.setFixedHeight(ROW_HEIGHT)
        entry_delay.setMaximumWidth(90)
        entry_delay.setCursorPosition(0)
        self.table.setCellWidget(row, self.COL_DELAY, entry_delay)
        btn_del = QPushButton("✕")
        btn_del.setFixedSize(26, ROW_HEIGHT)
        btn_del.setToolTip(self.app.lang.get("col_del", "删除"))
        btn_del.clicked.connect(lambda _, r=row: self.delete_row(r))
        self.table.setCellWidget(row, self.COL_OP, btn_del)

    def delete_row(self, row):
        self.table.removeRow(row)

    def del_selected(self):
        to_del = [r for r in range(self.table.rowCount())
                  if (cb := self.table.cellWidget(r, self.COL_SEL)) and cb.isChecked()]
        for r in reversed(to_del):
            self.table.removeRow(r)

    def match_and_answer(self, recv_bytes):
        """匹配接收数据并自动应答，仅勾选且开启应答的规则生效。"""
        if not self.answer_enabled or not self.app.serial_manager.is_open:
            return
        recv = bytes(recv_bytes)
        for row in range(self.table.rowCount()):
            cb = self.table.cellWidget(row, self.COL_SEL)
            entry_recv = self.table.cellWidget(row, self.COL_RECV)
            entry_answer = self.table.cellWidget(row, self.COL_ANSWER)
            entry_delay = self.table.cellWidget(row, self.COL_DELAY)
            if not all((cb, entry_recv, entry_answer, entry_delay)) or not cb.isChecked():
                continue
            target_recv = entry_recv.text().strip()
            answer_hex = entry_answer.text().strip()
            if not target_recv or not answer_hex:
                continue
            # 规则文本未变化时复用上次解析结果
            if target_recv not in self._parse_cache:
                try:
                    self._parse_cache[target_recv] = parse_hex_bytes(target_recv)
                except ValueError:
                    self._parse_cache[target_recv] = None
            target_bytes = self._parse_cache[target_recv]
            if not target_bytes or recv.find(target_bytes) == -1:
                continue
            try:
                delay_ms = max(0, int(entry_delay.text()))
            except ValueError:
                delay_ms = 100
            QTimer.singleShot(delay_ms, lambda a=answer_hex: self._send_answer(a))
            break

    def _send_answer(self, answer_hex):
        if not self.app.serial_manager.is_open:
            return
        try:
            send_bytes = build_payload(
                answer_hex,
                hex_mode=True,
                add_crlf=self.app.add_crlf.isChecked(),
                checksum=self.app.checksum_type.currentText() if self.app.add_checksum.isChecked() else None,
                encoding=self.app.text_encoding.currentText(),
            )
        except ValueError:
            return
        if not send_bytes:
            return
        if self.app.serial_manager.write(send_bytes):
            self.app.update_status()
            self.app.log_data(self.app.lang.get("dir_send", "发送"), send_bytes)

    def load_cache(self):
        data = load_json(AUTO_ANSWER_CACHE)
        if not isinstance(data, list) or len(data) == 0:
            self.add_item(delay="100")
            self.add_item(delay="100")
            return
        for item in data:
            self.add_item(
                selected=item.get("selected", False),
                recv_data=item.get("recv_data", ""),
                answer_data=item.get("answer_data", ""),
                delay=str(item.get("delay", 100)),
            )

    def save_cache(self):
        data = []
        for row in range(self.table.rowCount()):
            cb = self.table.cellWidget(row, self.COL_SEL)
            entry_recv = self.table.cellWidget(row, self.COL_RECV)
            entry_answer = self.table.cellWidget(row, self.COL_ANSWER)
            entry_delay = self.table.cellWidget(row, self.COL_DELAY)
            if not all((cb, entry_recv, entry_answer, entry_delay)):
                continue
            delay = entry_delay.text()
            data.append({
                "selected": cb.isChecked(),
                "recv_data": entry_recv.text(),
                "answer_data": entry_answer.text(),
                "delay": int(delay) if delay.isdigit() else 100,
            })
        save_json(AUTO_ANSWER_CACHE, data)
