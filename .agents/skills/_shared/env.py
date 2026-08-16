"""跨 skill 共享：环境变量 + .env 加载。"""
import os
import sys
from pathlib import Path


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
    if "LLM_API_KEY" not in os.environ or "your-api" in os.environ.get("LLM_API_KEY", ""):
        sys.exit(
            "ERROR: LLM_API_KEY 未配置。请：\n"
            "  1. cp .env.example .env\n"
            "  2. 编辑 .env 填入你的 LLM 服务 API key（OpenAI 兼容接口）\n"
            "  或设置环境变量：export LLM_API_KEY=your-key"
        )
    if "LLM_BASE_URL" not in os.environ:
        sys.exit(
            "ERROR: LLM_BASE_URL 未配置。请：\n"
            "  1. cp .env.example .env\n"
            "  2. 编辑 .env 设置 LLM_BASE_URL（你的 LLM 服务 OpenAI 兼容接口地址）\n"
            "  或设置环境变量：export LLM_BASE_URL=https://your-llm-service/v1"
        )
