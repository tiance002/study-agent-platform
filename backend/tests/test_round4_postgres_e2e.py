"""第 4 轮退出门：PostgreSQL 上的「摄取 → 检索 → 引用回读」整条链路（任务 14）。

计划 Step 6 把退出门写成一条链：

    cookie 登录 → 建项目 → 登记资料 → 上传 Markdown（API 返回 queued）
    → 新 worker 认领并完成 → 重启整个 PlatformState → 中文查询命中期望片段
    → 精确 span/hash 校验原文 → 另一个项目与租户既搜不到也读不到
    → 强制 worker 崩溃留下 processing 租约 → 过期租约被回收一次
    → 重复完成不产生重复片段

这里把它拆成**三个**用例：三件性质各自独立成立，合起来才是那条链。

1. `test_ingestion_survives_a_full_platform_restart_and_stays_citable`
   持久化 + 可核验引用。失败模式是"内存里对、重启后就没了"，
   以及"引用看起来有据、实际指向别处"。
2. `test_another_project_and_another_tenant_cannot_retrieve_or_read`
   隔离真的在挡。失败模式是"越权但看起来完全正常"—— 内容读得通，只是不属于你。
3. `test_crashed_lease_is_reclaimed_once_and_repeated_completion_is_idempotent`
   崩溃可恢复且不重复。失败模式是"重试留下两套片段"或"任务被永久卡住"。

合成一个两百行用例的代价很具体：失败时只知道"某一步挂了"，
而这三类失败的排查方向完全不同（存储 / 权限 / 并发）。

⚠️ 与前几轮一样，这里**不用内存适配器**：退出门要证明的是
「换一个进程、换一份本地状态，事实还在」。内存版永远能通过这一条 ——
它压根没有"重启"这回事。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pg_support
import psycopg
import pytest
from app.core.hashing import content_hash
from app.deployment import DeploymentSettings
from app.identity.models import Principal
from app.identity.ports import SystemContext
from app.knowledge.retrieval import RANKING_VERSION
from app.main import PlatformState, build_platform, create_app
from app.workers.ingestion import run_once
from fastapi.testclient import TestClient

ORIGIN = "http://testserver"

#: 语料刻意包含四种结构：标题、段落、围栏代码块、表格。
#: 围栏代码块是必需的 —— 它的存在让"代码里的 `#` 会不会被当成标题"
#: 这件事故在退出门上被走一遍（切块器里 `_fence_marker` 守的就是它）。
DOCUMENT = (
    "# 数据库事务\n"
    "\n"
    "事务把多条写入打包成一个不可分割的执行单位。\n"
    "\n"
    "## 回滚\n"
    "\n"
    "失败时必须回滚，撤销已经写入的修改。\n"
    "\n"
    "```sql\n"
    "BEGIN;\n"
    "ROLLBACK;\n"
    "```\n"
    "\n"
    "| 隔离级别 | 脏读 |\n"
    "| --- | --- |\n"
    "| 读已提交 | 不可能 |\n"
)


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.invariant,
    # 整场会话跑在随机临时库上（`conftest.pg_database`，会话级 autouse）：
    # 本文件要排空跨租户队列（`_drain_queue`），在业务库上做这件事会终结
    # 用户的在途任务，而测试全绿看不出来。
    pytest.mark.skipif(
        not pg_support.reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"
    ),
]


# ------------------------------------------------------------------ 基建


@dataclass
class _Stage:
    """一次退出门用例的全部上下文。"""

    client: TestClient
    platform: PlatformState
    settings: DeploymentSettings
    tmp_path: Path
    suffix: str
    tenant_id: str
    principal_id: str
    cookie: str
    project_id: str
    source_id: str
    job_id: str

    @property
    def actor(self) -> Principal:
        """直接调用仓储时用的身份对象。

        这不是"客户端自报身份"：HTTP 路径的身份一律来自会话 cookie，
        这里的 `Principal` 只喂给不经 HTTP 的仓储调用（worker 与断言）。
        """
        return Principal(principal_id=self.principal_id, tenant_id=self.tenant_id)


def _headers(key: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Idempotency-Key": key}


def _drain_queue() -> None:
    """把遗留的未终态任务推进到终态，让本用例从空队列开始。

    为什么必须显式做：`claim_next` 是**跨租户**的系统级操作（worker 要先发现
    "哪个租户有活干"），拿回来的是全库最早的一条排队任务 —— 包括上一次运行、
    上一个用例留下的。用超级用户连接直接写终态，是因为这一步要绕过 RLS
    才能覆盖所有租户。

    ⚠️ 这一句会终结目标库里**所有**在途任务，所以先过 `require_test_database`：
    指向业务库时在**任何写入之前**失败，而不是先把用户的任务杀掉再报错。
    """
    dsn = pg_support.require_test_database(pg_support.migration_dsn())
    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute(
            "UPDATE ingestion_jobs"
            " SET status = 'failed', error_code = 'TEST_DRAIN',"
            "     error_detail = '测试前置：排空遗留任务',"
            "     lease_owner = NULL, lease_until = NULL"
            " WHERE status IN ('queued', 'processing')"
        )


def _expire_lease(job_id: str) -> None:
    """把租约改到过去 —— 等价于"等满了 300 秒"，但不用真的等。

    直接改数据库而不是 `sleep`：睡一秒会让测试变慢，睡满租约期（300 秒）
    则根本不可行；而租约过期的语义就是"`lease_until` 落在 `now()` 之前"，
    把那一列改掉是这句话的**逐字**实现，不是近似。
    """
    with psycopg.connect(pg_support.migration_dsn()) as conn, conn.transaction():
        updated = conn.execute(
            "UPDATE ingestion_jobs SET lease_until = now() - interval '1 second'"
            " WHERE job_id = %s",
            (job_id,),
        ).rowcount
    assert updated == 1, "租约没被改到 —— 后面那句'token 已过期'就没有意义"


def _seed_tenant(prefix: str, name: str) -> tuple[str, str]:
    """建租户与主体（运维动作，用超级用户写）。返回 `(tenant_id, principal_id)`。

    租户 id 每次运行唯一：PG 是跨运行持久的，固定 id 会让第二次运行撞上
    上一次留下的项目、成员与唯一约束，而报错指向上一次的数据。
    """
    suffix = uuid.uuid4().hex
    tenant_id, principal_id = f"t_{prefix}_{suffix}", f"u_{prefix}_{suffix}"
    with psycopg.connect(pg_support.migration_dsn()) as conn, conn.transaction():
        conn.execute(
            "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)", (tenant_id, name)
        )
        conn.execute(
            "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)",
            (principal_id, tenant_id),
        )
    return tenant_id, principal_id


def _login(platform: PlatformState, tenant_id: str, principal_id: str) -> tuple[TestClient, str]:
    """走完整认证路径拿一个 cookie 会话（签发邀请 → 兑换）。

    不绕过认证：绕过等于用另一种形式把"客户端自报身份"放回来。
    """
    token = "invite-r4-" + uuid.uuid4().hex
    now = platform.clock.now()
    platform.invitations.issue(
        SystemContext(tenant_id, "Round 4 exit gate"),
        invitation_id="inv_" + uuid.uuid4().hex[:8],
        token_hash="sha256:" + hashlib.sha256(token.encode()).hexdigest(),
        issued_by=principal_id,
        invitee_principal_id=principal_id,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )
    client = TestClient(create_app(platform=platform))
    exchanged = client.post("/auth/invitations/exchange", json={"token": token})
    assert exchanged.status_code == 200, exchanged.text
    return client, exchanged.cookies["study_session"]


def _stage(tmp_path: Path) -> _Stage:
    """登录 → 建项目 → 登记资料 → 上传 Markdown（停在 `queued`）。

    **不做**认领与切块：让每个用例自己决定要不要跑 worker、跑几次 ——
    这正是用例 1/2/3 分岔的地方。
    """
    suffix = uuid.uuid4().hex
    tenant_id, principal_id = _seed_tenant("r4", "Round 4 exit gate")
    settings = DeploymentSettings.load(
        {"STUDY_PLATFORM_PERSISTENCE": "postgres", "STUDY_PLATFORM_EXCHANGE_LIMIT": "100000"}
    )
    platform = build_platform(var_dir=tmp_path / "a", settings=settings)
    client, cookie = _login(platform, tenant_id, principal_id)

    project = client.post(
        "/projects",
        headers=_headers("r4-project-" + suffix),
        json={"name": "事务闭环", "goal": "理解数据库事务"},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["project_id"]

    source = client.post(
        f"/projects/{project_id}/sources",
        headers=_headers("r4-source-" + suffix),
        json={"display_name": "事务讲义"},
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["source_id"]

    # 排空必须在**上传之前**：排空之后再入队，队列里就只剩本用例这一条。
    _drain_queue()

    upload = client.post(
        f"/projects/{project_id}/sources/{source_id}/content",
        headers=_headers("r4-upload-" + suffix),
        json={"title": "事务讲义", "content": DOCUMENT, "media_type": "text/markdown"},
    )
    assert upload.status_code == 202, upload.text
    payload = upload.json()
    assert payload["job"]["status"] == "queued", "登记即返回：此刻还没有任何片段"
    assert payload["document"]["version"] == 1
    assert "content" not in payload["document"], "响应体不得携带原文"

    return _Stage(
        client=client,
        platform=platform,
        settings=settings,
        tmp_path=tmp_path,
        suffix=suffix,
        tenant_id=tenant_id,
        principal_id=principal_id,
        cookie=cookie,
        project_id=project_id,
        source_id=source_id,
        job_id=payload["job"]["job_id"],
    )


def _ingest_once(stage: _Stage, worker_id: str) -> int:
    """跑一个 worker 直到本用例的任务落定，返回片段数。"""
    outcome = run_once(stage.platform, worker_id=worker_id)
    assert outcome.kind == "succeeded", outcome.describe()
    assert outcome.job_id == stage.job_id, "认领到的不是本用例的任务（队列没排干净？）"
    return outcome.chunk_count


def _without_request_id(response) -> dict:
    """错误体去掉 `request_id` —— 它按设计每请求唯一，不参与"逐字相同"的比对。"""
    body = dict(response.json())
    body.pop("request_id", None)
    return body


# ------------------------------------------------- 一、重启后仍在，且引用可核验


def test_ingestion_survives_a_full_platform_restart_and_stays_citable(tmp_path):
    """上传 → worker 切块 → **整个平台重建** → 中文检索命中 → 引用回读原文。

    这条链里最关键的一步是"重建平台"：只断言"同一个进程里后面查得到"
    测不出持久化，因为内存适配器也能过。
    """
    stage = _stage(tmp_path)
    chunk_count = _ingest_once(stage, "r4-worker-" + stage.suffix)
    assert chunk_count >= 2, "标题 + 段落 + 代码块 + 表格至少要切出两块"

    # worker 完成后，状态接口从 202 时的 queued 变成 succeeded。
    job = stage.client.get(
        f"/projects/{stage.project_id}/ingestion-jobs/{stage.job_id}"
    ).json()
    assert job["status"] == "succeeded"
    assert job["attempt_count"] == 1

    # ------------------------------------------------ 重启：全新实例 + 全新本地目录
    restarted = build_platform(var_dir=tmp_path / "b", settings=stage.settings)
    client = TestClient(create_app(platform=restarted))
    client.cookies.set("study_session", stage.cookie)

    search = client.post(
        f"/projects/{stage.project_id}/knowledge/search",
        headers={"Origin": ORIGIN},
        json={"query": "回滚", "limit": 5},
    )
    assert search.status_code == 200, search.text
    payload = search.json()

    assert payload["ranking_version"] == RANKING_VERSION
    assert payload["retrieval_health"] == "clean", "检索按预期跑完"
    # 命中 ≠ 支持：没有冻结的核心结论标注集，证据状态必然是 insufficient。
    assert payload["evidence"]["state"] == "insufficient"
    hits = payload["hits"]
    assert hits, "重启后仍然应当搜得到"

    hit = hits[0]
    assert "回滚" in hit["content"]

    # ---- 精确 span/hash 校验原文：切片必须真的是原文的切片
    start, end = hit["span"]
    assert DOCUMENT[start:end] == hit["content"], "span 指回的位置与内容不一致"
    assert content_hash(hit["content"]) == hit["content_hash"]
    assert hit["citation"]["source_id"] == stage.source_id
    assert hit["citation"]["span"] == [start, end]
    assert hit["citation"]["content_hash"] == hit["content_hash"]
    assert hit["citation"]["parser_version"] == hit["parser_version"]

    # ---- 引用回读：拿到引用的人应当能独立取回同一段原文
    span = client.get(
        f"/projects/{stage.project_id}/sources/{stage.source_id}/span",
        params={"start": start, "end": end},
    )
    assert span.status_code == 200, span.text
    assert span.json()["content"] == hit["content"]
    assert span.json()["citation"] == hit["citation"]

    # ---- 围栏代码块整块保留：小写英文查询命中的是**大写 SQL 原文**。
    # 这同时证明两件事：代码块没有被 `#` 之类的行内字符切碎，
    # 也没有被归一化改写（原文只增结构元数据，不改字）。
    code = client.post(
        f"/projects/{stage.project_id}/knowledge/search",
        headers={"Origin": ORIGIN},
        json={"query": "rollback", "limit": 5},
    ).json()
    assert any(
        "BEGIN" in item["content"] and "ROLLBACK;" in item["content"] for item in code["hits"]
    ), "围栏代码块应当整块可检索"


# ------------------------------------------------------- 二、另一个项目与另一个租户


def test_another_project_and_another_tenant_cannot_retrieve_or_read(tmp_path):
    """片段已落库之后，别的项目与别的租户**既搜不到、也读不到**。

    注意这里问的不是"结果里有没有被标成越权"：候选根本不会离开数据库
    （作用域在 SQL / RLS 里收窄，02 号规格 §7），所以断言的对象是
    "结果集合为空"与"回读 404"。
    """
    stage = _stage(tmp_path)
    _ingest_once(stage, "r4-worker-" + stage.suffix)

    # 阳性对照：本项目内**确实**搜得到。
    # 没有这一条，"别的项目搜不到"在摄取悄悄产出零片段时也会通过 ——
    # 那样这条断言测的是"什么都没有"，而不是"隔离生效"。
    mine = stage.client.post(
        f"/projects/{stage.project_id}/knowledge/search",
        headers={"Origin": ORIGIN},
        json={"query": "回滚", "limit": 5},
    )
    assert mine.status_code == 200, mine.text
    assert mine.json()["hits"], "阳性对照失败：本项目内应当搜得到，否则下面的'搜不到'没有意义"

    # -------------------------------------------------- 同一个主体的另一个项目
    other_project = stage.client.post(
        "/projects",
        headers=_headers("r4-project2-" + stage.suffix),
        json={"name": "另一个项目"},
    ).json()["project_id"]
    assert other_project != stage.project_id

    empty = stage.client.post(
        f"/projects/{other_project}/knowledge/search",
        headers={"Origin": ORIGIN},
        json={"query": "回滚", "limit": 5},
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["hits"] == [], "别的项目里的片段不得成为候选"
    assert empty.json()["evidence"]["state"] == "insufficient"

    assert stage.client.get(
        f"/projects/{other_project}/sources/{stage.source_id}/span",
        params={"start": 0, "end": 5},
    ).status_code == 404
    assert stage.client.get(
        f"/projects/{other_project}/ingestion-jobs/{stage.job_id}"
    ).status_code == 404

    # ------------------------------------------------------------ 另一个租户
    other_tenant, other_principal = _seed_tenant("r4b", "Round 4 other tenant")
    outsider, outsider_cookie = _login(stage.platform, other_tenant, other_principal)
    assert outsider_cookie != stage.cookie

    denied = outsider.post(
        f"/projects/{stage.project_id}/knowledge/search",
        headers={"Origin": ORIGIN},
        json={"query": "回滚", "limit": 5},
    )
    assert denied.status_code == 404
    outsider_denied = outsider.get(
        f"/projects/{stage.project_id}/sources/{stage.source_id}/span",
        params={"start": 0, "end": 5},
    )
    assert outsider_denied.status_code == 404
    assert outsider.get(
        f"/projects/{stage.project_id}/ingestion-jobs/{stage.job_id}"
    ).status_code == 404
    assert outsider.get(f"/projects/{stage.project_id}/sources").status_code == 404

    # 拒绝的形状与"来源不存在"逐字相同 —— 同一个端点、不同主体、不同原因，
    # 客户端据以分支的字段必须一模一样。否则话术或状态码的差异本身就是
    # 一根存在性探针：探测者据此能数出别人的项目里有哪些资料。
    missing = stage.client.get(
        f"/projects/{stage.project_id}/sources/src_不存在/span",
        params={"start": 0, "end": 5},
    )
    assert missing.status_code == 404
    assert _without_request_id(outsider_denied) == _without_request_id(missing)
    assert outsider_denied.json()["request_id"] != missing.json()["request_id"], (
        "追踪 id 仍必须逐请求唯一 —— 否则上面那条'相同'是因为它压根没生成"
    )


# ------------------------------------------- 三、崩溃租约可回收、重复完成不重复


def test_crashed_lease_is_reclaimed_once_and_repeated_completion_is_idempotent(tmp_path):
    """worker 认领后崩溃 → 租约期内任务不被抢 → 租约过期后被**回收一次** →
    再次完成不产生重复片段。

    为什么"重复完成"必须单独测：worker 写完片段、还没来得及确认的那一瞬间
    进程被杀，消息会被重新投递。此时再来一次 `complete` 必须是**幂等成功** ——
    否则用户看到的是同一份资料搜出两遍，或者一个唯一键冲突的 500。
    """
    stage = _stage(tmp_path)

    # 认领后崩溃 = 认领成功但没人把结果写回去。这里直接调 `claim_next`：
    # 它正是 `run_once` 的第一步，所以这是"崩在解析过程中"的**逐字**状态，
    # 而不是某种近似（构造一个抛异常的 processor 反而绕了远路）。
    claimed = stage.platform.ingestion.claim_next(worker_id="crasher", lease_seconds=300)
    assert claimed is not None and claimed.job_id == stage.job_id

    job = stage.client.get(
        f"/projects/{stage.project_id}/ingestion-jobs/{stage.job_id}"
    ).json()
    assert job["status"] == "processing"
    assert job["attempt_count"] == 1

    # 租约期内，第二个 worker 认领不到它 —— 崩溃不该让任务立刻被两个人做。
    assert stage.platform.ingestion.claim_next(worker_id="other", lease_seconds=300) is None

    # 租约过期：等价于"等满 300 秒"。
    _expire_lease(stage.job_id)

    chunk_count = _ingest_once(stage, "r4-recoverer-" + stage.suffix)
    assert chunk_count >= 2

    job = stage.client.get(
        f"/projects/{stage.project_id}/ingestion-jobs/{stage.job_id}"
    ).json()
    assert job["status"] == "succeeded"
    assert job["attempt_count"] == 2, "回收恰好发生一次（不是零次，也不是三次）"

    # 片段只写了一次。
    chunks = stage.platform.ingestion.stored_chunks(stage.actor, stage.project_id)
    assert len(chunks) == chunk_count

    # 重复投递：拿同一个任务 + 同一批片段再 complete 一次。
    settled = stage.platform.ingestion.get_job(stage.actor, stage.project_id, stage.job_id)
    stage.platform.ingestion.complete(settled, chunks)

    again = stage.platform.ingestion.stored_chunks(stage.actor, stage.project_id)
    assert len(again) == chunk_count, "重复完成不得追加第二套片段"
    assert {item.content_hash for item in again} == {item.content_hash for item in chunks}

    # 已落定任务的重复完成也不改变尝试次数（那是"又被处理了一次"的痕迹）。
    assert stage.platform.ingestion.get_job(
        stage.actor, stage.project_id, stage.job_id
    ).attempt_count == 2
