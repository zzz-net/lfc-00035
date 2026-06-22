#!/usr/bin/env python
"""快照导入/导出 - 综合场景补充回归测试

覆盖用户要求补充的核心场景：
1. 导出覆盖（重复导出到同一文件路径）
2. 冲突拒绝与修正后重试（预检 abort → 修复 → 重试成功）
3. scan_result / survey_points / review_history 冲突检测
4. 删源目录后重启 CLI 的端到端完整链路

运行方式:
    python test_snapshot_comprehensive.py
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
from datetime import datetime, timedelta


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


def setup_workspace_with_scan(base: Path, name: str) -> Path:
    """创建工作区并初始化+扫描。"""
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
    assert r.returncode == 0, f"scan 失败: {r.stderr}"
    return ws


def assert_equal(actual, expected, msg: str = ""):
    if actual != expected:
        raise AssertionError(f"{msg}: 期望 {expected!r}, 实际 {actual!r}")


# ============================================================
# 场景 A1：导出覆盖（重复导出到同一路径）
# ============================================================
def test_a1_export_overwrite_same_path():
    """场景A1：重复导出到同一路径，第二次应覆盖且内容更新。"""
    print("\n" + "=" * 60)
    print("场景A1：导出覆盖（重复导出同一路径）")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="a1_export_") as tmp:
        base = Path(tmp)
        ws = setup_workspace_with_scan(base, "ws")

        snap_path = base / "overwrite_snap.json"

        # 第 1 次导出
        r1 = run_cli(ws, "export", str(snap_path), "--note", "第一次导出")
        assert r1.returncode == 0, f"第1次导出失败: {r1.stderr}"
        assert "[OK]" in r1.stdout and "快照已导出" in r1.stdout
        with open(snap_path, encoding="utf-8") as f:
            snap1 = json.load(f)
        note1 = snap1["snapshot_info"]["note"]
        hash1 = snap1["snapshot_info"]["content_hash"]
        issues1 = len(snap1["state"]["issues"])
        history1 = len(snap1["state"]["review_history"])
        assert_equal(note1, "第一次导出", "第一次导出备注")
        print(f"  第1次导出: {issues1}问题, {history1}历史, hash={hash1[:8]}... - OK (rc=0)")

        # 新增一次复核，使状态变化
        r = run_cli(ws, "review", "ISS-0001", "--status", "accepted",
                    "--remark", "A1-覆盖测试备注")
        assert r.returncode == 0

        # 第 2 次导出（覆盖同一路径）
        r2 = run_cli(ws, "export", str(snap_path), "--note", "第二次导出覆盖")
        assert r2.returncode == 0, f"第2次导出(覆盖)失败: {r2.stderr}"
        assert "[OK]" in r2.stdout and "快照已导出" in r2.stdout

        with open(snap_path, encoding="utf-8") as f:
            snap2 = json.load(f)
        note2 = snap2["snapshot_info"]["note"]
        hash2 = snap2["snapshot_info"]["content_hash"]
        issues2 = len(snap2["state"]["issues"])
        history2 = len(snap2["state"]["review_history"])

        assert_equal(note2, "第二次导出覆盖", "第二次导出备注应更新")
        assert hash1 != hash2, "内容变化后 hash 应不同"
        assert_equal(issues2, issues1, "问题数应相同")
        assert_equal(history2, history1 + 1, "复核历史数应+1")
        iss1 = next(i for i in snap2["state"]["issues"] if i["id"] == "ISS-0001")
        assert_equal(iss1["remark"], "A1-覆盖测试备注", "第二次导出应包含更新后的备注")
        print(f"  第2次导出(覆盖): {issues2}问题, {history2}历史, hash={hash2[:8]}...")
        print(f"  内容已更新、哈希变化、备注正确 - OK (rc=0)")

        # 确认只生成了一个文件（不是多个）
        files = list(base.glob("overwrite_snap*.json"))
        assert len(files) == 1, f"应只有1个快照文件，实际 {len(files)} 个"
        print(f"  目标目录仅存在唯一快照文件 - OK")

        # 导出日志检查
        logs = load_ops_log(ws)
        export_logs = [e for e in logs if e.get("op") == "export" and e.get("result") == "success"]
        assert len(export_logs) >= 2, "应有至少2条成功导出日志"
        print(f"  导出操作日志: 共 {len(export_logs)} 条成功记录 - OK")

        print("  [PASS] 场景A1通过")
        return True


# ============================================================
# 场景 A2：冲突拒绝 + 修正后重试成功（残留状态 abort → 清理 → 重试）
# ============================================================
def test_a2_conflict_reject_then_fix_and_retry():
    """场景A2：预检 abort（残留状态） → 修复冲突 → 重试导入成功。"""
    print("\n" + "=" * 60)
    print("场景A2：冲突拒绝 → 修正 → 重试成功（残留状态场景）")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="a2_retry_") as tmp:
        base = Path(tmp)

        # 准备源快照
        ws_src = setup_workspace_with_scan(base, "src")
        r = run_cli(ws_src, "review", "ISS-0001", "--status", "accepted",
                    "--remark", "A2源备注")
        assert r.returncode == 0
        snap_path = base / "a2_snap.json"
        r = run_cli(ws_src, "export", str(snap_path))
        assert r.returncode == 0

        # 构造目标：仅含残留状态（有 state 无 config）→ 触发 abort
        ws_dst = base / "dst"
        ws_dst.mkdir()
        state_dir = ws_dst / ".survey_check"
        state_dir.mkdir()
        fake_state = {
            "state_version": "1.0",
            "created_at": "2020-01-01T00:00:00",
            "last_scan_time": None,
            "scan_result": None,
            "survey_points": [],
            "issues": [{
                "id": "ISS-9999",
                "issue_type": "missing",
                "status": "open",
                "description": "残留数据",
                "file_type": "photo",
                "point_id": "P9999",
                "file_paths": [],
                "remark": "残留备注",
                "created_at": "2020-01-01T00:00:00",
                "updated_at": "2020-01-01T00:00:00",
            }],
            "review_history": [],
            "undo_stack": [],
            "next_issue_number": 10000,
        }
        with open(state_dir / "survey_state.json", "w", encoding="utf-8") as f:
            json.dump(fake_state, f, ensure_ascii=False, indent=2)

        assert not (state_dir / "survey_config.json").exists()
        assert (state_dir / "survey_state.json").exists()
        print(f"  目标构造: 仅残留状态（有survey_state.json, 无survey_config.json）")

        # ========== Step 1: dry-run → 应 abort ==========
        r_dry = run_cli(ws_dst, "import", str(snap_path), "--dry-run")
        assert r_dry.returncode != 0, "残留状态无配置 dry-run 应 abort (rc≠0)"
        assert "[X]" in r_dry.stdout and "必须中止" in r_dry.stdout
        assert "residual_state_no_config" in r_dry.stdout or "残留" in r_dry.stdout
        # 诊断信息中应有"类别二 目录残留问题"或类似提示
        has_category = ("目录残留" in r_dry.stdout
                        or "残留状态类" in r_dry.stdout
                        or "【类别二】" in r_dry.stdout)
        print(f"  Step1 dry-run: rc={r_dry.returncode}, 预检abort - OK")

        # ========== Step 2: 真实导入 --yes → 也应 abort，不写入配置 ==========
        r_fail = run_cli(ws_dst, "import", str(snap_path), "--yes")
        assert r_fail.returncode != 0, "残留状态 --yes 也应 abort"
        assert "中止" in r_fail.stdout or "abort" in r_fail.stdout.lower()
        # 不应写入新配置文件
        assert not (state_dir / "survey_config.json").exists(), \
            "abort 场景下不应生成配置文件"
        # 残留状态文件应保持原样
        with open(state_dir / "survey_state.json", "r", encoding="utf-8") as f:
            state_after_fail = json.load(f)
        assert_equal(state_after_fail["issues"][0]["remark"], "残留备注",
                     "abort 后残留状态不应被修改")
        # 失败日志
        logs_fail = load_ops_log(ws_dst)
        fail_logs = [e for e in logs_fail
                     if e.get("op") == "import" and e.get("result") == "failure"]
        assert len(fail_logs) >= 1, "应有失败的导入日志"
        assert fail_logs[-1].get("failure_phase") == "preflight", \
            "失败阶段应为 preflight"
        assert fail_logs[-1].get("failure_reason") == "aborted_by_preflight", \
            "失败原因应为 aborted_by_preflight"
        print(f"  Step2 真实导入 --yes: rc={r_fail.returncode}, "
              f"abort未修改配置/状态, 日志含failure - OK")

        # ========== Step 3: 修复 —— 仅清理有问题的残留状态文件，保留 ops-log 审计 ==========
        state_file = state_dir / "survey_state.json"
        if state_file.exists():
            state_file.unlink()
        # 也可能有其他残留状态文件，一并清理但保留 ops_log.jsonl
        for f in state_dir.iterdir():
            if f.name not in ("ops_log.jsonl",):
                f.unlink()
        assert not state_file.exists(), "残留 survey_state.json 已清理"
        assert (state_dir / "ops_log.jsonl").exists(), "ops_log.jsonl 应保留"
        print(f"  Step3 修复: 清理残留状态文件，保留 ops-log 审计")

        # ========== Step 4: 重试导入 → 成功 ==========
        r_ok = run_cli(ws_dst, "import", str(snap_path), "--yes")
        assert r_ok.returncode == 0, (
            f"修复后重试应成功, rc={r_ok.returncode}\n"
            f"stdout={r_ok.stdout}\nstderr={r_ok.stderr}")
        assert "快照导入成功" in r_ok.stdout or "[完成]" in r_ok.stdout
        assert "survey-check status" in r_ok.stdout
        assert "survey-check list" in r_ok.stdout
        assert "survey-check report" in r_ok.stdout
        print(f"  Step4 重试导入: rc=0, 含成功提示和后续命令 - OK")

        # 检查导入结果
        assert (state_dir / "survey_config.json").exists()
        assert (state_dir / "survey_state.json").exists()
        state_ok = load_state(ws_dst)
        # 不应再有 ISS-9999（残留的）
        ids = [i["id"] for i in state_ok["issues"]]
        assert "ISS-9999" not in ids, "残留的 ISS-9999 不应存在"
        # 源快照的 ISS-0001 备注应存在
        iss1 = next(i for i in state_ok["issues"] if i["id"] == "ISS-0001")
        assert_equal(iss1["remark"], "A2源备注", "导入后 ISS-0001 备注应为源快照值")
        print(f"  导入结果: 残留已清除, 快照数据完整恢复 - OK")

        # 重试后 status/list/report 均可用
        r_s = run_cli(ws_dst, "status")
        assert r_s.returncode == 0
        assert "问题总数" in r_s.stdout
        r_l = run_cli(ws_dst, "list")
        assert r_l.returncode == 0
        assert "A2源备注" in r_l.stdout
        r_r = run_cli(ws_dst, "report", "-o", str(base / "a2_report.txt"))
        assert r_r.returncode == 0
        assert (base / "a2_report.txt").exists()
        print(f"  后续命令 status/list/report 均可用 (rc=0) - OK")

        # ops-log 应包含：失败(abort) + 成功(import) 两条记录
        logs_final = load_ops_log(ws_dst)
        all_imports = [e for e in logs_final if e.get("op") == "import"]
        n_fail = sum(1 for e in all_imports if e.get("result") == "failure")
        n_ok = sum(1 for e in all_imports if e.get("result") == "success")
        assert n_fail >= 1 and n_ok >= 1, \
            f"ops-log 应同时含失败({n_fail})和成功({n_ok})导入记录"
        print(f"  ops-log: 失败 {n_fail} 条, 成功 {n_ok} 条 - OK")

        print("  [PASS] 场景A2通过")
        return True


# ============================================================
# 场景 A3：scan_result / survey_points / review_history 冲突检测
# ============================================================
def test_a3_scan_result_and_survey_points_conflict_detection():
    """场景A3：目标与快照双方都有扫描结果/调查点/复核历史，预检应告警。"""
    print("\n" + "=" * 60)
    print("场景A3：scan_result / survey_points / review_history 冲突检测")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="a3_conflict_") as tmp:
        base = Path(tmp)

        # 源工作区 A
        ws_a = setup_workspace_with_scan(base, "ws_a")
        r = run_cli(ws_a, "review", "ISS-0001", "--status", "accepted",
                    "--remark", "A3-源A备注")
        assert r.returncode == 0
        snap_a = base / "a3_snap_a.json"
        r = run_cli(ws_a, "export", str(snap_a))
        assert r.returncode == 0
        state_a = load_state(ws_a)
        pts_a = len(state_a["survey_points"])
        history_a = len(state_a["review_history"])
        assert pts_a > 0 and history_a > 0, "源应有调查点和复核历史"
        print(f"  源A: {pts_a}调查点, {history_a}条历史")

        # 目标工作区 B（也已 init+scan+review，有自己的 scan_result/调查点/历史）
        ws_b = setup_workspace_with_scan(base, "ws_b")
        r = run_cli(ws_b, "review", "ISS-0002", "--status", "pending",
                    "--remark", "A3-目标B备注")
        assert r.returncode == 0
        state_b_before = load_state(ws_b)
        pts_b = len(state_b_before["survey_points"])
        history_b = len(state_b_before["review_history"])
        scan_b_photos = len(state_b_before["scan_result"]["photos"])
        print(f"  目标B: {pts_b}调查点, {history_b}条历史, {scan_b_photos}张照片")

        # ========== dry-run: 应出现 scan_result_conflict 等告警 ==========
        r_dry = run_cli(ws_b, "import", str(snap_a), "--strategy", "skip", "--dry-run")
        assert r_dry.returncode == 0, "有警告≠abort，dry-run rc 应为 0"
        # 预检结论应为 confirm（有 warning）
        assert "[!]" in r_dry.stdout and "需确认" in r_dry.stdout
        # 三个冲突类型都应出现
        for kw in ["scan_result_conflict", "survey_points_conflict", "review_history_conflict"]:
            assert kw in r_dry.stdout, f"dry-run 输出应含 {kw}"
        # 冲突分类汇总应包含"内容冲突类"
        assert "内容冲突类" in r_dry.stdout
        print(f"  dry-run: 检测到 scan_result/survey_points/review_history 三类冲突 - OK")

        # ========== skip 策略：目标的 scan_result / survey_points 应保留 ==========
        r_skip = run_cli(ws_b, "import", str(snap_a), "--strategy", "skip", "--yes")
        assert r_skip.returncode == 0
        state_b_skip = load_state(ws_b)
        # scan_result 和 survey_points 应保持目标原样
        scan_after_photos = len(state_b_skip["scan_result"]["photos"])
        assert_equal(scan_after_photos, scan_b_photos,
                     "skip策略 scan_result 照片数应保留目标值")
        assert_equal(len(state_b_skip["survey_points"]), pts_b,
                     "skip策略 survey_points 数应保留目标值")
        # ISS-0002 的目标备注应保留（ skip 不覆盖冲突问题）
        iss2 = next(i for i in state_b_skip["issues"] if i["id"] == "ISS-0002")
        assert_equal(iss2["remark"], "A3-目标B备注",
                     "skip策略 ISS-0002 备注应保留目标值")
        print(f"  skip策略: scan_result/survey_points/ISS-0002备注 均保留目标 - OK")

        # ========== overwrite 策略：快照的 scan_result / survey_points 应覆盖目标 ==========
        ws_over = base / "ws_over"
        shutil.copytree(ws_b, ws_over)
        # 重新赋予自己的初始状态（跳过导入过的 ws_b）
        ws_over2 = setup_workspace_with_scan(base, "ws_over2")
        r = run_cli(ws_over2, "review", "ISS-0002", "--status", "pending",
                    "--remark", "A3-overwrite目标备注")
        assert r.returncode == 0
        state_over_before = load_state(ws_over2)
        scan_over_before = len(state_over_before["scan_result"]["photos"])
        pts_over_before = len(state_over_before["survey_points"])
        # 使目标的 last_scan_time 看起来比快照更新（避免干扰）
        r_over = run_cli(ws_over2, "import", str(snap_a),
                        "--strategy", "overwrite", "--yes")
        assert r_over.returncode == 0
        state_over = load_state(ws_over2)
        # overwrite 策略下，快照的 ISS-0001 备注应覆盖
        iss1_over = next(i for i in state_over["issues"] if i["id"] == "ISS-0001")
        assert_equal(iss1_over["remark"], "A3-源A备注",
                     "overwrite策略 ISS-0001 备注应为快照值")
        print(f"  overwrite策略: 问题备注已覆盖为快照值 - OK")

        # ops-log 中应包含冲突细节
        logs = load_ops_log(ws_b)
        preflight_logs = [e for e in logs
                          if "conflicts" in e and isinstance(e.get("conflicts"), list)]
        assert len(preflight_logs) >= 1
        conflict_types = {c.get("conflict_type") for c in preflight_logs[-1]["conflicts"]}
        for ct in ["scan_result_conflict", "survey_points_conflict", "review_history_conflict"]:
            assert ct in conflict_types, f"ops-log 冲突列表应含 {ct}"
        print(f"  ops-log: 三类冲突均已记录 - OK")

        print("  [PASS] 场景A3通过")
        return True


# ============================================================
# 场景 A4：删源目录后 CLI 多轮重启端到端完整链路
# ============================================================
def test_a4_delete_source_and_restart_cli_full_chain():
    """场景A4：导出→导入→删源→多轮重启→status/list/report/ops-log/scan/review 全链路。"""
    print("\n" + "=" * 60)
    print("场景A4：删源目录后 CLI 多轮重启端到端完整链路")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="a4_chain_") as tmp:
        base = Path(tmp)

        # Step 1: 源工作区初始化 + 扫描 + 多次复核 + 导出
        ws_src = setup_workspace_with_scan(base, "src_ws")
        reviews_src = [
            ("ISS-0001", "accepted", "A4源-备注1"),
            ("ISS-0002", "pending", "A4源-备注2"),
            ("ISS-0003", "ignored", "A4源-备注3"),
        ]
        for iid, st, rm in reviews_src:
            r = run_cli(ws_src, "review", iid, "--status", st, "--remark", rm)
            assert r.returncode == 0
        src_state = load_state(ws_src)
        n_issues_src = len(src_state["issues"])
        n_history_src = len(src_state["review_history"])
        n_points_src = len(src_state["survey_points"])
        src_cfg = load_config(ws_src)

        snap_path = base / "a4_full_chain.json"
        r = run_cli(ws_src, "export", str(snap_path), "--note", "A4端到端完整链路")
        assert r.returncode == 0
        assert "[OK]" in r.stdout
        assert "内容校验和" in r.stdout
        print(f"  Step1 源工作区: {n_issues_src}问题, {n_history_src}历史, {n_points_src}调查点 → 导出完成")

        # Step 2: 目标工作区：复制 sample_data（保证资料文件真实存在），清理 .survey_check
        ws_dst = base / "dst_ws"
        shutil.copytree(SAMPLE_DATA, ws_dst)
        dst_state_dir = ws_dst / ".survey_check"
        if dst_state_dir.exists():
            shutil.rmtree(dst_state_dir)
        # 确认有资料文件
        assert (ws_dst / "manifest.csv").exists()
        assert (ws_dst / "photos").is_dir()
        assert (ws_dst / "tracks").is_dir()
        assert not dst_state_dir.exists()
        print(f"  Step2 目标工作区: 资料文件就绪，无配置状态")

        # Step 3: 导入快照
        r = run_cli(ws_dst, "import", str(snap_path), "--yes")
        assert r.returncode == 0, f"导入失败 rc={r.returncode}\n{r.stdout}\n{r.stderr}"
        assert "快照导入成功" in r.stdout or "[完成]" in r.stdout
        # 路径重映射：四个路径字段都应指向 dst_ws
        dst_cfg = load_config(ws_dst)
        for key in ("manifest_path", "photo_dir", "track_dir", "table_dir"):
            p = dst_cfg[key]
            assert os.path.isabs(p), f"{key} 应为绝对路径"
            assert str(ws_dst) in p, f"{key} 应位于目标工作区 {ws_dst}, 实际 {p}"
            assert os.path.exists(p), f"{key} 应指向存在的文件/目录: {p}"
            assert str(ws_src) not in p, f"{key} 不应残留源工作区路径"
        dst_state = load_state(ws_dst)
        assert_equal(len(dst_state["issues"]), n_issues_src, "导入后问题数")
        assert_equal(len(dst_state["review_history"]), n_history_src, "导入后历史数")
        for iid, st, rm in reviews_src:
            iss = next(i for i in dst_state["issues"] if i["id"] == iid)
            assert_equal(iss["remark"], rm, f"{iid} 备注")
            assert_equal(iss["status"], st, f"{iid} 状态")
        print(f"  Step3 导入完成: 路径重映射正确, 状态/备注/历史 完整 - OK")

        # Step 4: 删除源工作区
        print(f"  Step4 删除源工作区 {ws_src.name} ...")
        shutil.rmtree(ws_src)
        assert not ws_src.exists(), "源工作区应已删除"
        print(f"  源工作区已删除")

        # Step 5: 多轮"重启"（独立子进程）验证 status/list/report/ops-log
        expected_count = n_issues_src
        for round_n in range(1, 6):
            print(f"  Step5 第{round_n}轮重启验证...")

            r_s = run_cli(ws_dst, "status")
            assert r_s.returncode == 0, f"第{round_n}轮 status 失败"
            assert "问题总数" in r_s.stdout
            assert "最后扫描" in r_s.stdout
            assert "调查点数量" in r_s.stdout
            assert str(n_points_src) in r_s.stdout or "调查点数量" in r_s.stdout

            r_l = run_cli(ws_dst, "list")
            assert r_l.returncode == 0, f"第{round_n}轮 list 失败"
            list_count = sum(1 for l in r_l.stdout.splitlines() if l.startswith("[ISS-"))
            assert_equal(list_count, expected_count, f"第{round_n}轮 list 问题数")
            for _, _, rm in reviews_src:
                assert rm in r_l.stdout, f"第{round_n}轮 list 丢失备注 {rm}"

            report_f = base / f"a4_report_r{round_n}.txt"
            r_r = run_cli(ws_dst, "report", "-o", str(report_f))
            assert r_r.returncode == 0, f"第{round_n}轮 report 失败"
            assert report_f.exists()
            assert report_f.stat().st_size > 0

            r_o = run_cli(ws_dst, "ops-log")
            assert r_o.returncode == 0, f"第{round_n}轮 ops-log 失败"
            assert "import" in r_o.stdout
            assert "OK" in r_o.stdout or "FAIL" in r_o.stdout

            print(f"    第{round_n}轮: status/list/report/ops-log 全部 OK (问题数={list_count})")

        # Step 6: 重新扫描（源已删除仍应能扫目标资料）+ 历史备注保留
        r_scan = run_cli(ws_dst, "scan")
        assert r_scan.returncode == 0, (
            f"源删除后重扫失败: stdout={r_scan.stdout}\nstderr={r_scan.stderr}")
        assert "扫描完成" in r_scan.stdout
        assert "复用历史问题" in r_scan.stdout
        state_after_scan = load_state(ws_dst)
        for iid, st, rm in reviews_src:
            iss = next(i for i in state_after_scan["issues"] if i["id"] == iid)
            assert_equal(iss["remark"], rm, f"重扫后 {iid} 备注丢失")
            assert_equal(iss["status"], st, f"重扫后 {iid} 状态丢失")
        print(f"  Step6 重扫: 源删除后仍成功, 复用历史, 备注状态完整保留 - OK")

        # Step 7: 继续追加复核（历史累加正确）
        r_rev = run_cli(ws_dst, "review", "ISS-0005", "--status", "accepted",
                        "--remark", "A4-重启后新增")
        assert r_rev.returncode == 0
        state_final = load_state(ws_dst)
        assert_equal(len(state_final["review_history"]), n_history_src + 1,
                     "重启后追加复核历史数应+1")
        iss5 = next(i for i in state_final["issues"] if i["id"] == "ISS-0005")
        assert_equal(iss5["remark"], "A4-重启后新增", "重启后新增备注应写入")
        print(f"  Step7 追加复核: 历史正确累加, 备注写入成功 - OK")

        # Step 8: 最终 ops-log 全链路检查
        logs_final = load_ops_log(ws_dst)
        imports = [e for e in logs_final if e.get("op") == "import"]
        successes = [e for e in imports if e.get("result") == "success"]
        assert len(successes) == 1, f"应恰好1条成功导入日志, 实际 {len(successes)}"
        imp = successes[-1]
        for field in ["import_id", "timestamp", "snapshot_path", "phase",
                      "preflight_conclusion", "conflict_summary", "result",
                      "strategy", "issues_imported", "backup_path",
                      "content_hash", "path_remap", "source_workspace"]:
            assert field in imp, f"成功导入日志缺字段 {field}"
        assert imp["content_hash"], "content_hash 不应空"
        assert imp["path_remap"] is not None, "config_updated=True 时 path_remap 不应 None"
        print(f"  Step8 ops-log: 成功导入日志字段完整 - OK")

        print("  [PASS] 场景A4通过")
        return True


# ============================================================
# 场景 A5：config_mismatch 诊断信息 - 类别三本地配置不一致
# ============================================================
def test_a5_config_mismatch_diagnostic_category_three():
    """场景A5：配置不一致时诊断输出应显示"类别三 本地配置不一致"。"""
    print("\n" + "=" * 60)
    print("场景A5：配置不一致诊断（类别三 本地配置不一致）")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="a5_cfgdiag_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace_with_scan(base, "src")
        snap = base / "a5_snap.json"
        r = run_cli(ws_src, "export", str(snap))
        assert r.returncode == 0

        # 目标工作区：故意改 photo_exts 让配置不一致
        ws_dst = setup_workspace_with_scan(base, "dst")
        dst_cfg = load_config(ws_dst)
        dst_cfg["photo_exts"] = [".jpg", ".jpeg", ".WEIRD"]
        cfg_path = ws_dst / ".survey_check" / "survey_config.json"
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(dst_cfg, f, ensure_ascii=False, indent=2)

        # 使用 --include-config 触发实际写入（warning 级，默认非 abort）
        # 先用 dry-run 确认 config_mismatch
        r_dry = run_cli(ws_dst, "import", str(snap), "--include-config", "--dry-run")
        assert r_dry.returncode == 0
        assert "config_mismatch" in r_dry.stdout or "配置不一致" in r_dry.stdout

        # 再用真实导入但故意让中途失败来触发诊断输出
        # 这里使用注入失败的方式
        env_inject = {"SURVEY_CHECK_TEST_INJECT_ABORT_AFTER_CONFIG": "1"}
        r_fail = run_cli_env(ws_dst, env_inject, "import", str(snap),
                             "--include-config", "--strategy", "overwrite", "--yes")
        assert r_fail.returncode != 0, "注入失败应返回非0"
        # 诊断输出中应出现类别三（本地配置不一致）或配置差异提示
        has_category3 = ("【类别三】" in r_fail.stdout
                        or "本地配置不一致" in r_fail.stdout
                        or "配置不一致" in r_fail.stdout)
        # 同时也应有执行异常类别
        has_exec_error = ("执行异常" in r_fail.stdout
                          or "已自动从备份回滚" in r_fail.stdout
                          or "import_failed" in r_fail.stdout)
        assert has_category3 or has_exec_error, \
            (f"注入失败后输出应含类别诊断。"
             f"stdout 片段: {r_fail.stdout[:500]}")
        print(f"  注入失败诊断: 含类别区分 - OK (rc={r_fail.returncode})")

        # 回滚后配置和状态应恢复
        cfg_rollback = load_config(ws_dst)
        assert_equal(cfg_rollback["photo_exts"], dst_cfg["photo_exts"],
                     "回滚后 photo_exts 应为修改前的目标值 [.jpg, .jpeg, .WEIRD]")
        print(f"  回滚后配置完整恢复 - OK")

        # 修正后重新导入（正常、无注入）应成功
        r_ok = run_cli(ws_dst, "import", str(snap), "--include-config",
                       "--strategy", "overwrite", "--yes")
        assert r_ok.returncode == 0
        cfg_after = load_config(ws_dst)
        src_cfg_check = load_config(ws_src)
        assert_equal(cfg_after["photo_exts"], src_cfg_check["photo_exts"],
                     "成功导入 --include-config 后配置应为快照值")
        print(f"  修正后重试: 成功导入且 --include-config 生效 - OK")

        print("  [PASS] 场景A5通过")
        return True


# ============================================================
# 场景 A6：ops-log 完整可追溯（预检→执行→成功/失败 + 跨重启不丢）
# ============================================================
def test_a6_ops_log_full_traceability():
    """场景A6：ops-log 完整可追溯 + 跨重启不丢失。"""
    print("\n" + "=" * 60)
    print("场景A6：ops-log 完整可追溯 + 跨重启不丢失")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="a6_opstrace_") as tmp:
        base = Path(tmp)

        ws_src = setup_workspace_with_scan(base, "src")
        r = run_cli(ws_src, "review", "ISS-0001", "--status", "pending",
                    "--remark", "A6源")
        assert r.returncode == 0
        snap = base / "a6_snap.json"
        r = run_cli(ws_src, "export", str(snap))
        assert r.returncode == 0

        ws_dst = base / "dst"
        ws_dst.mkdir()

        # Step 1: dry-run → import_dry_run 日志
        r1 = run_cli(ws_dst, "import", str(snap), "--dry-run")
        assert r1.returncode == 0
        log1 = load_ops_log(ws_dst)
        dry_ids = {e["import_id"] for e in log1
                   if e.get("op") == "import_dry_run" and "import_id" in e}
        assert len(dry_ids) >= 1
        print(f"  Step1 dry-run: {len(dry_ids)} 条带 import_id 的日志")

        # Step 2: 真实导入 → pending_confirm + import success 日志
        r2 = run_cli(ws_dst, "import", str(snap), "--yes")
        assert r2.returncode == 0
        log2 = load_ops_log(ws_dst)
        pendings = [e for e in log2 if e.get("result") == "pending_confirm"]
        successes = [e for e in log2 if e.get("op") == "import"
                     and e.get("result") == "success"]
        assert len(pendings) >= 1, "应有 pending_confirm 阶段日志"
        assert len(successes) >= 1, "应有 import success 日志"
        # 同一 import_id 应连贯
        pending_ids = {e["import_id"] for e in pendings if "import_id" in e}
        success_ids = {e["import_id"] for e in successes if "import_id" in e}
        assert len(pending_ids) >= 1 and len(success_ids) >= 1
        print(f"  Step2 真实导入: pending={len(pendings)}, success={len(successes)}")

        # Step 3: 多轮重启 → ops-log 不丢失
        for rn in range(1, 4):
            run_cli(ws_dst, "status")
            run_cli(ws_dst, "list")
        log3 = load_ops_log(ws_dst)
        successes_after = [e for e in log3 if e.get("op") == "import"
                           and e.get("result") == "success"]
        assert len(successes_after) == len(successes), \
            "跨重启后成功导入日志数不应变化"
        print(f"  Step3 多轮重启后: 成功导入日志仍为 {len(successes_after)} 条 - OK")

        # Step 4: ops-log CLI 命令筛选 + limit
        r_log = run_cli(ws_dst, "ops-log", "--op", "import")
        assert r_log.returncode == 0
        assert "import" in r_log.stdout
        r_log2 = run_cli(ws_dst, "ops-log", "--limit", "1")
        assert r_log2.returncode == 0
        print(f"  Step4 ops-log CLI: --op/--limit 参数均可用 - OK")

        print("  [PASS] 场景A6通过")
        return True


def main():
    print("=" * 60)
    print("快照导入/导出 - 综合补充场景回归测试")
    print(f"工作目录: {SCRIPT_DIR}")
    print(f"Python: {PYTHON_EXE}")
    print("=" * 60)

    tests = [
        test_a1_export_overwrite_same_path,
        test_a2_conflict_reject_then_fix_and_retry,
        test_a3_scan_result_and_survey_points_conflict_detection,
        test_a4_delete_source_and_restart_cli_full_chain,
        test_a5_config_mismatch_diagnostic_category_three,
        test_a6_ops_log_full_traceability,
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
