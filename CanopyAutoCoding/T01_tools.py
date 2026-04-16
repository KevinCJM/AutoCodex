# -*- encoding: utf-8 -*-
"""
@File: T01_tools.py
@Modify Time: 2026/4/10 16:22       
@Author: Kevin-Chen
@Descriptions: 
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _stderr_message(text: object) -> None:
    sys.stderr.write(f"{text}\n")
    sys.stderr.flush()


def merge_review_records(
        directory: str | Path,
        pattern: str = "任务单评审记录_*.md",
        output_name: str = "任务单评审记录.md",
        encoding: str = "utf-8",
) -> Path:
    """
    将匹配的文件内容纯合并。若无匹配文件，则生成一个空的输出文件。
    """
    dir_path = Path(directory).expanduser().resolve()
    if not dir_path.exists():
        raise FileNotFoundError(f"目录不存在: {dir_path}")

    output_path = dir_path / output_name

    # 获取并排序匹配的文件
    files = sorted(
        f for f in dir_path.glob(pattern)
        if f.is_file() and f.resolve() != output_path
    )

    # 如果没有任何匹配文件，生成一个彻底的空文件
    if not files:
        output_path.write_text("", encoding=encoding)
        return output_path

    merged_parts: list[str] = []

    for file_path in files:
        content = file_path.read_text(encoding=encoding).strip()
        if content:
            merged_parts.append(content)
            # 在非最后一个文件后添加分隔线
            merged_parts.append("\n\n---\n")

    # 如果所有文件都是空的，merged_parts 也会是空的
    if not merged_parts:
        merged_text = ""
    else:
        # 移除最后一个多余的分隔线
        merged_text = "\n\n".join(merged_parts).rstrip("- \n") + "\n"

    output_path.write_text(merged_text, encoding=encoding)

    return output_path


def create_empty_json_files(
        directory: str | Path,
        name_list: list[str],
        pattern: str = "代码评审记录_*.json",
        encoding: str = "utf-8"
) -> None:
    """
    根据指定的 pattern 和 name_list 批量创建包含空列表的 JSON 文件。

    :param directory: 目标文件夹
    :param name_list: 名称列表，如 ['a', 'b']
    :param pattern: 文件名模式，必须包含 '*'，例如 "评审记录_*.json"
    """
    dir_path = Path(directory).expanduser().resolve()
    dir_path.mkdir(parents=True, exist_ok=True)

    if "*" not in pattern:
        raise ValueError("pattern 必须包含 '*' 用于替换文件名部分")

    for name in name_list:
        # 将 pattern 中的 * 替换为具体的 name
        file_name = pattern.replace("*", name)
        file_path = dir_path / file_name

        with file_path.open("w", encoding=encoding) as f:
            # 写入空列表
            json.dump([], f)


def check_all_reviews_passed(
        directory: str | Path,
        task_name: str,
        pattern: str = "代码评审记录_*.json",
        encoding: str = "utf-8",
) -> bool:
    """
    检查所有 JSON 文件中，特定 task_name 对应的 review_pass 是否全部为 true。
    """
    dir_path = Path(directory).expanduser().resolve()
    files = list(dir_path.glob(pattern))

    if not files:
        return True

    def get_task_status(file_path: Path) -> bool:
        try:
            with file_path.open(encoding=encoding) as f:
                data = json.load(f)

            # 确保数据是列表格式
            if not isinstance(data, list):
                return False

            # 在列表中寻找 task_name 匹配的项
            for item in data:
                if item.get("task_name") == task_name:
                    # 找到任务，返回其 review_pass 状态（缺省为 False）
                    return item.get("review_pass", False)

            # 如果遍历完整个列表都没找到指定的 task_name
            return False

        except (json.JSONDecodeError, IOError, AttributeError):
            # 文件损坏、读取错误或数据格式异常
            return False

    # 只有当所有文件中该任务的 review_pass 均为 True 时，才返回 True
    return all(get_task_status(f) for f in files)


def is_file_empty(file_path: str | Path, encoding: str = "utf-8") -> bool:
    """
    判断指定文件是否为空。
    兼容逻辑：
    1. 物理大小为 0 字节 -> True
    2. 仅包含空格、回车 (\n)、制表符 (\t) 等空白字符 -> True
    3. 文件不存在或不是文件 -> True
    """
    path = Path(file_path)

    # 1. 基础检查：不存在或不是文件
    if not path.exists() or not path.is_file():
        return True

    # 2. 性能优化：如果物理大小就是 0，直接返回 True，无需读取
    if path.stat().st_size == 0:
        return True

    # 3. 内容检查：读取并剔除空白字符
    try:
        content = path.read_text(encoding=encoding)
        # strip() 会移除字符串首尾的所有空白字符（包括 \n, \r, \t, space）
        return not content.strip()
    except (UnicodeDecodeError, IOError):
        # 如果读取失败（如二进制文件），根据业务逻辑可以返回 False 或抛出异常
        return False


def get_first_false_task(file_path: str | Path, encoding: str = "utf-8") -> str | None:
    """
    读取 JSON 文件，按顺序返回第一个值为 False 的任务 Key。
    如果全部为 True，则返回 None。
    """
    path = Path(file_path)
    if not path.exists():
        return None

    try:
        with path.open(encoding=encoding) as f:
            data = json.load(f)

        # 第一层遍历：M1, M2, M3...
        for module_key, tasks in data.items():
            # 确保 tasks 是字典格式
            if isinstance(tasks, dict):
                # 第二层遍历：M1-T1, M1-T2...
                for task_key, status in tasks.items():
                    # 找到第一个 False
                    if status is False:
                        return task_key

    except (json.JSONDecodeError, IOError):
        return None

    return None


def update_task_to_true(file_path: str | Path, target_key: str, encoding: str = "utf-8") -> bool:
    """
    将 JSON 文件中指定 task key 的值修改为 true。
    """
    path = Path(file_path)
    if not path.exists():
        return False

    try:
        # 1. 读取原始数据
        with path.open("r", encoding=encoding) as f:
            data = json.load(f)

        # 2. 遍历查找并修改
        found = False
        for module_name, tasks in data.items():
            if isinstance(tasks, dict) and target_key in tasks:
                tasks[target_key] = True
                found = True
                break  # 找到后立即退出循环

        # 3. 如果修改成功，写回文件
        if found:
            with path.open("w", encoding=encoding) as f:
                # indent=2 保持 JSON 的美观易读
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True

        return False

    except (json.JSONDecodeError, IOError):
        return False


def task_done(directory, file_path, task_name="M1-T1", json_pattern="代码评审记录_*.json",
              md_pattern="代码评审记录_*.md", md_output_name="代码评审记录.md"):
    # 合并 各个评审md文件 为 单一评审md文件
    md_output_file = merge_review_records(directory, md_pattern, md_output_name)
    # 判断 单一评审md文件 是否为空
    md_bool = is_file_empty(md_output_file)
    # 判断是否所有评审json文件全部是通过
    json_bool = check_all_reviews_passed(directory, task_name, json_pattern)
    if md_bool and json_bool:
        # 如果所有的 md 和 json 都为通过, 则更新该任务为通过
        update_task_to_true(file_path, target_key=task_name)
        return True
    else:
        return False


def get_task_review_status(
        file_path: str | Path,
        target_task: str,
        encoding: str = "utf-8"
) -> bool | None:
    """
    获取 JSON 文件中指定 task_name 对应的 review_pass 值。

    :param file_path: JSON 文件路径
    :param target_task: 目标任务名称
    :return: 对应的布尔值；如果未找到任务或文件异常，返回 None
    """
    path = Path(file_path)
    if not path.exists():
        return None

    try:
        with path.open("r", encoding=encoding) as f:
            data = json.load(f)

        if not isinstance(data, list):
            return None

        # 遍历列表寻找匹配的 task_name
        for item in data:
            if item.get("task_name") == target_task:
                # 返回找到的 review_pass 值，如果该键不存在则默认为 False
                return item.get("review_pass", False)

    except (json.JSONDecodeError, IOError):
        return None

    # 如果遍历结束仍未找到匹配项
    return None


def check_task_exists(file_path: str | Path, target_task: str, encoding: str = "utf-8") -> bool:
    """
    检查 JSON 列表文件中是否存在指定的 task_name。
    """
    path = Path(file_path)
    if not path.exists():
        return False

    try:
        with path.open("r", encoding=encoding) as f:
            data = json.load(f)

        # 确保数据是列表格式
        if not isinstance(data, list):
            return False

        # 遍历列表，检查是否有任何一项的 task_name 匹配
        return any(item.get("task_name") == target_task for item in data)

    except (json.JSONDecodeError, IOError):
        # 如果文件损坏或读取失败，返回 False
        return False


def get_markdown_content(file_path: str | Path, encoding: str = "utf-8") -> str:
    """
    读取指定 Markdown 文件的内容并返回字符串。

    :param file_path: 文件路径（支持字符串或 Path 对象）
    :param encoding: 文件编码，默认为 utf-8
    :return: 文件文本内容；若文件不存在则返回空字符串或抛出异常
    """
    path = Path(file_path).expanduser().resolve()

    if not path.exists():
        # 根据你的业务需求，也可以选择 raise FileNotFoundError
        _stderr_message(f"警告：文件不存在 -> {path}")
        return ""

    if not path.is_file():
        _stderr_message(f"警告：目标不是文件 -> {path}")
        return ""

    try:
        # read_text 自动处理打开和关闭操作
        return path.read_text(encoding=encoding)
    except Exception as e:
        _stderr_message(f"读取文件时出错: {e}")
        return ""


# 获取 Markdown 内容并判断是否存在指定的字符串
def check_string_in_markdown(file_path: str | Path, target_string: str = "M1-T1") -> bool:
    """
    获取 Markdown 内容并判断是否存在指定的字符串。
    """
    # 1. 调用之前定义的函数获取内容
    content = get_markdown_content(file_path)

    # 2. 判断字符串是否在内容中
    # 建议使用 in 操作符，它简单且高效
    if target_string in content:
        return True

    return False


# 将字典数据以 JSON 格式写入指定文件
def write_dict_to_json(file_path, data, encoding: str = "utf-8") -> Path:
    """
    将字典数据以 JSON 格式写入指定文件。

    :param file_path: 目标文件路径
    :param data: 要写入的字典数据
    :param encoding: 字符编码，默认为 utf-8
    :return: 写入后的文件 Path 对象
    """
    path = Path(file_path).expanduser().resolve()

    # 确保父级目录存在
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with path.open("w", encoding=encoding) as f:
            # indent=2: 让 JSON 有缩进，方便人工查看
            # ensure_ascii=False: 允许直接写入中文，不转义为 \uXXXX
            json.dump(data, f, indent=2, ensure_ascii=False)

        return path
    except Exception as e:
        _stderr_message(f"写入 JSON 文件时出错: {e}")
        raise


# 判断 JSON 文件是否符合特定的初始标准格式
def is_standard_task_initial_json(file_path: str | Path, encoding: str = "utf-8") -> bool:
    """
    判断 JSON 文件是否符合特定的初始标准格式：
    1. 必须是合法的 JSON。
    2. 根节点必须是字典。
    3. 二级结构也必须是字典。
    4. 所有二级字典中的值必须全部为 False。
    """
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return False

    try:
        # 1. 语法检查：尝试解析 JSON
        data = json.loads(path.read_text(encoding=encoding))

        # 2. 结构检查：根节点必须是字典且不能为空
        if not isinstance(data, dict) or not data:
            return False

        # 3. 深度检查
        for module_name, tasks in data.items():
            # 二级节点必须是字典
            if not isinstance(tasks, dict):
                return False

            # 如果二级字典为空（可选检查），视作不标准
            if not tasks:
                return False

            # 4. 值检查：二级字典中的所有值必须严格为布尔值 False
            for task_status in tasks.values():
                # 注意：必须使用 is False 进行身份判断，防止 0 被误判为 False
                if task_status is not False:
                    return False

        return True

    except (json.JSONDecodeError, UnicodeDecodeError, IOError):
        # 语法错误、编码错误或 IO 错误统统返回 False
        return False


if __name__ == "__main__":
    # create_empty_json_files(directory="/Users/chenjunming/Desktop/Canopy/canopy-api-v3",
    #                         name_list=['G1', 'G2', 'Q', 'CC'],
    #                         pattern="代码评审记录_*.json")
    task = "M4-T2"
    print(get_first_false_task('/Users/chenjunming/Desktop/Canopy/canopy-api-v3/任务单.json'))
    print(task_done(directory="/Users/chenjunming/Desktop/Canopy/canopy-api-v3",
                    file_path='/Users/chenjunming/Desktop/Canopy/canopy-api-v3/任务单.json',
                    task_name=task,
                    json_pattern="代码评审记录_*.json",
                    md_pattern="代码评审记录_*.md", md_output_name="代码评审记录.md"))

    print(check_string_in_markdown('/Users/chenjunming/Desktop/Canopy/canopy-api-v3/工程师开发内容.md', task))
