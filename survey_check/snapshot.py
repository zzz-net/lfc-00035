"""复核快照导出/导入模块

支持将工作区的配置、问题、复核状态、历史记录等打包成快照文件，
在新工作区或清空后的工作区导入以继续工作。

增强特性：
- 导出时附带完整元数据与内容校验和
- 导入前进行版本兼容性、目录可写性、快照完整性、残留冲突检测
- 所有导出/导入操作记录日志到 .survey_check/ops_log.jsonl
- 冲突与失败时给出明确原因与处理建议
"""
import hashlib
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


SNAPSHOT_VERSION = "1.1"
BACKUP_DIRNAME = "backups"
OPS_LOG_FILENAME = "ops_log.jsonl"

SUPPORTED_SNAPSHOT_VERSIONS = {"1.0", "1.1"}
SUPPORTED_STATE_VERSIONS = {"1.0"}
SUPPORTED_CONFIG_VERSIONS = {"1.0"}

REQUIRED_SNAPSHOT_TOP_KEYS = {"snapshot_info", "config", "state"}
REQUIRED_SNAPSHOT_INFO_KEYS = {"snapshot_version", "exported_at"}
REQUIRED_STATE_KEYS = {"state_version", "issues", "review_history", "undo_stack"}
REQUIRED_CONFIG_KEYS = {"config_version", "manifest_path", "photo_dir", "track_dir", "table_dir"}


@dataclass
class SnapshotInfo:
    snapshot_version: str = SNAPSHOT_VERSION
    exported_at: str = ""
    source_workspace: str = ""
    note: str = ""
    state_version: str = ""
    config_version: str = ""
    issue_count: int = 0
    history_count: int = 0
    undo_stack_count: int = 0
    survey_points_count: int = 0
    content_hash: str = ""


@dataclass
class ImportConflict:
    conflict_type: str
    severity: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    hint: str = ""


@dataclass
class ImportReport:
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
    phase: str = ""

    @property
    def has_errors(self) -> bool:
        return any(c.severity == "error" for c in self.conflicts)

    @property
    def has_warnings(self) -> bool:
        return any(c.severity == "warning" for c in self.conflicts)


