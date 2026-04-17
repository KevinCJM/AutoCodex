# -*- encoding: utf-8 -*-
"""
@File: A03_RequirementsReview.py
@Modify Time: 2026/4/17
@Author: Kevin-Chen
@Descriptions: 兼容层，已顺延为 A04_RequirementsReview
"""

from __future__ import annotations

import sys

import A04_RequirementsReview as _impl

sys.modules[__name__] = _impl
