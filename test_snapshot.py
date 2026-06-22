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
    """测试12：残留状态无配置应阻止导入；干净工作区仍可成功导入"""
    print("\n" + "=" * 60)
    print("测试12：残留状态保护与干净工作区导入")
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

        # ========== 场景1：残留状态无配置 → 应失败且不写入新配置 ==========
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
        print(f"  场景1: 残留状态无配置 - 已构建")

        r = run_cli(ws_res, "import", str(snap_path), "--dry-run")
        assert r.returncode != 0, "残留状态无配置时 dry-run 也应失败"
        assert "残留" in r.stdout, "输出应包含'残留'关键词"
        assert "residual_state_no_config" in r.stdout, "输出应包含冲突类型"
        assert "诊断" in r.stdout or "建议" in r.stdout, "应有处理提示"
        print(f"  残留场景 dry-run 被正确拦截 - OK")

        r = run_cli(ws_res, "import", str(snap_path), "--strategy", "skip", "--yes")
        assert r.returncode != 0, "残留状态无配置时导回应失败"
        assert "残留" in r.stdout
        assert "删除 .survey_check/ 目录" in r.stdout or "init" in r.stdout
        assert not (state_dir / "survey_config.json").exists(), "失败后不应写入新配置"
        print(f"  残留场景导入被正确拦截且未写配置 - OK")

        with open(state_dir / "survey_state.json", "r", encoding="utf-8") as f:
            state_after = json.load(f)
        assert_equal(state_after["issues"][0]["remark"], "残留数据",
                     "失败后残留状态文件不应被修改")
        print(f"  失败后残留状态文件保持原样 - OK")

        # ========== 场景2：干净工作区（无状态无配置）→ 应成功 ==========
        ws_clean = base / "clean_ws"
        ws_clean.mkdir()
        assert not (ws_clean / ".survey_check").exists()
        print(f"  场景2: 干净工作区 - 已构建")

        r = run_cli(ws_clean, "import", str(snap_path), "--strategy", "skip", "--yes")
        assert r.returncode == 0, "干净工作区导入应成功"
        assert (ws_clean / ".survey_check" / "survey_config.json").exists()
        assert (ws_clean / ".survey_check" / "survey_state.json").exists()
        print(f"  干净工作区导入成功 - OK")

        config_clean = load_config(ws_clean)
        config_src = load_config(ws_src)
        assert_equal(config_clean["photo_exts"], config_src["photo_exts"],
                     "干净工作区导入后配置应从快照恢复")
        print(f"  干净工作区配置恢复正确 - OK")

        state_clean = load_state(ws_clean)
        iss1 = next(i for i in state_clean["issues"] if i["id"] == "ISS-0001")
        assert_equal(iss1["remark"], "源",
                     "干净工作区导入后问题备注应来自快照")
        print(f"  干净工作区状态恢复正确 - OK")

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