def _compute_content_hash(config_dict: dict, state_dict: dict) -> str:
    payload = json.dumps({"config": config_dict, "state": state_dict},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _validate_snapshot_structure(data: dict) -> List[ImportConflict]:
    conflicts: List[ImportConflict] = []
    missing_top = REQUIRED_SNAPSHOT_TOP_KEYS - set(data.keys())
    if missing_top:
        conflicts.append(ImportConflict(
            conflict_type="snapshot_missing_keys",
            severity="error",
            message=f"快照缺少顶层字段: {', '.join(sorted(missing_top))}",
            hint="快照文件可能已损坏或不完整，请重新导出",
        ))
        return conflicts

    info = data.get("snapshot_info", {})
    missing_info = REQUIRED_SNAPSHOT_INFO_KEYS - set(info.keys())
    if missing_info:
        conflicts.append(ImportConflict(
            conflict_type="snapshot_info_missing",
            severity="error",
            message=f"快照信息缺少字段: {', '.join(sorted(missing_info))}",
            hint="快照元信息不完整，请确认快照来源是否可靠",
        ))

    state = data.get("state", {})
    missing_state = REQUIRED_STATE_KEYS - set(state.keys())
    if missing_state:
        conflicts.append(ImportConflict(
            conflict_type="snapshot_state_missing",
            severity="error",
            message=f"快照状态缺少字段: {', '.join(sorted(missing_state))}",
            hint="快照状态数据不完整，请重新导出",
        ))

    cfg = data.get("config", {})
    missing_cfg = REQUIRED_CONFIG_KEYS - set(cfg.keys())
    if missing_cfg:
        conflicts.append(ImportConflict(
            conflict_type="snapshot_config_missing",
            severity="error",
            message=f"快照配置缺少字段: {', '.join(sorted(missing_cfg))}",
            hint="快照配置不完整，请重新导出",
        ))

    return conflicts


def _validate_version_compatibility(info: SnapshotInfo) -> List[ImportConflict]:
    conflicts: List[ImportConflict] = []
    if info.snapshot_version not in SUPPORTED_SNAPSHOT_VERSIONS:
        conflicts.append(ImportConflict(
            conflict_type="snapshot_version_unsupported",
            severity="error",
            message=f"快照版本 {info.snapshot_version} 不受支持（支持: {', '.join(sorted(SUPPORTED_SNAPSHOT_VERSIONS))}）",
            hint="请升级 survey-check 或使用匹配版本的快照",
        ))
    if info.state_version and info.state_version not in SUPPORTED_STATE_VERSIONS:
        conflicts.append(ImportConflict(
            conflict_type="state_version_unsupported",
            severity="error",
            message=f"快照状态版本 {info.state_version} 不受支持（支持: {', '.join(sorted(SUPPORTED_STATE_VERSIONS))}）",
            hint="请升级 survey-check 或重新导出快照",
        ))
    if info.config_version and info.config_version not in SUPPORTED_CONFIG_VERSIONS:
        conflicts.append(ImportConflict(
            conflict_type="config_version_unsupported",
            severity="error",
            message=f"快照配置版本 {info.config_version} 不受支持（支持: {', '.join(sorted(SUPPORTED_CONFIG_VERSIONS))}）",
            hint="请升级 survey-check 或重新导出快照",
        ))
    return conflicts


def _validate_integrity(data: dict, info: SnapshotInfo) -> List[ImportConflict]:
    conflicts: List[ImportConflict] = []
    if not info.content_hash:
        conflicts.append(ImportConflict(
            conflict_type="snapshot_no_checksum",
            severity="warning",
            message="快照无内容校验和，无法验证完整性",
            hint="该快照可能由旧版工具导出，建议重新导出以附带校验和",
        ))
        return conflicts
    actual_hash = _compute_content_hash(data.get("config", {}), data.get("state", {}))
    if actual_hash != info.content_hash:
        conflicts.append(ImportConflict(
            conflict_type="snapshot_checksum_mismatch",
            severity="error",
            message=f"快照内容校验和不匹配（期望 {info.content_hash}，实际 {actual_hash}）",
            hint="快照文件可能在导出后被篡改或传输损坏，请重新导出",
        ))
    return conflicts


def _validate_target_workspace(workspace: str) -> List[ImportConflict]:
    conflicts: List[ImportConflict] = []
    state_dir = os.path.join(workspace, STATE_DIRNAME)

    if not os.path.isdir(workspace):
        try:
            os.makedirs(workspace, exist_ok=True)
        except OSError as e:
            conflicts.append(ImportConflict(
                conflict_type="target_dir_not_creatable",
                severity="error",
                message=f"目标目录不存在且无法创建: {workspace} ({e})",
                hint="请检查路径是否正确，或手动创建目录",
            ))
            return conflicts

    test_file = os.path.join(workspace, ".survey_check_write_test")
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
    except OSError as e:
        conflicts.append(ImportConflict(
            conflict_type="target_dir_not_writable",
            severity="error",
            message=f"目标目录不可写: {workspace} ({e})",
            hint="请检查目录权限，确保当前用户有写入权限",
        ))

    return conflicts


def _detect_residual_conflicts(workspace: str) -> List[ImportConflict]:
    conflicts: List[ImportConflict] = []
    state_dir = os.path.join(workspace, STATE_DIRNAME)
    config_path = get_config_path(workspace)
    state_path = get_state_path(workspace)

    has_config = os.path.exists(config_path)
    has_state = os.path.exists(state_path)

    if has_state and not has_config:
        conflicts.append(ImportConflict(
            conflict_type="residual_state_no_config",
            severity="error",
            message="目标目录存在状态文件但缺少配置文件（残留状态），可能是不完整的旧工作区",
            hint="请先手动清理残留状态文件（删除 .survey_check/ 目录），或先运行 'survey-check init' 初始化后再导入",
        ))

    if not has_state and has_config:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg_data = json.load(f)
            if not isinstance(cfg_data, dict) or "config_version" not in cfg_data:
                conflicts.append(ImportConflict(
                    conflict_type="residual_config_corrupted",
                    severity="warning",
                    message="目标配置文件内容异常（缺少 config_version）",
                    hint="配置文件可能已损坏，导入时将从快照覆盖；建议备份后手动检查",
                ))
        except (json.JSONDecodeError, OSError) as e:
            conflicts.append(ImportConflict(
                conflict_type="residual_config_corrupted",
                severity="warning",
                message=f"目标配置文件无法解析: {e}",
                hint="配置文件可能已损坏，导入时将从快照覆盖；建议备份后手动检查",
            ))

    if has_state:
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                st_data = json.load(f)
            if not isinstance(st_data, dict) or "issues" not in st_data:
                conflicts.append(ImportConflict(
                    conflict_type="residual_state_corrupted",
                    severity="warning",
                    message="目标状态文件内容异常（缺少 issues 字段）",
                    hint="状态文件可能已损坏，导入时将自动备份并覆盖；建议备份后手动检查",
                ))
        except (json.JSONDecodeError, OSError) as e:
            conflicts.append(ImportConflict(
                conflict_type="residual_state_corrupted",
                severity="warning",
                message=f"目标状态文件无法解析: {e}",
                hint="状态文件可能已损坏，导入时将自动备份并覆盖；建议备份后手动检查",
            ))

    backup_dir = os.path.join(state_dir, BACKUP_DIRNAME)
    if os.path.isdir(backup_dir):
        n_backups = len(os.listdir(backup_dir))
        if n_backups > 10:
            conflicts.append(ImportConflict(
                conflict_type="stale_backups",
                severity="info",
                message=f"目标工作区已有 {n_backups} 个导入备份",
                hint="如需清理旧备份，可手动删除 .survey_check/backups/ 目录中的旧条目",
            ))

    return conflicts


def export_snapshot(workspace: str, output_path: str, note: str = "") -> SnapshotInfo:
    config = load_config(workspace)
    state = load_state(workspace)

    config_dict = config.to_dict()
    state_dict = state.to_dict()
    content_hash = _compute_content_hash(config_dict, state_dict)

    info = SnapshotInfo(
        snapshot_version=SNAPSHOT_VERSION,
        exported_at=datetime.now().isoformat(),
        source_workspace=os.path.abspath(workspace),
        note=note,
        state_version=state.state_version,
        config_version=config.config_version,
        issue_count=len(state.issues),
        history_count=len(state.review_history),
        undo_stack_count=len(state.undo_stack),
        survey_points_count=len(state.survey_points),
        content_hash=content_hash,
    )

    snapshot = {
        "snapshot_info": asdict(info),
        "config": config_dict,
        "state": state_dict,
    }

    output_path = os.path.abspath(output_path)
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    _append_ops_log(workspace, {
        "op": "export",
        "timestamp": datetime.now().isoformat(),
        "output_path": output_path,
        "note": note,
        "snapshot_version": SNAPSHOT_VERSION,
        "content_hash": content_hash,
        "issue_count": len(state.issues),
        "history_count": len(state.review_history),
        "result": "success",
    })

    return info


def load_snapshot(snapshot_path: str) -> Tuple[SnapshotInfo, SurveyConfig, WorkspaceState]:
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
    target_by_id = {i.id: i for i in target_state.issues}
    conflicts = []
    for issue in snap_state.issues:
        if issue.id in target_by_id:
            conflicts.append(issue.id)
    return conflicts, target_by_id


def _issue_same_key(issue_a: Issue, issue_b: Issue) -> bool:
    return (issue_a.issue_type == issue_b.issue_type
            and issue_a.point_id == issue_b.point_id
            and issue_a.description == issue_b.description)


def _generate_backup_path(workspace: str, label: str = "") -> str:
    state_dir = os.path.join(workspace, STATE_DIRNAME)
    backup_dir = os.path.join(state_dir, BACKUP_DIRNAME)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    label_suffix = f"_{label}" if label else ""
    backup_name = f"import_{timestamp}{label_suffix}"
    return os.path.join(backup_dir, backup_name)


def _backup_workspace(workspace: str, backup_path: str) -> None:
    os.makedirs(backup_path, exist_ok=True)

    config_src = get_config_path(workspace)
    state_src = get_state_path(workspace)

    if os.path.exists(config_src):
        shutil.copy2(config_src, os.path.join(backup_path, os.path.basename(config_src)))
    if os.path.exists(state_src):
        shutil.copy2(state_src, os.path.join(backup_path, os.path.basename(state_src)))


def restore_from_backup(workspace: str, backup_path: str) -> bool:
    config_src = os.path.join(backup_path, os.path.basename(get_config_path(workspace)))
    state_src = os.path.join(backup_path, os.path.basename(get_state_path(workspace)))

    if not os.path.exists(config_src) and not os.path.exists(state_src):
        return False

    if os.path.exists(config_src):
        shutil.copy2(config_src, get_config_path(workspace))
    if os.path.exists(state_src):
        shutil.copy2(state_src, get_state_path(workspace))

    _append_ops_log(workspace, {
        "op": "backup_restore",
        "timestamp": datetime.now().isoformat(),
        "backup_path": backup_path,
        "result": "success",
    })

    return True


def preflight_import(workspace: str, snapshot_path: str) -> ImportReport:
    return _do_import(workspace, snapshot_path, dry_run=True, strategy="skip")


def import_snapshot(workspace: str, snapshot_path: str,
                   strategy: str = "skip",
                   include_config: bool = False,
                   dry_run: bool = False) -> ImportReport:
    valid_strategies = {"skip", "overwrite", "renumber", "merge"}
    if strategy not in valid_strategies:
        raise ValueError(f"无效的策略: {strategy}，有效策略: {valid_strategies}")

    return _do_import(workspace, snapshot_path, dry_run=dry_run,
                     strategy=strategy, include_config=include_config)


def _do_import(workspace: str, snapshot_path: str,
              dry_run: bool = False,
              strategy: str = "skip",
              include_config: bool = False) -> ImportReport:
    report = ImportReport(dry_run=dry_run)

    if not os.path.exists(snapshot_path):
        report.conflicts.append(ImportConflict(
            conflict_type="snapshot_missing",
            severity="error",
            message=f"快照文件不存在: {snapshot_path}",
            hint="请检查快照文件路径是否正确",
        ))
        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "snapshot_path": snapshot_path,
            "result": "failure",
            "failure_phase": "file_check",
            "failure_reason": "snapshot_missing",
        })
        return report

    report.phase = "snapshot_validation"
    try:
        with open(snapshot_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    except json.JSONDecodeError as e:
        report.conflicts.append(ImportConflict(
            conflict_type="snapshot_invalid_json",
            severity="error",
            message=f"快照文件不是有效的 JSON: {e}",
            hint="文件可能在传输中损坏，请重新导出快照",
        ))
        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "snapshot_path": snapshot_path,
            "result": "failure",
            "failure_phase": "snapshot_validation",
            "failure_reason": "invalid_json",
        })
        return report
    except Exception as e:
        report.conflicts.append(ImportConflict(
            conflict_type="snapshot_read_error",
            severity="error",
            message=f"快照文件读取失败: {e}",
            hint="请检查文件是否存在且可读",
        ))
        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "snapshot_path": snapshot_path,
            "result": "failure",
            "failure_phase": "snapshot_validation",
            "failure_reason": "read_error",
        })
        return report

    structure_conflicts = _validate_snapshot_structure(raw_data)
    report.conflicts.extend(structure_conflicts)
    if report.has_errors:
        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "snapshot_path": snapshot_path,
            "result": "failure",
            "failure_phase": "snapshot_validation",
            "failure_reason": "structure_invalid",
            "conflicts": [c.conflict_type for c in structure_conflicts],
        })
        return report

    try:
        snap_info, snap_config, snap_state = load_snapshot(snapshot_path)
        report.snapshot_info = snap_info
    except Exception as e:
        report.conflicts.append(ImportConflict(
            conflict_type="snapshot_parse_error",
            severity="error",
            message=f"快照数据解析失败: {e}",
            hint="快照内部数据格式异常，请确认导出工具版本并重新导出",
        ))
        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "snapshot_path": snapshot_path,
            "result": "failure",
            "failure_phase": "snapshot_validation",
            "failure_reason": "parse_error",
        })
        return report

    version_conflicts = _validate_version_compatibility(snap_info)
    report.conflicts.extend(version_conflicts)
    if report.has_errors:
        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "snapshot_path": snapshot_path,
            "result": "failure",
            "failure_phase": "version_check",
            "failure_reason": "version_unsupported",
            "conflicts": [c.conflict_type for c in version_conflicts],
        })
        return report

    integrity_conflicts = _validate_integrity(raw_data, snap_info)
    report.conflicts.extend(integrity_conflicts)
    if report.has_errors:
        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "snapshot_path": snapshot_path,
            "result": "failure",
            "failure_phase": "integrity_check",
            "failure_reason": "checksum_mismatch",
        })
        return report

    report.phase = "target_validation"
    target_conflicts = _validate_target_workspace(workspace)
    report.conflicts.extend(target_conflicts)
    if report.has_errors:
        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "snapshot_path": snapshot_path,
            "result": "failure",
            "failure_phase": "target_validation",
            "failure_reason": "target_dir_invalid",
            "conflicts": [c.conflict_type for c in target_conflicts],
        })
        return report

    residual_conflicts = _detect_residual_conflicts(workspace)
    report.conflicts.extend(residual_conflicts)
    if report.has_errors:
        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "snapshot_path": snapshot_path,
            "result": "failure",
            "failure_phase": "target_validation",
            "failure_reason": "residual_conflict",
            "conflicts": [c.conflict_type for c in residual_conflicts],
        })
        return report

    report.phase = "content_check"
    target_config = load_config(workspace) if os.path.exists(get_config_path(workspace)) else None
    target_state = load_state(workspace)

    config_diffs = _compare_configs(snap_config, target_config) if target_config else []

    if target_config is None:
        report.conflicts.append(ImportConflict(
            conflict_type="no_target_config",
            severity="info",
            message="目标工作区无配置，将从快照恢复配置",
            hint="导入将自动恢复配置，无需额外操作",
        ))
    elif config_diffs:
        diff_desc = "; ".join(f"{k}" for k, _, _ in config_diffs)
        report.conflicts.append(ImportConflict(
            conflict_type="config_mismatch",
            severity="warning",
            message=f"配置不一致（{len(config_diffs)} 处差异: {diff_desc}）",
            details={"diffs": config_diffs},
            hint="使用 --include-config 可覆盖目标配置，否则保留当前配置",
        ))

    if target_state.last_scan_time and snap_state.last_scan_time:
        snap_time = snap_state.last_scan_time
        target_time = target_state.last_scan_time
        if target_time > snap_time:
            report.conflicts.append(ImportConflict(
                conflict_type="target_newer_scan",
                severity="warning",
                message=f"目标工作区扫描时间 ({target_time}) 晚于快照 ({snap_time})，目标可能有更新的结果",
                hint="建议先备份目标工作区（使用 export 命令），再决定是否导入",
            ))

    conflicting_ids, target_by_id = _find_conflicting_issues(snap_state, target_state)

    if conflicting_ids:
        report.conflicts.append(ImportConflict(
            conflict_type="issue_id_conflict",
            severity="warning" if strategy != "skip" else "info",
            message=f"发现 {len(conflicting_ids)} 个问题编号冲突，将使用 '{strategy}' 策略处理",
            details={"conflicting_ids": conflicting_ids, "strategy": strategy},
            hint=f"可使用 --strategy 指定冲突处理方式: skip(保留目标)/overwrite(覆盖)/renumber(重编号)/merge(智能合并)",
        ))

    if dry_run:
        _simulate_import(snap_state, target_state, strategy, report)
        report.phase = "dry_run_complete"
        _append_ops_log(workspace, {
            "op": "import_dry_run",
            "timestamp": datetime.now().isoformat(),
            "snapshot_path": snapshot_path,
            "result": "success",
            "conflicts_count": len(report.conflicts),
            "warnings_count": sum(1 for c in report.conflicts if c.severity == "warning"),
            "strategy": strategy,
        })
        return report

    report.phase = "executing"
    backup_path = _generate_backup_path(workspace, "pre_import")
    _backup_workspace(workspace, backup_path)
    report.backup_path = backup_path

    try:
        need_write_config = False
        if target_config is None:
            need_write_config = True
        elif include_config and config_diffs:
            need_write_config = True

        if need_write_config:
            save_config(workspace, snap_config)
            report.config_updated = True

        _apply_import(snap_state, target_state, strategy, report)
        save_state(workspace, target_state)

        report.success = True
        report.phase = "completed"

        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "snapshot_path": snapshot_path,
            "result": "success",
            "strategy": strategy,
            "include_config": include_config,
            "issues_imported": report.issues_imported,
            "issues_skipped": report.issues_skipped,
            "issues_overwritten": report.issues_overwritten,
            "issues_renumbered": report.issues_renumbered,
            "history_imported": report.history_imported,
            "config_updated": report.config_updated,
            "backup_path": backup_path,
            "content_hash": snap_info.content_hash,
            "snapshot_version": snap_info.snapshot_version,
        })
    except Exception as e:
        report.conflicts.append(ImportConflict(
            conflict_type="import_failed",
            severity="error",
            message=f"导入失败: {e}",
            hint="已从备份恢复，请检查错误信息后重试",
        ))
        restore_from_backup(workspace, backup_path)
        report.backup_path = backup_path
        report.success = False
        report.phase = "failed_rolled_back"

        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "snapshot_path": snapshot_path,
            "result": "failure",
            "failure_phase": "executing",
            "failure_reason": str(e),
            "backup_path": backup_path,
            "rolled_back": True,
        })

    return report


