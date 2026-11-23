"""Context-local routing also covers helper task objects and Config calls."""
from contextvars import ContextVar

_capture = ContextVar('oas_schedule_capture', default=None)


def capture_schedule(config, task, *, start_time=None, success=None, server=True, target=None):
    handler = _capture.get()
    if handler is None or handler[0] is not config:
        return False
    handler[1](task=task, start_time=start_time, success=success, server=server, target=target)
    return True


def capture_enabled(config, task, enabled):
    handler = _capture.get()
    if handler is None or handler[0] is not config:
        return False
    handler[2](task, enabled)
    return True
