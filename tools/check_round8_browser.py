"""Deterministic browser regressions for the round 8 workbench."""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def focus_with_keyboard(page, locator, *, max_tabs: int = 80) -> None:
    for _ in range(max_tabs):
        if locator.evaluate("element => element === document.activeElement"):
            return
        page.keyboard.press("Tab")
    raise AssertionError(f"Keyboard focus did not reach {locator}")


def type_with_keyboard(page, locator, value: str) -> None:
    focus_with_keyboard(page, locator)
    page.keyboard.press("Control+A")
    page.keyboard.type(value)


def submit_with_keyboard(page, locator) -> None:
    focus_with_keyboard(page, locator)
    page.keyboard.press("Enter")


def open_project_form(page, *, keyboard: bool = False) -> None:
    form = page.locator(".project-form")
    if form.is_visible():
        return
    summary = page.locator(".project-create summary")
    if keyboard:
        submit_with_keyboard(page, summary)
    else:
        summary.click()
    expect(form).to_be_visible()


def install_late_response_gate(page) -> None:
    page.evaluate(
        """() => {
          const nativeFetch = window.fetch.bind(window);
          const gate = {
            targets: new Set(),
            responses: [],
            calls: [],
            configure(paths) {
              this.targets = new Set(paths);
              this.responses = [];
              this.calls = [];
            },
            async body(path) {
              const item = this.responses.find((entry) => entry.path === path);
              return item ? await item.body : null;
            },
            async release(path) {
              const index = this.responses.findIndex((entry) => entry.path === path);
              if (index < 0) throw new Error(`No delayed response for ${path}`);
              const [item] = this.responses.splice(index, 1);
              item.resolve(item.response);
            },
          };
          window.__lateResponseGate = gate;
          window.fetch = (input, init = {}) => {
            const requestUrl = input instanceof Request ? input.url : input;
            const requestPath = new URL(requestUrl, window.location.href).pathname;
            const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
            gate.calls.push({ method, path: requestPath });
            if (method !== "GET" || !gate.targets.has(requestPath)) return nativeFetch(input, init);

            const uncancellableInit = { ...init };
            delete uncancellableInit.signal;
            const responsePromise = nativeFetch(input, uncancellableInit);
            return new Promise((resolve, reject) => {
              responsePromise.then((response) => {
                gate.responses.push({
                  path: requestPath,
                  response,
                  body: response.clone().json(),
                  resolve,
                });
              }, reject);
            });
          };
        }"""
    )


def wait_for_late_response(page, path: str) -> None:
    page.wait_for_function(
        "(path) => window.__lateResponseGate.responses.some((entry) => entry.path === path)",
        arg=path,
        timeout=10000,
    )


