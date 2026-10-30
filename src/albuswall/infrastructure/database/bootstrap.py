#
""""""

from albuswall.configue import configclass
from albuswall.core import Application, Container

from .connector import Connector


# noinspection bad-assignment
@configclass("db", "main")
class Config:
    """``[db]`` 段。

    ``db_file`` 在旧包里是 ``ConfigField("files", "db")``（挂在 ``[files]`` 段），
    迁移后由 ``configue.bootstrap`` 的 ``[files]`` 声明统一提供
    （``tree.files.db``），这里只留 ``[db]`` 自己的键。

    ``root = ConfigField("db", default={})`` 的「确保段存在」不再需要：
    新包只支持 2 层 path，空段由第一次写入时自然产生。
    """

    must_exist: bool = False


def register_database(
        container: Container
):
    def _make_connector() -> Connector:
        config = container.require("configue")
        tree = config.tree.main
        return Connector(
            tree.path.data / tree.files.db,
            must_exist=Config().must_exist,
        )

    container.register("db", _make_connector, returns=Connector)

    @Application.on_init
    def _keep_db_alive():
        container.get("db").keep_alive()  # 主线程持锚

    @Application.on_exit
    def _release_db_alive():
        container.get("db").release_keep_alive()
