from collections.abc import Callable, Sequence
from typing import Any, TypeVar

import orjson


def json_dumps(value: Any) -> str:
    # On bytes/str dumps output:
    # https://github.com/ijl/orjson/issues/66
    return orjson.dumps(value).decode()


def dumpcut(data: Any, max_length: int, full_key: str, cut_key: str, cut_sep: str = "…") -> dict[str, Any]:
    """
    Shortcut for logging some JSONable data with limited length and a distinct key for the cut values.

    >>> dumpcut({"value": "short"}, max_length=20, full_key="fk", cut_key="ck")
    {'fk': {'value': 'short'}}
    >>> dumpcut({"value": "long" * 10}, max_length=20, full_key="fk", cut_key="ck")
    {'ck': '{"value":"…onglong"}'}
    """
    assert len(cut_sep) < max_length // 2
    data_s = json_dumps(data)
    if len(data_s) <= max_length:
        return {full_key: data}
    half_len = max_length // 2
    right_len = half_len - len(cut_sep)
    assert right_len > 0
    data_cut = "".join((data_s[:half_len], cut_sep, data_s[-right_len:]))
    return {cut_key: data_cut}


TItem = TypeVar("TItem")


def pick_out_special_items(
    items: Sequence[TItem], is_special: Callable[[TItem], bool]
) -> tuple[list[TItem], list[tuple[int, TItem]]]:
    normal_items = []
    special_items = []
    for idx, item in enumerate(items):
        if is_special(item):
            special_items.append((idx, item))
        else:
            normal_items.append(item)
    return normal_items, special_items


def put_in_special_results(
    normal_results: Sequence[TItem], special_results: Sequence[tuple[int, TItem]]
) -> list[TItem]:
    """
    Inverse of `pick_out_special_items`.

    >>> items = ["aa", "xbb", "cc", "xdd"]
    >>> normal_items, special_items = pick_out_special_items(items, is_special=lambda item: item.startswith("x"))
    >>> normal_items_res = [f"res_{item}" for item in normal_items]
    >>> special_items_res = [(idx, f"xres_{item}") for idx, item in special_items]
    >>> result = put_in_special_results(normal_items_res, special_items_res)
    >>> result
    ['res_aa', 'xres_xbb', 'res_cc', 'xres_xdd']
    """
    result = list(normal_results)
    for idx, item_res in special_results:
        result.insert(idx, item_res)
    return result
