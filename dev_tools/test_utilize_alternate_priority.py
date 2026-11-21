"""Nested foster prompts must block all clicks on the underlying confirmation."""
from collections import deque
from types import SimpleNamespace

import pytest

from module.device.device import Device
from module.exception import GameStuckError
from tasks.Component.ReplaceShikigami import replace_shikigami as module


ReplaceShikigami = module.ReplaceShikigami
SMALL = ReplaceShikigami.I_U_CONFIRM_SMALL
ALTERNATE = ReplaceShikigami.I_U_CONFIRM_ALTERNATE
CIRCLE = ReplaceShikigami.I_U_CIRCLE_ALTERNATE
STOP = ReplaceShikigami.I_RS_NO_ADD


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=1000.0)

    class Timer:
        def __init__(self, limit):
            self.limit = limit
            self.deadline = 0

        def start(self):
            return self.reset()

        def reset(self):
            self.deadline = clock.now + self.limit
            return self

        def reached(self):
            return clock.now >= self.deadline

        def reached_and_reset(self):
            if self.reached():
                self.reset()
                return True
            return False

    monkeypatch.setattr(module, 'Timer', Timer)
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: clock.now))
    return clock


class Harness(ReplaceShikigami):
    def __init__(self, clock, *, state='select', modal_on=1, succeed_on=1,
                 stays_open=False, slot_visible=False):
        self.clock = clock
        self.state = state
        self.modal_on = modal_on
        self.succeed_on = succeed_on
        self.stays_open = stays_open
        self.slot_visible = slot_visible
        self.response_delay = 1.75
        self.confirmations = 0
        self.last_confirmation = 0
        self.clicks = []
        self.click_record = deque(maxlen=15)
        self.screenshots = 0
        self.intervals = {}
        self.hide_on_recheck = False
        self.hide_pending = False
        self.underlying_attempts = 0
        self.device = SimpleNamespace(screenshot=self.screenshot, click=self.device_click)

    def click_record_clear(self):
        raise AssertionError('Global click guard must not be disabled or reset')

    def screenshot(self):
        self.clock.now += 0.25
        self.screenshots += 1
        assert self.screenshots < 600, 'Modal handling did not terminate'
        if self.hide_pending:
            self.state = 'done'
            self.hide_pending = False
        if (self.state == 'responding'
                and self.clock.now - self.last_confirmation >= self.response_delay):
            if self.confirmations == self.modal_on:
                self.state = 'alternate'
            elif self.succeed_on is not None and self.confirmations >= self.succeed_on:
                self.state = 'done'
            else:
                self.state = 'confirm' if self.stays_open else 'select'

    def _wait_click_reaction(self, *args):
        pass

    def appear(self, target, interval=None, **kwargs):
        if interval and self.clock.now < self.intervals.get(target.name, 0):
            return False
        if target == ALTERNATE:
            matched = self.state in ('alternate', 'checked')
        elif target == CIRCLE:
            matched = self.state == 'alternate'
        elif target == SMALL:
            matched = self.state in ('confirm', 'responding', 'alternate', 'checked')
        elif target == STOP:
            matched = self.state == 'select' or (self.slot_visible and self.state in ('alternate', 'checked'))
        else:
            matched = False
        if interval and matched:
            self.intervals[target.name] = self.clock.now + interval
            if target == SMALL:
                if self.hide_on_recheck:
                    self.hide_pending = True
                    self.hide_on_recheck = False
        return matched

    # Keep the real BaseTask.appear_then_click: it takes another screenshot before clicking.
    def click(self, target, interval=None):
        if interval and self.clock.now < self.intervals.get(target.name, 0):
            return False
        self.device_click(*target.coord(), control_name=target.name)
        if interval:
            self.intervals[target.name] = self.clock.now + interval
        return bool(interval)  # Matches BaseTask.click's return convention.

    def device_click(self, x, y, control_name):
        self.click_record.append(control_name)
        Device.click_record_check(self)
        self.clicks.append(control_name)
        if control_name == SMALL.name:
            self.underlying_attempts += 1
            assert self.state not in ('alternate', 'checked'), 'Clicked through the top modal'
            self.confirmations += 1
            self.last_confirmation = self.clock.now
            self.state = 'responding'
        elif control_name == CIRCLE.name:
            assert self.state == 'alternate', 'Checkbox toggled off or wrong modal clicked'
            self.state = 'checked'
        elif control_name == ALTERNATE.name:
            assert self.state == 'checked', 'Confirmed before checking do-not-show-again'
            self.state = 'done'
        else:
            assert self.state == 'select', 'Selected a shikigami while a modal was open'
            self.state = 'confirm'


