"""Windows calls and keyboard hooks are isolated from the exam/UI event loop."""
import os
from threading import Thread
from time import perf_counter as monotonic


def should_block_key(key, modifiers, target_focused, session_active):
    if not target_focused or not session_active:
        return False
    key = key.lower()
    if key == 'f12':  # Always retain emergency exit.
        return False
    if key in {'windows', 'left windows', 'right windows', 'print screen', 'printscreen'}:
        return True
    if 'alt' in modifiers and key in {'tab', 'esc', 'f4'}:
        return True
    if 'ctrl' in modifiers and (key in {'c', 'v', 'x', 'tab', 'page up', 'page down',
                                       'pageup', 'pagedown', 't', 'n', 'w', 'l'} or key in '123456789'):
        return True
    return False


class WindowsTarget:
    def __init__(self, hwnd, pid=None):
        import win32gui
        import win32process
        self.gui, self.process = win32gui, win32process
        self.hwnd, self.pid = hwnd, os.getpid() if pid is None else pid

    def is_focused(self):
        if not self.gui.IsWindow(self.hwnd):
            raise OSError('Target window has been destroyed')
        _, actual_pid = self.process.GetWindowThreadProcessId(self.hwnd)
        if actual_pid != self.pid:
            raise OSError('Target HWND was reused by another process')
        return self.gui.GetForegroundWindow() == self.hwnd


class FocusMonitor(Thread):
    def __init__(self, probe, controller, stop, interval=.1, hooks=None):
        super().__init__(name='system-lock', daemon=True)
        self.probe, self.controller, self.stop_event = probe, controller, stop
        self.interval, self.hooks = interval, hooks

    def run(self):
        try:
            if self.hooks:
                self.hooks.install()
            while not self.stop_event.is_set():
                try:
                    self.controller.focus(self.probe(), monotonic())
                except Exception as exc:
                    self.controller.focus(None, monotonic(), f'{type(exc).__name__}: {exc}')
                self.stop_event.wait(self.interval)
        except Exception as exc:
            self.controller.focus(None, monotonic(), f'Hook initialization failed: {exc}')
        finally:
            if self.hooks:
                self.hooks.close()


class ScopedKeyboardHooks:
    """Opt-in hooks, bound to our window; Ctrl+Shift+F12 always requests shutdown."""
    def __init__(self, probe, stop):
        self.probe, self.stop_event = probe, stop
        self.keyboard = None
        self.unhook = None
        self.modifiers = set()
        self.key_decisions = {}

    def install(self):
        import keyboard
        self.keyboard = keyboard
        self.unhook = keyboard.hook(self._event, suppress=True)

    def _event(self, event):
        name = (event.name or '').lower()
        modifier = next((m for m in ('ctrl', 'alt', 'shift') if m in name.split()), None)
        if modifier:
            if event.event_type == 'down':
                self.modifiers.add(modifier)
            else:
                self.modifiers.discard(modifier)
        if name == 'f12' and {'ctrl', 'shift'} <= self.modifiers:
            self.stop_event.set()
            return True
        if event.event_type == 'up':
            return not self.key_decisions.pop(event.scan_code, False)
        if event.scan_code in self.key_decisions:
            return not self.key_decisions[event.scan_code]
        if self.stop_event.is_set():
            return True
        try:
            blocked = should_block_key(name, self.modifiers, self.probe(), True)
        except Exception:
            # Do not trap desktop input when the target is unknown. Poller pauses the session.
            self.key_decisions[event.scan_code] = False
            return True
        self.key_decisions[event.scan_code] = blocked
        return not blocked

    def close(self):
        if self.unhook:
            self.unhook()
            self.unhook = None
