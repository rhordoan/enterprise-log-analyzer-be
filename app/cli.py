from __future__ import annotations

import argparse
from pathlib import Path

from app.services.chroma_service import ChromaClientProvider
from app.services.ingest_templates import ingest_all_data_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Enterprise Log Analyzer CLI")
    parser.add_argument(
        "command",
        choices=["ingest-templates"],
        help="Command to run",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(Path(__file__).resolve().parents[1] / "data"),
        help="Path to data directory containing *_templates.csv",
    )
    args = parser.parse_args()

    if args.command == "ingest-templates":
        provider = ChromaClientProvider()
        report = ingest_all_data_dir(Path(args.data_dir), provider)
        for os_name, count in report.items():
            print(f"Ingested {count} templates into collection for {os_name}")


if __name__ == "__main__":
    main()


