"""持久化教学预算的 SQL 内核（在**调用方的事务**内执行）。

为什么是"内核函数"而不是独立适配器：预算记账必须与运行创建/落定
**同事务**（`start_run` 原子写消息 + 运行 + 预留；`finish_run` 原子写
消息 + 结算 + 终态）。独立适配器自开事务会把一次原子命令拆成两个
提交点 —— "已扣款但运行不存在"就是这么来的。

本模块的函数只做三件事：读账户事实、按共享不变量（`budget.ports`）
判定、执行记账 UPDATE。容量判定的规则只有一个出口（`assert_capacity`），
PG 与内存适配器跑的是同一份。

调用约定：`conn` 必须已在一个设置了租户（及项目，视表而定）RLS 上下文的
事务内 —— 本模块不自行开事务，也不自行设置上下文。
"""

from __future__ import annotations

from app.budget.ports import (
    BudgetAccountFacts,
    ReservationState,
    assert_capacity,
)
from app.core.errors import ErrorCode, PlatformError, deny


def ensure_and_lock_accounts(
    conn,
    *,
    tenant_id: str,
    project_id: str,
    total_micro: int,
    max_input_tokens: int,
    max_output_tokens: int,
) -> tuple[BudgetAccountFacts, BudgetAccountFacts]:
    """账户不存在则按部署配置建，并 `FOR UPDATE` 锁住两行（防并发预留竞态）。

    双行锁的顺序固定为租户 → 项目：所有调用方按同一顺序加锁，
    两个项目同时预留才不会互相死锁。
    """
    conn.execute(
        "INSERT INTO teaching_tenant_budgets (tenant_id, total_micro, price_version)"
        " VALUES (%s, %s, %s) ON CONFLICT (tenant_id) DO NOTHING",
        (tenant_id, total_micro, _price_version()),
    )
    conn.execute(
        "INSERT INTO teaching_budgets (tenant_id, project_id, total_micro,"
        " max_input_tokens, max_output_tokens, price_version)"
        " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, project_id) DO NOTHING",
        (tenant_id, project_id, total_micro, max_input_tokens, max_output_tokens, _price_version()),
    )
    tenant_row = conn.execute(
        "SELECT total_micro, reserved_micro, in_flight_micro, spent_micro, price_version"
        " FROM teaching_tenant_budgets WHERE tenant_id = %s FOR UPDATE",
        (tenant_id,),
    ).fetchone()
    project_row = conn.execute(
        "SELECT total_micro, reserved_micro, in_flight_micro, spent_micro, price_version"
        " FROM teaching_budgets WHERE tenant_id = %s AND project_id = %s FOR UPDATE",
        (tenant_id, project_id),
    ).fetchone()
    if tenant_row is None or project_row is None:
        # 刚 INSERT 过还读不到 = RLS 上下文没设对 —— 立刻炸，不静默继续。
        raise PlatformError(
            ErrorCode.TENANT_CONTEXT_MISSING,
            "预算账户读取失败：请检查 RLS 上下文（tenant/project）",
        )
    return _facts("tenant", tenant_row), _facts("project", project_row)


def reserve_in_conn(
    conn,
    *,
    tenant_id: str,
    project_id: str,
    reservation_id: str,
    run_id: str,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    total_micro: int,
    max_input_tokens: int,
    max_output_tokens: int,
) -> None:
    """预留：容量判定 + 双账户扣额度 + 写预留行。全部在调用方事务内。"""
    from app.budget.ports import estimate_micro

    estimated_micro = estimate_micro(estimated_input_tokens, estimated_output_tokens)
    tenant_facts, project_facts = ensure_and_lock_accounts(
        conn,
        tenant_id=tenant_id,
        project_id=project_id,
        total_micro=total_micro,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
    )
    assert_capacity(
        tenant_facts,
        project_facts,
        estimated_micro=estimated_micro,
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
    )
    conn.execute(
        "UPDATE teaching_tenant_budgets SET reserved_micro = reserved_micro + %s,"
        " updated_at = now() WHERE tenant_id = %s",
        (estimated_micro, tenant_id),
    )
    conn.execute(
        "UPDATE teaching_budgets SET reserved_micro = reserved_micro + %s,"
        " updated_at = now() WHERE tenant_id = %s AND project_id = %s",
        (estimated_micro, tenant_id, project_id),
    )
    conn.execute(
        "INSERT INTO teaching_reservations (reservation_id, tenant_id, project_id,"
        " run_id, state, estimated_micro, estimated_input_tokens,"
        " estimated_output_tokens, price_version)"
        " VALUES (%s, %s, %s, %s, 'held', %s, %s, %s, %s)",
        (
            reservation_id,
            tenant_id,
            project_id,
            run_id,
            estimated_micro,
            estimated_input_tokens,
            estimated_output_tokens,
            _price_version(),
        ),
    )


