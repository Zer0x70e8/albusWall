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

import io
import json
import logging
import re
import types
import warnings
from collections.abc import Mapping
from configparser import (
    ConfigParser,
    ExtendedInterpolation,
    InterpolationError,
    InterpolationMissingOptionError,
    MissingSectionHeaderError,
    NoOptionError,
    NoSectionError,
)
from enum import Enum  # configue1: _deconvert 的 Enum 分支需要，原本是注释掉的
from pathlib import Path
from pprint import pformat
from typing import Any, Callable, Dict, Optional, Union, get_args, get_origin

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

# bool 的真值写法（大小写不敏感）。不在集合里的字符串一律转换为 False。
# 注意：这里曾是 ("true", "1", "yes", "boot")——"boot" 是 "on" 的笔误，
# 已修正；`_deconvert` 的反向只产出 "true"/"false"，往返不受影响。
_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})

_UNION_ORIGINS = (Union, getattr(types, "UnionType", Union))


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
                key = match.group(1)  # 例如 "path:_config" 或 "files:logger"

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
def _json_object(raw: str) -> dict:
    """dict 型配置值的 INI 表示：一个 JSON 对象（空串 = 空 dict）。

    与 ``_deconvert`` 里的 ``json.dumps`` 对称，保证「写出去的能读回来」。
    """
    text = raw.strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"expected a JSON object for a dict value, got {raw!r}"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError(
            f"expected a JSON object for a dict value, got "
            f"{type(value).__name__}: {raw!r}"
        )
    return value


_CONTAINER_ORIGINS = (list, tuple, dict, set, frozenset)


def _json_nodes(raw: str):
    """``raw`` 是一个 JSON 数组时返回元素列表，否则返回 None。

    专门用来表达「元素本身是容器」的序列（``list[list[int]]``、
    ``list[dict[str, int]]``）——这种结构用逗号分隔根本表示不了。
    """
    text = raw.strip()
    if not text.startswith("["):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def _is_container_type(typ: Any) -> bool:
    """声明的类型本身是容器（``list[X]`` / ``dict[str, X]`` / ...）时 True。"""
    if typ is dict or typ is list or typ is tuple or typ is set:
        return True
    return get_origin(typ) in _CONTAINER_ORIGINS


def _needs_json_escape(text: str) -> bool:
    """逗号分隔装不下这个元素 → 序列必须改用 JSON 数组表达。

    ``",".join`` / ``split(",")`` 是有损的：元素里带逗号、首尾空白（会被
    ``strip()`` 吃掉）、空串、或以 ``[`` 开头（读取侧会当成 JSON 数组）都会
    破坏往返。``QKeySequence("Ctrl+,")`` 就是第一个真实受害者。
    """
    if text == "" or "," in text:
        return True
    if text != text.strip():
        return True
    return text.startswith("[")


# 默认 repr 形如 "<pkg.Cls object at 0x7f...>"，局部类还带 "<locals>"
_OBJECT_REPR_RE = re.compile(r"^<.+\bat 0x[0-9a-fA-F]+>$")


def _looks_like_object_repr(text: str) -> bool:
    """``str(value)`` 是不是「默认对象 repr」——那种文本写出去必然读不回来。"""
    return bool(_OBJECT_REPR_RE.match(text.strip()))


def _json_plain(value: Any) -> Any:
    """把 typed 值降成 JSON 原生形态（嵌套容器递归，其余非标量 str 化）。"""
    if isinstance(value, Mapping):
        return {str(k): _json_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_plain(v) for v in value]
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


