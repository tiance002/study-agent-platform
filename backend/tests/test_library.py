"""用户级共享知识库的端到端测试（内存适配器）。

守住四件"不会报错、只会慢慢坏掉"的语义：

1. **登记幂等**：同一份材料（同 `acquisition`）重复登记返回既有记录，不产生第二条；
2. **主体隔离**：别人主体的材料在自己这里既看不到也用不了，且拒绝**不泄露存在性**；
3. **关联复用**：关联在目标项目内复用同一份材料（同 `identity_hash`），
   重复关联不产生第二份资料、也不产生第二个摄取；
4. **关联产物是普通的项目资料**：worker 摄取后仍能被既有检索命中、
   引用仍能精确回读 —— 这是"零改动"这条声明唯一可验证的形式。
"""

from __future__ import annotations

import uuid

import pytest
from app.identity.models import Principal
from app.identity.ports import SystemContext

LIBRARY_KEYS = {
    "library_source_id",
    "display_name",
    "media_type",
    "identity_hash",
    "registered_at",
    "has_content",
}

MARKDOWN = "# 事务\n\n提交成功。\n\n## 回滚\n\n失败时回滚。\n"


def _keyed(headers: dict, key: str | None = None) -> dict:
    return {
        **headers,
        "Origin": "http://testserver",
        "Idempotency-Key": key or "lib-" + uuid.uuid4().hex,
    }


def _register(client, headers, *, acquisition=None, content=None, name="事务讲义"):
    body = {
        "display_name": name,
        "media_type": "text/markdown",
        "acquisition": acquisition or {"kind": "upload", "name": name},
    }
    if content is not None:
        body["content"] = content
    return client.post("/library/sources", json=body, headers=_keyed(headers))


@pytest.mark.invariant
def test_register_is_idempotent_and_returns_the_frozen_shape(client, auth_headers):
    headers = auth_headers()
    acquisition = {"kind": "upload", "name": "事务讲义"}

    first = _register(client, headers, acquisition=acquisition)
    assert first.status_code == 201, first.text
    assert set(first.json()) == LIBRARY_KEYS, "响应的字段集合是冻结契约"
    assert first.json()["has_content"] is False

    second = _register(client, headers, acquisition=acquisition)
    assert second.status_code == 200, "已存在返回既有记录（200），不是 201，也不是报错"
    assert second.json()["library_source_id"] == first.json()["library_source_id"]
    assert second.json()["identity_hash"] == first.json()["identity_hash"]

    listed = client.get("/library/sources", headers=headers)
    assert listed.status_code == 200
    assert [s["library_source_id"] for s in listed.json()["sources"]] == [
        first.json()["library_source_id"]
    ]


@pytest.mark.invariant
def test_content_can_be_registered_inline_or_uploaded_later(client, auth_headers):
    headers = auth_headers()

    inline = _register(client, headers, acquisition={"kind": "upload", "n": "a"}, content=MARKDOWN)
    assert inline.json()["has_content"] is True

    later = _register(client, headers, acquisition={"kind": "upload", "n": "b"})
    assert later.json()["has_content"] is False
    library_source_id = later.json()["library_source_id"]

    uploaded = client.post(
        f"/library/sources/{library_source_id}/content",
        json={"content": MARKDOWN},
        headers=_keyed(headers),
    )
    assert uploaded.status_code == 202, uploaded.text
    assert uploaded.json()["has_content"] is True

    # 列表里反映的是**派生事实**（库里有没有原文），不是某个可以自己变的开关。
    listed = client.get("/library/sources", headers=headers).json()["sources"]
    assert {s["library_source_id"]: s["has_content"] for s in listed} == {
        inline.json()["library_source_id"]: True,
        library_source_id: True,
    }


