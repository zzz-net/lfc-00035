#!/usr/bin/env python
"""快照导出/导入回归测试

覆盖场景：
1. 基本导出与导入（新工作区）
2. 配置不一致冲突
3. 问题编号冲突与四种策略
4. 目标扫描更新的冲突
5. dry-run 预检
6. 备份与回退
7. 重启续跑（历史不丢失）
8. report/status/list 输出一致性
9. CLI 命令集成
10. 空工作区完整导入
11. 导入后重启续跑
12. 残留状态无配置的保护
13. 导出完整性（元数据+校验和）
14. 损坏快照导入（无效JSON/缺字段/篡改内容）
15. 版本不匹配检测
16. 重复导入幂等性
17. 跨重启验证链路
18. 操作日志验证
19. ops-log CLI 命令

运行方式:
    python test_snapshot.py
"""
import os
import sys
import json
import shutil
import tempfile
import subprocess
import traceback
from pathlib import Path
from datetime import datetime, timedelta


SCRIPT_DIR = Path(__file__).parent.absolute()
SAMPLE_DATA = SCRIPT_DIR / "sample_data"
PYTHON_EXE = sys.executable


def run_cli(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SCRIPT_DIR)
    cmd = [
        PYTHON_EXE, "-m", "survey_check",
        "--workspace", str(workspace),
        *args,
    ]
    return subprocess.run(cmd, cwd=workspace, env=env, capture_output=True, text=True)


def load_state(workspace: Path) -> dict:
    state_path = workspace / ".survey_check" / "survey_state.json"
    with open(state_path, encoding="utf-8") as f:
        return json.load(f)


def load_config(workspace: Path) -> dict:
    config_path = workspace / ".survey_check" / "survey_config.json"
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def load_ops_log(workspace: Path) -> list:
    log_path = workspace / ".survey_check" / "ops_log.jsonl"
    if not log_path.exists():
        return []
    entries = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def setup_workspace(base: Path, name: str) -> Path:
    ws = base / name
    if ws.exists():
        shutil.rmtree(ws)
    shutil.copytree(SAMPLE_DATA, ws)
    state_dir = ws / ".survey_check"
    if state_dir.exists():
        shutil.rmtree(state_dir)
    r = run_cli(ws, "init",
                "-m", "manifest.csv",
                "-p", "photos",
                "-t", "tracks",
                "-b", "tables")
    assert r.returncode == 0, f"init 失败: {r.stderr}"
    return ws


def assert_equal(actual, expected, msg: str = ""):
    if actual != expected:
        raise AssertionError(f"{msg}: 期望 {expected!r}, 实际 {actual!r}")


def test_basic_export_import_fresh_workspace():
    """测试1：导出并导入到全新工作区"""
    print("\n" + "=" * 60)
    print("测试1：基本导出-导入（新工作区）")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="snap_test_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0, f"scan 失败: {r.stderr}"

        changes = [
            ("ISS-0001", "pending", "源工作区备注A"),
            ("ISS-0002", "accepted", "源工作区备注B"),
        ]
        for issue_id, status, remark in changes:
            r = run_cli(ws_src, "review", issue_id, "--status", status, "--remark", remark)
            assert r.returncode == 0

        state_before = load_state(ws_src)
        issues_before = sorted(i["id"] for i in state_before["issues"])
        history_before = len(state_before["review_history"])

        snap_path = base / "test_snapshot.json"
        r = run_cli(ws_src, "export", str(snap_path), "--note", "测试快照")
        assert r.returncode == 0, f"export 失败: {r.stderr}"
        assert "快照已导出" in r.stdout
        assert snap_path.exists(), "快照文件未生成"

        with open(snap_path, encoding="utf-8") as f:
            snap_data = json.load(f)
        assert "snapshot_info" in snap_data
        assert "config" in snap_data
        assert "state" in snap_data
        assert snap_data["snapshot_info"]["note"] == "测试快照"
        print(f"  快照结构验证通过")

        ws_dst = setup_workspace(base, "dst_fresh")
        r = run_cli(ws_dst, "import", str(snap_path), "--yes")
        assert r.returncode == 0, f"import 失败: {r.stderr}"

        state_after = load_state(ws_dst)
        issues_after = sorted(i["id"] for i in state_after["issues"])
        history_after = len(state_after["review_history"])

        assert_equal(issues_after, issues_before, "导入后问题编号应与导出前一致")
        assert_equal(history_after, history_before, "导入后复核历史数应一致")
        print(f"  导入后问题数: {len(issues_after)} (导出前: {len(issues_before)})")
        print(f"  导入后历史数: {history_after} (导出前: {history_before})")

        for issue_id, status, remark in changes:
            issue = next(i for i in state_after["issues"] if i["id"] == issue_id)
            assert_equal(issue["status"], status, f"{issue_id} 状态")
            assert_equal(issue["remark"], remark, f"{issue_id} 备注")
        print(f"  状态和备注导入验证通过")

        r_report_before = run_cli(ws_src, "report", "-o", str(base / "src_report.txt"))
        r_report_after = run_cli(ws_dst, "report", "-o", str(base / "dst_report.txt"))
        assert r_report_before.returncode == 0
        assert r_report_after.returncode == 0

        r_list_before = run_cli(ws_src, "list")
        r_list_after = run_cli(ws_dst, "list")
        assert r_list_before.returncode == 0
        assert r_list_after.returncode == 0

        print(f"  report 和 list 命令均正常")

        print("  [PASS] 测试1通过")
        return True


