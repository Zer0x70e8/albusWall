#
"""``albuswall.common.keymap.ShortcutRecord``：配置值的解析 / 格式化。

它是「快捷键表」在配置器里的**值类型**（纯标准库，不 import Qt），所以这里
只测它自己的契约：

* 四种写法（简写数组 / 单个键位 / 完整三元组 / 空）都能解析；
* ``from_ini(to_ini(r)) == r`` 往返恒等；
* 非法 JSON / 非法结构 → ``ValueError``（配置层会把它当「读不出来的值」）；
* 不校验键位**语法**（那是 UI 层 ``parse_sequences`` 的事）——这里只保证
  结构合法、键位是字符串。
"""

from __future__ import annotations

import pytest

from albuswall.common.keymap import SECTION, ShortcutRecord, type_shortcut_keys


# ==================== 解析 ====================

@pytest.mark.parametrize(("raw", "keys"), [
    ('["F5"]', ("F5",)),
    ('["Ctrl+Shift+P"]', ("Ctrl+Shift+P",)),
    ('["G, G"]', ("G, G",)),
    ('["F11", "Ctrl+Shift+F"]', ("F11", "Ctrl+Shift+F")),
    ('"Ctrl+Q"', ("Ctrl+Q",)),
    ("Ctrl+Q", ("Ctrl+Q",)),          # 裸写（INI 风格，不需要引号）
    ("[]", ()),
    ("", ()),
    ("   ", ()),
])
def test_parse_shorthand_forms(raw: str, keys: tuple[str, ...]) -> None:
    record = ShortcutRecord.from_ini(raw)

    assert record.keys == keys
    assert record.args == ()
    assert record.kwargs == {}
    assert record.enabled == bool(keys)


def test_parse_full_record() -> None:
    record = ShortcutRecord.from_ini('["Alt+1", [3], {"mode": "fit"}]')

    assert record.keys == ("Alt+1",)
    assert record.args == (3,)
    assert record.kwargs == {"mode": "fit"}


def test_parse_full_record_with_several_keys() -> None:
    record = ShortcutRecord.from_ini(
        '[["F11", "Ctrl+Shift+F"], [], {}]'
    )

    assert record.keys == ("F11", "Ctrl+Shift+F")
    assert record.args == ()


def test_parse_none_is_an_empty_record() -> None:
    """INI 的「有 key 没 value」会给出 ``None`` / 空串：都算没有键位。"""
    assert ShortcutRecord.from_ini(None) == ShortcutRecord()


def test_bare_text_is_one_key_even_if_it_looks_like_json() -> None:
    """不以 ``[`` / ``"`` 开头的值一律当**单个键位**（与旧解析器一致）。

    ``{"keys": []}`` 这种写法不是「JSON 记录」而是「键位文本」——它会在绑定
    阶段被 ``parse_sequences`` 拒掉（那时才是真的错）。
    """
    assert ShortcutRecord.from_ini('{"keys": []}').keys == ('{"keys": []}',)


def test_parse_accepts_json_native_structures() -> None:
    """JSON 后端（将来）会直接给 list，不走字符串解析。"""
    assert ShortcutRecord.from_ini(["F5"]).keys == ("F5",)
    assert ShortcutRecord.from_ini(
        ["Alt+1", [1], {"mode": "x"}]
    ).kwargs == {"mode": "x"}


@pytest.mark.parametrize("raw", [
    '[',                                    # JSON 坏了
    '["Alt+1", "1", {}]',                   # 位置参数不是数组
    '["Alt+1", [1], []]',                   # 关键字参数不是对象
    '[[1], [1], {}]',                       # 键位不是字符串
    '["Alt+1", [1]]',                       # 少了第三项且不是扁平数组
])
def test_bad_records_raise_value_error(raw: str) -> None:
    with pytest.raises(ValueError):
        ShortcutRecord.from_ini(raw)


# ==================== 格式化 ====================

@pytest.mark.parametrize("record", [
    ShortcutRecord(),
    ShortcutRecord(keys=("F5",)),
    ShortcutRecord(keys=("F11", "Ctrl+Shift+F")),
    ShortcutRecord(keys=("G, G",)),
    ShortcutRecord(keys=("Alt+1",), args=(1, "x"), kwargs={"mode": "fit"}),
    ShortcutRecord(args=(1,), kwargs={"中文": "值"}),
])
def test_round_trip(record: ShortcutRecord) -> None:
    assert ShortcutRecord.from_ini(record.to_ini()) == record


def test_to_ini_writes_the_canonical_triple() -> None:
    record = ShortcutRecord(keys=("Alt+1",), args=(1,), kwargs={"mode": "fit"})

    assert record.to_ini() == '["Alt+1", [1], {"mode": "fit"}]'


def test_to_ini_keeps_non_ascii_readable() -> None:
    text = ShortcutRecord(kwargs={"模式": "适应"}).to_ini()

    assert "模式" in text


def test_to_binding_returns_plain_tuple() -> None:
    record = ShortcutRecord(keys=("F5",), args=(1,), kwargs={"a": 1})

    keys, args, kwargs = record.to_binding()

    assert keys == ("F5",)
    assert args == (1,)
    assert kwargs == {"a": 1}


# ==================== 值语义 ====================

def test_record_coerces_sequences_into_tuples() -> None:
    record = ShortcutRecord(keys=["F5"], args=[1], kwargs={"a": 1})  # type: ignore[arg-type]

    assert record.keys == ("F5",)
    assert record.args == (1,)


def test_record_rejects_non_mapping_kwargs() -> None:
    with pytest.raises(TypeError):
        ShortcutRecord(kwargs=[("a", 1)])  # type: ignore[arg-type]


def test_section_name_is_the_config_section() -> None:
    assert SECTION == "shortcuts"


# ==================== 加载钩子 ====================

class _FakeResolver:
    """只实现 ``type_shortcut_keys`` 需要的那三个方法。"""

    def __init__(self, keys: list[str], values: dict[str, str] | None = None) -> None:
        self._keys = keys
        self._values = values or {key: "[]" for key in keys}
        self.types: dict[tuple[str, str], object] = {}

    def keys_of(self, section: str) -> list[str]:
        return list(self._keys) if section == SECTION else []

    def get_str(self, section: str, key: str) -> str:
        assert section == SECTION
        return self._values[key]

    def register_type(self, section: str, key: str, converter: object) -> None:
        self.types[(section, key)] = converter


def test_type_shortcut_keys_registers_every_key() -> None:
    resolver = _FakeResolver(["app.quit", "window.close"])

    type_shortcut_keys(resolver)

    assert resolver.types == {
        (SECTION, "app.quit"): ShortcutRecord,
        (SECTION, "window.close"): ShortcutRecord,
    }


def test_type_shortcut_keys_skips_broken_records() -> None:
    """解析不了的记录不补类型：否则 ``check_all`` 会让整个 load 失败。"""
    resolver = _FakeResolver(
        ["good", "broken"],
        values={"good": '["F5"]', "broken": '["F5", [1]'},
    )

    type_shortcut_keys(resolver)

    assert resolver.types == {(SECTION, "good"): ShortcutRecord}


def test_type_shortcut_keys_ignores_other_sections() -> None:
    resolver = _FakeResolver(["a"])

    type_shortcut_keys(resolver)

    assert all(section == SECTION for section, _key in resolver.types)