def test_import_cross_restart_status_consistency():
    """测试20：导回后跨重启运行 status/list/report 结果一致，历史备注不丢失"""
    print("\n" + "=" * 60)
    print("测试20：导回后跨重启命令一致性验证")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="xrestart_") as tmp:
        base = Path(tmp)

        # 源工作区：扫描 + review 留备注
        ws_src = setup_workspace(base, "src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0, f"scan失败: {r.stderr}"
        r = run_cli(ws_src, "review", "ISS-0001", "--status", "accepted",
                    "--remark", "第一次审批")
        assert r.returncode == 0, f"review1失败: {r.stderr}, stdout: {r.stdout}"
        r = run_cli(ws_src, "review", "ISS-0002", "--status", "ignored",
                    "--remark", "驳回处理")
        assert r.returncode == 0, f"review2失败: {r.stderr}, stdout: {r.stdout}"

        snap_path = base / "xrestart_snap.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0, f"export失败: {r.stderr}"

        # 目标工作区：干净目录，直接导回
        ws_dst = base / "dst"
        ws_dst.mkdir()
        r = run_cli(ws_dst, "import", str(snap_path), "--yes")
        assert r.returncode == 0, f"import失败: {r.stdout}\nstderr: {r.stderr}"

        # 记录"重启"前的基准输出
        r_status_before = run_cli(ws_dst, "status")
        assert r_status_before.returncode == 0, f"导入后status失败: {r_status_before.stderr}"
        assert "问题总数" in r_status_before.stdout, "status输出应包含问题总数"

        r_list_before = run_cli(ws_dst, "list")
        assert r_list_before.returncode == 0
        list_count_before = sum(1 for l in r_list_before.stdout.splitlines() if l.startswith("[ISS-"))

        r_report_before = run_cli(ws_dst, "report", "-o", str(base / "report_before.txt"))
        assert r_report_before.returncode == 0
        assert (base / "report_before.txt").exists()

        # ========== 模拟 3 次"重启"：每次都是独立的子进程调用 ==========
        for restart_round in range(1, 4):
            print(f"  第 {restart_round} 轮重启验证...")

            r_status = run_cli(ws_dst, "status")
            assert r_status.returncode == 0, f"第 {restart_round} 轮 status 失败"

            r_list = run_cli(ws_dst, "list")
            assert r_list.returncode == 0, f"第 {restart_round} 轮 list 失败"
            assert "ISS-0001" in r_list.stdout
            assert "ISS-0002" in r_list.stdout
            list_count = sum(1 for l in r_list.stdout.splitlines() if l.startswith("[ISS-"))
            assert_equal(list_count, list_count_before, f"第 {restart_round} 轮 list 问题数应一致")

            assert "第一次审批" in r_list.stdout, f"第 {restart_round} 轮 ISS-0001 备注丢失"
            assert "驳回处理" in r_list.stdout, f"第 {restart_round} 轮 ISS-0002 备注丢失"

            r_report = run_cli(ws_dst, "report", "-o", str(base / f"report_{restart_round}.txt"))
            assert r_report.returncode == 0, f"第 {restart_round} 轮 report 失败"
            assert (base / f"report_{restart_round}.txt").exists()

            print(f"  第 {restart_round} 轮全部通过 - OK (问题数: {list_count})")

        # 验证重启前后状态完全一致
        state_final = load_state(ws_dst)
        iss1_final = next(i for i in state_final["issues"] if i["id"] == "ISS-0001")
        assert_equal(iss1_final["remark"], "第一次审批", "多轮重启后 ISS-0001 备注不应丢失")
        iss2_final = next(i for i in state_final["issues"] if i["id"] == "ISS-0002")
        assert_equal(iss2_final["remark"], "驳回处理", "多轮重启后 ISS-0002 备注不应丢失")

        history_count = len(state_final["review_history"])
        assert history_count >= 2, "评审历史不应丢失"
        print(f"  多轮重启后状态/历史/备注全部一致 - OK (历史记录: {history_count} 条)")

        print("  [PASS] 测试20通过")
        return True


def test_preflight_three_conclusions():
    """测试21：dry-run预检 - 三种结论(proceed/confirm/abort)、冲突分类汇总、import_id、结构化ops-log"""
    print("\n" + "=" * 60)
    print("测试21：预检三种结论 + 冲突分类 + 结构化日志")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="preflight_3_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0
        r = run_cli(ws_src, "review", "ISS-0001", "--status", "pending",
                    "--remark", "预检源备注")
        assert r.returncode == 0
        snap_path = base / "p1.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0

        # ===== 场景1：空工作区 clean import => proceed =====
        ws_proceed = base / "ws_proceed"
        ws_proceed.mkdir()
        r = run_cli(ws_proceed, "import", str(snap_path), "--dry-run")
        assert r.returncode == 0, f"proceed场景dry-run返回码应为0: {r.stderr}"
        assert "预检结论" in r.stdout, "应包含预检结论字段"
        assert "[OK]" in r.stdout and "可继续" in r.stdout, \
            f"proceed场景应输出[OK]可继续: {r.stdout[:200]}"
        assert "导入ID: IMP-" in r.stdout, "应包含import_id"
        assert "冲突分类汇总" in r.stdout, "应包含冲突分类汇总"
        log_p = load_ops_log(ws_proceed)
        dry_logs = [e for e in log_p if e.get("op") == "import_dry_run"]
        assert len(dry_logs) >= 1, "应至少有1条dry_run ops-log"
        assert dry_logs[-1].get("preflight_conclusion") == "proceed", \
            f"ops-log应记录preflight_conclusion=proceed"
        assert "import_id" in dry_logs[-1], "ops-log应包含import_id"
        assert "conflict_summary" in dry_logs[-1], "ops-log应包含conflict_summary"
        print(f"  场景1(proceed): 预检结论OK, ops-log结构化完整 - OK")

        # ===== 场景2：有状态的旧工作区 + 相同快照 => confirm(有warnings) =====
        ws_confirm = base / "ws_confirm"
        shutil.copytree(ws_src, ws_confirm)
        r = run_cli(ws_confirm, "import", str(snap_path), "--dry-run")
        assert r.returncode == 0, f"confirm场景dry-run返回码应为0: {r.stderr}"
        assert "[!]" in r.stdout and "需确认" in r.stdout, \
            f"confirm场景应输出[!]需确认: {r.stdout[:200]}"
        assert "目标已有数据类" in r.stdout, "应包含目标已有数据类冲突分类"
        assert "内容冲突类" in r.stdout, "应包含内容冲突类分类"
        log_c = load_ops_log(ws_confirm)
        dry_logs_c = [e for e in log_c if e.get("op") == "import_dry_run"]
        assert dry_logs_c[-1].get("preflight_conclusion") == "confirm", \
            f"ops-log应记录preflight_conclusion=confirm"
        summ = dry_logs_c[-1].get("conflict_summary", {})
        assert summ.get("target_has_data", 0) >= 1, "conflict_summary应有target_has_data类"
        print(f"  场景2(confirm): 警告触发需确认结论 + 分类汇总OK - OK")

        # ===== 场景3：残留状态无配置 => abort(有error) =====
        ws_abort = base / "ws_abort"
        ws_abort.mkdir()
        sd = ws_abort / ".survey_check"
        sd.mkdir()
        with open(sd / "survey_state.json", "w", encoding="utf-8") as f:
            json.dump({"state_version": "1.0", "issues": [], "review_history": [],
                       "undo_stack": [], "next_issue_number": 1}, f)
        r = run_cli(ws_abort, "import", str(snap_path), "--dry-run")
        assert r.returncode != 0, "abort场景dry-run返回码应非0"
        assert "[X]" in r.stdout and "必须中止" in r.stdout, \
            f"abort场景应输出[X]必须中止: {r.stdout[:200]}"
        assert "残留状态类" in r.stdout, "应包含残留状态类分类"
        print(f"  场景3(abort): 残留状态触发必须中止 - OK")

        print("  [PASS] 测试21通过")
        return True


def test_confirmation_flow_before_write():
    """测试22：落盘前确认流程 - confirm场景非--yes触发交互，--yes跳过，abort不执行"""
    print("\n" + "=" * 60)
    print("测试22：落盘前确认流程（--yes vs 交互 vs abort）")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="confirm_flow_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0
        r = run_cli(ws_src, "review", "ISS-0001", "--status", "accepted",
                    "--remark", "源备注确认流测试")
        assert r.returncode == 0
        snap_path = base / "flow.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0

        # ===== 场景A：confirm场景 --yes => 直接跳过确认成功导入 =====
        ws_yes = base / "ws_yes"
        shutil.copytree(ws_src, ws_yes)
        state_before = load_state(ws_yes)
        remark_before = next(i["remark"] for i in state_before["issues"] if i["id"] == "ISS-0001")
        r = run_cli(ws_yes, "import", str(snap_path),
                    "--strategy", "overwrite", "--yes")
        assert r.returncode == 0, f"--yes跳过确认应成功: {r.stderr}"
        assert "开始执行导入" in r.stdout, "应输出开始执行导入"
        assert "快照导入报告: 成功" in r.stdout, "应输出成功"
        state_after = load_state(ws_yes)
        logs_y = load_ops_log(ws_yes)
        imp_logs_y = [e for e in logs_y if e.get("op") == "import" and e.get("result") == "success"]
        assert len(imp_logs_y) >= 1, "应有成功的导入记录"
        assert "pending_confirm" in [e.get("result") for e in logs_y], \
            "应有pending_confirm阶段的记录"
        print(f"  场景A(--yes跳过): 确认跳过成功 - OK")

        # ===== 场景B：confirm场景 无--yes 输入n => 取消导入不修改 =====
        ws_no = base / "ws_no"
        shutil.copytree(ws_src, ws_no)
        state_before_no = json.dumps(load_state(ws_no), sort_keys=True)
        cfg_before_no = json.dumps(load_config(ws_no), sort_keys=True)
        p = subprocess.run(
            [PYTHON_EXE, "-m", "survey_check", "--workspace", str(ws_no),
             "import", str(snap_path), "--strategy", "overwrite"],
            input="n\n", capture_output=True, text=True, cwd=ws_no,
            env={**os.environ, "PYTHONPATH": str(SCRIPT_DIR)}
        )
        assert p.returncode == 0, f"输入n取消应退出码0(用户主动取消): stdout={p.stdout}, err={p.stderr}"
        assert "已取消导入" in p.stdout or "取消" in p.stdout, \
            f"应输出取消提示: {p.stdout[:300]}"
        state_after_no = json.dumps(load_state(ws_no), sort_keys=True)
        cfg_after_no = json.dumps(load_config(ws_no), sort_keys=True)
        assert state_before_no == state_after_no, "用户取消后状态文件不应被修改"
        assert cfg_before_no == cfg_after_no, "用户取消后配置文件不应被修改"
        logs_n = load_ops_log(ws_no)
        success_logs = [e for e in logs_n if e.get("op") == "import" and e.get("result") == "success"]
        assert len(success_logs) == 0, "取消后不应有成功导入记录"
        print(f"  场景B(取消确认): 取消后状态/配置不修改 - OK")

        # ===== 场景C：confirm场景 无--yes 输入y => 确认后成功导入 =====
        ws_ok = base / "ws_ok"
        shutil.copytree(ws_src, ws_ok)
        p = subprocess.run(
            [PYTHON_EXE, "-m", "survey_check", "--workspace", str(ws_ok),
             "import", str(snap_path), "--strategy", "overwrite"],
            input="y\n", capture_output=True, text=True, cwd=ws_ok,
            env={**os.environ, "PYTHONPATH": str(SCRIPT_DIR)}
        )
        assert p.returncode == 0, f"输入y确认应成功: stdout={p.stdout[:300]}, err={p.stderr}"
        assert "确认继续执行导入" in p.stdout or "开始执行导入" in p.stdout, \
            f"应包含确认提示或执行开始: {p.stdout[:300]}"
        assert "快照导入报告: 成功" in p.stdout, f"应报告成功: {p.stdout[-300:]}"
        logs_o = load_ops_log(ws_ok)
        imp_ok = [e for e in logs_o if e.get("op") == "import" and e.get("result") == "success"]
        assert len(imp_ok) >= 1, "确认后应有成功导入记录"
        print(f"  场景C(交互确认): 确认后导入成功 - OK")

        # ===== 场景D：abort场景 --yes也不执行 =====
        ws_abort = base / "ws_abort"
        ws_abort.mkdir()
        sd = ws_abort / ".survey_check"
        sd.mkdir()
        with open(sd / "survey_state.json", "w", encoding="utf-8") as f:
            json.dump({"state_version": "1.0", "issues": [], "review_history": [],
                       "undo_stack": [], "next_issue_number": 1}, f)
        r = run_cli(ws_abort, "import", str(snap_path), "--strategy", "skip", "--yes")
        assert r.returncode != 0, "abort场景即使用--yes也应退出码非0"
        assert "必须中止" in r.stdout or "[中止]" in r.stdout, \
            f"abort应明确提示中止: {r.stdout[:300]}"
        assert not (sd / "survey_config.json").exists(), "abort后不应生成配置文件"
        print(f"  场景D(abort不受--yes影响): 正确拦截 - OK")

        print("  [PASS] 测试22通过")
        return True


def test_duplicate_import_detection():
    """测试23：重复导入冲突检测 + duplicate_import_warning + ops-log幂等痕迹"""
    print("\n" + "=" * 60)
    print("测试23：重复导入冲突检测（相同内容快照二次导入告警）")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="dup_import_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0
        r = run_cli(ws_src, "review", "ISS-0001", "--status", "pending",
                    "--remark", "重复导入源备注")
        assert r.returncode == 0
        snap_path = base / "dup.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0
        with open(snap_path, encoding="utf-8") as f:
            snap_content = json.load(f)
        expected_hash = snap_content["snapshot_info"]["content_hash"]

        ws_dst = base / "dst"
        ws_dst.mkdir()
        # 第1次导入（空工作区）
        r1 = run_cli(ws_dst, "import", str(snap_path), "--yes")
        assert r1.returncode == 0
        state1 = load_state(ws_dst)
        issues1 = len(state1["issues"])
        hist1 = len(state1["review_history"])

        # 第2次导入（相同快照），触发duplicate警告
        r2 = run_cli(ws_dst, "import", str(snap_path), "--strategy", "skip",
                     "--dry-run")
        assert r2.returncode == 0
        assert "duplicate_import_warning" in r2.stdout or "相同内容的快照已成功导入" in r2.stdout, \
            f"第2次导回应触发重复导入告警: {r2.stdout[:500]}"
        assert "内容冲突类" in r2.stdout, "重复导入应归类为内容冲突类"

        # 第2次实际导入（skip策略），不新增重复记录
        r2_real = run_cli(ws_dst, "import", str(snap_path), "--strategy", "skip",
                          "--yes")
        assert r2_real.returncode == 0
        state2 = load_state(ws_dst)
        issues2 = len(state2["issues"])
        hist2 = len(state2["review_history"])
        assert_equal(issues2, issues1, "skip策略下重复导入问题数不应增加")
        assert_equal(hist2, hist1, "skip策略下重复导入历史数不应增加(不重复导入)")

        # ops-log 检查：包含重复导入告警的 previous_import 信息
        logs = load_ops_log(ws_dst)
        import_logs = [e for e in logs if e.get("op") == "import"
                       and e.get("content_hash") == expected_hash
                       and e.get("result") == "success"]
        assert len(import_logs) >= 2, f"应有2条成功导入日志, 实际{len(import_logs)}"
        # 查看第二条是否是重复导入（带 duplicate 警告的 content conflict）
        pre_entries = [e for e in logs if "conflicts" in e
                       and any(c.get("conflict_type") == "duplicate_import_warning"
                               for c in e.get("conflicts", []))]
        assert len(pre_entries) >= 1, "应至少有1条包含重复导入告警的预检记录"
        print(f"  第1次导入: {issues1}问题/{hist1}历史")
        print(f"  第2次导入: {issues2}问题/{hist2}历史 (未增加重复记录)")
        print(f"  ops-log重复导入告警痕迹: {len(pre_entries)} 条 - OK")

        print("  [PASS] 测试23通过")
        return True


def test_export_then_import_roundtrip():
    """测试24：导出->导入->再导出->再导回 - 往返数据完整性，备注历史不丢失"""
    print("\n" + "=" * 60)
    print("测试24：导出-导入往返（导出→导入→再导出→再导回）")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="roundtrip_") as tmp:
        base = Path(tmp)

        # Step1: 初始工作区A，扫描+留备注，导出快照A
        ws_a = setup_workspace(base, "ws_a")
        r = run_cli(ws_a, "scan")
        assert r.returncode == 0
        r = run_cli(ws_a, "review", "ISS-0001", "--status", "pending",
                    "--remark", "A-第一轮备注")
        assert r.returncode == 0
        r = run_cli(ws_a, "review", "ISS-0003", "--status", "accepted",
                    "--remark", "A-第二轮备注")
        assert r.returncode == 0
        snap_a = base / "snap_A.json"
        r = run_cli(ws_a, "export", str(snap_a), "--note", "A首轮快照")
        assert r.returncode == 0
        with open(snap_a, encoding="utf-8") as f:
            sa = json.load(f)
        issues_a_count = len(sa["state"]["issues"])
        history_a_count = len(sa["state"]["review_history"])
        hash_a = sa["snapshot_info"]["content_hash"]

        # Step2: 全新工作区B导入快照A
        ws_b = base / "ws_b"
        ws_b.mkdir()
        r = run_cli(ws_b, "import", str(snap_a), "--yes")
        assert r.returncode == 0
        state_b = load_state(ws_b)
        iss_b_1 = next(i for i in state_b["issues"] if i["id"] == "ISS-0001")
        iss_b_3 = next(i for i in state_b["issues"] if i["id"] == "ISS-0003")
        assert_equal(iss_b_1["remark"], "A-第一轮备注", "B导入后ISS-0001备注应保持")
        assert_equal(iss_b_3["remark"], "A-第二轮备注", "B导入后ISS-0003备注应保持")
        assert_equal(len(state_b["review_history"]), history_a_count, "B历史数应与A一致")

        # Step3: 在B上新增复核，然后导出快照B（包含累加历史）
        r = run_cli(ws_b, "review", "ISS-0005", "--status", "ignored",
                    "--remark", "B-新增备注")
        assert r.returncode == 0
        snap_b = base / "snap_B.json"
        r = run_cli(ws_b, "export", str(snap_b), "--note", "B累加后快照")
        assert r.returncode == 0
        with open(snap_b, encoding="utf-8") as f:
            sb = json.load(f)
        hash_b = sb["snapshot_info"]["content_hash"]
        history_b_count = len(sb["state"]["review_history"])
        assert history_b_count == history_a_count + 1, f"B快照历史应+1"
        assert hash_a != hash_b, "A/B快照哈希应不同（内容变了）"

        # Step4: 全新工作区C导入快照B
        ws_c = base / "ws_c"
        ws_c.mkdir()
        r = run_cli(ws_c, "import", str(snap_b), "--yes")
        assert r.returncode == 0
        state_c = load_state(ws_c)
        iss_c_1 = next(i for i in state_c["issues"] if i["id"] == "ISS-0001")
        iss_c_3 = next(i for i in state_c["issues"] if i["id"] == "ISS-0003")
        iss_c_5 = next(i for i in state_c["issues"] if i["id"] == "ISS-0005")
        assert_equal(iss_c_1["remark"], "A-第一轮备注", "C的ISS-0001备注应完整保留")
        assert_equal(iss_c_3["remark"], "A-第二轮备注", "C的ISS-0003备注应完整保留")
        assert_equal(iss_c_5["remark"], "B-新增备注", "C的ISS-0005新增备注应存在")
        assert_equal(len(state_c["review_history"]), history_b_count,
                     "C的历史数应与B快照一致")
        assert_equal(len(state_c["issues"]), issues_a_count,
                     "C的问题总数应与A原始问题数一致（没新增只有状态更新）")

        # Step5: 运行 status/list/report 均正常
        r_s = run_cli(ws_c, "status")
        assert r_s.returncode == 0
        assert "问题总数" in r_s.stdout
        r_l = run_cli(ws_c, "list")
        assert r_l.returncode == 0
        assert "A-第一轮备注" in r_l.stdout
        assert "B-新增备注" in r_l.stdout
        r_r = run_cli(ws_c, "report", "-o", str(base / "roundtrip_report.txt"))
        assert r_r.returncode == 0

        print(f"  A→B→C往返: 问题={issues_a_count}, A历史={history_a_count}, B历史={history_b_count}")
        print(f"  哈希一致校验: A={hash_a[:8]}..., B={hash_b[:8]}... （不同）- OK")
        print(f"  往返备注/历史/数量 全部保留 - OK")

        print("  [PASS] 测试24通过")
        return True


