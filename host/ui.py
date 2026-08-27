from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

import matplotlib
import numpy as np

import ingest
from analytics import RoomState

UI = {
    "fig": "#F5F6F8",
    "ax": "#FFFFFF",
    "ink": "#1C1C1C",
    "muted": "#6B7280",
    "faint": "#9CA3AF",
    "rule": "#E5E7EB",
    "grid": "#ECEEF1",
    "thresh": "#9CA3AF",
    "estimate": "#0F766E",
    "truth": "#1D4ED8",
    "node": ("#1D4ED8", "#0F766E", "#B45309", "#7C3AED"),
    "heat": "cividis",
    "state": {
        "EMPTY": "#047857",
        "STATIC": "#B45309",
        "MOTION": "#B91C1C",
        "CALIBRATING": "#6B7280",
    },
}


def apply_ui_style() -> None:
    matplotlib.rcParams.update(
        {
            "figure.facecolor": UI["fig"],
            "axes.facecolor": UI["ax"],
            "axes.edgecolor": UI["rule"],
            "axes.labelcolor": UI["muted"],
            "axes.titlecolor": UI["ink"],
            "axes.titleweight": "normal",
            "axes.titlesize": 10.5,
            "axes.labelsize": 8.5,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.linewidth": 0.7,
            "xtick.color": UI["faint"],
            "ytick.color": UI["faint"],
            "xtick.labelcolor": UI["muted"],
            "ytick.labelcolor": UI["muted"],
            "xtick.labelsize": 7.5,
            "ytick.labelsize": 7.5,
            "grid.color": UI["grid"],
            "grid.linewidth": 0.55,
            "grid.alpha": 1.0,
            "font.family": "sans-serif",
            "font.sans-serif": [
                "Segoe UI",
                "Helvetica Neue",
                "Arial",
                "DejaVu Sans",
            ],
            "font.size": 9,
            "text.color": UI["ink"],
            "legend.frameon": False,
            "legend.fontsize": 7.5,
            "figure.dpi": 110,
        }
    )


def node_color(nid: int) -> str:
    palette = UI["node"]
    return palette[(int(nid) - 1) % len(palette)]


def style_ax(ax, *, grid_y: bool = False) -> None:
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(UI["rule"])
        ax.spines[spine].set_linewidth(0.8)
    ax.tick_params(colors=UI["faint"], labelcolor=UI["muted"], length=3, width=0.6)
    if grid_y:
        ax.yaxis.grid(True, color=UI["grid"], linewidth=0.6)
        ax.xaxis.grid(False)
        ax.set_axisbelow(True)
    else:
        ax.grid(False)


def _active_node_ids(scores: Dict[int, float], fallback: Iterable[int]) -> List[int]:
    ids = sorted(scores.keys()) if scores else sorted(fallback)
    return [n for n in ids if n in ingest.NODE_XY]


def draw_room(
    ax,
    scores: Dict[int, float],
    person: Optional[Tuple[float, float]],
    state: RoomState,
    node_ids: Iterable[int],
    live: Optional[Iterable[int]] = None,
) -> None:
    ax.clear()
    ax.set_facecolor(UI["ax"])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    # Match real room aspect so the panel is a scaled floor plan, not a fake square.
    ax.set_aspect(ingest.ROOM_HEIGHT_M / ingest.ROOM_WIDTH_M if ingest.ROOM_WIDTH_M > 0 else 1.0)
    ax.set_title(
        f"{ingest.ROOM_NAME}  ·  {ingest.ROOM_WIDTH_M:.1f} × {ingest.ROOM_HEIGHT_M:.1f} m",
        pad=8,
    )
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    ax.set_xticks([0, 0.5, 1])
    ax.set_yticks([0, 0.5, 1])
    ax.set_xticklabels(
        ["0", f"{ingest.ROOM_WIDTH_M/2:.1f}", f"{ingest.ROOM_WIDTH_M:.1f}"]
    )
    ax.set_yticklabels(
        ["0", f"{ingest.ROOM_HEIGHT_M/2:.1f}", f"{ingest.ROOM_HEIGHT_M:.1f}"]
    )
    style_ax(ax)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.add_patch(
        matplotlib.patches.Rectangle(
            (0.04, 0.04), 0.92, 0.92,
            fill=False, lw=1.0, edgecolor=UI["rule"],
        )
    )

    live_set = set(live) if live is not None else None
    active = _active_node_ids(scores, node_ids)
    for nid in sorted(set(active) | set(node_ids)):
        if nid not in ingest.NODE_XY:
            continue
        if nid not in active:
            active.append(nid)
    active = sorted(set(active))
    vmax = max([scores.get(n, 0.0) for n in active] + [1.0])

    for nid in active:
        x, y = ingest.NODE_XY[nid]
        s = scores.get(nid, 0.0)
        is_live = True if live_set is None else (nid in live_set)
        intensity = min(1.0, s / vmax) if is_live else 0.0
        base = node_color(nid)
        if is_live and intensity > 0.05:
            ax.add_patch(
                matplotlib.patches.Circle(
                    (x, y),
                    0.06 + 0.10 * intensity,
                    color=base,
                    alpha=0.10 + 0.28 * intensity,
                    lw=0,
                )
            )
        disc_alpha = 0.95 if is_live else 0.35
        disc_color = base if is_live else UI["faint"]
        ax.add_patch(
            matplotlib.patches.Circle(
                (x, y), 0.038, facecolor=disc_color, edgecolor=UI["ax"],
                lw=1.4, alpha=disc_alpha,
            )
        )
        ax.text(
            x, y, f"{nid}", ha="center", va="center",
            fontsize=8, fontweight="bold", color=UI["ax"] if is_live else UI["muted"],
        )
        score_txt = f"{s:.0f}" if is_live else "--"
        ax.text(
            x, y - 0.075, score_txt, ha="center", va="top",
            fontsize=7.5, color=UI["ink"] if is_live else UI["faint"],
        )

    if state.estimate_xy is not None and state.label != "EMPTY":
        ex, ey = state.estimate_xy
        ax.add_patch(
            matplotlib.patches.Circle(
                (ex, ey), 0.07, fill=False, ls=(0, (2.5, 2)),
                color=UI["estimate"], lw=1.0, alpha=0.9,
            )
        )
        ax.plot(
            ex, ey, marker="+", ms=10, mew=1.4,
            color=UI["estimate"], label="estimate",
        )

    if person is not None:
        ax.plot(
            person[0], person[1], marker="o", ms=7,
            color=UI["truth"], markeredgecolor=UI["ax"],
            markeredgewidth=1.2, label="sim",
        )

    badge = UI["state"].get(state.label, UI["ink"])
    ax.text(
        0.08, 0.92,
        state.label,
        ha="left", va="top",
        fontsize=10, fontweight="bold", color=badge,
    )
    ax.text(
        0.92, 0.92,
        f"{state.confidence:.0%}",
        ha="right", va="top",
        fontsize=9, color=UI["muted"],
    )
    if person is not None or (state.estimate_xy is not None and state.label != "EMPTY"):
        ax.legend(loc="lower center", fontsize=7, ncol=2, handlelength=1.2)


