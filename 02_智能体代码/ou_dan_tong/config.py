"""模型与运行配置。Key 走环境变量或同级 .env 文件，缺省走 mock 模式（无 Key 也能测全管线）。"""
import os


def _load_env_file():
    """零依赖读取同级 .env，注入 os.environ（不覆盖已存在的环境变量）。
    便于在不污染 git / 不暴露明文到聊天的前提下本地配置 Key。"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k:
                os.environ.setdefault(k, v)


_load_env_file()

# DeepSeek（默认，OpenAI 兼容）
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# 任意 OpenAI 兼容端点（智谱 / MiniMax / 通义 等），设置后自动覆盖
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "")

# 数据文件存放目录（与本项目同级的 data 运行时目录）
DATA_DIR = os.getenv("OU_DATA_DIR", os.path.join(os.path.dirname(__file__), "runtime"))
