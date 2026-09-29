#
"""INI parsing and type conversion primitives for albuswall.configue.

This module is *internal*: it knows about albuswall's INI conventions
(section names, __default__ handling, TRACE logging) and should not be
treated as a general-purpose INI library.

Two pipelines coexist:

1. Legacy (kept for reference / fallback):
   TypedConfigParser + DictInterpolation

2. Current (see albuswall.configue.resolver):
   RawINIParser -> Resolver

Both share `_convert`, which is the only truly self-contained piece here
and is imported by the resolver.
"""

import re
import warnings
import logging
import io
from pathlib import Path
from pprint import pformat
from typing import Optional, Dict, Callable, Any, Union, get_origin, get_args
from configparser import (
    ExtendedInterpolation, InterpolationMissingOptionError, ConfigParser,
    MissingSectionHeaderError, InterpolationError,
    NoSectionError, NoOptionError
)

try:
    import albuswall
    from albuswall.log import TRACE
except ImportError:
    from types import SimpleNamespace

    albuswall = SimpleNamespace(__name__="albuswall")
    TRACE = 5

interpolation_pattern = re.compile(r'\$([^}]+)')  # r'\$\{([^}]+)\}'
MISSING_SECTION_HEAD = "__default__"
logger = logging.getLogger(f"{albuswall.__name__}.configue")
# noinspection unresolved-references
logger.trace = lambda msg: logger.log(TRACE, msg)


def _needs_default_section(content: str) -> bool:
    """第一行有效内容不是节头时返回 True。"""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        return not stripped.startswith("[")
    return False


class RawINIParser(ConfigParser):
    def __init__(self, *args, missing_section_head="__default__", **kwargs):
        kwargs.setdefault("interpolation", None)
        super().__init__(*args, **kwargs)
        self._missing_section_head = missing_section_head

    # ── 唯一需要覆盖的入口 ─────────────────────────────
    def read_file(self, f, source=None):
        content = f.read()
        if _needs_default_section(content):
            content = f"[{self._missing_section_head}]\n" + content
        super().read_file(io.StringIO(content), source)

    # read_string 不要覆盖！
    # configparser 自己的 read_string 会走 self.read_file，
    # 也就是上面的版本，节头会被自动补上。

    def to_raw_dict(self) -> dict[tuple[str, str], str]:
        out = {}
        for section in self.sections():
            for key, value in self.items(section):
                out[(section, key)] = value
        return out


# noinspection PyStringConversionWithoutDunderMethod
class DictInterpolation(ExtendedInterpolation):
    _SENTINEL = object()  # 用于区分“未找到”和值为 None

    def __init__(self, external_dict=None):
        super().__init__()
        self.external_dict = external_dict or {}
        self._resolving = set()  # 记录正在解析的 (section, option)，用于检测循环依赖
        # noinspection PyNoneFunctionAssignment
        [logger.trace(l) for l in
         f"[DictInterpolation] Initialized with external dict: \n{pformat(self.external_dict)}".split("\n")]

    def _resolve_key(self, key, data):
        """支持扁平键和嵌套键（冒号或点号分隔），返回对应值或 _SENTINEL"""
        # 1. 直接查找扁平键
        if key in data:
            return data[key]
        # 2. 尝试嵌套字典
        for sep in (':', '.'):
            if sep in key:
                parts = key.split(sep)
                current = data
                for part in parts:
                    if isinstance(current, dict) and part in current:
                        current = current[part]
                    else:
                        break
                else:
                    return current
        return self._SENTINEL

    def before_get(self, parser, section, option, value, defaults):
        current_key = (section, option)
        if current_key in self._resolving:
            raise InterpolationError(
                option, section,
                f"Circular reference detected for '{option}' in section '{section}'"
            )

        self._resolving.add(current_key)
        try:
            def replace(match):
                key = match.group(1)  # 例如 "path:config" 或 "files:log"

                # 1. 优先从解析器已解析的内容中查找（支持跨节引用）
                target_section, target_option = self._parse_key_for_parser(key, section)
                target_key = (target_section, target_option)
                if target_key in self._resolving:
                    raise InterpolationError(
                        option, section,
                        f"Circular reference detected for '{key}' while resolving '{option}' in section '{section}'"
                    )
                try:
                    resolved_value = parser.get(target_section, target_option)
                    # 注意：parser.get 返回的总是字符串，不会返回 None
                    return resolved_value
                except (NoSectionError, NoOptionError):
                    pass  # 未找到，继续尝试其他来源
                except Exception as e:
                    # 其他异常（如嵌套插值错误）包装后抛出
                    raise InterpolationError(
                        option, section,
                        f"Error resolving '{key}' from parser: {e}"
                    ) from e

                # 2. 外部字典（支持嵌套）
                resolved = self._resolve_key(key, self.external_dict)
                if resolved is not self._SENTINEL:
                    return str(resolved)

                # 3. 最后使用默认值（扁平优先，再尝试嵌套）
                if key in defaults:
                    return str(defaults[key])
                resolved_default = self._resolve_key(key, defaults)
                if resolved_default is not self._SENTINEL:
                    return str(resolved_default)

                # 所有来源均未找到，抛出异常
                raise InterpolationMissingOptionError(option, section, value, key)

            # 使用 ExtendedInterpolation 的正则替换 ${...}
            # noinspection PyUnresolvedReferences
            return self._KEYCRE.sub(replace, value)
        finally:
            self._resolving.remove(current_key)

    @staticmethod
    def _parse_key_for_parser(key, current_section):
        """将 'section:option' 或 'section.option' 解析为 (section, option)，若无分隔符则视为当前 section"""
        for sep in (':', '.'):
            if sep in key:
                parts = key.split(sep, 1)
                if len(parts) == 2 and parts[0] and parts[1]:
                    return parts[0], parts[1]
        return current_section, key


