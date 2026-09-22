"""认领围栏：`claim_token` 让"失去租约的一方"无法改写新持有者的任务。

第九份迁移。修的是第 4 轮审查的 R4-02（P1）。

## 缺陷：租约只规定"谁能认领"，不规定"谁能落定"

0007 起 `complete` / `fail` 只比对**任务状态**是不是 `processing`。于是在这条
时序上出错（内存适配器实测复现）：

| 步骤 | 发生的事情 |
|---|---|
| 1 | worker A 认领，租约 300 秒 |
| 2 | A 卡住（GC / 网络 / 主机暂停），租约到期 |
| 3 | worker B 认领同一条任务，开始处理 |
| 4 | A 苏醒，调用 `fail()` |
| 结果 | 任务被改成 `failed` —— 而 B 正在正常处理它 |

A 的迟到 `complete` 同样能抢先落结果：B 写完的片段与 A 的混在一起，
而"谁写的"没有任何记录。**租约只挡住了"同时做"，没挡住"事后改"。**

`worker_id` 不能当代次：同一个 worker 进程重启后 id 可能复用
（`--worker-id` 是运维给的），拿它比等于"同一个名字就算同一代"。

## 修法：每次认领产生不可复用的 `claim_token`

- `claim_next` 在认领的**同一条 UPDATE** 里写入 `gen_random_uuid()`；
- `complete` / `fail` 用条件更新：
  `WHERE job_id = ... AND status = 'processing' AND claim_token = ... AND lease_until > now()`；
- 条件落空时**再读一次状态**，把两种情况分开：
  - 已经是终态 → 幂等重放（worker 崩溃后消息重投），返回成功；
  - 仍是 `processing` 但 token 不是我的 → 稳定冲突 `ILLEGAL_STATE_TRANSITION`。

「已成功请求的幂等回执」与「过期请求」必须分开：前者必须成功（否则重投会
永远失败），后者必须失败（否则它就改写别人的任务了）。0007 的实现把两者
混成了一个分支 —— 而那正是本缺陷的入口。

## 为什么 `lease_until > now()` 也在条件里

只比 token 不够：同一代 worker 的租约**已经过期**时，它不该再落定任何东西 ——
否则"过期后又被第三方收回"的窗口里，旧持有者仍能抢先写入。
把"有效期限"放进同一个 WHERE，就把它变成了一次原子判定，而不是
"先查租约、再决定写入"的 check-then-act。

## 三处 SQL 与 Python 契约的重复（与 0007 同一取舍）

`(status = 'processing') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL
AND claim_token IS NOT NULL)` 在 Python 契约里也有一条。保留两份的理由与
0007 相同且更强：一条 `processing` 却没有 token 的行**永远无法被落定**
（`complete` 的条件更新永不匹配），任务静默卡死到下一次租约回收 —— 而回收后
仍然写不进去，因为那时它已经被重新认领、有了新 token（这正是设计意图）。

本迁移顺带把两个匿名 CHECK 改成**具名**约束：0007 里它们是在 `CREATE TABLE`
内联写的，名字由 PostgreSQL 自动生成（`ingestion_jobs_check` / `_check1`），
改约束时只能靠猜名字。具名之后，改约束的迁移写得出来，也读得懂。

与 0001–0008 一致的约定：迁移自包含、`downgrade` 真的还原（把列与两个
0007 形态的 CHECK 都写回去）。
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TABLE = "ingestion_jobs"

#: 两个约束的具名版本。名字定死，后续迁移才不必去猜 PostgreSQL 的自动命名。
LEASE_CONSTRAINT = "ingestion_jobs_lease_requires_processing"
FAILED_CONSTRAINT = "ingestion_jobs_failed_requires_error"

#: 租约 ↔ processing 的绑定关系（0009 起：多要求一个不可复用的认领 token）。
_LEASE_WITH_TOKEN = (
    "(status = 'processing') = (lease_owner IS NOT NULL"
    " AND lease_until IS NOT NULL AND claim_token IS NOT NULL)"
)

#: 0007 的形态 —— `downgrade` 要写回它。
_LEASE_LEGACY = (
    "(status = 'processing') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL)"
)

FAILED_RULE = "(status = 'failed') = (error_code <> '')"


def _drop_anonymous_checks() -> None:
    """删掉 0007 内联写的两个匿名 CHECK。

    按**定义文本**定位，而不是按猜测的名字：内联约束的名字由 PostgreSQL
    生成（`ingestion_jobs_check`、`ingestion_jobs_check1`），顺序还可能随
    建表语句的写法变化。猜名字的迁移在多库环境里迟早会漏删或错删。
    """
    op.execute(
        f"""
DO $$
DECLARE target record;
BEGIN
    FOR target IN
        SELECT conname FROM pg_constraint
         WHERE conrelid = '{TABLE}'::regclass
           AND contype = 'c'
           AND (pg_get_constraintdef(oid) LIKE '%processing%'
                OR pg_get_constraintdef(oid) LIKE '%error_code%')
    LOOP
        EXECUTE format('ALTER TABLE {TABLE} DROP CONSTRAINT %I', target.conname);
    END LOOP;
END
$$
"""
    )


def upgrade() -> None:
    # 列的默认值是 NULL（"没有认领"），唯一写它的地方是 claim_next。
    op.execute(f"ALTER TABLE {TABLE} ADD COLUMN claim_token uuid")

    _drop_anonymous_checks()

    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {LEASE_CONSTRAINT}"
        f" CHECK ({_LEASE_WITH_TOKEN})"
    )
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {FAILED_CONSTRAINT}"
        f" CHECK ({FAILED_RULE})"
    )


def downgrade() -> None:
    """还原成 0007 的形态：列删掉，两个 CHECK 写回不带 token 的版本。"""
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {LEASE_CONSTRAINT}")
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {FAILED_CONSTRAINT}")

    # 不指定名字：0007 的这两个约束本来就是匿名的，交给 PostgreSQL 自动命名
    # 才与"回到 0007"这件事一致（语义完全相同，命名方式也相同）。
    op.execute(f"ALTER TABLE {TABLE} ADD CHECK ({_LEASE_LEGACY})")
    op.execute(f"ALTER TABLE {TABLE} ADD CHECK ({FAILED_RULE})")

    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN claim_token")
