"""moments-mcp：朋友圈自动回复的 MCP 壳（stdio JSON-RPC，零第三方依赖）。

切片一（2026-11-17 用户选型）：moments_walk + moments_status +
moments_stop。走轮本体 feed_reader.py 一行不动——壳 = executor_host 的
spawn/停止纪律 + runs_api 的 job 模式，去掉 WebSocket 层（stdio MCP
server 自己就是脑子+手合体）。

切片二（2026-11-22）：moments_walk 开 act 档（真实公开发评论，工具层
强制 confirm 门——用户明确同意才放行）；moments_settings_get/set 接
回复内容定制（单一事实源 = services/digest/reply_settings.py，与
webapp UI 同一份文件）；act 组稿读定制（post_test.generate_fresh 注入）。

为什么手写协议不用官方 SDK：官方 mcp 包要 Python≥3.10，本机 3.9.6；
且壳要薄——需要的协议面只有 initialize / tools/list / tools/call /
ping（MCP stdio = 每行一个 JSON-RPC 2.0 消息；stdout 只走协议，
一切日志进 stderr）。

安全纪律（从 executor_host 原样继承，不许在任何改造中丢掉）：
- 同时只允许一轮——微信窗口是单机资源（"微信忙"锁跟机器走）；
- 锁屏预检（最前普通层窗口 = loginwindow 即拒发）；
- caffeinate -is 包裹（防跑一半睡着）；
- 停止 = SIGTERM 进程组（walker 每落地存帐本，最多丢当前一轮动作）；
- spawn 参数与手一致：--keep --landings N --mode <mode> --events <path>
  （--keep = 帐本续用；不带它本体清账本！--mode 由工具参数决定，
  act 必须过 confirm 门）。
"""
import json
import os
import random
import signal
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
# 从任何工作目录启动都能找到本仓库的 services/（此前依赖 cwd=仓库根）。
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from services.digest import reply_settings  # noqa: E402
MCP_HOME = os.environ.get(
    "MOMENTS_MCP_HOME", os.path.join(os.path.expanduser("~"), ".moments-mcp"))
EVENTS_DIR = os.path.join(MCP_HOME, "events")
LOGS_DIR = os.path.join(MCP_HOME, "logs")
SCRIPT = os.path.join(_HERE, "scripts", "feed_reader.py")

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "moments-mcp", "version": "0.1.0"}

