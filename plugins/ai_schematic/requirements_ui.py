"""将后端 requirement-baseline/v1 按 HTML 报告的列展示为可筛选表格。"""
from __future__ import annotations

import json
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout)

HEADERS = ("要求编号", "指标名称", "要求值", "单位", "比较", "来源", "采用状态", "依据")
ORIGINS = {"document": "协议原文", "user": "用户输入", "user_feedback": "用户修改", "ai_suggestion": "AI 建议"}
STATUSES = {"adopted": "本版采用", "assumed": "本版假设", "proposed": "建议待定", "pending": "待补值或条件",
            "released": "用户已撤销", "superseded": "已由局部要求替代"}
COMPARISONS = {"min": "≥", "max": "≤", "range": "范围", "equal": "=", "eq": "=", "text": "文字要求",
               "gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "typical": "典型值", "target": "目标值"}


def display_value(value):
    if value is None:
        return "待明确"
    if isinstance(value, list):
        return " ～ ".join(display_value(v) for v in value) if len(value) == 2 else "、".join(display_value(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def requirement_rows(baseline):
    """取原始提取值与来源，不用模型补值；零值、待定值和假设均显式保留。"""
    rows = []
    for item in baseline.get("items") or []:
        field = item.get("field") or {}
        value = field.get("raw_value", field.get("value", field.get("normalized_value")))
        origin, status = item.get("origin", ""), item.get("status", "")
        rows.append({"cells": [str(item.get("requirement_id") or field.get("field_id") or ""),
            str(field.get("field_name") or field.get("name") or "未命名指标"), display_value(value),
            str(field.get("unit") or ""), COMPARISONS.get(field.get("comparison"), str(field.get("comparison") or "")),
            ORIGINS.get(origin, origin), STATUSES.get(status, status), str(item.get("reason") or "")],
            "origin": origin, "status": status,
            "details": "\n".join(["原文：" + str(field.get("source_text") or item.get("source_quote") or "未提供"),
                "位置：" + str(field.get("source_location") or "未提供"),
                "条件：" + display_value(field.get("conditions") or []),
                "备注：" + str(field.get("notes") or ""),
                "依据：" + str(item.get("reason") or "")])})
    for item in baseline.get("design_constraints") or []:
        status, origin = item.get("status", ""), item.get("origin", "user_feedback")
        rows.append({"cells": [str(item.get("requirement_id") or ""), "持续设计要求",
            str(item.get("source_quote") or ""), "", "文字要求", ORIGINS.get(origin, origin),
            STATUSES.get(status, status), str(item.get("reason") or f"{item.get('target', '')} 至少 {item.get('min_count', '')} 个")],
            "origin": origin, "status": status, "details": str(item.get("source_quote") or "")})
    return rows


class RequirementsDialog(QDialog):
    def __init__(self, baseline, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设计指标要求")
        self.resize(860, 540)
        self.setMinimumSize(520, 360)
        self.rows = requirement_rows(baseline)
        root = QVBoxLayout(self)
        title = QLabel(f"设计指标要求 · v{baseline.get('revision') or '—'}")
        title.setObjectName("sectionTitle")
        root.addWidget(title)
        summary = QLabel(f"共 {len(self.rows)} 项 · 协议原文 {sum(r['origin'] == 'document' for r in self.rows)} 项。采用不等于性能已验证。")
        summary.setWordWrap(True)
        root.addWidget(summary)
        self.search = QLineEdit()
        self.search.setPlaceholderText("筛选指标、数值、单位或来源…")
        root.addWidget(self.search)
        self.table = QTableWidget(len(self.rows), len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((90, 140, 110, 65, 80, 95, 130, 200)):
            self.table.setColumnWidth(column, width)
        for row_index, row in enumerate(self.rows):
            for column, text in enumerate(row["cells"]):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                if row["status"] in ("pending", "proposed", "assumed"):
                    cell.setForeground(QColor("#e5c27e"))
                elif row["status"] in ("released", "superseded"):
                    cell.setForeground(QColor("#8593a0"))
                self.table.setItem(row_index, column, cell)
        root.addWidget(self.table, 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(108)
        self.details.setPlaceholderText("选择一项指标查看提取原文、位置和条件")
        root.addWidget(self.details)
        buttons = QHBoxLayout()
        copy = QPushButton("复制表格")
        copy.clicked.connect(self.copy_table)
        buttons.addWidget(copy)
        buttons.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        root.addLayout(buttons)
        self.search.textChanged.connect(self.filter_rows)
        self.table.currentCellChanged.connect(self.show_details)
        if self.rows:
            self.table.selectRow(0)

    def filter_rows(self, text):
        query = text.strip().casefold()
        for index, row in enumerate(self.rows):
            self.table.setRowHidden(index, bool(query and query not in (" ".join(row["cells"]) + row["details"]).casefold()))

    def show_details(self, row, *_):
        self.details.setPlainText(self.rows[row]["details"] if 0 <= row < len(self.rows) else "")

    def copy_table(self):
        rows = [HEADERS, *(row["cells"] for index, row in enumerate(self.rows) if not self.table.isRowHidden(index))]
        QApplication.clipboard().setText("\n".join("\t".join(str(v).replace("\t", " ").replace("\n", " ") for v in row) for row in rows))
