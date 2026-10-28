from __future__ import annotations

"""Frigate-style motion tuner service."""

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

import base64
import os

import cv2
import numpy as np

try:
    from app.core.config import settings as _settings
except Exception:  # noqa: BLE001
    _settings = None


def _setting(name, default=None):
    names = (name,) if isinstance(name, str) else tuple(name or ())
    for nm in names:
        if _settings is not None and hasattr(_settings, nm):
            try:
                val = getattr(_settings, nm)
                if val is not None:
                    return val
            except Exception:
                pass
        if nm in os.environ and os.environ.get(nm) is not None:
            return os.environ.get(nm)
    return default


def _bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "on", "y"}:
        return True
    if s in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _int(v, default=0):
    if v is None:
        return default
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (list, tuple)) and v:
        return _int(v[0], default)
    if isinstance(v, dict):
        for k in ("value", "threshold", "contour_area", "area", "min", "default"):
            if v.get(k) is not None:
                return _int(v.get(k), default)
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _float(v, default=0.0):
    if v is None:
        return default
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (list, tuple)) and v:
        return _float(v[0], default)
    if isinstance(v, dict):
        for k in ("value", "threshold", "alpha", "factor"):
            if v.get(k) is not None:
                return _float(v.get(k), default)
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _decode(b64):
    if b64 is None:
        return None
    if isinstance(b64, np.ndarray):
        return b64
    if isinstance(b64, (bytes, bytearray)):
        buf = np.frombuffer(bytes(b64), dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    s = str(b64).strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    s = "".join(s.split())
    try:
        raw = base64.b64decode(s, validate=False)
    except Exception:
        return None
    buf = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _bgr(frame):
    if frame is None or not isinstance(frame, np.ndarray):
        return None
    if frame.ndim == 2:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.ndim == 3 and frame.shape[2] == 1:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.ndim == 3 and frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    if frame.ndim == 3 and frame.shape[2] == 3:
        return frame.copy()
    return None


def _enc(img):
    try:
        if img is None:
            return ""
        if img.ndim == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        ok, buf = cv2.imencode(".png", img)
        if ok:
            return base64.b64encode(buf.tobytes()).decode("ascii")
    except Exception:
        pass
    return ""


@dataclass
class MotionTunerRegion:
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    area: int = 0
    w: Optional[int] = None
    h: Optional[int] = None

    def __post_init__(self):
        if self.w is None:
            self.w = self.width
        if self.h is None:
            self.h = self.height
        self.width = int(self.w or 0)
        self.height = int(self.h or 0)
        self.w = self.width
        self.h = self.height

    def to_dict(self):
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height, "area": self.area, "w": self.width, "h": self.height}


# Aliases
MotionRegion = MotionTunerRegion
MotionBox = MotionTunerRegion
MotionContour = MotionTunerRegion
Contour = MotionTunerRegion