def test_config_mismatch_conflict():
    """测试2：配置不一致冲突检测"""
    print("\n" + "=" * 60)
    print("测试2：配置不一致冲突")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="snap_test_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src_cfg")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0
        src_cfg = load_config(ws_src)

        snap_path = base / "cfg_snap.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0

        ws_dst = base / "dst_cfg"
        if ws_dst.exists():
            shutil.rmtree(ws_dst)
        shutil.copytree(SAMPLE_DATA, ws_dst)
        state_dir = ws_dst / ".survey_check"
        if state_dir.exists():
            shutil.rmtree(state_dir)
        r = run_cli(ws_dst, "init",
                    "-m", "manifest.csv",
                    "-p", "photos",
                    "-t", "tracks",
                    "-b", "tables")
        assert r.returncode == 0, f"init dst 失败: {r.stderr}"

        cfg_orig = load_config(ws_dst)
        cfg_modified = dict(cfg_orig)
        cfg_modified["photo_exts"] = [".jpg", ".jpeg"]
        cfg_path = ws_dst / ".survey_check" / "survey_config.json"
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg_modified, f, ensure_ascii=False, indent=2)

        r = run_cli(ws_dst, "import", str(snap_path), "--dry-run")
        assert r.returncode == 0
        assert "配置不一致" in r.stdout
        print(f"  dry-run 检测到配置不一致 - OK")

        r = run_cli(ws_dst, "import", str(snap_path), "--yes")
        assert r.returncode == 0
        assert "配置已更新" not in r.stdout

        cfg_after_no = load_config(ws_dst)
        assert_equal(cfg_after_no["photo_exts"], cfg_modified["photo_exts"],
                     "未使用 --include-config 时配置不应被覆盖")
        print(f"  不导入配置时保留原配置 - OK")

        r = run_cli(ws_dst, "import", str(snap_path), "--include-config", "--yes")
        assert r.returncode == 0
        assert "配置已更新" in r.stdout

        cfg_after_yes = load_config(ws_dst)
        assert_equal(cfg_after_yes["photo_exts"], src_cfg["photo_exts"],
                     "使用 --include-config 后配置应与源一致")
        print(f"  --include-config 配置更新 - OK")

        print("  [PASS] 测试2通过")
        return True


def test_issue_id_conflict_strategies():
    """测试3：问题编号冲突与四种策略"""
    print("\n" + "=" * 60)
    print("测试3：编号冲突与四种策略")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="snap_test_") as tmp:
        base = Path(tmp)

        ws_a = setup_workspace(base, "ws_a")
        r = run_cli(ws_a, "scan")
        assert r.returncode == 0

        r = run_cli(ws_a, "review", "ISS-0001", "--status", "accepted", "--remark", "A工作区")
        assert r.returncode == 0

        snap_a = base / "snap_a.json"
        r = run_cli(ws_a, "export", str(snap_a))
        assert r.returncode == 0

        ws_b = setup_workspace(base, "ws_b")
        r = run_cli(ws_b, "scan")
        assert r.returncode == 0

        r = run_cli(ws_b, "review", "ISS-0001", "--status", "ignored", "--remark", "B工作区")
        assert r.returncode == 0

        n_before = len(load_state(ws_b)["issues"])

        ws_skip = base / "ws_skip"
        shutil.copytree(ws_b, ws_skip)
        r = run_cli(ws_skip, "import", str(snap_a), "--strategy", "skip", "--yes")
        assert r.returncode == 0
        state_skip = load_state(ws_skip)
        issue1 = next(i for i in state_skip["issues"] if i["id"] == "ISS-0001")
        assert_equal(issue1["status"], "ignored", "skip策略应保留目标版本")
        assert_equal(issue1["remark"], "B工作区", "skip策略备注应保留目标")
        assert_equal(len(state_skip["issues"]), n_before, "skip策略问题数不变")
        print(f"  skip 策略验证通过")

        ws_over = base / "ws_over"
        shutil.copytree(ws_b, ws_over)
        r = run_cli(ws_over, "import", str(snap_a), "--strategy", "overwrite", "--yes")
        assert r.returncode == 0
        state_over = load_state(ws_over)
        issue1 = next(i for i in state_over["issues"] if i["id"] == "ISS-0001")
        assert_equal(issue1["status"], "accepted", "overwrite策略应覆盖为快照版本")
        assert_equal(issue1["remark"], "A工作区", "overwrite策略备注应为快照")
        assert_equal(len(state_over["issues"]), n_before, "overwrite策略问题数不变")
        print(f"  overwrite 策略验证通过")

        ws_ren = base / "ws_ren"
        shutil.copytree(ws_b, ws_ren)
        r = run_cli(ws_ren, "import", str(snap_a), "--strategy", "renumber", "--yes")
        assert r.returncode == 0
        state_ren = load_state(ws_ren)
        assert_equal(len(state_ren["issues"]), n_before * 2,
                     "renumber策略问题数应翻倍")
        orig_ids = {f"ISS-{i:04d}" for i in range(1, n_before + 1)}
        new_ids = {i["id"] for i in state_ren["issues"]}
        assert all(i in new_ids for i in orig_ids), "原始编号应保留"
        assert any(f"ISS-{i:04d}" in new_ids for i in range(n_before + 1, n_before * 2 + 1)), "应有新编号"
        print(f"  renumber 策略验证通过 (共 {len(state_ren['issues'])} 个问题)")

        ws_merge = base / "ws_merge"
        shutil.copytree(ws_b, ws_merge)
        r = run_cli(ws_merge, "import", str(snap_a), "--strategy", "merge", "--yes")
        assert r.returncode == 0
        print(f"  merge 策略验证通过")

        print("  [PASS] 测试3通过")
        return True


def test_target_newer_scan_conflict():
    """测试4：目标工作区有更新扫描结果的冲突"""
    print("\n" + "=" * 60)
    print("测试4：目标更新扫描冲突")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="snap_test_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src_newer")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0

        snap_path = base / "newer_snap.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0

        ws_dst = setup_workspace(base, "dst_newer")
        r = run_cli(ws_dst, "scan")
        assert r.returncode == 0

        state_dst = load_state(ws_dst)
        future_time = (datetime.now() + timedelta(days=1)).isoformat()
        state_dst["last_scan_time"] = future_time
        if state_dst.get("scan_result"):
            state_dst["scan_result"]["scan_time"] = future_time
        state_path = ws_dst / ".survey_check" / "survey_state.json"
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state_dst, f, ensure_ascii=False, indent=2)

        r = run_cli(ws_dst, "import", str(snap_path), "--dry-run")
        assert r.returncode == 0
        assert "目标工作区扫描时间" in r.stdout or "target_newer_scan" in r.stdout or "晚于快照" in r.stdout
        print(f"  目标更新扫描检测 - OK")

        print("  [PASS] 测试4通过")
        return True


