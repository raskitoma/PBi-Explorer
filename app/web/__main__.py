"""`python -m app.web` → uvicorn server."""
from __future__ import annotations

import uvicorn

from app.core.config import settings


def main() -> None:
    uvicorn.run(
        "app.web:asgi",
        host="0.0.0.0",
        port=settings().web_port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
