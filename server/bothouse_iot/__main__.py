"""python -m bothouse_iot --config config.toml"""
import argparse
import asyncio

from .app import App
from .config import load


def main() -> None:
    ap = argparse.ArgumentParser(prog="bothouse-iot", description="bothouse-iot device service + bothouse adapter")
    ap.add_argument("--config", default="config.toml")
    a = ap.parse_args()
    app = App(load(a.config))
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