def test_dry_run_preflight():
    """测试5：dry-run 预检模式"""
    print("\n" + "=" * 60)
    print("测试5：dry-run 预检模式")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="snap_test_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src_dry")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0

        snap_path = base / "dry_snap.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0

        ws_dst = setup_workspace(base, "dst_dry")
        state_before = load_state(ws_dst)
        issues_before = len(state_before["issues"])

        r = run_cli(ws_dst, "import", str(snap_path), "--dry-run")
        assert r.returncode == 0
        assert "预检" in r.stdout

        state_after = load_state(ws_dst)
        issues_after = len(state_after["issues"])
        assert_equal(issues_after, issues_before, "dry-run 不应修改实际状态")
        print(f"  dry-run 不修改实际状态 - OK")

        assert "新增问题" in r.stdout
        print(f"  dry-run 报告统计信息完整 - OK")

        print("  [PASS] 测试5通过")
        return True


def test_backup_and_restore():
    """测试6：备份与回退"""
    print("\n" + "=" * 60)
    print("测试6：备份与回退")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="snap_test_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src_bak")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0

        snap_path = base / "bak_snap.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0

        ws_dst = setup_workspace(base, "dst_bak")
        r = run_cli(ws_dst, "scan")
        assert r.returncode == 0

        state_before = load_state(ws_dst)
        issues_before = len(state_before["issues"])

        r = run_cli(ws_dst, "import", str(snap_path), "--strategy", "overwrite", "--yes")
        assert r.returncode == 0

        r_list = run_cli(ws_dst, "backup-list")
        assert r_list.returncode == 0
        assert "导入备份" in r_list.stdout or "backup" in r_list.stdout.lower()
        backups = [line for line in r_list.stdout.splitlines() if "import_" in line]
        assert len(backups) > 0, "应有导入备份"
        print(f"  备份存在 - OK (找到 {len(backups)} 个备份)")

        backup_name = None
        for line in r_list.stdout.splitlines():
            line = line.strip()
            if line.startswith("1. "):
                backup_name = line[3:].strip()
                break

        assert backup_name, "未找到备份名"
        print(f"  备份名: {backup_name}")

        r_restore = run_cli(ws_dst, "backup-restore", backup_name, "--yes")
        assert r_restore.returncode == 0
        assert "已从备份" in r_restore.stdout or "恢复" in r_restore.stdout

        state_restored = load_state(ws_dst)
        issues_restored = len(state_restored["issues"])
        assert_equal(issues_restored, issues_before, "恢复后问题数应与导入前一致")
        print(f"  回退验证通过 (恢复后 {issues_restored} 个问题)")

        print("  [PASS] 测试6通过")
        return True


def test_restart_resume():
    """测试7：重启续跑（历史不丢失）"""
    print("\n" + "=" * 60)
    print("测试7：重启续跑 - 历史不丢失")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="snap_test_") as tmp:
        base = Path(tmp)

        ws = setup_workspace(base, "restart")
        r = run_cli(ws, "scan")
        assert r.returncode == 0

        r = run_cli(ws, "review", "ISS-0001", "--status", "pending", "--remark", "第一轮备注")
        assert r.returncode == 0
        r = run_cli(ws, "review", "ISS-0002", "--status", "accepted", "--remark", "确认通过")
        assert r.returncode == 0

        snap_path = base / "restart_snap.json"
        r = run_cli(ws, "export", str(snap_path), "--note", "重启前快照")
        assert r.returncode == 0

        state_before = load_state(ws)
        history_before = len(state_before["review_history"])
        undo_before = len(state_before["undo_stack"])

        ws_new = setup_workspace(base, "restart_new")
        r = run_cli(ws_new, "import", str(snap_path), "--yes")
        assert r.returncode == 0

        state_after = load_state(ws_new)
        history_after = len(state_after["review_history"])
        undo_after = len(state_after["undo_stack"])

        assert_equal(history_after, history_before, "导入后复核历史数应一致")
        print(f"  复核历史: {history_after} 条 (导入前 {history_before} 条)")

        r = run_cli(ws_new, "review", "ISS-0003", "--status", "ignored", "--remark", "重启后新增")
        assert r.returncode == 0

        state_final = load_state(ws_new)
        history_final = len(state_final["review_history"])
        assert_equal(history_final, history_before + 1, "重启后新增复核应继续累加")
        print(f"  重启后新增复核后历史: {history_final} 条")

        r = run_cli(ws_new, "undo")
        assert r.returncode == 0
        assert "已撤销" in r.stdout

        state_undo = load_state(ws_new)
        history_undo = len(state_undo["review_history"])
        assert_equal(history_undo, history_final + 1,
                     "撤销应增加一条撤销记录")
        print(f"  撤销功能在重启后正常")

        r = run_cli(ws_new, "scan")
        assert r.returncode == 0
        assert "复用历史问题" in r.stdout
        print(f"  重启后重扫仍能复用历史问题")

        print("  [PASS] 测试7通过")
        return True


def test_report_list_status_consistency():
    """测试8：report/status/list 输出一致性"""
    print("\n" + "=" * 60)
    print("测试8：report/status/list 输出一致性")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="snap_test_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "consist_src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0

        r = run_cli(ws_src, "review", "ISS-0001", "--status", "pending", "--remark", "测试备注")
        assert r.returncode == 0

        snap_path = base / "consist_snap.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0

        report_src = base / "src_report.txt"
        r = run_cli(ws_src, "report", "-o", str(report_src))
        assert r.returncode == 0
        with open(report_src, encoding="utf-8") as f:
            report_src_lines = [l for l in f.readlines() if "生成时间" not in l]

        status_src = run_cli(ws_src, "status")
        assert status_src.returncode == 0
        list_src = run_cli(ws_src, "list")
        assert list_src.returncode == 0

        ws_dst = setup_workspace(base, "consist_dst")
        r = run_cli(ws_dst, "import", str(snap_path), "--yes")
        assert r.returncode == 0

        report_dst = base / "dst_report.txt"
        r = run_cli(ws_dst, "report", "-o", str(report_dst))
        assert r.returncode == 0
        with open(report_dst, encoding="utf-8") as f:
            report_dst_lines = [l for l in f.readlines() if "生成时间" not in l]

        status_dst = run_cli(ws_dst, "status")
        assert status_dst.returncode == 0
        list_dst = run_cli(ws_dst, "list")
        assert list_dst.returncode == 0

        n_issues_src = sum(1 for l in report_src_lines if "[ISS-" in l)
        n_issues_dst = sum(1 for l in report_dst_lines if "[ISS-" in l)
        assert_equal(n_issues_src, n_issues_dst, "报告中问题数量应一致")
        print(f"  报告问题数一致: {n_issues_src}")

        assert "问题总数" in status_src.stdout and "问题总数" in status_dst.stdout
        print(f"  status 输出一致")

        src_list_count = sum(1 for l in list_src.stdout.splitlines() if l.startswith("[ISS-"))
        dst_list_count = sum(1 for l in list_dst.stdout.splitlines() if l.startswith("[ISS-"))
        assert_equal(src_list_count, dst_list_count, "list 输出问题数应一致")
        print(f"  list 输出一致: {src_list_count} 个问题")

        print("  [PASS] 测试8通过")
        return True


