"""moments-mcp 壳（2026-11-17 切片一）：协议层 / 工具派发 / 运行管理。

走轮本体不动——测的是壳：initialize 协商、tools/list、tools/call 派发
（可注入假 manager）、通知不回、单轮锁、锁屏拒发、事件文件解析、
SIGTERM 进程组停止、历史轮查询。真实微信走轮不在测试里（那要真微信
+真授权，是用户的验收环节）。
"""
import io
import json
import os
import subprocess
import tempfile
import time
import unittest

import moments_mcp as mm


def _req(id_, method, params=None):
    msg = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


class FakeManager:
    def __init__(self):
        self.calls = []

    def start(self, max_landings=30, mode="readonly", confirm=False):
        self.calls.append(("start", max_landings, mode, confirm))
        return ("假发起 ok", False)

    def status(self, run_id=None):
        self.calls.append(("status", run_id))
        return ("假状态 ok", False)

    def stop(self, run_id=None):
        self.calls.append(("stop", run_id))
        return ("假停止 ok", False)


class TestProtocol(unittest.TestCase):
    def test_initialize_echoes_client_version(self):
        r = mm.handle(_req(1, "initialize", {
            "protocolVersion": "2025-06-18",
            "clientInfo": {"name": "workbuddy"}}))
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(r["result"]["serverInfo"]["name"], "moments-mcp")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertIn("moments_status", r["result"]["instructions"])

    def test_initialize_default_version(self):
        r = mm.handle(_req(2, "initialize", {}))
        self.assertEqual(r["result"]["protocolVersion"], mm.PROTOCOL_VERSION)

    def test_tools_list_five_tools_act_gated(self):
        r = mm.handle(_req(3, "tools/list"))
        names = [t["name"] for t in r["result"]["tools"]]
        self.assertEqual(names, ["moments_walk", "moments_status",
                                 "moments_stop", "moments_settings_get",
                                 "moments_settings_set"])
        walk = r["result"]["tools"][0]
        # readonly 仍声明不评论；act 声明真实公开发 + confirm 门
        self.assertIn("不发表任何评论", walk["description"])
        self.assertIn("真实公开发表评论", walk["description"])
        props = walk["inputSchema"]["properties"]
        self.assertEqual(props["mode"]["enum"], ["readonly", "act"])
        self.assertIn("confirm", props)

    def test_act_without_confirm_is_refused(self):
        # 只测门本身，不碰 spawn：confirm 缺失/False 都拦在第一道
        #（confirm=True 的放行路径在 TestRunManager 里用假 cmd 验证，
        # 这里真 spawn 会打出真评论——2026-11-17 冒烟事故的教训）
        mgr = mm.RunManager()
        text, is_err = mgr.start(mode="act")
        self.assertTrue(is_err)
        self.assertIn("act 模式被拒绝", text)
        self.assertIsNone(mgr.proc)          # 门拦在 spawn 之前
        text3, _ = mgr.start(mode="act", confirm=False)
        self.assertIn("act 模式被拒绝", text3)
        text4, is_err4 = mgr.start(mode="turbo")
        self.assertTrue(is_err4)

    def test_walk_dispatch_carries_mode_and_confirm(self):
        fake = FakeManager()
        mm.handle(_req(30, "tools/call",
                       {"name": "moments_walk",
                        "arguments": {"mode": "act", "confirm": True,
                                      "max_landings": 40}}), fake)
        self.assertEqual(fake.calls[0], ("start", 40, "act", True))
        mm.handle(_req(31, "tools/call",
                       {"name": "moments_walk", "arguments": {}}), fake)
        self.assertEqual(fake.calls[1], ("start", 30, "readonly", False))

    def test_ping_and_notification(self):
        self.assertEqual(mm.handle(_req(4, "ping"))["result"], {})
        self.assertIsNone(mm.handle(  # 通知（无 id）不应答
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_method_and_tool(self):
        self.assertEqual(
            mm.handle(_req(5, "resources/list"))["error"]["code"], -32601)
        r = mm.handle(_req(6, "tools/call",
                           {"name": "nope", "arguments": {}}))
        self.assertEqual(r["result"]["isError"], True)

    def test_tool_call_dispatches_to_manager(self):
        fake = FakeManager()
        r = mm.handle(_req(7, "tools/call",
                           {"name": "moments_walk",
                            "arguments": {"max_landings": 12}}), fake)
        self.assertEqual(fake.calls, [("start", 12, "readonly", False)])
        self.assertEqual(r["result"]["content"][0]["text"], "假发起 ok")
        self.assertIs(r["result"]["isError"], False)
        mm.handle(_req(8, "tools/call",
                       {"name": "moments_status",
                        "arguments": {"run_id": "r-x"}}), fake)
        mm.handle(_req(9, "tools/call",
                       {"name": "moments_stop", "arguments": {}}), fake)
        self.assertEqual(fake.calls[1], ("status", "r-x"))
        self.assertEqual(fake.calls[2], ("stop", None))

    def test_garbage_line_and_internal_error_never_crash(self):
        r = mm.handle({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                       "params": {"name": "moments_walk",
                                  "arguments": {"max_landings": "abc"}}})
        # int("abc") 在 try 外？—— 在 mgr.start 的参数换算里，异常被
        # 协议层兜住
        self.assertEqual(r["error"]["code"], -32603)

    def test_settings_tools_roundtrip_and_validation(self):
        # 定制与 webapp 同一份存储（reply_settings.SETTINGS_PATH 换到
        # tmp 隔离）；署名固定必须出现在 get 的回话里
        from services.digest import reply_settings as rs
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)
        old = rs.SETTINGS_PATH
        rs.SETTINGS_PATH = path
        try:
            r = mm.handle(_req(40, "tools/call",
                               {"name": "moments_settings_set",
                                "arguments": {"style": "warm",
                                              "extra": "多问近况"}}))
            self.assertEqual(r["result"]["isError"], False)
            self.assertIn("已保存", r["result"]["content"][0]["text"])
            r2 = mm.handle(_req(41, "tools/call",
                                {"name": "moments_settings_get",
                                 "arguments": {}}))
            t2 = r2["result"]["content"][0]["text"]
            self.assertIn("style=warm", t2)
            self.assertIn("多问近况", t2)
            self.assertIn("AI助手胡乱解读", t2)
            r3 = mm.handle(_req(42, "tools/call",
                                {"name": "moments_settings_set",
                                 "arguments": {"extra": "少用 emoji"}}))
            self.assertEqual(r3["result"]["isError"], False)
            r4 = mm.handle(_req(43, "tools/call",
                                {"name": "moments_settings_get",
                                 "arguments": {}}))
            t4 = r4["result"]["content"][0]["text"]
            self.assertIn("style=warm", t4)      # 只传 extra：style 保持
            self.assertIn("少用 emoji", t4)
            r5 = mm.handle(_req(44, "tools/call",
                                {"name": "moments_settings_set",
                                 "arguments": {"style": "poetic"}}))
            self.assertEqual(r5["result"]["isError"], True)
            r6 = mm.handle(_req(45, "tools/call",
                                {"name": "moments_settings_set",
                                 "arguments": {}}))
            self.assertEqual(r6["result"]["isError"], True)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
            rs.SETTINGS_PATH = old

    def test_serve_loop_writes_only_responses_to_stdout(self):
        lines = "\n".join([
            json.dumps(_req(1, "ping")),
            "not json at all",
            json.dumps({"jsonrpc": "2.0",
                        "method": "notifications/initialized"}),
            "",
        ]) + "\n"
        out = io.StringIO()
        mm.serve(stdin=io.StringIO(lines), stdout=out)
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(len(replies), 1)          # 通知/坏行不产生应答
        self.assertEqual(replies[0]["id"], 1)


class TestRunManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = (mm.EVENTS_DIR, mm.LOGS_DIR, mm._build_cmd,
                     mm.screen_locked, mm._ledger_path)
        mm.EVENTS_DIR = os.path.join(self.tmp, "events")
        mm.LOGS_DIR = os.path.join(self.tmp, "logs")
        mm.screen_locked = lambda: False
        mm._build_cmd = lambda m, l, e: ["sleep", "30"]
        # 帐本指到 tmp 不存在文件——测试密不透风，不吃本机真帐本
        mm._ledger_path = lambda: os.path.join(self.tmp, "ledger.json")

    def tearDown(self):
        (mm.EVENTS_DIR, mm.LOGS_DIR, mm._build_cmd,
         mm.screen_locked, mm._ledger_path) = self._old
        self._kill_leftovers()

    def _kill_leftovers(self):
        for p in (self.mgr.proc if hasattr(self, "mgr") else None,
                  getattr(self, "extra_proc", None)):
            if p is not None and p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), 9)
                except OSError:
                    pass

    def test_start_busy_lock_and_lockscreen_refusal(self):
        mm._build_cmd = lambda m, l, e: ["sleep", "30"]
        mgr = self.mgr = mm.RunManager()
        text, is_err = mgr.start()
        self.assertFalse(is_err)
        self.assertIn("已发起", text)
        self.assertTrue(mgr._live())
        text2, is_err2 = mgr.start()          # 第二发起 = 微信忙
        self.assertTrue(is_err2)
        self.assertIn("微信忙", text2)
        mm.screen_locked = lambda: True
        mgr2 = mm.RunManager()
        text3, is_err3 = mgr2.start()
        self.assertTrue(is_err3)
        self.assertIn("屏幕已锁定", text3)

    def test_status_running_then_summary_complete(self):
        mm._build_cmd = lambda m, l, e: ["sleep", "30"]
        mgr = self.mgr = mm.RunManager()
        mgr.start()
        text, is_err = mgr.status()
        self.assertFalse(is_err)
        self.assertIn("进行中", text)
        self.assertIn("落地 0 屏", text)
        # 刷新链提示：进行中+0 屏+无 summary → 别停（验收教训）
        self.assertIn("别停", text)
        # 假 events 文件补写 run_start+landing+summary → 完成态
        with open(mgr.events_path, "a") as f:
            f.write(json.dumps({"t": "run_start", "mode": "readonly",
                                "max_landings": 30, "pid": 1,
                                "ts": time.time()}) + "\n")
            f.write(json.dumps({"t": "landing", "n": 1, "ts": time.time()}) + "\n")
            f.write(json.dumps({"t": "landing", "n": 2, "ts": time.time()}) + "\n")
            f.write(json.dumps({"t": "action", "author": "张三",
                                "result": "ok", "deferred": False,
                                "reply": "这趟爬山看着真痛快",
                                "ts": time.time()}) + "\n")
            f.write(json.dumps({"t": "action", "author": "李四",
                                "result": "skipped-too-short",
                                "deferred": False, "reply": None,
                                "ts": time.time()}) + "\n")
            f.write(json.dumps({"t": "summary", "stop_reason": "boundary",
                                "landings": 2, "presses": 2, "acted": 2,
                                "replied_ok": 1, "ts": time.time()}) + "\n")
        text2, _ = mgr.status(mgr.run_id)
        self.assertIn("完成（stop_reason=boundary）", text2)
        self.assertIn("落地 2 屏", text2)
        self.assertNotIn("别停", text2)      # 收场后提示消失
        # 动作明细账（2026-11-22）：谁/结果/回复摘要
        self.assertIn("动作明细", text2)
        self.assertIn("张三：ok「这趟爬山看着真痛快」", text2)
        self.assertIn("李四：skipped-too-short", text2)

    def test_act_with_confirm_spawns_act_mode_and_sigterm_stops(self):
        # confirm 门放行路径：mode 原样进 spawn 参数（--mode act），
        # 且与只读轮共用同一套停止纪律
        seen = {}

        def fake_cmd(mode, landings, ev_path):
            seen["mode"] = mode
            return ["sleep", "30"]

        mm._build_cmd = fake_cmd
        mgr = self.mgr = mm.RunManager()
        text, is_err = mgr.start(mode="act", confirm=True, max_landings=40)
        self.assertFalse(is_err)
        self.assertIn("自动回复走轮", text)
        self.assertEqual(seen["mode"], "act")
        text2, _ = mgr.stop()
        self.assertIn("SIGTERM", text2)
        deadline = time.time() + 3
        while mgr.proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(mgr.proc.poll())

    def test_stop_sigterm_process_group(self):
        mm._build_cmd = lambda m, l, e: ["sleep", "30"]
        mgr = self.mgr = mm.RunManager()
        mgr.start()
        text, is_err = mgr.stop()
        self.assertFalse(is_err)
        self.assertIn("SIGTERM", text)
        deadline = time.time() + 3
        while mgr.proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(mgr.proc.poll(), "进程组未被 SIGTERM 终止")

    def test_stop_without_live_run_is_honest(self):
        mgr = self.mgr = mm.RunManager()
        text, is_err = mgr.stop()
        self.assertFalse(is_err)
        self.assertIn("没有进行中", text)

    def test_status_unknown_run_id_and_empty_history(self):
        mgr = self.mgr = mm.RunManager()
        text, is_err = mgr.status()
        self.assertFalse(is_err)
        self.assertIn("还没有任何走轮记录", text)
        text2, is_err2 = mgr.status("r-does-not-exist")
        self.assertTrue(is_err2)
        self.assertIn("查无", text2)

    def test_status_tolerates_phase_events(self):
        # 2026-11-22 起 run_start 提前 + 刷新链带 phase 事件——status
        # 只认 landing/action/summary，其余类型必须安静略过
        mm._build_cmd = lambda m, l, e: ["sleep", "30"]
        mgr = self.mgr = mm.RunManager()
        mgr.start()
        with open(mgr.events_path, "a") as f:
            f.write(json.dumps({"t": "run_start", "mode": "readonly",
                                "max_landings": 30, "pid": 1,
                                "ts": time.time()}) + "\n")
            f.write(json.dumps({"t": "phase", "step": "refresh",
                                "status": "CONFIRMED"}) + "\n")
            f.write(json.dumps({"t": "phase", "step": "settle-wait",
                                "seconds": 20}) + "\n")
        text, is_err = mgr.status()
        self.assertFalse(is_err)
        self.assertIn("进行中", text)
        self.assertIn("落地 0 屏", text)
        self.assertIn("别停", text)

    def test_action_unaccounted_surfaces_in_detail(self):
        # 无账可记是账本断裂警报——status 里必须亮出来，不许静默
        os.makedirs(mm.EVENTS_DIR, exist_ok=True)
        rid = "r-unaccounted"
        with open(os.path.join(mm.EVENTS_DIR, rid + ".jsonl"), "w") as f:
            f.write(json.dumps({"t": "run_start", "mode": "act",
                                "max_landings": 40, "pid": 1,
                                "ts": time.time()}) + "\n")
            f.write(json.dumps({"t": "action-unaccounted", "author": "王五",
                                "result": "ok", "ts": time.time()}) + "\n")
        mgr = self.mgr = mm.RunManager()
        text, _ = mgr.status(rid)
        self.assertIn("⚠ 王五：发帖后无账可记", text)

    def test_historical_run_without_summary_reports_dead_process(self):
        os.makedirs(mm.EVENTS_DIR, exist_ok=True)
        p = subprocess.Popen(["sleep", "0"])   # 即刻死亡的真实 pid
        p.wait()
        rid = "r-hist-dead"
        with open(os.path.join(mm.EVENTS_DIR, rid + ".jsonl"), "w") as f:
            f.write(json.dumps({"t": "run_start", "mode": "readonly",
                                "max_landings": 30, "pid": p.pid,
                                "ts": time.time()}) + "\n")
        mgr = self.mgr = mm.RunManager()
        text, _ = mgr.status(rid)
        self.assertIn("异常退出", text)


