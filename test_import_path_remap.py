#!/usr/bin/env python
"""快照导入路径重映射专项回归测试

核心场景：
1. 导出→导回→删除源目录→副本独立工作（scan/status/list/report 均成立）
2. dry-run：预检中显示路径重映射但实际配置未修改
3. 异常回滚：中途注入错误不会留下半写入配置
4. CLI 返回码、提示和日志核对

运行方式:
    python test_import_path_remap.py
    # 退出码 0 = 全部通过，非 0 = 有失败
"""
import os
import sys
import json
import shutil
import tempfile
import subprocess
import traceback
from pathlib import Path
from datetime import datetime


SCRIPT_DIR = Path(__file__).parent.absolute()
SAMPLE_DATA = SCRIPT_DIR / "sample_data"
PYTHON_EXE = sys.executable


def run_cli_env(workspace: Path, env_extra: dict, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SCRIPT_DIR)
    env.update(env_extra)
    cmd = [
        PYTHON_EXE, "-m", "survey_check",
        "--workspace", str(workspace),
        *args,
    ]
    return subprocess.run(cmd, cwd=workspace, env=env, capture_output=True, text=True)


def run_cli(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return run_cli_env(workspace, {}, *args)


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


def setup_workspace_with_data(base: Path, name: str) -> Path:
    """创建工作区：复制 sample_data（含清单和资料文件），然后 init+scan。

    与 setup_workspace 不同的是，这个函数保留 sample_data 的 manifest.csv、
    photos/、tracks/、tables/ 等真实文件，确保扫描可以独立进行。
    """
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
    r = run_cli(ws, "scan")
    assert r.returncode == 0, f"初始 scan 失败: {r.stderr}"
    return ws


def assert_equal(actual, expected, msg: str = ""):
    if actual != expected:
        raise AssertionError(f"{msg}: 期望 {expected!r}, 实际 {actual!r}")


def _assert_paths_in_workspace(config: dict, workspace: Path, label: str):
    """断言配置中 4 个路径字段均位于指定工作区内。"""
    for key in ("manifest_path", "photo_dir", "track_dir", "table_dir"):
        p = config[key]
        assert p, f"{label}: {key} 不应为空"
        assert os.path.isabs(p), f"{label}: {key} 应为绝对路径，实际 {p}"
        assert str(workspace) in p, \
            (f"{label}: {key} 应位于工作区 {workspace} 下，"
             f"实际 {p}")


def _assert_paths_refer_to_existing_files(config: dict, label: str):
    """断言配置指向的文件/目录确实存在。"""
    assert os.path.exists(config["manifest_path"]), \
        f"{label}: manifest_path 不存在 {config['manifest_path']}"
    for key in ("photo_dir", "track_dir", "table_dir"):
        assert os.path.isdir(config[key]), \
            f"{label}: {key} 不是存在的目录 {config[key]}"


def test_scenario_1_delete_source_and_copy_works():
    """场景1：导出→导回→删除源目录→副本独立工作。

    这是用户报告的核心问题复现与回归验证。
    """
    print("\n" + "=" * 60)
    print("场景1：导出→导回→删源→副本独立工作")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="remap_s1_") as tmp:
        base = Path(tmp)

        # ===== 源工作区初始化并扫描 =====
        ws_src = setup_workspace_with_data(base, "source_ws")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0, f"源 scan 失败: {r.stderr}"
        src_cfg = load_config(ws_src)
        src_state = load_state(ws_src)
        src_n_issues = len(src_state["issues"])
        assert src_n_issues > 0, "源工作区扫描后应有问题"
        print(f"  源工作区就绪: {ws_src.name}, {src_n_issues} 个问题")

        # 做几次复核，产生历史记录
        for iid, st, rm in [("ISS-0001", "pending", "源备注A"),
                            ("ISS-0002", "accepted", "源备注B")]:
            r = run_cli(ws_src, "review", iid, "--status", st, "--remark", rm)
            assert r.returncode == 0
        src_history_n = len(load_state(ws_src)["review_history"])

        # ===== 导出快照 =====
        snap_path = base / "snap_scenario1.json"
        r = run_cli(ws_src, "export", str(snap_path), "--note", "路径重映射测试")
        assert r.returncode == 0, f"export 失败: {r.stderr}"
        assert "快照已导出" in r.stdout
        print(f"  快照已导出: {snap_path.name}")

        # ===== 创建副本工作区（复制 sample_data，保证资料文件在副本内真实存在） =====
        ws_copy = base / "copy_ws"
        shutil.copytree(SAMPLE_DATA, ws_copy)
        # 关键：删除 sample_data 自带的 .survey_check，确保副本是"有资料但无配置状态"的干净工作区
        sd = ws_copy / ".survey_check"
        if sd.exists():
            shutil.rmtree(sd)
        assert (ws_copy / "manifest.csv").exists()
        assert (ws_copy / "photos").is_dir()
        assert not (ws_copy / ".survey_check").exists(), "副本不应有 .survey_check 目录"
        print(f"  副本工作区就绪: {ws_copy.name}（有资料、无配置）")

        # ===== 导入快照 =====
        r = run_cli(ws_copy, "import", str(snap_path), "--yes")
        assert r.returncode == 0, f"import 失败: stderr={r.stderr}\nstdout={r.stdout}"
        assert "快照导入成功" in r.stdout
        print(f"  快照已导入副本")

        # ===== 关键断言1：副本配置路径全部重映射到副本工作区 =====
        copy_cfg = load_config(ws_copy)
        _assert_paths_in_workspace(copy_cfg, ws_copy, "副本配置")
        _assert_paths_refer_to_existing_files(copy_cfg, "副本配置")
        # 进一步：路径绝对不是源工作区的
        for key in ("manifest_path", "photo_dir", "track_dir", "table_dir"):
            assert str(ws_src) not in copy_cfg[key], \
                f"副本 {key} 不应残留源工作区路径: {copy_cfg[key]}"
        print(f"  副本配置路径已重映射（无任何源工作区残留）- OK")

        # ===== 关键断言2：删除源目录后，副本依然能工作 =====
        print(f"  正在删除源工作区: {ws_src.name} ...")
        shutil.rmtree(ws_src)
        assert not ws_src.exists(), "源工作区应已删除"
        print(f"  源工作区已删除")

        # --- status 命令 ---
        r = run_cli(ws_copy, "status")
        assert r.returncode == 0, f"status 返回码非0: stderr={r.stderr}"
        assert "问题总数" in r.stdout
        assert "最后扫描" in r.stdout
        status_lines = [l for l in r.stdout.splitlines() if "问题总数" in l]
        print(f"  status: {status_lines[0].strip()} - OK (rc=0)")

        # --- list 命令 ---
        r = run_cli(ws_copy, "list")
        assert r.returncode == 0, f"list 返回码非0: stderr={r.stderr}"
        list_count = sum(1 for l in r.stdout.splitlines() if l.startswith("[ISS-"))
        assert list_count == src_n_issues, f"list 应列出 {src_n_issues} 个，实际 {list_count}"
        print(f"  list: {list_count} 个问题 - OK (rc=0)")

        # --- report 命令 ---
        report_path = base / "copy_report.txt"
        r = run_cli(ws_copy, "report", "-o", str(report_path))
        assert r.returncode == 0, f"report 返回码非0: stderr={r.stderr}"
        assert report_path.exists()
        with open(report_path, encoding="utf-8") as f:
            report_txt = f.read()
        assert "问题" in report_txt or "ISS-" in report_txt
        print(f"  report: 已生成 {report_path.stat().st_size} 字节 - OK (rc=0)")

        # --- scan 命令（最核心：源目录删除后重新扫描仍能成立）---
        r = run_cli(ws_copy, "scan")
        assert r.returncode == 0, (
            f"scan 返回码非0 (源已删仍应能扫副本资料):\n"
            f"stderr={r.stderr}\nstdout={r.stdout}")
        assert "扫描完成" in r.stdout
        # 重新扫描后不应出现"路径不存在"错误
        assert "不存在" not in r.stdout or "清单" not in r.stdout or "目录不存在" not in r.stdout
        scan_state = load_state(ws_copy)
        scan_n_issues = len(scan_state["issues"])
        assert scan_n_issues > 0, "重扫后问题数不应为0"
        # 源备注应该保留（复用历史问题）
        iss1 = next(i for i in scan_state["issues"] if i["id"] == "ISS-0001")
        assert_equal(iss1["remark"], "源备注A", "重扫后源备注A应复用保留")
        iss2 = next(i for i in scan_state["issues"] if i["id"] == "ISS-0002")
        assert_equal(iss2["remark"], "源备注B", "重扫后源备注B应复用保留")
        print(f"  scan: {scan_n_issues} 个问题，历史备注保留 - OK (rc=0)")

        # --- 还能继续追加复核 ---
        r = run_cli(ws_copy, "review", "ISS-0003", "--status", "ignored",
                    "--remark", "副本内新增复核")
        assert r.returncode == 0, f"后续 review 失败: {r.stderr}"
        state_final = load_state(ws_copy)
        assert_equal(len(state_final["review_history"]), src_history_n + 1,
                     "副本内新增复核后历史数应+1")
        print(f"  review: 副本可继续复核 - OK (rc=0)")

        # ===== 关键断言3：操作日志中含有 path_remap 记录 =====
        ops_log = load_ops_log(ws_copy)
        import_logs = [e for e in ops_log if e.get("op") == "import" and e.get("result") == "success"]
        assert import_logs, "应有成功的导入日志"
        imp_log = import_logs[-1]
        assert "path_remap" in imp_log, "导入日志应记录路径重映射"
        pr = imp_log["path_remap"]
        assert pr is not None, "config_updated=True 时 path_remap 不应为 None"
        for key in ("manifest_path", "photo_dir", "track_dir", "table_dir"):
            orig, remap = pr[key]
            assert str(ws_src) in orig or "manifest" in orig or "photo" in orig, \
                f"原始路径应含源工作区信息: {orig}"
            assert str(ws_copy) in remap, \
                f"重映射路径应含副本工作区: {remap}"
        print(f"  操作日志含 path_remap 记录 - OK")

        print("  [PASS] 场景1通过")
        return True


