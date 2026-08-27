from __future__ import annotations

import json
import math
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import serial

CSI_PREFIX = "CSI"
CSI_HEADER_FIELDS = 14
FAKE_CSI_LEN = 128

DEFAULT_NODE_XY = {
    1: (0.18, 0.20),
    2: (0.82, 0.20),
    3: (0.50, 0.82),
    4: (0.50, 0.50),
}

NODE_XY: Dict[int, Tuple[float, float]] = dict(DEFAULT_NODE_XY)
ROOM_WIDTH_M = 1.0
ROOM_HEIGHT_M = 1.0
ROOM_NAME = "room"


@dataclass
class RoomLayout:
    name: str
    width_m: float
    height_m: float
    node_xy_norm: Dict[int, Tuple[float, float]]
    node_xy_m: Dict[int, Tuple[float, float]]


def load_room_layout(path: Optional[Path] = None) -> RoomLayout:
    """Load meter positions; store normalized NODE_XY for the map + soft zone."""
    global NODE_XY, ROOM_WIDTH_M, ROOM_HEIGHT_M, ROOM_NAME

    if path is None or not path.is_file():
        NODE_XY = dict(DEFAULT_NODE_XY)
        ROOM_WIDTH_M = 1.0
        ROOM_HEIGHT_M = 1.0
        ROOM_NAME = "room"
        return RoomLayout(ROOM_NAME, 1.0, 1.0, dict(NODE_XY), {})

    data = json.loads(path.read_text(encoding="utf-8"))
    width = float(data.get("width_m", 4.0))
    height = float(data.get("height_m", 3.0))
    if width <= 0 or height <= 0:
        raise SystemExit(f"Invalid room size in {path}")

    node_m: Dict[int, Tuple[float, float]] = {}
    node_n: Dict[int, Tuple[float, float]] = {}
    for entry in data.get("nodes", []):
        nid = int(entry["id"])
        xm = float(entry["x_m"])
        ym = float(entry["y_m"])
        node_m[nid] = (xm, ym)
        node_n[nid] = (xm / width, ym / height)

    if len(node_n) < 1:
        raise SystemExit(f"No nodes in layout file {path}")

    NODE_XY = node_n
    ROOM_WIDTH_M = width
    ROOM_HEIGHT_M = height
    ROOM_NAME = str(data.get("name", path.stem))
    print(
        f"[layout] {ROOM_NAME}: {width:.1f}m x {height:.1f}m  nodes="
        + ", ".join(f"N{n}=({node_m[n][0]:.1f},{node_m[n][1]:.1f})m" for n in sorted(node_m)),
        flush=True,
    )
    return RoomLayout(ROOM_NAME, width, height, node_n, node_m)


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


def _enqueue_parsed(
    raw_line: str,
    out_q: "queue.Queue[CsiPacket]",
    stats: Dict[str, int],
) -> None:
    stats["lines"] += 1
    pkt = parse_csi_line(raw_line)
    if pkt is None:
        stats["bad"] += 1
        return
    stats["parsed"] += 1
    try:
        out_q.put_nowait(pkt)
    except queue.Full:
        try:
            out_q.get_nowait()
        except queue.Empty:
            pass
        try:
            out_q.put_nowait(pkt)
        except queue.Full:
            stats["q_drop"] += 1


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


class TcpReaderThread(threading.Thread):
    """Connect to aggregator SoftAP TCP stream (same CSV lines as USB)."""

    def __init__(
        self,
        host: str,
        port: int,
        out_q: "queue.Queue[CsiPacket]",
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name=f"tcp:{host}:{port}", daemon=True)
        self.host = host
        self.tcp_port = port
        self.out_q = out_q
        self.stop_event = stop_event
        self.stats = {"lines": 0, "parsed": 0, "bad": 0, "q_drop": 0}
        self.port = f"{host}:{port}"

    def run(self) -> None:
        import socket

        while not self.stop_event.is_set():
            try:
                print(f"[tcp] connecting to {self.host}:{self.tcp_port} ...", flush=True)
                with socket.create_connection((self.host, self.tcp_port), timeout=5.0) as sock:
                    sock.settimeout(0.5)
                    print(f"[tcp] connected to aggregator stream", flush=True)
                    buf = ""
                    while not self.stop_event.is_set():
                        try:
                            chunk = sock.recv(8192)
                        except socket.timeout:
                            continue
                        if not chunk:
                            print("[tcp] aggregator closed connection", flush=True)
                            break
                        buf += chunk.decode("utf-8", errors="ignore")
                        while "\n" in buf:
                            raw_line, buf = buf.split("\n", 1)
                            _enqueue_parsed(raw_line, self.out_q, self.stats)
            except OSError as exc:
                if self.stop_event.is_set():
                    break
                print(f"[tcp] {exc} — retry in 2s (join SoftAP CSI-Sentry first)", file=sys.stderr)
                time.sleep(2.0)


