# 安装指南（MacBook）

## ⚠️ 使用前必读（安全警示）

1. **act 模式会以你的微信账号真实公开发表评论**，评论公开可见、无法
   自动撤回。每条评论带固定署名声明是 AI 自动内容。启用前请确认你
   接受这一行为及其在目标社交圈的影响。
2. **平台风险自担**：本项目通过 GUI 自动化操作 Mac 版微信，可能不符
   合微信的使用条款，账号风险由你自己评估。
3. **隐私**：act 模式会把帖子文字内容发送给你配置的 LLM 服务（用于
   生成回复）；readonly 模式识别全程在本机完成（Apple Vision OCR），
   不联网、不调用 LLM。除此之外没有任何数据上传。
4. **不要把 `act` 当无人值守机器人**：设计上每轮都要你在场（MCP 确认
   门就是为此），跑完后看一眼 `moments_status`。

## 0. 前提

- macOS + 已登录的 Mac 版微信（客户端在前台可用）
- Python 3.9+（`python3 --version`）
- 一个大模型 API 钥匙。**不需要 OpenAI**：默认直连智谱 GLM
  （open.bigmodel.cn，国内可用），DeepSeek/Kimi/通义千问等说同一套
  接口协议的服务都能用——`openai` 只是那个协议的免费客户端库，不是
  OpenAI 服务本身（详见 `.env.example`）

## 1. 获取代码并安装依赖

```bash
git clone <本仓库地址> moments-mcp
cd moments-mcp
pip3 install -r requirements.txt
```

## 2. 给系统权限（两处，缺一不可）

**系统设置 → 隐私与安全性 →**

- **辅助功能**：把你运行 MCP 的宿主 App 勾上（终端/iTerm/Claude 等）；
- **屏幕录制**：同上。

验证（终端里跑，输出 True 即通过）：

```bash
python3 -c "from services.digest.scroller import accessibility_trusted; print(accessibility_trusted())"
```

## 3. 配置钥匙和自己的名字

```bash
cp .env.example .env
# 编辑 .env，填入 LLM_API_KEY
```

**设置 own_names（必做）**——你自己微信上的名字（显示在朋友圈自己
帖子作者行的名字，可含多个账号名，逗号分隔）。没有它，自己的帖子不
被识别，实跑可能回复到自己的动态：

```bash
mkdir -p data
cat > data/moments_settings.json <<'EOF'
{
 "style": "friend",
 "extra": "",
 "own_names": ["你的微信名字"]
}
EOF
```

（之后可用 `moments_settings_get` 查看确认。语气风格/自由补充可用
`moments_settings_set` 调整。）

## 4. 接入 Claude

Claude Code：

```bash
claude mcp add moments -- python3 /完整路径/moments-mcp/moments_mcp.py
```

Claude Desktop（`claude_desktop_config.json`）：

```json
{"mcpServers": {"moments": {
  "command": "python3",
  "args": ["/完整路径/moments-mcp/moments_mcp.py"]}}}
```

## 5. 第一次跑（务必先只看不评）

1. 让 Claude 调用 `moments_walk`（**不带 act**，即 readonly 模式）：
   只翻阅、识别、记帐，不发任何评论。
2. 结束后调用 `moments_status` 检查：帖子识别是否正常、own_names 有
   没生效（自己的帖子应标记为 own 跳过）。
3. 一切正常后，且你**明确同意**时，才让 Claude 调用
   `moments_walk mode="act" confirm=true`。没有 confirm=true 的 act
   请求会被工具直接拒绝。

## 6. 数据都在哪（全本机）

| 数据 | 位置 | 性质 |
|---|---|---|
| 帐本（回没回过） | `/tmp/walker_ledger.json` | 运行时产生，重启即清 |
| 每轮事件/日志 | `~/.moments-mcp/` | 运行时产生 |
| 回复定制 + own_names | 仓库内 `data/moments_settings.json` | 你手动设置 |
| 条目数据库 | 仓库内 `data/` | 运行时产生 |

全部不在 git 仓库里（`.gitignore` 已墙住）。

## 7. 故障排查

| 症状 | 处理 |
|---|---|
| `accessibility_trusted()` False | 权限没给到正确的宿主 App；给完需重启终端 |
| 找不到微信窗口 | 微信需登录且未被最小化到 Dock；窗口别开全屏独占 |
| 识别出 0 帖 | 检查屏幕录制权限；窗口太小会量不出列（建议默认大小以上） |
| LLM 报 401/403 | `.env` 的 LLM_API_KEY 没填对，或 BASE_URL 与钥匙不匹配 |
| 想从头再来 | 删 `/tmp/walker_ledger.json`（帐本即清） |