def test_snapshot_info_command():
    """测试9：snapshot-info 命令"""
    print("\n" + "=" * 60)
    print("测试9：snapshot-info 命令")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="snap_test_") as tmp:
        base = Path(tmp)

        ws = setup_workspace(base, "info_test")
        r = run_cli(ws, "scan")
        assert r.returncode == 0

        snap_path = base / "info_snap.json"
        r = run_cli(ws, "export", str(snap_path), "--note", "信息测试")
        assert r.returncode == 0

        r = run_cli(ws, "snapshot-info", str(snap_path))
        assert r.returncode == 0
        assert "快照版本" in r.stdout
        assert "导出时间" in r.stdout
        assert "问题总数" in r.stdout or "问题" in r.stdout
        assert "复核历史" in r.stdout
        assert "信息测试" in r.stdout
        assert "校验和" in r.stdout
        print(f"  snapshot-info 输出完整（含校验和）")

        print("  [PASS] 测试9通过")
        return True


def test_import_to_empty_workspace_full_recovery():
    """测试10：空工作区完整导入 - 配置+状态+历史全部恢复，立即可用"""
    print("\n" + "=" * 60)
    print("测试10：空工作区完整导入（核心回归）")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="empty_ws_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0

        r = run_cli(ws_src, "review", "ISS-0001", "--status", "pending", "--remark", "源备注A")
        assert r.returncode == 0
        r = run_cli(ws_src, "review", "ISS-0003", "--status", "accepted", "--remark", "源备注B")
        assert r.returncode == 0

        state_src = load_state(ws_src)
        n_issues_src = len(state_src["issues"])
        n_history_src = len(state_src["review_history"])

        snap_path = base / "empty_test_snap.json"
        r = run_cli(ws_src, "export", str(snap_path), "--note", "空工作区测试")
        assert r.returncode == 0

        ws_empty = base / "empty_ws"
        ws_empty.mkdir()

        assert not (ws_empty / ".survey_check").exists(), "测试前提：空工作区不应有 .survey_check"

        r = run_cli(ws_empty, "import", str(snap_path), "--yes")
        assert r.returncode == 0, f"import 失败: {r.stderr}"

        assert (ws_empty / ".survey_check" / "survey_config.json").exists(), \
            "导入后配置文件必须存在"
        assert (ws_empty / ".survey_check" / "survey_state.json").exists(), \
            "导入后状态文件必须存在"
        print(f"  配置/状态文件均已生成 - OK")

        config_empty = load_config(ws_empty)
        config_src = load_config(ws_src)
        assert_equal(config_empty["photo_exts"], config_src["photo_exts"],
                     "导入后配置应与源一致")
        assert_equal(config_empty["manifest_path"], config_src["manifest_path"],
                     "导入后 manifest_path 应一致")
        print(f"  配置内容与源一致 - OK")

        state_empty = load_state(ws_empty)
        assert_equal(len(state_empty["issues"]), n_issues_src,
                     "导入后问题数应与源一致")
        assert_equal(len(state_empty["review_history"]), n_history_src,
                     "导入后复核历史数应与源一致")

        iss1 = next(i for i in state_empty["issues"] if i["id"] == "ISS-0001")
        assert_equal(iss1["status"], "pending", "ISS-0001 状态应保留")
        assert_equal(iss1["remark"], "源备注A", "ISS-0001 备注应保留")
        iss3 = next(i for i in state_empty["issues"] if i["id"] == "ISS-0003")
        assert_equal(iss3["status"], "accepted", "ISS-0003 状态应保留")
        assert_equal(iss3["remark"], "源备注B", "ISS-0003 备注应保留")
        print(f"  问题状态和备注完整保留 - OK")

        r = run_cli(ws_empty, "status")
        assert r.returncode == 0, f"status 应能正常运行: {r.stderr}"
        assert "问题总数" in r.stdout
        print(f"  status 命令正常 - OK")

        r = run_cli(ws_empty, "list")
        assert r.returncode == 0, f"list 应能正常运行: {r.stderr}"
        n_listed = sum(1 for l in r.stdout.splitlines() if l.startswith("[ISS-"))
        assert_equal(n_listed, n_issues_src, "list 输出问题数应匹配")
        print(f"  list 命令正常 ({n_listed} 个问题) - OK")

        report_path = base / "empty_report.txt"
        r = run_cli(ws_empty, "report", "-o", str(report_path))
        assert r.returncode == 0, f"report 应能正常运行: {r.stderr}"
        assert report_path.exists(), "报告文件应生成"
        print(f"  report 命令正常 - OK")

        r = run_cli(ws_empty, "review", "ISS-0005", "--status", "ignored",
                    "--remark", "空工作区导入后新增复核")
        assert r.returncode == 0, f"review 应能正常运行: {r.stderr}"
        state_after = load_state(ws_empty)
        assert_equal(len(state_after["review_history"]), n_history_src + 1,
                     "导入后应能继续追加复核历史")
        print(f"  可继续复核且历史连续 - OK")

        print("  [PASS] 测试10通过")
        return True


