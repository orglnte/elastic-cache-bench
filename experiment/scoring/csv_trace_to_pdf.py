#!/usr/bin/env python3
"""Render a 4-panel timeline PDF from a run_exp_artifact trace CSV.

SEPARATE + ON-DEMAND: this is NOT run per matrix cell. It exists to turn a
recorded trace (e.g. exp-1's reference runs) into a figure. Point it
at the CSV run_exp_artifact wrote:

  experiment/scoring/csv_trace_to_pdf.py <trace.csv> <out.pdf> [--title "KEDA reference"]
      [--events <trace_events.json>]

With --events it reads the trace's events for the ceiling and the
infra-gated mount, and draws the honest mount-delay window (onset -> mount)
right on the cache-lifecycle panel. Prints the output path on success.

Panels (shared x = time):
  1. Load envelope   — offered rps, the store ceiling, 2xx/s + 404/s.
  2. Latency vs SLO  — p99 latency against the SLO line.
  3. Cache lifecycle — pool_pressure, cache_mounted (app self-report) and
                       infra_cache_up (ground truth), with the onset and
                       mount markers + the measured mount-delay annotation.
  4. Failure events  — 429 shed, 5xx, transport errors, k6 drops (per-second).
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no display; CI-safe
import matplotlib.pyplot as plt

_DEFAULT_SLO_MS = 200.0


def _load(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="") as f:
        return list(csv.DictReader(f))


def _seconds(rows: list[dict[str, str]], ev: dict) -> list[float]:
    """Each line's second. A trace that numbered its lines (ts_s) keeps its
    numbers; one that only stamps the clock is placed from the load
    generator's first request, else from its own first line."""
    if rows and rows[0].get("ts_s") not in (None, ""):
        return _f(rows, "ts_s")
    epoch = _f(rows, "epoch")
    zero = ev.get("k6_start_epoch") or (epoch[0] if epoch else 0.0)
    return [round(e - float(zero), 3) for e in epoch]


def _f(rows: list[dict[str, str]], col: str) -> list[float]:
    out = []
    for r in rows:
        try:
            out.append(float(r.get(col, 0) or 0))
        except ValueError:
            out.append(0.0)
    return out


def _b(rows: list[dict[str, str]], col: str) -> list[bool]:
    return [str(r.get(col, "")).strip() == "True" for r in rows]


def _per_second_rate(cum: list[float]) -> list[float]:
    if not cum:
        return []
    return [max(0.0, cum[0])] + [max(0.0, cum[i] - cum[i - 1]) for i in range(1, len(cum))]


def _events(events: Path | None) -> dict:
    """The trace's event epochs (the file --events names), {} without one."""
    if events is None or not events.is_file():
        return {}
    try:
        return json.loads(events.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def render_single(csv_path: Path, out_path: Path, title: str, slo_ms: float,
                  events: Path | None = None) -> None:
    """One compact panel (paper column width): offered rps (left axis) + p99
    (right axis), the mounted window shaded, and onset/decision/mount/release
    markers. Everything the 4-panel view says, minus the per-signal breakdown."""
    rows = _load(csv_path)
    if not rows:
        raise SystemExit(f"empty CSV: {csv_path}")
    ev = _events(events)

    ts = _seconds(rows, ev)
    epoch = _f(rows, "epoch")
    offered = _f(rows, "offered_rps")
    p99 = _f(rows, "p99_ms")
    cache_app = _b(rows, "cache_mounted")
    infra = _b(rows, "infra_cache_up")
    ceiling = float(ev.get("ceiling_qps", 0) or 0)

    def ev_ts(key: str) -> float | None:
        v = ev.get(key)
        if v is None or not epoch or epoch[0] <= 0:
            return None
        return float(v) - epoch[0] + ts[0]

    onset_ts = next((ts[i] for i, v in enumerate(offered) if ceiling > 0 and v >= ceiling), None)
    decision_ts = ev_ts("decision_epoch")
    mount_ts = ev_ts("mount_epoch")
    if mount_ts is None:
        mount_ts = next((ts[i] for i in range(len(rows)) if cache_app[i] and infra[i]), None)
    release_ts = ev_ts("release_epoch")
    if release_ts is None and mount_ts is not None:
        release_ts = next((ts[i] for i in range(len(rows))
                           if ts[i] > mount_ts and not cache_app[i] and cache_app[i - 1]), None)

    fig, ax = plt.subplots(figsize=(6.6, 2.6))
    if title:
        ax.set_title(title, fontsize=10)

    if mount_ts is not None:
        ax.axvspan(mount_ts, release_ts if release_ts is not None else ts[-1],
                   color="#e6f2e6", zorder=0, label="cache mounted")
    ax.plot(ts, offered, color="#3355aa", linewidth=1.8, label="offered rps")
    if ceiling > 0:
        ax.axhline(ceiling, color="#aa3355", linestyle="--", linewidth=1.1,
                   label=f"store ceiling ({ceiling:g} qps)")
    ax.set_ylabel("requests / sec")
    ax.set_xlabel("time (s)")
    ax.set_ylim(bottom=0)
    ax.grid(True, alpha=0.25)

    ax2 = ax.twinx()
    ax2.plot(ts, p99, color="#cc4400", linewidth=1.6, label="p99 (ms)")
    ax2.axhline(slo_ms, color="#888888", linestyle=":", linewidth=1.0)
    ax2.set_ylabel("p99 latency (ms)")
    ax2.set_ylim(bottom=0)

    # onset/decision/mount cluster within ~10s — alternate label heights so the
    # annotations never overlap.
    for x, lbl, c, y in ((onset_ts, "onset", "#aa3355", 1.02),
                         (decision_ts, "decision", "#6a5acd", 1.10),
                         (mount_ts, "mount", "#338833", 1.02),
                         (release_ts, "release", "#338833", 1.02)):
        if x is not None:
            ax.axvline(x, color=c, linewidth=1.0, alpha=0.8)
            ax.annotate(lbl, xy=(x, y), xycoords=("data", "axes fraction"),
                        fontsize=7.5, color=c, ha="center")

    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=7.5, framealpha=0.9)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    print(out_path)


