"""Deterministic browser regressions for the round 8 workbench."""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8008")
    parser.add_argument("--artifacts", default="var/round8-browser")
    parser.add_argument("--invite", default="round8-preview-invite")
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
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: console_messages.append(f"{message.type}: {message.text}"))
        page.on("response", lambda response: responses.append(f"{response.status} {response.request.method} {response.url}"))
        page.goto(args.base_url, wait_until="networkidle")
        page.screenshot(path=str(artifact_dir / "before-login.png"), full_page=True)
        if not page.get_by_label("用户名").is_visible():
            raise AssertionError({"page_errors": errors, "console": console_messages, "url": page.url})

        keyboard_page = browser.new_page(viewport={"width": 768, "height": 900})
        keyboard_page.goto(args.base_url, wait_until="networkidle")
        keyboard_page.get_by_role("tab", name="注册账号").focus()
        keyboard_page.keyboard.press("Enter")
        expect(keyboard_page.get_by_role("tab", name="注册账号")).to_have_attribute(
            "aria-selected", "true"
        )
        keyboard_page.get_by_label("用户名").focus()
        keyboard_page.keyboard.type("r8key" + uuid.uuid4().hex[:7])
        keyboard_page.keyboard.press("Tab")
        keyboard_page.keyboard.type("round8-keyboard-password")
        keyboard_page.keyboard.press("Tab")
        keyboard_page.keyboard.press("Enter")
        expect(keyboard_page.locator(".project-form")).to_be_visible()
        assert keyboard_page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        keyboard_page.close()

        page.get_by_role("tab", name="注册账号").click()
        page.get_by_label("用户名").fill("r8user" + uuid.uuid4().hex[:6])
        page.get_by_label("密码").fill("round8-password-123")
        page.get_by_role("button", name="创建账号").click()
        expect(page.locator(".project-form")).to_be_visible()
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
        page.get_by_label("新建项目").fill("R8 手机学习项目")
        page.get_by_placeholder("项目目标（可选）").fill("验证普通用户工作台")
        page.get_by_role("button", name="+ 创建项目", exact=True).click()
        expect(page.get_by_text("创建项目结果未知，请确认后重试。", exact=True)).to_be_visible()
        page.get_by_role("button", name="用同一请求重试", exact=True).click()
        expect(page.get_by_role("heading", name="R8 手机学习项目", exact=True)).to_be_visible()
        assert len(project_requests) == 2
        assert project_requests[0] == project_requests[1]

        page.get_by_label("新会话名称").fill("第一问")
        page.get_by_role("button", name="+ 新会话", exact=True).click()
        expect(page.locator("#conversation-select")).to_be_visible()
        page.get_by_role("button", name="资料", exact=True).click()
        page.get_by_label("资料名称").fill("R8 资料")
        page.get_by_label("文档标题").fill("工作台边界")
        page.get_by_label("正文").fill("每个项目都必须隔离。")
        page.wait_for_timeout(200)
        page.get_by_role("button", name="登记资料", exact=True).click()
        try:
            expect(page.get_by_text("资料已登记，正在处理", exact=True)).to_be_visible()
        except AssertionError as caught:
            raise AssertionError({"page_errors": errors, "console": console_messages, "responses": responses, "body": page.locator("body").inner_text()}) from caught
        expect(page.locator(".source-status")).to_contain_text("已处理", timeout=15000)

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
        assert len(teaching_requests) == 2
        assert teaching_requests[0] == teaching_requests[1]
        page.get_by_role("button", name="读取原文", exact=True).click()
        expect(page.get_by_text("每个项目都必须隔离。", exact=True)).to_be_visible(timeout=10000)

        page.set_viewport_size({"width": 768, "height": 900})
        page.screenshot(path=str(artifact_dir / "workbench-tablet.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.screenshot(path=str(artifact_dir / "workbench-desktop.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not errors, errors
        browser.close()
    print("ROUND8_BROWSER_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
