# Local Proctoring MVP

Локальный прототип для Windows 11: окно тестирования, независимый захват камеры,
YOLO11n + MediaPipe, контроль фокуса и сохранение ответов при отказе контроля.

## Быстрый запуск без камеры

Дважды нажмите **Start-demo.cmd** или выполните из этой папки:

```powershell
python -m proctoring --demo
```

Режим **СИМУЛЯЦИЯ** работает на стандартной библиотеке Python с Tkinter.
Переключатели внизу моделируют отключение камеры, зависание анализа, потерю
фокуса и телефон. Во время PAUSED ответы и отправка блокируются. Для восстановления
выключите переключатели отказов и дождитесь нового анализа. Телефон создаёт
событие для проверки, без автоматического завершения теста.

Автоматический сценарий без окна: `python -m proctoring --self-test`.
Он проверяет `RUNNING → PAUSED → PAUSED → RUNNING → STOPPED` с настоящим SQLite.
Это проверка логики, не производительности моделей.

## Установка реального режима

Нужны Windows 11 и 64-битный Python 3.10+ с Tkinter. Для установки создайте
отдельное виртуальное окружение:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[vision,windows,build]"
```

OpenCV и OpenCV contrib закреплены на одной версии: обе зависимости поставляют
модуль `cv2`. Не обновляйте одну отдельно. Полный снимок проверенного окружения:
`requirements-validated.txt`.

Разместите официальные веса в `models/`. Загрузите их один раз до тестирования:

```powershell
New-Item -ItemType Directory models -Force
Invoke-WebRequest 'https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt' -OutFile models/yolo11n.pt
Invoke-WebRequest 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task' -OutFile models/face_landmarker.task
```

Во время работы используются локальные модели. Для Ultralytics включён offline-режим
и отключена синхронизация. Видео и изображения лиц не сохраняются.

## Замеры и запуск

Проверка совместимости реальных моделей на искусственных чёрных кадрах:

```powershell
.venv\Scripts\python.exe -m proctoring.profile --synthetic --seconds 10 --yolo-model models/yolo11n.pt --face-model models/face_landmarker.task --output data/profile-synthetic.json
```

Замер на демонстрационном компьютере с настоящей камерой (эта команда включает камеру):

```powershell
.venv\Scripts\python.exe -m proctoring.profile --camera 0 --seconds 60 --yolo-model models/yolo11n.pt --face-model models/face_landmarker.task --output data/profile-camera.json
```

Метрики: `yolo_ms`, `mediapipe_ms`, `analysis_ms`, `frame_age_ms`, `queue_age_ms`,
`analysis_gap_ms`, пропущенные кадры. Среднее, p95 и максимум рассчитываются по
последним 2000 отсчётам; полное количество измерений сохраняется отдельно.
Первое выполнение не исключается. Время загрузки до первого анализа записывается
отдельно. Чёрные кадры не позволяют оценить точность и задержку при обработке лиц.

Выберите допустимый возраст кадра и интервал без результата по реальным замерам
и требованиям тестирования. Простое увеличение таймаута не устраняет слепые зоны.
Таймаут наблюдения за камерой/фокусом задаётся отдельно. Реальный запуск запрашивает
все три значения; готовых «откалиброванных» значений в коде нет:

```powershell
$frameAge = Read-Host 'Допустимый возраст кадра, секунды'
$analysisTimeout = Read-Host 'Максимальный интервал без анализа, секунды'
$monitorTimeout = Read-Host 'Таймаут камеры/фокуса, секунды'
.venv\Scripts\python.exe -m proctoring --yolo-model models/yolo11n.pt --face-model models/face_landmarker.task --max-frame-age $frameAge --analysis-timeout $analysisTimeout --monitor-timeout $monitorTimeout --lock-keys
```

`--lock-keys` включает перехват Alt+Tab, Alt+Esc/F4, Ctrl+C/V/X, Win, PrtScn и
основных сочетаний переключения вкладок **в целевом окне**. Без флага остаются
контроль фокуса и пауза при потере фокуса. Ctrl+Shift+F12 и закрытие окна доступны
для выхода. Принудительного возвращения фокуса нет: участник возвращается сам.

## Сохранение

Каждый запуск создаёт отдельный `data/<дата-время-id>/`:

- `session.sqlite3`: ответы, метаданные и журнал; транзакции, WAL, synchronous=FULL.
- `report.json`: экспорт при завершении/закрытии; события `technical`, `violation`,
  `session`, UTC и монотонное время текущего процесса.
- `metrics.json`: профиль с явной маркировкой LIVE или SIMULATION.

Для восстановления прерванной сессии передайте каталог через `--data-dir`.
Ответы восстанавливаются, исправность контроля устанавливается заново; старт — PAUSED.
Отправленная сессия остаётся STOPPED. Один каталог одновременно обслуживает один
процесс. Переносите весь каталог после закрытия приложения, учитывая файлы WAL/SHM.

## Границы MVP

- Управляемый тест находится в Tkinter. Произвольная вкладка браузера не подключена:
  для LMS нужен адаптер, проверяющий состояние контроллера при вводе/отправке ответов.
  Одного заголовка окна недостаточно.
- Фокус определяется по HWND и PID раз в 100 мс. Короткое переключение между
  опросами может быть пропущено. Это не kiosk-политика; Ctrl+Alt+Del, защищённые
  системные экраны и все способы захвата экрана не блокируются.
- YOLO определяет телефон, но не намерение сфотографировать экран и не направление
  камеры телефона. События требуют проверки, автоматического обвинения нет.
- MediaPipe определяет лица и выдаёт сетку, коэффициенты и матрицу позы.
  Взгляд и положение головы — некалиброванные эвристики: коэффициент 0.55,
  поворот 25° / наклон 20°, длительность 2 с (`--gaze-dwell`). Разрыв более 1 с
  сбрасывает длительность. Проверяйте точность на участниках.
- `captured_at` — время получения кадра из драйвера, не аппаратная экспозиция.
  Скрытая очередь внутри драйвера остаётся возможной.
- Зависший нативный вызов не перезапускается параллельно. Отсутствие свежих данных
  приостанавливает тест. Закрытие ждёт потоки суммарно не более 1 с и записывает
  `workers_alive_at_exit`; при нативном зависании может понадобиться перезапуск.
- Потоки не защищают от отказа всего процесса или длительного удержания GIL.
  Неподтверждённая запись не считается сохранённой. Журнал не защищён от правок
  владельцем компьютера.

## Тесты и сборка

```powershell
python -m unittest discover -s tests -v
$env:PROCTORING_GUI_TEST = '1'
python -m unittest discover -s tests -v
.venv\Scripts\python.exe -m build --no-isolation
```

GUI-тест использует скрытое окно и SQLite без камеры/хуков. Остальные тесты не
требуют CV-зависимостей. Wheel в `dist/` — Python-пакет, не самостоятельный exe.
Ручные проверки: [чек-лист](manual-windows-checklist.md).
Выполненная проверка: [протокол](verification.md).

## Код

`controller.py` — состояния и входные проверки; `buffer.py` — последний кадр;
`workers.py` — камера и CV; `system_lock.py` — Win32 и хуки; `vision.py` — модели;
`storage.py` — SQLite; `metrics.py` и `profile.py` — замеры; `app.py` — локальный тест;
`demo.py` — явно обозначенная симуляция.

API сверены с [Ultralytics Predict](https://docs.ultralytics.com/modes/predict/)
и [MediaPipe Face Landmarker](https://developers.google.com/edge/mediapipe/solutions/vision/face_landmarker/python).
