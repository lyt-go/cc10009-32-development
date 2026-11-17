"""配置同步域服务层回归测试（仅依赖标准库，无需起 HTTP 服务）。

直接驱动 ConfigsyncService / WorkerService，覆盖：握手与离线门禁、完整应用校验、
幂等与并发唯一、版本冲突分批、断点续传、容量侧回写与执行侧采集、审计留痕。

运行：cd backend && .venv/bin/python -m unittest tests.test_configsync_domain -v
"""
from __future__ import annotations

import threading
import unittest

from app.services.configsync import ConfigsyncService, SyncRuleError
from app.services.worker import WorkerService
from app.store import store

ADMIN = {"name": "测试管理员", "role": "管理员"}


class ConfigsyncDomainTest(unittest.TestCase):
    def setUp(self) -> None:
        store.__init__()  # 每个用例从干净种子重建内存表
        self.sync = ConfigsyncService()
        self.worker = WorkerService()

    def _task_id(self, code: str) -> int:
        return next(row["id"] for row in store.rows("configsync") if row["任务编号"] == code)

    def _worker_id(self, code: str) -> int:
        return next(row["id"] for row in store.rows("worker") if row["节点编号"] == code)

    def _act(self, code: str, action: str, values: dict | None = None, actor=ADMIN):
        return self.sync.run_action(self._task_id(code), action, values or {}, actor)

    # 1. 未握手不能确认完成
    def test_cannot_complete_before_handshake(self):
        with self.assertRaises(SyncRuleError) as ctx:
            self._act("CONF-0003", "提交同步结果")
        self.assertIn("握手", str(ctx.exception))

    # 2. 目标离线不能提前确认完成
    def test_offline_target_cannot_complete(self):
        with self.assertRaises(SyncRuleError) as ctx:
            self._act("CONF-0002", "提交同步结果")
        self.assertIn("离线", str(ctx.exception))

    # 3. 恢复→断点续传→提交：唯一结果 + 幂等 + 容量侧回写
    def test_resume_then_complete_is_idempotent_and_writes_back(self):
        self._act("CONF-0002", "节点恢复")
        msg = self._act("CONF-0002", "断点续传")[1]
        self.assertIn("第 3 段", msg)  # 种子已应用 [1,2]，应从第 3 段续传
        self._act("CONF-0002", "提交同步结果")
        # 重复提交：仍成功，但不产生第二条结果
        repeat = self._act("CONF-0002", "提交同步结果")[1]
        self.assertIn("已存在", repeat)
        results = [r for r in self.sync.list_results("CONF-0002") if r["结果状态"] == "完成"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["提交次数"], 2)
        node = next(n for n in store.rows("worker") if n["节点编号"] == "WORK-0002")
        self.assertEqual(node["当前配置版本号"], "cfg-v3")
        self.assertEqual(node["关联同步结果"], results[0]["结果编号"])

    # 4. 并发提交只产生一个完成结果
    def test_concurrent_submit_single_result(self):
        code = "CONF-T"
        self.sync.create_entry(
            {"任务编号": code, "源节点": "WORK-0003", "目标节点": "WORK-0001"}, ADMIN)
        self._act(code, "发起握手")
        self._act(code, "握手确认")
        for _ in range(4):
            self._act(code, "应用分段")
        errors: list[str] = []

        def submit():
            try:
                self._act(code, "提交同步结果")
            except SyncRuleError as exc:  # 竞争失败者应收到规则错误而非产生脏数据
                errors.append(str(exc))

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        done = [r for r in self.sync.list_results(code) if r["结果状态"] == "完成"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["提交次数"], 8)  # 8 次提交都记在同一结果上

    # 5. 版本冲突：两源版本落不同批次，冲突态不能完成
    def test_version_conflict_splits_batches(self):
        code = "CONF-V"
        self.sync.create_entry(
            {"任务编号": code, "源节点": "WORK-0003", "目标节点": "WORK-0001"}, ADMIN)
        self._act(code, "发起握手")
        self._act(code, "握手确认")
        self._act(code, "应用分段")
        self._act(code, "应用分段")
        old_batch = next(r for r in store.rows("configsync") if r["任务编号"] == code)["批次号"]
        self._act(code, "发布源新版本", {"新版本号": "cfg-v4"})
        with self.assertRaises(SyncRuleError) as ctx:
            self._act(code, "应用分段")
        self.assertIn("版本冲突", str(ctx.exception))
        task = next(r for r in store.rows("configsync") if r["任务编号"] == code)
        self.assertEqual(task["status"], "版本冲突")
        with self.assertRaises(SyncRuleError):
            self._act(code, "提交同步结果")
        audits = [a for a in self.sync.list_audits(code) if a["动作"] == "版本冲突"]
        self.assertTrue(audits)
        self.assertEqual(audits[0]["批次号"], old_batch)  # 旧批次绑定旧版本
        self.assertEqual(audits[0]["源版本"], "cfg-v3")

    # 6. 差异摘要列未应用项，失败原因进审计
    def test_diff_lists_unapplied_and_failure_audited(self):
        code = "CONF-D"
        # 先释放 WORK-0002 上的两个种子活动任务（CONF-0002 中断、CONF-0003 待握手）
        for seed_code in ("CONF-0002", "CONF-0003"):
            self.sync.run_action(self._task_id(seed_code), "标记失败", {"失败原因": "测试释放"}, ADMIN)
        self.sync.create_entry(
            {"任务编号": code, "源节点": "WORK-0003", "目标节点": "WORK-0002"}, ADMIN)
        self.worker.run_action(self._worker_id("WORK-0002"), "标记在线")  # 干净种子里该节点离线
        self._act(code, "发起握手")
        self._act(code, "握手确认")
        self._act(code, "应用分段")
        self.worker.run_action(self._worker_id("WORK-0002"), "标记离线")
        self._act(code, "标记中断")
        task = next(r for r in store.rows("configsync") if r["任务编号"] == code)
        self.assertIn("未应用项", task["差异摘要"])
        audits = [a for a in self.sync.list_audits(code) if a["动作"] == "分段中断"]
        self.assertTrue(audits[0]["失败原因"])
        self.assertEqual(len(audits[0]["未应用项"]), 3)

    # 7. 同一目标活动任务唯一
    def test_duplicate_active_task_rejected(self):
        with self.assertRaises(SyncRuleError) as ctx:
            self.sync.create_entry(
                {"任务编号": "CONF-DUP", "源节点": "WORK-0003", "目标节点": "WORK-0002"}, ADMIN)
        self.assertIn("活动任务", str(ctx.exception))

    # 8. 容量侧回写幂等 + 执行侧配置节点清单采集三要素
    def test_worker_writeback_and_config_node_collection(self):
        node, msg = self.worker.apply_sync_result("WORK-0001")
        self.assertTrue(node is not None)
        self.assertEqual(node["当前配置版本号"], "cfg-v3")
        _, msg2 = self.worker.apply_sync_result("WORK-0001")
        self.assertIn("无需重复", msg2)
        nodes = self.worker.collect_config_nodes()
        row = next(n for n in nodes if n["节点编号"] == "WORK-0001")
        self.assertTrue(row["当前配置版本号"] and row["差异摘要"] and row["最后同步时间"])

    # 9. 每个关键动作都留审计痕迹
    def test_audit_trail_records_actor_and_versions(self):
        self._act("CONF-0002", "节点恢复", actor={"name": "张三", "role": "管理员"})
        self._act("CONF-0002", "断点续传", actor={"name": "张三", "role": "管理员"})
        self._act("CONF-0002", "提交同步结果", actor={"name": "张三", "role": "管理员"})
        submit = [a for a in self.sync.list_audits("CONF-0002")
                  if a["动作"] == "提交同步结果" and a["关联结果编号"]]
        self.assertTrue(submit)
        self.assertEqual(submit[0]["发起人"], "张三")
        self.assertEqual((submit[0]["目标前版本"], submit[0]["目标后版本"]), ("cfg-v1", "cfg-v3"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