def draw_events(ax, lines: List[str]) -> None:
    ax.clear()
    ax.set_facecolor(UI["ax"])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title("Events", loc="left", pad=10)
    ax.axvline(0.02, ymin=0.06, ymax=0.90, color=UI["rule"], lw=1.2, clip_on=False)
    if not lines:
        ax.text(
            0.06, 0.78, "waiting for transitions",
            fontsize=8.5, color=UI["faint"], transform=ax.transAxes,
        )
        return
    y = 0.84
    for line in lines[:7]:
        pretty = line.replace(" -> ", "  →  ")
        ax.text(
            0.06, y, pretty,
            va="top", ha="left",
            family="monospace", fontsize=7.5,
            color=UI["ink"], transform=ax.transAxes,
        )
        y -= 0.11


def draw_spec(ax, spec: Optional[np.ndarray], node_id: Optional[int] = None) -> None:
    ax.clear()
    ax.set_facecolor(UI["ax"])
    title = "CSI amplitude"
    if node_id is not None:
        title += f"  ·  N{node_id}"
    ax.set_title(title, pad=10)
    style_ax(ax)
    if spec is None:
        ax.text(
            0.5, 0.5, "waiting for CSI",
            ha="center", va="center", color=UI["faint"], fontsize=9,
            transform=ax.transAxes,
        )
        ax.set_xticks([])
        ax.set_yticks([])
        return
    ax.imshow(
        spec,
        aspect="auto",
        origin="lower",
        cmap=UI["heat"],
        interpolation="nearest",
    )
    ax.set_xlabel("subcarrier")
    ax.set_ylabel("recent →")
    ax.tick_params(length=0)


def make_figure():
    import matplotlib.pyplot as plt

    apply_ui_style()
    fig = plt.figure(figsize=(13.2, 7.2), layout="constrained")
    fig.patch.set_facecolor(UI["fig"])
    gs = fig.add_gridspec(
        2, 2,
        height_ratios=[2.15, 1.15],
        hspace=0.12,
        wspace=0.10,
    )
    ax_ts = fig.add_subplot(gs[0, 0])
    ax_room = fig.add_subplot(gs[0, 1])
    ax_spec = fig.add_subplot(gs[1, 0])
    ax_evt = fig.add_subplot(gs[1, 1])
    for ax in (ax_ts, ax_room, ax_spec, ax_evt):
        ax.set_facecolor(UI["ax"])
    return fig, ax_ts, ax_room, ax_spec, ax_evt


def setup_timeseries(ax_ts, thresh: float, title: str = "Presence score") -> object:
    ax_ts.set_xlabel("time (s)")
    ax_ts.set_ylabel("disturbance")
    ax_ts.set_title(title, pad=10)
    style_ax(ax_ts, grid_y=True)
    ax_ts.axhline(thresh, color=UI["thresh"], ls=(0, (4, 3)), lw=0.9, alpha=0.9)
    status = ax_ts.text(
        0.0,
        1.02,
        "",
        transform=ax_ts.transAxes,
        va="bottom",
        ha="left",
        fontsize=8,
        family="monospace",
        color=UI["muted"],
    )
    return status
