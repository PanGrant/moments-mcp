# moments-mcp — 朋友圈自动回复 MCP

一个 **本机 MCP 服务器**：让 Claude（或其他 MCP 宿主）替你浏览 Mac 版
微信的朋友圈，识别帖子、维护"回没回过"的帐本，并可（经你明确同意后）
自动发表评论。

> ⚠️ **先读 [INSTALL.md](INSTALL.md) 的安全警示再决定是否启用 act 模式。**
> act 模式会以你的账号**真实公开发表评论**。

## 它做什么

| 工具 | 作用 |
|---|---|
| `moments_walk` | 发起一次朋友圈走轮。`mode="readonly"`（默认）只看不评；`mode="act"` 自动回复（**真实发评论**，必须 `confirm=true`） |
| `moments_status` | 查看最近一轮 / 帐本状态（哪些帖子回过、跳过、待回） |
| `moments_stop` | 停止进行中的走轮 |
| `moments_settings_get` / `moments_settings_set` | 回复语气定制（预设 4 风格 + 自由补充）；get 同时显示 own_names |

产品规则（写死在代码里）：

- 只处理 **24 小时内**的帖子，走到"昨天"边界即停；
- 自己人的帖子永不进入回复管线（`own_names` 名单）；
- 广告帖（作者行右侧下拉箭头）识别后跳过；
- 正文信息不足的帖子不硬回（最低可懂字数门槛）；
- 每条评论自动附固定署名「AI助手胡乱解读自动回复，内容请忽略」；
- act 模式在 MCP 工具层强制确认门——没有你的明确同意不放行。

## 架构速览

三层，定义与实现分离的思路（详见源码文档串）：

1. **感知**（`services/digest/post_schema.py` + `feed_layout.py` +
   `ocr_extract.py`）：屏幕是一张"桌子"——照片列 / 正文列 / 名字行 /
   正文 / 时间行 / 评论区。所有判断来自**位置**（首次运行实测窗口几何，
   Apple Vision 本机 OCR），名字区不看内容。
2. **决策**（`services/digest/walker.py`）：帐本（`/tmp/walker_ledger.json`，
   运行时产物）记住每个帖子的身份与回没回过；跨屏认帖靠帐本，不靠精确停位。
3. **移动**（`scripts/feed_reader.py`）：纯 ↓ 键推进，每个落地问
   schema"我在哪"；唯一例外是 ↑ 门牌回攀（帖子头被跳过时显头用）。

MCP 壳 `moments_mcp.py` 是手写 stdio JSON-RPC（零第三方依赖，兼容
Python 3.9），负责单轮互斥锁、锁屏预检、caffeinate 防睡眠、SIGTERM 停止。

## 边界（明确不含）

- 不含 webapp / 会议安排（Google Calendar）/ 任何其他功能——那是父项目
  （agent-test）的另外两个案例；
- 不含任何云 OCR（读屏用 Apple Vision，本机运行，无钥匙）；
- 不含任何凭据/帐本/个人数据（全部运行时产物，已 gitignore）。

## 测试

```bash
python3 -m pytest tests/ -q
```

测试套件全部离线（真机帧 fixture + 注入式 stub，不会启动真实走轮）。

## 安装

见 [INSTALL.md](INSTALL.md)（MacBook 专用指南）。
