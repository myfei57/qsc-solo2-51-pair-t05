"""Start up settings for the control console."""

import argparse
from dataclasses import dataclass

from .brine.water_quality import WaterType


@dataclass(frozen=True)
class Config:
    """Runtime settings resolved once at start up."""

    host: str = "127.0.0.1"
    port: int = 8080
    data_dir: str = "data"
    water: str = "brackish"

    def listen(self) -> tuple[str, int]:
        """Address tuple handed to the HTTP server."""
        return (self.host, self.port)

    def store_path(self) -> str:
        """Location of the file-backed key space."""
        return f"{self.data_dir}/plant.json"


def parse_args(argv: list[str] | None = None) -> Config:
    """Build a :class:`Config` from command line arguments."""
    parser = argparse.ArgumentParser(prog="python -m desalplant")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", dest="data_dir", default="data")
    parser.add_argument(
        "--water",
        dest="water",
        choices=[w.value for w in WaterType],
        default=WaterType.BRACKISH.value,
        help="feedwater type; fixes whether emergency exit is stepped or one-shot",
    )
    namespace = parser.parse_args(argv)
    return Config(
        host=namespace.host,
        port=namespace.port,
        data_dir=namespace.data_dir,
        water=namespace.water,
    )
