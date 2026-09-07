from __future__ import annotations

from dataclasses import dataclass

MIN_DURATION = 15.0
PREFERRED_MAX = 45.0
HARD_MAX = 60.0
THRESHOLD_RATIO = 0.55
SNAP = 0.1


class NoHeatmapError(ValueError):
    pass


@dataclass(frozen=True)
class HeatPoint:
    start: float
    end: float
    value: float


@dataclass(frozen=True)
class ClipWindow:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


def normalize_heatmap(raw: list | None) -> list[HeatPoint]:
    if not raw:
        return []
    if isinstance(raw[0], HeatPoint):
        return list(raw)
    points: list[HeatPoint] = []
    for item in raw:
        start = float(item.get("start_time", item.get("start", 0)))
        end = item.get("end_time", item.get("end"))
        if end is None:
            duration = float(item.get("duration", 0) or 0)
            end = start + duration
        value = float(item.get("value", item.get("intensity", 0) or 0))
        points.append(HeatPoint(start=start, end=float(end), value=value))
    points.sort(key=lambda p: p.start)
    return points


def pick_peak_window(raw_heatmap: list | None, video_duration: float) -> ClipWindow:
    points = normalize_heatmap(raw_heatmap)
    if not points:
        raise NoHeatmapError(
            "This video has no Most replayed heatmap yet. Try a more popular video."
        )

    duration = max(float(video_duration or 0), points[-1].end)
    if duration <= 0:
        raise NoHeatmapError("Could not read video duration.")

    peak_idx = max(range(len(points)), key=lambda i: points[i].value)
    peak_value = points[peak_idx].value
    threshold = peak_value * THRESHOLD_RATIO

    left = right = peak_idx
    while left > 0 and points[left - 1].value >= threshold:
        left -= 1
    while right < len(points) - 1 and points[right + 1].value >= threshold:
        right += 1

    start = points[left].start
    end = points[right].end

    if end - start < MIN_DURATION:
        peak_center = (points[peak_idx].start + points[peak_idx].end) / 2
        start = peak_center - MIN_DURATION / 2
        end = peak_center + MIN_DURATION / 2

    start, end = _clamp_to_video(start, end, duration)

    if end - start > PREFERRED_MAX:
        start, end = _hottest_span(points, start, end, PREFERRED_MAX)

    if end - start > HARD_MAX:
        start, end = _hottest_span(points, start, end, HARD_MAX)

    start, end = _clamp_to_video(start, end, duration)
    start, end = _ensure_min_duration(start, end, duration)

    start = _snap(max(0.0, start))
    end = _snap(min(duration, end))
    if end <= start:
        end = min(duration, start + MIN_DURATION)
        start = max(0.0, end - MIN_DURATION)
        start, end = _snap(start), _snap(end)

    return ClipWindow(start=start, end=end)


def _value_at(points: list[HeatPoint], t: float) -> float:
    for point in points:
        if point.start <= t < point.end:
            return point.value
    if points and t >= points[-1].end:
        return points[-1].value
    return 0.0


def _hottest_span(
    points: list[HeatPoint], region_start: float, region_end: float, length: float
) -> tuple[float, float]:
    span = region_end - region_start
    if span <= length:
        return region_start, region_end

    step = SNAP
    samples: list[float] = []
    t = region_start
    while t < region_end:
        samples.append(_value_at(points, t))
        t += step

    window = max(1, int(round(length / step)))
    if len(samples) <= window:
        return region_start, min(region_end, region_start + length)

    current = sum(samples[:window])
    best = current
    best_i = 0
    for i in range(1, len(samples) - window + 1):
        current += samples[i + window - 1] - samples[i - 1]
        if current > best:
            best = current
            best_i = i

    start = region_start + best_i * step
    end = start + length
    if end > region_end:
        end = region_end
        start = end - length
    return start, end


def _clamp_to_video(start: float, end: float, duration: float) -> tuple[float, float]:
    length = end - start
    if length >= duration:
        return 0.0, duration
    if start < 0:
        start = 0.0
        end = min(duration, length)
    if end > duration:
        end = duration
        start = max(0.0, end - length)
    return start, end


def _ensure_min_duration(start: float, end: float, duration: float) -> tuple[float, float]:
    if duration < MIN_DURATION:
        return 0.0, duration
    if end - start >= MIN_DURATION:
        return start, end
    end = min(duration, start + MIN_DURATION)
    start = max(0.0, end - MIN_DURATION)
    return start, end


def _snap(value: float) -> float:
    return round(round(value / SNAP) * SNAP, 1)
