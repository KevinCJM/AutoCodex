# -*- encoding: utf-8 -*-
"""
@File: other_utils.py
@Modify Time: 2025/12/5 11:36       
@Author: Kevin-Chen
@Descriptions: 
"""
import os
import json
from pathlib import Path

# 当前文件夹路径
CURRENT_DIR = Path(__file__).resolve().parent


def is_valid_json_data(data):
    """
    判断 data 是否为 JSON 支持的基础数据结构
    JSON 支持: dict, list, str, int, float, bool, None
    """
    if isinstance(data, (str, int, float, bool)) or data is None:
        return True

    if isinstance(data, list):
        return all(is_valid_json_data(item) for item in data)

    if isinstance(data, dict):
        # key 必须是 str
        return all(
            isinstance(k, str) and is_valid_json_data(v)
            for k, v in data.items()
        )

    return False


def session_name_json(folder_path, file_path="session_name.json"):
    """
    确保 folder_path/file_path 这个 JSON 文件存在，不存在则创建 {}
    返回文件的绝对路径
    """
    # 确保目录存在
    os.makedirs(folder_path, exist_ok=True)

    session_name_json_path = os.path.join(folder_path, file_path)
    if not os.path.exists(session_name_json_path):
        with open(session_name_json_path, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False)

    return session_name_json_path


def write_json(folder_path, file_path, data):
    """
    在 folder_path/file_path 写入 JSON 数据（以追加键值对方式合并）
    """

    if not isinstance(data, dict):
        raise TypeError("data 必须是 dict 才能追加键值对")

    if not is_valid_json_data(data):
        raise TypeError("data 含有不能转换为 JSON 的类型")

    # 确保文件存在，并拿到路径
    json_path = session_name_json(folder_path, file_path)

    # 读原有内容
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
            if not isinstance(existing, dict):
                existing = {}
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {}

    # 合并（追加键值对）
    existing.update(data)

    # 写回
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def json_key_exists(folder_path, file_path, key):
    """
    判断指定的 key 是否已经存在于 folder_path/file_path 对应的 JSON 文件中
    仅检查顶层键，返回 True / False
    """
    if not isinstance(key, str):
        raise TypeError("key 必须是 str")

    # 确保文件存在（若不存在则创建一个空的 {}）
    json_path = session_name_json(folder_path, file_path)

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
            if not isinstance(existing, dict):
                return False
    except (FileNotFoundError, json.JSONDecodeError):
        return False

    return key in existing


def delete_json_key(folder_path, file_path, key):
    """
    从 folder_path/file_path 对应的 JSON 文件中
    删除指定的顶层 key 及其对应的值。

    无论 key 是否存在，函数都会在检查后重写整个 JSON 文件。
    返回布尔值：True 表示确实删除了一个已存在的 key，False 表示原本就不存在。
    """
    if not isinstance(key, str):
        raise TypeError("key 必须是 str")

    # 确保文件存在（若不存在则创建一个空的 {}）
    json_path = session_name_json(folder_path, file_path)

    # 读取原有内容
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
            if not isinstance(existing, dict):
                existing = {}
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {}

    # 删除指定 key（若存在）
    existed = key in existing
    if existed:
        del existing[key]

    # 重写文件
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    return existed