# noinspection PyNoneFunctionAssignment
class TypedConfigParser(ConfigParser):
    def __init__(self, type_map, *args, **kwargs):
        self._missing_section_head = kwargs.pop("missing_section_head", MISSING_SECTION_HEAD)
        self._converters = kwargs.pop("converters", {})
        self._default = kwargs.pop("default", {})

        # Store the type_map as an instance attribute
        self.type_map = type_map

        super().__init__(*args, **kwargs)
        [logger.trace(l) for l in f"[TypedConfigParser] Init: type_map(\n\t "
                                  f"sections={list(type_map.keys())}, \n\t"
                                  f"converters={list(self._converters.keys())}, \n\t"
                                  f"defaults={list(self._default.keys())})".split("\n")]

    def get_typed(self, section, option, fallback=None):
        logger.trace(f"[get_typed] Request: section={section}, option={option}, fallback={fallback!r}")
        if self.has_option(section, option):
            raw = self.get(section, option)
            converter = self._get_converter(section, option)
            logger.trace(f"[get_typed] Found raw value: {raw!r}, converter={converter}")
            return _convert(raw, converter, self._converters)

        # 先检查该 section 是否有显式默认值（注意不是 DEFAULT 节）
        if section in self._default and option in self._default[section]:
            logger.trace(
                f"[get_typed] Using explicit default for [{section}] {option} = {self._default[section][option]!r}")
            return self._default[section][option]

        # 若 section 是 __default__，再检查顶层键默认值（已包含在 self._default["__default__"] 中）
        logger.trace(f"[get_typed] No value found, returning fallback: {fallback!r}")
        return fallback

    def read_string(self, string, source="<string>"):
        logger.trace(f"[read_string] Reading from source={source}, string_length={len(string)}")
        try:
            super().read_string(string, source)
        except MissingSectionHeaderError:
            logger.trace("[read_string] Missing section header, prepending default header")
            string = f"[{self._missing_section_head}]\n" + string
            super().read_string(string, source)

    def validate(self):
        logger.trace("[validate] Starting validation")
        for section in self.sections():
            if section == self._missing_section_head:
                # 检查顶层键
                top_keys = {k: v for k, v in self.type_map.items() if not isinstance(v, dict)}
                actual = set(self.options(section))
                extra = actual - set(top_keys.keys())
                if extra:
                    logger.trace(f"[validate] Extra top-level keys in [{section}]: {extra}")
                    warnings.warn(f"Undefined top-level keys in [{section}]: {extra}")
            elif section not in self.type_map or not isinstance(self.type_map[section], dict):
                logger.trace(f"[validate] Undefined section: [{section}]")
                warnings.warn(f"Undefined section [{section}]")
            else:
                defined = set(self.type_map[section].keys())
                actual = set(self.options(section))
                extra = actual - defined
                if extra:
                    logger.trace(f"[validate] Extra keys in [{section}]: {extra}")
                    warnings.warn(f"Undefined keys in [{section}]: {extra}")

    def _get_converter(self, section, option):
        if section in self.type_map and isinstance(self.type_map[section], dict):
            return self.type_map[section].get(option)
        if section == self._missing_section_head and option in self.type_map:
            return self.type_map[option]
        raise KeyError(f"No converter defined for [{section}] {option}")

    def load(self) -> Dict[str, Any]:
        """返回与 type_map 结构一致的完整配置字典"""
        logger.trace("[load] Starting full configuration load")
        top_keys = {}
        sections = {}
        for key, value in self.type_map.items():
            if isinstance(value, dict):
                sections[key] = value
            else:
                top_keys[key] = value

        effective_map = sections.copy()
        if top_keys:
            effective_map[self._missing_section_head] = top_keys

        result = {}
        for section, fields in effective_map.items():
            [logger.trace(l) for l in
             f"[load] Processing section: {pformat(section)}, fields={pformat(list(fields.keys()))}".split("\n")]
            section_data = {}
            # 如果整个 section 在 _default 中存在（且是字典），则先复制
            if section in self._default and isinstance(self._default[section], dict):
                section_data = self._default[section].copy()
                [logger.trace(l) for l in
                 f"[load] Section {section} has default dict: {pformat(section_data)}".split("\n")]

            for key, converter in fields.items():
                if self.has_option(section, key):
                    raw = self.get(section, key)
                    logger.trace(f"[load] Converting [{section}] {key}: raw={raw!r}, converter={converter}")
                    section_data[key] = _convert(raw, converter, self._converters)
                elif key in section_data:  # 已从默认值复制
                    logger.trace(f"[load] Key {key} already set from defaults: {section_data[key]!r}")
                    pass
                else:
                    logger.trace(f"[load] Key {key} not found, setting None")
                    section_data[key] = None
            result[section] = section_data
        logger.trace(f"[load] Load complete: result sections={list(result.keys())}")
        return result