TOOLS = [
    {
        "name": "moments_walk",
        "description": (
            "发起一次微信朋友圈走轮。两种模式：mode=\"readonly\"（默认）"
            "只看不评——回顶、⟳ 刷新、逐屏走到 Yesterday 边界、识别帖子"
            "并更新账本，不发表任何评论；mode=\"act\" 是自动回复——识别"
            "到帖子后**真实公开发表评论**（每条自动带固定署名「AI助手胡"
            "乱解读自动回复，内容请忽略」），必须先获得用户的明确同意才"
            "可调用（arguments 里 confirm=true，缺它会直接被拒绝——先把"
            "要做什么如实讲给用户听）。**act 还有一道前置义务：先 "
            "moments_settings_get 读当前定制（风格/补充/跳过名单/署名"
            "四项）并原样展示给用户，得到确认后才可发起**。act 默认按帐本里全部待回候选"
            "**全量**回复；想只回部分时用 only_authors 参数按**作者**"
            "挑选（只能整作者挑，没有按单帖挑选的参数，不要承诺「只回"
            "某一条」）；持久跳过名单（moments_settings_set 的 "
            "skip_authors）里的作者会被自动跳过。本工具立即返回 run_id，不等待走完"
            "；用 moments_status 查进度（建议每 5-10 秒）。⟳ 刷新链起步"
            "可长至约 4 分钟，起步 0 屏是正常现象，不要提前放弃。要走到"
            "Yesterday 边界建议 max_landings≥40。同一时刻只允许一轮（"
            "微信窗口单机独占）；要求：Mac 微信已登录、窗口未被用户占用。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string", "enum": ["readonly", "act"],
                    "description": (
                        "readonly=只看不评（默认）；act=真实公开发表"
                        "自动回复，需用户明确同意（confirm=true）"),
                    "default": "readonly",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "act 模式的用户同意标记；readonly 不需要",
                    "default": False,
                },
                "max_landings": {
                    "type": "integer",
                    "description": "最多走多少屏（默认 30；要走到 Yesterday 边界建议 ≥40）",
                    "default": 30, "minimum": 1, "maximum": 200,
                },
                "only_authors": {
                    "type": "array", "items": {"type": "string"},
                    "description": (
                        "act 模式本轮正向挑选：只对名单内作者的帖子发表"
                        "评论（作者名按 moments_status 未回清单里的写法"
                        "抄）。不传或空数组 = 不挑选，待回候选全量回复"
                        "（默认）。只对本轮有效，不改持久设置。"),
                },
            },
        },
    },
    {
        "name": "moments_status",
        "description": (
            "查询走轮进度：状态（进行中/完成/异常）、落地屏数、按键数、"
            "停止原因；act 轮附动作明细（回给谁/结果/回复摘要）；最新"
            "一轮的查询还附未回清单（帐本当前待回帖：作者+正文摘要），"
            "仅供圈定参考——圈定手段是按**作者**：act 传 only_authors "
            "只回名单内的（按单帖挑选的参数**没有**）；也**没有**视频"
            "转文字能力（视频帖只有识别到的字幕文字或〔正文无文字〕），"
            "不要向用户承诺这两类服务。"
            "不带 run_id = 查当前进行中的"
            "一轮（没有则列最近几轮）；带 run_id 可查历史任意一轮。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string",
                           "description": "要查询的运行 ID（如 r-20261117-154847-2506）"},
            },
        },
    },
    {
        "name": "moments_stop",
        "description": (
            "停止进行中的走轮（SIGTERM 进程组；walker 每落地已存账，"
            "最多丢当前一屏的动作）。没有进行中的轮则如实说明。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string",
                           "description": "要停止的运行 ID；缺省 = 当前进行中的一轮"},
            },
        },
    },
    {
        "name": "moments_settings_get",
        "description": (
            "读取回复定制全貌：语气风格 + 自由补充 + 持久跳过名单（"
            "skip_authors，名单内作者 act 永不回）+ 署名现状（可定制，"
            "清空=评论不再带 AI 披露）。act 模式组稿评论时按它写。"
            "**发起 act（自动回复）前必须先调本工具，并把风格/补充/"
            "跳过名单/署名四项原样展示给用户，得到确认后才可发起**"
            "——没有这一步就不要发 act；用户没看就发的 act 是违规。"
            "用户问「现在是什么语气」或想改定制前，也先读这个。"),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "moments_settings_set",
        "description": (
            "设置回复定制。style=单选预设（可选键与含义见 "
            "moments_settings_get 返回的风格目录）；extra=自由文本 ≤500"
            " 字，原样追加进组稿要求（如「别聊政治」「多问近况」）。"
            "skip_authors=持久跳过名单（字符串数组；空数组=清空名单"
            "恢复全回；名单内作者的帖子 act 永不回——帖留待回状态，"
            "之后从名单移除即可恢复可达）。signature_label="
            "评论署名（≤60 字）；**传空字符串=清空署名，之后发出的评论"
            "不再带「AI 助手」披露标注**——必须先向用户确认这是本意才"
            "可清。用户用自然语言描述想要的写法时（如「热情点，多问"
            "近况」）：选最接近的风格键，其余细节放 extra。只传要改的"
            "字段，未传的保持原值。保存后下一轮 act 组稿即生效。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "style": {"type": "string",
                          "description": "风格键（见 moments_settings_get 的目录）"},
                "extra": {"type": "string",
                          "description": "自由补充要求，≤500 字"},
                "skip_authors": {
                    "type": "array", "items": {"type": "string"},
                    "description": ("持久跳过名单（作者名完整写法）；空数组="
                                    "清空名单恢复默认全回"),
                },
                "signature_label": {
                    "type": "string",
                    "description": ("评论署名，≤60 字，自动追加在每条评论"
                                    "末尾；空字符串=清空（无 AI 披露，需用户"
                                    "明确确认）"),
                },
            },
        },
    },
]


