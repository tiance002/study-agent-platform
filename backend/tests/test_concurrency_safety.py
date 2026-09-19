"""并发安全：这批修的四处都属于「单线程下完全正确，两个线程同时进来就出错」。

它们的共同点更要紧：**都不会报错**。

- 审计断链要等到校验时才发现；
- 幂等重复执行对客户端完全不可见（两次都返回 `idempotent_replay=false`）；
- 预算预留泄漏要等账户关不掉才暴露；
- 跨租户键冲突表现为"另一个租户莫名被拒"。

所以每条都必须有并发回归测试，且**必须配合 `racy_scheduling`** ——
默认 5ms 的 GIL 切换间隔会让这些测试集体空转，给出假信心。
本文件里的实测数字都是在压到 1μs 之后量出来的。
"""

from __future__ import annotations

import threading
import time

import pytest
from app.audit.sink import AuditSink, ChainProblem, RiskLevel
from app.budget.ledger import BudgetLedger, Dimension
from app.core.errors import ErrorCode, PlatformError
from app.workflow import runtime as rt
from app.workflow.runtime import InteractionRequest

CURRENCY = str(Dimension.CURRENCY_MICROS)


def _run_concurrently(tasks: list) -> list[BaseException]:
    """让所有任务尽量在同一时刻冲进去，返回过程中抛出的异常。"""
    barrier = threading.Barrier(len(tasks))
    errors: list[BaseException] = []

    def wrap(fn):
        def inner() -> None:
            try:
                barrier.wait()
                fn()
            except BaseException as exc:  # noqa: BLE001 — 测试要收集全部异常
                errors.append(exc)

        return inner

    threads = [threading.Thread(target=wrap(fn)) for fn in tasks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


def _request(**overrides) -> InteractionRequest:
    payload = {
        "request_id": "req_conc",
        "tenant_id": "tenant_a",
        "principal_id": "user_a",
        "learning_project_id": "proj_a",
        "node_id": "intake_goal",
        "user_input": "x",
    }
    payload.update(overrides)
    return InteractionRequest(**payload)


# --------------------------------------------------------------- 审计链


@pytest.mark.invariant
def test_audit_appends_stay_chained_under_concurrency(tmp_path, racy_scheduling):
    """并发追加审计必须保持成链。

    改前实测：30 轮里 **24 轮断链**，`seqs = [1, 1]` —— 两条记录拿到同一个
    序号与同一个前序哈希，第二条当场断链。
    修法是让「分配序号 → 取前序哈希 → 算本记录哈希 → 更新状态 → 写文件」
    落在同一个临界区里。
    """
    sink = AuditSink(tmp_path)

    errors = _run_concurrently(
        [
            (lambda i=i: sink.append("concurrent", {"i": i}, risk=RiskLevel.LOW))
            for i in range(4)
        ]
    )
    assert not errors, errors

    report = sink.verify_chain_report()
    assert report.ok is True, report.describe()
    seqs = [record["seq"] for record in sink.read_all()]
    assert seqs == list(range(1, 5)), f"序号必须连续不重复，实际 {seqs}"


@pytest.mark.invariant
def test_audit_duplicate_seq_is_what_broke_the_chain(tmp_path):
    """把断链的成因固定下来：**重复序号**，而不是别的。

    这条是上面那条的"解释"：如果哪天有人把并发测试删了，这条仍然说明
    「序号唯一且递增」是链的前提，且 `_build_record` 里那句
    `self._seq + 1` 必须在互斥保护下执行。
    """
    sink = AuditSink(tmp_path, filename="manual.jsonl")
    sink.append("a", {"i": 0}, risk=RiskLevel.LOW)
    record = sink.read_all()[0]

    # 手工伪造一条同序号记录（模拟无锁并发下的第二个写入者）
    forgery = dict(record)
    forgery["event_id"] = "aud_forged"
    sink.path.write_text(
        "\n".join(
            __import__("json").dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for r in (record, forgery)
        )
        + "\n",
        encoding="utf-8",
    )
    report = sink.verify_chain_report()
    assert report.ok is False
    assert report.problem is ChainProblem.BROKEN_LINK
    assert report.bad_index == 1


# --------------------------------------------------------------- 幂等占用


@pytest.mark.invariant
def test_same_key_concurrent_request_executes_only_once(
    platform, racy_scheduling, monkeypatch
):
    """同键并发：handler 必须**只执行一次**，而不是各执行一遍。

    改前实测（把 handler 做 2ms I/O 让临界区变宽）：能抓到
    `handler_executions = 2`、且两次都返回 `idempotent_replay=false`。
    成因是"先查缓存 → 执行 → 再写缓存"，中间隔着整个执行流程。
    """
    executions = {"n": 0}
    original = rt._HANDLERS["intake_goal"]

    def counting_handler(invoker, request, ctx):
        executions["n"] += 1
        # 真实的 handler 会做 I/O（检索、模型调用），临界区本来就这么宽。
        time.sleep(0.002)
        return original(invoker, request, ctx)

    monkeypatch.setitem(rt._HANDLERS, "intake_goal", counting_handler)

    results: list = []
    lock = threading.Lock()

    def run() -> None:
        result = platform.runtime.run(_request(idempotency_key="k1"))
        with lock:
            results.append(result)

    errors = _run_concurrently([run, run])
    assert not errors, errors

    assert executions["n"] == 1, f"handler 被执行了 {executions['n']} 次，期望 1"
    # 恰好一个真执行、一个重放。
    replays = sorted(r.output.get("idempotent_replay", False) for r in results)
    assert replays == [False, True], replays


@pytest.mark.invariant
def test_idempotency_key_is_scoped_per_tenant(platform):
    """同一把键在不同租户下互不干扰。

    改前：字典只以客户端字符串为键，租户 A 用 `shared` 成功后，
    租户 B 用同名键会收到 `IDEMPOTENCY_VIOLATION` ——
    一个租户能"占住"另一个租户的键，属于跨租户可用性干扰。
    """
    first = platform.runtime.run(_request(tenant_id="tenant_A", idempotency_key="shared"))
    second = platform.runtime.run(_request(tenant_id="tenant_B", idempotency_key="shared"))

    assert first.status == "ok", first.error
    assert second.status == "ok", second.error
    assert second.output.get("idempotent_replay") is not True, (
        "不同租户的同名键是两回事，不该命中对方的缓存"
    )


@pytest.mark.invariant
def test_same_tenant_same_key_still_deduplicates(platform):
    """按租户划分之后，同租户内的幂等仍然生效 —— 别把作用域改宽了。"""
    platform.runtime.run(_request(tenant_id="tenant_A", idempotency_key="shared"))
    again = platform.runtime.run(_request(tenant_id="tenant_A", idempotency_key="shared"))
    assert again.output.get("idempotent_replay") is True


# --------------------------------------------------------------- 处理中状态


@pytest.mark.invariant
def test_in_flight_same_key_reports_in_progress_and_is_retryable(
    platform, monkeypatch
):
    """执行还没结束就来同键请求 → 明确的「处理中 + 可重试」。

    必须与 `IDEMPOTENCY_VIOLATION` 区分：那是"钥匙用错了"，这是"等一下再来"。
    混为一谈会让客户端把并发重试当成参数冲突，从而放弃一个本来会成功的请求。
    """
    monkeypatch.setattr(rt.InteractionRuntime, "IDEMPOTENCY_WAIT_SECONDS", 0.2)

    entered = threading.Event()
    release = threading.Event()
    original = rt._HANDLERS["intake_goal"]

    def blocking_handler(invoker, request, ctx):
        entered.set()
        release.wait(timeout=5)
        return original(invoker, request, ctx)

    monkeypatch.setitem(rt._HANDLERS, "intake_goal", blocking_handler)

    holder_done: list = []

    def holder() -> None:
        holder_done.append(platform.runtime.run(_request(idempotency_key="k1")))

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert entered.wait(timeout=5), "第一个请求没有进入执行"
        blocked = platform.runtime.run(_request(idempotency_key="k1"))
    finally:
        release.set()
        thread.join(timeout=5)

    assert blocked.status == "failed", blocked.error
    assert blocked.error is not None
    assert blocked.error["code"] == str(ErrorCode.IDEMPOTENCY_IN_PROGRESS)
    assert blocked.error["retryable"] is True, (
        "它是可重试的：占用者一完成，同样的请求就会拿到重放结果"
    )
    assert holder_done and holder_done[0].status == "ok"


@pytest.mark.invariant
def test_failed_request_releases_the_claim(platform, monkeypatch):
    """失败/拒绝不缓存，且必须释放占用，让**同样的**重试真的再执行一次。

    缓存一个"被拒绝"的结果会让后续重试永远拿到旧拒绝；
    而只释放不够、必须有明确状态 —— 否则等待者会干等到超时。

    ⚠️ 重试必须是**同一把钥匙开同一扇门**，也就是请求内容完全相同。
    这个用例原先让第二次换一个 `node_id` 来表达"重试"，而那是"同一把钥匙开两扇门"，
    按契约一律 `IDEMPOTENCY_VIOLATION`，与"占用有没有被释放"根本是两件事。
    它当时能通过，靠的是 `released` 分支漏掉了指纹比较（第八轮修复）。

    所以这里改成让第一次因**外部原因**失败（handler 抛可重试错误），
    两次请求内容完全一致 —— 这才真正测到"失败不留占用"。
    """
    attempts = {"n": 0}
    original = rt._HANDLERS["intake_goal"]

    def flaky(invoker, request, ctx):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise PlatformError(
                code=ErrorCode.ILLEGAL_STATE_TRANSITION,
                message="第一次执行的瞬时故障",
                retryable=True,
            )
        return original(invoker, request, ctx)

    monkeypatch.setitem(rt._HANDLERS, "intake_goal", flaky)

    failed = platform.runtime.run(_request(idempotency_key="k1"))
    assert failed.status == "failed", failed.error

    retried = platform.runtime.run(_request(idempotency_key="k1"))
    assert retried.status == "ok", retried.error
    assert retried.output.get("idempotent_replay") is not True
    assert attempts["n"] == 2, "重试必须真的再执行一次：失败没有被当成结果缓存下来"


# --------------------------------------------------------------- 预算账本


@pytest.mark.invariant
def test_concurrent_child_account_creation_leaves_no_residue(racy_scheduling):
    """并发创建同一个子账户：**失败不留部分状态**。

    改前实测：第二个线程在父账户**已经预留成功之后**才撞上「账户已存在」
    并抛错 —— 那次预留既没被消费也没被释放，正好违反
    `BudgetLedger` 自己文档里承诺的那条不变量。
    """
    ledger = BudgetLedger()
    ledger.open_account("parent", limits={CURRENCY: 1000})

    def grant() -> None:
        try:
            ledger.grant_to_child("parent", "child", {CURRENCY: 100})
        except PlatformError as exc:
            # 「子账户已存在」是合法结果：并发的另一方已经建好了。
            assert exc.code is ErrorCode.BUDGET_TREE_INVALID

    errors = _run_concurrently([grant, grant])
    assert not errors, errors

    parent = ledger.account("parent")
    assert parent.reserved.get(CURRENCY, 0) == 100, (
        f"父账户预留应恰好 100，实际 {parent.reserved.get(CURRENCY, 0)} —— 多出来的就是泄漏"
    )
    assert ledger.account("child").limits.get(CURRENCY) == 100


@pytest.mark.invariant
def test_concurrent_budget_tree_creation_is_atomic(platform, racy_scheduling):
    """并发建立三级账户树：不得抛「账户已存在」，也不得留下残留预留。

    这条测的是**跨方法**原子性 —— 反向验证时才暴露出来的一层：

    给账本的每个变动方法都加锁之后，并发仍然稳定抛 `BUDGET_TREE_INVALID`。
    因为 `_ensure_budget_tree` 是 `if 不存在: 创建`（check-then-act），
    两个线程会**同时通过检查**，第二个在创建时撞车，而且是在父账户已经
    授予额度**之后**才撞的 —— 那次授予就挂在那里没人回收。

    **单个方法原子 ≠ 一组方法原子。** 修法是 `BudgetLedger.atomic()`
    把整段「检查 + 创建」关进同一个临界区。
    """
    results: list = []
    collect_lock = threading.Lock()

    def run() -> None:
        result = platform.runtime.run(_request())
        with collect_lock:
            results.append(result)

    errors = _run_concurrently([run, run, run])
    assert not errors, errors
    assert all(r.status == "ok" for r in results), [r.error for r in results]

    # 三级账户都只应存在一份，额度也应是**单份**授予。
    assert platform.ledger.account("acct_tenant_tenant_a").limits[CURRENCY] == 1_000_000_000
    assert platform.ledger.account("acct_project_proj_a").limits[CURRENCY] == 100_000_000

    # 父账户上的授予预留也必须恰好一份。
    # ⚠️ 授予是**按维度**拆成多条预留的（5 个维度 → 5 条），不是一条，
    # 所以按「同一账户 + 同一维度」的金额合计来判断，而不是数条数 ——
    # 数条数会把"正常"误判成异常（这条断言我第一版就写错了）。
    tenant_grants = [
        r
        for r in platform.ledger.grant_reservations()
        if r.account_id == "acct_tenant_tenant_a" and r.dimension == CURRENCY
    ]
    assert [r.amount for r in tenant_grants] == [100_000_000], (
        f"授予预留异常：{[r.amount for r in tenant_grants]} —— 重复授予或残留会在这里露出来"
    )


@pytest.mark.invariant
def test_concurrent_reservations_never_oversubscribe(racy_scheduling):
    """并发预留不得超发：可用额度是跨字段不变量，读-改-写必须原子。

    两个设计要点，都很容易写错：

    1. 额度（50）必须**小于**总请求量（10×10=100），否则必然 5 个成功、
       5 个失败；把额度设成刚好够用，无论有没有锁都会"全部成功" ——
       那种测试没有鉴别力，只是看起来很努力。
    2. **不能把 `reserve()` 放进取结果的锁里**：那会把调用彻底串行化，
       变成一条披着并发外衣的顺序测试。锁只保护收集结果的列表。
    """
    ledger = BudgetLedger()
    ledger.open_account("acct", limits={CURRENCY: 50})
    taken: list = []
    collect_lock = threading.Lock()

    def reserve() -> None:
        try:
            reservation = ledger.reserve("acct", CURRENCY, 10)
        except PlatformError:
            return
        with collect_lock:  # 只保护列表，不保护被测量的调用
            taken.append(reservation)

    errors = _run_concurrently([reserve] * 10)
    assert not errors, errors
    assert len(taken) == 5, f"应恰好 5 个成功（50/10），实际 {len(taken)}"
    assert ledger.account("acct").reserved.get(CURRENCY) == 50