def hold_to_in_flight_in_conn(conn, *, run_id: str) -> int:
    """held → in_flight：额度从 reserved 划入 in_flight。返回估计金额。"""
    row = conn.execute(
        "SELECT reservation_id, state, estimated_micro FROM teaching_reservations"
        " WHERE run_id = %s FOR UPDATE",
        (run_id,),
    ).fetchone()
    if row is None:
        raise PlatformError(
            ErrorCode.BUDGET_TREE_INVALID, "运行没有预算预留（记账与运行脱节）"
        )
    reservation_id, state, estimated_micro = row
    if state != str(ReservationState.HELD):
        raise PlatformError(
            ErrorCode.BUDGET_TREE_INVALID,
            f"派发前的预留必须停在 held，当前 {state}（记账与派发已经脱节）",
        )
    conn.execute(
        "UPDATE teaching_reservations SET state = 'in_flight', updated_at = now()"
        " WHERE reservation_id = %s",
        (reservation_id,),
    )
    _move(conn, run_id, from_col="reserved_micro", amount=estimated_micro)
    _add(conn, run_id, col="in_flight_micro", amount=estimated_micro)
    return int(estimated_micro)


def settle_in_conn(
    conn, *, run_id: str, actual_micro: int | None, input_tokens: int | None,
    output_tokens: int | None,
) -> None:
    """结算（actual_micro 非空）或保留敞口（None → 预留停在 in_flight）。"""
    row = conn.execute(
        "SELECT reservation_id, state, estimated_micro FROM teaching_reservations"
        " WHERE run_id = %s FOR UPDATE",
        (run_id,),
    ).fetchone()
    if row is None:
        raise PlatformError(
            ErrorCode.BUDGET_TREE_INVALID, "运行没有预算预留（记账与运行脱节）"
        )
    reservation_id, state, estimated_micro = row
    if actual_micro is None:
        # 缺权威用量：不做任何记账动作 —— 预留停在 in_flight，敞口可见。
        # "没报用量"绝不等于"没花钱"。
        return
    if state != str(ReservationState.IN_FLIGHT):
        raise PlatformError(
            ErrorCode.BUDGET_TREE_INVALID,
            f"只能结算 in_flight 的预留，当前 {state}",
        )
    conn.execute(
        "UPDATE teaching_reservations SET state = 'settled', actual_micro = %s,"
        " updated_at = now() WHERE reservation_id = %s",
        (actual_micro, reservation_id),
    )
    # in_flight 释放**估计**，spent 记**实际** —— 估计 ≠ 实际的差额
    # 自然反映在可用额度里。把估计当实际入账会伪造账目（实测抓下）。
    _move(conn, run_id, from_col="in_flight_micro", amount=estimated_micro)
    _add(conn, run_id, col="spent_micro", amount=actual_micro)


def release_in_conn(conn, *, run_id: str) -> None:
    """释放预留：held（未派发）或 in_flight（可证明未送达）→ released。"""
    row = conn.execute(
        "SELECT reservation_id, state, estimated_micro FROM teaching_reservations"
        " WHERE run_id = %s FOR UPDATE",
        (run_id,),
    ).fetchone()
    if row is None:
        raise PlatformError(
            ErrorCode.BUDGET_TREE_INVALID, "运行没有预算预留（记账与运行脱节）"
        )
    reservation_id, state, estimated_micro = row
    if state not in (str(ReservationState.HELD), str(ReservationState.IN_FLIGHT)):
        raise PlatformError(
            ErrorCode.BUDGET_TREE_INVALID, f"只能释放 held / in_flight 的预留，当前 {state}"
        )
    conn.execute(
        "UPDATE teaching_reservations SET state = 'released', updated_at = now()"
        " WHERE reservation_id = %s",
        (reservation_id,),
    )
    if state == str(ReservationState.IN_FLIGHT):
        _move(conn, run_id, from_col="in_flight_micro", amount=estimated_micro)
    else:
        _move(conn, run_id, from_col="reserved_micro", amount=estimated_micro)


