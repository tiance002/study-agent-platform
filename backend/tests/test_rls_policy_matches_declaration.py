"""RLS 声明与数据库**实际策略**的一致性。

## 为什么需要这条

SQL 契约里的"隔离级别"来自迁移里的**人工声明**（`PROJECT_SCOPED` /
`PRINCIPAL_SCOPED` / `POLICY_OVERRIDES`），而真正生效的是数据库里的 `pg_policy`。
两者一旦漂移，契约就是在**撒谎**。

这不是假想的 —— 本轮自查实测抓到：`http_idempotency` 只受租户级策略保护，
而它的 `response_body` 存着命令响应（业务载荷），**同租户的另一个用户能直接读到**。
当时声明的常量写的是"租户级"，看起来完全正常；问题恰恰在于
**没有人问过"这张表有 `principal_id` 列，为什么不约束它"**。

## 规则完全从数据库推导

| 表里有这一列（且 **NOT NULL**） | 策略就必须真的约束它 |
|---|---|
| `tenant_id` | `app.tenant_id`（且必须 `ENABLE` + `FORCE ROW LEVEL SECURITY`） |
| `project_id` | `app.project_id` |
| `principal_id` | `app.principal_id` |

限定 NOT NULL 是必要的：`http_idempotency.project_id` 可空（创建项目的命令被占用时
项目还不存在），给它加项目维度会让那条幂等记录**永远读不出来**。规则粗糙时，
只能用豁免去补 —— 而豁免清单会自己长大。

## 两类例外，分开记

- `EXEMPT`：**规则在这里本不适用**（附理由）。
- `KNOWN_GAP`：**规则适用，但现在还没做**（附理由与解除条件）。
  并且有测试断言"缺口仍然是缺口" —— 修好之后它会失败，提醒你来删条目。
"""

from __future__ import annotations

import os

import psycopg
import pytest

MIGRATION_DSN = os.environ.get(
    "STUDY_PLATFORM_MIGRATION_DSN",
    "postgresql://postgres@127.0.0.1:5432/study_platform",
)

#: 身份目录表：它们**就是**身份的载体，按主体过滤会让它们读不出任何行。
IDENTITY_TABLES = frozenset({"principals", "tenants"})

#: 规则在这里**本不适用**。每条都必须给出理由。
EXEMPT: dict[tuple[str, str], str] = {
    ("project_grants", "project"): (
        "授权记录刻意留在租户级：`projects` 的策略要引用它，"
        "若它自身也要求 `app.project_id` 会形成自引用。"
        "代价是同租户成员能看到授权关系 —— 属租户内管理信息，可接受。"
    ),
    ("project_grants", "principal"): (
        "同上：这里的 `principal_id` 是「被授权人」，不是「当前主体」，"
        "按当前主体过滤会让授权表读不出任何行。"
    ),
    ("projects", "project"): (
        "它的策略按 `project_grants` 的成员关系过滤（成员感知），"
        "而不是简单的 `project_id` 列匹配。这是**刻意的更强约束**，不是遗漏。"
    ),
}

#: 规则适用、但**现在还没做**的真缺口。修好之后
#: `test_known_gaps_are_still_real` 会失败，提醒你来删条目。
KNOWN_GAP: dict[tuple[str, str], str] = {
    ("confirmations", "principal"): (
        "确认记录带 `params_hash` 与成本上界，项目级策略下同项目的其他成员"
        "能读到彼此批准了什么。不含业务载荷（比幂等响应体轻），但仍不该互相可见。"
        "补它的连带改动：`tenant_transaction` 要能设置 `app.principal_id`，"
        "`PostgresConfirmationStore` 与现有确认测试要同步 —— "
        "属第一轮任务 2/3 的范围。"
    ),
}


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(MIGRATION_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not _postgres_reachable(),
        reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）",
    ),
]


def _table_columns(conn: psycopg.Connection) -> dict[str, dict[str, bool]]:
    """`{表: {列: 是否可空}}`。"""
    rows = conn.execute(
        "SELECT table_name, column_name, is_nullable FROM information_schema.columns"
        " WHERE table_schema = 'public'"
    ).fetchall()
    columns: dict[str, dict[str, bool]] = {}
    for table, column, nullable in rows:
        columns.setdefault(table, {})[column] = nullable == "YES"
    return columns


def _policies(conn: psycopg.Connection) -> dict[str, dict[str, object]]:
    rows = conn.execute(
        "SELECT c.relname,"
        "       coalesce(pg_get_expr(p.polqual, p.polrelid), ''),"
        "       c.relrowsecurity, c.relforcerowsecurity"
        "  FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid"
    ).fetchall()
    policies: dict[str, dict[str, object]] = {}
    for table, using, rls, forced in rows:
        entry = policies.setdefault(
            table, {"using": "", "rls": False, "forced": False}
        )
        entry["using"] = f"{entry['using']} {using}"
        entry["rls"] = bool(rls)
        entry["forced"] = bool(forced)
    return policies