def screen_locked():
    """锁屏预检（executor_host 同款）：最前普通层窗口是 loginwindow。"""
    try:
        import Quartz
        opts = Quartz.kCGWindowListOptionOnScreenOnly | \
            Quartz.kCGWindowListExcludeDesktopElements
        wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)
        for w in wins:
            if w.get("kCGWindowLayer") == 0:
                return (w.get("kCGWindowOwnerName") or "").lower() == \
                    "loginwindow"
    except Exception:
        pass
    return False


def _ledger_path():
    """帐本路径：与 feed_reader.LEDGER 同源（惰性 import，壳启动保持薄）。
    import 不了（异常环境）时退 env/默认值——只影响清单，不影响走轮。"""
    try:
        from scripts.feed_reader import LEDGER
        return LEDGER
    except Exception:
        return os.environ.get("MOMENTS_LEDGER", "/tmp/walker_ledger.json")


def _ledger_pending_text():
    """未回清单（2026-11-22 切片 2.1，"看一遍→列给用户圈定→再回"流程的
    地基）。候选 = 近 ~36h 内、status=pending、未 acted、非广告非自己
    （与 walker 一天边界的工作集对齐；再老的帐本留着但不可达，列出来
    是噪音）。持久跳过名单内的作者分流出去（2026-11-25：他们的帖会
    一直 pending，列出来是噪音；末尾一行如实交代分流了几条）。顺序 =
    帐本序（feed 新→旧），cap 15。缺文件/读不到/无候选都如实说。"""
    path = _ledger_path()
    if not os.path.exists(path):
        return "未回清单：帐本文件不在（还没成功走过一轮）。"
    try:
        with open(path) as f:
            led = json.load(f)
    except (OSError, ValueError) as e:
        return f"未回清单：帐本读不到（{e}）。"
    posts = led.get("posts", []) if isinstance(led, dict) else led
    skip = set(reply_settings.load().get("skip_authors") or [])
    now = time.time()
    fresh = [p for p in posts
             if not p.get("acted")
             and (p.get("status") or "pending") == "pending"
             and p.get("author")
             and isinstance(p.get("abs_time"), (int, float))
             and now - p["abs_time"] <= 36 * 3600]
    shunted = [p for p in fresh if p.get("author") in skip]
    fresh = [p for p in fresh if p.get("author") not in skip]
    if not fresh:
        if shunted:
            names = "、".join(sorted({p["author"] for p in shunted}))
            return ("未回清单：帐本当前没有待回帖"
                    f"（跳过名单内分流 {len(shunted)} 帖：{names}）。")
        return "未回清单：帐本当前没有待回帖。"
    lines = []
    for p in fresh[:15]:
        # time_text 带来源尾巴（真账实测："1 minute（s） ago Channels•
        # 谁的旺旺"）——清单里只留时间本身
        t = p.get("time_text") or "?"
        for mark in ("Channels", "Official Accounts"):
            i = t.find(mark)
            if i >= 0:
                t = t[:i]
        t = t.strip(" •·") or "?"
        head = (p.get("body_head") or "").strip().replace("\n", " ")
        if not head:
            # 纯图帖：真账实测存在；AI 组稿读不到文字多半会跳过，如实标
            head = "〔正文无文字〕"
        elif len(head) > 40:
            head = head[:40] + "…"
        lines.append(f"- {p['author']}（{t}）：{head}")
    more = f"\n- …另有 {len(fresh) - 15} 条" if len(fresh) > 15 else ""
    shunt_note = ""
    if shunted:
        names = "、".join(sorted({p["author"] for p in shunted}))
        shunt_note = (f"\n（另有跳过名单内 {len(shunted)} 帖未列出"
                      f"：{names}——要回他们先从名单移除）")
    return (f"未回清单（帐本当前·待回 {len(fresh)} 帖）：\n"
            + "\n".join(lines) + more + shunt_note)


def _build_cmd(mode, landings, ev_path, only_authors=None):
    """caffeinate -is 包裹（FACE_DESIGN §6）；参数与手完全一致。
    --keep 无条件带上（帐本续用；不带它本体清账本！）。
    only_authors 每作者一个 --only-author（重复参数，名字含任意字符
    都安全——不用分隔符拼接）。"""
    cmd = ["caffeinate", "-is", sys.executable, SCRIPT,
           "--keep", "--landings", str(int(landings)),
           "--mode", mode, "--events", ev_path]
    for a in (only_authors or []):
        cmd += ["--only-author", str(a)]
    return cmd


