import platform
import sqlite3
from threading import Event
from time import perf_counter as monotonic
import tkinter as tk
from tkinter import ttk

from .buffer import LatestFrameBuffer
from .controller import Controller, SessionBlocked
from .demo import DemoAnalyzer, DemoControls, DemoSource
from .metrics import Metrics
from .storage import SessionStore
from .system_lock import FocusMonitor, ScopedKeyboardHooks, WindowsTarget
from .types import Policy, State
from .vision import VisionAnalyzer
from .workers import CaptureWorker, CVWorker, open_camera


QUESTIONS = (
    ('q1', 'Что предотвращает накопление кадров?', ('Очередь всех кадров', 'Буфер последнего кадра', 'Задержка камеры')),
    ('q2', 'Какие часы подходят для контроля таймаутов?', ('Календарные', 'Монотонные', 'Часовой пояс ОС')),
    ('q3', 'Что делать при отказе контроля?', ('Продолжить тест', 'Удалить ответы', 'Приостановить и сохранить')),
)

REASONS = {
    'camera': 'Нет свежего кадра камеры', 'focus_monitor': 'Контроль фокуса недоступен',
    'focus_lost': 'Вернитесь в окно теста', 'cv_error': 'Ошибка CV — подробности в журнале',
    'cv_timeout': 'Ожидание завершённого анализа', 'cv_stale': 'Ожидание свежего проанализированного кадра',
    'storage': 'Ошибка записи данных — ответы не принимаются', 'initializing': 'Подготовка контроля',
    'audit_overflow': 'Журнал событий переполнен — перезапустите приложение',
    'ui_error': 'Ошибка интерфейса — сохранённые ответы доступны после перезапуска',
}


