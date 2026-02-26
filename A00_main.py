# -*- encoding: utf-8 -*-
"""
@File: A00_main.py
@Modify Time: 2026/1/17 23:35       
@Author: Kevin-Chen
@Descriptions: 
"""

from A01_requiment_analysis_workflow import main as requirement_workflow_main
from A02_task_workflow import main as task_workflow_main
from A03_coding_agent_workflow import main as coding_workflow_main

if __name__ == "__main__":
    # # 需求分析 工作流
    # requirement_workflow_main()

    # 任务拆分 工作流
    task_workflow_main()

    # 代码开发 工作流
    coding_workflow_main()