def _convert_dict_item(
        value: Any,
        typ: Any,
        custom_converters: Optional[Dict[Any, Callable[[str], Any]]],
) -> Any:
    """把 JSON 解析出的值按声明的 key / value 类型收敛（容器内部递归）。

    JSON 里 value 可能是 ``["dark", "light"]`` 或 ``{"a": 1}``，所以这里
    必须往下递归泛型，否则 ``dict[str, list[Theme]]`` 里的 ``Theme`` 永远
    不会被应用（原先直接 ``return value`` 原样透传）。
    """
    if typ is Any or typ is None:
        return value

    origin = get_origin(typ)
    args = get_args(typ)

    # ── 容器：递归到内层类型 ─────────────────────────
    if (origin is dict or typ is dict) and isinstance(value, Mapping):
        if len(args) != 2:
            return {str(k): v for k, v in value.items()}
        key_type, value_type = args
        return {
            _convert_dict_item(k, key_type, custom_converters):
                _convert_dict_item(v, value_type, custom_converters)
            for k, v in value.items()
        }
    if origin in (list, tuple, set, frozenset) \
            and isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        if origin is tuple and len(args) == 2 and args[1] is ...:
            item_types = [args[0]] * len(items)
        elif origin is tuple and args:
            item_types = list(args)
        elif args:
            item_types = [args[0]] * len(items)
        else:
            item_types = None
        converted = [
            _convert_dict_item(
                item,
                item_types[i] if item_types and i < len(item_types) else None,
                custom_converters,
            )
            for i, item in enumerate(items)
        ]
        if origin is tuple:
            return tuple(converted)
        if origin is set:
            return set(converted)
        if origin is frozenset:
            return frozenset(converted)
        return converted

    # ── Optional / Union：先按 isinstance 选中，再退化为逐个试 ──
    if origin is Union:
        non_none = [t for t in args if t is not type(None)]
        if value is None:
            return None
        for t in non_none:
            if _matches_type(value, t):
                return _convert_dict_item(value, t, custom_converters)
        for t in non_none:
            # noinspection PyBroadException
            try:
                return _convert_dict_item(value, t, custom_converters)
            except Exception:
                continue
        return value

    # ── 标量 ─────────────────────────────────────────
    if isinstance(value, str):
        return _convert(value, typ, custom_converters)
    if typ is bool:
        return bool(value)
    if typ in (int, float, str):
        return _convert(str(value), typ, custom_converters)
    if custom_converters and typ in custom_converters:
        try:
            return custom_converters[typ](str(value))
        except Exception as exc:
            raise ValueError(
                f"custom converter for {typ!r} failed on {str(value)!r}: {exc}"
            ) from exc
    if isinstance(typ, type):
        # 默认回退，与 _convert 末尾保持一致：Enum / Path 子类 / 自定义类
        try:
            return typ(value)
        except Exception as exc:
            raise ValueError(
                f"failed to convert {value!r} to {typ!r}: {exc}"
            ) from exc
    return value


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
    # Any：声明为 Any 的项就是「原样保留」。必须在默认回退之前拦下——
    # 3.11+ 的 ``typing.Any`` 本身是类，``Any(raw)`` 只会报错。
    if converter is Any:
        return raw
    # Custom converter（显式注册的优先；失败必须抛，不能静默变 None）
    if custom_converters and converter in custom_converters:
        fn = custom_converters[converter]
        try:
            result = fn(raw)
        except Exception as exc:
            logger.trace(f"[_convert] Custom converter failed: {exc}")
            raise ValueError(
                f"custom converter for {converter!r} failed on {raw!r}: {exc}"
            ) from exc
        logger.trace(f"[_convert] Custom converter succeeded: {result!r}")
        return result

    origin = get_origin(converter)
    args = get_args(converter)
    if origin in _UNION_ORIGINS:
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
    # 注意：container 分支必须放在 union 分支**外面**——它只处理
    # ``dict[...] / list[...] / tuple[...] / set[...]``，与 Union 无关。
    # （历史 bug：这些分支曾被缩进进 ``if origin in _UNION_ORIGINS``，
    # 导致非 Union 的泛型永远走不到，``list[str]`` 会退化成逐字符。）
    if origin is not None:
        if origin is dict:
            parsed = _json_object(raw)
            if len(args) != 2:
                result = {str(k): v for k, v in parsed.items()}
            else:
                key_type, value_type = args
                result = {
                    _convert_dict_item(k, key_type, custom_converters):
                        _convert_dict_item(v, value_type, custom_converters)
                    for k, v in parsed.items()
                }
            logger.trace(f"[_convert] Dict result: {result!r}")
            return result
        if origin is list:
            if not args:
                return raw
            item_type = args[0]
            stripped = raw.strip()
            if stripped == "":
                return []
            # 先试 JSON 数组：容器元素、含逗号的字符串（"Ctrl+,"）、空串、
            # 首尾空白都以 JSON 形态写出，不先试就读不回来
            nodes = _json_nodes(stripped)
            if nodes is not None:
                result = [
                    _convert_dict_item(n, item_type, custom_converters)
                    for n in nodes
                ]
                logger.trace(f"[_convert] List (json) result: {result!r}")
                return result
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
            nodes = _json_nodes(stripped)
            if nodes is not None:
                if len(args) == 2 and args[1] is ...:
                    result = tuple(
                        _convert_dict_item(n, args[0], custom_converters)
                        for n in nodes
                    )
                else:
                    result = tuple(
                        _convert_dict_item(
                            n,
                            args[i] if i < len(args) else None,
                            custom_converters,
                        )
                        for i, n in enumerate(nodes)
                    )
                logger.trace(f"[_convert] Tuple (json) result: {result!r}")
                return result
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
        if origin in (set, frozenset):
            # set[X] / frozenset[X]：与 list[X] 同一套 raw 形态（先试 JSON
            # 数组，失败再逗号分隔），保持与 ``_deconvert`` 对称
            if not args:
                return raw
            stripped = raw.strip()
            factory = set if origin is set else frozenset
            if stripped == "":
                return factory()
            item_type = args[0]
            nodes = _json_nodes(stripped)
            if nodes is not None:
                result = factory(
                    _convert_dict_item(n, item_type, custom_converters)
                    for n in nodes
                )
                logger.trace(f"[_convert] Set (json) result: {result!r}")
                return result
            parts = [p.strip() for p in stripped.split(",") if p.strip()]
            result = factory(
                _convert(p, item_type, custom_converters) for p in parts
            )
            logger.trace(f"[_convert] Set result: {result!r}")
            return result
        raise TypeError(f"Unsupported generic type: {converter}")

    # Basic types
    if converter is bool:
        # 真值集合：true / 1 / yes / on（大小写不敏感，容忍首尾空白）；
        # 其余（false / 0 / no / off / 任意串）一律为 False。
        result = raw.strip().lower() in _TRUE_STRINGS
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
    if converter is dict:
        result = {str(k): v for k, v in _json_object(raw).items()}
        logger.trace(f"[_convert] bare dict result: {result!r}")
        return result
    # 默认回退：任何「吃一个 raw 参数就能构造」的类型都直接调构造函数。
    # 覆盖 Enum / Path 子类 / str、int 子类 / 带 X(str) 构造的自定义类，
    # 都不需要注册。类型自己带 ``from_ini`` 类方法时优先用它（与
    # ``_deconvert`` 的 ``to_ini`` 约定配对，读写都不必注册）。
    if callable(converter):
        text = raw.strip()
        reader = getattr(converter, "from_ini", None)
        try:
            result = reader(text) if callable(reader) else converter(text)
        except Exception as exc:
            raise ValueError(
                f"failed to convert {raw!r} to {converter!r}: {exc}"
            ) from exc
        # noinspection PyStringConversionWithoutDunderMethod
        logger.trace(f"[_convert] callable result: {result!r}")
        return result
    # noinspection PyStringConversionWithoutDunderMethod
    raise TypeError(f"Unhandled converter type: {type(converter)} (value: {converter})")


