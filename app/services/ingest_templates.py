from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Tuple

from app.services.chroma_service import ChromaClientProvider, collection_name_for_os


def read_templates(csv_path: Path) -> Tuple[list[str], list[str]]:
    ids: list[str] = []
    texts: list[str] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            event_id = str(row.get("EventId") or "").strip()
            template = str(row.get("EventTemplate") or "").strip()
            if not template:
                continue
            ids.append(event_id or f"auto_{len(ids)}")
            texts.append(template)
    return ids, texts


def ingest_csv_to_collection(os_name: str, csv_path: Path, provider: ChromaClientProvider) -> int:
    collection_name = collection_name_for_os(os_name)
    collection = provider.get_or_create_collection(collection_name)

    ids, texts = read_templates(csv_path)
    if not texts:
        return 0

    # Upsert templates with metadata
    metadatas = [{"os": os_name, "source": str(csv_path), "event_id": ids[i]} for i in range(len(ids))]
    collection.upsert(ids=ids, documents=texts, metadatas=metadatas)
    return len(texts)


def ingest_all_data_dir(data_dir: Path, provider: ChromaClientProvider) -> dict[str, int]:
    report: dict[str, int] = {}
    mapping = {
        "macos": "Mac_2k.log_templates.csv",
        "linux": "Linux_2k.log_templates.csv",
        "windows": "Windows_2k.log_templates.csv",
    }

    for os_name, filename in mapping.items():
        csv_path = data_dir / filename
        if csv_path.exists():
            count = ingest_csv_to_collection(os_name, csv_path, provider)
            report[os_name] = count
    return report


