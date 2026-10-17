import os
import sys


def get_app_dir():
    """程序目录：打包后为 exe 所在目录；源码运行时优先取脚本所在目录。

    若脚本目录没有 config 而上级目录有（源码放在 source/ 子目录的项目布局），
    则沿用上级目录，与发布目录中 config/、logs/ 的布局保持一致。
    这样无论从哪个工作目录启动，配置都读写到固定位置。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(here, "config")):
        return here
    parent = os.path.dirname(here)
    if os.path.isdir(os.path.join(parent, "config")):
        return parent
    return here


APP_DIR = get_app_dir()


def config_path(name):
    return os.path.join(APP_DIR, "config", name)


def log_dir():
    path = os.path.join(APP_DIR, "logs")
    os.makedirs(path, exist_ok=True)
    return path
