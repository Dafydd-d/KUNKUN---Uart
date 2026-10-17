"""JSON 配置读写：统一容错与日志，避免各处裸 except 静默吞掉保存失败。"""
import json
import logging
import os


def load_json(path, default=None):
    """读取 JSON 配置；文件不存在或内容异常时返回 default 并记录日志。"""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning("Load config failed: %s (%s)", path, e)
        return default


def save_json(path, data):
    """保存 JSON 配置（自动建目录）；失败时记录日志并返回 False。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logging.warning("Save config failed: %s (%s)", path, e)
        return False