@pytest.fixture(scope="module")
def db_facts() -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, object]]]:
    with psycopg.connect(MIGRATION_DSN) as conn:
        columns = _table_columns(conn)
        policies = _policies(conn)

    assert "user_sessions" in columns, (
        "数据库里没有 0002 的表 —— 先跑 `alembic upgrade head`。"
        "这组测试验的是迁移后的真实策略，跳过它会等于没有这道门"
    )
    return columns, policies


def _required_dimensions(
    table: str, columns: dict[str, bool]
) -> list[tuple[str, str]]:
    """这张表按规则需要哪些维度的约束。返回 [(维度, 列名)]。"""
    required: list[tuple[str, str]] = []
    if table not in IDENTITY_TABLES:
        if columns.get("project_id") is False:
            required.append(("project", "app.project_id"))
        if columns.get("principal_id") is False:
            required.append(("principal", "app.principal_id"))
    return required


@pytest.mark.invariant
def test_every_tenant_table_has_forced_rls(db_facts) -> None:
    """带 `tenant_id` 列的表必须真的有策略，且 ENABLE + FORCE 都开着。

    只 `ENABLE` 不够：表 owner（迁移角色）会绕过策略，
    `FORCE` 才是"连 owner 也得守规矩"。少了它，任何一次以 owner 身份
    跑的查询都能看到全部租户。
    """
    columns, policies = db_facts
    missing: list[str] = []
    for table, cols in sorted(columns.items()):
        if "tenant_id" not in cols:
            continue
        policy = policies.get(table)
        if policy is None:
            missing.append(f"{table}：没有策略")
        elif not policy["rls"] or not policy["forced"]:
            missing.append(f"{table}：ENABLE={policy['rls']} FORCE={policy['forced']}")
    assert not missing, missing


@pytest.mark.invariant
def test_not_null_dimension_columns_are_constrained(db_facts) -> None:
    """NOT NULL 的项目/主体列必须在策略里真的被约束。

    这条规则的两个"本不适用"由 `EXEMPT` 记录，
    "该做未做"由 `KNOWN_GAP` 记录 —— **两者都不在这里悄悄放行**。
    """
    columns, policies = db_facts
    offenders: list[str] = []
    for table, cols in sorted(columns.items()):
        policy = policies.get(table)
        predicate = str(policy["using"]) if policy else ""
        for dimension, column_setting in _required_dimensions(table, cols):
            if (table, dimension) in EXEMPT or (table, dimension) in KNOWN_GAP:
                continue
            if column_setting not in predicate:
                offenders.append(f"{table}：策略未约束 {column_setting}")
    assert not offenders, offenders


@pytest.mark.invariant
def test_exemptions_are_all_still_needed(db_facts) -> None:
    """`EXEMPT` 的条目必须仍然"本来会被规则命中"。

    否则它会永远躺在清单里 —— 而一条不再需要的豁免，
    下次会被人当作"这里确实可以不加"的依据。**豁免清单会自己长大。**
    """
    columns, _policies = db_facts
    stale: list[str] = []
    for (table, dimension), reason in EXEMPT.items():
        if table not in columns:
            stale.append(f"({table}, {dimension})：表已不存在 —— {reason}")
            continue
        if (dimension, f"app.{dimension}_id") not in _required_dimensions(table, columns[table]):
            stale.append(f"({table}, {dimension})：规则已不再命中 —— {reason}")
    assert not stale, stale


@pytest.mark.invariant
def test_known_gaps_are_still_real(db_facts) -> None:
    """`KNOWN_GAP` 必须**仍然是缺口**。

    反过来说：一旦有人补上了它，这条会失败 —— 那正是该来删条目的信号。
    没有这条，"已知缺口"会变成永久的自我安慰。
    """
    columns, policies = db_facts
    fixed: list[str] = []
    for (table, dimension) in KNOWN_GAP:
        policy = policies.get(table)
        predicate = str(policy["using"]) if policy else ""
        if f"app.{dimension}_id" in predicate:
            fixed.append(
                f"({table}, {dimension}) 已经补上了 —— "
                f"请从 KNOWN_GAP 里删掉它（这份清单只该收留真正的缺口）"
            )
    assert not fixed, fixed


@pytest.mark.invariant
def test_projects_policy_is_membership_aware(db_facts) -> None:
    """`projects` 必须是成员感知的，不能退回成纯租户级。

    判据：谓词里出现 `project_grants` 与 `app.principal_id`。
    退回租户级意味着**同租户的用户 A 能看到用户 B 的项目** ——
    实测过：改前 1 行，改后 0 行。
    """
    _columns, policies = db_facts
    policy = policies.get("projects")
    assert policy is not None, "projects 没有策略"
    predicate = str(policy["using"])
    assert "project_grants" in predicate, f"projects 策略丢了成员关系：{predicate}"
    assert "app.principal_id" in predicate, f"projects 策略丢了主体维度：{predicate}"
