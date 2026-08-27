from __future__ import annotations

import collections
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

import ingest
from ingest import CsiPacket

PLOT_HISTORY = 400
MOVING_AVG_WIN = 8
OCCUPY_THRESH = 25.0
MOTION_DELTA = 8.0
SPEC_HISTORY = 80


class NodeBaseline:
    def __init__(self) -> None:
        self.count = 0
        self.mean: Optional[np.ndarray] = None
        self.target_bins: Optional[int] = None

    def update(self, amp: np.ndarray) -> None:
        if amp.size == 0:
            return
        if self.mean is None:
            self.mean = amp.astype(np.float64).copy()
            self.target_bins = amp.size
            self.count = 1
            return
        vec = self._align(amp)
        self.count += 1
        self.mean += (vec - self.mean) / self.count

    def _align(self, amp: np.ndarray) -> np.ndarray:
        assert self.target_bins is not None
        n = self.target_bins
        if amp.size == n:
            return amp.astype(np.float64)
        if amp.size > n:
            return amp[:n].astype(np.float64)
        out = np.zeros(n, dtype=np.float64)
        out[: amp.size] = amp
        return out

    def disturbance(self, amp: np.ndarray) -> float:
        if self.mean is None or amp.size == 0:
            return 0.0
        vec = self._align(amp)
        err = vec - self.mean
        return float(np.mean(err * err))

    def ready(self) -> bool:
        return self.mean is not None and self.count >= 10


@dataclass
class RoomState:
    label: str
    confidence: float
    estimate_xy: Optional[Tuple[float, float]]
    hottest_node: Optional[int]
    max_score: float


