"""跨 skill 共享：环境变量 + .env 加载。"""
import os
import sys
from pathlib import Path


def _env_hint(env_path: Path) -> str:
    """报错文案里的 .env 位置提示。

    ⚠️ 2026-10-06 修：原文案让用户 `cp .env.example .env`（当前目录），
    但本模块只读 `script_dir/.env` —— 照做会把文件放到一个**永远不会被读**的
    位置，于是「配了 key 还是报同一个错」，极难自查。改为打印真实生效路径。
    同目录下确有配套模板 `.env.example`（注意它与仓库根目录那份**内容不同**，
    以脚本目录下这份为准）。
    """
    return f"cp {env_path.parent}/.env.example {env_path}"


def load_env(script_dir: Path) -> None:
    """读 script_dir/.env 到 os.environ（不覆盖已有值）+ 校验 LLM_API_KEY。

    5 处原实现行为完全一致：读 .env + key 缺失或为占位符则 sys.exit。
    """
    env_path = script_dir / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                # 剥离成对引号（KEY="v" / KEY='v'），与常见 shell .env 语义一致
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                os.environ.setdefault(k.strip(), v)
    # ⚠️ 2026-10-06 标注（**不要**当 bug「修」）：下面只硬读通用名
    # `LLM_API_KEY`，不认任何厂商专有名 —— 这是**本开源分支有意为之的差异**。
    # 本仓库只开源 rss，面向的是任意用户，不该把别人绑到某一家服务上。
    # 主项目那份同名模块走 `env_get()` 支持「通用名 + 专有名」双命名兜底，
    # 因为那里要同时服务多个源、且已内定用某家服务。
    # `_shared` 在两个仓库里是两份独立拷贝，**这就是它们该有的差异**，
    # 下次同步时不要把这边改成双命名。
    if "LLM_API_KEY" not in os.environ or "your-api" in os.environ.get("LLM_API_KEY", ""):
        sys.exit(
            "ERROR: LLM_API_KEY 未配置。请：\n"
            f"  1. {_env_hint(env_path)}\n"
            "  2. 编辑该文件填入你的 LLM 服务 API key（OpenAI 兼容接口）\n"
            "  或设置环境变量：export LLM_API_KEY=your-key"
        )
    if "LLM_BASE_URL" not in os.environ:
        sys.exit(
            "ERROR: LLM_BASE_URL 未配置。请：\n"
            f"  1. {_env_hint(env_path)}\n"
            "  2. 编辑该文件设置 LLM_BASE_URL（你的 LLM 服务 OpenAI 兼容接口地址）\n"
            "  或设置环境变量：export LLM_BASE_URL=https://your-llm-service/v1"
        )
    if "LLM_MODEL" not in os.environ:
        sys.exit(
            "ERROR: LLM_MODEL 未配置。请：\n"
            f"  1. {_env_hint(env_path)}\n"
            "  2. 编辑该文件设置 LLM_MODEL（你的模型名，必填——"
            "此前模板误标为可选，实际所有脚本都必读）\n"
            "  或设置环境变量：export LLM_MODEL=your-model"
        )
