# Local Proctoring MVP Implementation Plan

**Goal:** Build the approved Windows 11 prototype with latest-frame capture, isolated inference and focus monitoring, persistent pause/resume semantics and measurements.

**Approved design:** The conversation of 2026-10-08 approves one CV worker, an overwrite-only frame slot, a separate system_lock module, RUNNING/PAUSED/STOPPED, preservation of answers, monotonic freshness checks, technical/violation distinction, hybrid tests and measurement before tuning.

**Execution:** Inline, in a new project directory; the input directory is not a Git repository. No additional design approval is required.

**Stack:** Python 3.10+, unittest, SQLite, Tkinter; optional OpenCV, Ultralytics YOLO and MediaPipe Tasks Face Landmarker (current face-mesh API), pywin32 and keyboard.

## Constraints

- The local exam is controlled by the session controller. An arbitrary external browser cannot be paused through window polling alone.
- Camera capture, CV and Windows monitoring each own their thread and resources. Model inference never holds the frame-slot lock.
- No unbounded image queue, no automatic replacement of a hung worker, bounded shutdown waits.
- Every accepted answer is committed to SQLite before acknowledgement. Resume restores answers but never restores healthy status from disk.
- Timeout values are explicitly supplied for live runs, not claimed to be calibrated.
- Camera and model failures pause without penalizing a participant. Phone/face/gaze observations are reviewable events, not automatic cheating verdicts.
- No automatic focus stealing. Hooks are scoped to the target foreground window, with an emergency stop.

## Deliverables and test cycles

- [x] Core: `proctoring/controller.py`, `storage.py`, `buffer.py`, `types.py`; write `tests/test_core.py` first. Verify missing core fails, implement and run `python -m unittest discover -s tests -v`. Cover stale results, repeated frame IDs, multi-fault recovery, rejected edits/submission, disk failure and restart persistence.
- [x] Workers: `workers.py`, `system_lock.py`, `vision.py`, `metrics.py`; write `tests/test_workers.py` first. Block inference with a threading.Event; publish frames 2..100; release inference and require frame 100 next. Test focus API exceptions, transient camera failure, model exceptions, bounded metrics, gaze dwell and stop wakeup.
- [x] Runnable application: `app.py`, `__main__.py`, `demo.py`; write integration tests for deterministic failure/recovery and command-line validation first. Provide a Tkinter local exam, explicit simulated mode, opt-in hooks, model paths, persistent journal and metrics export.
- [x] Delivery: `pyproject.toml`, README, manual Windows checklist, offline core demo, wheel build and a fresh full test run. Real camera/Windows/model measurements must be reported separately from simulations.

## Commands

```powershell
python -m unittest discover -s tests -v
python -m proctoring --self-test --data-dir data/self-test
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

## Review checklist

Freshness is checked both at delivery and continuously, using capture and completion times. Focus is identified with HWND and PID rather than a mutable title. Input methods re-evaluate health before accepting changes. A failure to persist is a blocking technical error. Recorded video is not required or stored. Native inference hangs remain a known thread-isolation limit and require restarting the application.