class RunManager:
    """单轮运行管理：spawn / 查状态 / 停止。磁盘即账（events 文件）。"""

    def __init__(self):
        self.proc = None
        self.run_id = None
        self.events_path = None
        self.mode = "readonly"          # 最近一次发起的模式（展示用）

    # -- helpers ------------------------------------------------------
    def _live(self):
        return (self.proc is not None and self.proc.poll() is None)

    @staticmethod
    def _newest_events_path():
        best, best_t = None, -1.0
        if os.path.isdir(EVENTS_DIR):
            for fn in os.listdir(EVENTS_DIR):
                if fn.endswith(".jsonl"):
                    p = os.path.join(EVENTS_DIR, fn)
                    mt = os.path.getmtime(p)
                    if mt > best_t:
                        best, best_t = p, mt
        return best

    def _resolve_events(self, run_id):
        """run_id → events 路径；None → 进行中的一轮，否则磁盘最新。
        显式 run_id 优先（查历史不被进行中的轮劫持）；进行中的轮即使
        文件尚未写出也返回路径（起步延迟不算"没有记录"）。"""
        if run_id:
            p = os.path.join(EVENTS_DIR, f"{run_id}.jsonl")
            if os.path.exists(p) or run_id == self.run_id:
                return p
            return None
        if self._live() and self.events_path:
            return self.events_path
        return self._newest_events_path()

    # -- tools --------------------------------------------------------
    def start(self, max_landings=30, mode="readonly", confirm=False,
              only_authors=None):
        if mode not in ("readonly", "act"):
            return ("mode 只能是 readonly 或 act。", True)
        if mode == "act" and confirm is not True:
            # confirm 门：act = 真实公开发评论，工具层强制用户同意标记。
            # 提醒调用方回到用户那边，不能靠模型自觉。
            return ("act 模式被拒绝：这会真实公开发表评论，必须用户本人"
                    "明确同意后才可调用——把要做的事如实讲给用户，得到"
                    "同意后带上 confirm=true 再来。", True)
        # only_authors 清洗（2026-11-25）：字符串数组 → 去空白/去重/封顶；
        # 传了但格式不对 = 明确报错（宿主好自查），静默吞掉会变成"说好
        # 挑选却全量回复"——那是这次要消灭的事故。
        if only_authors is not None:
            if not isinstance(only_authors, list) or any(
                    not isinstance(a, str) for a in only_authors):
                return ("only_authors 必须是字符串数组（作者名，"
                        "见 moments_status 未回清单）。", True)
            only_authors = list(dict.fromkeys(
                a.strip() for a in only_authors if a.strip()))[:50]
            if not only_authors:
                only_authors = None
        if self._live():
            return (f"微信忙：运行 {self.run_id} 进行中"
                    f"（先 moments_status 查看或 moments_stop 停止，"
                    f"再发起新一轮）。", True)
        if screen_locked():
            return ("屏幕已锁定——不发起走轮（先解锁再试）。", True)
        os.makedirs(EVENTS_DIR, exist_ok=True)
        os.makedirs(LOGS_DIR, exist_ok=True)
        run_id = time.strftime("r-%Y%m%d-%H%M%S") + \
            f"-{random.randint(0, 9999):04d}"
        ev_path = os.path.join(EVENTS_DIR, f"{run_id}.jsonl")
        log_path = os.path.join(LOGS_DIR, f"{run_id}.out.log")
        log_f = open(log_path, "w")
        try:
            self.proc = subprocess.Popen(
                _build_cmd(mode, max_landings, ev_path, only_authors),
                stdout=log_f, stderr=log_f, start_new_session=True)
        finally:
            log_f.close()
        self.run_id, self.events_path, self.mode = run_id, ev_path, mode
        label = "只读" if mode == "readonly" else "自动回复"
        pick = (f"，本轮挑选：只回 {len(only_authors)} 位作者的帖子"
                f"（{'、'.join(only_authors)}）") \
            if (mode == "act" and only_authors) else ""
        return (f"已发起{label}走轮 {run_id}（mode={mode}，"
                f"max_landings={max_landings}{pick}）。"
                f"立即返回，不等走完——用 moments_status 查进度，"
                f"需要中止用 moments_stop。", False)

    def status(self, run_id=None):
        ev = self._resolve_events(run_id)
        if not ev:
            if run_id:
                return (f"查无 {run_id} 的事件文件（事件目录：{EVENTS_DIR}）。",
                        True)
            return ("还没有任何走轮记录。用 moments_walk 发起。", False)
        rid = os.path.basename(ev)[:-len(".jsonl")]
        evs, read_err = [], None
        try:
            with open(ev) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            evs.append(json.loads(line))
                        except ValueError:
                            pass
        except FileNotFoundError:
            pass                     # 进行中的轮起步延迟——按 0 屏进行中处理
        except OSError as e:
            read_err = str(e)
        started = {}
        landings = sum(1 for e in evs if e.get("t") == "landing")
        actions = sum(1 for e in evs if e.get("t") == "action")
        summary = next((e for e in evs if e.get("t") == "summary"), None)
        if evs:
            started = evs[0]
        # 动作明细（2026-11-22 用户要的"回给谁/结果"账）：act 轮每条
        # action 事件带 author/result/reply 前 60 字；无账可记的异常
        # （action-unaccounted）一并亮出来。进行中就显示——轮询的
        # agent 能顺口转述"刚回了谁"。
        acts = [e for e in evs if e.get("t") in ("action",
                                                 "action-unaccounted")]
        detail = ""
        if acts:
            lines = []
            for e in acts[:20]:
                if e.get("t") == "action-unaccounted":
                    lines.append(f"- ⚠ {e.get('author', '?')}：发帖后无账"
                                 f"可记（{e.get('result', '?')}）")
                    continue
                line = f"- {e.get('author', '?')}：{e.get('result', '?')}"
                if e.get("deferred"):
                    line += "·deferred（等尾屏送达）"
                if e.get("error"):
                    line += f"（错：{e['error']}）"
                if e.get("reply"):
                    line += f"「{e['reply']}」"
                lines.append(line)
            if len(acts) > 20:
                lines.append(f"- …另有 {len(acts) - 20} 条")
            detail = "\n动作明细：\n" + "\n".join(lines)
        # 状态判定：summary 在 = 完整收场；无 summary 时看进程死活
        if summary:
            st = f"完成（stop_reason={summary.get('stop_reason')}）"
        elif read_err:
            st = "事件文件读不到（" + read_err + "）"
        elif self._live() and ev == self.events_path:
            st = "进行中"
        elif started.get("pid"):
            try:
                os.kill(started["pid"], 0)
                st = "进程存活但无事件更新（可疑，可 moments_stop）"
            except OSError:
                st = "进程已死且无 summary（异常退出）"
        else:
            st = "历史轮（本壳重启后无法探其进程，以文件为准）"
        t0 = started.get("ts")
        dur = (f"· 开始 {time.strftime('%H:%M:%S', time.localtime(t0))}"
               f" · 历时 {int(time.time() - t0)}s") if t0 else ""
        mode = started.get("mode") or (self.mode if self._live() else "?")
        hint = ""
        if (self._live() and ev == self.events_path and not summary
                and landings == 0 and not read_err):
            # 验收教训（2026-11-17 切片一）：刷新链可长至约 4 分钟，agent
            # 0 屏时误判卡死 SIGTERM 掉健康轮——这条 hint 挡住那个冲动。
            hint = ("\n提示：⟳ 刷新链起步可长至约 4 分钟，0 屏是正常"
                    "起步——继续用 moments_status 轮询，别停。")
        # 未回清单只挂在"最新一轮"的查询上：帐本是全局现状，只有最新
        # 轮（进行中或收场）与它对应；查历史轮附现状是张冠李戴。
        is_latest = ((self._live() and ev == self.events_path)
                     or ev == self._newest_events_path())
        ledger_txt = ("\n" + _ledger_pending_text()) if is_latest else ""
        return (f"{rid}（{mode}）状态={st}\n"
                f"落地 {landings} 屏 · 按键 {actions} · "
                f"max_landings={started.get('max_landings', '?')} {dur}"
                f"{hint}{detail}{ledger_txt}",
                False)

    def stop(self, run_id=None):
        if not self._live():
            return ("当前没有进行中的走轮。", False)
        if run_id and run_id != self.run_id:
            return (f"{run_id} 不是本壳发起的进行中运行"
                    f"（当前：{self.run_id or '无'}）——历史轮无需停止。", True)
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            return (f"已发送 SIGTERM 到 {self.run_id} 进程组"
                    f"（walker 每落地存账，最多丢当前一屏动作）。", False)
        except OSError as e:
            return (f"停止失败：{e}", True)