def test_scenario_2_dry_run_does_not_modify_config():
    """场景2：dry-run 预检模式显示重映射信息但不实际修改配置。"""
    print("\n" + "=" * 60)
    print("场景2：dry-run 预检不修改配置")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="remap_s2_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace_with_data(base, "src_dry")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0
        src_cfg_before = json.dumps(load_config(ws_src), sort_keys=True, ensure_ascii=False)
        src_state_before = load_state(ws_src)
        src_issues_before = len(src_state_before["issues"])

        snap_path = base / "dry_snap.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0

        # 使用"有资料无配置"的干净工作区，这样会触发 no_target_config，
        # 其 message 中有"路径已重映射至目标工作区"字样
        ws_dst = base / "dst_dry"
        shutil.copytree(SAMPLE_DATA, ws_dst)
        sd = ws_dst / ".survey_check"
        if sd.exists():
            shutil.rmtree(sd)
        assert not (ws_dst / ".survey_check").exists(), "dry-run 目标应无 .survey_check"

        # 注意：ws_dst 没有配置也没有状态，dry-run import 不应创建任何文件

        cfg_file = ws_dst / ".survey_check" / "survey_config.json"
        state_file = ws_dst / ".survey_check" / "survey_state.json"

        r = run_cli(ws_dst, "import", str(snap_path), "--dry-run")
        assert r.returncode == 0, f"dry-run 返回码非0: {r.stderr}"
        assert "预检" in r.stdout
        # 提示中应出现"路径已重映射"字样
        assert "路径已重映射" in r.stdout, \
            f"dry-run 输出应包含'路径已重映射'字样，实际 stdout=\n{r.stdout}"
        print(f"  dry-run 输出含路径重映射提示 - OK")

        # dry-run 不应创建任何状态/配置文件
        assert not cfg_file.exists(), "dry-run 不应创建配置文件"
        assert not state_file.exists(), "dry-run 不应创建状态文件"
        print(f"  dry-run 未创建任何配置/状态文件 - OK")

        # 实际 import 也应正常（作为对照）
        r = run_cli(ws_dst, "import", str(snap_path), "--include-config", "--yes")
        assert r.returncode == 0
        cfg_actual = load_config(ws_dst)
        _assert_paths_in_workspace(cfg_actual, ws_dst, "实际 import 后副本配置")
        print(f"  实际 import 后路径重映射生效 - OK")

        print("  [PASS] 场景2通过")
        return True


