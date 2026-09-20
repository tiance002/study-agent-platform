"""第七轮工作台的同源静态资源门禁。"""

from pathlib import Path

from app.main import build_platform, create_app
from fastapi.testclient import TestClient


def test_workbench_and_local_react_assets_are_served_from_same_origin(tmp_path: Path):
    client = TestClient(create_app(platform=build_platform(var_dir=tmp_path)))

    page = client.get("/")
    script = client.get("/assets/app.js")
    stylesheet = client.get("/assets/app.css")
    react = client.get("/assets/vendor/react.production.min.js")

    assert page.status_code == 200
    assert "学习工作台" in page.text
    assert "/assets/app.js" in page.text
    assert script.status_code == 200
    assert "Authorization" not in script.text
    assert stylesheet.status_code == 200
    assert react.status_code == 200