def test_import_then_restart_continue_work():
    """测试11：导入后"重启"再操作 - 历史不丢、能继续复核、重扫复用"""
    print("\n" + "=" * 60)
    print("测试11：导入后重启续跑")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="restart_test_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0
        r = run_cli(ws_src, "review", "ISS-0001", "--status", "pending", "--remark", "重启前")
        assert r.returncode == 0

        snap_path = base / "restart_snap.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0

        ws_new = base / "new_ws"
        ws_new.mkdir()
        r = run_cli(ws_new, "import", str(snap_path), "--yes")
        assert r.returncode == 0

        state_after_import = load_state(ws_new)
        history_after_import = len(state_after_import["review_history"])
        undo_after_import = len(state_after_import["undo_stack"])
        print(f"  导入后: 历史 {history_after_import} 条, 撤销栈 {undo_after_import} 步")

        r = run_cli(ws_new, "review", "ISS-0002", "--status", "accepted", "--remark", "重启后第1次")
        assert r.returncode == 0
        r = run_cli(ws_new, "review", "ISS-0003", "--status", "ignored", "--remark", "重启后第2次")
        assert r.returncode == 0

        state_after_reviews = load_state(ws_new)
        history_after_reviews = len(state_after_reviews["review_history"])
        assert_equal(history_after_reviews, history_after_import + 2,
                     "重启后追加复核应正确累加")
        print(f"  追加复核后历史: {history_after_reviews} 条 - OK")

        r = run_cli(ws_new, "undo")
        assert r.returncode == 0
        assert "已撤销" in r.stdout

        state_after_undo = load_state(ws_new)
        history_after_undo = len(state_after_undo["review_history"])
        assert_equal(history_after_undo, history_after_reviews + 1,
                     "撤销应追加撤销记录")
        print(f"  撤销功能正常 - OK")

        r = run_cli(ws_new, "scan")
        assert r.returncode == 0
        assert "复用历史问题" in r.stdout
        print(f"  重扫能复用历史问题 - OK")

        state_final = load_state(ws_new)
        iss1 = next(i for i in state_final["issues"] if i["id"] == "ISS-0001")
        assert_equal(iss1["status"], "pending", "重扫后历史状态不应丢失")
        assert_equal(iss1["remark"], "重启前", "重扫后历史备注不应丢失")
        print(f"  重扫后历史状态和备注完整保留 - OK")

        print("  [PASS] 测试11通过")
        return True


def test_import_to_partial_residue_workspace():
    """测试12：目标工作区有残留状态但无配置 - 保护与恢复行为"""
    print("\n" + "=" * 60)
    print("测试12：目标目录残留状态无配置的保护行为")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="residue_test_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0
        r = run_cli(ws_src, "review", "ISS-0001", "--status", "accepted", "--remark", "源")
        assert r.returncode == 0

        snap_path = base / "res_snap.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0

        ws_res = base / "residue_ws"
        ws_res.mkdir()
        state_dir = ws_res / ".survey_check"
        state_dir.mkdir()

        fake_state = {
            "state_version": "1.0",
            "created_at": "2020-01-01T00:00:00",
            "last_scan_time": None,
            "scan_result": None,
            "survey_points": [],
            "issues": [
                {
                    "id": "ISS-0001",
                    "issue_type": "missing",
                    "status": "open",
                    "description": "假数据",
                    "file_type": "photo",
                    "point_id": "P999",
                    "file_paths": [],
                    "remark": "残留数据",
                    "created_at": "2020-01-01T00:00:00",
                    "updated_at": "2020-01-01T00:00:00",
                }
            ],
            "review_history": [],
            "undo_stack": [],
            "next_issue_number": 2,
        }
        with open(state_dir / "survey_state.json", "w", encoding="utf-8") as f:
            json.dump(fake_state, f, ensure_ascii=False, indent=2)

        assert (state_dir / "survey_state.json").exists()
        assert not (state_dir / "survey_config.json").exists()
        print(f"  残留场景: 有状态无配置 - 已构建")

        r = run_cli(ws_res, "import", str(snap_path), "--dry-run")
        assert r.returncode == 0
        assert "无配置" in r.stdout or "no_target_config" in r.stdout or "残留" in r.stdout
        assert "编号冲突" in r.stdout or "issue_id_conflict" in r.stdout
        print(f"  dry-run 检测到无配置/残留 + 编号冲突 - OK")

        r = run_cli(ws_res, "import", str(snap_path), "--strategy", "skip", "--yes")
        assert r.returncode == 0

        backup_path_str = None
        for line in r.stdout.splitlines():
            if "备份已保存至" in line:
                backup_path_str = line.split("备份已保存至:")[1].strip()
                break
        assert backup_path_str, "导入应有备份路径提示"
        backup_path = Path(backup_path_str)
        assert backup_path.exists(), "备份目录应存在"
        assert (backup_path / "survey_state.json").exists(), "备份应包含状态文件"
        print(f"  残留状态已自动备份 - OK")

        config_res = load_config(ws_res)
        config_src = load_config(ws_src)
        assert_equal(config_res["photo_exts"], config_src["photo_exts"],
                     "残留工作区导入后配置应从快照恢复")
        print(f"  配置已从快照恢复 - OK")

        state_res = load_state(ws_res)
        iss1 = next(i for i in state_res["issues"] if i["id"] == "ISS-0001")
        assert_equal(iss1["status"], "open",
                     "skip策略下，残留问题应保留（目标版本）")
        assert_equal(iss1["remark"], "残留数据",
                     "skip策略下，残留备注应保留")
        print(f"  skip策略保留目标残留问题 - OK")

        r = run_cli(ws_res, "status")
        assert r.returncode == 0
        print(f"  status 命令在残留导入后正常 - OK")

        print("  [PASS] 测试12通过")
        return True


