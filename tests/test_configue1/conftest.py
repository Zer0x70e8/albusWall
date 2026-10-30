#
"""``albuswall.configue1`` 的测试隔离。

``configue1`` 的登记表是**进程全局**的（这是设计：导入期注册、运行期只读），
所以每个用例前后都必须：

1. 关掉上一个单例（顺带 flush + 注销 atexit）；
2. 清空 ``_FILES`` / ``_CLASSES`` / ``_PATHS`` / ``_FIELD_OWNER``（原地 clear，
   保住 dict 身份——``configue.py`` / ``tree.py`` 在 import 期拿到的是这些 dict
   对象本身）；
3. 清空 ``_LOAD_HOOKS``（长期注册，``load()`` 不清）与
   ``_PENDING_CONVERTERS``（构造前登记、``load()`` 消费）；
4. 把 ``Configue._instance`` 置回 ``None``。
"""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from albuswall.configue1 import _LOAD_HOOKS, _PENDING_CONVERTERS, Configue
from albuswall.configue1.registry import _CLASSES, _FIELD_OWNER, _FILES, _PATHS


def _teardown_singleton() -> None:
    instance = Configue._instance
    if instance is not None:
        with suppress(Exception):  # 清理路径不能影响用例结果
            instance.close()
    _FILES.clear()
    _CLASSES.clear()
    _PATHS.clear()
    _FIELD_OWNER.clear()
    _LOAD_HOOKS.clear()
    _PENDING_CONVERTERS.clear()
    Configue._instance = None


@pytest.fixture(autouse=True)
def isolated_configue1():
    """每个用例一张全新的登记表 + 全新单例。"""
    _teardown_singleton()
    try:
        yield
    finally:
        _teardown_singleton()


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    return tmp_path


def write_ini(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
