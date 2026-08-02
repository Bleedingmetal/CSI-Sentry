from __future__ import annotations

import argparse
import collections
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Tuple

import numpy as np

try:
    import serial
    from serial.tools import list_ports
except ImportError as exc:
    raise SystemExit("Install deps: pip install -r host/requirements.txt") from exc

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation


CSI_PREFIX = "CSI"
CSI_HEADER_FIELDS = 14
DEFAULT_BAUD = 921600
DEFAULT_CALIBRATE_S = 30.0
PLOT_HISTORY = 400
MOVING_AVG_WIN = 8
QUEUE_MAX = 2000


@dataclass(frozen=True)
class CsiPacket:
    node_id: int
    seq: int
    rssi: int
    noise: int
    channel: int
    mac: str
    len_: int
    fw_inv: bool
    samples: np.ndarray
    t_recv: float = field(default_factory=time.time)

    def amplitude(self) -> np.ndarray:
        raw = self.samples.astype(np.float32)
        if self.fw_inv and raw.size >= 4:
            raw = raw[4:]
        if raw.size < 2:
            return np.empty(0, dtype=np.float32)
        if raw.size % 2:
            raw = raw[:-1]
        iq = raw.reshape(-1, 2)
        return np.sqrt(iq[:, 0] ** 2 + iq[:, 1] ** 2)