def _matches_type(value: Any, typ: Any) -> bool:
    """``_deconvert`` 内部用：判断 value 是否属于 typ（Union 选型用）。"""
    if typ is bool:
        return isinstance(value, bool)
    if typ is int:
        return isinstance(value, int) and not isinstance(value, bool)
    try:
        return isinstance(value, typ)
    except TypeError:
        return False


def _dump_json_sequence(
        items,
        item_types=None,
        custom_converters=None,
        custom_serializers=None,
) -> str:
    """把序列写成 JSON 数组（与 ``_json_nodes`` 对称）。"""
    values = [
        _json_value(
            item,
            item_types[i] if item_types and i < len(item_types) else None,
            custom_converters,
            custom_serializers,
        )
        for i, item in enumerate(items)
    ]
    try:
        return json.dumps(values, ensure_ascii=False)
    except TypeError as exc:
        raise ValueError(
            f"list value is not JSON-serializable: {list(items)!r}"
        ) from exc


def _plain_text(value: Any, converter: Any = None) -> str:
    """``str(value)`` 兜底；只是默认 repr 时直接报错，绝不把配置写坏。

    典型受害者是 ``QKeySequence``：``str()`` 在部分版本返回 NativeText
    （macOS 上是 ``⌘S``），有的版本干脆返回 ``<... object at 0x...>``。
    这类类型必须注册 ``writer``，或实现 ``to_ini()``。
    """
    text = str(value)
    if not isinstance(value, str) and _looks_like_object_repr(text):
        name = getattr(converter, "__name__", converter) or "this type"
        raise ValueError(
            f"{name!r} has no INI text form: str(value) is {text!r}; "
            f"register a writer (register_converter({name!r}, reader, "
            f"writer=...)) or implement {name}.to_ini()"
        )
    return text


