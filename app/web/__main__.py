"""`python -m app.web` → uvicorn server.

The container-internal port is pinned to 8080 to match Dockerfile EXPOSE
and HEALTHCHECK and the compose route. WEB_PORT in .env is the *host* port
(via the docker-compose `ports:` mapping) — it does not affect the listener
inside the container.
"""
from __future__ import annotations

import uvicorn

CONTAINER_PORT = 8080


def main() -> None:
    uvicorn.run(
        "app.web:asgi",
        host="0.0.0.0",
        port=CONTAINER_PORT,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
