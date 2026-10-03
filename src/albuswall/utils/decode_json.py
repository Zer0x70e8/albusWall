#
""""""

import json
from typing import Any, Optional, Dict, List


def decode_json(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def json_loads_list(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    parsed = decode_json(raw)
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed]


def json_loads_dict(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, dict):
        return raw
    parsed = decode_json(raw)
    return parsed if isinstance(parsed, dict) else None