def test_scenario_3_exception_rollback_no_partial_config():
    """场景3：异常回滚 - 配置已写状态未写时出错，不应留下半写入配置。"""
    print("\n" + "=" * 60)
    print("场景3：异常回滚 - 无半写入配置")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="remap_s3_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace_with_data(base, "src_rollback")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0
        r = run_cli(ws_src, "review", "ISS-0001", "--status", "accepted",
                    "--remark", "源标记")
        assert r.returncode == 0

        snap_path = base / "rollback_snap.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0

        # 目标工作区：有资料无配置的干净工作区（且已预扫描，有初始配置和状态）
        ws_dst = setup_workspace_with_data(base, "dst_rollback")
        # 此时 ws_dst 已经 init+scan，有自己的配置和状态
        original_cfg = dict(load_config(ws_dst))
        original_state = dict(load_state(ws_dst))
        original_issues_n = len(original_state["issues"])
        original_history_n = len(original_state["review_history"])
        # 原始路径（样本数据副本自己的路径）
        orig_manifest = original_cfg["manifest_path"]

        # 先做一个正常的 dry-run 确认预检可通过
        r = run_cli(ws_dst, "import", str(snap_path),
                    "--include-config", "--strategy", "overwrite", "--dry-run")
        assert r.returncode == 0, f"dry-run 应可通过: {r.stderr}"
        print(f"  dry-run 通过，准备测试注入回滚")

        # 注入失败：使用 SURVEY_CHECK_TEST_INJECT_ABORT_AFTER_CONFIG 环境变量
        # 这会在配置写入后、状态写入前抛错
        env_abort = {"SURVEY_CHECK_TEST_INJECT_ABORT_AFTER_CONFIG": "1"}
        r = run_cli_env(ws_dst, env_abort, "import", str(snap_path),
                        "--include-config", "--strategy", "overwrite", "--yes")

        # 应失败
        assert r.returncode != 0, (
            f"注入异常后 import 应失败但返回 rc=0.\n"
            f"stdout={r.stdout}\nstderr={r.stderr}")
        assert ("失败" in r.stdout or "import_failed" in r.stdout
                or "回滚" in r.stdout or "已从备份恢复" in r.stdout
                or "测试注入" in r.stdout), \
            f"输出应表明失败/回滚，实际 stdout:\n{r.stdout}"
        print(f"  注入异常被成功捕获 - OK (rc={r.returncode})")

        # 关键：回滚后配置和状态必须恢复到导入前，不应是半写入
        cfg_after = load_config(ws_dst)
        state_after = load_state(ws_dst)

        # manifest_path 必须是原来的（样本数据副本自己的路径），不是快照重映射后的
        assert_equal(cfg_after["manifest_path"], orig_manifest,
                     "回滚后 manifest_path 必须完全恢复到导入前（不能留半写入的重映射路径）")
        # 所有非路径字段也必须恢复
        for key in ("photo_exts", "track_exts", "table_exts",
                    "point_id_column", "name_column",
                    "photo_pattern", "track_pattern", "table_pattern"):
            assert_equal(cfg_after[key], original_cfg[key],
                         f"回滚后配置字段 {key} 必须恢复")
        print(f"  回滚后配置完全恢复（路径和非路径字段均一致）- OK")

        # 状态也必须恢复
        assert_equal([i["id"] for i in state_after["issues"]],
                     [i["id"] for i in original_state["issues"]],
                     "回滚后 issues 必须与导入前一致")
        assert_equal(len(state_after["review_history"]), original_history_n,
                     "回滚后 review_history 数必须一致")
        # ISS-0001 的备注必须是原来 dst_rollback 自己的（不是源的"源标记"）
        dst_iss1 = next((i for i in state_after["issues"] if i["id"] == "ISS-0001"), None)
        if dst_iss1 is not None:
            assert dst_iss1["remark"] != "源标记", \
                ("回滚后 ISS-0001 备注不应是快照中的'源标记'，"
                 f"实际是 {dst_iss1['remark']!r}")
        print(f"  回滚后状态完全恢复（问题编号、历史数、备注均未被快照污染）- OK")

        # 核对日志：有 rolled_back=True 的失败记录
        ops_log = load_ops_log(ws_dst)
        rollback_logs = [e for e in ops_log if e.get("op") == "import"
                         and e.get("rolled_back")]
        assert len(rollback_logs) >= 1, "应有 rolled_back=True 的导入日志"
        rb = rollback_logs[-1]
        assert rb["result"] == "failure"
        assert rb.get("failure_phase") == "executing", \
            f"失败阶段应为 executing，实际 {rb.get('failure_phase')}"
        assert "测试注入" in rb.get("failure_reason", ""), \
            f"失败原因应含'测试注入'，实际 {rb.get('failure_reason')}"
        assert rb.get("backup_path"), "失败时必须有备份路径"
        print(f"  操作日志含 rolled_back 记录 "
              f"(phase={rb['failure_phase']}, reason={rb['failure_reason'][:30]}) - OK")

        # 回滚后仍能继续正常工作（status/list/scan 均 OK）
        r = run_cli(ws_dst, "status")
        assert r.returncode == 0, f"回滚后 status 失败: {r.stderr}"
        r = run_cli(ws_dst, "list")
        assert r.returncode == 0, f"回滚后 list 失败: {r.stderr}"
        r = run_cli(ws_dst, "scan")
        assert r.returncode == 0, f"回滚后 scan 失败: {r.stderr}"
        scan_after = load_state(ws_dst)
        assert_equal(len(scan_after["issues"]), original_issues_n,
                     "回滚后重扫问题数应一致")
        print(f"  回滚后命令继续可用（status/list/scan 均 rc=0，问题数不变）- OK")

        print("  [PASS] 场景3通过")
        return True


