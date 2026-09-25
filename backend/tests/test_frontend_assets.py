"""第七轮工作台的同源静态资源门禁。"""

from pathlib import Path

from app.main import build_platform, create_app
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_workbench_and_local_react_assets_are_served_from_same_origin(tmp_path: Path):
    client = TestClient(create_app(platform=build_platform(var_dir=tmp_path)))

    page = client.get("/")
    script = client.get("/assets/app.js")
    api_client = client.get("/assets/api-client.js")
    views = client.get("/assets/views.js")
    commands = client.get("/assets/commands.js")
    project_state = client.get("/assets/project-state.js")
    stylesheet = client.get("/assets/app.css")
    react = client.get("/assets/vendor/react.production.min.js")

    assert page.status_code == 200
    assert "学习工作台" in page.text
    assert "/assets/app.js" in page.text
    assert page.text.index('/assets/api-client.js') < page.text.index('/assets/app.js')
    assert page.text.index('/assets/views.js') < page.text.index('/assets/app.js')
    assert page.text.index('/assets/commands.js') < page.text.index('/assets/app.js')
    assert page.text.index('/assets/project-state.js') < page.text.index('/assets/app.js')
    assert script.status_code == 200
    assert api_client.status_code == 200
    assert views.status_code == 200
    assert commands.status_code == 200
    assert project_state.status_code == 200
    assert "Authorization" not in script.text
    assert "Authorization" not in commands.text
    assert "Authorization" not in project_state.text
    assert stylesheet.status_code == 200
    assert react.status_code == 200


def test_round8_gate_syntax_checks_every_frontend_script():
    """门禁必须对每个前端脚本逐一执行 `node --check`。

    早期门禁只检查 `app.js`；其他脚本里的语法错误会直接进入浏览器才
    暴露。这个测试锁住门禁清单，删除任何一条 `node --check` 都会让它
    失败。
    """
    gate = (REPO_ROOT / "tools" / "run_round8_gate.py").read_text(encoding="utf-8")
    for script in (
        "frontend/api-client.js",
        "frontend/views.js",
        "frontend/commands.js",
        "frontend/project-state.js",
        "frontend/app.js",
    ):
        expected = f'"node", "--check", "{script}"'
        assert expected in gate, f"run_round8_gate.py 缺少对 {script} 的语法检查"


def test_index_html_script_order_matches_asset_dependency():
    """脚本加载顺序是脚本间的依赖契约。

    `api-client.js` 提供请求身份工具，`commands.js` 与 `project-state.js`
    依赖它构建状态 hook，`app.js` 最后装配整个工作台。
    """
    html = (REPO_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert html.index("/assets/api-client.js") < html.index("/assets/views.js")
    assert html.index("/assets/views.js") < html.index("/assets/commands.js")
    assert html.index("/assets/commands.js") < html.index("/assets/project-state.js")
    assert html.index("/assets/project-state.js") < html.index("/assets/app.js")