def _join_items(
        items,
        item_types,
        custom_converters,
        custom_serializers=None,
) -> str:
    """序列的 INI 形态：能逗号分隔就逗号分隔，装不下就 JSON 数组。

    与 ``_convert`` 的「先试 JSON，再逗号分隔」严格对称：
      - 元素本身是容器（``list[list[int]]``）→ JSON 数组；
      - 元素文本含逗号 / 首尾空白 / 是空串 / 以 ``[`` 开头 → JSON 数组。
        否则 ``QKeySequence("Ctrl+,")`` 这种值会被逗号吞掉，下次读回来少一项。

    item_types: 与 items 等长的类型列表，或 None（元素直接 str）。
    """
    texts = [
        _deconvert(
            item,
            item_types[i] if item_types and i < len(item_types) else None,
            custom_converters,
            custom_serializers,
        )
        for i, item in enumerate(items)
    ]
    if item_types and item_types[0] is not None \
            and _is_container_type(item_types[0]):
        return _dump_json_sequence(
            items, item_types, custom_converters, custom_serializers,
        )
    if any(_needs_json_escape(text) for text in texts):
        return _dump_json_sequence(
            items, item_types, custom_converters, custom_serializers,
        )
    return ", ".join(texts)


def _json_value(
        value: Any,
        typ: Any,
        custom_converters: Optional[Dict[Any, Callable[[str], Any]]] = None,
        custom_serializers: Optional[Dict[Any, Callable[[Any], str]]] = None,
) -> Any:
    """typed 值 -> JSON 原生值（容器递归，标量走 ``_deconvert`` + 标量还原）。

    与 ``_convert_dict_item`` 对称：``dict[str, list[Theme]]`` 的 value 是
    数组时就写数组，而不是字符串化的 ``"dark, light"`` 或二次编码的 JSON。
    """
    if typ is None or typ is Any:
        return _json_plain(value)
    if value is None:
        return None

    origin = get_origin(typ)
    args = get_args(typ)

    if (origin is dict or typ is dict) and isinstance(value, Mapping):
        key_type, value_type = (args[0], args[1]) if len(args) == 2 else (None, None)
        return {
            _json_value(k, key_type, custom_converters, custom_serializers):
                _json_value(v, value_type, custom_converters, custom_serializers)
            for k, v in value.items()
        }
    if origin in (list, tuple, set, frozenset) \
            and isinstance(value, (list, tuple, set, frozenset)):
        if origin is tuple and len(args) == 2 and args[1] is not ...:
            item_types = list(args)
        else:
            item_types = [args[0]] * len(value) if args else None
        return [
            _json_value(
                item,
                item_types[i] if item_types and i < len(item_types) else None,
                custom_converters,
                custom_serializers,
            )
            for i, item in enumerate(value)
        ]
    if origin is Union:
        non_none = [t for t in args if t is not type(None)]
        for t in non_none:
            if _matches_type(value, t):
                return _json_value(value, t, custom_converters, custom_serializers)
        return _json_plain(value)

    return _jsonify_scalar(
        _deconvert(value, typ, custom_converters, custom_serializers), typ,
    )