def _simulate_import(snap_state: WorkspaceState, target_state: WorkspaceState,
                    strategy: str, report: ImportReport) -> None:
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

    for batch in snap_state.undo_stack:
        new_batch = []
        for action in batch:
            new_action = _clone_review_action(action)
            new_action.action_id = f"ACT-{len(target_state.review_history) + 1 + len(new_batch):04d}"
            if strategy == "renumber" and renumbered_ids:
                id_map = {old: new for old, new in renumbered_ids}
                if new_action.issue_id in id_map:
                    new_action.issue_id = id_map[new_action.issue_id]
            new_batch.append(new_action)
        target_state.undo_stack.append(new_batch)

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
    backup_dir = os.path.join(workspace, STATE_DIRNAME, BACKUP_DIRNAME)
    if not os.path.isdir(backup_dir):
        return []
    return sorted(os.listdir(backup_dir), reverse=True)


def get_backup_path(workspace: str, backup_name: str) -> str:
    return os.path.join(workspace, STATE_DIRNAME, BACKUP_DIRNAME, backup_name)


def _get_ops_log_path(workspace: str) -> str:
    return os.path.join(workspace, STATE_DIRNAME, OPS_LOG_FILENAME)


def _append_ops_log(workspace: str, entry: dict) -> None:
    log_path = _get_ops_log_path(workspace)
    state_dir = os.path.dirname(log_path)
    if not os.path.isdir(state_dir):
        return
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_ops_log(workspace: str, op_filter: str = None, limit: int = 50) -> List[dict]:
    log_path = _get_ops_log_path(workspace)
    if not os.path.exists(log_path):
        return []
    entries = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if op_filter is None or entry.get("op") == op_filter:
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries[-limit:]


