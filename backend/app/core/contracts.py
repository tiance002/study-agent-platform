"""契约层共用的校验原语。

为什么需要这个模块：`identity` 与 `product` 两个包的契约都要校验同一组规则 ——
「id 不能为空」「时间戳必须带时区」「序号必须为正」。各写一份的话，
两边会以不同速度演进，而它们表达的是**同一条不变量**。

放在 `core` 而不是某一侧，是为了不让 `identity` 依赖 `product`（或反过来）：
跨层契约住 `core` 是本项目的既定分层。

## 为什么"时间戳必须带时区"值得一条断言

naive `datetime` 参与比较时不会报错，只会给出**按本地时区解释**的结果。
在跨进程重放、跨时区部署、以及"把库里的值读回来再比"这三种场景里，
它会静默地差几个小时 —— 而这类错误不会在开发机上暴露。
"""
from __future__ import annotations

from datetime import datetime


def require_text(value: str, field: str, *, allow_empty: bool = False) -> str:
    """要求一个非空白字符串。

    `TypeError` 与 `ValueError` 刻意区分：类型不对是**调用方写错了代码**，
    值为空是**数据不合法**。混成一个会让排障时分不清该改代码还是改数据。
    """
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是字符串，收到 {type(value).__name__}")
    if not allow_empty and value.strip() == "":
        raise ValueError(f"{field} 不能为空")
    return value


def require_id(value: str, field: str) -> str:
    """标识符：非空字符串。"""
    return require_text(value, field)


def require_aware(value: datetime, field: str) -> datetime:
    """要求带时区的时间戳。"""
    if not isinstance(value, datetime):
        raise TypeError(f"{field} 必须是 datetime，收到 {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field} 必须是带时区的时间（naive 时间在不同时区下含义不同）")
    return value


def later_than(later: datetime, earlier: datetime, field: str) -> None:
    """要求 `later` 严格晚于 `earlier`（用在 expires_at / updated_at 这类字段）。"""
    if later <= earlier:
        raise ValueError(f"{field} 必须晚于签发时间")


def _require_int(value: int, field: str) -> None:
    # `bool` 是 `int` 的子类，但 `seq=True` 显然是写错了 —— 单独挡掉。
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} 必须是整数，收到 {type(value).__name__}")


def require_positive(value: int, field: str) -> None:
    """要求正整数（序号、版本号）。"""
    _require_int(value, field)
    if value <= 0:
        raise ValueError(f"{field} 必须为正整数，收到 {value}")


def require_non_negative(value: int, field: str) -> None:
    """要求非负整数（从 0 开始的排序位）。"""
    _require_int(value, field)
    if value < 0:
        raise ValueError(f"{field} 不能为负，收到 {value}")
