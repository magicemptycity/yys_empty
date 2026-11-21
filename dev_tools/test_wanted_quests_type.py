"""Wanted quest type OCR tolerates extra text around the Shikigami label."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tasks.WantedQuests.config import WQType
from tasks.WantedQuests.script_task import ScriptTask


def build_wq_info(type_text, allowed_types=None):
    config = SimpleNamespace(
        _wq_type_ordered_list=list(WQType) if allowed_types is None else allowed_types,
    )
    task = SimpleNamespace(
        device=SimpleNamespace(image=object()),
        get_config=lambda: config,
    )
    type_rule = Mock()
    type_rule.ocr.return_value = type_text
    info_rule = Mock()
    info_rule.ocr.return_value = '第十章（数量：14）'
    goto_btn = object()
    result = ScriptTask.build_wq_info(task, type_rule, info_rule, goto_btn, 15)
    return result, info_rule, goto_btn


@pytest.mark.parametrize('type_text,expected_type', [
    ('式神', WQType.CHALLENGE),
    ('式神碎片', WQType.CHALLENGE),
    ('式神石', WQType.CHALLENGE),
    ('【式神】', WQType.CHALLENGE),
    (' 式神 ', WQType.CHALLENGE),
    ('挑战', WQType.CHALLENGE),
    ('秘闻', WQType.SECRET),
    ('探索', WQType.EXPLORE),
    ('妖气', WQType.YOKAI),
])
def test_recognized_type_builds_wanted_quest(type_text, expected_type):
    result, _, goto_btn = build_wq_info(type_text)
    assert result.type is expected_type
    assert result.dest == '第十章'
    assert result.number == 14
    assert result.do_num == 2
    assert result.goto_btn is goto_btn


@pytest.mark.parametrize('type_text', ['', '未知', '式申', '碎片', '式 神'])
def test_unknown_types_are_still_skipped(type_text):
    result, info_rule, _ = build_wq_info(type_text)
    assert result is None
    info_rule.ocr.assert_not_called()


def test_normalized_challenge_still_respects_configured_types():
    result, info_rule, _ = build_wq_info('式神石', [WQType.SECRET, WQType.EXPLORE])
    assert result is None
    info_rule.ocr.assert_not_called()