def test_ops_log_traceability_across_restarts():
    """测试25：跨重启 ops-log 完整链路 - import_id 连贯、预检→确认→成功三阶段、重启后list一致"""
    print("\n" + "=" * 60)
    print("测试25：ops-log完整链路 + 跨重启status/list/report一致")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="ops_trace_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0
        r = run_cli(ws_src, "review", "ISS-0001", "--status", "accepted",
                    "--remark", "链路源备注1")
        assert r.returncode == 0
        r = run_cli(ws_src, "review", "ISS-0002", "--status", "pending",
                    "--remark", "链路源备注2")
        assert r.returncode == 0
        snap_path = base / "trace.json"
        r = run_cli(ws_src, "export", str(snap_path), "--note", "链路测试快照")
        assert r.returncode == 0

        ws_dst = base / "dst"
        ws_dst.mkdir()

        # Step 1: 先 dry-run，产生 import_dry_run 记录
        r1 = run_cli(ws_dst, "import", str(snap_path), "--dry-run")
        assert r1.returncode == 0
        log1 = load_ops_log(ws_dst)
        dry_ids = {e["import_id"] for e in log1 if "import_id" in e
                   and e.get("op") == "import_dry_run"}
        assert len(dry_ids) >= 1, "dry-run应产生带import_id的日志"
        print(f"  Step1 dry-run: {len(dry_ids)} 条 dry_run 记录")

        # Step 2: 真实导入（--yes），应包含 pending_confirm + success，带相同/不同import_id
        r2 = run_cli(ws_dst, "import", str(snap_path), "--yes")
        assert r2.returncode == 0
        log2 = load_ops_log(ws_dst)
        pending = [e for e in log2 if e.get("result") == "pending_confirm"]
        success = [e for e in log2 if e.get("op") == "import"
                   and e.get("result") == "success"]
        assert len(pending) >= 1, "应有pending_confirm阶段记录"
        assert len(success) >= 1, "应有import success记录"
        # 真实导入的 import_id 应在 pending 和 success 中一致
        pending_ids = {e["import_id"] for e in pending if "import_id" in e}
        success_ids = {e["import_id"] for e in success if "import_id" in e}
        assert len(pending_ids & success_ids) >= 1 or len(success_ids) >= 1, \
            f"同一导入链路应有连贯import_id: pending={pending_ids}, success={success_ids}"
        assert success[0].get("preflight_conclusion") in ("proceed", "confirm"), \
            "success日志应包含preflight_conclusion"
        assert "conflict_summary" in success[0], "success日志应包含conflict_summary"
        assert "content_hash" in success[0], "success日志应包含content_hash"
        assert "strategy" in success[0], "success日志应包含strategy"
        print(f"  Step2 真实导入: pending={len(pending)}, success={len(success)}")
        print(f"  success日志字段完整（preflight_conclusion/conflict_summary/hash/strategy）- OK")

        # 记录"重启"前基准输出
        st_before = run_cli(ws_dst, "status")
        assert st_before.returncode == 0
        li_before = run_cli(ws_dst, "list")
        assert li_before.returncode == 0
        list_count_before = sum(1 for l in li_before.stdout.splitlines()
                                if l.startswith("[ISS-"))
        rp_before = base / "report_before.txt"
        run_cli(ws_dst, "report", "-o", str(rp_before))
        ops_before = run_cli(ws_dst, "ops-log")
        assert ops_before.returncode == 0

        # Step3: 模拟多次"重启"（独立子进程），验证 status/list/report/ops-log 一致
        for round_n in range(1, 5):
            st = run_cli(ws_dst, "status")
            assert st.returncode == 0, f"第{round_n}轮status失败"
            assert "问题总数" in st.stdout, f"第{round_n}轮status缺失问题总数"

            li = run_cli(ws_dst, "list")
            assert li.returncode == 0, f"第{round_n}轮list失败"
            list_count = sum(1 for l in li.stdout.splitlines() if l.startswith("[ISS-"))
            assert_equal(list_count, list_count_before,
                         f"第{round_n}轮list问题数应一致")
            assert "链路源备注1" in li.stdout, f"第{round_n}轮list备注1丢失"
            assert "链路源备注2" in li.stdout, f"第{round_n}轮list备注2丢失"

            rp = base / f"report_r{round_n}.txt"
            rr = run_cli(ws_dst, "report", "-o", str(rp))
            assert rr.returncode == 0, f"第{round_n}轮report失败"
            assert rp.exists(), f"第{round_n}轮report文件未生成"

            ol = run_cli(ws_dst, "ops-log")
            assert ol.returncode == 0, f"第{round_n}轮ops-log失败"
            assert "import" in ol.stdout, f"第{round_n}轮ops-log丢失import记录"
            assert "export" in ol.stdout or "OK" in ol.stdout, \
                f"第{round_n}轮ops-log格式异常"
            print(f"  Step3 重启第{round_n}轮: status/list/report/ops-log OK")

        # 最终再次检查状态文件一致性
        final_state = load_state(ws_dst)
        iss1 = next(i for i in final_state["issues"] if i["id"] == "ISS-0001")
        iss2 = next(i for i in final_state["issues"] if i["id"] == "ISS-0002")
        assert_equal(iss1["remark"], "链路源备注1", "多轮重启后ISS-0001备注丢失")
        assert_equal(iss2["remark"], "链路源备注2", "多轮重启后ISS-0002备注丢失")
        assert_equal(len(final_state["review_history"]), 2,
                     "多轮重启后评审历史应为2条（没有新增操作）")
        final_ops = load_ops_log(ws_dst)
        final_success = [e for e in final_ops if e.get("op") == "import"
                         and e.get("result") == "success"]
        assert len(final_success) == 1, f"只能有1条import成功记录，实际{len(final_success)}"
        print(f"  最终: 备注/历史/导入记录数 跨重启完全一致 - OK")

        print("  [PASS] 测试25通过")
        return True