def validate_snapshot_file(snapshot_path: str) -> ImportReport:
    report = ImportReport(dry_run=True)
    report.phase = "file_validation"

    if not os.path.exists(snapshot_path):
        report.conflicts.append(ImportConflict(
            conflict_type="snapshot_missing",
            severity="error",
            message=f"快照文件不存在: {snapshot_path}",
            hint="请检查快照文件路径是否正确",
        ))
        return report

    try:
        with open(snapshot_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    except json.JSONDecodeError as e:
        report.conflicts.append(ImportConflict(
            conflict_type="snapshot_invalid_json",
            severity="error",
            message=f"快照文件不是有效的 JSON: {e}",
            hint="文件可能在传输中损坏，请重新导出快照",
        ))
        return report
    except Exception as e:
        report.conflicts.append(ImportConflict(
            conflict_type="snapshot_read_error",
            severity="error",
            message=f"快照文件读取失败: {e}",
            hint="请检查文件是否存在且可读",
        ))
        return report

    structure_conflicts = _validate_snapshot_structure(raw_data)
    report.conflicts.extend(structure_conflicts)
    if report.has_errors:
        return report

    try:
        snap_info, _, _ = load_snapshot(snapshot_path)
        report.snapshot_info = snap_info
    except Exception as e:
        report.conflicts.append(ImportConflict(
            conflict_type="snapshot_parse_error",
            severity="error",
            message=f"快照数据解析失败: {e}",
            hint="快照内部数据格式异常，请确认导出工具版本并重新导出",
        ))
        return report

    version_conflicts = _validate_version_compatibility(snap_info)
    report.conflicts.extend(version_conflicts)

    integrity_conflicts = _validate_integrity(raw_data, snap_info)
    report.conflicts.extend(integrity_conflicts)

    if not report.has_errors:
        report.success = True
        report.phase = "file_valid"

    return report