def release_late_response(page, path: str) -> None:
    page.evaluate("(path) => window.__lateResponseGate.release(path)", path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8008")
    parser.add_argument("--artifacts", default="var/round8-browser")
    args = parser.parse_args()
    artifact_dir = Path(args.artifacts)
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
        responses: list[str] = []
        request_failures: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: console_messages.append(f"{message.type}: {message.text}"))
        page.on("response", lambda response: responses.append(f"{response.status} {response.request.method} {response.url}"))
        page.on("requestfailed", lambda request: request_failures.append(f"{request.method} {request.url}: {request.failure}"))
        page.goto(args.base_url, wait_until="networkidle")
        page.screenshot(path=str(artifact_dir / "before-login.png"), full_page=True)
        if not page.get_by_label("用户名").is_visible():
            raise AssertionError({"page_errors": errors, "console": console_messages, "url": page.url})

        keyboard_page = browser.new_page(viewport={"width": 768, "height": 900})
        keyboard_events: list[str] = []
        keyboard_page.on("response", lambda response: keyboard_events.append(f"{response.status} {response.request.method} {response.url}"))
        keyboard_page.on("requestfailed", lambda request: keyboard_events.append(f"FAILED {request.method} {request.url}: {request.failure}"))
        keyboard_page.on("pageerror", lambda error: keyboard_events.append(f"PAGEERROR {error}"))
        keyboard_page.goto(args.base_url, wait_until="networkidle")
        focus_with_keyboard(keyboard_page, keyboard_page.get_by_role("tab", name="注册账号"))
        keyboard_page.keyboard.press("Enter")
        expect(keyboard_page.get_by_role("tab", name="注册账号")).to_have_attribute(
            "aria-selected", "true"
        )
        type_with_keyboard(keyboard_page, keyboard_page.get_by_label("用户名"), "r8key" + uuid.uuid4().hex[:7])
        keyboard_page.keyboard.press("Tab")
        keyboard_page.keyboard.type("r8-keyboard")
        keyboard_page.keyboard.press("Tab")
        keyboard_page.keyboard.press("Enter")
        expect(keyboard_page.locator(".project-create summary")).to_be_visible()
        expect(keyboard_page.locator(".project-form")).not_to_be_visible()
        open_project_form(keyboard_page, keyboard=True)
        type_with_keyboard(keyboard_page, keyboard_page.get_by_label("项目名称"), "R8 键盘闭环")
        keyboard_page.keyboard.press("Tab")
        keyboard_page.keyboard.type("键盘完成建项目、登记资料、创建计划和读取引用")
        keyboard_page.keyboard.press("Tab")
        keyboard_page.keyboard.press("Enter")
        expect(keyboard_page.get_by_role("heading", name="R8 键盘闭环", exact=True)).to_be_visible()
        type_with_keyboard(keyboard_page, keyboard_page.get_by_label("新会话名称"), "键盘会话")
        keyboard_page.keyboard.press("Tab")
        keyboard_page.keyboard.press("Enter")
        expect(keyboard_page.locator("#conversation-select")).to_be_visible()
        submit_with_keyboard(keyboard_page, keyboard_page.get_by_role("button", name="资料", exact=True))
        type_with_keyboard(keyboard_page, keyboard_page.get_by_label("资料名称"), "键盘资料")
        keyboard_page.keyboard.press("Tab")
        keyboard_page.keyboard.type("键盘资料原文")
        keyboard_page.keyboard.press("Tab")
        keyboard_page.keyboard.type("键盘用户可以完成真实学习闭环并读取精确引用。")
        keyboard_page.keyboard.press("Tab")
        keyboard_events.append(
            "before-source-submit "
            + keyboard_page.evaluate(
                """() => {
                  const form = document.activeElement?.form;
                  const key = form && Object.keys(form).find((name) => name.startsWith("__reactProps$"));
                  return JSON.stringify({
                    active: document.activeElement?.outerHTML,
                    form: form?.outerHTML,
                    target: form?.getAttribute("class"),
                    reactSubmit: key ? typeof form[key].onSubmit : "missing",
                  });
                }"""
            )
        )
        keyboard_page.keyboard.press("Enter")
        try:
            expect(keyboard_page.get_by_text("资料已登记，正在处理", exact=True)).to_be_visible()
        except AssertionError as caught:
            raise AssertionError(
                {
                    "url": keyboard_page.url,
                    "active_element": keyboard_page.evaluate("() => document.activeElement?.outerHTML"),
                    "body": keyboard_page.locator("body").inner_text(),
                    "events": keyboard_events[-30:],
                }
            ) from caught
        expect(keyboard_page.locator(".source-list:not(.candidate-list) .source-status").last).to_contain_text("已处理", timeout=15000)
        submit_with_keyboard(keyboard_page, keyboard_page.get_by_role("button", name="计划", exact=True))
        type_with_keyboard(keyboard_page, keyboard_page.get_by_label("学习目标"), "使用键盘完成学习闭环")
        keyboard_page.keyboard.press("Tab")
        keyboard_page.keyboard.type("能用键盘登记资料并读取原文引用")
        keyboard_page.keyboard.press("Tab")
        keyboard_page.keyboard.press("Enter")
        expect(keyboard_page.get_by_text("能用键盘登记资料并读取原文引用", exact=True)).to_be_visible()
        submit_with_keyboard(keyboard_page, keyboard_page.get_by_role("button", name="会话", exact=True))
        type_with_keyboard(keyboard_page, keyboard_page.get_by_label("输入问题"), "键盘资料说明什么？")
        keyboard_page.keyboard.press("Tab")
        keyboard_page.keyboard.press("Enter")
        expect(keyboard_page.get_by_text("回答已准备好", exact=True)).to_be_visible(timeout=15000)
        expect(keyboard_page.get_by_text("阅读资料后给出可执行的学习解释。", exact=True)).to_be_visible(timeout=15000)
        submit_with_keyboard(keyboard_page, keyboard_page.get_by_role("button", name="读取原文", exact=True))
        expect(keyboard_page.get_by_text("键盘用户可以完成真实学习闭环并读取精确引用。", exact=True)).to_be_visible(timeout=10000)
        assert keyboard_page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        keyboard_page.screenshot(path=str(artifact_dir / "workbench-keyboard-tablet.png"), full_page=True)
        keyboard_page.close()

        page.get_by_role("tab", name="注册账号").click()
        page.get_by_label("用户名").fill("r8user" + uuid.uuid4().hex[:6])
        page.get_by_label("密码").fill("r8-password")
        page.get_by_role("button", name="创建账号").click()
        expect(page.locator(".project-create summary")).to_be_visible()
        expect(page.locator(".project-form")).not_to_be_visible()
        page.get_by_role("button", name="资料", exact=True).click()
        search_requests: list[tuple[str, str]] = []

        def fail_first_search(route) -> None:
            request = route.request
            if request.method == "POST" and request.url.endswith("/source-search"):
                search_requests.append(
                    (request.headers.get("idempotency-key", ""), request.post_data or "")
                )
                if len(search_requests) == 1:
                    route.fulfill(
                        status=503,
                        json={
                            "code": "SOURCE_SEARCH_UNAVAILABLE",
                            "message": "搜索结果暂时无法确认；再次搜索会发起新请求，可能产生额外费用",
                            "retryable": False,
                            "request_id": None,
                            "next_action": "",
                        },
                    )
                    return
                route.fulfill(status=200, json={"candidates": []})
                return
            route.continue_()

        page.route("**/source-search", fail_first_search)
        page.get_by_label("搜索公开资料").fill("幂等搜索重试")
        page.get_by_role("button", name="搜索", exact=True).click()
        expect(page.get_by_text("搜索结果暂时无法确认；再次搜索会发起新请求，可能产生额外费用", exact=True)).to_be_visible()
        assert page.evaluate("innerWidth === 390 && document.documentElement.scrollWidth <= innerWidth + 1")
        expect(page.get_by_text("搜索资料结果未知，请确认后重试。", exact=True)).to_have_count(0)
        page.get_by_role("button", name="搜索", exact=True).click()
        expect(page.get_by_text("没有找到可用资料候选", exact=True)).to_be_visible()
        assert len(search_requests) == 2
        assert search_requests[0][0] != search_requests[1][0]
        assert search_requests[0][1] == search_requests[1][1]

        page.unroute("**/source-search", fail_first_search)
        candidate = {
            "candidate_id": "cand_round9_browser",
            "url": "https://example.com/round9-guide",
            "title": "第九轮受控候选资料",
            "snippet": "用于验证资料确认与下载状态展示。",
            "source_domain": "example.com",
            "status": "discovered",
            "discovered_at": "2026-09-23T00:00:00+00:00",
            "expires_at": "2026-09-24T00:00:00+00:00",
        }
        source = {
            "source_id": "src_round9_browser",
            "display_name": candidate["title"],
            "media_type": "text/markdown",
        }
        acquisition = {
            "acquisition_id": "acq_round9_browser",
            "source_id": source["source_id"],
            "candidate_id": candidate["candidate_id"],
            "url": candidate["url"],
            "title": candidate["title"],
            "media_type": "text/markdown",
            "language": "zh",
            "status": "queued",
            "attempt_count": 0,
            "error_code": "",
            "error_detail": "",
        }

        def round9_search(route) -> None:
            route.fulfill(status=200, json={"candidates": [candidate]})

        def round9_acquisition(route) -> None:
            request = route.request
            if request.method == "POST" and request.url.endswith("/select"):
                candidate["status"] = "selected"
                route.fulfill(
                    status=202,
                    json={"candidate": {**candidate, "status": "selected"}, "source": source, "acquisition": acquisition},
                )
            elif request.method == "GET" and "/acquisition-jobs/" in request.url:
                route.fulfill(status=200, json={**acquisition, "status": "succeeded"})
            elif request.method == "GET" and request.url.endswith("/source-candidates"):
                response = route.fetch()
                payload = response.json()
                payload["candidates"] = [candidate, *payload.get("candidates", [])]
                route.fulfill(response=response, json=payload)
            elif request.method == "GET" and request.url.endswith("/sources"):
                response = route.fetch()
                payload = response.json()
                payload["sources"] = [source, *payload.get("sources", [])]
                route.fulfill(response=response, json=payload)
            else:
                route.continue_()

        page.route("**/source-search", round9_search)
        page.route("**/source-candidates/*/select", round9_acquisition)
        page.route("**/acquisition-jobs/*", round9_acquisition)
        page.route("**/source-candidates", round9_acquisition)
        page.route("**/sources", round9_acquisition)
        page.get_by_label("搜索公开资料").fill("第九轮采集闭环")
        page.get_by_role("button", name="搜索", exact=True).click()
        expect(page.get_by_text(candidate["title"], exact=True)).to_be_visible()
        page.get_by_role("button", name="确认下载", exact=True).click()
        expect(page.get_by_text("资料已加入下载队列", exact=True)).to_be_visible()
        expect(page.get_by_text("已下载", exact=True)).to_be_visible(timeout=10000)

        project_requests: list[tuple[str, str]] = []

        def abort_first_project_response(route) -> None:
            request = route.request
            if request.method == "POST" and request.url.endswith("/projects"):
                project_requests.append((request.headers.get("idempotency-key", ""), request.post_data or ""))
                if len(project_requests) == 1:
                    response = route.fetch()
                    route.fulfill(response=response, status=503)
                    return
            route.continue_()

        page.route("**/projects", abort_first_project_response)
        mobile_project_name = "R8 移动端长中文项目名称用于布局检查"
        open_project_form(page)
        page.get_by_label("项目名称").fill(mobile_project_name)
        page.get_by_placeholder("项目目标（可选）").fill("验证普通用户工作台")
        page.get_by_role("button", name="+ 创建项目", exact=True).click()
        expect(page.get_by_text("创建项目结果未知，请确认后重试。", exact=True)).to_be_visible()
        page.get_by_role("button", name="用同一请求重试", exact=True).click()
        expect(page.get_by_role("heading", name=mobile_project_name, exact=True)).to_be_visible()
        expect(page.locator(".project-form")).not_to_be_visible()
        assert len(project_requests) == 2
        assert project_requests[0] == project_requests[1]
        page.unroute("**/projects", abort_first_project_response)

        page.get_by_label("新会话名称").fill("第一问")
        page.get_by_role("button", name="+ 新会话", exact=True).click()
        expect(page.locator("#conversation-select")).to_be_visible()
        page.get_by_role("button", name="资料", exact=True).click()
        page.get_by_label("资料名称").fill("R8 资料")
        page.get_by_label("文档标题").fill("工作台边界")
        mobile_source_text = (
            "每个项目的数据必须隔离。用户只能检索当前项目最新成功版本中的资料。"
            "每条引用都要按来源、文档版本、原文跨度和内容指纹精确回读。"
            "资料登记成功不代表已经处理完成；只有摄取任务成功后，资料才可参与检索。"
        )
        page.get_by_label("正文").fill(mobile_source_text)
        page.wait_for_timeout(200)
        page.get_by_role("button", name="登记资料", exact=True).click()
        try:
            expect(page.get_by_text("资料已登记，正在处理", exact=True)).to_be_visible()
        except AssertionError as caught:
            raise AssertionError({"page_errors": errors, "console": console_messages, "responses": responses, "body": page.locator("body").inner_text()}) from caught
        expect(
            page.locator(".source-list:not(.candidate-list) .source-status").last
        ).to_contain_text("已处理", timeout=15000)

        page.get_by_role("button", name="计划", exact=True).click()
        page.get_by_label("学习目标").fill("完成普通用户闭环")
        page.get_by_label("第一个里程碑").fill("能创建和读取学习资料")
        page.get_by_role("button", name="创建计划", exact=True).click()
        expect(page.get_by_text("当前计划为只读版本；后续编辑会保留任务身份和学习证据。", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="创建计划", exact=True)).to_have_count(0)

        page.get_by_role("button", name="会话", exact=True).click()
        teaching_requests: list[tuple[str, str]] = []

        def degrade_first_teaching_response(route) -> None:
            request = route.request
            if request.method == "POST":
                teaching_requests.append(
                    (request.headers.get("idempotency-key", ""), request.post_data or "")
                )
                if len(teaching_requests) == 1:
                    response = route.fetch()
                    route.fulfill(response=response, status=503)
                    return
            route.continue_()

        page.route("**/teaching-runs", degrade_first_teaching_response)
        page.get_by_label("输入问题").fill("项目为什么需要隔离？")
        page.get_by_role("button", name="发送问题", exact=True).click()
        expect(page.get_by_text("发送问题结果未知，请确认后重试。", exact=True)).to_be_visible()
        page.get_by_role("button", name="用同一请求重试", exact=True).click()
        expect(page.get_by_text("回答已准备好", exact=True)).to_be_visible(timeout=15000)
        expect(page.get_by_text("阅读资料后给出可执行的学习解释。", exact=True)).to_be_visible(timeout=15000)
        # P9 把检索模式与路由状态拆成两个独立标签：本地预览未配置查询改写，
        # 路由标签为"云端教学回答"，检索标签为"关键词检索"。
        expect(page.get_by_text("云端教学回答", exact=True)).to_be_visible()
        expect(page.get_by_text("关键词检索", exact=True)).to_be_visible()
        assert len(teaching_requests) == 2
        assert teaching_requests[0] == teaching_requests[1]
        page.unroute("**/teaching-runs", degrade_first_teaching_response)
        page.get_by_role("button", name="读取原文", exact=True).click()
        expect(page.get_by_text(mobile_source_text, exact=True)).to_be_visible(timeout=10000)
        assert page.evaluate("innerWidth === 390 && document.documentElement.scrollWidth <= innerWidth + 1")
        page.screenshot(path=str(artifact_dir / "workbench-mobile-answer-citation.png"), full_page=True)
        page.get_by_role("button", name="计划", exact=True).click()
        expect(page.get_by_text("能创建和读取学习资料", exact=True)).to_be_visible()
        page.screenshot(path=str(artifact_dir / "workbench-mobile-plan.png"), full_page=True)
        page.get_by_role("button", name="资料", exact=True).click()
        expect(page.locator(".source-list:not(.candidate-list)").get_by_text("R8 资料", exact=True)).to_be_visible()
        expect(page.locator(".source-list:not(.candidate-list) .source-status").last).to_contain_text("已处理")
        page.screenshot(path=str(artifact_dir / "workbench-mobile-source-status.png"), full_page=True)

        page.set_viewport_size({"width": 768, "height": 900})
        page.screenshot(path=str(artifact_dir / "workbench-tablet.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.screenshot(path=str(artifact_dir / "workbench-desktop.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

        page.unroute("**/source-search", round9_search)
        page.unroute("**/source-candidates/*/select", round9_acquisition)
        page.unroute("**/acquisition-jobs/*", round9_acquisition)
        page.unroute("**/source-candidates", round9_acquisition)
        page.unroute("**/sources", round9_acquisition)

        desktop_project_name = "R8 桌面完整闭环"
        open_project_form(page)
        page.get_by_label("项目名称").fill(desktop_project_name)
        page.get_by_placeholder("项目目标（可选）").fill("在 1440px 完成资料到引用的完整流程")
        page.get_by_role("button", name="+ 创建项目", exact=True).click()
        expect(page.get_by_role("heading", name=desktop_project_name, exact=True)).to_be_visible()
        page.get_by_label("新会话名称").fill("桌面会话")
        page.get_by_role("button", name="+ 新会话", exact=True).click()
        expect(page.locator("#conversation-select")).to_be_visible()
        page.get_by_role("button", name="资料", exact=True).click()
        page.get_by_label("资料名称").fill("桌面来源资料")
        page.get_by_label("文档标题").fill("桌面引用核验")
        desktop_source_text = "桌面用户从资料登记、计划创建到引用回读的路径必须可完成。"
        page.get_by_label("正文").fill(desktop_source_text)
        page.get_by_role("button", name="登记资料", exact=True).click()
        expect(page.get_by_text("资料已登记，正在处理", exact=True)).to_be_visible()
        expect(page.locator(".source-list:not(.candidate-list) .source-status").last).to_contain_text("已处理", timeout=15000)
        page.get_by_role("button", name="计划", exact=True).click()
        page.get_by_label("学习目标").fill("在桌面完整完成资料驱动学习")
        page.get_by_label("第一个里程碑").fill("可以精确读取来源原文")
        page.get_by_role("button", name="创建计划", exact=True).click()
        expect(page.get_by_text("可以精确读取来源原文", exact=True)).to_be_visible()
        page.get_by_role("button", name="会话", exact=True).click()
        page.get_by_label("输入问题").fill("桌面资料说明什么？")
        page.get_by_role("button", name="发送问题", exact=True).click()
        expect(page.get_by_text("回答已准备好", exact=True)).to_be_visible(timeout=15000)
        expect(page.get_by_text("阅读资料后给出可执行的学习解释。", exact=True)).to_be_visible(timeout=15000)
        page.get_by_role("button", name="读取原文", exact=True).click()
        expect(page.get_by_text(desktop_source_text, exact=True)).to_be_visible(timeout=10000)
        assert page.evaluate("innerWidth === 1440 && document.documentElement.scrollWidth <= innerWidth + 1")
        page.screenshot(path=str(artifact_dir / "workbench-desktop-answer-citation.png"), full_page=True)
        page.get_by_role("button", name="计划", exact=True).click()
        expect(page.get_by_text("可以精确读取来源原文", exact=True)).to_be_visible()
        page.screenshot(path=str(artifact_dir / "workbench-desktop-plan.png"), full_page=True)
        page.get_by_role("button", name="资料", exact=True).click()
        expect(page.locator(".source-list:not(.candidate-list)").get_by_text("桌面来源资料", exact=True)).to_be_visible()
        expect(page.locator(".source-list:not(.candidate-list) .source-status").last).to_contain_text("已处理")
        page.screenshot(path=str(artifact_dir / "workbench-desktop-source-status.png"), full_page=True)

        install_late_response_gate(page)
        project_b_name = "R8 作用域 B"
        open_project_form(page)
        page.get_by_label("项目名称").fill(project_b_name)
        page.get_by_placeholder("项目目标（可选）").fill("验证项目切换时旧响应隔离")
        page.get_by_role("button", name="+ 创建项目", exact=True).click()
        try:
            expect(page.get_by_role("heading", name=project_b_name, exact=True)).to_be_visible(timeout=15000)
        except AssertionError as caught:
            raise AssertionError(
                {
                    "responses": responses[-12:],
                    "page_errors": errors,
                    "console": console_messages[-20:],
                    "request_failures": request_failures[-20:],
                    "fetch_calls": page.evaluate("() => window.__lateResponseGate.calls"),
                    "project_name_value": page.get_by_label("项目名称").input_value(),
                    "body": page.locator("body").inner_text(),
                }
            ) from caught
        page.get_by_label("新会话名称").fill("B 会话一")
        page.get_by_role("button", name="+ 新会话", exact=True).click()
        page.locator("#conversation-select").select_option(label="B 会话一")
        page.get_by_role("button", name="计划", exact=True).click()
        page.get_by_label("学习目标").fill("C1_B_PLAN_GOAL")
        page.get_by_label("第一个里程碑").fill("C1_B_PLAN_MARKER")
        page.get_by_role("button", name="创建计划", exact=True).click()
        expect(page.get_by_text("C1_B_PLAN_MARKER", exact=True)).to_be_visible()
        page.get_by_role("button", name="会话", exact=True).click()

        selection_key = page.evaluate(
            "() => Object.keys(localStorage).find((key) => key.startsWith('study:selection:'))"
        )
        project_b_id = page.evaluate(
            "(key) => JSON.parse(localStorage.getItem(key)).projectId", selection_key
        )
        project_a_name = mobile_project_name
        page.get_by_role("button", name=project_a_name, exact=True).click()
        expect(page.get_by_role("heading", name=project_a_name, exact=True)).to_be_visible()
        project_a_id = page.evaluate(
            "(key) => JSON.parse(localStorage.getItem(key)).projectId", selection_key
        )
        run_pointers = page.evaluate(
            """() => Object.keys(localStorage)
              .filter((key) => key.startsWith("study:last-run:"))
              .map((key) => ({ key, ...JSON.parse(localStorage.getItem(key)) }))"""
        )
        project_a_run = next(pointer for pointer in run_pointers if pointer["projectId"] == project_a_id)
        page.get_by_role("button", name=project_b_name, exact=True).click()
        expect(page.get_by_role("heading", name=project_b_name, exact=True)).to_be_visible()
        assert page.evaluate("(key) => JSON.parse(localStorage.getItem(key)).projectId", selection_key) == project_b_id
        project_a_conversation = project_a_run["conversationId"]
        project_a_plan_path = f"/projects/{project_a_id}/plan"
        project_a_messages_path = (
            f"/projects/{project_a_id}/conversations/{project_a_conversation}/messages"
        )
        project_a_run_path = f"/projects/{project_a_id}/teaching-runs/{project_a_run['runId']}"

        page.evaluate(
            "(paths) => window.__lateResponseGate.configure(paths)", [project_a_plan_path]
        )
        page.get_by_role("button", name=project_a_name, exact=True).click()
        wait_for_late_response(page, project_a_plan_path)
        page.get_by_role("button", name=project_b_name, exact=True).click()
        expect(page.get_by_role("heading", name=project_b_name, exact=True)).to_be_visible()
        page.get_by_role("button", name="计划", exact=True).click()
        expect(page.get_by_text("C1_B_PLAN_MARKER", exact=True)).to_be_visible()
        release_late_response(page, project_a_plan_path)
        expect(page.get_by_text("C1_B_PLAN_MARKER", exact=True)).to_be_visible()
        page.get_by_role("button", name="会话", exact=True).click()

        page.evaluate(
            "(paths) => window.__lateResponseGate.configure(paths)",
            [project_a_messages_path, project_a_run_path],
        )
        page.get_by_role("button", name=project_a_name, exact=True).click()
        wait_for_late_response(page, project_a_messages_path)
        wait_for_late_response(page, project_a_run_path)
        page.get_by_role("button", name=project_b_name, exact=True).click()
        expect(page.get_by_role("heading", name=project_b_name, exact=True)).to_be_visible()
        expect(page.locator(".run-strip")).to_have_count(0)
        release_late_response(page, project_a_messages_path)
        release_late_response(page, project_a_run_path)
        expect(page.get_by_text("项目为什么需要隔离？", exact=True)).to_have_count(0)
        expect(page.locator(".run-strip")).to_have_count(0)
        page.get_by_role("button", name="计划", exact=True).click()
        expect(page.get_by_text("C1_B_PLAN_MARKER", exact=True)).to_be_visible()
        page.get_by_role("button", name="会话", exact=True).click()

        question_b1 = "C2 专属于会话一的消息标记"
        page.get_by_label("输入问题").fill(question_b1)
        page.get_by_role("button", name="发送问题", exact=True).click()
        expect(page.get_by_text("回答已准备好", exact=True)).to_be_visible(timeout=15000)
        expect(page.get_by_text(question_b1, exact=True)).to_be_visible()
        selected_b1 = page.evaluate("(key) => JSON.parse(localStorage.getItem(key))", selection_key)
        pointer_b1 = page.evaluate(
            """(scope) => Object.keys(localStorage)
              .filter((key) => key.startsWith("study:last-run:"))
              .map((key) => JSON.parse(localStorage.getItem(key)))
              .find((pointer) => pointer.projectId === scope.projectId && pointer.conversationId === scope.conversationId)""",
            {"projectId": project_b_id, "conversationId": selected_b1["conversationId"]},
        )
        page.get_by_label("新会话名称").fill("B 会话二")
        page.get_by_role("button", name="+ 新会话", exact=True).click()
        page.locator("#conversation-select").select_option(label="B 会话二")
        question_b2 = "C2 专属于会话二的消息标记"
        page.get_by_label("输入问题").fill(question_b2)
        page.get_by_role("button", name="发送问题", exact=True).click()
        expect(page.get_by_text("回答已准备好", exact=True)).to_be_visible(timeout=15000)
        expect(page.get_by_text(question_b2, exact=True)).to_be_visible()
        selected_b2 = page.evaluate("(key) => JSON.parse(localStorage.getItem(key))", selection_key)
        pointer_b2 = page.evaluate(
            """(scope) => Object.keys(localStorage)
              .filter((key) => key.startsWith("study:last-run:"))
              .map((key) => JSON.parse(localStorage.getItem(key)))
              .find((pointer) => pointer.projectId === scope.projectId && pointer.conversationId === scope.conversationId)""",
            {"projectId": project_b_id, "conversationId": selected_b2["conversationId"]},
        )
        assert pointer_b1["conversationId"] != pointer_b2["conversationId"]
        b1_messages_path = (
            f"/projects/{project_b_id}/conversations/{pointer_b1['conversationId']}/messages"
        )
        b1_run_path = f"/projects/{project_b_id}/teaching-runs/{pointer_b1['runId']}"
        page.evaluate(
            "(paths) => window.__lateResponseGate.configure(paths)",
            [b1_messages_path, b1_run_path],
        )
        page.locator("#conversation-select").select_option(label="B 会话一")
        wait_for_late_response(page, b1_messages_path)
        wait_for_late_response(page, b1_run_path)
        page.locator("#conversation-select").select_option(label="B 会话二")
        expect(page.get_by_text(question_b2, exact=True)).to_be_visible()
        release_late_response(page, b1_messages_path)
        release_late_response(page, b1_run_path)
        expect(page.get_by_text(question_b2, exact=True)).to_be_visible()
        expect(page.get_by_text(question_b1, exact=True)).to_have_count(0)

        second_user = "r8scope" + uuid.uuid4().hex[:7]
        second_page = browser.new_page(viewport={"width": 390, "height": 844})
        second_page_errors: list[str] = []
        second_page_console: list[str] = []
        second_page.on("pageerror", lambda error: second_page_errors.append(str(error)))
        second_page.on(
            "console",
            lambda message: second_page_console.append(f"{message.type}: {message.text}"),
        )
        second_page.goto(args.base_url, wait_until="networkidle")
        second_page.get_by_role("tab", name="注册账号").click()
        second_page.get_by_label("用户名").fill(second_user)
        second_page.get_by_label("密码").fill("r8-scope-pw")
        second_page.get_by_role("button", name="创建账号").click()
        expect(second_page.locator(".project-create summary")).to_be_visible()
        expect(second_page.locator(".project-form")).not_to_be_visible()
        second_principal = second_page.locator(".identity").inner_text()
        second_page.close()

        page.evaluate(
            "(paths) => window.__lateResponseGate.configure(paths)",
            [project_a_run_path, project_a_messages_path],
        )
        page.get_by_role("button", name=project_a_name, exact=True).click()
        wait_for_late_response(page, project_a_run_path)
        wait_for_late_response(page, project_a_messages_path)
        old_run = page.evaluate(
            "(path) => window.__lateResponseGate.body(path)", project_a_run_path
        )
        assert old_run["status"] == "succeeded"
        assert old_run["citations"]
        old_messages = page.evaluate(
            "(path) => window.__lateResponseGate.body(path)", project_a_messages_path
        )
        old_answer = next(
            message["content"] for message in old_messages["messages"] if message["role"] == "assistant"
        )
        old_draft = "只属于旧用户的未提交草稿"
        page.get_by_label("输入问题").fill(old_draft)
        page.get_by_role("button", name="退出", exact=True).click()
        expect(page.get_by_label("用户名")).to_be_visible()
        page.get_by_role("tab", name="登录").click()
        page.get_by_label("用户名").fill(second_user)
        page.get_by_label("密码").fill("r8-scope-pw")
        page.get_by_role("button", name="登录").click()
        expect(page.locator(".identity")).to_have_text(second_principal)
        open_project_form(page)
        page.get_by_label("项目名称").fill("C3 新用户项目")
        page.get_by_placeholder("项目目标（可选）").fill("验证账户切换后隔离旧响应")
        page.get_by_role("button", name="+ 创建项目", exact=True).click()
        expect(page.get_by_role("heading", name="C3 新用户项目", exact=True)).to_be_visible()
        page.get_by_label("新会话名称").fill("新用户会话")
        page.get_by_role("button", name="+ 新会话").click()
        expect(page.get_by_label("输入问题")).to_be_visible()
        release_late_response(page, project_a_messages_path)
        release_late_response(page, project_a_run_path)
        expect(page.get_by_text(old_answer, exact=True)).to_have_count(0)
        expect(page.get_by_text(old_draft, exact=True)).to_have_count(0)
        expect(page.get_by_text("回答已准备好", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="读取原文", exact=True)).to_have_count(0)
        expect(page.locator(".run-strip")).to_have_count(0)
        expect(page.get_by_label("输入问题")).to_have_value("")

        assert not errors, errors

        def genuine_console_errors(messages: list[str]) -> list[str]:
            # "Failed to load resource" 是网络层日志：回归本身会故意触发
            # 401/404/503（幂等重试、登出后迟到响应）。这里只保留真正的
            # JavaScript 控制台错误。
            return [
                message
                for message in messages
                if message.startswith("error:") and "Failed to load resource" not in message
            ]

        console_errors = genuine_console_errors(console_messages)
        assert not console_errors, {"console_errors": console_errors, "console": console_messages}
        keyboard_page_errors = [event for event in keyboard_events if event.startswith("PAGEERROR")]
        assert not keyboard_page_errors, keyboard_page_errors
        assert not second_page_errors, second_page_errors
        second_page_console_errors = genuine_console_errors(second_page_console)
        assert not second_page_console_errors, {
            "console_errors": second_page_console_errors,
            "console": second_page_console,
        }
        browser.close()
    print("ROUND8_BROWSER_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