def snapshot_in_conn(conn, *, tenant_id: str, project_id: str) -> dict:
    """租户级与项目级账户事实。行不存在时给 None（还没有过预留）。"""
    tenant_row = conn.execute(
        "SELECT total_micro, reserved_micro, in_flight_micro, spent_micro, price_version"
        " FROM teaching_tenant_budgets WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchone()
    project_row = conn.execute(
        "SELECT total_micro, reserved_micro, in_flight_micro, spent_micro, price_version"
        " FROM teaching_budgets WHERE tenant_id = %s AND project_id = %s",
        (tenant_id, project_id),
    ).fetchone()
    return {
        "tenant": _facts("tenant", tenant_row).to_dict() if tenant_row else None,
        "project": _facts("project", project_row).to_dict() if project_row else None,
    }


# ------------------------------------------------------------------ 内部


def _price_version() -> str:
    from app.budget.ports import PRICE_VERSION

    return PRICE_VERSION


def _facts(scope: str, row: tuple) -> BudgetAccountFacts:
    return BudgetAccountFacts(
        scope=scope,
        total_micro=int(row[0]),
        reserved_micro=int(row[1]),
        in_flight_micro=int(row[2]),
        spent_micro=int(row[3]),
        price_version=str(row[4]),
    )


def _move(conn, run_id: str, *, from_col: str, amount: int) -> None:
    """在租户与项目两个账户里同时扣减某一列。行必须已被 FOR UPDATE 锁住。"""
    owner = conn.execute(
        "SELECT tenant_id, project_id FROM teaching_runs WHERE run_id = %s", (run_id,)
    ).fetchone()
    if owner is None:  # pragma: no cover - 调用方已确认运行存在
        raise deny(ErrorCode.CROSS_TENANT_DENIED, "运行不存在", run_id=run_id)
    tenant_id, project_id = owner
    for table, where in (
        ("teaching_tenant_budgets", "tenant_id = %s"),
        ("teaching_budgets", "tenant_id = %s AND project_id = %s"),
    ):
        # 占位符顺序：SET 子句在前，WHERE 子句在后。
        where_params: list = (
            [tenant_id, project_id] if "project_id" in where else [tenant_id]
        )
        set_clauses = [f"{from_col} = {from_col} - %s"]
        set_params: list = [amount]
        updated = conn.execute(
            f"UPDATE {table} SET {', '.join(set_clauses)}, updated_at = now()"
            f" WHERE {where}",
            tuple([*set_params, *where_params]),
        ).rowcount
        if updated != 1:  # pragma: no cover - 账户行在前面 ensure 过
            raise PlatformError(
                ErrorCode.TENANT_CONTEXT_MISSING,
                f"预算账户划转未命中（{table}）：RLS 上下文异常",
            )


def _add(conn, run_id: str, *, col: str, amount: int) -> None:
    """在租户与项目两个账户里同时增加某一列（结算入账）。"""
    owner = conn.execute(
        "SELECT tenant_id, project_id FROM teaching_runs WHERE run_id = %s", (run_id,)
    ).fetchone()
    if owner is None:  # pragma: no cover - 调用方已确认运行存在
        raise deny(ErrorCode.CROSS_TENANT_DENIED, "运行不存在", run_id=run_id)
    tenant_id, project_id = owner
    for table, where in (
        ("teaching_tenant_budgets", "tenant_id = %s"),
        ("teaching_budgets", "tenant_id = %s AND project_id = %s"),
    ):
        where_params: list = (
            [tenant_id, project_id] if "project_id" in where else [tenant_id]
        )
        updated = conn.execute(
            f"UPDATE {table} SET {col} = {col} + %s, updated_at = now() WHERE {where}",
            tuple([amount, *where_params]),
        ).rowcount
        if updated != 1:  # pragma: no cover - 账户行在前面 ensure 过
            raise PlatformError(
                ErrorCode.TENANT_CONTEXT_MISSING,
                f"预算账户入账未命中（{table}）：RLS 上下文异常",
            )
