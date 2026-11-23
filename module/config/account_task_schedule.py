"""Capture scheduling only for existing independently scheduled account tasks.

Timed entries and orchestration singles use their native private Scheduler.
Ordinary lists, group members and collections do not use this adapter; there
are no task_schedules overlays, group timestamps or separate runtime queues.
"""
from datetime import datetime, time, timedelta
import random

from module.config.utils import convert_to_underscore, nearest_future, parse_next_server_schedule
from module.exception import TaskEnd
from module.execution.schedule_capture import _capture
from module.logger import logger


def next_run_from_scheduler(scheduler, start, success=None, server=True, target=None):
    candidates = []
    if success is not None:
        interval = getattr(scheduler, 'success_interval' if success else 'failure_interval', timedelta(minutes=10))
        candidates.append(start + interval)
    if target is not None:
        candidates.append(nearest_future(target) if isinstance(target, (list, tuple)) else target)
    if not candidates:
        raise ValueError('任务调度必须指定 success 或 target')
    value = min(candidates).replace(microsecond=0)
    if not server or scheduler is None or not hasattr(scheduler, 'server_update'):
        return value
    jitter = getattr(scheduler, 'float_time', time.min)
    seconds = random.randint(0, jitter.hour * 3600 + jitter.minute * 60 + jitter.second)
    if scheduler.server_update == time(hour=9):
        return value + timedelta(seconds=seconds)
    return parse_next_server_schedule(scheduler, seconds)


class AccountTaskScheduleMixin:
    def _schedule_entry(self, account, task_name):
        key = convert_to_underscore(task_name)
        return next((item for item in account.task_list
                     if convert_to_underscore(item.task_name) == key), None)

    def _request_account_schedule(self, account, task_name, *, target=None, enabled=None):
        entry = self._schedule_entry(account, task_name)
        if entry is None:
            return
        scheduler = entry.private_config.setdefault('scheduler', {})
        if target is not None:
            scheduler['next_run'] = target.isoformat()
            if hasattr(entry, 'next_run'):
                entry.next_run = target  # Timed compatibility mirror, not a second scheduler.
        if enabled is not None:
            scheduler['enable'] = enabled
        # Foreign requests survive source failure or interruption.
        self._save_repeat_config()

    def _call_account_task(self, task_obj, task_name, account, *, resolve_next=None, check_due=True) -> bool | None:
        """Return the original bool result, or None when disabled/not due."""
        self._task_next_run = None
        key = convert_to_underscore(task_name)
        configured = {convert_to_underscore(name) for name in self._tasks_for_link(account)}
        if key not in configured:
            return None
        if check_due:
            scheduler = self._scheduler_for_link(account, task_name)
            if scheduler is not None and scheduler.next_run > datetime.now():
                self._task_next_run = scheduler.next_run
                return None
        captured = []
        original = task_obj.__dict__.get('set_next_run')
        had_original = 'set_next_run' in task_obj.__dict__

        def capture(task=None, finish=False, success=None, server=True, target=None, start_time=None, **kwargs):
            requested = str(task or task_name)
            requested_key = convert_to_underscore(requested)
            # Legacy wrappers still read their inner Scheduler during cleanup.
            inner = {'multi_account_kekkai_activation': 'kekkai_activation',
                     'multi_account_kekkai_utilize': 'kekkai_utilize'}.get(key)
            if requested_key == inner:
                scheduler = getattr(task_obj.config, inner).scheduler
                start = start_time or (datetime.now() if finish else task_obj.start_time)
                scheduler.next_run = next_run_from_scheduler(scheduler, start, success, server, target)
                return
            # Only configured/enabled independent entries in this account are targets.
            configured = {convert_to_underscore(name) for name in self._tasks_for_link(account)}
            if requested_key not in configured:
                logger.info('忽略联动 %s：当前账号未配置或未启用该独立任务', requested)
                return
            start = start_time or (datetime.now() if finish else task_obj.start_time)
            scheduler = self._scheduler_for_link(account, requested)
            if requested_key == key and resolve_next is not None:
                value = resolve_next(start, success, server, target)
            else:
                value = next_run_from_scheduler(scheduler, start, success, server, target)
            if requested_key == key:
                captured.append(value)
            else:
                self._request_account_schedule(account, requested, target=value)

        def capture_enable(requested, enabled):
            configured = {convert_to_underscore(name) for name in self._tasks_for_link(account)}
            if convert_to_underscore(requested) in configured:
                self._request_account_schedule(account, requested, enabled=enabled)

        task_obj.set_next_run = capture
        token = _capture.set((task_obj.config, capture, capture_enable))
        try:
            value = None
            try:
                value = task_obj.run()
            except TaskEnd:
                pass
            success = value is not False
            self._task_next_run = captured[-1] if captured else None
            return success
        finally:
            _capture.reset(token)
            if had_original:
                task_obj.set_next_run = original
            else:
                task_obj.__dict__.pop('set_next_run', None)
