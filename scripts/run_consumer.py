from pathlib import Path
import sys
import asyncio

# Ensure project root is on sys.path so `import app` works when running this script
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.streams.consumer import consume_logs


def main() -> None:
    """Run the Redis stream consumer.

    Usage: python scripts/run_consumer.py
    """
    asyncio.run(consume_logs())


if __name__ == "__main__":
    main()