def _person_xy(t: float, calib_s: float) -> Optional[Tuple[float, float]]:
    if t < calib_s:
        return None
    u = (t - calib_s) % 24.0
    if u < 6:
        return (0.25 + 0.5 * (u / 6.0), 0.25)
    if u < 12:
        return (0.75, 0.25 + 0.5 * ((u - 6) / 6.0))
    if u < 18:
        return (0.75 - 0.5 * ((u - 12) / 6.0), 0.75)
    return (0.25, 0.75 - 0.35 * ((u - 18) / 6.0))


def _fake_samples(
    node_id: int,
    person: Optional[Tuple[float, float]],
    rng: np.random.Generator,
) -> np.ndarray:
    n_sc = FAKE_CSI_LEN // 2
    phase = np.linspace(0, 2 * np.pi, n_sc, endpoint=False) + node_id
    base_amp = 40.0 + 8.0 * np.sin(phase * (1 + node_id * 0.3))
    if person is not None:
        nx, ny = NODE_XY.get(node_id, (0.5, 0.5))
        dist = math.hypot(person[0] - nx, person[1] - ny)
        strength = max(0.0, 1.0 - dist / 0.95) ** 2
        base_amp = base_amp + strength * 25.0 * np.sin(phase * 3.0 + time.time())
    noise = rng.normal(0.0, 1.5, size=n_sc)
    amp = np.clip(base_amp + noise, 1.0, 120.0)
    phase_n = phase + rng.normal(0.0, 0.05, size=n_sc)
    imag = np.clip(amp * np.sin(phase_n), -127, 127).astype(np.int8)
    real = np.clip(amp * np.cos(phase_n), -127, 127).astype(np.int8)
    out = np.empty(FAKE_CSI_LEN, dtype=np.int8)
    out[0::2] = imag
    out[1::2] = real
    return out


class FakeCsiThread(threading.Thread):
    def __init__(
        self,
        out_q: "queue.Queue[CsiPacket]",
        stop_event: threading.Event,
        node_ids: Iterable[int],
        calib_s: float,
        hz: float = 40.0,
    ) -> None:
        super().__init__(name="fake-csi", daemon=True)
        self.out_q = out_q
        self.stop_event = stop_event
        self.node_ids = list(node_ids)
        self.calib_s = calib_s
        self.period = 1.0 / max(hz, 1.0)
        self.stats = {"lines": 0, "parsed": 0, "bad": 0, "q_drop": 0}
        self.port = "DEMO"
        self.person_xy: Optional[Tuple[float, float]] = None
        self._seq = {n: 0 for n in self.node_ids}
        self._rng = {n: np.random.default_rng(1000 + n) for n in self.node_ids}
        self.t0 = time.time()

    def run(self) -> None:
        print(
            f"[demo] fake nodes={self.node_ids} calib={self.calib_s:.0f}s then walk loop",
            flush=True,
        )
        while not self.stop_event.is_set():
            t = time.time() - self.t0
            self.person_xy = _person_xy(t, self.calib_s)
            for nid in self.node_ids:
                self._seq[nid] += 1
                samples = _fake_samples(nid, self.person_xy, self._rng[nid])
                pkt = CsiPacket(
                    node_id=nid,
                    seq=self._seq[nid],
                    rssi=-55 if self.person_xy is None else -48,
                    noise=-90,
                    channel=6,
                    mac=f"aabbccddee{nid:02x}",
                    len_=FAKE_CSI_LEN,
                    fw_inv=False,
                    samples=samples,
                )
                self.stats["lines"] += 1
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
            time.sleep(self.period)