# noinspection PyNoneFunctionAssignment
def _convert(
        raw: str,
        converter: Any,
        custom_converters: Optional[Dict[Any, Callable[[str], Any]]] = None
) -> Any:
    # noinspection string-conversion-without-dunder-method
    [logger.trace(l) for l in
     f"[_convert] Input: raw={raw!r}, \n\t"
     f"converter={converter}, \n\t"
     f"custom_converters={list(custom_converters.keys()) if custom_converters else None}".split("\n")]
    # Custom converter
    if custom_converters and converter in custom_converters:
        # noinspection PyBroadException
        try:
            result = custom_converters[converter](raw)
            logger.trace(f"[_convert] Custom converter succeeded: {result!r}")
            return result
        except Exception as e:
            logger.trace(f"[_convert] Custom converter failed: {e}, returning None")
            return None

    origin = get_origin(converter)
    if origin is not None:
        args = get_args(converter)
        if origin is Union:
            non_none = [t for t in args if t is not type(None)]
            if not non_none:
                return None
            if len(non_none) == 1:
                inner_type = non_none[0]
                stripped = raw.strip()
                if stripped == "":
                    logger.trace("[_convert] Empty string for Optional type, returning None")
                    return None
                # noinspection PyBroadException
                try:
                    result = _convert(stripped, inner_type, custom_converters)
                    logger.trace(f"[_convert] Optional conversion succeeded: {result!r}")
                    return result
                except Exception:
                    logger.trace("[_convert] Optional conversion failed, returning None")
                    return None
            else:
                for t in non_none:
                    # noinspection PyBroadException
                    try:
                        result = _convert(raw.strip(), t, custom_converters)
                        logger.trace(f"[_convert] Union member {t} succeeded: {result!r}")
                        return result
                    except Exception:
                        logger.trace(f"[_convert] Union member {t} failed, trying next")
                        continue
                logger.trace("[_convert] All Union members failed, returning None")
                return None
        if origin is list:
            if not args:
                return raw
            item_type = args[0]
            stripped = raw.strip()
            if stripped == "":
                return []
            parts = [p.strip() for p in stripped.split(",") if p.strip()]
            logger.trace(f"[_convert] List conversion: parts={parts}, item_type={item_type}")
            result = [_convert(p, item_type, custom_converters) for p in parts]
            logger.trace(f"[_convert] List result: {result!r}")
            return result
        if origin is tuple:
            if not args:
                return raw
            stripped = raw.strip()
            if stripped == "":
                return ()
            parts = [p.strip() for p in stripped.split(",") if p.strip()]
            logger.trace(f"[_convert] Tuple conversion: parts={parts}, type_args={args}")
            if len(args) == 2 and args[1] is ...:
                item_type = args[0]
                result = tuple(_convert(p, item_type, custom_converters) for p in parts)
                logger.trace(f"[_convert] Tuple (variadic) result: {result!r}")
                return result
            if len(parts) != len(args):
                warning_msg = (
                    f"[INILoader] Tuple conversion: expected {len(args)} elements, got {len(parts)}. "
                    f"[INILoader] Will truncate or pad with None."
                )
                warnings.warn(warning_msg)
            result_list = []
            for i, p in enumerate(parts):
                if i < len(args):
                    result_list.append(_convert(p, args[i], custom_converters))
                else:
                    break
            while len(result_list) < len(args):
                result_list.append(None)
            logger.trace(f"[_convert] Tuple (fixed) result: {tuple(result_list)!r}")
            return tuple(result_list)
        raise TypeError(f"Unsupported generic type: {converter}")

    # Basic types
    if converter is bool:
        result = raw.strip().lower() in ("true", "1", "yes", "boot")
        logger.trace(f"[_convert] bool result: {result}")
        return result
    if converter is int:
        result = int(raw)
        logger.trace(f"[_convert] int result: {result}")
        return result
    if converter is float:
        result = float(raw)
        logger.trace(f"[_convert] float result: {result}")
        return result
    if converter is str:
        logger.trace("[_convert] str result (unchanged)")
        return raw
    if converter is Path:
        result = Path(raw)
        logger.trace(f"[_convert] Path result: {result}")
        return result
    if callable(converter) and not isinstance(converter, type):
        result = converter(raw)
        # noinspection PyStringConversionWithoutDunderMethod
        logger.trace(f"[_convert] callable result: {result!r}")
        return result
    # noinspection PyStringConversionWithoutDunderMethod
    raise TypeError(f"Unhandled converter type: {type(converter)} (value: {converter})")


