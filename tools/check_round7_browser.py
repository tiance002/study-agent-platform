"""Browser smoke test for the ordinary-user workbench."""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    artifact_dir = Path("var") / "round7-preview"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        launch_options = {"headless": True}
        cached_chromium = Path.home() / "AppData" / "Local" / "ms-playwright" / "chromium-1234" / "chrome-win64" / "chrome.exe"
        if cached_chromium.exists():
            launch_options["executable_path"] = str(cached_chromium)
        browser = playwright.chromium.launch(**launch_options)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        page.screenshot(path=str(artifact_dir / "desktop.png"), full_page=True)
        assert page.get_by_role("heading", name="把下一步学什么，变成今天能完成的事").is_visible()

        page.get_by_label("邀请访问码").fill("round7-preview-invite")
        page.get_by_role("button", name="进入学习空间").click()
        page.get_by_role("heading", name="演示学习项目").wait_for()

        page.get_by_label("新建项目").fill("浏览器验收项目")
        page.get_by_placeholder("项目目标（可选）").fill("验证普通用户学习闭环")
        page.get_by_role("button", name="+ 创建项目").click()
        page.get_by_role("heading", name="浏览器验收项目").wait_for()

        page.get_by_label("新会话名称").fill("第一次提问")
        page.get_by_role("button", name="+ 新会话").click()
        page.get_by_role("button", name="资料").click()
        page.get_by_label("资料名称").fill("浏览器资料")
        page.get_by_label("文档标题").fill("边界说明")
        page.get_by_label("正文").fill("工具调用需要清晰的权限边界。")
        page.get_by_role("button", name="登记资料").click()
        page.get_by_text("资料已登记，正在等待处理").wait_for()

        page.get_by_role("button", name="计划").click()
        page.get_by_label("学习目标").fill("能够解释学习平台的安全边界")
        page.get_by_label("第一个里程碑").fill("完成 Cookie 会话与项目隔离练习")
        page.get_by_role("button", name="保存计划").click()
        page.get_by_text("计划已保存").wait_for()

        page.get_by_role("button", name="会话", exact=True).click()
        page.get_by_label("输入问题").fill("为什么需要项目隔离？")
        page.get_by_role("button", name="发送问题").click()
        page.get_by_text("教学服务尚未启用").wait_for()

        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        mobile.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        mobile.screenshot(path=str(artifact_dir / "mobile.png"), full_page=True)
        assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        browser.close()
    print("ROUND7_BROWSER_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
