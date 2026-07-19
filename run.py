import logging
import os

import uvicorn

from hearthmage.app import app
from hearthmage.settings import Settings, config_path

if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("HEARTHMAGE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    s = Settings(config_path())
    uvicorn.run(app, host=s.bind_host, port=s.port)
