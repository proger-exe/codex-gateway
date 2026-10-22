from __future__ import annotations

import uvicorn

from codex_gateway.app import create_app
from codex_gateway.config import load_settings
from codex_gateway.logging_utils import setup_logging


def main() -> None:
    settings = load_settings()
    setup_logging(settings)
    uvicorn.run(create_app(), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