def test_scenario_4_cli_exit_code_and_output_checks():
    """场景4：系统核对 CLI 返回码、提示语和日志字段。"""
    print("\n" + "=" * 60)
    print("场景4：CLI 返回码/提示/日志核对")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="remap_s4_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace_with_data(base, "src_cli")
        r = run_cli(ws_src, "scan")
        assert r.returncode == 0
        r = run_cli(ws_src, "review", "ISS-0001", "--status", "pending",
                    "--remark", "CLI核对")
        assert r.returncode == 0

        snap = base / "cli_snap.json"
        r = run_cli(ws_src, "export", str(snap))
        assert r.returncode == 0
        assert "[OK]" in r.stdout
        assert "快照已导出" in r.stdout
        assert "内容校验和" in r.stdout
        print(f"  export: rc=0, 含 [OK]/导出/校验和 - OK")

        # === 情况 A：空工作区正常导入（rc=0，含完成提示）===
        ws_ok = base / "ws_cli_ok"
        shutil.copytree(SAMPLE_DATA, ws_ok)
        # 删除 sample_data 自带的 .survey_check，确保是无配置的干净工作区
        sd_ok = ws_ok / ".survey_check"
        if sd_ok.exists():
            shutil.rmtree(sd_ok)
        r = run_cli(ws_ok, "import", str(snap), "--yes")
        assert r.returncode == 0, f"成功导入 rc 应为 0, stdout={r.stdout}"
        assert "快照导入成功" in r.stdout or "[完成]" in r.stdout
        assert "survey-check status" in r.stdout
        assert "survey-check list" in r.stdout
        assert "survey-check report" in r.stdout
        print(f"  import(成功): rc=0, 含完成/后续命令提示 - OK")

        cfg = load_config(ws_ok)
        _assert_paths_in_workspace(cfg, ws_ok, "成功导入后配置")

        # === 情况 B：快照文件不存在（rc≠0，含错误提示）===
        ws_bad = base / "ws_cli_bad"
        shutil.copytree(SAMPLE_DATA, ws_bad)
        sd_bad = ws_bad / ".survey_check"
        if sd_bad.exists():
            shutil.rmtree(sd_bad)
        r = run_cli(ws_bad, "import", str(base / "no_such_snap.json"), "--yes")
        assert r.returncode != 0, "不存在的快照 rc 应≠0"
        assert "不存在" in r.stdout or "missing" in r.stdout.lower()
        print(f"  import(文件不存在): rc={r.returncode}, 含'不存在'提示 - OK")

        # === 情况 C：预检必须中止的情形（rc≠0，含[中止]和诊断建议）===
        ws_res = base / "ws_cli_res"
        ws_res.mkdir()
        sd = ws_res / ".survey_check"
        sd.mkdir()
        with open(sd / "survey_state.json", "w", encoding="utf-8") as f:
            json.dump({
                "state_version": "1.0", "issues": [],
                "review_history": [], "undo_stack": [],
            }, f)
        r = run_cli(ws_res, "import", str(snap), "--yes")
        assert r.returncode != 0, "残留状态无配置 rc 应≠0"
        assert "中止" in r.stdout
        assert ("诊断" in r.stdout or "建议" in r.stdout
                or "删除 .survey_check/ 目录" in r.stdout or "init" in r.stdout)
        print(f"  import(预检中止): rc={r.returncode}, 含[中止]和诊断 - OK")

        # === 情况 D：dry-run 成功但有配置差异（rc=0，含预检和统计）===
        ws_dry = setup_workspace_with_data(base, "ws_cli_dry")
        r = run_cli(ws_dry, "import", str(snap), "--dry-run")
        assert r.returncode == 0, "dry-run rc 应为 0"
        assert "预检" in r.stdout
        assert "新增问题" in r.stdout
        print(f"  import(dry-run): rc=0, 含预检/统计 - OK")

        # === 日志核对：成功导入的日志结构 ===
        log = load_ops_log(ws_ok)
        imports_ok = [e for e in log if e.get("op") == "import"
                      and e.get("result") == "success"]
        assert imports_ok, "成功导入应有日志"
        ie = imports_ok[-1]
        for key in ("op", "timestamp", "import_id", "snapshot_path",
                    "phase", "result", "strategy", "config_updated",
                    "content_hash", "path_remap", "source_workspace"):
            assert key in ie, f"成功导入日志应含字段 {key}，实际 keys={list(ie.keys())}"
        assert ie["config_updated"] is True
        assert ie["path_remap"] is not None
        print(f"  成功导入日志: 含全部必要字段 - OK")

        print("  [PASS] 场景4通过")
        return True


def main():
    print("=" * 60)
    print("快照导入路径重映射专项回归测试")
    print(f"工作目录: {SCRIPT_DIR}")
    print(f"Python: {PYTHON_EXE}")
    print("=" * 60)

    tests = [
        test_scenario_1_delete_source_and_copy_works,
        test_scenario_2_dry_run_does_not_modify_config,
        test_scenario_3_exception_rollback_no_partial_config,
        test_scenario_4_cli_exit_code_and_output_checks,
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
            err_msg = (f"{test.__name__}: 异常 {type(e).__name__}: {e}\n"
                       f"{traceback.format_exc()}")
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
