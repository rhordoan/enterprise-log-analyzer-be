import argparse
import os
import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Enterprise Log Analyzer API")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--with-producer", action="store_true", help="Start the log producer in-process")
    parser.add_argument("--with-enricher", action="store_true", help="Start the enricher in-process")

    args = parser.parse_args()

    if args.with_producer:
        os.environ["ENABLE_PRODUCER"] = "1"
    if args.with_enricher:
        os.environ["ENABLE_ENRICHER"] = "1"

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()