@pytest.mark.parametrize('limit', [2, 3, 4])
@pytest.mark.parametrize('slot_visible', [False, True])
def test_modal_after_last_allowed_confirmation_has_priority(clock, limit, slot_visible):
    harness = Harness(clock, modal_on=limit, succeed_on=None, slot_visible=slot_visible)
    assert harness.set_shikigami(4, STOP, max_confirm_attempts=limit) is True
    assert harness.confirmations == limit
    assert harness.clicks[-2:] == [CIRCLE.name, ALTERNATE.name]


@pytest.mark.parametrize('state,expected', [
    ('alternate', [CIRCLE.name, ALTERNATE.name]),
    ('checked', [ALTERNATE.name]),
])
def test_modal_blocks_visible_underlying_confirmation(clock, state, expected):
    harness = Harness(clock, state=state)
    assert harness.set_shikigami(4, STOP, max_confirm_attempts=2) is True
    assert harness.clicks == expected
    assert harness.confirmations == 0


@pytest.mark.parametrize('button', [CIRCLE, ALTERNATE])
def test_throttled_modal_buttons_never_fall_through(clock, button):
    harness = Harness(clock, state='alternate')
    harness.intervals[button.name] = clock.now + 3
    assert harness.set_shikigami(4, STOP, max_confirm_attempts=2) is True
    assert harness.clicks == [CIRCLE.name, ALTERNATE.name]
    assert harness.screenshots > 4


def test_vanished_confirmation_is_not_clicked_or_counted(clock):
    harness = Harness(clock, state='confirm')
    harness.hide_on_recheck = True
    assert harness.set_shikigami(4, STOP, max_confirm_attempts=2) is True
    assert harness.confirmations == 0 and not harness.clicks


@pytest.mark.parametrize('limit', [2, 3, 4])
@pytest.mark.parametrize('stays_open', [False, True])
def test_real_competed_slot_still_obeys_confirmation_limit(clock, limit, stays_open):
    harness = Harness(clock, modal_on=None, succeed_on=None, stays_open=stays_open)
    assert harness.set_shikigami(4, STOP, max_confirm_attempts=limit) is False
    assert harness.confirmations == limit
    assert all(name not in harness.clicks for name in (CIRCLE.name, ALTERNATE.name))


@pytest.mark.parametrize('limit', [2, 3, 4])
def test_no_modal_success_still_works(clock, limit):
    harness = Harness(clock, modal_on=None, succeed_on=limit)
    assert harness.set_shikigami(4, STOP, max_confirm_attempts=limit) is True
    assert harness.confirmations == limit


def test_own_realm_fill_does_not_gain_a_confirmation_limit(clock):
    harness = Harness(clock, modal_on=None, succeed_on=5)
    harness.response_delay = 0  # Own-realm fill has no server-response wait.
    assert harness.set_shikigami(4, STOP) is True
    assert harness.confirmations == 5


def test_stuck_modal_keeps_original_timeout(clock):
    harness = Harness(clock, state='checked')
    harness.intervals[ALTERNATE.name] = clock.now + 200
    with pytest.raises(GameStuckError):
        harness.set_shikigami(4, STOP, max_confirm_attempts=2)
    assert not harness.clicks
