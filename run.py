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
    # proxy_headers=False: uvicorn would otherwise trust X-Forwarded-For/-Proto
    # from 127.0.0.1 on its own. The app decides that, from
    # HEARTHMAGE_TRUSTED_PROXIES (see _security in app.py).
    uvicorn.run(app, host=s.bind_host, port=s.port, proxy_headers=False)