@pytest.mark.invariant
def test_attach_creates_a_project_source_and_is_idempotent(client, platform, auth_headers, demo):
    headers = auth_headers()
    registered = _register(client, headers, acquisition={"kind": "upload", "n": "attach"}, content=MARKDOWN)
    library_source_id = registered.json()["library_source_id"]
    project_id = demo["project"]

    first = client.post(
        f"/projects/{project_id}/library-sources/{library_source_id}/attach",
        headers=_keyed(headers),
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == {"source_id", "ingestion_job_id", "created"}
    assert body["created"] is True
    assert body["ingestion_job_id"], "库内有原文时服务端直接入队摄取"

    sources = client.get(f"/projects/{project_id}/sources", headers=headers).json()["sources"]
    assert [s["source_id"] for s in sources] == [body["source_id"]]
    jobs = client.get(f"/projects/{project_id}/ingestion-jobs", headers=headers).json()["jobs"]
    assert [j["job_id"] for j in jobs] == [body["ingestion_job_id"]]

    # 换一个幂等键再关联一次：必须复用既有资料与既有任务。
    second = client.post(
        f"/projects/{project_id}/library-sources/{library_source_id}/attach",
        headers=_keyed(headers),
    )
    assert second.status_code == 200, second.text
    assert second.json() == {**body, "created": False}, "重复关联不产生第二份资料或第二个摄取"
    assert len(client.get(f"/projects/{project_id}/sources", headers=headers).json()["sources"]) == 1
    assert len(client.get(f"/projects/{project_id}/ingestion-jobs", headers=headers).json()["jobs"]) == 1


@pytest.mark.invariant
def test_attach_rejects_a_project_the_actor_has_no_membership_for(client, platform, auth_headers):
    headers = auth_headers()
    registered = _register(client, headers, acquisition={"kind": "upload", "n": "no-member"})
    library_source_id = registered.json()["library_source_id"]

    # 同租户但**没有授予**这个主体的项目：判定必须走成员关系，不是"同租户就放行"。
    platform.membership.create_project(
        SystemContext("tenant_demo", "测试铸造"), project_id="proj_ungranted", name="未授权"
    )
    denied = client.post(
        "/projects/proj_ungranted/library-sources/" + library_source_id + "/attach",
        headers=_keyed(headers),
    )
    assert denied.status_code == 404, denied.text
    assert denied.json()["code"] == "NOT_FOUND"


@pytest.mark.invariant
def test_other_principal_cannot_see_or_use_a_library_source(client, auth_headers):
    owner = auth_headers(principal_id="lib_owner")
    intruder = auth_headers(principal_id="lib_intruder")
    registered = _register(client, owner, acquisition={"kind": "upload", "n": "private"}, content=MARKDOWN)
    library_source_id = registered.json()["library_source_id"]

    assert client.get("/library/sources", headers=intruder).json()["sources"] == []

    # 上传原文 / 关联：与"不存在"同一个 404 与同一句话术（不泄露存在性）。
    upload = client.post(
        f"/library/sources/{library_source_id}/content",
        json={"content": MARKDOWN},
        headers=_keyed(intruder),
    )
    attach = client.post(
        f"/projects/tenant_demo_probe/library-sources/{library_source_id}/attach",
        headers=_keyed(intruder),
    )
    assert (upload.status_code, attach.status_code) == (404, 404)
    assert upload.json()["code"] == attach.json()["code"] == "NOT_FOUND"
    assert upload.json()["message"] == "资源不存在"

    # 反向对照：所有者自己仍拿得到 —— 否则上面两条通过得毫无意义。
    assert client.get("/library/sources", headers=owner).json()["sources"][0][
        "library_source_id"
    ] == library_source_id


@pytest.mark.invariant
def test_attached_content_stays_searchable_and_citable(client, platform, auth_headers, demo):
    """关联后走既有检索/引用路径 —— 「项目级不变量零改动」的可验证形式。"""
    headers = auth_headers()
    registered = _register(client, headers, acquisition={"kind": "upload", "n": "retrieval"}, content=MARKDOWN)
    library_source_id = registered.json()["library_source_id"]
    project_id = demo["project"]

    attached = client.post(
        f"/projects/{project_id}/library-sources/{library_source_id}/attach",
        headers=_keyed(headers),
    ).json()

    from app.workers.ingestion import run_once

    assert run_once(platform, worker_id="library-ingestion").kind == "succeeded"

    hits = client.post(
        f"/projects/{project_id}/knowledge/search",
        json={"query": "回滚", "limit": 5},
        headers={**headers, "Origin": "http://testserver"},
    ).json()["hits"]
    assert hits, "关联进来的原文必须能被既有检索命中"
    citation = hits[0]["citation"]
    assert citation["source_id"] == attached["source_id"]

    span = client.get(
        f"/projects/{project_id}/sources/{citation['source_id']}/span",
        params={
            "document_id": citation["document_id"],
            "start": citation["span"][0],
            "end": citation["span"][1],
            "content_hash": citation["content_hash"],
        },
        headers={**headers, "Origin": "http://testserver"},
    )
    assert span.status_code == 200, span.text
    assert span.json()["citation"]["content_hash"] == citation["content_hash"]


@pytest.mark.invariant
def test_url_material_attach_enqueues_a_fetch_instead_of_ingestion(client, platform, auth_headers, demo):
    """库内没有原文的 URL 材料：关联产生 durable 下载任务，而不是凭空产生原文。"""
    headers = auth_headers()
    url = "https://example.com/agent-harness"
    registered = _register(
        client, headers, acquisition={"kind": "web", "url": url}, name="Agent harness"
    )
    assert registered.json()["has_content"] is False
    library_source_id = registered.json()["library_source_id"]
    project_id = demo["project"]

    attached = client.post(
        f"/projects/{project_id}/library-sources/{library_source_id}/attach",
        headers=_keyed(headers),
    )
    assert attached.status_code == 200, attached.text
    assert attached.json()["ingestion_job_id"] is None, "下载完成前没有摄取任务"

    principal = Principal(principal_id=demo["principal"], tenant_id=demo["tenant"])
    candidates = platform.acquisition.list_candidates(principal, project_id)
    assert [c.url for c in candidates] == [url]
    assert str(candidates[0].status) == "selected"

    # 重复关联不新建候选、也不新建下载任务（幂等键由 library_source_id 派生）。
    again = client.post(
        f"/projects/{project_id}/library-sources/{library_source_id}/attach",
        headers=_keyed(headers),
    )
    assert again.status_code == 200, again.text
    assert len(platform.acquisition.list_candidates(principal, project_id)) == 1
