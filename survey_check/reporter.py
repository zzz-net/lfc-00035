"""核对报告生成"""
import os
from datetime import datetime
from typing import List

from .state import WorkspaceState, compute_stats
from .models import Issue, IssueStatus, IssueType


def generate_text_report(state: WorkspaceState, output_path: str) -> str:
    """
    生成文本格式的核对报告

    Args:
        state: 工作区状态
        output_path: 输出文件路径

    Returns:
        报告内容
    """
    stats = compute_stats(state)
    lines = []

    lines.append("=" * 60)
    lines.append("外业调查资料包核对报告")
    lines.append("=" * 60)
    lines.append(f"生成时间: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append(f"状态版本: {state.state_version}")
    lines.append(f"配置版本: {state.config_version}")
    lines.append(f"创建时间: {state.created_at}")
    lines.append(f"最后扫描: {state.last_scan_time or '未扫描' if state.last_scan_time else '未扫描'}")
    lines.append("")

    lines.append("-" * 60)
    lines.append("统计摘要")
    lines.append("-" * 60)
    lines.append(f"调查点总数: {stats.total_points}")
    lines.append(f"问题总数: {stats.total_issues}")
    lines.append(f"  待处理: {stats.open_issues}")
    lines.append(f"  待补充: {stats.pending_issues}")
    lines.append(f"  已接受: {stats.accepted_issues}")
    lines.append(f"  已忽略: {stats.ignored_issues}")
    lines.append("")
    lines.append(f"按类型分类:")
    lines.append(f"  缺失文件: {stats.missing_count}")
    lines.append(f"  重复文件: {stats.duplicate_count}")
    lines.append(f"  命名冲突: {stats.name_conflict_count}")
    lines.append(f"  路径错误: {stats.bad_path_count}")
    lines.append("")

    lines.append("-" * 60)
    lines.append("问题明细")
    lines.append("-" * 60)
    lines.append("")

    issue_types = [
        (IssueStatus.OPEN, "待处理"),
        (IssueStatus.PENDING, "待补充"),
        (IssueStatus.ACCEPTED, "已接受"),
        (IssueStatus.IGNORED, "已忽略"),
    ]

    for status, status_label in issue_types:
        status_issues = [i for i in state.issues if i.status == status]
        if status_issues:
            lines.append(f"【{status_label}】 ({len(status_issues)} 条)")
            lines.append("")
            for idx, issue in enumerate(status_issues, 1):
                type_label = _issue_type_label(issue.issue_type)
                lines.append(f"  {idx}. [{issue.id}] {type_label}: {issue.description}")
                if issue.point_id:
                    lines.append(f"     调查点: {issue.point_id}")
                if issue.file_type:
                    lines.append(f"     文件类型: {_file_type_label(issue.file_type)}")
                if issue.file_paths:
                    lines.append(f"     文件路径:")
                    for fp in issue.file_paths:
                        lines.append(f"       - {fp}")
                if issue.remark:
                    lines.append(f"     备注: {issue.remark}")
                lines.append(f"     创建时间: {issue.created_at}")
                lines.append(f"     更新时间: {issue.updated_at}")
                lines.append("")
            lines.append("")

    lines.append("-" * 60)
    lines.append("复核历史")
    lines.append("-" * 60)
    if state.review_history:
        for action in state.review_history:
            lines.append(f"  {action.action_id}: 问题 {action.issue_id}")
            lines.append(f"    时间: {action.timestamp}")
            lines.append(f"    状态: {_status_label(action.old_status)} -> {_status_label(action.new_status)}")
            if action.new_remark:
                lines.append(f"    备注: {action.new_remark}")
    else:
        lines.append("  暂无复核记录")
    lines.append("")

    content = "\n".join(lines)

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    return content


def _issue_type_label(issue_type) -> str:
    labels = {
        IssueType.MISSING: "缺失",
        IssueType.DUPLICATE: "重复",
        IssueType.NAME_CONFLICT: "命名冲突",
        IssueType.BAD_PATH: "路径错误",
    }
    return labels.get(issue_type, str(issue_type))


def _status_label(status) -> str:
    labels = {
        IssueStatus.OPEN: "待处理",
        IssueStatus.PENDING: "待补充",
        IssueStatus.ACCEPTED: "已接受",
        IssueStatus.IGNORED: "已忽略",
    }
    return labels.get(status, str(status))


def _file_type_label(file_type) -> str:
    from .models import FileType
    labels = {
        FileType.PHOTO: "照片",
        FileType.TRACK: "轨迹",
        FileType.TABLE: "表格",
    }
    return labels.get(file_type, str(file_type))


def generate_csv_report(state: WorkspaceState, output_path: str) -> None:
    """生成 CSV 格式的问题清单"""
    import csv

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "问题编号", "问题类型", "状态", "描述",
            "调查点编号", "文件类型", "文件路径", "备注",
            "创建时间", "更新时间",
        ])

        for issue in state.issues:
            writer.writerow([
                issue.id,
                _issue_type_label(issue.issue_type),
                _status_label(issue.status),
                issue.description,
                issue.point_id or "",
                _file_type_label(issue.file_type) if issue.file_type else "",
                "; ".join(issue.file_paths) if issue.file_paths else "",
                issue.remark,
                issue.created_at,
                issue.updated_at,
            ])
