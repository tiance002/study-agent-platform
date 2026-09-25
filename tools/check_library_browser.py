"""知识库真实旅程浏览器回归（用户级资料库 → 关联到当前项目）。

旅程（全部走真实 HTTP，不做路由桩）：
1. 注册并创建一个学习项目；
2. 在左栏知识库面板用「粘贴文本」添加一份库资料；
3. 用「关联到当前项目」把它关联进当前项目；
4. 切到项目「资料」页，确认该项目资料列表出现了这份库资料；
5. 390px 视口无横向溢出，且全程没有 pageerror。

成功打印 LIBRARY_BROWSER_PASSED。

依赖：`/library/sources`、`POST /projects/{id}/library-sources/{id}/attach`
由并行的后端轨道提供；后端未就绪时本脚本会失败，这属于集成阶段验证。
"""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[1]

LIBRARY_NAME = "库资料：事务入门指南"
LIBRARY_TEXT = (
    "事务把一组读写绑定成一个原子单元。隔离级别决定并发事务互相看得到什么，"
    "而可重复读与读已提交的差异只在并发写冲突时才显现。"
)
# 11 个字符：落在统一密码策略 6-12 之内。
# 值里带 dev-only 标记：这是浏览器夹具的本地占位凭据，不是真实密钥
# （项目秘密扫描要求占位值显式标注，见 tools/security/scan_secrets.py）。
PASSWORD = "dev-only-pw"


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

        page.get_by_role("tab", name="注册账号").click()
        page.get_by_label("用户名").fill("libuser" + uuid.uuid4().hex[:6])
        page.get_by_label("密码").fill(PASSWORD)
        page.get_by_role("button", name="创建账号").click()
        expect(page.locator(".project-create summary")).to_be_visible(timeout=10000)
        expect(page.locator(".library-section")).to_be_visible()

        project_name = "知识库联调项目"
        page.locator(".project-create summary").click()
        page.get_by_label("项目名称").fill(project_name)
        page.get_by_role("button", name="+ 创建项目", exact=True).click()
        expect(page.get_by_role("heading", name=project_name, exact=True)).to_be_visible(timeout=15000)

        # 1) 添加库资料（粘贴文本路径）
        page.get_by_label("条目名称").fill(LIBRARY_NAME)
        page.get_by_label("粘贴文本（可选）").fill(LIBRARY_TEXT)
        page.get_by_role("button", name="添加到知识库", exact=True).click()
        expect(page.get_by_text("库资料已添加", exact=True)).to_be_visible(timeout=15000)
        library_item = page.locator(".library-item").filter(has_text=LIBRARY_NAME)
        expect(library_item).to_be_visible(timeout=10000)
        expect(library_item.get_by_text("已有正文", exact=True)).to_be_visible()

        # 2) 关联到当前项目
        library_item.get_by_role("button", name="关联到当前项目", exact=True).click()
        expect(page.get_by_text("已关联到当前项目", exact=True)).to_be_visible(timeout=15000)

        # 3) 当前项目资料列表出现这份库资料
        page.get_by_role("button", name="资料", exact=True).click()
        project_sources = page.locator(".source-list:not(.candidate-list)")
        expect(project_sources.get_by_text(LIBRARY_NAME, exact=True)).to_be_visible(timeout=15000)
        expect(project_sources.locator(".source-status").last).to_be_visible()

        assert page.evaluate("innerWidth === 390 && document.documentElement.scrollWidth <= innerWidth + 1"), {
            "overflow": page.evaluate(
                "() => ({scroll_width: document.documentElement.scrollWidth, inner_width: innerWidth})"
            ),
            "body": page.locator("body").inner_text(),
        }
        page.screenshot(path=str(artifact_dir / "library-attached-390.png"), full_page=True)

        genuine_console_errors = [
            message
            for message in console_messages
            if message.startswith("error:") and "Failed to load resource" not in message
        ]
        if errors or genuine_console_errors:
            raise AssertionError(
                {
                    "page_errors": errors,
                    "console_errors": genuine_console_errors,
                    "console": console_messages[-20:],
                    "responses": responses[-20:],
                }
            )
        browser.close()
    print("LIBRARY_BROWSER_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
