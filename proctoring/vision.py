"""Model adapter and explicitly heuristic, uncalibrated attention observations."""
from pathlib import Path
import os
from time import perf_counter as monotonic
from .types import Analysis


def configure_local_runtime(model_path):
    config = Path(model_path).resolve().parent / 'ultralytics-config'
    config.mkdir(parents=True, exist_ok=True)
    os.environ['YOLO_OFFLINE'] = 'true'
    os.environ['YOLO_CONFIG_DIR'] = str(config)


def observations(object_names, face_count):
    codes = []
    if 'cell phone' in object_names:
        codes.append('phone')
    if face_count == 0:
        codes.append('face_absent')
    elif face_count > 1:
        codes.append('multiple_faces')
    return tuple(codes)


class AttentionTracker:
    def __init__(self, dwell=2.0, max_gap=1.0):
        self.dwell, self.max_gap = dwell, max_gap
        self.direction = 'center'
        self.since = None
        self.last = None

    def update(self, direction, at):
        gap = self.last is not None and (at <= self.last or at - self.last > self.max_gap)
        if direction != self.direction or gap or direction == 'center':
            self.since = at
        elif self.since is None:
            self.since = at
        self.direction, self.last = direction, at
        return direction != 'center' and at - self.since >= self.dwell


def gaze_direction(blendshapes, threshold=.55):
    """Expression coefficients are proxies, not calibrated screen-gaze coordinates."""
    scores = {item.category_name: item.score for item in blendshapes}
    # Blinks are not downward gaze. Directions follow the participant's perspective.
    if max(scores.get('eyeBlinkLeft', 0), scores.get('eyeBlinkRight', 0)) > .5:
        return 'center'
    down = (scores.get('eyeLookDownLeft', 0) + scores.get('eyeLookDownRight', 0)) / 2
    left = (scores.get('eyeLookOutLeft', 0) + scores.get('eyeLookInRight', 0)) / 2
    right = (scores.get('eyeLookInLeft', 0) + scores.get('eyeLookOutRight', 0)) / 2
    direction, score = max((('down', down), ('left', left), ('right', right)), key=lambda x: x[1])
    return direction if score >= threshold else 'center'


class VisionAnalyzer:
    def __init__(self, yolo_path, face_path, confidence=.45, dwell=2.0):
        # Requiring local assets prevents hidden network downloads during a test.
        if not Path(yolo_path).is_file() or not Path(face_path).is_file():
            raise FileNotFoundError('Provide local YOLO .pt and MediaPipe .task model files')
        configure_local_runtime(yolo_path)
        import cv2
        import mediapipe as mp
        from ultralytics import YOLO, settings
        settings.update({'sync': False})
        self.cv2, self.mp = cv2, mp
        self.yolo = YOLO(str(yolo_path))
        self.confidence = confidence
        self.face = mp.tasks.vision.FaceLandmarker.create_from_options(
            mp.tasks.vision.FaceLandmarkerOptions(
                base_options=mp.tasks.BaseOptions(model_asset_buffer=Path(face_path).read_bytes()),
                running_mode=mp.tasks.vision.RunningMode.VIDEO,
                num_faces=2, output_face_blendshapes=True,
                output_facial_transformation_matrixes=True,
            )
        )
        self.attention = AttentionTracker(dwell=dwell)
        self.last_timestamp = -1

    def analyze(self, frame):
        start = monotonic()
        detections = self.yolo.predict(frame.image, conf=self.confidence, imgsz=640,
                                       verbose=False, save=False, device='cpu')
        after_yolo = monotonic()
        names = [result.names[int(cls)] for result in detections for cls in result.boxes.cls.tolist()]
        rgb = self.cv2.cvtColor(frame.image, self.cv2.COLOR_BGR2RGB)
        image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        timestamp = max(self.last_timestamp + 1, int(frame.captured_at * 1000))
        self.last_timestamp = timestamp
        faces = self.face.detect_for_video(image, timestamp)
        completed = monotonic()
        count = len(faces.face_landmarks)
        codes = list(observations(names, count))
        direction = gaze_direction(faces.face_blendshapes[0]) if count == 1 and faces.face_blendshapes else 'center'
        # Rotation from the canonical face is an approximate head orientation cue.
        head = 'center'
        if count == 1 and faces.facial_transformation_matrixes:
            import math
            matrix = faces.facial_transformation_matrixes[0]
            yaw = math.degrees(math.atan2(float(matrix[0, 2]), float(matrix[2, 2])))
            pitch = math.degrees(math.atan2(-float(matrix[1, 2]),
                                           math.hypot(float(matrix[0, 2]), float(matrix[2, 2]))))
            if abs(yaw) > 25:
                head = 'left' if yaw > 0 else 'right'
            elif abs(pitch) > 20:
                head = 'vertical'
        attention = direction if direction != 'center' else ('head_' + head if head != 'center' else 'center')
        if self.attention.update(attention, frame.captured_at):
            codes.append('attention_' + attention)
        return Analysis(frame.frame_id, frame.captured_at, completed, tuple(codes),
                        (('yolo_ms', (after_yolo - start) * 1000),
                         ('mediapipe_ms', (completed - after_yolo) * 1000)),
                        f'faces={count}; gaze={direction}; head={head}')

    def close(self):
        self.face.close()