def parse_csi_line(line: str) -> Optional[CsiPacket]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if not line.startswith(CSI_PREFIX + ","):
        return None

    parts = line.split(",")
    if len(parts) < CSI_HEADER_FIELDS:
        return None

    try:
        node_id = int(parts[1])
        seq = int(parts[2])
        rssi = int(parts[3])
        noise = int(parts[4])
        channel = int(parts[5])
        mac = parts[11].strip().lower()
        declared_len = int(parts[12])
        fw_inv = bool(int(parts[13]))
    except (ValueError, IndexError):
        return None

    sample_tokens = parts[CSI_HEADER_FIELDS:]
    if declared_len <= 0:
        return None

    if len(sample_tokens) < max(1, declared_len // 2):
        return None

    samples_list: List[int] = []
    for tok in sample_tokens[:declared_len]:
        tok = tok.strip()
        if not tok:
            continue
        try:
            samples_list.append(int(tok))
        except ValueError:
            break

    if len(samples_list) < 8:
        return None

    return CsiPacket(
        node_id=node_id,
        seq=seq,
        rssi=rssi,
        noise=noise,
        channel=channel,
        mac=mac,
        len_=len(samples_list),
        fw_inv=fw_inv,
        samples=np.asarray(samples_list, dtype=np.int8),
    )


class SerialReaderThread(threading.Thread):
    def __init__(
        self,
        port: str,
        baud: int,
        out_q: "queue.Queue[CsiPacket]",
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name=f"serial:{port}", daemon=True)
        self.port = port
        self.baud = baud
        self.out_q = out_q
        self.stop_event = stop_event
        self.stats = {"lines": 0, "parsed": 0, "bad": 0, "q_drop": 0}

    def run(self) -> None:
        try:
            with serial.Serial(self.port, self.baud, timeout=0.05) as ser:
                ser.reset_input_buffer()
                buf = ""
                while not self.stop_event.is_set():
                    chunk = ser.read(4096)
                    if not chunk:
                        continue
                    try:
                        buf += chunk.decode("utf-8", errors="ignore")
                    except Exception:
                        continue

                    while "\n" in buf:
                        raw_line, buf = buf.split("\n", 1)
                        self.stats["lines"] += 1
                        pkt = parse_csi_line(raw_line)
                        if pkt is None:
                            self.stats["bad"] += 1
                            continue
                        self.stats["parsed"] += 1
                        try:
                            self.out_q.put_nowait(pkt)
                        except queue.Full:
                            try:
                                self.out_q.get_nowait()
                            except queue.Empty:
                                pass
                            try:
                                self.out_q.put_nowait(pkt)
                            except queue.Full:
                                self.stats["q_drop"] += 1
        except serial.SerialException as exc:
            print(f"[serial] {self.port}: {exc}", file=sys.stderr)


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


class PresenceAnalytics:
    def __init__(self, calibrate_s: float, moving_avg: int = MOVING_AVG_WIN) -> None:
        self.calibrate_s = calibrate_s
        self.calibrating = calibrate_s > 0
        self.calib_deadline = time.time() + calibrate_s if self.calibrating else 0.0
        self.baselines: Dict[int, NodeBaseline] = {}
        self.history: Dict[int, Deque[Tuple[float, float]]] = {}
        self.moving_avg = max(1, moving_avg)
        self._score_bufs: Dict[int, Deque[float]] = {}
        self.last_seq: Dict[int, int] = {}
        self.seq_gaps: Dict[int, int] = {}
        self.lock = threading.Lock()

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

            bl = self._node_baseline(pkt.node_id)

            if self.calibrating:
                if time.time() < self.calib_deadline:
                    bl.update(amp)
                    self.history[pkt.node_id].append((pkt.t_recv, 0.0))
                    return
                self.calibrating = False
                print(
                    "[calib] done — baselines: "
                    + ", ".join(
                        f"node {nid} n={b.count} bins={b.target_bins}"
                        for nid, b in self.baselines.items()
                    )
                )

            score = bl.disturbance(amp) if bl.ready() else 0.0
            buf = self._score_bufs[pkt.node_id]
            buf.append(score)
            smooth = float(sum(buf) / len(buf))
            self.history[pkt.node_id].append((pkt.t_recv, smooth))

    def snapshot(self) -> Dict[int, List[Tuple[float, float]]]:
        with self.lock:
            return {nid: list(hist) for nid, hist in self.history.items()}

    def status_line(self) -> str:
        with self.lock:
            if self.calibrating:
                left = max(0.0, self.calib_deadline - time.time())
                return f"CALIBRATING empty room… {left:4.1f}s left"
            parts = []
            for nid, bl in sorted(self.baselines.items()):
                hist = self.history.get(nid)
                score = hist[-1][1] if hist else 0.0
                gaps = self.seq_gaps.get(nid, 0)
                parts.append(f"N{nid}:{score:6.1f} gaps={gaps}")
            return " | ".join(parts) if parts else "waiting for CSI…"

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
            print(f"[calib] saved → {path}")

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
            print(f"[calib] loaded ← {path} nodes={list(self.baselines)}")


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


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CSI-Sentry real-time presence monitor")
    p.add_argument(
        "--ports",
        type=str,
        default="",
        help="Comma-separated serial ports (e.g. COM12,COM13). Empty = auto-list & prompt.",
    )
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument(
        "--calibrate",
        type=float,
        default=DEFAULT_CALIBRATE_S,
        help="Empty-room calibration duration in seconds (0 = skip / use --baseline).",
    )
    p.add_argument(
        "--baseline",
        type=str,
        default="",
        help="Load a previously saved .npz baseline (skips calibration if present).",
    )
    p.add_argument(
        "--save-baseline",
        type=str,
        default="baseline.npz",
        help="Where to write the baseline after calibration.",
    )
    p.add_argument("--list-ports", action="store_true", help="List serial ports and exit.")
    return p


def resolve_ports(ports_arg: str) -> List[str]:
    if ports_arg.strip():
        return [p.strip() for p in ports_arg.split(",") if p.strip()]

    available = list(list_ports.comports())
    if not available:
        raise SystemExit("No serial ports found. Pass --ports COMx explicitly.")
    print("Available ports:")
    for i, info in enumerate(available):
        print(f"  [{i}] {info.device:10s}  {info.description}")
    choice = input("Select port index(es), comma-separated (e.g. 0 or 0,1): ").strip()
    idxs = [int(x) for x in choice.split(",") if x.strip() != ""]
    return [available[i].device for i in idxs]


def run_monitor(args: argparse.Namespace) -> None:
    ports = resolve_ports(args.ports)
    print(f"[init] ports={ports} baud={args.baud}")

    pkt_q: queue.Queue[CsiPacket] = queue.Queue(maxsize=QUEUE_MAX)
    stop_event = threading.Event()
    readers = [
        SerialReaderThread(port, args.baud, pkt_q, stop_event) for port in ports
    ]
    for r in readers:
        r.start()

    analytics = PresenceAnalytics(calibrate_s=args.calibrate)
    if args.baseline:
        path = Path(args.baseline)
        if path.is_file():
            analytics.load(path)
        else:
            print(f"[warn] baseline file not found: {path} — calibrating instead")

    fig, ax = plt.subplots(figsize=(11, 5))
    fig.canvas.manager.set_window_title("CSI-Sentry Presence")
    ax.set_xlabel("time (s)")
    ax.set_ylabel("disturbance (MSE vs baseline)")
    ax.set_title("CSI presence / motion score by node")
    ax.grid(True, alpha=0.3)
    lines: Dict[int, object] = {}
    t0 = time.time()
    status = ax.text(
        0.01,
        0.98,
        "",
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=9,
        family="monospace",
        bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.8),
    )
    saved_baseline = False

    def on_timer(_frame: int):
        nonlocal saved_baseline
        drain_queue(pkt_q, analytics)

        if (
            not analytics.calibrating
            and not saved_baseline
            and args.calibrate > 0
            and args.save_baseline
        ):
            try:
                analytics.save(Path(args.save_baseline))
                saved_baseline = True
            except RuntimeError:
                pass

        snap = analytics.snapshot()
        for nid, hist in snap.items():
            if not hist:
                continue
            xs = [t - t0 for t, _ in hist]
            ys = [s for _, s in hist]
            if nid not in lines:
                (ln,) = ax.plot(xs, ys, lw=1.5, label=f"node {nid}")
                lines[nid] = ln
                ax.legend(loc="upper right")
            else:
                lines[nid].set_data(xs, ys)

        if snap:
            ax.relim()
            ax.autoscale_view()

        status.set_text(analytics.status_line())
        return list(lines.values()) + [status]

    anim = FuncAnimation(fig, on_timer, interval=50, blit=False, cache_frame_data=False)

    try:
        plt.show()
    finally:
        stop_event.set()
        for r in readers:
            r.join(timeout=1.0)
        print("[exit] reader stats:")
        for r in readers:
            print(f"  {r.port}: {r.stats}")
        _ = anim


def main(argv: Optional[Iterable[str]] = None) -> None:
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    if args.list_ports:
        for info in list_ports.comports():
            print(f"{info.device}\t{info.description}")
        return
    run_monitor(args)


if __name__ == "__main__":
    main()