def _dump_dict(
        value: Any,
        key_type: Any = None,
        value_type: Any = None,
        custom_converters: Optional[Dict[Any, Callable[[str], Any]]] = None,
        custom_serializers: Optional[Dict[Any, Callable[[Any], str]]] = None,
) -> str:
    """dict 值的 INI 表示：按 key 排序的 JSON 对象，与 ``_json_object`` 对称。

    与其它分支不同，这里**会**抛 ``ValueError``：写出去却读不回来的值
    比写失败更糟（下次启动会在类型检查时炸）。调用方（``flush``）会把
    异常包成 ``ConfigError`` 并保留 dirty。
    """
    if not isinstance(value, Mapping):
        raise ValueError(
            f"expected a mapping for a dict value, got "
            f"{type(value).__name__}: {value!r}"
        )

    dumped: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = (
            _deconvert(raw_key, key_type, custom_converters, custom_serializers)
            if key_type is not None else str(raw_key)
        )
        if value_type is None:
            item = raw_value
        elif _is_container_type(value_type):
            # 值是容器：写原生 JSON（数组 / 对象），与读取侧的递归对称
            item = _json_value(
                raw_value, value_type, custom_converters, custom_serializers,
            )
        else:
            item = _deconvert(
                raw_value, value_type, custom_converters, custom_serializers,
            )
            # dict[str, int] 这类：_deconvert 得到的是字符串，JSON 里要还原成数字
            item = _jsonify_scalar(item, value_type)
        dumped[key] = item

    try:
        return json.dumps(dumped, ensure_ascii=False, sort_keys=True)
    except TypeError as exc:
        raise ValueError(
            f"dict value is not JSON-serializable: {value!r}"
        ) from exc


def _jsonify_scalar(value: Any, value_type: Any) -> Any:
    """``_deconvert`` 的字符串结果在不该是字符串时还原成 JSON 标量。"""
    if value_type is None or not isinstance(value, str):
        return value
    if value_type is int:
        try:
            return int(value)
        except ValueError:
            return value
    if value_type is float:
        try:
            return float(value)
        except ValueError:
            return value
    if value_type is bool:
        return value.strip().lower() in _TRUE_STRINGS
    return value