class PresenceAnalytics:
    def __init__(
        self,
        calibrate_s: float,
        moving_avg: int = MOVING_AVG_WIN,
        occupy_thresh: float = OCCUPY_THRESH,
    ) -> None:
        self.calibrate_s = calibrate_s
        self.calibrating = calibrate_s > 0
        self.calib_deadline = time.time() + calibrate_s if self.calibrating else 0.0
        self.occupy_thresh = occupy_thresh
        self.baselines: Dict[int, NodeBaseline] = {}
        self.history: Dict[int, Deque[Tuple[float, float]]] = {}
        self.moving_avg = max(1, moving_avg)
        self._score_bufs: Dict[int, Deque[float]] = {}
        self.last_seq: Dict[int, int] = {}
        self.seq_gaps: Dict[int, int] = {}
        self.last_amp: Dict[int, np.ndarray] = {}
        self.last_seen: Dict[int, float] = {}
        self.spec_rows: Deque[np.ndarray] = collections.deque(maxlen=SPEC_HISTORY)
        self.events: Deque[str] = collections.deque(maxlen=8)
        self._prev_label = "EMPTY"
        self._spec_node: Optional[int] = None
        self._live_prev: Dict[int, bool] = {}
        self.lock = threading.Lock()

    def start_recalibrate(self, seconds: Optional[float] = None) -> None:
        """Re-lock empty-room baseline (e.g. after a door/furniture change)."""
        dur = self.calibrate_s if seconds is None else float(seconds)
        with self.lock:
            self.baselines.clear()
            self.history.clear()
            self._score_bufs.clear()
            self.last_amp.clear()
            self.last_seen.clear()
            self._live_prev.clear()
            self.spec_rows.clear()
            self._spec_node = None
            self.calibrating = dur > 0
            self.calib_deadline = time.time() + dur if self.calibrating else 0.0
            self._prev_label = "EMPTY"
            self.events.appendleft(
                time.strftime("%H:%M:%S") + f"  recalibrate {dur:.0f}s (empty room)"
            )
            print(f"[calib] recalibrating for {dur:.0f}s — keep room empty", flush=True)

    def _node_baseline(self, node_id: int) -> NodeBaseline:
        if node_id not in self.baselines:
            self.baselines[node_id] = NodeBaseline()
            self.history[node_id] = collections.deque(maxlen=PLOT_HISTORY)
            self._score_bufs[node_id] = collections.deque(maxlen=self.moving_avg)
            self.seq_gaps[node_id] = 0
        return self.baselines[node_id]

    def process(self, pkt: CsiPacket) -> None:
        amp = pkt.amplitude()
        with self.lock:
            prev = self.last_seq.get(pkt.node_id)
            if prev is not None and pkt.seq > prev + 1:
                self.seq_gaps[pkt.node_id] = self.seq_gaps.get(pkt.node_id, 0) + (
                    pkt.seq - prev - 1
                )
            self.last_seq[pkt.node_id] = pkt.seq
            self.last_seen[pkt.node_id] = pkt.t_recv
            live = self._is_live_unlocked(pkt.node_id, pkt.t_recv)
            was = self._live_prev.get(pkt.node_id)
            if was is not True and live:
                self.events.appendleft(
                    time.strftime("%H:%M:%S") + f"  node {pkt.node_id} live"
                )
                self._live_prev[pkt.node_id] = True
            if amp.size:
                self.last_amp[pkt.node_id] = amp

            bl = self._node_baseline(pkt.node_id)

            if self.calibrating:
                if time.time() < self.calib_deadline:
                    bl.update(amp)
                    self.history[pkt.node_id].append((pkt.t_recv, 0.0))
                    return
                self.calibrating = False
                self.events.appendleft(time.strftime("%H:%M:%S") + "  baseline locked")
                print(
                    "[calib] done - baselines: "
                    + ", ".join(
                        f"node {nid} n={b.count} bins={b.target_bins}"
                        for nid, b in self.baselines.items()
                    ),
                    flush=True,
                )

            score = bl.disturbance(amp) if bl.ready() else 0.0
            buf = self._score_bufs[pkt.node_id]
            buf.append(score)
            smooth = float(sum(buf) / len(buf))
            self.history[pkt.node_id].append((pkt.t_recv, smooth))

            state = self._compute_state_unlocked()
            if amp.size and state.hottest_node is not None:
                # Waterfall tracks the hottest node so bands stay comparable.
                if self._spec_node != state.hottest_node:
                    self.spec_rows.clear()
                    self._spec_node = state.hottest_node
                if pkt.node_id == self._spec_node:
                    self.spec_rows.append(amp.copy())
            elif amp.size and self._spec_node is None:
                self._spec_node = pkt.node_id
                self.spec_rows.append(amp.copy())

            if state.label != self._prev_label and state.label != "CALIBRATING":
                self.events.appendleft(
                    time.strftime("%H:%M:%S") + f"  {self._prev_label} -> {state.label}"
                )
                self._prev_label = state.label

    def _compute_state_unlocked(self) -> RoomState:
        if self.calibrating:
            return RoomState("CALIBRATING", 0.0, None, None, 0.0)

        scores = {}
        for nid, hist in self.history.items():
            if hist:
                scores[nid] = hist[-1][1]
        if not scores:
            return RoomState("EMPTY", 0.0, None, None, 0.0)

        max_score = max(scores.values())
        hottest = max(scores, key=scores.get)

        # Temporal jitter on each node (not cross-node spread — that looks like
        # "motion" whenever one corner is hot and another is quiet).
        motion_energy = 0.0
        for hist in self.history.values():
            ys = [s for _, s in list(hist)[-16:]]
            if len(ys) >= 5:
                motion_energy = max(motion_energy, float(np.std(ys)))

        if max_score < self.occupy_thresh:
            label = "EMPTY"
            conf = float(np.clip(1.0 - max_score / self.occupy_thresh, 0.0, 1.0))
        elif motion_energy >= MOTION_DELTA:
            label = "MOTION"
            conf = float(np.clip(max_score / (self.occupy_thresh * 3.0), 0.35, 1.0))
        else:
            label = "STATIC"
            conf = float(np.clip(max_score / (self.occupy_thresh * 2.5), 0.35, 1.0))

        est = None
        if max_score >= self.occupy_thresh:
            wsum = 0.0
            sx = 0.0
            sy = 0.0
            for nid, s in scores.items():
                w = max(0.0, s - self.occupy_thresh * 0.5)
                if w <= 0 or nid not in ingest.NODE_XY:
                    continue
                x, y = ingest.NODE_XY[nid]
                sx += w * x
                sy += w * y
                wsum += w
            if wsum > 0:
                est = (sx / wsum, sy / wsum)

        return RoomState(label, conf, est, hottest, max_score)

    def room_state(self) -> RoomState:
        with self.lock:
            return self._compute_state_unlocked()

    def snapshot(self) -> Dict[int, List[Tuple[float, float]]]:
        with self.lock:
            return {nid: list(hist) for nid, hist in self.history.items()}

    def latest_scores(self) -> Dict[int, float]:
        with self.lock:
            out: Dict[int, float] = {}
            for nid, hist in self.history.items():
                if hist:
                    out[nid] = hist[-1][1]
            return out

    def _is_live_unlocked(self, node_id: int, now: Optional[float] = None) -> bool:
        t = self.last_seen.get(node_id)
        if t is None:
            return False
        return ((now if now is not None else time.time()) - t) < 2.5

    def live_nodes(self) -> List[int]:
        with self.lock:
            now = time.time()
            live = sorted(n for n in self.last_seen if self._is_live_unlocked(n, now))
            for n, was in list(self._live_prev.items()):
                is_live = n in live
                if was and not is_live:
                    self.events.appendleft(
                        time.strftime("%H:%M:%S") + f"  node {n} quiet"
                    )
                    self._live_prev[n] = False
            return live

    def spectrogram(self) -> Optional[np.ndarray]:
        with self.lock:
            if len(self.spec_rows) < 2:
                return None
            rows = list(self.spec_rows)
            width = min(r.size for r in rows)
            return np.stack([r[:width] for r in rows], axis=0)

    def spectrogram_node(self) -> Optional[int]:
        with self.lock:
            return self._spec_node

    def event_lines(self) -> List[str]:
        with self.lock:
            return list(self.events)

    def status_line(self) -> str:
        st = self.room_state()
        live = self.live_nodes()
        live_s = " ".join(f"N{n}" for n in live) if live else "--"
        if st.label == "CALIBRATING":
            left = max(0.0, self.calib_deadline - time.time())
            return f"calibrating  {left:4.1f}s  |  live {live_s}"
        zone = f"N{st.hottest_node}" if st.hottest_node else "--"
        return (
            f"{st.label.lower()}  |  {st.confidence:.0%}  |  "
            f"max {st.max_score:.0f}  |  zone {zone}  |  live {live_s}"
        )

    def save(self, path: Path) -> None:
        with self.lock:
            payload = {}
            for nid, bl in self.baselines.items():
                if bl.mean is None:
                    continue
                payload[f"node_{nid}_mean"] = bl.mean
                payload[f"node_{nid}_count"] = np.asarray([bl.count])
            if not payload:
                raise RuntimeError("No baseline to save")
            np.savez(path, **payload)
            print(f"[calib] saved -> {path}")

    def load(self, path: Path) -> None:
        data = np.load(path)
        with self.lock:
            self.calibrating = False
            for key in data.files:
                if not key.endswith("_mean"):
                    continue
                nid = int(key.split("_")[1])
                bl = self._node_baseline(nid)
                bl.mean = data[key].astype(np.float64)
                bl.target_bins = int(bl.mean.size)
                count_key = f"node_{nid}_count"
                bl.count = int(data[count_key][0]) if count_key in data.files else 100
            print(f"[calib] loaded <- {path} nodes={list(self.baselines)}")


def drain_queue(q: "queue.Queue[CsiPacket]", analytics: PresenceAnalytics) -> int:
    n = 0
    while True:
        try:
            pkt = q.get_nowait()
        except queue.Empty:
            break
        analytics.process(pkt)
        n += 1
    return n
