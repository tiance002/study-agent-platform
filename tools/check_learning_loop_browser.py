"""学习闭环真实旅程浏览器回归（诊断 → 生成计划 → 任务流转 → 自报提交 → 重开读回）。

旅程（全部走真实 HTTP + 本地预览，不做路由桩）：

1. 注册新账号（开放注册 + 6-12 位密码）并创建一个带目标的学习项目；
2. 在「计划」页提交基础诊断，断言诊断表单可提交且结论回显；
3. 生成带任务的计划：断言界面出现 3 个以上任务，且任务标题既不等价、
   也不包含用户目标原文（防止"任务复述目标"回归），并断言任务卡片展示了
   objective / deliverable / acceptance_criteria 详情字段；
4. 任务流转 pending → in_progress，在 in_progress 上提交自报反馈
   （后端只在进行中允许提交，见 `loop_store.submit_self_report`），
   断言界面把自报表述为"已记录，不等于自动评分"，且该任务**不显示为已验证**；
   再把任务流转到 done；
5. 390px 视口无横向溢出，并把截图写入 var/round8-browser-gate/；
6. 刷新页面后读回：计划、任务状态、**提交记录**都在界面上重建；提交的
   持久化事实另以真实 HTTP 读回交叉验证（任务详情 `self_reported=true` /
   `verified=false`、`GET .../submissions` 命中原文）。

成功打印 LEARNING_LOOP_BROWSER_PASSED。

历史说明：本脚本最初发现"刷新后前端不重建提交列表"（`submissionsByTask`
是组件内状态，而刷新只重拉 `/plan` 与 `/diagnosis`），当时改用 HTTP 读回
持久化事实、并在报告中如实记录缺口；前端随后补齐了读回逻辑，因此第 6 步
已升级为**界面断言**，HTTP 读回保留为交叉验证。
"""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

#: 目标里带 "Agent" 关键词，命中确定性领域模板（任务标题来自模板，不抄目标）。
GOAL = "能独立搭建并调试一个 Agent 工具调用闭环"
PROJECT_NAME = "学习闭环验收项目"
SELF_REPORT = "我画了一轮运行的时序图，并标注了每一步的输入与输出。"
# 11 个字符：落在统一密码策略 6-12 之内。
# 值里带 dev-only 标记：这是浏览器夹具的本地占位凭据，不是真实密钥
# （项目秘密扫描要求占位值显式标注，见 tools/security/scan_secrets.py）。
PASSWORD = "dev-only-pw"
#: 任务卡片必须展示的详情字段标签（objective / deliverable / acceptance_criteria）。
TASK_FIELD_LABELS = ("学习目标", "要交出的东西", "完成标准")
#: 自报反馈的诚实标注（terms.submission.selfReportNotice 的关键子串）。
SELF_REPORT_NOTICE = "已记录，不等于自动评分"


