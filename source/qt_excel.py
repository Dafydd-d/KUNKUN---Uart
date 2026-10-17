"""Qt 版 Excel 导出：后台线程写盘 + 进度对话框。"""
import time

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import QFileDialog, QMessageBox, QProgressDialog

import excel_core


class _ExportWorker(QThread):
    done = pyqtSignal(bool, str)  # (成功, 提示) / (失败, 消息)

    def __init__(self, frames, file_path, labels):
        super().__init__()
        self.frames = frames
        self.file_path = file_path
        self.labels = labels

    def run(self):
        try:
            excel_core.export_frames(self.frames, self.file_path, self.labels)
        except ImportError:
            self.done.emit(False, self.labels["excel_missing_tip"])
        except Exception as e:
            self.done.emit(False, str(e))
        else:
            self.done.emit(True, self.labels["save_success_tip"].format(self.file_path))


def start_export(app, data_frames):
    """主入口：导出收发数据为 Excel（后台线程执行，界面不卡顿）。"""
    lg = app.lang
    if not data_frames:
        QMessageBox.warning(app, lg["tip"], lg["no_data_to_save"])
        return
    if app._exporting:
        QMessageBox.information(app, lg["tip"], lg["exporting_excel"])
        return
    file_path, _ = QFileDialog.getSaveFileName(
        app, lg["save_excel"], f"串口数据_{time.strftime('%Y%m%d_%H%M%S')}.xlsx",
        "Excel文件 (*.xlsx)")
    if not file_path:
        return

    labels = {
        "sheet_name": lg["excel_sheet_name"],
        "col_timestamp": lg["col_timestamp"],
        "col_direction": lg["col_direction"],
        "byte_label": lg["byte_label"],
        "excel_missing_tip": lg["excel_missing_tip"],
        "save_success_tip": lg["save_success_tip"],
    }
    progress = QProgressDialog(lg["exporting_excel"], None, 0, 0, app)
    progress.setWindowTitle(lg["export_excel_title"])
    progress.setWindowModality(Qt.WindowModality.ApplicationModal)
    progress.setMinimumDuration(0)
    progress.show()

    app._exporting = True
    worker = _ExportWorker(data_frames, file_path, labels)
    app._export_worker = worker  # 防止提前回收

    def _done(ok, message):
        app._exporting = False
        progress.close()
        if ok:
            QMessageBox.information(app, lg["success"], message)
        else:
            QMessageBox.critical(app, lg["save_fail"], message)

    worker.done.connect(_done)
    worker.start()