if __name__ == "__main__":
    import sys
    import types
    import unittest
    from typing import Optional, List, Tuple

    # ----------------------------------------------------------------------
    # 模拟 albuswall 模块，确保能正常导入 configue 模块
    # ----------------------------------------------------------------------
    albuswall = types.ModuleType('albuswall')
    albuswall.__name__ = 'albuswall'
    albuswall.TRACE = 5  # 自定义日志级别，数值任意
    sys.modules['albuswall'] = albuswall


    # ----------------------------------------------------------------------
    # 测试 _convert 函数
    # ----------------------------------------------------------------------
    # noinspection PyTypeChecker
    class TestConvertFunction(unittest.TestCase):
        def test_bool_conversion(self):
            self.assertTrue(_convert("true", bool))
            self.assertTrue(_convert("1", bool))
            self.assertTrue(_convert("yes", bool))
            self.assertTrue(_convert("ON", bool))
            self.assertFalse(_convert("false", bool))
            self.assertFalse(_convert("0", bool))
            self.assertFalse(_convert("no", bool))

        def test_int_conversion(self):
            self.assertEqual(_convert("123", int), 123)
            with self.assertRaises(ValueError):
                _convert("abc", int)

        def test_float_conversion(self):
            self.assertEqual(_convert("3.14", float), 3.14)
            with self.assertRaises(ValueError):
                _convert("xyz", float)

        def test_str_conversion(self):
            self.assertEqual(_convert("hello", str), "hello")

        def test_path_conversion(self):
            self.assertEqual(_convert("/tmp/test", Path), Path("/tmp/test"))

        def test_optional_conversion(self):
            # Optional[int]
            self.assertEqual(_convert("42", Optional[int]), 42)
            self.assertIsNone(_convert("", Optional[int]))
            self.assertIsNone(_convert("   ", Optional[int]))
            # Optional[str]
            self.assertEqual(_convert("abc", Optional[str]), "abc")
            self.assertIsNone(_convert("", Optional[str]))

        def test_union_conversion(self):
            # Union[int, str]
            self.assertEqual(_convert("42", Union[int, str]), 42)
            self.assertEqual(_convert("hello", Union[int, str]), "hello")
            # Union[int, float] - "3.14" 会先尝试 int 失败，再尝试 float 成功
            self.assertEqual(_convert("3.14", Union[int, float]), 3.14)
            # 全部失败返回 None
            self.assertIsNone(_convert("abc", Union[int, float]))

        def test_list_conversion(self):
            # List[int]
            result = _convert("1, 2, 3", List[int])
            self.assertEqual(result, [1, 2, 3])
            # List[str]
            result = _convert("a, b, c", List[str])
            self.assertEqual(result, ["a", "b", "c"])
            # 空字符串返回空列表
            self.assertEqual(_convert("", List[int]), [])

        def test_tuple_conversion_variadic(self):
            # Tuple[int, ...]
            result = _convert("1,2,3", Tuple[int, ...])
            self.assertEqual(result, (1, 2, 3))
            # 空字符串返回空元组
            self.assertEqual(_convert("", Tuple[int, ...]), ())

        def test_tuple_conversion_fixed(self):
            # Tuple[int, str]
            result = _convert("42,hello", Tuple[int, str])
            self.assertEqual(result, (42, "hello"))
            # 元素不足时补 None
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                result = _convert("42", Tuple[int, str])
                self.assertEqual(result, (42, None))
                self.assertTrue(any("Tuple conversion" in str(item.message) for item in w))
            # 元素过多时截断
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                result = _convert("42,hello,extra", Tuple[int, str])
                self.assertEqual(result, (42, "hello"))
                self.assertTrue(any("Tuple conversion" in str(item.message) for item in w))

        def test_custom_converter(self):
            custom = {
                "upper": lambda s: s.upper(),
                "double": lambda s: int(s) * 2,
            }
            self.assertEqual(_convert("abc", "upper", custom), "ABC")
            self.assertEqual(_convert("5", "double", custom), 10)
            # 自定义转换器未定义时，如果类型是不可识别的字符串，会抛出 TypeError
            with self.assertRaises(TypeError):
                _convert("abc", "unknown", custom)

        def test_unhandled_type(self):
            with self.assertRaises(TypeError):
                _convert("123", dict)


    # ----------------------------------------------------------------------
    # 测试 DictInterpolation
    # ----------------------------------------------------------------------
    class TestDictInterpolation(unittest.TestCase):
        def test_basic_interpolation(self):
            parser = TypedConfigParser(type_map={}, interpolation=DictInterpolation({"name": "Alice"}))
            parser.read_string("[section]\nkey = Hello ${name}\n")
            self.assertEqual(parser.get("section", "key"), "Hello Alice")

        def test_missing_key_raises(self):
            parser = TypedConfigParser(type_map={}, interpolation=DictInterpolation({}))
            parser.read_string("[section]\nkey = Hello ${missing}\n")
            with self.assertRaises(Exception):
                parser.get("section", "key")


    # ----------------------------------------------------------------------
    # 测试 TypedConfigParser
    # ----------------------------------------------------------------------
    # noinspection PyTypeChecker
    class TestTypedConfigParser(unittest.TestCase):
        def setUp(self):
            self.type_map = {
                "name": str,
                "count": int,
                "enabled": bool,
                "ratio": float,
                "path": Path,
                "optional": Optional[int],
                "items": List[int],
                "tuple_var": Tuple[int, ...],
                "tuple_fixed": Tuple[int, str],
                "section1": {
                    "host": str,
                    "port": int,
                    "debug": bool,
                },
            }
            self.parser = TypedConfigParser(type_map=self.type_map, interpolation=DictInterpolation({}))

        def test_read_string_without_section(self):
            # 无节头时自动添加默认节
            self.parser.read_string("name = test\ncount = 42\nenabled = true\n")
            self.assertEqual(self.parser.get_typed(MISSING_SECTION_HEAD, "name"), "test")
            self.assertEqual(self.parser.get_typed(MISSING_SECTION_HEAD, "count"), 42)
            self.assertTrue(self.parser.get_typed(MISSING_SECTION_HEAD, "enabled"))

        def test_read_string_with_section(self):
            config_text = """
    [section1]
    host = localhost
    port = 8080
    debug = false
    """
            self.parser.read_string(config_text)
            self.assertEqual(self.parser.get_typed("section1", "host"), "localhost")
            self.assertEqual(self.parser.get_typed("section1", "port"), 8080)
            self.assertFalse(self.parser.get_typed("section1", "debug"))

        def test_get_typed_fallback(self):
            self.parser.read_string("name = test\n")
            # 未设置的键返回 fallback
            self.assertIsNone(self.parser.get_typed(MISSING_SECTION_HEAD, "count"))
            self.assertEqual(self.parser.get_typed(MISSING_SECTION_HEAD, "count", fallback=99), 99)

        def test_get_typed_explicit_default(self):
            # 使用 default 参数提供默认值
            parser = TypedConfigParser(
                type_map={"count": int, "section1": {"port": int}},
                default={MISSING_SECTION_HEAD: {"count": 10}, "section1": {"port": 8080}},
            )
            parser.read_string("")  # 空配置
            self.assertEqual(parser.get_typed(MISSING_SECTION_HEAD, "count"), 10)
            self.assertEqual(parser.get_typed("section1", "port"), 8080)

        def test_validate_warnings(self):
            # 未定义的顶层键
            self.parser.read_string("unknown_key = value\n")
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                self.parser.validate()
                self.assertTrue(any("Undefined top-level keys" in str(item.message) for item in w))

            # 未定义的节
            self.parser.read_string("[unknown_section]\nkey = value\n")
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                self.parser.validate()
                self.assertTrue(any("Undefined section" in str(item.message) for item in w))

            # 节内未定义的键
            self.parser.read_string("[section1]\nunknown_key = value\n")
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                self.parser.validate()
                self.assertTrue(any("Undefined keys in" in str(item.message) for item in w))

        def test_load_full_config(self):
            config_text = """
    name = Alice
    count = 5
    enabled = yes
    ratio = 3.14
    path = /tmp/example
    optional = 42
    items = 1,2,3
    tuple_var = 7,8,9
    tuple_fixed = 10,hello

    [section1]
    host = example.com
    port = 443
    debug = true
    """
            self.parser.read_string(config_text)
            result = self.parser.load()
            # 检查顶层键
            self.assertEqual(result[MISSING_SECTION_HEAD]["name"], "Alice")
            self.assertEqual(result[MISSING_SECTION_HEAD]["count"], 5)
            self.assertTrue(result[MISSING_SECTION_HEAD]["enabled"])
            self.assertAlmostEqual(result[MISSING_SECTION_HEAD]["ratio"], 3.14)
            self.assertEqual(result[MISSING_SECTION_HEAD]["path"], Path("/tmp/example"))
            self.assertEqual(result[MISSING_SECTION_HEAD]["optional"], 42)
            self.assertEqual(result[MISSING_SECTION_HEAD]["items"], [1, 2, 3])
            self.assertEqual(result[MISSING_SECTION_HEAD]["tuple_var"], (7, 8, 9))
            self.assertEqual(result[MISSING_SECTION_HEAD]["tuple_fixed"], (10, "hello"))
            # 检查节
            self.assertEqual(result["section1"]["host"], "example.com")
            self.assertEqual(result["section1"]["port"], 443)
            self.assertTrue(result["section1"]["debug"])

        def test_load_with_missing_keys(self):
            # 缺少键时应为 None
            self.parser.read_string("name = Alice\n")
            result = self.parser.load()
            self.assertIsNone(result[MISSING_SECTION_HEAD]["count"])
            self.assertIsNone(result[MISSING_SECTION_HEAD]["items"])


    unittest.main()
