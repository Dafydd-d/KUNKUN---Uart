"""Qt UI 冒烟测试：QT_QPA_PLATFORM=offscreen python test_qt_smoke.py"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

# 防止冒烟测试污染真实配置 / 弹阻塞对话框
import qt_app
import qt_panels

qt_app.MainWindow.save_lang_config = lambda self: None
qt_app.MainWindow.save_display_config = lambda self: None
qt_app.MainWindow.save_serial_config = lambda self: None
qt_app.MainWindow.save_send_area_cache = lambda self: None
qt_panels.MultiSendPanel.save_cache = lambda self: None
qt_panels.AutoAnswerPanel.save_cache = lambda self: None
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)

app = QApplication(sys.argv)
win = qt_app.MainWindow()
win.show()

# 语言切换（覆盖菜单重建 / 全部文案刷新）
win.switch_lang("en")
win.switch_lang("zh")

# 查找栏
win.open_find_bar()
win.find_edit.setText("AA")
win.find_highlight_all()
win.find_next()
win.find_prev()
win.close_find_bar()

# 发送历史
win.txt_manual_send.setPlainText("")
win._push_send_history("AA BB")
assert win.history_prev()
assert win.txt_manual_send.toPlainText() == "AA BB"
assert win.history_next()
assert win.txt_manual_send.toPlainText() != "AA BB"

# 文本编码参数
win.hex_send.setChecked(False)
win.text_encoding.setCurrentText("UTF-8")
assert win.process_send_data("测") == "测".encode("utf-8")

# 面板切换
win.toggle_akson_panel()
assert win.akson_scroll.isVisible()
# 面板文案完整性（防止构建后未刷新文案导致标签/按钮空白）
assert win.akson.btn_parse.text().strip(), "埃克森解析按钮无文案"
assert win.akson.count_label.text().strip(), "埃克森数量标签无文案"
assert win.akson.btn_build.text().strip() and win.akson.btn_send.text().strip(), "埃克森生成/发送按钮无文案"
assert win.akson.txt_custom.toPlainText().strip(), "埃克森自定义数据区无占位提示"
win.toggle_multi_panel()
assert win.multi_panel.isVisible() and not win.akson_scroll.isVisible()
win.multi_panel.add_item(content="11 22")
win.multi_panel.delete_row(win.multi_panel.table.rowCount() - 1)
win.toggle_auto_answer_panel()
assert win.auto_answer.isVisible() and not win.multi_panel.isVisible()

# 模拟接收数据（日志管线 + 自动应答匹配）
win.auto_answer.answer_enabled = True
win.auto_answer.table.cellWidget(0, win.auto_answer.COL_SEL).setChecked(True)
win.auto_answer.table.cellWidget(0, win.auto_answer.COL_RECV).setText("01 02")
win.auto_answer.table.cellWidget(0, win.auto_answer.COL_ANSWER).setText("03 04")
win._on_frame(b"\x01\x02")
QTest.qWait(120)  # 等 50ms 批量刷新定时器触发
assert "01 02" in win.log_text.toPlainText()

# HC 蓝牙配置（嵌入主窗口右侧面板）
win.toggle_hc_panel()
assert win.hc_panel.isVisible()
assert win.hc_panel.title().strip(), "HC 面板标题为空"
assert win.hc_panel.status_label.text().strip(), "HC 面板状态文案为空"
win.hc_panel.refresh_lang()
assert win.hc_panel.title().strip()

# 埃克森解析/生成帧
win.toggle_akson_panel()
lines = ["05"] + [f"{i:02X} {i+1:02X} {i+2:02X}" for i in range(10)]
win.akson.txt_custom.setPlainText("\n".join(lines))
win.akson.parse_and_fill()
win.akson.build_frame()
assert len(win.akson.frame_bytes) == 33, len(win.akson.frame_bytes)

# 缓冲裁剪
win.buffer_limit = 1000
win._trim_log_buffer()
assert win.log_text.document().characterCount() < 2000

win.close()
app.processEvents()
print("qt smoke OK")
