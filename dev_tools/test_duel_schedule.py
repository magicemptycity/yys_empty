"""Exercise duel exit scheduling without connecting to a game device."""
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from module.exception import TaskEnd
from tasks.Duel import script_task as module


def make_task(monkeypatch, now, *, score, target, celeb=False, celeb_battle=False,
              honor_full=False, honor_exit=False, practice=False, overtime=False):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls):
            return now

    monkeypatch.setattr(module, 'datetime', FixedDatetime)
    task = module.ScriptTask.__new__(module.ScriptTask)
    task.config = SimpleNamespace(duel=SimpleNamespace(
        duel_config=SimpleNamespace(
            limit_time=time(0, 30), target_score=target, honor_full_exit=honor_exit,
        ),
        duel_celeb_config=SimpleNamespace(celeb_battle=celeb_battle),
    ))
    task.start_time = now - timedelta(minutes=30 if overtime else 0)
    task.current_score = score
    task.is_celeb = celeb
    task.prepare_duel = Mock()
    task.screenshot = Mock()
    task.check_and_get_reward = Mock()
    task.duel_main = Mock(return_value=True)
    task.get_and_update_cur_score = lambda: task.current_score
    task.appear = lambda asset: practice and asset is task.I_BATTLE_WITH_TRAIN
    task.check_honor = Mock(return_value=honor_full)
    task.yield_to_higher_priority_task = Mock()
    task.start_duel = Mock(side_effect=AssertionError('Unexpected duel'))
    task.goto_page = Mock()
    task.set_next_run = Mock()
    return task


@pytest.mark.parametrize('now,options,expected', [
    pytest.param(datetime(2026, 11, 29, 13), dict(score=2000, target=2000),
                 datetime(2026, 11, 30, 12), id='ordinary-target-on-sunday'),
    pytest.param(datetime(2026, 11, 23, 12),
                 dict(score=3000, target=3000, celeb=True, celeb_battle=True),
                 datetime(2026, 11, 30, 12), id='celeb-target-at-opening-on-monday'),
    pytest.param(datetime(2026, 11, 25, 13),
                 dict(score=3800, target=4500, celeb=True, celeb_battle=True,
                      honor_exit=True, honor_full=True),
                 datetime(2026, 11, 30, 12), id='honor-full-during-celeb-battle'),
])
def test_weekly_completion_schedules_next_monday(monkeypatch, now, options, expected):
    task = make_task(monkeypatch, now, **options)

    with pytest.raises(TaskEnd):
        task.run()

    task.set_next_run.assert_called_once_with(task='Duel', server=False, target=expected)
    task.goto_page.assert_called_once_with(module.page_main)


@pytest.mark.parametrize('hour,options,finish', [
    pytest.param(13, dict(celeb=True), True, id='celeb-without-celeb-battle'),
    pytest.param(13, dict(overtime=True), True, id='time-limit'),
    pytest.param(13, dict(practice=True), True, id='practice'),
    pytest.param(23, {}, False, id='closed'),
])
def test_other_exits_keep_existing_schedule(monkeypatch, hour, options, finish):
    task = make_task(monkeypatch, datetime(2026, 11, 23, hour), score=3000, target=4000, **options)

    with pytest.raises(TaskEnd):
        task.run()

    task.set_next_run.assert_called_once_with(task='Duel', success=True, finish=finish)


def test_celeb_battle_continues_until_target_with_honor_exit_off(monkeypatch):
    task = make_task(monkeypatch, datetime(2026, 11, 23, 13), score=3900, target=4000,
                     celeb=True, celeb_battle=True, honor_full=True)
    task.start_duel = Mock(side_effect=lambda: setattr(task, 'current_score', 4000))

    with pytest.raises(TaskEnd):
        task.run()

    task.start_duel.assert_called_once_with()
    task.check_honor.assert_not_called()
    task.set_next_run.assert_called_once_with(task='Duel', server=False, target=datetime(2026, 11, 30, 12))


@pytest.mark.parametrize('counter,is_full', [((0, 0, 0), False), ((900, 100, 1000), False), ((1000, 0, 1000), True)])
def test_honor_requires_valid_full_counter(counter, is_full):
    task = module.ScriptTask.__new__(module.ScriptTask)
    task.device = SimpleNamespace(image=object())
    task.I_DUEL_HONOR = SimpleNamespace(roi_front=(233, 644, 131, 31))
    task.O_D_HONOR = SimpleNamespace(ocr=lambda image: counter)
    task.appear = Mock(return_value=True)

    assert task.check_honor() is is_full
