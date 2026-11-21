"""Wanted quest details must be ready before execution and closed after skipping."""
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from module.base import timer as timer_module
from module.exception import TaskEnd
from tasks.WantedQuests import script_task as module
from tasks.WantedQuests.config import WQInfo, WQType


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(timer_module, 'time', SimpleNamespace(time=lambda: clock.now))
    return clock


class Harness(module.ScriptTask):
    def __init__(self, reason, clock, *, goto_frames=None, frame_seconds=0.25):
        self.reason = reason
        self.clock = clock
        self.frame_seconds = frame_seconds
        self.goto_frames = None if goto_frames is None else deque(goto_frames)
        self.visible_gotos = set()
        self.info_reads = []
        self.screen = 'detail'
        self.traced = True
        self.screenshots = 0
        self.clicks = []
        self.pages = []
        self.battles = []
        self.wq_executed_set = set()
        self.unwanted_boss_name_list = ['ignored']
        self.device = SimpleNamespace(image=object(), screenshot=self.screenshot, click=self.device_click)
        self.O_WQ_MONSTER_TYPE = SimpleNamespace(detect_text=lambda image: 'ignored' if self.reason == 'unwanted' else 'target')
        self.O_WQ_TEXT_ALL = SimpleNamespace(area=None)
        self.info = WQInfo(WQType.CHALLENGE, '第二章', 14, self.I_GOTO_1, 1)

    def screenshot(self):
        self.clock.now += self.frame_seconds
        self.screenshots += 1
        if self.goto_frames is None:
            self.visible_gotos = set() if self.reason == 'locked' else {self.I_GOTO_1}
        elif self.goto_frames:
            self.visible_gotos = self.goto_frames.popleft()
        assert self.screenshots < 100, 'Skipping a quest failed to return to exploration'
        return self.device.image

    def appear(self, target, interval=None, **kwargs):
        if target in (self.I_GOTO_1, self.I_GOTO_2, self.I_GOTO_3, self.I_GOTO_4):
            return self.screen == 'detail' and target in self.visible_gotos
        if target is self.I_TRACE_TRUE:
            return self.screen == 'detail' and self.traced
        if target is self.I_TRACE_FALSE:
            return self.screen == 'detail' and not self.traced
        if target is self.I_UI_BACK_RED:
            return self.screen == 'detail'
        return False

    def _wait_click_reaction(self, *args):
        pass

    def device_click(self, x, y, control_name):
        assert self.screen == 'detail'
        self.clicks.append(control_name)
        if control_name == self.I_TRACE_TRUE.name:
            self.traced = False
        elif control_name == self.I_UI_BACK_RED.name:
            self.screen = 'exploration'
        else:
            raise AssertionError(f'Unexpected click: {control_name}')

    def goto_page(self, page):
        assert page is module.page_exploration
        assert self.screen == 'exploration', 'The detail overlay must be closed before reading the list'
        self.pages.append(page)

    def get_ordered_wq_infos(self, num_want):
        self.info_reads.append(self.screenshots)
        return [] if self.reason == 'no_routes' else [self.info]

    def get_need_exec_wq(self, infos):
        return None if self.reason == 'unavailable' else infos[0]

    def challenge(self, button, number):
        self.battles.append((button, number))
        self.screen = 'exploration'


@pytest.mark.parametrize('reason', ['locked', 'unwanted', 'no_routes', 'unavailable'])
def test_each_skip_branch_closes_detail_before_returning(reason, clock):
    task = Harness(reason, clock)
    assert task.execute_mission(12) is False
    assert task.screen == 'exploration'
    assert not task.traced
    assert task.I_UI_BACK_RED.name in task.clicks
    assert task.pages == [module.page_exploration]
    assert not task.battles
    assert not task.wq_executed_set


def test_successful_quest_still_records_execution_and_returns(clock):
    task = Harness('success', clock)
    task.execute_mission(12)
    assert task.battles == [(task.I_GOTO_1, 1)]
    assert task.wq_executed_set == {task.info}
    assert task.pages == [module.page_exploration]
    assert not task.clicks


def test_run_continues_with_next_quest_after_skipping(monkeypatch, clock):
    task = Harness('locked', clock)
    task.screen = 'exploration'
    task.reads = 0
    task.config = SimpleNamespace(model=SimpleNamespace(wanted_quests=SimpleNamespace(
        wanted_quests_config=SimpleNamespace(unwanted_boss_names=''),
        switch_soul_config=SimpleNamespace(enable=False, enable_switch_by_name=False),
    )))
    task.get_config = lambda: SimpleNamespace(cooperation_only=False)
    task.pre_work = lambda: True
    task.next_run = Mock()
    task.is_wq_remained = lambda: task.reads < 2

    def find_wq(image):
        assert task.screen == 'exploration', 'Task progress was read behind the detail overlay'
        task.reads += 1
        return 0, 1, 12, [0, 0, 10, 10]

    def open_wq_info():
        task.screen = 'detail'
        task.traced = True
        task.reason = 'locked' if task.reads == 1 else 'success'
        return True

    task.find_wq = find_wq
    task.open_wq_info = open_wq_info
    monkeypatch.setattr(module, 'sleep', lambda seconds: None)
    with pytest.raises(TaskEnd):
        task.run()
    assert task.reads == 2
    assert task.battles == [(task.I_GOTO_1, 1)]
    assert task.screen == 'exploration'
    task.next_run.assert_called_once_with()


@pytest.mark.parametrize('row', [1, 2, 3, 4])
def test_any_goto_row_must_be_seen_in_two_new_frames_before_reading_info(clock, row):
    button = getattr(module.ScriptTask, f'I_GOTO_{row}')
    task = Harness('success', clock, goto_frames=[set(), {button}, {button}])
    task.execute_mission(12)
    assert task.info_reads == [3]
    assert task.battles == [(task.I_GOTO_1, 1)]  # Readiness must not change route selection.
    assert not task.clicks


def test_disappearing_goto_resets_confirmation(clock):
    button = module.ScriptTask.I_GOTO_1
    task = Harness('success', clock, goto_frames=[{button}, set(), {button}, {button}])
    task.execute_mission(12)
    assert task.info_reads == [4]
    assert task.battles == [(task.I_GOTO_1, 1)]


def test_one_persistent_goto_is_enough_when_other_rows_change(clock):
    first, second, third = (module.ScriptTask.I_GOTO_1, module.ScriptTask.I_GOTO_2, module.ScriptTask.I_GOTO_3)
    task = Harness('success', clock, goto_frames=[{first, second}, {second, third}])
    task.execute_mission(12)
    assert task.info_reads == [2]
    assert len(task.battles) == 1


def test_different_goto_rows_in_adjacent_frames_do_not_confirm_readiness(clock):
    first, second = module.ScriptTask.I_GOTO_1, module.ScriptTask.I_GOTO_2
    task = Harness('success', clock, goto_frames=[{first}, {second}] * 10)
    assert task.execute_mission(12) is False
    assert not task.info_reads and not task.battles
    assert task.screen == 'exploration'


def test_no_goto_times_out_in_three_seconds_without_reading_task_info(clock):
    task = Harness('locked', clock)
    assert task._wait_wq_goto() is False
    assert 3 <= clock.now - 1000 <= 3.25
    assert not task.info_reads and not task.battles and not task.clicks


def test_confirmation_arriving_after_deadline_does_not_start_task(clock):
    button = module.ScriptTask.I_GOTO_1
    task = Harness('success', clock, goto_frames=[{button}, {button}], frame_seconds=2)
    assert task.execute_mission(12) is False
    assert not task.info_reads and not task.battles
    assert task.screen == 'exploration'