def render(csv_path: Path, out_path: Path, title: str, slo_ms: float,
           events: Path | None = None) -> None:
    rows = _load(csv_path)
    if not rows:
        raise SystemExit(f"empty CSV: {csv_path}")
    ev = _events(events)

    ts = _seconds(rows, ev)
    epoch = _f(rows, "epoch")
    offered = _f(rows, "offered_rps")
    p99 = _f(rows, "p99_ms")
    pressure = _f(rows, "pool_pressure")
    cache_app = _b(rows, "cache_mounted")
    infra = _b(rows, "infra_cache_up")
    ceiling = float(ev.get("ceiling_qps", 0) or 0)

    # onset ts = first tick offered >= ceiling; mount ts = from events (infra-
    # gated) mapped onto the ts axis via the row epochs, else first tick where both
    # app + infra agree.
    onset_ts = next((ts[i] for i, v in enumerate(offered) if ceiling > 0 and v >= ceiling), None)
    mount_ts = None
    mount_epoch = ev.get("mount_epoch")
    if mount_epoch is not None and epoch and epoch[0] > 0:
        mount_ts = float(mount_epoch) - epoch[0] + ts[0]
    if mount_ts is None:
        mount_ts = next((ts[i] for i in range(len(rows)) if cache_app[i] and infra[i]), None)
    mount_delay = (mount_ts - onset_ts) if (onset_ts is not None and mount_ts is not None) else None

    fig, (ax_load, ax_lat, ax_cache, ax_fail) = plt.subplots(
        4, 1, figsize=(11, 10), sharex=True, gridspec_kw={"hspace": 0.35}
    )
    fig.suptitle(title, fontsize=14, weight="bold")

    # Panel 1 — load envelope + ceiling + response rates.
    ax_load.plot(ts, offered, label="offered rps", color="#3355aa", linewidth=2)
    if ceiling > 0:
        ax_load.axhline(ceiling, color="#aa3355", linestyle="--", linewidth=1.4,
                        label=f"store ceiling (~{ceiling:g} qps)")
    ax_load.plot(ts, _per_second_rate(_f(rows, "http_2xx")), label="2xx /s",
                 color="#22aa55", linewidth=1.3, linestyle=":")
    ax_load.plot(ts, _per_second_rate(_f(rows, "http_404")), label="404 /s (app-state)",
                 color="#b8860b", linewidth=1.3, linestyle=":")
    ax_load.set_ylabel("requests / sec")
    ax_load.legend(loc="upper right", fontsize=8)
    ax_load.grid(True, alpha=0.3)
    ax_load.set_title("Load envelope + response rates", fontsize=10, loc="left")

    # Panel 2 — latency vs SLO.
    ax_lat.plot(ts, p99, label="p99 latency", color="#cc4400", linewidth=2)
    ax_lat.axhline(slo_ms, color="#888888", linestyle=":", label=f"SLO ({slo_ms:g} ms)")
    ax_lat.set_ylabel("latency (ms)")
    ax_lat.legend(loc="upper right", fontsize=8)
    ax_lat.grid(True, alpha=0.3)
    ax_lat.set_title("Latency vs SLO", fontsize=10, loc="left")

    # Panel 3 — cache lifecycle + pool pressure + the mount-delay window.
    ax_cache.plot(ts, pressure, label="pool_pressure", color="#6a5acd", linewidth=2)
    ax_cache.plot(ts, [1.0 if m else 0.0 for m in cache_app], color="#338833",
                  linewidth=2, drawstyle="steps-post", label="cache_mounted (app)")
    ax_cache.plot(ts, [1.0 if s else 0.0 for s in infra], color="#1f77b4",
                  linewidth=1.6, linestyle="--", drawstyle="steps-post",
                  label="infra_cache_up (ground truth)")
    if onset_ts is not None:
        ax_cache.axvline(onset_ts, color="#aa3355", linestyle="-", linewidth=1.2)
        ax_cache.text(onset_ts, 1.06, "onset", color="#aa3355", fontsize=8, ha="center")
    if mount_ts is not None:
        ax_cache.axvline(mount_ts, color="#338833", linestyle="-", linewidth=1.2)
        ax_cache.text(mount_ts, 1.06, "mount", color="#338833", fontsize=8, ha="center")
    if mount_delay is not None and onset_ts is not None and mount_ts is not None:
        ax_cache.annotate(
            f"mount-delay = {mount_delay:.1f}s",
            xy=((onset_ts + mount_ts) / 2, 0.5), ha="center", fontsize=9,
            color="#333333",
            bbox=dict(boxstyle="round,pad=0.3", fc="#ffffcc", ec="#999999"),
        )
    ax_cache.set_ylim(-0.05, 1.2)
    ax_cache.set_ylabel("pressure / state")
    ax_cache.legend(loc="upper right", fontsize=8)
    ax_cache.grid(True, alpha=0.3)
    ax_cache.set_title("Cache lifecycle + control-loop latency", fontsize=10, loc="left")

    # Panel 4 — failure events (per-second).
    ax_fail.plot(ts, _per_second_rate(_f(rows, "http_429")), label="429 shed /s",
                 color="#cc0000", linewidth=1.6, drawstyle="steps-post")
    ax_fail.plot(ts, _per_second_rate(_f(rows, "http_5xx")), label="5xx /s",
                 color="#ff8800", linewidth=1.4, drawstyle="steps-post")
    ax_fail.plot(ts, _per_second_rate(_f(rows, "http_err")), label="transport err /s",
                 color="#4488cc", linewidth=1.3, linestyle="--", drawstyle="steps-post")
    ax_fail.plot(ts, _per_second_rate(_f(rows, "k6_drop")), label="k6 drops /s",
                 color="#884488", linewidth=1.3, linestyle="--", drawstyle="steps-post")
    ax_fail.set_ylabel("events / sec")
    ax_fail.set_xlabel("time (s)")
    ax_fail.set_ylim(bottom=0)
    ax_fail.legend(loc="upper left", fontsize=8)
    ax_fail.grid(True, alpha=0.3)
    ax_fail.set_title("Failure events", fontsize=10, loc="left")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    print(out_path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--title", default=None)
    ap.add_argument("--slo-ms", type=float, default=_DEFAULT_SLO_MS)
    ap.add_argument("--events", type=Path, default=None,
                    help="the trace's trace_events.json: the ceiling and the mount markers")
    ap.add_argument("--single", action="store_true",
                    help="one compact panel (paper column width) instead of the 4-panel view")
    args = ap.parse_args()
    if not args.csv.is_file():
        raise SystemExit(f"input not found: {args.csv}")
    if args.single:
        render_single(args.csv, args.out, args.title or "", args.slo_ms, args.events)
    else:
        title = args.title or f"run_exp_artifact trace — {args.csv.stem}"
        render(args.csv, args.out, title, args.slo_ms, args.events)


if __name__ == "__main__":
    main()