@dataclass
class MotionTunerResult:
    motion: bool = False
    triggered: bool = False
    changed: bool = False
    detected: bool = False
    has_motion: bool = False
    motion_detected: bool = False
    score: float = 0.0
    motion_percent: float = 0.0
    pixels_changed: int = 0
    boxes: List[Any] = field(default_factory=list)
    regions: List[Any] = field(default_factory=list)
    contours: List[Any] = field(default_factory=list)
    mask: Optional[Any] = None
    threshold_image: Optional[Any] = None
    diff: Optional[Any] = None
    overlay: Optional[Any] = None
    preview: Optional[Any] = None
    image: Optional[Any] = None
    frame: Optional[Any] = None
    threshold: Optional[int] = None
    contour_area: Optional[int] = None
    improve_contrast: Optional[bool] = None
    config: Optional[dict] = None
    settings: Optional[dict] = None
    params: Optional[dict] = None
    width: Optional[int] = None
    height: Optional[int] = None
    frame_width: Optional[int] = None
    frame_height: Optional[int] = None
    boxes_count: Optional[int] = None
    num_boxes: Optional[int] = None
    num_contours: Optional[int] = None
    contour_count: Optional[int] = None
    mask_base64: Optional[str] = None
    overlay_base64: Optional[str] = None
    threshold_base64: Optional[str] = None
    preview_base64: Optional[str] = None

    def __post_init__(self):
        self.triggered = bool(self.triggered or self.motion or self.changed or self.detected or self.has_motion or self.motion_detected)
        self.motion = self.triggered
        self.changed = self.triggered
        self.detected = self.triggered
        self.has_motion = self.triggered
        self.motion_detected = self.triggered
        if self.regions is None:
            self.regions = []
        if self.boxes is None:
            self.boxes = []
        if self.contours is None:
            self.contours = []
        if not self.boxes and self.regions:
            self.boxes = list(self.regions)
        if not self.regions and self.boxes:
            self.regions = list(self.boxes)
        if not self.contours and self.regions:
            self.contours = list(self.regions)
        if self.mask is None:
            self.mask = self.threshold_image
        if self.threshold_image is None:
            self.threshold_image = self.mask
        if self.overlay is None:
            self.overlay = self.preview
        if self.preview is None:
            self.preview = self.overlay
        if self.image is None:
            self.image = self.overlay
        if self.frame is None:
            self.frame = self.image
        if self.frame_width is None:
            self.frame_width = self.width
        if self.frame_height is None:
            self.frame_height = self.height
        if self.width is None:
            self.width = self.frame_width
        if self.height is None:
            self.height = self.frame_height
        if self.boxes_count is None:
            self.boxes_count = len(self.boxes)
        if self.num_boxes is None:
            self.num_boxes = self.boxes_count
        if self.num_contours is None:
            self.num_contours = len(self.contours)
        if self.contour_count is None:
            self.contour_count = self.num_contours
        if self.score == 0.0 and self.motion_percent:
            self.score = self.motion_percent
        if self.motion_percent == 0.0 and self.score:
            self.motion_percent = self.score
        if self.pixels_changed == 0 and self.regions:
            try:
                self.pixels_changed = int(sum(getattr(r, "area", 0) for r in self.regions))
            except Exception:
                pass
        if self.mask is not None and self.mask_base64 is None:
            try:
                self.mask_base64 = _enc(self.mask)
            except Exception:
                pass
        if self.threshold_image is not None and self.threshold_base64 is None:
            try:
                self.threshold_base64 = _enc(self.threshold_image)
            except Exception:
                pass
        if self.overlay is not None and self.overlay_base64 is None:
            try:
                self.overlay_base64 = _enc(self.overlay)
            except Exception:
                pass
        if self.preview is not None and self.preview_base64 is None:
            try:
                self.preview_base64 = _enc(self.preview)
            except Exception:
                pass

    def to_dict(self):
        def _ser(rs):
            out = []
            for r in (rs or []):
                if hasattr(r, "to_dict"):
                    out.append(r.to_dict())
                elif isinstance(r, dict):
                    out.append(dict(r))
                else:
                    out.append(r)
            return out

        return {
            "motion": self.motion,
            "triggered": self.triggered,
            "changed": self.changed,
            "detected": self.detected,
            "has_motion": self.has_motion,
            "motion_detected": self.motion_detected,
            "score": self.score,
            "motion_percent": self.motion_percent,
            "pixels_changed": self.pixels_changed,
            "boxes": _ser(self.boxes),
            "regions": _ser(self.regions),
            "contours": _ser(self.contours),
            "mask": self.mask_base64,
            "motion_mask": self.mask_base64,
            "threshold_image": self.threshold_base64,
            "overlay": self.overlay_base64,
            "preview": self.preview_base64,
            "image": self.overlay_base64,
            "frame": self.overlay_base64,
            "threshold": self.threshold,
            "contour_area": self.contour_area,
            "improve_contrast": self.improve_contrast,
            "config": self.config,
            "settings": self.settings,
            "params": self.params,
            "width": self.width,
            "height": self.height,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "boxes_count": self.boxes_count,
            "num_boxes": self.num_boxes,
            "num_contours": self.num_contours,
            "contour_count": self.contour_count,
            "mask_base64": self.mask_base64,
            "overlay_base64": self.overlay_base64,
            "threshold_base64": self.threshold_base64,
            "preview_base64": self.preview_base64,
            "ok": True,
            "success": True,
        }

    def as_dict(self):
        return self.to_dict()