def read_json(page, path: str) -> dict:
    """在当前页面同源发起真实 GET，复用会话 cookie 读回服务端事实。"""
    return page.evaluate(
        """async (path) => {
          const response = await fetch(path, {
            credentials: "same-origin",
            headers: { Accept: "application/json" },
          });
          const text = await response.text();
          return { status: response.status, body: text ? JSON.parse(text) : null };
        }""",
        path,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8008")
    parser.add_argument("--artifacts", default="var/round8-browser-gate")
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

        # 1) 注册新账号并创建带目标的项目。
        page.get_by_role("tab", name="注册账号").click()
        page.get_by_label("用户名").fill("loopuser" + uuid.uuid4().hex[:6])
        page.get_by_label("密码").fill(PASSWORD)
        page.get_by_role("button", name="创建账号").click()
        expect(page.locator(".project-create summary")).to_be_visible(timeout=15000)
        page.locator(".project-create summary").click()
        page.get_by_label("项目名称").fill(PROJECT_NAME)
        page.get_by_placeholder("项目目标（可选）").fill(GOAL)
        page.get_by_role("button", name="+ 创建项目", exact=True).click()
        expect(page.get_by_role("heading", name=PROJECT_NAME, exact=True)).to_be_visible(timeout=15000)

        # 2) 计划页：提交诊断（诊断表单可提交且结论回显）。
        page.get_by_role("button", name="计划", exact=True).click()
        expect(page.locator(".diagnosis-form")).to_be_visible(timeout=15000)
        page.locator("#diagnosis-hours").fill("10")
        page.get_by_role("button", name="保存诊断").click()
        diagnosis_summary = page.locator(".diagnosis-summary")
        expect(diagnosis_summary).to_be_visible(timeout=15000)
        expect(diagnosis_summary).to_contain_text("10 小时/周")

        # 3) 生成计划：任务数、标题不复述目标、详情字段可见。
        page.get_by_role("button", name="生成学习计划", exact=True).click()
        cards = page.locator(".task-card")
        expect(cards.first).to_be_visible(timeout=15000)
        task_count = cards.count()
        assert task_count >= 3, f"生成计划只出现 {task_count} 个任务，应为 3 个以上"
        titles = page.locator(".task-title").all_inner_texts()
        for title in titles:
            assert title.strip() != GOAL, f"任务标题复述了目标原文：{title!r}"
            assert GOAL not in title, f"任务标题包含目标原文：{title!r}"
        first_card = cards.first
        for label in TASK_FIELD_LABELS:
            expect(first_card).to_contain_text(label, timeout=10000)
        target_title = first_card.locator(".task-title").inner_text()
        page.screenshot(path=str(artifact_dir / "learning-loop-plan-390.png"), full_page=True)

        # 4) 任务流转 + 自报提交。后端只在"进行中"允许提交，所以顺序是
        #    pending → in_progress →（自报）→ done。
        first_card.get_by_role("button", name="开始任务", exact=True).click()
        expect(first_card.locator(".task-status-tag")).to_contain_text("进行中", timeout=10000)
        first_card.get_by_role("button", name="提交学习反馈", exact=True).click()
        page.get_by_label("写下你的完成情况").fill(SELF_REPORT)
        page.get_by_role("button", name="提交反馈", exact=True).click()
        expect(page.get_by_text("学习反馈已记录", exact=True)).to_be_visible(timeout=15000)
        submission_list = first_card.locator(".submission-list")
        expect(submission_list).to_be_visible(timeout=10000)
        expect(submission_list).to_contain_text(SELF_REPORT_NOTICE)
        expect(submission_list).to_contain_text(SELF_REPORT)
        # 自报只被记录：任务仍显示"未验证"，绝不因自报变绿。
        card_head = first_card.locator(".task-card-head")
        expect(card_head).to_contain_text("未验证")
        assert "已验证" not in card_head.inner_text(), card_head.inner_text()
        first_card.get_by_role("button", name="标记完成", exact=True).click()
        expect(first_card.locator(".task-status-tag")).to_contain_text("已完成", timeout=10000)

        # 5) 390px 无横向溢出 + 截图。
        assert page.evaluate("innerWidth === 390 && document.documentElement.scrollWidth <= innerWidth + 1"), {
            "overflow": page.evaluate(
                "() => ({scroll_width: document.documentElement.scrollWidth, inner_width: innerWidth})"
            ),
            "body": page.locator("body").inner_text(),
        }
        page.screenshot(path=str(artifact_dir / "learning-loop-submitted-390.png"), full_page=True)

        # 6) 刷新后读回。
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(2000)
        # 前端只在存在会话时才持久化所选项目（app.js 的 selection useEffect），
        # 本旅程没有建会话，所以刷新后要像真实用户一样重新选中项目。
        page.get_by_role("button", name=PROJECT_NAME, exact=True).click()
        expect(page.get_by_role("heading", name=PROJECT_NAME, exact=True)).to_be_visible(timeout=15000)
        page.get_by_role("button", name="计划", exact=True).click()
        expect(cards.first).to_be_visible(timeout=15000)
        assert cards.count() == task_count, cards.count()
        reloaded_card = page.locator(".task-card", has_text=target_title).first
        expect(reloaded_card.locator(".task-status-tag")).to_contain_text("已完成")
        expect(page.locator(".diagnosis-summary")).to_contain_text("10 小时/周")
        reloaded_head = reloaded_card.locator(".task-card-head")
        expect(reloaded_head).to_contain_text("未验证")
        assert "已验证" not in reloaded_head.inner_text(), reloaded_head.inner_text()
        # 提交记录必须由**界面**读回：前端在项目数据加载后按任务重建提交列表，
        # 否则"重开读回"在 UI 上不成立（这是本脚本最初发现的 P1，已修复）。
        expect(reloaded_card.locator(".submission-list")).to_contain_text(SELF_REPORT, timeout=15000)
        expect(reloaded_card.locator(".submission-list")).to_contain_text("不等于自动评分")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.screenshot(path=str(artifact_dir / "learning-loop-reloaded-390.png"), full_page=True)

        # 提交记录的持久化事实另用真实 HTTP 读回，作为界面断言之外的交叉验证。
        projects = read_json(page, "/projects")
        assert projects["status"] == 200, projects
        project = next(
            item for item in projects["body"]["projects"] if item["name"] == PROJECT_NAME
        )
        project_id = project["project_id"]
        plan = read_json(page, f"/projects/{project_id}/plan")
        assert plan["status"] == 200, plan
        assert len(plan["body"]["tasks"]) == task_count, len(plan["body"]["tasks"])
        done_tasks = [task for task in plan["body"]["tasks"] if task["status"] == "done"]
        assert len(done_tasks) == 1, done_tasks
        task_id = done_tasks[0]["task_id"]
        detail = read_json(page, f"/projects/{project_id}/tasks/{task_id}")
        assert detail["status"] == 200, detail
        assert detail["body"]["status"] == "done", detail["body"]
        assert detail["body"]["self_reported"] is True, detail["body"]
        assert detail["body"]["verified"] is False, detail["body"]
        submissions = read_json(page, f"/projects/{project_id}/tasks/{task_id}/submissions")
        assert submissions["status"] == 200, submissions
        assert any(
            item["content"] == SELF_REPORT for item in submissions["body"]["submissions"]
        ), submissions["body"]

        assert not errors, errors

        genuine_console_errors = [
            message
            for message in console_messages
            # "Failed to load resource" 是网络层日志（如 favicon 404），不是 JS 错误。
            if message.startswith("error:") and "Failed to load resource" not in message
        ]
        if genuine_console_errors:
            raise AssertionError(
                {
                    "console_errors": genuine_console_errors,
                    "console": console_messages[-20:],
                    "responses": responses[-20:],
                }
            )
        browser.close()
    print("LEARNING_LOOP_BROWSER_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
