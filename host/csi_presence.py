from __future__ import annotations

import argparse
import queue
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

try:
    import serial  # noqa: F401
    from serial.tools import list_ports
except ImportError as exc:
    raise SystemExit("Install deps: pip install -r host/requirements.txt") from exc

import matplotlib

from analytics import OCCUPY_THRESH, PresenceAnalytics, drain_queue
from ingest import (
    CsiPacket,
    FakeCsiThread,
    SerialReaderThread,
    TcpReaderThread,
    load_room_layout,
)
from ui import (
    UI,
    draw_events,
    draw_room,
    draw_spec,
    make_figure,
    node_color,
    setup_timeseries,
)

DEFAULT_BAUD = 921600
DEFAULT_CALIBRATE_S = 30.0
QUEUE_MAX = 2000


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "CSI-Sentry real-time presence monitor. "
            "With the SoftAP aggregator, pass one COM port — remotes arrive over Wi-Fi."
        )
    )
    p.add_argument(
        "--ports",
        type=str,
        default="",
        help="COM port(s). Aggregator USB mode: one port, e.g. --ports COM5",
    )
    p.add_argument(
        "--tcp",
        type=str,
        default="",
        help="Wi-Fi mode: aggregator IP:port after joining SoftAP, e.g. 192.168.4.1:5006",
    )
    p.add_argument(
        "--layout",
        type=str,
        default=str(Path(__file__).with_name("room_layout.json")),
        help="Room size + ESP positions in meters (JSON). Calib does NOT discover these.",
    )
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD,
                   help="UART baud (default 921600; try 115200 if garbled)")
    p.add_argument("--calibrate", type=float, default=DEFAULT_CALIBRATE_S,
                   help="Empty-room baseline seconds (press r live to redo)")
    p.add_argument("--baseline", type=str, default="")
    p.add_argument("--save-baseline", type=str, default="baseline.npz")
    p.add_argument(
        "--demo",
        action="store_true",
        help="No hardware: synthesize CSI (default 3 nodes).",
    )
    p.add_argument(
        "--demo-nodes",
        type=str,
        default="1,2,3",
        help="Comma-separated node IDs for --demo (default 1,2,3).",
    )
    p.add_argument("--snapshot", type=str, default="")
    p.add_argument("--snapshot-seconds", type=float, default=18.0)
    p.add_argument("--thresh", type=float, default=OCCUPY_THRESH)
    p.add_argument("--list-ports", action="store_true")
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
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    pkt_q: queue.Queue[CsiPacket] = queue.Queue(maxsize=QUEUE_MAX)
    stop_event = threading.Event()
    readers: List[threading.Thread] = []
    fake: Optional[FakeCsiThread] = None
    node_ids = [1, 2, 3]

    if args.demo:
        node_ids = [int(x) for x in args.demo_nodes.split(",") if x.strip()]
        if args.calibrate == DEFAULT_CALIBRATE_S:
            args.calibrate = 5.0
        fake = FakeCsiThread(pkt_q, stop_event, node_ids, calib_s=args.calibrate)
        fake.start()
        readers.append(fake)
        print(f"[init] DEMO mode nodes={node_ids} calib={args.calibrate}s")
    elif args.tcp.strip():
        hostport = args.tcp.strip()
        if ":" in hostport:
            host, port_s = hostport.rsplit(":", 1)
            tcp_port = int(port_s)
        else:
            host, tcp_port = hostport, 5006
        print(f"[init] TCP mode {host}:{tcp_port} (join SoftAP CSI-Sentry first)")
        r = TcpReaderThread(host, tcp_port, pkt_q, stop_event)
        r.start()
        readers.append(r)
    else:
        ports = resolve_ports(args.ports)
        print(f"[init] ports={ports} baud={args.baud}")
        for port in ports:
            r = SerialReaderThread(port, args.baud, pkt_q, stop_event)
            r.start()
            readers.append(r)

    analytics = PresenceAnalytics(calibrate_s=args.calibrate, occupy_thresh=args.thresh)
    if args.baseline and not args.demo:
        path = Path(args.baseline)
        if path.is_file():
            analytics.load(path)
        else:
            print(f"[warn] baseline file not found: {path} - calibrating instead")

    fig, ax_ts, ax_room, ax_spec, ax_evt = make_figure()
    fig.canvas.manager.set_window_title("CSI-Sentry")
    status = setup_timeseries(ax_ts, args.thresh)

    lines: Dict[int, object] = {}
    t0 = time.time()
    saved_baseline = False
    draw_room(ax_room, {}, None, analytics.room_state(), node_ids, live=[])
    draw_spec(ax_spec, None)
    draw_events(ax_evt, ["r  recalibrate", "s  save baseline"])

    def on_key(event) -> None:
        nonlocal saved_baseline
        key = (event.key or "").lower()
        if key == "r":
            analytics.start_recalibrate(args.calibrate if args.calibrate > 0 else 20.0)
            saved_baseline = False
        elif key == "s" and not args.demo and args.save_baseline:
            try:
                analytics.save(Path(args.save_baseline))
                saved_baseline = True
            except RuntimeError as exc:
                print(f"[calib] save failed: {exc}", flush=True)

    fig.canvas.mpl_connect("key_press_event", on_key)
    print("[ui] keys: r = recalibrate empty room, s = save baseline", flush=True)

    def on_timer(_frame: int):
        nonlocal saved_baseline
        drain_queue(pkt_q, analytics)

        if (
            not analytics.calibrating
            and not saved_baseline
            and args.calibrate > 0
            and args.save_baseline
            and not args.demo
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
                (ln,) = ax_ts.plot(
                    xs, ys, lw=1.35, color=node_color(nid),
                    solid_capstyle="round", label=f"N{nid}",
                )
                lines[nid] = ln
                ax_ts.legend(loc="upper right", fontsize=8, handlelength=1.6)
            else:
                lines[nid].set_data(xs, ys)

        if snap:
            ax_ts.relim()
            ax_ts.autoscale_view()

        st = analytics.room_state()
        person = fake.person_xy if fake is not None else None
        live = analytics.live_nodes()
        draw_room(ax_room, analytics.latest_scores(), person, st, node_ids, live=live)
        draw_spec(ax_spec, analytics.spectrogram(), analytics.spectrogram_node())
        draw_events(ax_evt, analytics.event_lines())
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
            stats = getattr(r, "stats", {})
            port = getattr(r, "port", r.name)
            print(f"  {port}: {stats}")
        _ = anim


def run_snapshot(args: argparse.Namespace) -> None:
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    pkt_q: queue.Queue[CsiPacket] = queue.Queue(maxsize=QUEUE_MAX)
    stop_event = threading.Event()
    if args.calibrate == DEFAULT_CALIBRATE_S:
        args.calibrate = 5.0
    node_ids = [int(x) for x in args.demo_nodes.split(",") if x.strip()]
    fake = FakeCsiThread(pkt_q, stop_event, node_ids, calib_s=args.calibrate, hz=50.0)
    fake.start()
    analytics = PresenceAnalytics(calibrate_s=args.calibrate, occupy_thresh=args.thresh)

    t_end = time.time() + args.snapshot_seconds
    while time.time() < t_end:
        drain_queue(pkt_q, analytics)
        time.sleep(0.02)

    stop_event.set()
    fake.join(timeout=1.0)
    drain_queue(pkt_q, analytics)

    fig, ax_ts, ax_room, ax_spec, ax_evt = make_figure()
    status = setup_timeseries(ax_ts, args.thresh, title="Presence score")

    t0 = None
    for nid, hist in sorted(analytics.snapshot().items()):
        if not hist:
            continue
        if t0 is None:
            t0 = hist[0][0]
        xs = [t - t0 for t, _ in hist]
        ys = [s for _, s in hist]
        ax_ts.plot(
            xs, ys, lw=1.35, color=node_color(nid),
            solid_capstyle="round", label=f"N{nid}",
        )
    ax_ts.legend(loc="upper right", fontsize=8, handlelength=1.6)
    if args.calibrate > 0 and t0 is not None:
        ax_ts.axvline(args.calibrate, color=UI["faint"], ls=(0, (1, 2)), lw=0.9)

    st = analytics.room_state()
    draw_room(
        ax_room,
        analytics.latest_scores(),
        fake.person_xy,
        st,
        node_ids,
        live=analytics.live_nodes(),
    )
    draw_spec(ax_spec, analytics.spectrogram(), analytics.spectrogram_node())
    draw_events(ax_evt, analytics.event_lines())
    status.set_text(analytics.status_line())
    out = Path(args.snapshot)
    fig.savefig(out, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"[demo] snapshot saved -> {out.resolve()}")
    print(f"[demo] status: {analytics.status_line()}")


def main(argv: Optional[Iterable[str]] = None) -> None:
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    layout_path = Path(args.layout) if args.layout else None
    if layout_path and layout_path.is_file():
        load_room_layout(layout_path)
    else:
        load_room_layout(None)
        if args.layout:
            print(f"[layout] file missing ({args.layout}) — using default triangle", flush=True)
    if args.list_ports:
        for info in list_ports.comports():
            print(f"{info.device}\t{info.description}")
        return
    if args.snapshot:
        if not args.demo:
            args.demo = True
        run_snapshot(args)
        return
    run_monitor(args)


if __name__ == "__main__":
    main()