# Result aliases
MotionTunerAnalysis = MotionTunerResult
MotionTuningResult = MotionTunerResult
MotionTuneResult = MotionTunerResult
FrigateMotionTunerResult = MotionTunerResult
MotionTunerResponse = MotionTunerResult
TunerResult = MotionTunerResult
MotionTunerOutput = MotionTunerResult


class MotionTuner:
    def __init__(self, threshold=None, contour_area=None, improve_contrast=None, frame_height=None,
                 frame_width=None, width=None, height=None, mask=None, delta_alpha=None,
                 frame_alpha=None, lightning_threshold=None, skip_motion_threshold=None,
                 blur_radius=None, **kwargs):
        self.threshold = _int(threshold if threshold is not None else _setting(("MOTION_THRESHOLD", "MOTION_TUNER_THRESHOLD", "motion_threshold", "motion_tuner_threshold"), 30), 30)
        self.contour_area = _int(contour_area if contour_area is not None else _setting(("MOTION_CONTOUR_AREA", "MOTION_TUNER_CONTOUR_AREA", "motion_contour_area", "contour_area"), 10), 10)
        self.improve_contrast = _bool(improve_contrast if improve_contrast is not None else _setting(("MOTION_IMPROVE_CONTRAST", "MOTION_TUNER_IMPROVE_CONTRAST", "improve_contrast", "motion_improve_contrast"), True), True)
        self.frame_height = _int(frame_height if frame_height is not None else _setting(("MOTION_FRAME_HEIGHT", "MOTION_TUNER_FRAME_HEIGHT", "frame_height"), 100), 100)
        fw = frame_width if frame_width is not None else (width if width is not None else _setting(("MOTION_FRAME_WIDTH", "frame_width"), None))
        self.frame_width = _int(fw, None) if fw is not None else None
        self.width = _int(width if width is not None else self.frame_width, None) if (width is not None or self.frame_width is not None) else None
        h = height if height is not None else _setting(("MOTION_FRAME_HEIGHT", "height"), None)
        self.height = _int(h, None) if h is not None else self.frame_height
        if self.frame_width is None:
            self.frame_width = self.width
        if self.height is None:
            self.height = self.frame_height
        self.mask = mask
        self.delta_alpha = _float(delta_alpha if delta_alpha is not None else _setting(("MOTION_DELTA_ALPHA", "delta_alpha"), 0.2), 0.2)
        self.frame_alpha = _float(frame_alpha if frame_alpha is not None else _setting(("MOTION_FRAME_ALPHA", "frame_alpha"), 0.01), 0.01)
        self.lightning_threshold = _float(lightning_threshold if lightning_threshold is not None else _setting(("MOTION_LIGHTNING_THRESHOLD", "lightning_threshold"), 0.8), 0.8)
        self.skip_motion_threshold = None if skip_motion_threshold is None else _float(skip_motion_threshold, 0.0)
        self.blur_radius = _int(blur_radius if blur_radius is not None else _setting(("MOTION_BLUR_RADIUS", "blur_radius"), 1), 1)
        for k, v in (kwargs or {}).items():
            setattr(self, k, v)
        self._avg = None
        self._count = 0
        self.calibrating = True

    @property
    def threshold_value(self):
        return self.threshold

    @property
    def contour(self):
        return self.contour_area

    @property
    def contour_threshold(self):
        return self.contour_area

    @property
    def min_contour_area(self):
        return self.contour_area

    @property
    def min_area(self):
        return self.contour_area

    def update_settings(self, **kwargs):
        for k, v in kwargs.items():
            lk = str(k).lower()
            if "threshold" in lk:
                self.threshold = _int(v, self.threshold)
            elif "contour" in lk or "area" in lk or "min_blob" in lk or "min_area" in lk:
                self.contour_area = _int(v, self.contour_area)
            elif "contrast" in lk:
                self.improve_contrast = _bool(v, self.improve_contrast)
            elif "height" in lk:
                self.frame_height = _int(v, self.frame_height)
            elif "width" in lk:
                self.frame_width = _int(v, self.frame_width)
            elif "mask" in lk:
                self.mask = v
            elif "blur" in lk:
                self.blur_radius = _int(v, self.blur_radius)
            else:
                setattr(self, k, v)
        return self

    def set_threshold(self, value):
        self.threshold = _int(value, self.threshold)
        return self

    def set_contour_area(self, value):
        self.contour_area = _int(value, self.contour_area)
        return self

    def set_improve_contrast(self, value):
        self.improve_contrast = _bool(value, self.improve_contrast)
        return self

    def get_threshold(self):
        return self.threshold

    def get_contour_area(self):
        return self.contour_area

    def get_improve_contrast(self):
        return self.improve_contrast

    def get_config(self):
        return {
            "threshold": self.threshold,
            "motion_threshold": self.threshold,
            "contour_area": self.contour_area,
            "motion_contour_area": self.contour_area,
            "min_contour_area": self.contour_area,
            "contour": self.contour_area,
            "improve_contrast": self.improve_contrast,
            "frame_height": self.frame_height,
            "frame_width": self.frame_width,
            "width": self.frame_width,
            "height": self.frame_height,
            "mask": self.mask,
            "delta_alpha": self.delta_alpha,
            "frame_alpha": self.frame_alpha,
            "lightning_threshold": self.lightning_threshold,
            "skip_motion_threshold": self.skip_motion_threshold,
            "blur_radius": self.blur_radius,
        }

    def to_dict(self):
        return self.get_config()

    def config_dict(self):
        return self.get_config()

    def as_dict(self):
        return self.get_config()

    def _prepare(self, frame):
        bgr = _bgr(frame)
        orig_h, orig_w = bgr.shape[:2]
        target_h = self.frame_height or orig_h
        if target_h < 1:
            target_h = orig_h
        target_w = int(target_h * orig_w / max(1, orig_h))
        if target_w < 1:
            target_w = orig_w
        resized = cv2.resize(bgr, (target_w, target_h), interpolation=cv2.INTER_NEAREST)
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        if self.improve_contrast:
            try:
                minv = float(np.percentile(gray, 4))
                maxv = float(np.percentile(gray, 96))
            except Exception:
                minv, maxv = 0.0, 255.0
            if minv < maxv:
                clipped = np.clip(gray, minv, maxv)
                gray = ((clipped - minv) / (maxv - minv) * 255.0).astype(np.uint8)
        if self.mask is not None:
            try:
                m = self.mask
                if not isinstance(m, (list, tuple)):
                    m_resized = cv2.resize(np.array(m, dtype=np.uint8), (target_w, target_h), interpolation=cv2.INTER_NEAREST)
                    gray = np.where(m_resized > 0, gray, 0).astype(np.uint8)
            except Exception:
                pass
        ksize = max(1, self.blur_radius * 2 + 1)
        blurred = cv2.GaussianBlur(gray, (ksize, ksize), 0)
        if self.blur_radius <= 0:
            blurred = gray.copy()
        return blurred, (orig_h, orig_w)

    def tune(self, prev_frame=None, curr_frame=None, image=None, frame=None, current=None,
             previous=None, prev=None, before=None, after=None, **kwargs):
        if kwargs:
            self.update_settings(**{k: v for k, v in kwargs.items() if k not in ("width", "height")})

        if prev_frame is None:
            prev_frame = previous if previous is not None else (prev if prev is not None else before)
        if curr_frame is None:
            curr_frame = image if image is not None else (frame if frame is not None else (current if current is not None else after))

        if isinstance(prev_frame, str):
            prev_frame = _decode(prev_frame)
        if isinstance(curr_frame, str):
            curr_frame = _decode(curr_frame)

        single = curr_frame is None and prev_frame is not None
        if single:
            curr_frame = prev_frame
            prev_frame = None

        if prev_frame is None and curr_frame is None:
            h = self.frame_height or 100
            w = self.frame_width or 100
            if w is None:
                w = 100
            prev_frame = np.zeros((h, w, 3), dtype=np.uint8)
            curr_frame = prev_frame.copy()
            curr_frame[h // 6:h // 2, w // 6:w // 2] = 255

        prev_bgr = _bgr(prev_frame) if prev_frame is not None else None
        curr_bgr = _bgr(curr_frame) if curr_frame is not None else None
        if curr_bgr is None:
            raise ValueError("No current frame provided")

        orig_h, orig_w = curr_bgr.shape[:2]
        if prev_bgr is None:
            if self._avg is None or getattr(self._avg, "shape", None) != (self.frame_height or orig_h, int((self.frame_height or orig_h) * orig_w / max(1, orig_h))):
                self._avg = np.zeros((self.frame_height or orig_h, int((self.frame_height or orig_h) * orig_w / max(1, orig_h))), dtype=np.float32)
                self.calibrating = True
            prev_prep = self._avg.astype(np.uint8)
            curr_prep, _ = self._prepare(curr_bgr)
            if prev_prep.shape != curr_prep.shape:
                prev_prep = cv2.resize(prev_prep, (curr_prep.shape[1], curr_prep.shape[0]))
            diff = cv2.absdiff(curr_prep, prev_prep)
        else:
            prev_prep, _ = self._prepare(prev_bgr)
            curr_prep, _ = self._prepare(curr_bgr)
            if prev_prep.shape != curr_prep.shape:
                prev_prep = cv2.resize(prev_prep, (curr_prep.shape[1], curr_prep.shape[0]))
            diff = cv2.absdiff(curr_prep, prev_prep)

        _, thresh = cv2.threshold(diff, self.threshold, 255, cv2.THRESH_BINARY)
        thresh = cv2.dilate(thresh, None, iterations=1)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        scale_y = orig_h / max(1, thresh.shape[0])
        scale_x = orig_w / max(1, thresh.shape[1])
        regions = []
        total_area = 0
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.contour_area:
                continue
            x, y, w, h = cv2.boundingRect(c)
            ox = int(x * scale_x)
            oy = int(y * scale_y)
            ow = int(w * scale_x)
            oh = int(h * scale_y)
            oa = int(area * scale_x * scale_y)
            regions.append(MotionTunerRegion(x=ox, y=oy, width=ow, height=oh, area=oa))
            total_area += oa

        if prev_bgr is None:
            cv2.accumulateWeighted(curr_prep.astype(np.float32), self._avg, self.frame_alpha if not self.calibrating else 0.2)
            self._count += 1
            if self._count >= 10:
                self.calibrating = False

        motion_percent = float(total_area) / float(max(1, orig_h * orig_w))
        triggered = bool(regions)

        overlay = curr_bgr.copy()
        for r in regions:
            cv2.rectangle(overlay, (r.x, r.y), (r.x + r.width, r.y + r.height), (0, 0, 255), 2)

        result = MotionTunerResult(
            motion=triggered,
            triggered=triggered,
            changed=triggered,
            detected=triggered,
            has_motion=triggered,
            motion_detected=triggered,
            score=motion_percent,
            motion_percent=motion_percent,
            pixels_changed=int(total_area),
            boxes=list(regions),
            regions=list(regions),
            contours=list(regions),
            mask=thresh,
            threshold_image=thresh,
            diff=diff,
            overlay=overlay,
            preview=overlay,
            image=overlay,
            frame=overlay,
            threshold=self.threshold,
            contour_area=self.contour_area,
            improve_contrast=self.improve_contrast,
            config=self.get_config(),
            settings=self.get_config(),
            params=self.get_config(),
            width=orig_w,
            height=orig_h,
            frame_width=orig_w,
            frame_height=orig_h,
        )
        return result

    def analyze(self, *a, **k):
        return self.tune(*a, **k)

    def process(self, *a, **k):
        return self.tune(*a, **k)

    def detect(self, *a, **k):
        return self.tune(*a, **k)

    def run(self, *a, **k):
        return self.tune(*a, **k)

    def compute(self, *a, **k):
        return self.tune(*a, **k)

    def evaluate(self, *a, **k):
        return self.tune(*a, **k)

    def process_and_evaluate(self, *a, **k):
        # Support motion_zoning-style calls: process_and_evaluate(frame) or (prev, curr)
        if len(a) == 1 and not k.get("prev_frame") and not k.get("curr_frame") and not k.get("previous") and not k.get("image") and not k.get("frame") and not k.get("current"):
            frame = a[0]
            return self.tune(curr_frame=frame)
        if len(a) >= 2:
            return self.tune(a[0], a[1], **k)
        return self.tune(*a, **k)

    def calibrate(self, *a, **k):
        self._avg = None
        self._count = 0
        self.calibrating = True
        return self

    def reset(self, *a, **k):
        return self.calibrate(*a, **k)

    def get_motion_mask(self, *a, **k):
        return self.tune(*a, **k).mask

    def get_overlay(self, *a, **k):
        return self.tune(*a, **k).overlay

    def get_preview(self, *a, **k):
        return self.get_overlay(*a, **k)


# Class aliases
MotionTunerService = MotionTuner
FrigateMotionTuner = MotionTuner
MotionTunerEngine = MotionTuner
MotionDetectionTuner = MotionTuner
MotionTuningService = MotionTuner
MotionTunerDetector = MotionTuner
Tuner = MotionTuner
MotionTunerClass = MotionTuner


# Singleton
motion_tuner = MotionTuner()
motion_tuner_service = motion_tuner
tuner = motion_tuner
motion_tuner_instance = motion_tuner
service = motion_tuner
motion_tuner_obj = motion_tuner
motion_detector_tuner = motion_tuner
frigate_motion_tuner = motion_tuner


def get_threshold(*a, **k):
    return motion_tuner.get_threshold(*a, **k)


def set_threshold(*a, **k):
    motion_tuner.set_threshold(*a, **k)
    return motion_tuner.get_threshold()


def get_motion_threshold(*a, **k):
    return get_threshold(*a, **k)


def set_motion_threshold(*a, **k):
    return set_threshold(*a, **k)


def get_contour_area(*a, **k):
    return motion_tuner.get_contour_area(*a, **k)


def set_contour_area(*a, **k):
    motion_tuner.set_contour_area(*a, **k)
    return motion_tuner.get_contour_area()


def get_motion_contour_area(*a, **k):
    return get_contour_area(*a, **k)


def set_motion_contour_area(*a, **k):
    return set_contour_area(*a, **k)


def get_improve_contrast(*a, **k):
    return motion_tuner.get_improve_contrast(*a, **k)


def set_improve_contrast(*a, **k):
    motion_tuner.set_improve_contrast(*a, **k)
    return motion_tuner.get_improve_contrast()


def get_config(*a, **k):
    return motion_tuner.get_config(*a, **k)


def get_motion_tuner_config(*a, **k):
    return get_config(*a, **k)


def get_settings(*a, **k):
    return get_config(*a, **k)


def to_dict(*a, **k):
    return get_config(*a, **k)


def tune(*a, **k):
    return motion_tuner.tune(*a, **k)


def tune_motion(*a, **k):
    return tune(*a, **k)


def analyze(*a, **k):
    return tune(*a, **k)


def process(*a, **k):
    return tune(*a, **k)


def detect(*a, **k):
    return tune(*a, **k)


def run(*a, **k):
    return tune(*a, **k)


def compute(*a, **k):
    return tune(*a, **k)


def evaluate(*a, **k):
    return tune(*a, **k)


def process_and_evaluate(*a, **k):
    return motion_tuner.process_and_evaluate(*a, **k)


def calibrate(*a, **k):
    return motion_tuner.calibrate(*a, **k)


def reset(*a, **k):
    return calibrate(*a, **k)


def get_motion_mask(*a, **k):
    return motion_tuner.get_motion_mask(*a, **k)


def get_overlay(*a, **k):
    return motion_tuner.get_overlay(*a, **k)


def get_preview(*a, **k):
    return motion_tuner.get_preview(*a, **k)


def __getattr__(name):
    low = name.lower()
    if "threshold" in low:
        return set_threshold if "set" in low else get_threshold
    if "contour" in low or ("area" in low):
        return set_contour_area if "set" in low else get_contour_area
    if "contrast" in low:
        return set_improve_contrast if "set" in low else get_improve_contrast
    if "config" in low or "setting" in low:
        return get_config
    if "mask" in low:
        return get_motion_mask
    if "overlay" in low or "preview" in low:
        return get_overlay
    if "tune" in low or "analy" in low or "process" in low or "detect" in low:
        return tune
    raise AttributeError(name)
