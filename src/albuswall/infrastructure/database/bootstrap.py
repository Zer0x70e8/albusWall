#
""""""

from albuswall.core import Container
from albuswall.configue import ConfigField

from .connector import Connector


# noinspection bad-assignment
class Config:
    root = ConfigField("db", default={})  # Ensure section.
    must_exist: bool = ConfigField("db", "must_exist", default=False)
    db_file: str = ConfigField("files", "db", default="db.db")


config = Config()


def register_database(
        container  # type: Container
):
    container.register("db", lambda: Connector(
        container.get("configue").static.path.data / config.db_file,
        must_exist=config.must_exist
    ), returns=Connector)

    @container.on_boot
    def _keep_db_alive():
        container.get("db").keep_alive()  # 主线程持锚

    @container.on_final
    def _release_db_alive():
        container.get("db").release_keep_alive()
