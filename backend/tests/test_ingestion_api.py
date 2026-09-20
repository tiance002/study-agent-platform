"""上传 API 与摄取 worker 的端到端测试（任务 13）。

这个文件守四件事，每一件都对应一个"看起来正常但实际错位"的失败模式：

1. **上传是一次 durable 登记**，不是同步处理：202、幂等可重放、
   超限在**入队之前**拒绝（否则用户要等一个注定失败的任务跑完才收到错误）。
2. **切块只发生在 worker 里**。写在请求里的话，一次 1 MiB 的解析会占住
   web worker，而客户端超时重试又压一份进来 —— 重试本该是安全的。
3. **状态接口只回报状态**，不回报原文（它会进客户端可控的轮询）。
4. **`claim_next` 没有 HTTP 可达路径**。它是全项目唯一没有身份参数的仓储方法
   （原因见 `knowledge/ports.py`），所以"没有端点能调用它"必须由机械测试守着，
   而不是靠 review —— review 抓不到"顺手加了个调试端点"。

⚠️ worker 与 API 必须跑在**同一个平台实例**上。用两个实例的话，
"API 登记的任务"与"worker 认领的任务"是两份互不相干的内存状态，
测试会绿得毫无意义。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from app.core.errors import ERROR_PAYLOAD_KEYS
from app.identity.models import Principal
from app.knowledge.models import MAX_DOCUMENT_BYTES
from app.main import DEMO_PRINCIPAL, DEMO_TENANT
from app.workers.ingestion import Outcome, main, run_once

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

ORIGIN = {"Origin": "http://testserver"}

CONTENT_PATH = "/projects/{project}/sources/{source}/content"
JOB_PATH = "/projects/{project}/ingestion-jobs/{job}"

SAMPLE = "# 事务\n\n提交成功。\n\n## 回滚\n\n失败时回滚。"


def _keyed(key: str | None = None) -> dict:
    """写端点必须携带 `Idempotency-Key`。

    缺省每次唯一：**同键同内容会命中重放缓存**，测不到真实执行。
    需要测重放时显式传同一个 key。
    """
    return {**ORIGIN, "Idempotency-Key": key or "ing-" + uuid.uuid4().hex}


def _content_path(project_id: str, source_id: str) -> str:
    return CONTENT_PATH.format(project=project_id, source=source_id)


def _job_path(project_id: str, job_id: str) -> str:
    return JOB_PATH.format(project=project_id, job=job_id)


@pytest.fixture
def learner(cookie_project, platform):
    """已登录用户 + 一个已创建的项目 + 一份已登记的资料。

    返回 `(client, platform, project_id, source_id)`。身份走完整认证路径
    （签发邀请 → 兑换 cookie 会话），不绕过认证 —— 绕过就等于用另一种形式
    把"客户端自报身份"放回来。登录舞蹈本身在 `conftest.cookie_project` 里，
    与检索测试共用一份，避免两处各抄一遍再各自分叉。
    """
    client, project_id = cookie_project
    source = client.post(
        f"/projects/{project_id}/sources",
        json={"display_name": "事务讲义"},
        headers=_keyed(),
    )
    assert source.status_code == 201
    return client, platform, project_id, source.json()["source_id"]


def _upload(
    client, project_id: str, source_id: str, *, key: str | None = None, **overrides
):
    body = {
        "title": "事务讲义",
        "content": SAMPLE,
        "media_type": "text/markdown",
    }
    body.update(overrides)
    return client.post(_content_path(project_id, source_id), json=body, headers=_keyed(key))


def _principal() -> Principal:
    """读接口需要身份对象。这不是"自报身份"—— 它只用于直接调用仓储，
    与 HTTP 路径无关（HTTP 路径的身份一律来自会话）。"""
    return Principal(principal_id=DEMO_PRINCIPAL, tenant_id=DEMO_TENANT)


# ---------------------------------------------------------------- 上传端点


@pytest.mark.invariant
def test_upload_enqueues_durably_and_never_returns_content(learner):
    """202 + `queued`：登记已完成，处理还没开始。

    正文（`content`）不进响应：列表/状态响应里塞原文是客户端可控的
    响应体放大入口，原文按需通过引用回读。
    """
    client, platform, project_id, source_id = learner

    response = _upload(client, project_id, source_id)

    assert response.status_code == 202
    payload = response.json()
    assert payload["job"]["status"] == "queued"
    assert payload["job"]["attempt_count"] == 0
    assert payload["document"]["parser_version"] == "text/v1"
    assert payload["document"]["version"] == 1
    assert "content" not in payload["document"], "响应体不得携带原文"
    assert "content" not in payload["job"]
    # 上传**不**产出片段：切块是 worker 的活。
    assert platform.ingestion.stored_chunks(_principal(), project_id) == ()


@pytest.mark.invariant
def test_same_key_and_body_replays_the_cached_response(learner):
    """超时重试必须幂等：同键同内容返回同一份响应，并标注这是一次重放。

    重放必须**可观测**（`X-Idempotent-Replay`），否则客户端无法区分
    "服务端真的处理了两次"和"我收到了缓存"。
    """
    client, platform, project_id, source_id = learner

    first = _upload(client, project_id, source_id, key="upload-1")
    replay = _upload(client, project_id, source_id, key="upload-1")

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json() == first.json()
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert first.headers.get("X-Idempotent-Replay") is None
    # 重放**没有**再写一版原文：版本号仍是 1（重放若真执行了，这里会是 2）。
    assert replay.json()["document"]["version"] == 1


@pytest.mark.invariant
def test_same_key_with_different_body_is_rejected(learner):
    """同一把钥匙换内容 = 客户端 bug，必须报错而不是"以先到者为准"。

    静默沿用先到的结果会让客户端以为第二次上传生效了 —— 而库里是第一次的内容。
    """
    client, _, project_id, source_id = learner

    assert _upload(client, project_id, source_id, key="dup-1").status_code == 202
    conflicting = _upload(
        client, project_id, source_id, key="dup-1", content="# 另一份讲义\n\n内容不同。"
    )

    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "IDEMPOTENCY_VIOLATION"


@pytest.mark.invariant
def test_missing_idempotency_key_is_rejected_before_anything_happens(learner):
    """缺 `Idempotency-Key` 一律拒绝（400）—— 守卫不能靠"不发头"绕过。"""
    client, platform, project_id, source_id = learner

    response = client.post(
        _content_path(project_id, source_id),
        json={"title": "讲义", "content": SAMPLE, "media_type": "text/markdown"},
        headers=ORIGIN,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert platform.ingestion.claim_next(worker_id="probe", lease_seconds=60) is None


@pytest.mark.invariant
def test_oversized_ascii_content_is_rejected_before_enqueue(learner):
    """超过 1 MiB 的输入在**入队之前**拒绝，队列里必须一条任务都没有。"""
    client, platform, project_id, source_id = learner

    response = _upload(client, project_id, source_id, content="x" * (MAX_DOCUMENT_BYTES + 1))

    assert response.status_code == 422
    assert platform.ingestion.claim_next(worker_id="probe", lease_seconds=60) is None


@pytest.mark.invariant
def test_oversized_chinese_content_is_422_not_500(learner):
    """中文一字三字节：40 万字符是 1.2 MiB。

    只按字符设限的话，这个请求能过应用层校验、却在数据库 CHECK 上炸成 500 ——
    而错误原因是"我们量错了单位"，用户完全无法从中知道该改什么。
    """
    client, platform, project_id, source_id = learner
    content = "中" * (MAX_DOCUMENT_BYTES // 3 + 1)
    assert len(content) < MAX_DOCUMENT_BYTES, "前提：字符数在粗筛上限之内"

    response = _upload(client, project_id, source_id, content=content)

    assert response.status_code == 422
    assert platform.ingestion.claim_next(worker_id="probe", lease_seconds=60) is None


@pytest.mark.invariant
def test_lone_surrogate_is_rejected_as_422_not_500(learner):
    """JSON 允许 `\\ud800` 这样的孤立代理项转义。

    解出来是 Python 能持有、却编码不成 UTF-8 的字符串 —— 不拦的话
    `encode` 抛的 `UnicodeEncodeError` 不是 ValueError，会以 500 结束。
    """
    client, _, project_id, source_id = learner

    response = client.post(
        _content_path(project_id, source_id),
        content=(
            '{"title":"讲义","media_type":"text/markdown","content":"坏\\ud800内容"}'
        ).encode(),
        headers={**_keyed(), "Content-Type": "application/json"},
    )

    assert response.status_code == 422


@pytest.mark.invariant
def test_unsupported_media_type_is_rejected_at_request_boundary(learner):
    client, _, project_id, source_id = learner

    response = _upload(client, project_id, source_id, media_type="application/pdf")

    assert response.status_code == 422


@pytest.mark.invariant
def test_invalid_request_body_uses_the_canonical_error_shape(learner):
    """422 也必须用**规范错误体**，并且不回显请求输入。

    FastAPI 的默认实现返回 `{"detail": [...]}`：形状与本项目其它错误响应不同
    （客户端得为它写第二套解析），而且把 `input` 原样回显 —— 遇到无法编码成
    UTF-8 的输入（孤立代理项）时，编码响应本身抛异常，422 会变成 500。
    """
    client, _, project_id, source_id = learner

    response = _upload(client, project_id, source_id, media_type="application/pdf")

    payload = response.json()
    assert set(payload) == set(ERROR_PAYLOAD_KEYS)
    assert payload["code"] == "PARAMS_INVALID"
    assert payload["request_id"], "422 也要带 request_id，否则排障时对不上日志"
    assert "application/pdf" not in response.text, "错误体不得回显请求输入"


@pytest.mark.invariant
def test_unknown_field_is_rejected(learner):
    """请求模型 `extra="forbid"`：静默忽略未知字段会让客户端以为它生效了。"""
    client, _, project_id, source_id = learner

    response = _upload(client, project_id, source_id, derived_from=["doc_other"])

    assert response.status_code == 422


@pytest.mark.invariant
def test_source_from_another_project_cannot_receive_content(learner):
    """拿别的项目的 source_id 上传，与"这个 source 不存在"同码同话术（404）。

    区分原因等于提供一个存在性探针 —— 探测者据此能数出别的项目有哪些资料。
    """
    client, _, project_id, source_id = learner
    other = client.post("/projects", json={"name": "另一个项目"}, headers=_keyed())
    other_project = other.json()["project_id"]

    response = _upload(client, other_project, source_id)

    assert response.status_code == 404


@pytest.mark.invariant
def test_status_endpoint_reports_only_stable_fields(learner):
    client, _, project_id, source_id = learner
    upload = _upload(client, project_id, source_id)
    job_id = upload.json()["job"]["job_id"]

    response = client.get(_job_path(project_id, job_id))

    assert response.status_code == 200
    assert set(response.json()) == {
        "job_id",
        "source_id",
        "document_id",
        "status",
        "attempt_count",
        "error_code",
        "error_detail",
        "created_at",
        "updated_at",
    }
    assert response.json()["status"] == "queued"


@pytest.mark.invariant
def test_job_of_another_project_is_not_readable(learner):
    client, _, project_id, source_id = learner
    job_id = _upload(client, project_id, source_id).json()["job"]["job_id"]
    other_project = client.post(
        "/projects", json={"name": "另一个项目"}, headers=_keyed()
    ).json()["project_id"]

    assert client.get(_job_path(other_project, job_id)).status_code == 404


# ------------------------------------------------------------------ worker


@pytest.mark.invariant
def test_worker_turns_a_queued_job_into_verifiable_chunks(learner):
    """worker 跑完之后：任务成功、片段落库、且每个片段都能从原文精确回读。"""
    client, platform, project_id, source_id = learner
    job_id = _upload(client, project_id, source_id).json()["job"]["job_id"]

    outcome = run_once(platform, worker_id="wk-test")

    assert outcome == Outcome(kind="succeeded", job_id=job_id, chunk_count=2)
    assert client.get(_job_path(project_id, job_id)).json()["status"] == "succeeded"
    chunks = platform.ingestion.stored_chunks(_principal(), project_id)
    assert [chunk.heading_path for chunk in chunks] == [("事务",), ("事务", "回滚")]
    for chunk in chunks:
        assert SAMPLE[chunk.span_start : chunk.span_end] == chunk.content
        assert chunk.parser_version == "structure/v1"


@pytest.mark.invariant
def test_worker_is_idle_when_the_queue_is_empty(learner):
    _, platform, _, _ = learner

    assert run_once(platform, worker_id="wk-test") == Outcome(kind="idle")


@pytest.mark.invariant
def test_terminal_job_is_never_reclaimed(learner):
    """成功之后不再被认领 —— 否则每次轮询都会重跑一遍已完成的活。"""
    client, platform, project_id, source_id = learner
    _upload(client, project_id, source_id)

    assert run_once(platform, worker_id="wk-a").kind == "succeeded"
    assert run_once(platform, worker_id="wk-b") == Outcome(kind="idle")
    # 片段没有被追加第二套：`chunk_index` 仍然连续。
    chunks = platform.ingestion.stored_chunks(_principal(), project_id)
    assert [chunk.chunk_index for chunk in chunks] == [0, 1]


@pytest.mark.invariant
def test_crashed_worker_lease_expires_and_the_job_comes_back(learner):
    """崩溃残留（processing + 租约过期）可以被下一个 worker 接管。

    这条是"至少一次投递"的兜底：worker 拿到任务后被杀掉，任务必须还能回到队列 ——
    否则它会永远停在 processing，而用户看到的是"一直处理中"。
    """
    client, platform, project_id, source_id = learner
    job_id = _upload(client, project_id, source_id).json()["job"]["job_id"]

    crashed = platform.ingestion.claim_next(worker_id="wk-crashed", lease_seconds=0)
    assert crashed is not None and crashed.job_id == job_id
    assert client.get(_job_path(project_id, job_id)).json()["status"] == "processing"

    recovered = run_once(platform, worker_id="wk-next")

    assert recovered == Outcome(kind="succeeded", job_id=job_id, chunk_count=2)
    assert client.get(_job_path(project_id, job_id)).json()["attempt_count"] == 2


class _BrokenProcessor:
    """切块器算错偏移的替身：`StoredChunk` 会因长度不符而拒绝构造。

    异常文本里刻意放一个"秘密"标记，用来验证它**没有**被写进
    `error_detail` —— 那段文本会通过状态接口原样返回给用户。
    """

    def parse(self, document):  # noqa: ANN001, ARG002 — 替身只需签名相容
        raise ValueError("span 与内容长度不一致 secr3t-internal-path")


class _ExplodingProcessor:
    """不可判定失败的替身：模拟连接池耗尽这类"我们不知道该不该重试"的错误。"""

    def parse(self, document):  # noqa: ANN001, ARG002
        raise RuntimeError("连接池耗尽")


@pytest.mark.invariant
def test_deterministic_failure_becomes_terminal_with_a_safe_detail(learner):
    """确定性失败必须进终态，而且描述里**不得**出现原始异常文本。

    不进终态的话，每次租约到期都会重跑一遍注定失败的活 —— 队列看起来
    一直"有进度"，而实际上什么也不会发生。
    """
    client, platform, project_id, source_id = learner
    job_id = _upload(client, project_id, source_id).json()["job"]["job_id"]

    outcome = run_once(platform, worker_id="wk-test", processor=_BrokenProcessor())  # type: ignore[arg-type]

    assert outcome.kind == "failed"
    assert outcome.error_code == "INTERNAL_CONSISTENCY_ERROR"
    state = client.get(_job_path(project_id, job_id)).json()
    assert state["status"] == "failed"
    assert "secr3t" not in state["error_detail"], "原始异常文本泄露到了状态接口"
    assert state["error_detail"], "失败必须带可读描述，否则用户只知道'失败了'"
    # 终态不再被认领。
    assert run_once(platform, worker_id="wk-next") == Outcome(kind="idle")


@pytest.mark.invariant
def test_unexpected_failure_leaves_the_job_reclaimable(learner):
    """不可判定失败**不写终态**：我们不知道业务有没有提交，替它下结论会盖掉
    另一个 worker 正在做的事。任务停在 `processing`，只能靠租约到期回收。

    这里用 `lease_seconds=0` 代表"租约已到期" —— 真实的隔离是时间，而时间在测试里
    只能这样表达。注意这不是把租约调短以图省事：**它恰好证明恢复的入口是租约**，
    而不是"worker 自己宣告失败"。
    """
    client, platform, project_id, source_id = learner
    job_id = _upload(client, project_id, source_id).json()["job"]["job_id"]

    with pytest.raises(RuntimeError, match="连接池耗尽"):
        run_once(
            platform,
            worker_id="wk-test",
            lease_seconds=0,
            processor=_ExplodingProcessor(),  # type: ignore[arg-type]
        )

    state = client.get(_job_path(project_id, job_id)).json()
    assert state["status"] == "processing", "不可判定失败不得被写成终态"
    assert state["attempt_count"] == 1

    # 租约已过期 → 下一个 worker 能接管，并且真的把活干完。
    recovered = run_once(platform, worker_id="wk-next")
    assert recovered == Outcome(kind="succeeded", job_id=job_id, chunk_count=2)


class _ExplodingQueue:
    """认领本身就是坏掉的一道队列（模拟数据库不可用）。"""

    def claim_next(self, **_kwargs):  # noqa: ANN003 — 只需签名相容
        raise RuntimeError("数据库连接断了")


@pytest.mark.invariant
def test_worker_command_exits_nonzero_on_an_unexpected_failure(platform):
    """非零退出码是给监管进程的信号：它据此重启或告警。

    崩溃了却退出 0 会让监管进程以为"活都干完了" —— 队列里积压的任务
    会一直没人动，而监控全绿。

    这里也顺带证明"租约"是唯一的恢复手段：任务没有被写终态，
    所以恢复的前提是租约到期，而不是进程自己宣告成功。
    """

    class _Shell:
        ingestion = _ExplodingQueue()

    assert main(["--once", "--worker-id", "wk-test"], platform_factory=lambda: _Shell()) == 1


@pytest.mark.invariant
def test_worker_command_once_processes_exactly_one_job(learner):
    """`--once` 处理一个就退出，第二次调用返回 0（队列已空）。"""
    client, platform, project_id, source_id = learner
    _upload(client, project_id, source_id)
    _upload(client, project_id, source_id, content="# 第二份\n\n另一段内容。")

    factory = lambda: platform  # noqa: E731 — 单表达式工厂，命名反而更绕
    assert main(["--once", "--worker-id", "wk-test"], platform_factory=factory) == 0
    assert main(["--once", "--worker-id", "wk-test"], platform_factory=factory) == 0
    assert main(["--once", "--worker-id", "wk-test"], platform_factory=factory) == 0

    chunks = platform.ingestion.stored_chunks(_principal(), project_id)
    # 两份原文各自成功：一共两个任务，恰好两批片段。
    assert len({chunk.document_id for chunk in chunks}) == 2


# ------------------------------------------------- 机械守卫：入口的唯一性


def _sources_containing(token: str) -> set[str]:
    """全量扫描 `app/` 下的源码，返回包含该 token 的**相对路径集合**。

    静态扫描而非运行时反射：它不依赖模块被导入，也不受动态派发影响 ——
    而这一节要守的恰恰是"某段代码没有写在那里"。
    """
    found: set[str] = set()
    for path in sorted(APP_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if token in path.read_text(encoding="utf-8"):
            found.add(path.relative_to(APP_ROOT).as_posix())
    return found


def _route_pairs(app) -> set[tuple[str, str]]:
    """从 OpenAPI 文档里取全部 (METHOD, 路径)。

    **不遍历 `app.routes`**：这个 FastAPI 版本把 `include_router` 的结果包成
    `_IncludedRouter`，它的 `path` 是 `None` 且**不暴露** `.routes`
    （只有 `original_router` 之类的匹配内部件）。按 `app.routes` 递归收集会得到
    空集 —— 而空集会让下面那条"端点恰好是这两个"的断言**永远通过**，
    包括在一个真的多了个调试端点的时候。

    用 OpenAPI schema 还多一层好处：它是**客户端看到的**那份契约，
    断言对着它写，就不会出现"测试看到了路由、客户端看不到"的分家。
    """
    schema = app.openapi()
    return {
        (method.upper(), path)
        for path, operations in schema.get("paths", {}).items()
        for method in operations
    }


@pytest.mark.invariant
def test_claim_next_has_no_http_path_to_it():
    """`claim_next` 是唯一没有身份参数的仓储方法，因此必须不可从 HTTP 到达。

    用"出现位置的闭集"守，而不是靠 review：新增一个调用点会让测试失败，
    于是"再加一个入口"必须是一次有意识的决定。
    """
    assert _sources_containing("claim_next") == {
        "knowledge/ports.py",         # 协议声明
        "knowledge/memory_store.py",  # 内存实现（开发适配器）
        "db/ingestion_store.py",      # PostgreSQL 实现
        "workers/ingestion.py",       # 唯一的生产调用点
    }


@pytest.mark.invariant
def test_api_layer_never_chunks_and_never_claims():
    """接入层不得碰切块器，也不得碰队列认领。

    切块在请求里 = 重活占住 web worker，而超时重试本该是安全的；
    认领在请求里 = 客户端能直接驱动后台队列（而认领没有身份参数，
    等于开了一条不经过身份校验的路径）。
    """
    assert not {
        name for name in _sources_containing("DocumentProcessor") if name.startswith("api/")
    }


@pytest.mark.invariant
def test_ingestion_surface_is_exactly_two_endpoints(client):
    """摄取相关的 HTTP 面就是一个上传 + 一个状态查询。多一个都要先过这里。"""
    ingestion = {
        item
        for item in _route_pairs(client.app)
        if "ingestion-jobs" in item[1] or "/content" in item[1]
    }
    assert ingestion == {
        ("POST", "/projects/{project_id}/sources/{source_id}/content"),
        ("GET", "/projects/{project_id}/ingestion-jobs/{job_id}"),
    }
