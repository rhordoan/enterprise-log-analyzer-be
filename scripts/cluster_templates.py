from __future__ import annotations

import argparse

from app.services.clustering_service import cluster_os


def main() -> None:
    parser = argparse.ArgumentParser(description="Cluster templates/logs into proto_<os> collections")
    parser.add_argument("--os", dest="oses", action="append", choices=["linux", "macos", "windows"], help="OS key to process; can be repeated. Defaults to all.")
    parser.add_argument("--include-logs", type=int, default=0, help="Sample N items from logs_<os> to include in clustering")
    parser.add_argument("--threshold", type=float, default=None, help="Cosine distance threshold for clustering")
    parser.add_argument("--min-size", type=int, default=None, help="Minimum cluster size")
    args = parser.parse_args()

    oses = args.oses or ["linux", "macos", "windows"]
    for os_name in oses:
        report = cluster_os(os_name, include_logs_samples=args.include_logs, threshold=args.threshold, min_size=args.min_size)
        print(f"Clustered {os_name}: {report}")


if __name__ == "__main__":
    main()