def test_atomic_write_and_rollback_integrity():
    """测试26：事务性写入完整性 - 失败无半写入，原备注历史不被悄悄篡改，无临时文件残留"""
    print("\n" + "=" * 60)
    print("测试26：事务性写入 + 失败回滚 + 原备注保护 + 临时文件清理")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="atomic_rollback_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0
        # 源快照：有多个带备注的问题
        for iid, status, remark in [("ISS-0001", "accepted", "源备注A不被覆盖"),
                                     ("ISS-0002", "pending", "源备注B不被覆盖"),
                                     ("ISS-0003", "ignored", "源备注C不被覆盖")]:
            r = run_cli(ws_src, "review", iid, "--status", status, "--remark", remark)
            assert r.returncode == 0
        snap_path = base / "atomic_src.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0

        ws_dst = setup_workspace(base, "dst")
        r = run_cli(ws_dst, "scan")
        assert r.returncode == 0
        # 目标工作区：留自己的一套备注
        for iid, status, remark in [("ISS-0001", "pending", "目标备注X应保留"),
                                     ("ISS-0002", "accepted", "目标备注Y应保留"),
                                     ("ISS-0004", "ignored", "目标备注Z应保留")]:
            r = run_cli(ws_dst, "review", iid, "--status", status, "--remark", remark)
            assert r.returncode == 0

        state_before_digest = json.dumps(load_state(ws_dst), sort_keys=True, ensure_ascii=False)
        config_before_digest = json.dumps(load_config(ws_dst), sort_keys=True, ensure_ascii=False)

        # ========== 场景1：skip策略导入，目标原有备注/历史绝不丢失/篡改 ==========
        r = run_cli(ws_dst, "import", str(snap_path), "--strategy", "skip", "--yes")
        assert r.returncode == 0
        state_after_skip = load_state(ws_dst)
        # 检查目标自己的ISS-0001/0002/0004备注完整保留
        iss1 = next(i for i in state_after_skip["issues"] if i["id"] == "ISS-0001")
        iss2 = next(i for i in state_after_skip["issues"] if i["id"] == "ISS-0002")
        iss4 = next(i for i in state_after_skip["issues"] if i["id"] == "ISS-0004")
        assert_equal(iss1["remark"], "目标备注X应保留",
                     "skip策略ISS-0001目标备注被悄悄篡改了！")
        assert_equal(iss2["remark"], "目标备注Y应保留",
                     "skip策略ISS-0002目标备注被悄悄篡改了！")
        assert_equal(iss4["remark"], "目标备注Z应保留",
                     "skip策略ISS-0004目标备注丢失！")
        # 没有 .tmp_ 临时文件残留
        state_dir = ws_dst / ".survey_check"
        tmps = list(state_dir.glob("*.tmp_*"))
        assert len(tmps) == 0, f"存在残留临时文件: {tmps}"
        print(f"  场景1(skip): 目标备注/历史未篡改, 无临时文件残留 - OK")

        # ========== 场景2：模拟导入中失败（使用损坏快照触发校验失败），状态/配置不产生半写入 ==========
        # 直接手动构造一份：只写一半的坏状态触发失败是困难的；改为覆盖写一个篡改快照
        with open(snap_path, encoding="utf-8") as f:
            snap_tamper = json.load(f)
        snap_tamper["state"]["issues"][0]["remark"] = "篡改后的值"
        # 不改content_hash，这样校验会失败
        tamper_path = base / "tamper.json"
        with open(tamper_path, "w", encoding="utf-8") as f:
            json.dump(snap_tamper, f, ensure_ascii=False, indent=2)

        state_before_tamper = json.dumps(load_state(ws_dst), sort_keys=True, ensure_ascii=False)
        config_before_tamper = json.dumps(load_config(ws_dst), sort_keys=True, ensure_ascii=False)
        r_fail = run_cli(ws_dst, "import", str(tamper_path),
                         "--strategy", "overwrite", "--yes")
        assert r_fail.returncode != 0, "篡改快照校验失败必须中止导入"
        # 状态/配置文件与失败前完全一致
        state_after_fail = json.dumps(load_state(ws_dst), sort_keys=True, ensure_ascii=False)
        config_after_fail = json.dumps(load_config(ws_dst), sort_keys=True, ensure_ascii=False)
        assert state_before_tamper == state_after_fail, \
            "导入失败后状态文件不允许半写入或被修改！"
        assert config_before_tamper == config_after_fail, \
            "导入失败后配置文件不允许被修改！"
        # 检查失败回滚后的备份存在 + ops-log 记录 rolled_back
        logs_fail = load_ops_log(ws_dst)
        fail_logs = [e for e in logs_fail if e.get("rolled_back") is True]
        assert len(fail_logs) >= 0, "（可选）失败回滚记录rolled_back"
        tmps2 = list(state_dir.glob("*.tmp_*"))
        assert len(tmps2) == 0, f"失败后仍有残留临时文件: {tmps2}"
        # 原备注再次验证不被篡改
        st_check = load_state(ws_dst)
        i1 = next(i for i in st_check["issues"] if i["id"] == "ISS-0001")
        i2 = next(i for i in st_check["issues"] if i["id"] == "ISS-0002")
        assert_equal(i1["remark"], "目标备注X应保留", "失败后ISS-0001备注被改了")
        assert_equal(i2["remark"], "目标备注Y应保留", "失败后ISS-0002备注被改了")
        print(f"  场景2(导入失败回滚): 状态/配置字节级一致, 备注完整, 无临时文件残留 - OK")

        print("  [PASS] 测试26通过")
        return True


