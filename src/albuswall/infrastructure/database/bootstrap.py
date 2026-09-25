#
""""""

from albuswall.core import Container
from albuswall.configue import ConfigField

from .connector import Connector


# noinspection bad-assignment
class Config:
    root = ConfigField("db", default={})
    must_exist: bool = ConfigField("db", "must_exist", default=False)
    db_file: str = ConfigField("files", "db", default="db.db")

config = Config()


def register_database(
        container  # type: Container
):
    # noinspection PyTypeChecker
    container.register(
        "db", lambda: Connector(
            container.get("configue").static.path.data / config.db_file,
            must_exist=config.must_exist
        ),
        returns=Container
    )

    container.final(lambda: container.get("db").close_all())
