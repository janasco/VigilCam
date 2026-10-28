from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np


@dataclass
class MotionRegion:
    x: int
    y: int
    width: int
    height: int
    area: int


@dataclass
class MotionResult:
    changed: bool
    score: float
    regions: List[MotionRegion]


class MotionDetector:
    """Low-overhead motion gating using frame differencing + contours."""

    def __init__(
        self,
        threshold: int = 25,
        min_area: int = 500,
        blur_kernel: int = 21,
        region_of_interest: Optional[Tuple[int, int, int, int]] = None,
    ) -> None:
        self.threshold = threshold
        self.min_area = min_area
        self.blur_kernel = blur_kernel
        self.roi = region_of_interest
        self.previous_gray: Optional[np.ndarray] = None

    def process(self, frame: np.ndarray) -> MotionResult:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        gray = cv2.GaussianBlur(gray, (self.blur_kernel, self.blur_kernel), 0)

        if self.previous_gray is None or self.previous_gray.shape != gray.shape:
            self.previous_gray = gray
            return MotionResult(changed=False, score=0.0, regions=[])

        delta = cv2.absdiff(self.previous_gray, gray)
        self.previous_gray = gray

        _, thresh = cv2.threshold(delta, self.threshold, 255, cv2.THRESH_BINARY)
        thresh = cv2.dilate(thresh, None, iterations=2)

        if self.roi is not None:
            x, y, w, h = self.roi
            mask = np.zeros_like(thresh)
            mask[y : y + h, x : x + w] = 255
            thresh = cv2.bitwise_and(thresh, mask)

        contours, _ = cv2.findContours(
            thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        regions = []
        total_area = 0
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < self.min_area:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            regions.append(MotionRegion(x=x, y=y, width=w, height=h, area=int(area)))
            total_area += int(area)

        score = float(total_area) / float(max(1, gray.shape[0] * gray.shape[1]))
        return MotionResult(changed=bool(regions), score=score, regions=regions)
