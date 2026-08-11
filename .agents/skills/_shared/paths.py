"""跨 skill 共享：项目根路径定位。"""
import os
from pathlib import Path


def find_project_root() -> Path:
    """从 .agents/skills/<skill>/scripts/ 反推项目根（找 .agents 父目录）。

    优先级：CODEX_PROJECT_ROOT 环境变量 > __file__ 反推 > cwd 父目录。

    注意：合并自 6 处原实现，其中 5 处原本不带 env 优先。重构后这 5 处也会受
    CODEX_PROJECT_ROOT 影响（行为扩散）--若环境误设该 env 指向非项目根，
    会去错误目录找 raw/transcripts/notes。该 env 主要用于 Codex 协作场景。
    """
    env = os.environ.get("CODEX_PROJECT_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    try:
        idx = here.parts.index(".agents")
        return Path(*here.parts[:idx])
    except ValueError:
        return Path.cwd().parent
