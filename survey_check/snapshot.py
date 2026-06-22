"""复核快照导出/导入模块

支持将工作区的配置、问题、复核状态、历史记录等打包成快照文件，
在新工作区或清空后的工作区导入以继续工作。
"""
import json
import os
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional, Dict, Any, Tuple

from .config import (
    SurveyConfig, load_config, save_config,
    get_config_path, get_state_path, STATE_DIRNAME,
)
from .state import WorkspaceState, load_state, save_state
from .models import (
    Issue, IssueStatus, SurveyPoint, FileEntry, ReviewAction, ScanResult,
    IssueType, FileType,
)


SNAPSHOT_VERSION = "1.0"
BACKUP_DIRNAME = "backups"


@dataclass
class SnapshotInfo:
    """快照元信息"""
    snapshot_version: str = SNAPSHOT_VERSION
    exported_at: str = ""
    source_workspace: str = ""
    note: str = ""


@dataclass
class ImportConflict:
    """导入冲突记录"""
    conflict_type: str
    severity: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ImportReport:
    """导入操作报告"""
    dry_run: bool = False
    success: bool = False
    conflicts: List[ImportConflict] = field(default_factory=list)
    issues_imported: int = 0
    issues_skipped: int = 0
    issues_overwritten: int = 0
    issues_renumbered: int = 0
    history_imported: int = 0
    config_updated: bool = False
    backup_path: str = ""
    snapshot_info: Optional[SnapshotInfo] = None

    @property
    def has_errors(self) -> bool:
        return any(c.severity == "error" for c in self.conflicts)

    @property
    def has_warnings(self) -> bool:
        return any(c.severity == "warning" for c in self.conflicts)


def export_snapshot(workspace: str, output_path: str, note: str = "") -> SnapshotInfo:
    """
    导出工作区快照到文件

    Args:
        workspace: 工作区路径
        output_path: 输出快照文件路径
        note: 备注信息

    Returns:
        快照元信息
    """
    config = load_config(workspace)
    state = load_state(workspace)

    info = SnapshotInfo(
        snapshot_version=SNAPSHOT_VERSION,
        exported_at=datetime.now().isoformat(),
        source_workspace=os.path.abspath(workspace),
        note=note,
    )

    snapshot = {
        "snapshot_info": asdict(info),
        "config": config.to_dict(),
        "state": state.to_dict(),
    }

    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".",
                exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    return info