_mgr = RunManager()


# --------------------------------------- settings 工具（reply_settings 读写）
def settings_get_text():
    st = reply_settings.load()
    styles = "\n".join(f"  {k}: {v}"
                       for k, v in reply_settings.STYLES.items())
    extra = (st.get("extra") or "").strip()
    names = st.get("own_names") or []
    skip = st.get("skip_authors") or []
    sig = reply_settings.resolve_signature()
    if "signature_label" in st:
        sig_line = ("署名："
                    + (f"「{sig}」" if sig
                       else "【已清空——之后发出的评论不再带 AI 披露标注】"))
    else:
        sig_line = f"署名：「{sig}」（默认值，可用 signature_label 定制；" \
                   "清空=评论不再带 AI 披露，需用户明确确认）"
    return ("当前定制：style=" + st["style"] + "；"
            + (f"extra=「{extra}」" if extra else "无自由补充") + "\n"
            + "自己人名单（own_names）："
            + (", ".join(names) if names
               else "【未设置——自己的帖子将不被识别，实跑前必须设置！"
                    "编辑 data/moments_settings.json 加 own_names 字段】") + "\n"
            + "持久跳过名单（skip_authors）："
            + (", ".join(skip) if skip
               else "【空——默认全回；要永久不回某些作者，用 "
                    "moments_settings_set 的 skip_authors 设置】") + "\n"
            + sig_line + "\n"
            + "风格目录：\n" + styles, False)


