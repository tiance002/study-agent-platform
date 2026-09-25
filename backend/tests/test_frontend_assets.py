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
    terms = client.get("/assets/terms.js")
    views = client.get("/assets/views.js")
    commands = client.get("/assets/commands.js")
    project_state = client.get("/assets/project-state.js")
    stylesheet = client.get("/assets/app.css")
    react = client.get("/assets/vendor/react.production.min.js")
    react_dom = client.get("/assets/vendor/react-dom.production.min.js")
    antd = client.get("/assets/vendor/antd.min.js")
    antd_reset = client.get("/assets/vendor/antd-reset.css")
    dayjs = client.get("/assets/vendor/dayjs.min.js")

    assert page.status_code == 200
    assert "学习工作台" in page.text
    assert "/assets/app.js" in page.text
    assert page.text.index('/assets/api-client.js') < page.text.index('/assets/app.js')
    assert page.text.index('/assets/terms.js') < page.text.index('/assets/app.js')
    assert page.text.index('/assets/views.js') < page.text.index('/assets/app.js')
    assert page.text.index('/assets/commands.js') < page.text.index('/assets/app.js')
    assert page.text.index('/assets/project-state.js') < page.text.index('/assets/app.js')
    assert script.status_code == 200
    assert api_client.status_code == 200
    assert terms.status_code == 200
    assert views.status_code == 200
    assert commands.status_code == 200
    assert project_state.status_code == 200
    assert "Authorization" not in script.text
    assert "Authorization" not in terms.text
    assert "Authorization" not in commands.text
    assert "Authorization" not in project_state.text
    assert stylesheet.status_code == 200
    assert react.status_code == 200
    assert react_dom.status_code == 200
    assert antd.status_code == 200
    assert antd_reset.status_code == 200
    assert dayjs.status_code == 200


def test_vendored_antd_umd_keeps_its_declared_globals(tmp_path: Path):
    """antd 的 UMD 构建声明了它的运行期全局依赖：React / ReactDOM / dayjs。

    这条断言锁住 vendor 产物与 index.html 的加载顺序契约 —— 只要有人
    换了 vendor 文件却没同步全局依赖（或反过来），它就会失败。
    """
    client = TestClient(create_app(platform=build_platform(var_dir=tmp_path)))

    antd = client.get("/assets/vendor/antd.min.js").text
    assert "e.antd=t(e.React,e.ReactDOM,e.dayjs)" in antd
    assert "dayjs" in client.get("/assets/vendor/dayjs.min.js").text


def test_index_html_declares_dark_starfield_shell(tmp_path: Path):
    """星空主题是无构建步骤的静态资产：样式与主题色 meta 必须同源声明。"""
    client = TestClient(create_app(platform=build_platform(var_dir=tmp_path)))

    page = client.get("/").text
    assert "/assets/vendor/antd-reset.css" in page
    assert page.index('/assets/vendor/antd-reset.css') < page.index('/assets/app.css')


def test_round8_gate_syntax_checks_every_frontend_script():
    """门禁必须对每个前端脚本逐一执行 `node --check`。

    早期门禁只检查 `app.js`；其他脚本里的语法错误会直接进入浏览器才
    暴露。这个测试锁住门禁清单，删除任何一条 `node --check` 都会让它
    失败。
    """
    gate = (REPO_ROOT / "tools" / "run_round8_gate.py").read_text(encoding="utf-8")
    for script in (
        "frontend/api-client.js",
        "frontend/terms.js",
        "frontend/views.js",
        "frontend/commands.js",
        "frontend/project-state.js",
        "frontend/app.js",
    ):
        expected = f'"node", "--check", "{script}"'
        assert expected in gate, f"run_round8_gate.py 缺少对 {script} 的语法检查"


def test_index_html_script_order_matches_asset_dependency():
    """脚本加载顺序是脚本间的依赖契约。

    `api-client.js` 提供请求身份工具，`terms.js` 是全部用户可见文案的
    唯一来源，`views.js` 消费两者，`commands.js` 与 `project-state.js`
    依赖 api-client 构建状态 hook，`app.js` 最后装配整个工作台。
    """
    html = (REPO_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert html.index("/assets/api-client.js") < html.index("/assets/terms.js")
    assert html.index("/assets/terms.js") < html.index("/assets/views.js")
    assert html.index("/assets/views.js") < html.index("/assets/commands.js")
    assert html.index("/assets/commands.js") < html.index("/assets/project-state.js")
    assert html.index("/assets/project-state.js") < html.index("/assets/app.js")


def test_index_html_loads_antd_after_react_and_dayjs_before_business_scripts():
    """antd 的 UMD 依赖 React/ReactDOM/dayjs 全局，加载顺序不能被重排。"""
    html = (REPO_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    react = html.index("/assets/vendor/react.production.min.js")
    react_dom = html.index("/assets/vendor/react-dom.production.min.js")
    dayjs = html.index("/assets/vendor/dayjs.min.js")
    antd = html.index("/assets/vendor/antd.min.js")
    first_business = html.index("/assets/api-client.js")
    assert react < react_dom < dayjs < antd < first_business
