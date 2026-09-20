"""树形预算账本与原子预留。

设计依据：03 号规格 §5、不变量 #8。

三条性质（均有测试）：
1. 子账户只能用父账户授予的 reservation，**不能动用父账户的剩余额度**；
2. `completion_reserve` 预留给父账户自行收尾，**不可借出**；
3. reservation 进入 `in_flight` 后不能仅因 TTL 到期释放（03 号规格 §5）——
   已派发的调用不能被当作没发生。

账本本身是可丢失的加速结构吗？不是。它是**事实**，但可在 PostgreSQL 中重建，
因此本版用内存实现并由测试证明语义，持久化留给存储适配器。
"""

from __future__ import annotations

import contextlib
import functools
import threading
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.errors import ErrorCode, deny
from app.core.ids import new_id


def _synchronized(method):
    """把整个方法关进账本的临界区。

    账本的不变量（可用额度 = 上限 − 预留 − 消费 − 完成预留）是**跨字段**的，
    所以任何"读-改-写"都必须是原子的：两个并发预留会都看到"够用"，然后一起扣。

    这不是理论问题。实测中并发创建同一个 run 账户时，
    第二个线程**在父账户已经预留成功之后**才撞上「账户已存在」并抛错 ——
    那次预留就永远挂在那里，既没被消费也没被释放。**失败留了部分状态**，
    正好违反本类文档承诺的那条不变量。

    内存适配器用 `RLock`（可重入：`grant_to_child` 会嵌套调用 `batch_reserve`
    与 `open_account`）。生产实现应把不变量交给数据库 —— 行锁 + 约束，
    而不是进程内的锁（多 worker 下等于不存在）。
    """

    @functools.wraps(method)
    def wrapper(self: "BudgetLedger", *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Dimension(StrEnum):
    """预算维度。分别记账，不互相折算。"""

    CURRENCY_MICROS = "currency_micros"
    TOKENS = "tokens"
    SANDBOX_SECONDS = "sandbox_seconds"
    STEPS = "steps"
    TOOL_CALLS = "tool_calls"


ALL_DIMENSIONS: tuple[Dimension, ...] = tuple(Dimension)


class ReservationState(StrEnum):
    HELD = "held"            # 已预留，未派发，可释放
    IN_FLIGHT = "in_flight"  # 已派发，不可因 TTL 释放
    SETTLED = "settled"      # 已按实际结算
    RELEASED = "released"    # 已释放回可用池


def _zero_map() -> dict[str, int]:
    return {str(d): 0 for d in ALL_DIMENSIONS}


class ReservationKind(StrEnum):
    """预留给两种用途，性质完全不同，混在一起会让人误判「预算泄漏」。"""

    GRANT = "grant"  # 额度授予：父账户把额度锁定给子账户，直到子账户关闭
    CALL = "call"    # 单次调用：成本上界，调用结束后立即结算


@dataclass
class Reservation:
    reservation_id: str
    account_id: str
    dimension: str
    amount: int
    state: ReservationState = ReservationState.HELD
    consumed: int = 0
    kind: ReservationKind = ReservationKind.CALL

    @property
    def releasable(self) -> bool:
        """只有尚未派发的预留可以释放。"""
        return self.state is ReservationState.HELD


@dataclass
class Account:
    """预算账户。树形结构：Tenant → User/Project → Run → Node。

    `tenant_id` / `project_id` 是**结构化字段**，用于按租户与项目隔离查询。
    靠解析 `account_id` 的命名前缀来过滤是不可靠的：前缀只是命名约定，不是约束。
    """

    account_id: str
    parent_id: str | None
    tenant_id: str | None = None
    project_id: str | None = None
    limits: dict[str, int] = field(default_factory=_zero_map)
    completion_reserve: dict[str, int] = field(default_factory=_zero_map)
    reserved: dict[str, int] = field(default_factory=_zero_map)
    consumed: dict[str, int] = field(default_factory=_zero_map)

    def available(self, dimension: str) -> int:
        """可动用额度 = 上限 − 已预留 − 已消费 − 完成预留。"""
        return (
            self.limits.get(dimension, 0)
            - self.reserved.get(dimension, 0)
            - self.consumed.get(dimension, 0)
            - self.completion_reserve.get(dimension, 0)
        )

    def snapshot(self) -> dict:
        return {
            "account_id": self.account_id,
            "parent_id": self.parent_id,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "limits": dict(self.limits),
            "completion_reserve": dict(self.completion_reserve),
            "reserved": dict(self.reserved),
            "consumed": dict(self.consumed),
        }


class BudgetLedger:
    """预算账本。所有额度变动都是原子操作：失败不留部分状态。"""

    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._reservations: dict[str, Reservation] = {}
        # 子账户 → 授予时在父账户上占用的预留。关闭账户时据此回收额度。
        self._grant_reservations: dict[str, list[Reservation]] = {}
        # 额度变动一律走 `_synchronized`，见那个装饰器的说明。
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 临界区

    @contextlib.contextmanager
    def atomic(self):
        """把**一组**操作关进同一临界区。

        为什么需要它：单个方法原子 ≠ 一组方法原子。
        `if 账户不存在: 创建账户` 是典型的 check-then-act —— 两个线程都会
        通过检查，第二个再抛「账户已存在」。实测就是这样：
        `_ensure_budget_tree` 在并发下稳定抛 `BUDGET_TREE_INVALID`，
        而账本里每个方法各自都"加了锁"。

        ⚠️ 这**不是数据库事务**：没有隔离级别、**没有回滚**，
        只保证互斥。出错后已做的变动仍然保留，调用方需自行保证顺序。
        生产实现应换成数据库事务（行锁 + 约束）。

        用 `RLock` 实现，所以内部方法仍然可以各自加锁、可以重入。
        """
        with self._lock:
            yield

    # ------------------------------------------------------------------ 账户

    def has_account(self, account_id: str) -> bool:
        """账户是否存在。

        比让调用方摸 `_accounts` 更好：**不交出可变字典**，避免"顺手改一下"
        绕过所有额度不变量。需要跨方法原子时配合 `atomic()` 使用。
        """
        return account_id in self._accounts

    @_synchronized
    def open_account(
        self,
        account_id: str,
        *,
        parent_id: str | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
        limits: dict[str, int] | None = None,
        completion_reserve: dict[str, int] | None = None,
    ) -> Account:
        if account_id in self._accounts:
            raise deny(ErrorCode.BUDGET_TREE_INVALID, f"账户已存在：{account_id}")
        if parent_id is not None:
            parent = self._accounts.get(parent_id)
            if parent is None:
                raise deny(
                    ErrorCode.BUDGET_TREE_INVALID,
                    f"父账户不存在：{parent_id}；预算树必须自顶向下建立",
                )
            # 子账户**继承**父账户的租户/项目：调用方漏传时账目也不会脱离隔离范围。
            tenant_id = tenant_id or parent.tenant_id
            project_id = project_id or parent.project_id
        account = Account(
            account_id=account_id,
            parent_id=parent_id,
            tenant_id=tenant_id,
            project_id=project_id,
            limits=dict(limits or {}),
            completion_reserve=dict(completion_reserve or {}),
        )
        self._accounts[account_id] = account
        return account

    def account(self, account_id: str) -> Account:
        try:
            return self._accounts[account_id]
        except KeyError as exc:
            raise deny(ErrorCode.BUDGET_TREE_INVALID, f"账户不存在：{account_id}") from exc

    # ------------------------------------------------------------------ 预留

    @_synchronized
    def reserve(
        self,
        account_id: str,
        dimension: str,
        amount: int,
        *,
        kind: ReservationKind = ReservationKind.CALL,
    ) -> Reservation:
        """预留额度。不足即失败 —— 预留失败不得执行（不变量 #8）。"""
        if amount < 0:
            raise deny(ErrorCode.BUDGET_RESERVATION_FAILED, "预留额度不能为负")
        account = self.account(account_id)
        if amount > account.available(dimension):
            raise deny(
                ErrorCode.BUDGET_RESERVATION_FAILED,
                f"账户 {account_id} 的 {dimension} 可用额度为 "
                f"{account.available(dimension)}，不足以预留 {amount}",
                account_id=account_id,
                dimension=dimension,
                available=account.available(dimension),
                requested=amount,
            )
        account.reserved[dimension] = account.reserved.get(dimension, 0) + amount
        reservation = Reservation(
            reservation_id=new_id("rsv"),
            account_id=account_id,
            dimension=dimension,
            amount=amount,
            kind=kind,
        )
        self._reservations[reservation.reservation_id] = reservation
        return reservation

    @_synchronized
    def batch_reserve(
        self,
        account_id: str,
        grants: dict[str, int],
        *,
        kind: ReservationKind = ReservationKind.CALL,
    ) -> list[Reservation]:
        """批量预留，要么全成要么全不动。

        「原子」在这里的具体含义：任一维度失败，已成功的维度立即释放。
        """
        taken: list[Reservation] = []
        try:
            for dimension, amount in grants.items():
                taken.append(self.reserve(account_id, dimension, amount, kind=kind))
        except Exception:
            for reservation in taken:
                self._reservations.pop(reservation.reservation_id, None)
                account = self._accounts[reservation.account_id]
                account.reserved[reservation.dimension] -= reservation.amount
            raise
        return taken

    @_synchronized
    def mark_in_flight(self, reservation_id: str) -> Reservation:
        """标记为已派发。此后不可因 TTL 到期释放。"""
        reservation = self._reservation(reservation_id)
        if reservation.state is not ReservationState.HELD:
            raise deny(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                f"预留 {reservation_id} 处于 {reservation.state}，不能进入 in_flight",
            )
        reservation.state = ReservationState.IN_FLIGHT
        return reservation

    @_synchronized
    def settle(self, reservation_id: str, actual: int) -> Reservation:
        """按实际用量结算。实际用量不得超过预留上界。"""
        reservation = self._reservation(reservation_id)
        if reservation.state is not ReservationState.IN_FLIGHT:
            raise deny(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                f"预留 {reservation_id} 处于 {reservation.state}，不能结算；"
                f"未派发的预留应先释放",
            )
        if actual < 0 or actual > reservation.amount:
            raise deny(
                ErrorCode.BUDGET_EXCEEDED,
                f"实际用量 {actual} 超出预留上界 {reservation.amount}；"
                f"预留必须是成本上界，超出说明预留逻辑有缺陷",
                reservation_id=reservation_id,
            )
        account = self.account(reservation.account_id)
        account.reserved[reservation.dimension] -= reservation.amount
        account.consumed[reservation.dimension] = (
            account.consumed.get(reservation.dimension, 0) + actual
        )
        reservation.state = ReservationState.SETTLED
        reservation.consumed = actual
        return reservation

    @_synchronized
    def release(self, reservation_id: str) -> Reservation:
        """释放未派发的预留。已派发的调用不得释放。"""
        reservation = self._reservation(reservation_id)
        if not reservation.releasable:
            raise deny(
                ErrorCode.RECONCILIATION_REQUIRED,
                f"预留 {reservation_id} 处于 {reservation.state}，不可释放；"
                f"已派发的调用必须进入对账，不能当作没发生",
                reservation_id=reservation_id,
            )
        account = self.account(reservation.account_id)
        account.reserved[reservation.dimension] -= reservation.amount
        reservation.state = ReservationState.RELEASED
        return reservation

    # ------------------------------------------------------------------ 子树

    @_synchronized
    def grant_to_child(
        self,
        parent_id: str,
        child_id: str,
        grants: dict[str, int],
        *,
        child_completion_reserve: dict[str, int] | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> Account:
        """从父账户原子预留并开立子账户。

        子账户的额度**只能**来自这次授予；父账户剩余额度对子账户不可见。
        租户/项目缺省时继承父账户；语义上不属于父账户的（如用户级账户）必须显式传入。
        """
        if child_id in self._accounts:
            raise deny(ErrorCode.BUDGET_TREE_INVALID, f"子账户已存在：{child_id}")
        reservations = self.batch_reserve(parent_id, grants, kind=ReservationKind.GRANT)
        self._grant_reservations[child_id] = reservations
        return self.open_account(
            child_id,
            parent_id=parent_id,
            tenant_id=tenant_id,
            project_id=project_id,
            limits=grants,
            completion_reserve=child_completion_reserve,
        )

    @_synchronized
    def close_account(self, account_id: str) -> dict[str, int]:
        """关闭账户并把额度回收给父账户。

        三件事必须一起做，否则额度会泄漏：
        1. 账户下不得有未结预留（有则说明还有调用没结算，必须先对账）；
        2. 释放授予时在父账户上占用的预留；
        3. 把子账户的**实际消耗**计到父账户，而不是把整个授予额度算作消耗。

        第 3 点是容易写错的地方：把授予额度直接算作父账户消耗，会让父账户的
        可用额度随时间虚假下降，最终出现"明明没花那么多却说预算耗尽"。
        """
        account = self.account(account_id)
        outstanding = [
            r
            for r in self._reservations.values()
            if r.account_id == account_id
            and r.state in (ReservationState.HELD, ReservationState.IN_FLIGHT)
        ]
        if outstanding:
            raise deny(
                ErrorCode.RECONCILIATION_REQUIRED,
                f"账户 {account_id} 仍有 {len(outstanding)} 个未结预留，"
                f"关闭前必须先结算或对账",
                account_id=account_id,
                outstanding=len(outstanding),
            )

        settled: dict[str, int] = {}
        for reservation in self._grant_reservations.pop(account_id, []):
            parent = self._accounts[reservation.account_id]
            actual = account.consumed.get(reservation.dimension, 0)
            parent.reserved[reservation.dimension] -= reservation.amount
            parent.consumed[reservation.dimension] = (
                parent.consumed.get(reservation.dimension, 0) + actual
            )
            reservation.state = ReservationState.SETTLED
            reservation.consumed = actual
            settled[reservation.dimension] = actual
        return settled

    # ------------------------------------------------------------------ 对账

    def _reservation(self, reservation_id: str) -> Reservation:
        try:
            return self._reservations[reservation_id]
        except KeyError as exc:
            raise deny(
                ErrorCode.BUDGET_TREE_INVALID,
                f"预留不存在：{reservation_id}",
            ) from exc

    def open_reservations(self) -> tuple[Reservation, ...]:
        """未结的**调用**预留。用于对账与「预算敞口」指标。

        授予预留（`kind=GRANT`）不计入：它是额度分配，随子账户关闭而回收。
        把它算作敞口会让人误以为预算泄漏。
        """
        return tuple(
            r
            for r in self._reservations.values()
            if r.kind is ReservationKind.CALL
            and r.state in (ReservationState.HELD, ReservationState.IN_FLIGHT)
        )

    def grant_reservations(self) -> tuple[Reservation, ...]:
        """尚未回收的授予预留。用于运维观察额度分配情况。"""
        return tuple(
            r
            for r in self._reservations.values()
            if r.kind is ReservationKind.GRANT and r.state is ReservationState.HELD
        )

    def reservations_scoped(
        self, *, tenant_id: str, project_id: str | None = None
    ) -> tuple[Reservation, ...]:
        """按租户（可选项目）过滤的未结调用预留。

        先取到账户再比对结构化字段，而不是解析 `account_id` 的命名前缀。
        """
        scoped: list[Reservation] = []
        for reservation in self.open_reservations():
            account = self._accounts.get(reservation.account_id)
            if account is None or account.tenant_id != tenant_id:
                continue
            if project_id is not None and account.project_id != project_id:
                continue
            scoped.append(reservation)
        return tuple(scoped)

    def exposure(self, dimension: str) -> int:
        """未结风险敞口：所有未结算预留之和。"""
        return sum(r.amount for r in self.open_reservations() if r.dimension == dimension)