def settings_set_text(args):
    if not isinstance(args, dict):
        return ("参数必须是对象。", True)
    given = [k for k in ("style", "extra", "skip_authors", "signature_label")
             if k in args]
    if not given:
        return ("没给要改的字段：style / extra / skip_authors / "
                "signature_label 至少传一个。", True)
    cur = reply_settings.load()
    style = args.get("style") or cur["style"]
    extra = args.get("extra") if "extra" in args else cur["extra"]
    err = reply_settings.validate(style, extra or "")
    if err:
        return (f"设置被拒绝：{err}（可选风格见 moments_settings_get）。",
                True)
    skip = None
    if "skip_authors" in args:
        skip = args.get("skip_authors")
        if not isinstance(skip, list) or any(
                not isinstance(a, str) for a in skip):
            return ("skip_authors 必须是字符串数组（空数组=清空名单）。",
                    True)
        skip = list(dict.fromkeys(
            a.strip() for a in skip if a.strip()))
        if len(skip) > reply_settings.SKIP_AUTHORS_MAX:
            return (f"skip_authors 最多 {reply_settings.SKIP_AUTHORS_MAX} "
                    "人。", True)
    sig_given = "signature_label" in args
    sig = ""
    if sig_given:
        sig = str(args.get("signature_label") or "").strip()
        if len(sig) > reply_settings.SIGNATURE_MAX:
            return (f"signature_label 最长 {reply_settings.SIGNATURE_MAX} "
                    "字。", True)
    reply_settings.save(style, extra or "", skip_authors=skip,
                        signature_label=sig if sig_given else reply_settings.KEEP)
    st = reply_settings.load()
    extra2 = (st.get("extra") or "").strip()
    skip2 = st.get("skip_authors") or []
    sig2 = reply_settings.resolve_signature()
    parts = ["已保存：style=" + st["style"] + "；"
             + (f"extra=「{extra2}」" if extra2 else "无自由补充")]
    parts.append("跳过名单=" + (", ".join(skip2) if skip2 else "【空，全回】"))
    if "signature_label" in st:
        parts.append("署名=" + (f"「{sig2}」" if sig2
                                else "【已清空——评论不再带 AI 披露标注】"))
    warn = ""
    if sig_given and not sig:
        warn = ("\n⚠ 署名已清空：之后发出的评论不再带「AI 助手」披露"
                "标注，看评论的人无法知道这是自动回复——如非本意，"
                "马上把 signature_label 设回默认文本。")
    return ("；".join(parts) + "——下一轮 act 组稿即生效"
            "（readonly 不组稿，不受影响）。" + warn, False)


