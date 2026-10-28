"""指标表保留原始提取信息，不把零值、待定值或 AI 假设错误显示。"""
import os
import unittest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from ai_schematic.requirements_ui import RequirementsDialog, requirement_rows


def baseline():
    return {"schema": "requirement-baseline/v1", "revision": 3, "items": [
        {"requirement_id": "REQ_F", "field": {"field_name": "频率范围", "raw_value": [7200, 7250], "unit": "MHz", "comparison": "range",
            "source_text": "频率范围 7200～7250 MHz", "source_location": "协议第2节"}, "origin": "document", "status": "adopted", "reason": "协议明确要求"},
        {"requirement_id": "REQ_Z", "field": {"field_name": "偏置电压", "raw_value": 0, "normalized_value": 999, "unit": "V", "comparison": "eq"}, "origin": "user", "status": "adopted"},
        {"requirement_id": "REQ_N", "field": {"field_name": "噪声系数", "raw_value": None, "comparison": "max", "unit": "dB"}, "origin": "ai_suggestion", "status": "pending"}]}


class RequirementsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_extracted_values_units_origins_and_adoption_match_report(self):
        rows = requirement_rows(baseline())
        self.assertEqual(rows[0]["cells"], ["REQ_F", "频率范围", "7200 ～ 7250", "MHz", "范围", "协议原文", "本版采用", "协议明确要求"])
        self.assertIn("协议第2节", rows[0]["details"])
        self.assertIn("7200～7250", rows[0]["details"])

    def test_zero_and_unknown_are_not_replaced_by_normalized_or_invented_values(self):
        rows = requirement_rows(baseline())
        self.assertEqual(rows[1]["cells"][2], "0")
        self.assertEqual(rows[2]["cells"][2], "待明确")
        self.assertEqual(rows[2]["cells"][5], "AI 建议")
        self.assertEqual(rows[2]["cells"][6], "待补值或条件")

    def test_dialog_table_filters_and_shows_original_evidence(self):
        dialog = RequirementsDialog(baseline())
        try:
            self.assertEqual(dialog.windowTitle(), "设计指标要求")
            self.assertEqual(dialog.table.rowCount(), 3)
            dialog.search.setText("噪声")
            self.assertTrue(dialog.table.isRowHidden(0))
            self.assertFalse(dialog.table.isRowHidden(2))
            dialog.search.clear()
            dialog.table.setCurrentCell(0, 0)
            self.assertIn("协议第2节", dialog.details.toPlainText())
        finally:
            dialog.close()

    def test_empty_baseline_and_text_constraints_are_explicit(self):
        self.assertEqual(requirement_rows({}), [])
        data = {"design_constraints": [{"requirement_id": "D1", "source_quote": "保留滤波器", "status": "adopted", "target": "滤波器", "min_count": 1}]}
        self.assertEqual(requirement_rows(data)[0]["cells"][2], "保留滤波器")
