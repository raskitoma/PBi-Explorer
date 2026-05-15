"""Worker entrypoint shim — `python -m app.worker`."""
from app.ingest.runner import main

if __name__ == "__main__":
    main()