class TestLedgerPendingList(unittest.TestCase):
    """未回清单（切片 2.1）："看一遍→列给用户圈定→再回"的地基。
    帐本内容密不透风（tmp 假帐本），不吃本机真账。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ledger = os.path.join(self.tmp, "ledger.json")
        self._old = (mm.EVENTS_DIR, mm.LOGS_DIR, mm._ledger_path)
        mm.EVENTS_DIR = os.path.join(self.tmp, "events")
        mm.LOGS_DIR = os.path.join(self.tmp, "logs")
        mm._ledger_path = lambda: self.ledger

    def tearDown(self):
        (mm.EVENTS_DIR, mm.LOGS_DIR, mm._ledger_path) = self._old

    @staticmethod
    def _post(**kw):
        base = {"author": "张三", "status": "pending", "acted": False,
                "abs_time": time.time() - 3600,
                "time_text": "1 hour(s) ago",
                "body_head": "爬山归来，腿已经不是自己的了"}
        base.update(kw)
        return base

    def _write_ledger(self, posts):
        with open(self.ledger, "w") as f:
            json.dump({"posts": posts, "sightings": []}, f,
                      ensure_ascii=False)

    def _write_run(self, rid):
        os.makedirs(mm.EVENTS_DIR, exist_ok=True)
        with open(os.path.join(mm.EVENTS_DIR, rid + ".jsonl"), "w") as f:
            f.write(json.dumps({"t": "run_start", "mode": "readonly",
                                "max_landings": 30, "pid": 1,
                                "ts": time.time()}) + "\n")
            f.write(json.dumps({"t": "summary", "stop_reason": "boundary",
                                "landings": 1, "presses": 1, "acted": 0,
                                "replied_ok": 0,
                                "ts": time.time()}) + "\n")

    def test_candidate_filter_and_format(self):
        self._write_run("r-ok")
        self._write_ledger([
            self._post(),                                              # 待回 ✓
            self._post(author="老帖",
                       abs_time=time.time() - 3 * 86400),              # 超窗 ✗
            self._post(author="广告机", status="skipped_ad"),            # 广告 ✗
            self._post(author="我自己", status="own"),                   # 自己 ✗
            self._post(author="李四", acted=True),                       # 已回 ✗
            self._post(author="王五", time_text="2 hour(s) ago",
                       body_head="中午吃了碗牛肉面\n加了个蛋"),          # 待回 ✓
        ])
        text, _ = mm.RunManager().status()
        self.assertIn("未回清单（帐本当前·待回 2 帖）", text)
        self.assertIn("张三（1 hour(s) ago）：爬山归来", text)
        self.assertIn("王五", text)
        self.assertIn("牛肉面", text)          # 换行压掉、摘要保留
        self.assertNotIn("老帖", text)
        self.assertNotIn("广告机", text)
        self.assertNotIn("李四", text)

    def test_list_only_attaches_to_latest_run(self):
        # 帐本是全局现状：只配最新一轮；查历史轮附现状 = 张冠李戴
        self._write_run("r-old")
        self._write_run("r-new")
        self._write_ledger([self._post()])
        mgr = mm.RunManager()
        t_new, _ = mgr.status("r-new")
        self.assertIn("未回清单", t_new)
        t_old, _ = mgr.status("r-old")
        self.assertNotIn("未回清单", t_old)

    def test_missing_and_empty_ledger_are_honest(self):
        self._write_run("r-run")
        mgr = mm.RunManager()
        text, _ = mgr.status()
        self.assertIn("帐本文件不在", text)     # 从没成功走过一轮
        self._write_ledger([self._post(acted=True)])
        text2, _ = mgr.status()
        self.assertIn("帐本当前没有待回帖", text2)

    def test_time_pollution_and_empty_head(self):
        # 真账 2026-11-22 实测形状：time_text 混来源尾巴；纯图帖无摘要
        self._write_run("r-run")
        self._write_ledger([
            self._post(time_text="1 minute（s） ago Channels• 谁的旺旺"),
            self._post(author="纯图君", body_head=""),
        ])
        text, _ = mm.RunManager().status()
        self.assertIn("张三（1 minute（s） ago）：", text)   # 尾巴被剪掉
        self.assertNotIn("旺旺", text)
        self.assertIn("纯图君", text)
        self.assertIn("〔正文无文字〕", text)

    def test_cap_at_fifteen(self):
        self._write_run("r-run")
        self._write_ledger([self._post(author=f"用户{i}",
                                        abs_time=time.time() - 60 * (i + 1))
                            for i in range(16)])
        text, _ = mm.RunManager().status()
        self.assertIn("待回 16 帖", text)
        self.assertIn("…另有 1 条", text)


if __name__ == "__main__":
    unittest.main()
