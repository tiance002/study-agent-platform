"""P9 evidence rail browser regression against the real frontend bundle.

在 run_round8_preview.py 一次性预览页中加载真实前端 bundle，把
window.StudyViews.EvidenceRail 挂载到独立 DOM 容器（#p9-evidence-root），
依次渲染与 run HTTP 响应同形的 activeRun，并按完整 DOM 文本断言：
- 旧 run（无 retrieval_decision）不新增检索模式标签行；
- keyword / hybrid 模式显示对应中文标签；
- degraded 三个闭集原因各显示对应中文文案，未知原因显示固定兜底且不泄露原始码；
- 查询改写 fallback 标签与检索降级标签互不混称；
- 检索标签与路由标签同时存在时各行其是；
- 390px 视口下证据栏可见且页面无横向溢出，并保存截图。
成功打印 P9_EVIDENCE_BROWSER_PASSED。
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[1]

# EvidenceRail 在 status=succeeded、grounding=sourced、无引用时的基础行：
# 证据标题、状态行、grounding 行与空引用占位行。
BASE_EXPECTED_LINES = (
    "证据",
    "来源已核验",
    "回答含有已核验来源",
    "完成回答后，已核验的引用会出现在这里。",
)

KEYWORD_DECISION = {
    "mode": "keyword",
    "reason_code": "",
    "policy_version": "retrieval-route/v1",
    "ranking_version": "keyword/v1",
}
HYBRID_DECISION = {
    "mode": "hybrid",
    "reason_code": "",
    "policy_version": "retrieval-route/v1",
    "ranking_version": "hybrid-rrf/v1",
}


def degraded_decision(reason_code: str) -> dict:
    return {
        "mode": "degraded",
        "reason_code": reason_code,
        "policy_version": "retrieval-route/v1",
        "ranking_version": "keyword/v1",
    }


def applied_routing() -> dict:
    return {"query_rewrite_status": "applied"}


def run_payload(**overrides) -> dict:
    """构造与教学 run HTTP 响应同形的 activeRun（EvidenceRail 只读其中字段）。"""
    run = {
        "run_id": "run_p9_browser",
        "status": "succeeded",
        "grounding": "sourced",
        "citations": [],
    }
    run.update(overrides)
    return run


# name -> (activeRun, 额外期望行, 禁止出现的子串)
SCENARIOS: dict[str, tuple[dict, tuple[str, ...], tuple[str, ...]]] = {
    "legacy_without_decisions": (
        run_payload(),
        (),
        ("关键词检索", "混合检索", "向量检索不可用", "查询改写", "云端教学回答"),
    ),
    "keyword_mode": (
        run_payload(retrieval_decision=KEYWORD_DECISION),
        ("关键词检索",),
        ("混合检索", "向量检索不可用", "查询改写不可用"),
    ),
    "hybrid_mode": (
        run_payload(retrieval_decision=HYBRID_DECISION),
        ("混合检索（关键词 + 向量）",),
        ("向量检索不可用", "查询改写不可用"),
    ),
    "degraded_embedding_provider_failed": (
        run_payload(retrieval_decision=degraded_decision("embedding_provider_failed")),
        ("向量检索不可用，已回退关键词检索（向量服务不可用）",),
        ("向量索引未就绪", "向量模型版本不一致", "检索组件异常"),
    ),
    "degraded_vector_index_unavailable": (
        run_payload(retrieval_decision=degraded_decision("vector_index_unavailable")),
        ("向量检索不可用，已回退关键词检索（向量索引未就绪）",),
        ("向量服务不可用", "向量模型版本不一致", "检索组件异常"),
    ),
    "degraded_model_version_mismatch": (
        run_payload(
            retrieval_decision=degraded_decision("embedding_model_version_mismatch")
        ),
        ("向量检索不可用，已回退关键词检索（向量模型版本不一致）",),
        ("向量服务不可用", "向量索引未就绪", "检索组件异常"),
    ),
    "degraded_unknown_reason": (
        run_payload(retrieval_decision=degraded_decision("something_new")),
        ("向量检索不可用，已回退关键词检索（检索组件异常）",),
        ("something_new", "向量服务不可用", "向量索引未就绪", "向量模型版本不一致"),
    ),
    "query_rewrite_fallback": (
        run_payload(routing_decision={"query_rewrite_status": "fallback"}),
        ("查询改写不可用 · 云端教学回答",),
        ("向量检索不可用", "关键词检索降级", "本地查询改写"),
    ),
    "keyword_with_routing_applied": (
        run_payload(retrieval_decision=KEYWORD_DECISION, routing_decision=applied_routing()),
        ("关键词检索", "本地查询改写 · 云端教学回答"),
        ("向量检索不可用", "查询改写不可用"),
    ),
    "hybrid_with_routing_applied": (
        run_payload(retrieval_decision=HYBRID_DECISION, routing_decision=applied_routing()),
        ("混合检索（关键词 + 向量）", "本地查询改写 · 云端教学回答"),
        ("向量检索不可用", "查询改写不可用"),
    ),
    "degraded_with_routing_applied": (
        run_payload(
            retrieval_decision=degraded_decision("vector_index_unavailable"),
            routing_decision=applied_routing(),
        ),
        ("向量检索不可用，已回退关键词检索（向量索引未就绪）", "本地查询改写 · 云端教学回答"),
        ("查询改写不可用", "检索组件异常"),
    ),
}

MOUNT_JS = """() => {
  const host = document.createElement("div");
  host.id = "p9-evidence-root";
  document.body.appendChild(host);
  window.__p9Root = ReactDOM.createRoot(host);
}"""

RENDER_JS = """(run) => {
  ReactDOM.flushSync(() => {
    window.__p9Root.render(
      React.createElement(window.StudyViews.EvidenceRail, {
        activeRun: run,
        sources: [],
        plan: null,
        view: "conversation",
        onReadCitation: () => {},
        citationReading: null,
      })
    );
  });
  return document.getElementById("p9-evidence-root").innerText;
}"""


def render_run_lines(page, run) -> list[str]:
    """同步渲染一次 EvidenceRail 并返回容器内的非空文本行。"""
    text = page.evaluate(RENDER_JS, run)
    return [line.strip() for line in text.splitlines() if line.strip()]


def assert_scenario(page, name: str) -> None:
    run, extra_lines, forbidden = SCENARIOS[name]
    actual_lines = render_run_lines(page, run)
    expected_lines = [*BASE_EXPECTED_LINES, *extra_lines]
    joined = "\n".join(actual_lines)
    problems: list[str] = []
    if Counter(actual_lines) != Counter(expected_lines):
        missing = sorted(set(expected_lines) - set(actual_lines))
        unexpected = sorted(set(actual_lines) - set(expected_lines))
        problems.append(f"missing_lines={missing}; unexpected_lines={unexpected}")
    for snippet in forbidden:
        if snippet in joined:
            problems.append(f"forbidden_snippet_present={snippet!r}")
    if problems:
        raise AssertionError(
            {
                "scenario": name,
                "problems": problems,
                "expected_lines": sorted(expected_lines),
                "actual_lines": actual_lines,
            }
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8008")
    parser.add_argument(
        "--artifacts", default="var/round8-browser-gate",
        help="截图等产物的保存目录",
    )
    args = parser.parse_args()
    artifact_dir = REPO_ROOT / args.artifacts
    artifact_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        launch_options = {"headless": True}
        cached_chromium = Path.home() / "AppData" / "Local" / "ms-playwright" / "chromium-1234" / "chrome-win64" / "chrome.exe"
        if cached_chromium.exists():
            launch_options["executable_path"] = str(cached_chromium)
        browser = playwright.chromium.launch(**launch_options)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errors: list[str] = []
        console_messages: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: console_messages.append(f"{message.type}: {message.text}"))
        page.goto(args.base_url, wait_until="networkidle")

        availability = page.evaluate(
            """() => ({
              study_views: typeof window.StudyViews,
              evidence_rail: window.StudyViews ? typeof window.StudyViews.EvidenceRail : "undefined",
              react: typeof React,
              react_dom: typeof ReactDOM,
            })"""
        )
        if availability["evidence_rail"] != "function":
            raise AssertionError(
                {
                    "problem": "window.StudyViews.EvidenceRail 未在前端页面暴露",
                    "availability": availability,
                    "console": console_messages,
                }
            )
        page.evaluate(MOUNT_JS)

        for scenario in SCENARIOS:
            assert_scenario(page, scenario)

        # 390px 视口：标签共存场景下证据栏可见、无横向溢出，并保存截图。
        page.set_viewport_size({"width": 390, "height": 844})
        assert_scenario(page, "keyword_with_routing_applied")
        rail = page.locator("#p9-evidence-root .evidence-rail")
        expect(rail).to_be_visible()
        overflow = page.evaluate(
            "() => ({scroll_width: document.documentElement.scrollWidth, inner_width: window.innerWidth})"
        )
        if overflow["scroll_width"] > overflow["inner_width"] + 1:
            raise AssertionError(
                {
                    "problem": "390px 视口出现横向溢出",
                    "overflow": overflow,
                    "actual_lines": render_run_lines(page, SCENARIOS["keyword_with_routing_applied"][0]),
                }
            )
        page.screenshot(path=str(artifact_dir / "p9-evidence-390.png"), full_page=True)

        browser.close()

    # "Failed to load resource" 是网络层日志（页面自身登录请求等），只保留脚本错误。
    console_errors = [
        message
        for message in console_messages
        if message.startswith("error:") and "Failed to load resource" not in message
    ]
    if errors or console_errors:
        raise AssertionError({"page_errors": errors, "console_errors": console_errors, "console": console_messages})

    print("P9_EVIDENCE_BROWSER_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