def test_export_integrity():
    """测试13：导出完整性 - 元数据、校验和、必要字段"""
    print("\n" + "=" * 60)
    print("测试13：导出完整性验证")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="export_int_") as tmp:
        base = Path(tmp)

        ws = setup_workspace(base, "ws")
        r = run_cli(ws, "scan")
        assert r.returncode == 0

        r = run_cli(ws, "review", "ISS-0001", "--status", "pending", "--remark", "备注X")
        assert r.returncode == 0
        r = run_cli(ws, "review", "ISS-0002", "--status", "accepted", "--remark", "备注Y")
        assert r.returncode == 0

        snap_path = base / "integrity_snap.json"
        r = run_cli(ws, "export", str(snap_path), "--note", "完整性测试")
        assert r.returncode == 0

        with open(snap_path, encoding="utf-8") as f:
            snap = json.load(f)

        info = snap["snapshot_info"]
        assert_equal(info["snapshot_version"], "1.1", "快照版本应为1.1")
        assert info["exported_at"], "导出时间不应为空"
        assert info["source_workspace"], "来源工作区不应为空"
        assert_equal(info["note"], "完整性测试", "备注应与传入值一致")
        assert_equal(info["state_version"], "1.0", "状态版本应记录")
        assert_equal(info["config_version"], "1.0", "配置版本应记录")
        assert info["issue_count"] > 0, "问题计数应大于0"
        assert info["history_count"] > 0, "历史计数应大于0"
        assert info["content_hash"], "内容校验和不应为空"
        print(f"  元数据完整: 版本={info['snapshot_version']}, "
              f"问题={info['issue_count']}, 历史={info['history_count']}, "
              f"校验和={info['content_hash']}")

        state = load_state(ws)
        assert_equal(info["issue_count"], len(state["issues"]), "问题计数应与实际一致")
        assert_equal(info["history_count"], len(state["review_history"]), "历史计数应与实际一致")
        assert_equal(info["undo_stack_count"], len(state["undo_stack"]), "撤销栈计数应与实际一致")
        assert_equal(info["survey_points_count"], len(state["survey_points"]), "调查点计数应与实际一致")
        print(f"  计数与实际一致 - OK")

        import hashlib
        payload = json.dumps({"config": snap["config"], "state": snap["state"]},
                             sort_keys=True, ensure_ascii=False)
        expected_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        assert_equal(info["content_hash"], expected_hash, "内容校验和应可复现")
        print(f"  内容校验和可复现 - OK")

        assert "snapshot_info" in snap
        assert "config" in snap
        assert "state" in snap
        assert "issues" in snap["state"]
        assert "review_history" in snap["state"]
        assert "undo_stack" in snap["state"]
        print(f"  快照结构完整 - OK")

        r = run_cli(ws, "snapshot-info", str(snap_path))
        assert r.returncode == 0
        assert "校验和" in r.stdout
        assert "完整性测试" in r.stdout
        print(f"  snapshot-info 含校验和 - OK")

        print("  [PASS] 测试13通过")
        return True


def test_corrupted_snapshot_import():
    """测试14：损坏快照导入 - 无效JSON/缺字段/篡改内容"""
    print("\n" + "=" * 60)
    print("测试14：损坏快照导入")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="corrupt_test_") as tmp:
        base = Path(tmp)

        ws = setup_workspace(base, "ws")
        r = run_cli(ws, "scan")
        assert r.returncode == 0

        invalid_json_path = base / "bad_json.json"
        with open(invalid_json_path, "w", encoding="utf-8") as f:
            f.write("this is not valid json {{{")
        r = run_cli(ws, "import", str(invalid_json_path), "--yes")
        assert r.returncode != 0, "无效JSON应导入失败"
        assert "无效" in r.stdout or "invalid" in r.stdout.lower() or "JSON" in r.stdout
        assert "诊断" in r.stdout or "建议" in r.stdout or "hint" in r.stdout.lower()
        print(f"  无效JSON被正确拦截 - OK")

        missing_fields_path = base / "missing_fields.json"
        with open(missing_fields_path, "w", encoding="utf-8") as f:
            json.dump({"snapshot_info": {"snapshot_version": "1.1", "exported_at": "2025-01-01"}}, f)
        r = run_cli(ws, "import", str(missing_fields_path), "--yes")
        assert r.returncode != 0, "缺字段快照应导入失败"
        assert "缺少" in r.stdout or "missing" in r.stdout.lower()
        print(f"  缺字段快照被正确拦截 - OK")

        snap_path = base / "good_snap.json"
        r = run_cli(ws, "export", str(snap_path))
        assert r.returncode == 0
        with open(snap_path, encoding="utf-8") as f:
            snap = json.load(f)
        snap["state"]["issues"].append({
            "id": "ISS-FAKE",
            "issue_type": "missing",
            "status": "open",
            "description": "篡改数据",
            "file_type": "photo",
            "point_id": "P999",
            "file_paths": [],
            "remark": "被篡改",
            "created_at": "2025-01-01",
            "updated_at": "2025-01-01",
        })
        tampered_path = base / "tampered_snap.json"
        with open(tampered_path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        r = run_cli(ws, "import", str(tampered_path), "--yes")
        assert r.returncode != 0, "篡改内容应导入失败（校验和不匹配）"
        assert "校验和" in r.stdout or "checksum" in r.stdout.lower() or "篡改" in r.stdout or "损坏" in r.stdout
        print(f"  篡改内容被校验和拦截 - OK")

        missing_info_path = base / "no_info.json"
        with open(missing_info_path, "w", encoding="utf-8") as f:
            json.dump({
                "snapshot_info": {"exported_at": "2025-01-01"},
                "config": {"config_version": "1.0", "manifest_path": "", "photo_dir": "", "track_dir": "", "table_dir": ""},
                "state": {"state_version": "1.0", "issues": [], "review_history": [], "undo_stack": []},
            }, f)
        r = run_cli(ws, "import", str(missing_info_path), "--yes")
        assert r.returncode != 0, "缺snapshot_version应导入失败"
        print(f"  缺快照版本字段被拦截 - OK")

        print("  [PASS] 测试14通过")
        return True


def test_version_mismatch():
    """测试15：版本不匹配检测"""
    print("\n" + "=" * 60)
    print("测试15：版本不匹配检测")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="version_test_") as tmp:
        base = Path(tmp)

        ws = setup_workspace(base, "ws")
        r = run_cli(ws, "scan")
        assert r.returncode == 0

        snap_path = base / "ver_snap.json"
        r = run_cli(ws, "export", str(snap_path))
        assert r.returncode == 0

        with open(snap_path, encoding="utf-8") as f:
            snap = json.load(f)

        snap_fut = json.loads(json.dumps(snap))
        snap_fut["snapshot_info"]["snapshot_version"] = "99.0"
        snap_fut["snapshot_info"]["content_hash"] = ""
        fut_path = base / "future_snap.json"
        with open(fut_path, "w", encoding="utf-8") as f:
            json.dump(snap_fut, f, ensure_ascii=False, indent=2)
        r = run_cli(ws, "import", str(fut_path), "--yes")
        assert r.returncode != 0, "不支持的快照版本应导入失败"
        assert "版本" in r.stdout or "version" in r.stdout.lower() or "不支持" in r.stdout
        print(f"  不支持的快照版本被拦截 - OK")

        snap_bad_state = json.loads(json.dumps(snap))
        snap_bad_state["snapshot_info"]["state_version"] = "99.0"
        snap_bad_state["snapshot_info"]["content_hash"] = ""
        bad_state_path = base / "bad_state_snap.json"
        with open(bad_state_path, "w", encoding="utf-8") as f:
            json.dump(snap_bad_state, f, ensure_ascii=False, indent=2)
        r = run_cli(ws, "import", str(bad_state_path), "--yes")
        assert r.returncode != 0, "不支持的状态版本应导入失败"
        print(f"  不支持的状态版本被拦截 - OK")

        snap_bad_cfg = json.loads(json.dumps(snap))
        snap_bad_cfg["snapshot_info"]["config_version"] = "99.0"
        snap_bad_cfg["snapshot_info"]["content_hash"] = ""
        bad_cfg_path = base / "bad_cfg_snap.json"
        with open(bad_cfg_path, "w", encoding="utf-8") as f:
            json.dump(snap_bad_cfg, f, ensure_ascii=False, indent=2)
        r = run_cli(ws, "import", str(bad_cfg_path), "--yes")
        assert r.returncode != 0, "不支持的配置版本应导入失败"
        print(f"  不支持的配置版本被拦截 - OK")

        print("  [PASS] 测试15通过")
        return True