def load_snapshot(snapshot_path: str) -> Tuple[SnapshotInfo, SurveyConfig, WorkspaceState]:
    """
    加载快照文件

    Args:
        snapshot_path: 快照文件路径

    Returns:
        (快照信息, 配置, 状态)
    """
    with open(snapshot_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    info_data = data.get("snapshot_info", {})
    info = SnapshotInfo(**{k: v for k, v in info_data.items()
                          if k in SnapshotInfo.__dataclass_fields__})

    config_data = data.get("config", {})
    config = SurveyConfig.from_dict(config_data)

    state_data = data.get("state", {})
    state = WorkspaceState.from_dict(state_data)

    return info, config, state


def _compare_configs(snap_config: SurveyConfig, target_config: SurveyConfig) -> List[Tuple[str, Any, Any]]:
    """比较两个配置的差异，返回 (字段名, 快照值, 目标值) 列表"""
    diffs = []
    compare_fields = [
        "manifest_path", "photo_dir", "track_dir", "table_dir",
        "photo_exts", "track_exts", "table_exts",
        "point_id_column", "name_column",
        "photo_pattern", "track_pattern", "table_pattern",
    ]
    for field_name in compare_fields:
        snap_val = getattr(snap_config, field_name)
        target_val = getattr(target_config, field_name)
        if snap_val != target_val:
            diffs.append((field_name, snap_val, target_val))
    return diffs


def _find_conflicting_issues(snap_state: WorkspaceState, target_state: WorkspaceState
                            ) -> Tuple[List[str], Dict[str, Issue]]:
    """
    找出快照与目标工作区编号冲突的问题

    Returns:
        (冲突编号列表, 目标工作区按编号索引的问题字典)
    """
    target_by_id = {i.id: i for i in target_state.issues}
    conflicts = []
    for issue in snap_state.issues:
        if issue.id in target_by_id:
            conflicts.append(issue.id)
    return conflicts, target_by_id


def _issue_same_key(issue_a: Issue, issue_b: Issue) -> bool:
    """判断两个问题是否是同一个（按类型+调查点+描述匹配）"""
    return (issue_a.issue_type == issue_b.issue_type
            and issue_a.point_id == issue_b.point_id
            and issue_a.description == issue_b.description)


def _generate_backup_path(workspace: str, label: str = "") -> str:
    """生成备份目录路径"""
    state_dir = os.path.join(workspace, STATE_DIRNAME)
    backup_dir = os.path.join(state_dir, BACKUP_DIRNAME)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    label_suffix = f"_{label}" if label else ""
    backup_name = f"import_{timestamp}{label_suffix}"
    return os.path.join(backup_dir, backup_name)


def _backup_workspace(workspace: str, backup_path: str) -> None:
    """备份工作区的配置和状态"""
    os.makedirs(backup_path, exist_ok=True)

    config_src = get_config_path(workspace)
    state_src = get_state_path(workspace)

    if os.path.exists(config_src):
        shutil.copy2(config_src, os.path.join(backup_path, os.path.basename(config_src)))
    if os.path.exists(state_src):
        shutil.copy2(state_src, os.path.join(backup_path, os.path.basename(state_src)))


def restore_from_backup(workspace: str, backup_path: str) -> bool:
    """
    从备份恢复工作区

    Args:
        workspace: 工作区路径
        backup_path: 备份目录路径

    Returns:
        是否恢复成功
    """
    config_src = os.path.join(backup_path, os.path.basename(get_config_path(workspace)))
    state_src = os.path.join(backup_path, os.path.basename(get_state_path(workspace)))

    if not os.path.exists(config_src) and not os.path.exists(state_src):
        return False

    if os.path.exists(config_src):
        shutil.copy2(config_src, get_config_path(workspace))
    if os.path.exists(state_src):
        shutil.copy2(state_src, get_state_path(workspace))

    return True


def preflight_import(workspace: str, snapshot_path: str) -> ImportReport:
    """
    预检导入：检测冲突但不实际执行

    Args:
        workspace: 目标工作区路径
        snapshot_path: 快照文件路径

    Returns:
        导入报告（dry_run=True）
    """
    return _do_import(workspace, snapshot_path, dry_run=True, strategy="skip")


def import_snapshot(workspace: str, snapshot_path: str,
                   strategy: str = "skip",
                   include_config: bool = False,
                   dry_run: bool = False) -> ImportReport:
    """
    导入快照到工作区

    Args:
        workspace: 目标工作区路径
        snapshot_path: 快照文件路径
        strategy: 冲突处理策略
            - skip: 跳过冲突问题，保留目标版本（默认）
            - overwrite: 用快照版本覆盖冲突问题
            - renumber: 重编号快照中冲突的问题，追加到目标之后
            - merge: 智能合并，保留目标问题，将快照的状态/备注合并到目标（仅当目标是初始状态）
        include_config: 是否同时导入配置
        dry_run: 是否只预检不实际执行

    Returns:
        导入报告
    """
    valid_strategies = {"skip", "overwrite", "renumber", "merge"}
    if strategy not in valid_strategies:
        raise ValueError(f"无效的策略: {strategy}，有效策略: {valid_strategies}")

    return _do_import(workspace, snapshot_path, dry_run=dry_run,
                     strategy=strategy, include_config=include_config)


def _do_import(workspace: str, snapshot_path: str,
              dry_run: bool = False,
              strategy: str = "skip",
              include_config: bool = False) -> ImportReport:
    """实际执行导入逻辑"""
    report = ImportReport(dry_run=dry_run)

    if not os.path.exists(snapshot_path):
        report.conflicts.append(ImportConflict(
            conflict_type="snapshot_missing",
            severity="error",
            message=f"快照文件不存在: {snapshot_path}",
        ))
        return report

    try:
        snap_info, snap_config, snap_state = load_snapshot(snapshot_path)
        report.snapshot_info = snap_info
    except Exception as e:
        report.conflicts.append(ImportConflict(
            conflict_type="snapshot_invalid",
            severity="error",
            message=f"快照文件解析失败: {e}",
        ))
        return report

    target_config = load_config(workspace) if os.path.exists(get_config_path(workspace)) else None
    target_state = load_state(workspace)

    config_diffs = _compare_configs(snap_config, target_config) if target_config else []

    if target_config is None:
        report.conflicts.append(ImportConflict(
            conflict_type="no_target_config",
            severity="warning",
            message="目标工作区没有配置，将使用快照配置",
        ))
    elif config_diffs:
        diff_desc = "; ".join(f"{k}" for k, _, _ in config_diffs)
        report.conflicts.append(ImportConflict(
            conflict_type="config_mismatch",
            severity="warning",
            message=f"配置不一致（{len(config_diffs)} 处差异: {diff_desc}）",
            details={"diffs": config_diffs},
        ))

    if target_state.last_scan_time and snap_state.last_scan_time:
        snap_time = snap_state.last_scan_time
        target_time = target_state.last_scan_time
        if target_time > snap_time:
            report.conflicts.append(ImportConflict(
                conflict_type="target_newer_scan",
                severity="warning",
                message=f"目标工作区扫描时间 ({target_time}) 晚于快照 ({snap_time})，目标可能有更新的结果",
            ))

    conflicting_ids, target_by_id = _find_conflicting_issues(snap_state, target_state)

    if conflicting_ids:
        report.conflicts.append(ImportConflict(
            conflict_type="issue_id_conflict",
            severity="warning" if strategy != "skip" else "info",
            message=f"发现 {len(conflicting_ids)} 个问题编号冲突，将使用 '{strategy}' 策略处理",
            details={"conflicting_ids": conflicting_ids, "strategy": strategy},
        ))

    if dry_run:
        _simulate_import(snap_state, target_state, strategy, report)
        return report

    backup_path = _generate_backup_path(workspace, "pre_import")
    _backup_workspace(workspace, backup_path)
    report.backup_path = backup_path

    try:
        if include_config and config_diffs:
            save_config(workspace, snap_config)
            report.config_updated = True

        _apply_import(snap_state, target_state, strategy, report)
        save_state(workspace, target_state)

        report.success = True
    except Exception as e:
        report.conflicts.append(ImportConflict(
            conflict_type="import_failed",
            severity="error",
            message=f"导入失败: {e}",
        ))
        restore_from_backup(workspace, backup_path)
        report.backup_path = backup_path
        report.success = False

    return report


def _simulate_import(snap_state: WorkspaceState, target_state: WorkspaceState,
                    strategy: str, report: ImportReport) -> None:
    """模拟导入，仅统计不实际修改"""
    target_by_id = {i.id: i for i in target_state.issues}

    for issue in snap_state.issues:
        if issue.id not in target_by_id:
            report.issues_imported += 1
            continue

        if strategy == "skip":
            report.issues_skipped += 1
        elif strategy == "overwrite":
            report.issues_overwritten += 1
        elif strategy == "renumber":
            report.issues_renumbered += 1
        elif strategy == "merge":
            target_issue = target_by_id[issue.id]
            if _issue_same_key(issue, target_issue):
                report.issues_skipped += 1
            else:
                report.issues_renumbered += 1

    report.history_imported = len(snap_state.review_history)


def _apply_import(snap_state: WorkspaceState, target_state: WorkspaceState,
                 strategy: str, report: ImportReport) -> None:
    """实际执行导入操作"""
    target_by_id = {i.id: i for i in target_state.issues}

    next_num = target_state.next_issue_number

    imported_issues = []
    skipped_ids = []
    overwritten_ids = []
    renumbered_ids = []

    for issue in snap_state.issues:
        if issue.id not in target_by_id:
            imported_issues.append(issue)
            continue

        if strategy == "skip":
            skipped_ids.append(issue.id)
            continue

        if strategy == "overwrite":
            for idx, ti in enumerate(target_state.issues):
                if ti.id == issue.id:
                    target_state.issues[idx] = issue
                    break
            overwritten_ids.append(issue.id)
            continue

        if strategy == "renumber":
            new_issue = _clone_issue(issue)
            new_id = f"ISS-{next_num:04d}"
            next_num += 1
            new_issue.id = new_id
            imported_issues.append(new_issue)
            renumbered_ids.append((issue.id, new_id))
            continue

        if strategy == "merge":
            target_issue = target_by_id[issue.id]
            if _issue_same_key(issue, target_issue):
                skipped_ids.append(issue.id)
            else:
                new_issue = _clone_issue(issue)
                new_id = f"ISS-{next_num:04d}"
                next_num += 1
                new_issue.id = new_id
                imported_issues.append(new_issue)
                renumbered_ids.append((issue.id, new_id))
            continue

    for issue in imported_issues:
        target_state.issues.append(issue)

    target_state.next_issue_number = max(next_num, target_state.next_issue_number)

    for action in snap_state.review_history:
        action_id = f"ACT-{len(target_state.review_history) + 1:04d}"
        new_action = _clone_review_action(action)
        new_action.action_id = action_id

        if strategy == "renumber" and renumbered_ids:
            id_map = {old: new for old, new in renumbered_ids}
            if new_action.issue_id in id_map:
                new_action.issue_id = id_map[new_action.issue_id]

        target_state.review_history.append(new_action)

    if snap_state.scan_result and not target_state.scan_result:
        target_state.scan_result = snap_state.scan_result

    if snap_state.survey_points and not target_state.survey_points:
        target_state.survey_points = snap_state.survey_points

    if snap_state.last_scan_time and not target_state.last_scan_time:
        target_state.last_scan_time = snap_state.last_scan_time

    report.issues_imported = len(imported_issues)
    report.issues_skipped = len(skipped_ids)
    report.issues_overwritten = len(overwritten_ids)
    report.issues_renumbered = len(renumbered_ids)
    report.history_imported = len(snap_state.review_history)


def _clone_issue(issue: Issue) -> Issue:
    """深拷贝问题对象"""
    return Issue(
        id=issue.id,
        issue_type=issue.issue_type,
        status=issue.status,
        description=issue.description,
        file_type=issue.file_type,
        point_id=issue.point_id,
        file_paths=list(issue.file_paths),
        remark=issue.remark,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
    )


def _clone_review_action(action: ReviewAction) -> ReviewAction:
    """深拷贝复核操作对象"""
    return ReviewAction(
        action_id=action.action_id,
        issue_id=action.issue_id,
        old_status=action.old_status,
        new_status=action.new_status,
        old_remark=action.old_remark,
        new_remark=action.new_remark,
        timestamp=action.timestamp,
    )


def list_backups(workspace: str) -> List[str]:
    """列出工作区的所有导入备份"""
    backup_dir = os.path.join(workspace, STATE_DIRNAME, BACKUP_DIRNAME)
    if not os.path.isdir(backup_dir):
        return []
    return sorted(os.listdir(backup_dir), reverse=True)


def get_backup_path(workspace: str, backup_name: str) -> str:
    """获取备份目录的完整路径"""
    return os.path.join(workspace, STATE_DIRNAME, BACKUP_DIRNAME, backup_name)