def _deconvert(
        value: Any,
        converter: Any,
        custom_converters: Optional[Dict[Any, Callable[[str], Any]]] = None,
        custom_serializers: Optional[Dict[Any, Callable[[Any], str]]] = None,
) -> str:
    """与 ``_convert`` 对称：typed value -> INI 原始字符串。

    约定（fail-safe，不抛异常）：
      - None -> ""
      - bool -> "true" / "false"
      - Enum -> str(value.value)（与 ``_convert`` 的 ``EnumType(raw)`` 对称）
      - Path -> str(value)
      - 序列 -> ", ".join(...)（与 ``_convert`` 里 split(",") 对称）；
        元素是容器、或元素文本含逗号 / 首尾空白 / 空串 / 以 ``[`` 开头时，
        改写成 JSON 数组（``QKeySequence("Ctrl+,")`` 不会被逗号吞掉）
      - dict -> JSON 对象（与 ``_json_object`` 对称；键会排序），
        value 类型是容器时写原生 JSON 数组 / 对象
      - int / float / str -> str(value)
      - Optional / Union：None -> ""，其余按匹配的内部类型处理
      - 注册过 serializer 的类型 -> ``custom_serializers[type](value)``
      - 带 ``to_ini()`` 方法的对象 -> ``value.to_ini()``
      - 未知类型 -> str(value)

    注意：
      - 只注册了 reader 没注册 serializer 的类型只能 ``str(value)``——
        ``QKeySequence`` 就属于这种：``str()`` 在部分版本返回 NativeText
        （macOS 上是 ``⌘S``）。要么给它注册 ``writer``，要么实现 ``to_ini()``，
        要么别直接把它声明成 ``value_type``；
      - ``str(value)`` 只是默认 repr（``<... object at 0x...>``）时抛
        ``ValueError``：写出去下次启动必然读不回来，宁可现在炸；
      - dict / JSON 序列分支会抛 ``ValueError``（写不出可读回的值时宁可失败，
        也不写一个下次启动会炸的配置）。
    """
    logger.trace(f"[_deconvert] Input: value={value!r}, converter={converter}")

    # None 对任何类型都写成空串（Optional / Union 的对称面）
    if value is None:
        return ""

    # 显式注册的写回函数优先（与 reader 配对）
    if custom_serializers and converter in custom_serializers:
        writer = custom_serializers[converter]
        try:
            text = writer(value)
        except Exception as exc:
            raise ValueError(
                f"custom serializer for {converter!r} failed on "
                f"{value!r}: {exc}"
            ) from exc
        if not isinstance(text, str):
            raise ValueError(
                f"custom serializer for {converter!r} must return str, "
                f"got {type(text).__name__}: {text!r}"
            )
        return text

    if custom_converters and converter in custom_converters:
        # 只有 reader 没有 writer：只能沿用 str()，这是明确的退路而不是机制；
        # 若 str() 只是默认 repr 则报错（见 _plain_text）
        return _plain_text(value, converter)

    # Enum：``_convert`` 用 ``EnumType(raw)`` 按 *value* 查表，写回也必须写
    # value（``str(Theme.DARK)`` 会得到 "Theme.DARK"，读不回来）。
    if isinstance(converter, type) and issubclass(converter, Enum):
        return str(value.value)

    # 约定：类型自带 to_ini() 时用它（与 ``_convert`` 的 from_ini 配对）
    to_ini = getattr(value, "to_ini", None)
    if callable(to_ini):
        return str(to_ini())

    origin = get_origin(converter)
    if origin is not None:
        args = get_args(converter)
        if origin in _UNION_ORIGINS:
            non_none = [t for t in args if t is not type(None)]
            if not non_none:
                return str(value)
            if len(non_none) == 1:
                return _deconvert(
                    value, non_none[0], custom_converters, custom_serializers,
                )
            for t in non_none:
                if _matches_type(value, t):
                    return _deconvert(
                        value, t, custom_converters, custom_serializers,
                    )
            return str(value)
        if origin is dict:
            if len(args) != 2:
                return _dump_dict(
                    value, None, None, custom_converters, custom_serializers,
                )
            return _dump_dict(
                value, args[0], args[1], custom_converters, custom_serializers,
            )
        if origin is list:
            if not isinstance(value, (list, tuple)):
                return str(value)
            item_types = [args[0]] * len(value) if args else None
            return _join_items(
                value, item_types, custom_converters, custom_serializers,
            )
        if origin is tuple:
            if not isinstance(value, (list, tuple)):
                return str(value)
            if not args:
                item_types = None
            elif len(args) == 2 and args[1] is ...:
                item_types = [args[0]] * len(value)
            else:
                item_types = list(args)
            return _join_items(
                value, item_types, custom_converters, custom_serializers,
            )
        if origin in (set, frozenset):
            # 与 ``_convert`` 的 set / frozenset 分支对称（顺序不保证）
            if not isinstance(value, (set, frozenset, list, tuple)):
                return str(value)
            items = list(value)
            item_types = [args[0]] * len(items) if args else None
            return _join_items(
                items, item_types, custom_converters, custom_serializers,
            )
        return str(value)

    if converter is bool:
        return "true" if value else "false"
    if converter is dict:
        return _dump_dict(
            value, None, None, custom_converters, custom_serializers,
        )

    # 默认 repr 写出去下一次启动必然读不回来（QKeySequence 就是这种）：
    # 宁可在这里炸，也不要把用户的配置文件写坏
    return _plain_text(value, converter)


