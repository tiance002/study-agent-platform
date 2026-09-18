"""在 git 传输通道不可用时，改用 GitHub REST API 推送。

**为什么需要这个脚本**：某些网络环境下 `git push` 走的 HTTPS 通道会被代理拒绝
（`CONNECT tunnel failed`），但 `gh api` 的通道是可用的。此时可以用 Git Data API
（blobs → tree → commit → ref）把整个提交一次性推送上去。

相比"逐个文件用 Contents API 创建"，这种方式产出的是**一个完整提交**，
而不是几十个碎提交。

用法：

```bash
python tools/push_via_api.py --repo tiance002/study-agent-platform --branch main
python tools/push_via_api.py --repo owner/name --dry-run
```

前置：`gh` 已登录且具备 `repo` 权限。
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_git(*args: str) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{result.stderr.decode(errors='replace')}")
    return result.stdout


def gh_api(method: str, endpoint: str, payload: dict | None = None, *, attempts: int = 3) -> dict:
    """调用 gh api。失败时有限重试，不吃掉错误。"""
    command = ["gh", "api", "--method", method, endpoint]
    body = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    if body is not None:
        command += ["--input", "-"]

    last_error = ""
    for attempt in range(1, attempts + 1):
        result = subprocess.run(command, input=body, capture_output=True, text=True)
        if result.returncode == 0:
            return json.loads(result.stdout) if result.stdout.strip() else {}
        last_error = result.stderr.strip()
        if attempt < attempts:
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"gh api {method} {endpoint} 连续 {attempts} 次失败：{last_error}")


def head_commit_message() -> str:
    return run_git("log", "-1", "--pretty=%B").decode("utf-8").rstrip()


def head_commit_author() -> tuple[str, str]:
    name = run_git("log", "-1", "--pretty=%an").decode().strip() or "unknown"
    email = run_git("log", "-1", "--pretty=%ae").decode().strip() or "unknown@example.com"
    return name, email


def branch_exists(repo: str, branch: str) -> bool:
    result = subprocess.run(
        ["gh", "api", f"/repos/{repo}/git/ref/heads/{branch}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def tracked_files() -> list[str]:
    """列出当前提交跟踪的文件。用 NUL 分隔，避免中文路径被截断。"""
    raw = run_git("ls-files", "-z")
    return [item for item in raw.decode("utf-8").split("\0") if item]


def blob_content(path: str) -> bytes:
    """从 git 对象库读取内容，保证与本地提交完全一致（不受工作区行尾影响）。"""
    return run_git("cat-file", "blob", f"HEAD:{path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用 GitHub Git Data API 推送当前 HEAD")
    parser.add_argument("--repo", required=True, help="形如 owner/name")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    files = tracked_files()
    message = head_commit_message()
    author_name, author_email = head_commit_author()
    print(f"[info] 待推送文件 {len(files)} 个，分支 {args.branch}")
    if args.dry_run:
        for path in files[:10]:
            print(f"       {path}")
        print("       ...")
        return 0

    # 0) 空仓库无法直接使用 Git Data API：blob 接口会返回 409 "Git Repository is empty"。
    #    先用 Contents API 落一个初始提交把分支建起来，随后的完整树会覆盖它。
    if not branch_exists(args.repo, args.branch):
        print(f"[info] 仓库为空，先建立分支 {args.branch}")
        gh_api(
            "PUT",
            f"/repos/{args.repo}/contents/.gitkeep",
            {
                "message": "chore: 初始化仓库",
                "content": base64.b64encode(b"\n").decode("ascii"),
                "branch": args.branch,
            },
        )

    # 1) 逐个上传 blob
    tree_entries: list[dict] = []
    for index, path in enumerate(files, start=1):
        data = blob_content(path)
        response = gh_api(
            "POST",
            f"/repos/{args.repo}/git/blobs",
            {
                "content": base64.b64encode(data).decode("ascii"),
                "encoding": "base64",
            },
        )
        tree_entries.append(
            {"path": path, "mode": "100644", "type": "blob", "sha": response["sha"]}
        )
        if index % 10 == 0 or index == len(files):
            print(f"       已上传 {index}/{len(files)}")

    # 2) 建树
    tree = gh_api("POST", f"/repos/{args.repo}/git/trees", {"tree": tree_entries})
    print(f"[info] tree={tree['sha']}")

    # 3) 建提交
    commit = gh_api(
        "POST",
        f"/repos/{args.repo}/git/commits",
        {
            "message": message,
            "tree": tree["sha"],
            "author": {"name": author_name, "email": author_email},
            "committer": {"name": author_name, "email": author_email},
        },
    )
    print(f"[info] commit={commit['sha']}")

    # 4) 更新分支引用。优先 PATCH（分支已存在），失败再尝试创建。
    #    用 force=True：本脚本产出的提交没有 parent（是快照式推送），不是快进。
    patched = subprocess.run(
        [
            "gh", "api", "--method", "PATCH",
            f"/repos/{args.repo}/git/refs/heads/{args.branch}",
            "--input", "-",
        ],
        input=json.dumps({"sha": commit["sha"], "force": True}),
        capture_output=True,
        text=True,
    )
    if patched.returncode == 0:
        print(f"[info] 已更新分支 {args.branch}")
    else:
        gh_api(
            "POST",
            f"/repos/{args.repo}/git/refs",
            {"ref": f"refs/heads/{args.branch}", "sha": commit["sha"]},
        )
        print(f"[info] 已创建分支 {args.branch}")

    print(f"[ok]   推送完成：https://github.com/{args.repo}/commit/{commit['sha']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
