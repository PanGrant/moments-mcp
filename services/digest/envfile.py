"""读 .env 文件进 os.environ（键=值 行，# 注释，值两侧引号可去）。

规则：
- 文件缺失或不可读 → 静默跳过（零第三方依赖，不影响启动）；
- **已存在的环境变量优先**——宿主（WorkBuddy/Claude/CLI）env 段
  显式传入的值不被文件覆盖；
- 调用方：scripts/feed_reader.py 与 scripts/post_test.py 的启动自举
  （LLM 钥匙只在 act 组稿时用到；MCP 壳自己不调 LLM）。

背景（MCP 发布演练发现）：此前仓库没有任何代码读 .env，钥匙只能靠
宿主 env 段传入——照 INSTALL.md 配好 .env 的新用户第一次 act 必然
鉴权失败。
"""
import os


def load_env_file(path):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass
