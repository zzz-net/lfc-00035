#!/usr/bin/env python
"""幂等性回归测试：scan 命令编号复用与状态保留

覆盖三个核心场景：
1. 重复扫描不跳号
2. 已处理状态和备注保留
3. 空扫描后新增结果继续顺号

运行方式：
    python test_regression.py
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


SCRIPT_DIR = Path(__file__).parent.absolute()
SAMPLE_DATA = SCRIPT_DIR / "sample_data"
PYTHON_EXE = sys.executable


def run_cli(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    """运行 survey-check CLI，返回 CompletedProcess"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SCRIPT_DIR)
    cmd = [
        PYTHON_EXE, "-m", "survey_check",
        "--workspace", str(workspace),
        *args,
    ]
    return subprocess.run(cmd, cwd=workspace, env=env, capture_output=True, text=True)


def load_state(workspace: Path) -> dict:
    """加载工作区状态文件"""
    state_path = workspace / ".survey_check" / "survey_state.json"
    with open(state_path, encoding="utf-8") as f:
        return json.load(f)


def setup_workspace(base: Path, name: str) -> Path:
    """基于 sample_data 创建一个干净的独立工作区"""
    ws = base / name
    if ws.exists():
        shutil.rmtree(ws)
    shutil.copytree(SAMPLE_DATA, ws)
    # 清理之前的状态
    state_dir = ws / ".survey_check"
    if state_dir.exists():
        shutil.rmtree(state_dir)
    # 初始化
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


def test_case_1_no_skip_on_rescan():
    """测试用例1：重复扫描不跳号"""
    print("\n" + "=" * 60)
    print("测试用例1：重复扫描不跳号")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="survey_test_") as tmp:
        base = Path(tmp)
        ws = setup_workspace(base, "case1_rescan")

        # 第 1 次扫描
        r = run_cli(ws, "scan")
        assert r.returncode == 0, f"scan#1 失败: {r.stderr}"
        s1 = load_state(ws)
        n1 = s1["next_issue_number"]
        ids1 = sorted(i["id"] for i in s1["issues"])
        max_id1 = ids1[-1]
        print(f"  第1次扫描: {len(ids1)} 个问题, next_issue_number={n1}, 最大编号={max_id1}")

        # 第 2, 3, 4 次连续空重扫
        for round_i in range(2, 5):
            r = run_cli(ws, "scan")
            assert r.returncode == 0, f"scan#{round_i} 失败: {r.stderr}"
            assert "复用历史问题" in r.stdout, f"scan#{round_i} 未输出复用提示"
            s = load_state(ws)
            ids = sorted(i["id"] for i in s["issues"])
            assert_equal(s["next_issue_number"], n1,
                        f"第{round_i}次扫描 next_issue_number 不应变化")
            assert_equal(ids, ids1,
                        f"第{round_i}次扫描问题编号集合不应变化")
            print(f"  第{round_i}次扫描: OK, next_issue_number 仍为 {n1}")

        print("  [PASS] 测试用例1通过")
        return True


def test_case_2_status_and_remark_preserved():
    """测试用例2：已处理状态和备注保留"""
    print("\n" + "=" * 60)
    print("测试用例2：已处理状态和备注保留")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="survey_test_") as tmp:
        base = Path(tmp)
        ws = setup_workspace(base, "case2_preserve")

        # 首次扫描
        r = run_cli(ws, "scan")
        assert r.returncode == 0
        s1 = load_state(ws)
        ids = sorted(i["id"] for i in s1["issues"])
        # 挑几个问题做复核（至少3个不同位置的）
        n = len(ids)
        assert n >= 3, f"问题数量应至少3个，实际{n}个"
        changes = [
            (ids[0], "pending", "待补充备注A"),
            (ids[n // 2], "accepted", "已接受备注B"),
            (ids[-1], "ignored", "已忽略备注C"),
        ]
        for issue_id, status, remark in changes:
            r = run_cli(ws, "review", issue_id,
                       "--status", status, "--remark", remark)
            assert r.returncode == 0, f"review {issue_id} 失败: {r.stderr}"

        # 复核后的状态
        s_review = load_state(ws)
        for issue_id, status, remark in changes:
            issue = next(i for i in s_review["issues"] if i["id"] == issue_id)
            assert_equal(issue["status"], status, f"{issue_id} 状态未更新")
            assert_equal(issue["remark"], remark, f"{issue_id} 备注未更新")
        print(f"  已复核 {len(changes)} 个问题，状态/备注已写入")

        # 空重扫
        r = run_cli(ws, "scan")
        assert r.returncode == 0
        assert "复用历史问题" in r.stdout

        # 扫描后再次检查状态和备注
        s_after = load_state(ws)
        for issue_id, status, remark in changes:
            issue = next(i for i in s_after["issues"] if i["id"] == issue_id)
            assert_equal(issue["status"], status,
                        f"{issue_id} 重扫后状态丢失")
            assert_equal(issue["remark"], remark,
                        f"{issue_id} 重扫后备注丢失")
            print(f"  {issue_id}: status={status}, remark={remark} - OK")

        # 检查撤销历史仍在
        assert_equal(len(s_after["review_history"]), len(changes),
                    "复核历史记录不应丢失")
        print(f"  复核历史保留 {len(changes)} 条 - OK")

        print("  [PASS] 测试用例2通过")
        return True


def test_case_3_new_issue_after_empty_rescan():
    """测试用例3：空扫描后新增结果继续顺号"""
    print("\n" + "=" * 60)
    print("测试用例3：空扫描后新增结果继续顺号")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="survey_test_") as tmp:
        base = Path(tmp)
        ws = setup_workspace(base, "case3_continuous")

        # 第 1 次扫描
        r = run_cli(ws, "scan")
        assert r.returncode == 0
        s1 = load_state(ws)
        n1 = s1["next_issue_number"]
        ids1 = sorted(i["id"] for i in s1["issues"])
        max_id_num_1 = int(ids1[-1].split("-")[1])
        print(f"  首次扫描: {len(ids1)} 问题, next_issue_number={n1}, 最大编号 {ids1[-1]}")

        # 第 2、3 次空重扫（偷跑之前会让 n 增加，但修复后应不变）
        for _ in range(2):
            r = run_cli(ws, "scan")
            assert r.returncode == 0
        s_empty = load_state(ws)
        n_empty = s_empty["next_issue_number"]
        assert_equal(n_empty, n1, "空重扫后 next_issue_number 不应偷跑")
        print(f"  {2} 次空重扫后: next_issue_number 仍为 {n_empty}")

        # 人为新增一个清单外的冲突文件
        new_file = ws / "photos" / "P9999_unlisted.jpg"
        with open(new_file, "w") as f:
            f.write("")
        print(f"  新增冲突文件: {new_file.name}")

        # 再次扫描，应有且只有 1 个新增，编号必须紧接上次真实新增位置
        r = run_cli(ws, "scan")
        assert r.returncode == 0
        assert "新增问题: 1 条" in r.stdout, f"应显示新增 1 条，实际输出：{r.stdout}"

        s_final = load_state(ws)
        ids_final = sorted(i["id"] for i in s_final["issues"])
        new_id = ids_final[-1]
        new_id_num = int(new_id.split("-")[1])

        # 关键断言：新编号必须是 max_id_num_1 + 1，不能是空重扫偷跑后的值
        expected_new_num = max_id_num_1 + 1
        assert_equal(new_id_num, expected_new_num,
                    f"新问题编号应是 ISS-{expected_new_num:04d}，但实际是 {new_id}")

        # next_issue_number 也应该是 expected_new_num + 1
        expected_next = expected_new_num + 1
        assert_equal(s_final["next_issue_number"], expected_next,
                    f"next_issue_number 应是 {expected_next}")

        print(f"  新增问题编号: {new_id} (期望 ISS-{expected_new_num:04d}) - OK")
        print(f"  最终 next_issue_number: {s_final['next_issue_number']} (期望 {expected_next}) - OK")

        # 验证全部编号连续无空洞
        all_nums = sorted(int(i.split("-")[1]) for i in ids_final)
        expected_nums = list(range(1, expected_new_num + 1))
        assert_equal(all_nums, expected_nums,
                    f"全部编号应从 1 连续到 {expected_new_num}")
        print(f"  全部编号连续无空洞 (1~{expected_new_num}) - OK")

        print("  [PASS] 测试用例3通过")
        return True


def main():
    print("外业调查资料包核对工具 - 幂等性回归测试")
    print(f"工作目录: {SCRIPT_DIR}")
    print(f"Python: {PYTHON_EXE}")

    tests = [
        test_case_1_no_skip_on_rescan,
        test_case_2_status_and_remark_preserved,
        test_case_3_new_issue_after_empty_rescan,
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