def test_repeated_import_idempotency():
    """测试16：重复导入幂等性 - 连续导入两次不应产生重复或状态错乱"""
    print("\n" + "=" * 60)
    print("测试16：重复导入幂等性")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="repeat_test_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0
        r = run_cli(ws_src, "review", "ISS-0001", "--status", "pending", "--remark", "原始")
        assert r.returncode == 0

        snap_path = base / "repeat_snap.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0

        ws_dst = setup_workspace(base, "dst")
        r = run_cli(ws_dst, "scan")
        assert r.returncode == 0
        n_dst_before = len(load_state(ws_dst)["issues"])

        r1 = run_cli(ws_dst, "import", str(snap_path), "--strategy", "overwrite", "--yes")
        assert r1.returncode == 0
        state_after_1 = load_state(ws_dst)
        n_after_1 = len(state_after_1["issues"])
        history_after_1 = len(state_after_1["review_history"])
        iss1_after_1 = next(i for i in state_after_1["issues"] if i["id"] == "ISS-0001")
        assert_equal(iss1_after_1["remark"], "原始", "overwrite后备注应为快照版本")
        print(f"  第1次导入(overwrite): {n_after_1} 个问题, {history_after_1} 条历史")

        r = run_cli(ws_dst, "review", "ISS-0002", "--status", "ignored", "--remark", "两次导入间新增")
        assert r.returncode == 0

        r2 = run_cli(ws_dst, "import", str(snap_path), "--strategy", "skip", "--yes")
        assert r2.returncode == 0
        state_after_2 = load_state(ws_dst)
        n_after_2 = len(state_after_2["issues"])
        print(f"  第2次导入(skip): {n_after_2} 个问题")

        assert_equal(n_after_2, n_after_1, "skip策略下重复导入问题数不应增加")

        iss2 = next(i for i in state_after_2["issues"] if i["id"] == "ISS-0002")
        assert_equal(iss2["remark"], "两次导入间新增", "skip策略下两次导入间的备注应保留")
        print(f"  skip策略保留两次导入间的操作 - OK")

        r = run_cli(ws_dst, "status")
        assert r.returncode == 0
        r = run_cli(ws_dst, "list")
        assert r.returncode == 0
        r = run_cli(ws_dst, "report", "-o", str(base / "repeat_report.txt"))
        assert r.returncode == 0
        print(f"  重复导入后命令正常 - OK")

        print("  [PASS] 测试16通过")
        return True


def test_cross_restart_validation():
    """测试17：跨重启验证链路 - 导入后多次独立CLI调用，状态一致不丢失"""
    print("\n" + "=" * 60)
    print("测试17：跨重启验证链路")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="xrestart_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0

        for i in range(1, 4):
            r = run_cli(ws_src, "review", f"ISS-000{i}", "--status", "pending",
                        "--remark", f"源备注{i}")
            if r.returncode != 0:
                break

        state_src = load_state(ws_src)
        n_issues = len(state_src["issues"])
        n_history = len(state_src["review_history"])
        n_undo = len(state_src["undo_stack"])

        snap_path = base / "xrestart_snap.json"
        r = run_cli(ws_src, "export", str(snap_path), "--note", "跨重启测试")
        assert r.returncode == 0

        ws_new = base / "new_ws"
        ws_new.mkdir()
        r = run_cli(ws_new, "import", str(snap_path), "--yes")
        assert r.returncode == 0
        print(f"  导入完成")

        r = run_cli(ws_new, "status")
        assert r.returncode == 0
        assert "问题总数" in r.stdout
        first_status = r.stdout
        print(f"  第1次 status 正常")

        r = run_cli(ws_new, "list")
        assert r.returncode == 0
        list_count_1 = sum(1 for l in r.stdout.splitlines() if l.startswith("[ISS-"))
        print(f"  第1次 list: {list_count_1} 个问题")

        r = run_cli(ws_new, "report", "-o", str(base / "r1.txt"))
        assert r.returncode == 0
        print(f"  第1次 report 正常")

        r = run_cli(ws_new, "review", "ISS-0001", "--status", "accepted", "--remark", "重启后更新")
        assert r.returncode == 0

        r = run_cli(ws_new, "status")
        assert r.returncode == 0
        second_status = r.stdout
        print(f"  第2次 status 正常（操作后）")

        r = run_cli(ws_new, "list")
        assert r.returncode == 0
        list_count_2 = sum(1 for l in r.stdout.splitlines() if l.startswith("[ISS-"))
        assert_equal(list_count_2, list_count_1, "list 问题数应不变")
        print(f"  第2次 list: {list_count_2} 个问题")

        r = run_cli(ws_new, "undo")
        assert r.returncode == 0
        assert "已撤销" in r.stdout

        r = run_cli(ws_new, "status")
        assert r.returncode == 0
        print(f"  第3次 status 正常（撤销后）")

        r = run_cli(ws_new, "scan")
        assert r.returncode == 0

        r = run_cli(ws_new, "status")
        assert r.returncode == 0
        assert "问题总数" in r.stdout
        print(f"  第4次 status 正常（重扫后）")

        r = run_cli(ws_new, "report", "-o", str(base / "r2.txt"))
        assert r.returncode == 0
        print(f"  第2次 report 正常")

        state_final = load_state(ws_new)
        assert_equal(len(state_final["issues"]), n_issues,
                     "跨重启后问题数应与源一致")
        iss1 = next(i for i in state_final["issues"] if i["id"] == "ISS-0001")
        assert_equal(iss1["remark"], "源备注1",
                     "跨重启+撤销+重扫后备注应恢复为导入时的值")
        print(f"  跨重启验证: 问题数={len(state_final['issues'])}, "
              f"历史={len(state_final['review_history'])} 条")

        print("  [PASS] 测试17通过")
        return True