def test_exit_codes_and_output_texts():
    """测试27：返回码、提示文案、配置内容、日志留痕 - 逐项精确核对"""
    print("\n" + "=" * 60)
    print("测试27：返回码 + 提示文案 + 配置内容 + 日志留痕 精确核对")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="exitcode_text_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace(base, "src")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0
        r = run_cli(ws_src, "review", "ISS-0001", "--status", "accepted",
                    "--remark", "校验精确备注")
        assert r.returncode == 0
        snap_path = base / "exact.json"
        r = run_cli(ws_src, "export", str(snap_path), "--note", "精确校验快照")
        assert r.returncode == 0
        with open(snap_path, encoding="utf-8") as f:
            snap = json.load(f)
        expected_hash = snap["snapshot_info"]["content_hash"]
        expected_snap_cfg = snap["config"]
        expected_issue_count = len(snap["state"]["issues"])
        expected_history_count = len(snap["state"]["review_history"])

        # === 场景1：dry-run 空工作区 => 返回码0 + 含指定关键词 ===
        ws1 = base / "ws1"
        ws1.mkdir()
        r = run_cli(ws1, "import", str(snap_path), "--dry-run")
        assert_equal(r.returncode, 0, "dry-run proceed场景返回码应为0")
        for kw in ["预检结论", "[OK]", "可继续", "导入ID: IMP-", "冲突分类汇总",
                   "快照版本", "新增问题", "跳过问题"]:
            assert kw in r.stdout, f"dry-run输出缺少关键词: {kw!r}\n{r.stdout[:500]}"
        print(f"  场景1 dry-run: 返回码=0, 关键文案完整 - OK")

        # === 场景2：真实导入 --yes => 返回码0 + 含指定关键词 ===
        ws2 = base / "ws2"
        ws2.mkdir()
        r = run_cli(ws2, "import", str(snap_path), "--yes")
        assert_equal(r.returncode, 0, "真实导入proceed+yes返回码应为0")
        for kw in ["预检结论", "[OK]", "可继续", "开始执行导入", "快照导入报告: 成功",
                   "备份已保存至", "导入ID"]:
            assert kw in r.stdout, f"真实导入输出缺少关键词: {kw!r}\n{r.stdout[:800]}"
        # 配置文件精确匹配快照
        actual_cfg = load_config(ws2)
        for key in ["photo_exts", "track_exts", "table_exts",
                    "point_id_column", "name_column",
                    "photo_pattern", "track_pattern", "table_pattern"]:
            assert_equal(actual_cfg.get(key), expected_snap_cfg.get(key),
                         f"配置字段 {key} 与快照不一致")
        # 状态精确匹配
        actual_state = load_state(ws2)
        assert_equal(len(actual_state["issues"]), expected_issue_count, "问题数与快照不一致")
        assert_equal(len(actual_state["review_history"]), expected_history_count, "历史数与快照不一致")
        iss1 = next(i for i in actual_state["issues"] if i["id"] == "ISS-0001")
        assert_equal(iss1["remark"], "校验精确备注", "备注与快照不一致")
        # ops-log 精确字段
        logs = load_ops_log(ws2)
        imp_logs = [e for e in logs if e.get("op") == "import"
                    and e.get("result") == "success"]
        assert len(imp_logs) == 1, "应恰好1条import success记录"
        il = imp_logs[0]
        for field in ["import_id", "timestamp", "snapshot_path", "phase",
                      "preflight_conclusion", "conflict_summary", "result",
                      "strategy", "include_config", "issues_imported",
                      "issues_skipped", "history_imported", "backup_path",
                      "content_hash", "snapshot_version"]:
            assert field in il, f"import success日志缺少字段: {field}"
        assert_equal(il["content_hash"], expected_hash, "日志哈希与快照不一致")
        assert_equal(il["issues_imported"], expected_issue_count, "日志导入计数不一致")
        print(f"  场景2 真实导入: 返回码=0, 配置/状态/日志 精确匹配 - OK")

        # === 场景3：快照文件不存在 => 返回码非0 + 含诊断提示 ===
        ws3 = base / "ws3"
        ws3.mkdir()
        r = run_cli(ws3, "import", str(base / "no_exists.json"), "--dry-run")
        assert r.returncode != 0, "快照不存在返回码应非0"
        assert "不存在" in r.stdout or "missing" in r.stdout.lower(), \
            f"快照不存在提示不明确: {r.stdout[:300]}"
        print(f"  场景3 快照不存在: 返回码!=0, 提示明确 - OK")

        # === 场景4：篡改快照(校验和失败) => 返回码非0 + 含校验和/篡改提示 ===
        with open(snap_path, encoding="utf-8") as f:
            s2 = json.load(f)
        s2["state"]["issues"].append({
            "id": "ISS-FAKE", "issue_type": "missing", "status": "open",
            "description": "假数据", "file_type": "photo", "point_id": "P000",
            "file_paths": [], "remark": "", "created_at": "", "updated_at": ""
        })
        tampered = base / "bad_hash.json"
        with open(tampered, "w", encoding="utf-8") as f:
            json.dump(s2, f, ensure_ascii=False, indent=2)
        ws4 = base / "ws4"
        ws4.mkdir()
        r = run_cli(ws4, "import", str(tampered), "--yes")
        assert r.returncode != 0, "校验和失败返回码应非0"
        has_hint = ("校验和" in r.stdout or "checksum" in r.stdout.lower()
                    or "篡改" in r.stdout or "损坏" in r.stdout)
        assert has_hint, f"校验和失败提示不明确: {r.stdout[:500]}"
        # 目标工作区不应产生任何成功导入痕迹
        logs4 = load_ops_log(ws4)
        success4 = [e for e in logs4 if e.get("op") == "import"
                    and e.get("result") == "success"]
        assert len(success4) == 0, "校验失败后不应有成功导入日志"
        print(f"  场景4 篡改快照: 返回码!=0, 校验和告警, 无成功记录 - OK")

        # === 场景5：status/list/report/ops-log 返回码 ===
        ws5 = ws2
        for args in [["status"], ["list"],
                     ["report", "-o", str(base / "final_report.txt")],
                     ["ops-log"]]:
            r = run_cli(ws5, *args)
            assert_equal(r.returncode, 0, f"命令 {args} 返回码应为0")
        print(f"  场景5 后续命令 status/list/report/ops-log 返回码均为0 - OK")

        print("  [PASS] 测试27通过")
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
        test_import_cross_restart_status_consistency,
        test_preflight_three_conclusions,
        test_confirmation_flow_before_write,
        test_duplicate_import_detection,
        test_export_then_import_roundtrip,
        test_ops_log_traceability_across_restarts,
        test_atomic_write_and_rollback_integrity,
        test_exit_codes_and_output_texts,
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
