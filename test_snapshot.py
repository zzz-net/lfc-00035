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
        assert "问题总数" in r.stdout
        assert "复核历史" in r.stdout
        assert "信息测试" in r.stdout
        print(f"  snapshot-info 输出完整")

        print("  [PASS] 测试9通过")
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