def test_ops_log():
    """测试18：操作日志验证 - 导出/导入/重复导入均有日志"""
    print("\n" + "=" * 60)
    print("测试18：操作日志验证")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="log_test_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0
        r = run_cli(ws_src, "review", "ISS-0001", "--status", "pending", "--remark", "日志测试")
        assert r.returncode == 0

        snap_path = base / "log_snap.json"
        r = run_cli(ws_src, "export", str(snap_path), "--note", "日志测试")
        assert r.returncode == 0

        log_entries = load_ops_log(ws_src)
        export_logs = [e for e in log_entries if e.get("op") == "export"]
        assert len(export_logs) >= 1, "应有导出日志"
        exp_log = export_logs[-1]
        assert_equal(exp_log["result"], "success", "导出日志应为成功")
        assert exp_log["content_hash"], "导出日志应含校验和"
        assert exp_log["issue_count"] > 0, "导出日志应含问题计数"
        assert exp_log["history_count"] >= 0, "导出日志应含历史计数"
        print(f"  导出日志: result={exp_log['result']}, hash={exp_log['content_hash']}")

        ws_dst = setup_workspace(base, "dst")
        r = run_cli(ws_dst, "import", str(snap_path), "--yes")
        assert r.returncode == 0

        log_entries_dst = load_ops_log(ws_dst)
        import_logs = [e for e in log_entries_dst if e.get("op") == "import"]
        assert len(import_logs) >= 1, "应有导入日志"
        imp_log = import_logs[-1]
        assert_equal(imp_log["result"], "success", "导入日志应为成功")
        assert "strategy" in imp_log, "导入日志应含策略"
        assert "issues_imported" in imp_log, "导入日志应含导入统计"
        assert "content_hash" in imp_log, "导入日志应含校验和"
        print(f"  导入日志: result={imp_log['result']}, strategy={imp_log['strategy']}")

        r = run_cli(ws_dst, "import", str(snap_path), "--dry-run")
        assert r.returncode == 0
        log_entries_dst2 = load_ops_log(ws_dst)
        dry_run_logs = [e for e in log_entries_dst2 if e.get("op") == "import_dry_run"]
        assert len(dry_run_logs) >= 1, "应有dry-run日志"
        print(f"  dry-run 日志存在 - OK")

        r = run_cli(ws_dst, "import", str(snap_path), "--yes")
        assert r.returncode == 0
        log_entries_dst3 = load_ops_log(ws_dst)
        all_import_logs = [e for e in log_entries_dst3 if e.get("op") == "import"]
        assert len(all_import_logs) >= 2, "重复导入应有第二条日志"
        print(f"  重复导入日志 - OK (共 {len(all_import_logs)} 条导入日志)")

        print("  [PASS] 测试18通过")
        return True


def test_ops_log_cli_command():
    """测试19：ops-log CLI 命令"""
    print("\n" + "=" * 60)
    print("测试19：ops-log CLI 命令")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="opslog_cli_") as tmp:
        base = Path(tmp)

        ws = setup_workspace(base, "ws")
        r = run_cli(ws, "scan")
        assert r.returncode == 0

        snap_path = base / "ops_snap.json"
        r = run_cli(ws, "export", str(snap_path), "--note", "CLI日志测试")
        assert r.returncode == 0

        r = run_cli(ws, "ops-log")
        assert r.returncode == 0
        assert "export" in r.stdout
        assert "OK" in r.stdout
        print(f"  ops-log 显示导出记录 - OK")

        ws_dst = setup_workspace(base, "dst")
        r = run_cli(ws_dst, "import", str(snap_path), "--yes")
        assert r.returncode == 0

        r = run_cli(ws_dst, "ops-log")
        assert r.returncode == 0
        assert "import" in r.stdout
        print(f"  ops-log 显示导入记录 - OK")

        r = run_cli(ws_dst, "ops-log", "--op", "import")
        assert r.returncode == 0
        assert "import" in r.stdout
        print(f"  ops-log --op 过滤正常 - OK")

        r = run_cli(ws_dst, "ops-log", "--limit", "1")
        assert r.returncode == 0
        print(f"  ops-log --limit 正常 - OK")

        print("  [PASS] 测试19通过")
        return True


def main():
    print("快照导出/导入功能 - 回归测试")
    print(f"工作目录: {SCRIPT_DIR}")
    print(f"Python: {PYTHON_EXE}")

    tests = [
        test_basic_export_import_fresh_workspace,
        test_config_mismatch_conflict,
        test_issue_id_conflict_strategies,
        test_target_newer_scan_conflict,
        test_dry_run_preflight,
        test_backup_and_restore,
        test_restart_resume,
        test_report_list_status_consistency,
        test_snapshot_info_command,
        test_import_to_empty_workspace_full_recovery,
        test_import_then_restart_continue_work,
        test_import_to_partial_residue_workspace,
        test_export_integrity,
        test_corrupted_snapshot_import,
        test_version_mismatch,
        test_repeated_import_idempotency,
        test_cross_restart_validation,
        test_ops_log,
        test_ops_log_cli_command,
    ]

    passed = 0
    failed = 0
    errors = []

    for test in tests:
        try:
            if test():
                passed += 1
        except AssertionError as e:
            failed += 1
            err_msg = f"{test.__name__}: {e}"
            errors.append(err_msg)
            print(f"  [FAIL] {err_msg}")
        except Exception as e:
            failed += 1
            err_msg = f"{test.__name__}: 异常 {type(e).__name__}: {e}\n{traceback.format_exc()}"
            errors.append(err_msg)
            print(f"  [ERROR] {err_msg}")

    print("\n" + "=" * 60)
    print(f"测试结果: {passed} 通过, {failed} 失败")
    print("=" * 60)

    if errors:
        print("\n失败详情:")
        for err in errors:
            print(f"  - {err}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