# ------------------------------------------------------------- 协议层
def handle(msg, mgr=None):
    """一条 JSON-RPC 消息 → 应答（或 None=通知不回）。可注入 mgr 测试。"""
    mgr = mgr or _mgr
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return None
    rid = msg.get("id")
    method = msg.get("method")
    if rid is None:                     # 通知：不应答
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": code, "message": message}}

    try:
        if method == "initialize":
            params = msg.get("params") or {}
            return ok({
                # 客户端驱动协商：回它请求的版本，缺省给已知版
                "protocolVersion": params.get("protocolVersion")
                or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "选用时机：用户提到朋友圈、微信好友动态，或想「看一圈"
                    "/刷一下/回一下/自动回复」朋友圈时，直接使用本服务器"
                    "的工具——无需用户点名工具名或服务器名。"
                    "自动回复类请求的固定流程：先用 moments_settings_"
                    "get 读当前回复定制，把风格/补充/跳过名单/署名四项"
                    "**原样给用户看**，问要不要改（要改就用 "
                    "moments_settings_set 存）并得到确认 → mode=readonly "
                    "走一圈 → moments_status 等完成（最新一轮完成态附"
                    "未回清单）→ 把清单列给用户（共几条、都是谁），问"
                    "是否都回 → 用户确认后再 mode=act + confirm=true "
                    "走一遍（组稿按刚保存的指引写）；只想回其中某些"
                    "**作者**时给 act 传 only_authors（按单帖挑选没有）。"
                    "没让用户看过四项定制就发 act 是违规流程。"
                    "moments_walk 发起后立即返回 run_id"
                    "——不要原地等待，用 moments_status 每 5-10 秒轮询并"
                    "向用户转述进度；必要时 moments_stop。mode=readonly "
                    "只看不评；mode=act 会真实公开发评论（每条自动带署名"
                    "——署名可在 settings 定制或清空，清空前必须用户"
                    "明确确认），必须先获得用户明确同意（confirm=true），"
                    "且建议 max_landings≥40 才能走到边界；⟳ 刷新链起步"
                    "可长至约 4 分钟，0 屏不是卡死。回复语气/补充要求/"
                    "跳过名单/署名用 moments_settings_get/set 读写。"),
            })
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": TOOLS})
        if method == "tools/call":
            name = (msg.get("params") or {}).get("name")
            args = (msg.get("params") or {}).get("arguments") or {}
            if name == "moments_walk":
                text, is_err = mgr.start(
                    max_landings=int(args.get("max_landings") or 30),
                    mode=args.get("mode") or "readonly",
                    confirm=bool(args.get("confirm")),
                    only_authors=args.get("only_authors"))
            elif name == "moments_status":
                text, is_err = mgr.status(args.get("run_id"))
            elif name == "moments_stop":
                text, is_err = mgr.stop(args.get("run_id"))
            elif name == "moments_settings_get":
                text, is_err = settings_get_text()
            elif name == "moments_settings_set":
                text, is_err = settings_set_text(args)
            else:
                # 未知工具走工具级错误（模型能看见），不走协议级错误
                return ok({"content": [{"type": "text",
                                        "text": f"Unknown tool: {name}"}],
                           "isError": True})
            return ok({"content": [{"type": "text", "text": text}],
                       "isError": is_err})
        return err(-32601, f"Method not found: {method}")
    except Exception as e:              # 工具内任何异常都不许崩协议层
        return err(-32603, f"Internal error: {e}")


def serve(stdin=None, stdout=None):
    """stdio 主循环：每行一条 JSON-RPC。stdout 只走协议，日志全走 stderr。"""
    fin, fout = stdin or sys.stdin, stdout or sys.stdout
    print(f"[moments-mcp] home={MCP_HOME} script={SCRIPT}", file=sys.stderr)
    for line in fin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            print(f"[moments-mcp] 丢弃非 JSON 行", file=sys.stderr)
            continue
        resp = handle(msg)
        if resp is not None:
            fout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            fout.flush()


if __name__ == "__main__":
    serve()