class ProctoringApp:
    def __init__(self, root, args):
        self.root, self.args = root, args
        self.stop = Event()
        self.buffer = LatestFrameBuffer()
        self.metrics = Metrics()
        self.store = SessionStore(args.data_dir / 'session.sqlite3')
        self.controller = Controller(self.store, Policy(args.max_frame_age, args.analysis_timeout, args.monitor_timeout))
        self.controls = DemoControls()
        self.workers = []
        self.closed = False
        self.last_tick = monotonic()
        self.last_refresh = 0
        self.last_export = 0
        self.context = {'mode': 'SIMULATION' if args.demo else 'LIVE', 'python': platform.python_version(),
                        'platform': platform.platform(), 'policy': vars(self.controller.policy),
                        'yolo_model': str(args.yolo_model), 'face_model': str(args.face_model),
                        'hooks_enabled': args.lock_keys}
        self._build_ui()
        self.root.update_idletasks()
        self._start_workers()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind('<Control-Shift-F12>', lambda _: self.close())
        self.root.report_callback_exception = self._callback_error
        self.root.after(50, self._tick)

    def _build_ui(self):
        root = self.root
        root.title('Local Proctoring | Демонстрационный тест')
        root.geometry('1100x780')
        root.minsize(950, 680)
        root.configure(bg='#101827')
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TFrame', background='#101827')
        style.configure('TLabel', background='#101827', foreground='#e5eaf3', font=('Segoe UI', 11))
        style.configure('TButton', font=('Segoe UI', 10), padding=7)
        style.configure('TRadiobutton', background='#101827', foreground='#e5eaf3', font=('Segoe UI', 11))
        style.map('TRadiobutton', foreground=[('disabled', '#8894a9')], background=[('active', '#243249')])
        shell = ttk.Frame(root, padding=24)
        shell.pack(fill='both', expand=True)
        ttk.Label(shell, text='LOCAL PROCTORING', font=('Segoe UI', 22, 'bold')).pack(anchor='w')
        subtitle = 'СИМУЛЯЦИЯ · камера и модели не используются' if self.args.demo else 'Локальный контроль · видео не сохраняется'
        ttk.Label(shell, text=subtitle, foreground='#7fdac4').pack(anchor='w', pady=(2, 12))
        self.status = ttk.Label(shell, text='PAUSED', font=('Segoe UI', 16, 'bold'))
        self.status.pack(anchor='w')
        self.reason = ttk.Label(shell, text='Подготовка контроля', wraplength=1020)
        self.reason.pack(anchor='w', pady=(4, 14))
        body = ttk.Frame(shell)
        body.pack(fill='both', expand=True)
        left = ttk.Frame(body)
        left.pack(side='left', fill='both', expand=True, padx=(0, 20))
        right = ttk.Frame(body, width=340)
        right.pack(side='right', fill='both')
        self.variables, self.inputs = {}, []
        for question, title, choices in QUESTIONS:
            ttk.Label(left, text=title, font=('Segoe UI', 12, 'bold'), wraplength=580).pack(anchor='w', pady=(8, 5))
            variable = tk.StringVar(value=self.controller.answers.get(question, ''))
            self.variables[question] = variable
            for index, choice in enumerate(choices):
                value = chr(65 + index)
                button = ttk.Radiobutton(left, text=choice, variable=variable, value=value,
                                         command=lambda q=question: self._answer(q))
                button.pack(anchor='w', pady=3)
                button.state(['disabled'])
                self.inputs.append(button)
        self.submit_button = ttk.Button(left, text='Завершить тест', command=self._submit)
        self.submit_button.pack(anchor='w', pady=18)
        self.submit_button.state(['disabled'])
        self.notice = ttk.Label(left, text='Выбранные ответы сохраняются сразу.', wraplength=560, foreground='#a9bbd2')
        self.notice.pack(anchor='w')
        self.preview = ttk.Label(right, text='СИМУЛЯЦИЯ' if self.args.demo else 'Ожидание камеры', anchor='center')
        self.preview.pack(fill='x', pady=8)
        self.analysis_label = ttk.Label(right, text='Анализ ещё не завершён', wraplength=340)
        self.analysis_label.pack(anchor='w', pady=8)
        self.metric_label = ttk.Label(right, text='', wraplength=340, foreground='#a9bbd2')
        self.metric_label.pack(anchor='w', pady=8)
        ttk.Label(right, text='Последние события', font=('Segoe UI', 12, 'bold')).pack(anchor='w', pady=(10, 4))
        self.journal = tk.Text(right, width=42, height=8, bg='#172337', fg='#e5eaf3', relief='flat',
                               font=('Consolas', 9), state='disabled', wrap='word')
        self.journal.pack(fill='both', expand=True)
        if self.args.demo:
            panel = ttk.Frame(shell)
            panel.pack(fill='x', pady=(14, 4))
            for title, event in [('Сбой камеры', self.controls.camera_failed), ('Зависание CV', self.controls.cv_hung),
                                 ('Потеря фокуса', self.controls.focus_lost), ('Телефон', self.controls.phone)]:
                var = tk.BooleanVar()
                check = ttk.Checkbutton(panel, text=title, variable=var,
                                        command=lambda e=event, v=var: e.set() if v.get() else e.clear())
                check.pack(side='left', padx=(0, 14))
        ttk.Label(shell, text='Выход: закрыть окно или Ctrl+Shift+F12. Возобновление — после восстановления контроля.',
                  foreground='#a9bbd2', font=('Segoe UI', 9)).pack(anchor='w', pady=(12, 0))

    def _start_workers(self):
        if self.controller.state == State.STOPPED:
            self.notice.configure(text='Эта сессия уже отправлена. Для нового теста используйте новую папку данных.')
            return
        if self.args.demo:
            source = lambda: DemoSource(self.controls)
            analyzer = lambda: DemoAnalyzer(self.controls, self.stop)
            probe = lambda: not self.controls.focus_lost.is_set()
            hooks = None
        else:
            source = lambda: open_camera(self.args.camera)
            analyzer = lambda: VisionAnalyzer(self.args.yolo_model, self.args.face_model, dwell=self.args.gaze_dwell)
            try:
                import win32gui
                hwnd = win32gui.GetAncestor(self.root.winfo_id(), 2)
                target = WindowsTarget(hwnd)
                probe = target.is_focused
            except Exception as exc:
                message = str(exc)
                def probe():
                    raise RuntimeError(message)
            hooks = ScopedKeyboardHooks(probe, self.stop) if self.args.lock_keys else None
        self.workers = [CaptureWorker(self.buffer, source, self.controller, self.stop),
                        CVWorker(self.buffer, analyzer, self.controller, self.stop, self.metrics),
                        FocusMonitor(probe, self.controller, self.stop, hooks=hooks)]
        for worker in self.workers:
            worker.start()

    def _answer(self, question):
        try:
            self.controller.answer(question, self.variables[question].get())
            self.notice.configure(text='Ответ сохранён на диск.')
        except SessionBlocked:
            self.variables[question].set(self.controller.answers.get(question, ''))
            self.notice.configure(text='Ответ не принят: сессия приостановлена.')

    def _submit(self):
        try:
            self.controller.submit()
            self.notice.configure(text='Тест завершён. Ответы и журнал сохранены.')
            self.stop.set()
            self._export()
        except (SessionBlocked, OSError, sqlite3.Error) as exc:
            self.notice.configure(text=str(exc))

    def _tick(self):
        if self.closed:
            return
        try:
            self._refresh()
        except sqlite3.Error as exc:
            self.controller.storage_error(str(exc))
        except Exception as exc:
            self._callback_error(type(exc), exc, exc.__traceback__)
        finally:
            if not self.closed:
                try:
                    self._safe_render()
                finally:
                    self.root.after(100, self._tick)

    def _callback_error(self, exception_type, exception, traceback):
        self.controller.fatal_error('ui_error', f'{exception_type.__name__}: {exception}')
        self._safe_render()

    def _safe_render(self):
        try:
            self._render_state()
        except tk.TclError as exc:
            self.controller.fatal_error('ui_error', str(exc))

    def _render_state(self):
        running = self.controller.state == State.RUNNING
        self.status.configure(text=self.controller.state.value,
                              foreground='#7fdac4' if running else '#ffce80')
        reasons = ' · '.join(REASONS.get(x, x) for x in self.controller.reasons)
        self.reason.configure(text=reasons or ('Контроль активен. Можно отвечать.' if running else 'Сессия завершена.'))
        for widget in self.inputs + [self.submit_button]:
            widget.state(['!disabled'] if running else ['disabled'])

    def _refresh(self):
        now = monotonic()
        self.metrics.observe('ui_tick_gap_ms', (now - self.last_tick) * 1000)
        self.last_tick = now
        if self.stop.is_set() and self.controller.state != State.STOPPED:
            self.close()
            return
        self.controller.tick()
        result = self.controller.latest_analysis
        if result:
            self.analysis_label.configure(text=f'Кадр #{result.frame_id} · возраст {now - result.captured_at:.2f} с\n'
                                          f'{result.description}\nСобытия: {", ".join(result.violations) or "нет"}')
        frame = self.buffer.peek()
        if not self.args.demo and frame is not None:
            from PIL import Image, ImageTk
            image = Image.fromarray(frame.image[:, :, ::-1])
            image.thumbnail((340, 220))
            self.photo = ImageTk.PhotoImage(image)
            self.preview.configure(image=self.photo, text='')
        if now - self.last_refresh >= 1:
            self.last_refresh = now
            stats = self.metrics.snapshot()
            self.metric_label.configure(text='\n'.join(f'{name}: p95 {stats[name]["p95"]:.0f} мс'
                                         for name in ('yolo_ms', 'mediapipe_ms', 'frame_age_ms', 'ui_tick_gap_ms') if name in stats))
            self.journal.configure(state='normal')
            self.journal.delete('1.0', 'end')
            for event in self.store.events(limit=8):
                self.journal.insert('end', f'{event["kind"]}: {event["code"]}\n{event["detail"]}\n')
            self.journal.configure(state='disabled')
        if now - self.last_export >= 10:
            self.last_export = now
            try:
                self.metrics.export(self.args.data_dir / 'metrics.json', self.context)
            except OSError as exc:
                self.notice.configure(text=f'Не удалось экспортировать метрики: {exc}')

    def _export(self):
        self.store.export(self.args.data_dir / 'report.json')
        self.metrics.export(self.args.data_dir / 'metrics.json', self.context)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        self.buffer.close()
        try:
            self.controller.stop()
        except SessionBlocked as exc:
            print(str(exc))
        deadline = monotonic() + 1
        for worker in self.workers:
            worker.join(max(0, deadline - monotonic()))
        self.context['workers_alive_at_exit'] = [w.name for w in self.workers if w.is_alive()]
        try:
            self._export()
        except (OSError, sqlite3.Error) as exc:
            print(f'Export failed; committed answers remain in SQLite: {exc}')
        finally:
            self.store.close()
            self.root.destroy()


def run(args):
    root = tk.Tk()
    app = ProctoringApp(root, args)
    try:
        root.mainloop()
    finally:
        app.close()