if __name__ == "__main__":
    import sys
    import types
    import unittest
    from enum import Enum
    from typing import Dict, List, Optional, Tuple


    class MyPath(Path):
        """自测用：``Path`` 子类走默认回退，不需要注册。"""


    class Colour(Enum):
        """自测用：未注册的 ``Enum`` 走默认回退。"""

        DARK = "dark"
        LIGHT = "light"


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
            self.assertTrue(_convert("on", bool))
            self.assertTrue(_convert(" True ", bool))
            self.assertFalse(_convert("false", bool))
            self.assertFalse(_convert("0", bool))
            self.assertFalse(_convert("no", bool))
            self.assertFalse(_convert("off", bool))
            self.assertFalse(_convert("", bool))

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

        def test_custom_converter_failure_raises(self):
            """自定义转换器抛异常必须抛 ValueError，不能静默返回 None。"""
            custom = {"boom": lambda s: (_ for _ in ()).throw(RuntimeError("x"))}
            with self.assertRaises(ValueError):
                _convert("abc", "boom", custom)

        def test_unregistered_type_falls_back_to_ctor(self):
            """未注册的类只要 ``X(raw)`` 能成功就自动可用（Enum 等）。"""
            self.assertIs(_convert("dark", Colour), Colour.DARK)
            self.assertIsInstance(_convert("/tmp/a", MyPath), MyPath)
            with self.assertRaises(ValueError):
                _convert("nope", Colour)

        def test_dict_item_recurses_containers(self):
            """``dict[str, list[X]]`` / ``dict[str, dict[str, X]]`` 的内层类型生效。"""
            self.assertEqual(
                _convert('{"a": ["dark"]}', Dict[str, List[Colour]]),
                {"a": [Colour.DARK]},
            )
            self.assertEqual(
                _convert('{"a": {"b": 1}}', Dict[str, Dict[str, int]]),
                {"a": {"b": 1}},
            )

        def test_nested_list_roundtrip(self):
            """元素是容器时用 JSON 数组，``_deconvert`` / ``_convert`` 对称。"""
            value = [[1, 2], [3, 4]]
            text = _deconvert(value, List[List[int]])
            self.assertEqual(text, "[[1, 2], [3, 4]]")
            self.assertEqual(_convert(text, List[List[int]]), value)
            # 标量序列仍是逗号分隔
            self.assertEqual(_deconvert([1, 2], List[int]), "1, 2")

        def test_list_comma_escape(self):
            """元素含逗号 / 空串 / 首尾空白 / 以 [ 开头 → JSON 数组保住往返。"""
            for value in (["Ctrl+S", "Ctrl+,"], [""], ["a "], ["[x]"]):
                text = _deconvert(value, List[str])
                self.assertEqual(_convert(text, List[str]), value)
            # 没歧义时仍是逗号分隔
            self.assertEqual(_deconvert(["a", "b"], List[str]), "a, b")

        def test_custom_serializer(self):
            """写回方向的注册函数优先于 str(value)。"""

            class Seq(str):
                pass

            serializers = {Seq: lambda value: f"<{value}>"}
            self.assertEqual(_deconvert("x", Seq), "x")
            self.assertEqual(_deconvert("x", Seq, None, serializers), "<x>")
            with self.assertRaises(ValueError):
                _deconvert("x", Seq, None, {Seq: lambda v: 42})

        def test_unhandled_type(self):
            # dict 是已知类型，但值不是 JSON 对象 → ValueError（不是 TypeError）
            with self.assertRaises(ValueError):
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
