#!/usr/bin/env python3
"""Compare monitoring data across multiple test runs.

Reads monitoring_*-<label>/ directories and generates PNG comparison
charts overlaying CPU, memory, and disk metrics for each run.
"""

import argparse
import os
import re
import sys

try:
    import matplotlib.pyplot as plt
    import pandas as pd
except ImportError:
    print("ERROR: matplotlib and pandas are required.")
    print("Install with:")
    print(
        "  UV_NATIVE_TLS=1 uv pip install --python venv/bin/python3 matplotlib pandas"
    )
    sys.exit(1)


def discover_runs(base_dir):
    runs = []
    for entry in sorted(os.listdir(base_dir)):
        match = re.match(r"monitoring_(\d{8}_\d{6})-(.+)", entry)
        if match:
            full_path = os.path.join(base_dir, entry)
            if os.path.isdir(full_path):
                runs.append((match.group(1), match.group(2), full_path))
        elif entry.startswith("monitoring_") and os.path.isdir(
            os.path.join(base_dir, entry)
        ):
            print(f"  [skip] {entry} (no label suffix)")
    runs.sort(key=lambda x: x[0])
    return [(label, path) for _, label, path in runs]


def load_csv(path):
    if not os.path.isfile(path):
        return None
    try:
        df = pd.read_csv(path)
        return df if not df.empty else None
    except Exception as e:
        print(f"  [warn] Failed to read {path}: {e}")
        return None


def plot_container_comparison(runs, container, output_path, dpi=150):
    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    fig.suptitle(
        f"{container} — Comparison Across Runs", fontsize=14, fontweight="bold"
    )

    metrics = [
        {
            "ax": axes[0, 0],
            "file": f"{container}_podman_stats.csv",
            "y": "mem_usage_mb",
            "title": "Container Memory (podman stats)",
            "ylabel": "Memory (MB)",
        },
        {
            "ax": axes[0, 1],
            "file": f"{container}_process_memory.csv",
            "y": "vm_rss_kb",
            "title": "Process RSS Memory (/proc)",
            "ylabel": "VmRSS (MB)",
            "transform": lambda v: v / 1024,
        },
        {
            "ax": axes[1, 0],
            "file": f"{container}_podman_stats.csv",
            "y": "cpu_perc",
            "title": "CPU Usage",
            "ylabel": "CPU (%)",
        },
        {
            "ax": axes[1, 1],
            "file": f"{container}_disk_usage.csv",
            "y": "disk_mb",
            "title": "Disk Usage",
            "ylabel": "Disk (MB)",
        },
    ]

    lines_for_legend = []
    labels_for_legend = []

    for label, run_path in runs:
        display_label = label.replace("_", " ")
        first_line = None

        for m in metrics:
            df = load_csv(os.path.join(run_path, m["file"]))
            if df is None or m["y"] not in df.columns:
                continue

            x = df["elapsed_min"]
            y = df[m["y"]]
            if "transform" in m:
                y = m["transform"](y)

            (line,) = m["ax"].plot(x, y, label=display_label, linewidth=1.5, alpha=0.85)
            if first_line is None:
                first_line = line

        if first_line is not None:
            lines_for_legend.append(first_line)
            labels_for_legend.append(display_label)

    for m in metrics:
        ax = m["ax"]
        ax.set_title(m["title"], fontsize=11)
        ax.set_xlabel("Elapsed Time (min)", fontsize=10)
        ax.set_ylabel(m["ylabel"], fontsize=10)
        ax.grid(True, alpha=0.3)
        ax.tick_params(labelsize=9)

    fig.legend(
        lines_for_legend,
        labels_for_legend,
        loc="lower center",
        ncol=min(4, len(runs)),
        fontsize=9,
        framealpha=0.9,
        bbox_to_anchor=(0.5, -0.02),
    )

    fig.tight_layout(rect=[0, 0.08, 1, 0.96])
    fig.savefig(output_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  Saved: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Compare monitoring data across test runs"
    )
    parser.add_argument(
        "--base-dir",
        default=os.path.dirname(os.path.abspath(__file__)),
        help="Directory containing monitoring_* folders",
    )
    parser.add_argument(
        "--output-dir", default=None, help="Where to save PNGs (default: base-dir)"
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="Only include specific labels (repeatable)",
    )
    parser.add_argument(
        "--no-postgres", action="store_true", help="Skip postgres comparison"
    )
    parser.add_argument(
        "--dpi", type=int, default=150, help="Output DPI (default: 150)"
    )
    args = parser.parse_args()

    output_dir = args.output_dir or args.base_dir

    print("Discovering monitoring runs...")
    runs = discover_runs(args.base_dir)

    if args.only:
        runs = [(label, path) for label, path in runs if label in args.only]

    if not runs:
        print("ERROR: No monitoring runs found.")
        sys.exit(1)

    print(f"Found {len(runs)} runs: {', '.join(label for label, _ in runs)}\n")

    print("Generating insights-app comparison...")
    plot_container_comparison(
        runs,
        "insights-app",
        os.path.join(output_dir, "comparison_insights-app.png"),
        dpi=args.dpi,
    )

    if not args.no_postgres:
        print("Generating insights-postgres comparison...")
        plot_container_comparison(
            runs,
            "insights-postgres",
            os.path.join(output_dir, "comparison_insights-postgres.png"),
            dpi=args.dpi,
        )

    print("\nDone.")


if __name__ == "__main__":
    main()
