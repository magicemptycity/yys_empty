"""Duel must keep checking settlement while matching or switching to auto."""
from collections import deque
from types import SimpleNamespace

import pytest

from module.base import timer as timer_module
from tasks.Duel import script_task as module


Task = module.ScriptTask
FRAMES = {
    'loading': set(),
    'home': {Task.I_CHECK_MAIN.name},
    'town': {Task.I_CHECK_TOWN.name},
    'duel': {Task.I_CHECK_DUEL.name, Task.I_D_HELP.name, Task.I_D_BATTLE.name},
    'prepare': {Task.I_D_PREPARE.name},
    'battle': {Task.I_BATTLE_INFO.name},
    'manual': {Task.I_BATTLE_INFO.name, Task.O_BATTLE_HAND.name},
    'auto': {Task.I_BATTLE_INFO.name, Task.O_BATTLE_AUTO.name},
    'win': {Task.I_D_VICTORY.name},
    'lose': {Task.I_D_FAIL.name},
}


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(timer_module, 'time', SimpleNamespace(time=lambda: clock.now))
    monkeypatch.setattr(module, 'sleep', lambda seconds: setattr(clock, 'now', clock.now + seconds))
    return clock


class Harness(Task):
    """Run real duel and BaseTask click loops against a sequence of screen frames."""

    def __init__(self, clock, frames, *, frame_seconds=0.5):
        self.clock = clock
        self.frames = deque(frames)
        self.frame = 'loading'
        self.frame_seconds = frame_seconds
        self.screenshots = 0
        self.clicks = []
        self.pages = []
        self.green_marks = 0
        self.statuses = []
        self.exits = 0
        self.interval_timer = {}
        self.conf = SimpleNamespace(duel_config=SimpleNamespace(green_enable=True, green_mark='green_left1'))
        self.device = SimpleNamespace(image=object(), screenshot=self.screenshot, click=self.device_click)

    def screenshot(self):
        self.screenshots += 1
        assert self.screenshots < 100, 'Duel stopped checking page changes'
        self.clock.now += self.frame_seconds
        if self.frames:
            self.frame = self.frames.popleft()
        return self.device.image

    def appear(self, target, interval=None, **kwargs):
        return target.name in FRAMES[self.frame]

    def ocr_appear(self, target, interval=None):
        return self.appear(target)

    def _wait_click_reaction(self, *args):
        pass

    def device_click(self, x, y, control_name):
        if control_name == self.O_BATTLE_HAND.name:
            assert self.frame == 'manual', 'Clicked auto/manual on a settlement page'
        self.clicks.append(control_name)

    def green_mark(self, *args):
        assert self.frame == 'auto', 'Marked before auto was confirmed'
        self.green_marks += 1

    def reset_device(self, status):
        self.statuses.append(status)

    def goto_page(self, page):
        assert self.frame in ('home', 'town'), 'Navigation interrupted matching or battle'
        assert page is module.page_duel
        self.pages.append(page)
        self.frame = 'duel'

    def duel_exit_battle(self):
        self.exits += 1
        self.frames = deque(['lose', 'duel'])


@pytest.mark.parametrize('frames,manual_clicks,green_marks,result', [
    (['manual', 'auto', 'win', 'duel'], 1, 1, True),
    (['auto', 'lose', 'duel'], 0, 1, False),
    (['loading', 'loading', 'win', 'duel'], 0, 0, True),
    (['manual', 'loading', 'win', 'duel'], 1, 0, True),
    (['win', 'duel'], 0, 0, True),
])
def test_settlement_remains_reachable_during_auto_switch(clock, frames, manual_clicks, green_marks, result):
    task = Harness(clock, frames)
    assert task.wait_battle() is result
    assert task.clicks.count(task.O_BATTLE_HAND.name) == manual_clicks
    assert task.green_marks == green_marks
    assert any(name.startswith('result_random_') for name in task.clicks)
    assert not task.pages


def test_auto_switch_retries_until_confirmed(clock):
    task = Harness(clock, ['manual', 'manual', 'auto', 'auto', 'win', 'duel'], frame_seconds=1)
    assert task.wait_battle() is True
    assert task.clicks.count(task.O_BATTLE_HAND.name) == 2
    assert task.green_marks == 1


def test_missing_auto_button_does_not_block_battle_timeout(clock):
    task = Harness(clock, ['loading'], frame_seconds=271)
    assert task.wait_battle() is False
    assert task.exits == 1
    assert task.green_marks == 0


@pytest.mark.parametrize('page', ['home', 'town'])
def test_matching_recovers_confirmed_outside_page(clock, page):
    task = Harness(clock, [page, page, 'duel', 'duel', 'prepare'])
    assert task.enter_battle() is False
    assert task.pages == [module.page_duel]
    assert task.clicks == [task.I_D_BATTLE.name]


def test_transient_outside_marker_does_not_interrupt_preparation(clock):
    task = Harness(clock, ['home', 'prepare'])
    assert task.enter_battle() is False
    assert not task.pages and not task.clicks


@pytest.mark.parametrize('frame,result', [('prepare', False), ('battle', False), ('win', True)])
def test_matching_hands_off_to_current_battle_stage(clock, frame, result):
    task = Harness(clock, [frame])
    assert task.enter_battle() is result
    assert not task.pages and not task.clicks


def test_matching_result_arriving_during_click_recheck_is_not_clicked_as_battle(clock):
    task = Harness(clock, ['duel', 'win'])
    assert task.enter_battle() is True
    assert not task.pages and not task.clicks


def test_prepare_and_wait_can_handoff_directly_to_settlement(clock):
    task = Harness(clock, ['win', 'win', 'duel'])
    task.battle_prepare()
    assert task.wait_battle() is True
    assert task.green_marks == 0
