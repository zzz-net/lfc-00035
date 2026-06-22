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

PREFLIGHT_PROCEED = "proceed"
PREFLIGHT_CONFIRM = "confirm"
PREFLIGHT_ABORT = "abort"

CATEGORY_CONFIG_MISSING = "config_missing"
CATEGORY_RESIDUAL_STATE = "residual_state"
CATEGORY_VERSION_MISMATCH = "version_mismatch"
CATEGORY_TARGET_HAS_DATA = "target_has_data"
CATEGORY_SNAPSHOT_INVALID = "snapshot_invalid"
CATEGORY_TARGET_INVALID = "target_invalid"
CATEGORY_CONTENT_CONFLICT = "content_conflict"


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
    category: str = ""
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
    preflight_conclusion: str = ""
    conflict_summary: Dict[str, int] = field(default_factory=dict)
    import_id: str = ""

    @property
    def has_errors(self) -> bool:
        return any(c.severity == "error" for c in self.conflicts)

    @property
    def has_warnings(self) -> bool:
        return any(c.severity == "warning" for c in self.conflicts)

    @property
    def abort_conflicts(self) -> List[ImportConflict]:
        return [c for c in self.conflicts if c.severity == "error"]

    @property
    def confirm_conflicts(self) -> List[ImportConflict]:
        return [c for c in self.conflicts if c.severity == "warning"]


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
            category=CATEGORY_SNAPSHOT_INVALID,
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
            category=CATEGORY_SNAPSHOT_INVALID,
            message=f"快照信息缺少字段: {', '.join(sorted(missing_info))}",
            hint="快照元信息不完整，请确认快照来源是否可靠",
        ))

    state = data.get("state", {})
    missing_state = REQUIRED_STATE_KEYS - set(state.keys())
    if missing_state:
        conflicts.append(ImportConflict(
            conflict_type="snapshot_state_missing",
            severity="error",
            category=CATEGORY_SNAPSHOT_INVALID,
            message=f"快照状态缺少字段: {', '.join(sorted(missing_state))}",
            hint="快照状态数据不完整，请重新导出",
        ))

    cfg = data.get("config", {})
    missing_cfg = REQUIRED_CONFIG_KEYS - set(cfg.keys())
    if missing_cfg:
        conflicts.append(ImportConflict(
            conflict_type="snapshot_config_missing",
            severity="error",
            category=CATEGORY_CONFIG_MISSING,
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
            category=CATEGORY_VERSION_MISMATCH,
            message=f"快照版本 {info.snapshot_version} 不受支持（支持: {', '.join(sorted(SUPPORTED_SNAPSHOT_VERSIONS))}）",
            hint="请升级 survey-check 或使用匹配版本的快照",
        ))
    if info.state_version and info.state_version not in SUPPORTED_STATE_VERSIONS:
        conflicts.append(ImportConflict(
            conflict_type="state_version_unsupported",
            severity="error",
            category=CATEGORY_VERSION_MISMATCH,
            message=f"快照状态版本 {info.state_version} 不受支持（支持: {', '.join(sorted(SUPPORTED_STATE_VERSIONS))}）",
            hint="请升级 survey-check 或重新导出快照",
        ))
    if info.config_version and info.config_version not in SUPPORTED_CONFIG_VERSIONS:
        conflicts.append(ImportConflict(
            conflict_type="config_version_unsupported",
            severity="error",
            category=CATEGORY_VERSION_MISMATCH,
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
            category=CATEGORY_SNAPSHOT_INVALID,
            message="快照无内容校验和，无法验证完整性",
            hint="该快照可能由旧版工具导出，建议重新导出以附带校验和",
        ))
        return conflicts
    actual_hash = _compute_content_hash(data.get("config", {}), data.get("state", {}))
    if actual_hash != info.content_hash:
        conflicts.append(ImportConflict(
            conflict_type="snapshot_checksum_mismatch",
            severity="error",
            category=CATEGORY_SNAPSHOT_INVALID,
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
                category=CATEGORY_TARGET_INVALID,
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
            category=CATEGORY_TARGET_INVALID,
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
            category=CATEGORY_RESIDUAL_STATE,
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
                    category=CATEGORY_RESIDUAL_STATE,
                    message="目标配置文件内容异常（缺少 config_version）",
                    hint="配置文件可能已损坏，导入时将从快照覆盖；建议备份后手动检查",
                ))
        except (json.JSONDecodeError, OSError) as e:
            conflicts.append(ImportConflict(
                conflict_type="residual_config_corrupted",
                severity="warning",
                category=CATEGORY_RESIDUAL_STATE,
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
                    category=CATEGORY_RESIDUAL_STATE,
                    message="目标状态文件内容异常（缺少 issues 字段）",
                    hint="状态文件可能已损坏，导入时将自动备份并覆盖；建议备份后手动检查",
                ))
        except (json.JSONDecodeError, OSError) as e:
            conflicts.append(ImportConflict(
                conflict_type="residual_state_corrupted",
                severity="warning",
                category=CATEGORY_RESIDUAL_STATE,
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
                category=CATEGORY_TARGET_HAS_DATA,
                message=f"目标工作区已有 {n_backups} 个导入备份",
                hint="如需清理旧备份，可手动删除 .survey_check/backups/ 目录中的旧条目",
            ))

    return conflicts


def _generate_import_id() -> str:
    return f"IMP-{datetime.now().strftime('%Y%m%d%H%M%S')}-{os.getpid():05d}"


def _compute_conflict_summary(conflicts: List[ImportConflict]) -> Dict[str, int]:
    summary: Dict[str, int] = {}
    for c in conflicts:
        cat = c.category or "uncategorized"
        summary[cat] = summary.get(cat, 0) + 1
    return summary


def _determine_preflight_conclusion(conflicts: List[ImportConflict]) -> str:
    has_errors = any(c.severity == "error" for c in conflicts)
    has_warnings = any(c.severity == "warning" for c in conflicts)
    if has_errors:
        return PREFLIGHT_ABORT
    if has_warnings:
        return PREFLIGHT_CONFIRM
    return PREFLIGHT_PROCEED


def _check_duplicate_import(workspace: str, content_hash: str) -> Optional[dict]:
    if not content_hash:
        return None
    entries = read_ops_log(workspace, op_filter="import", limit=100)
    for entry in reversed(entries):
        if (entry.get("result") == "success"
                and entry.get("content_hash") == content_hash
                and not entry.get("rolled_back")):
            return entry
    return None


def _atomic_write_json(path: str, data: dict) -> None:
    tmp_path = f"{path}.tmp_{os.getpid()}_{datetime.now().strftime('%H%M%S%f')}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def save_config_atomic(workspace: str, config: SurveyConfig) -> None:
    config_path = get_config_path(workspace)
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    _atomic_write_json(config_path, config.to_dict())


def save_state_atomic(workspace: str, state: WorkspaceState) -> None:
    state_path = get_state_path(workspace)
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    _atomic_write_json(state_path, state.to_dict())


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


def _remap_single_path(source_path: str, source_workspace: str,
                       target_workspace: str, default_basename: str) -> str:
    """将单个源路径重映射到目标工作区内。

    策略：
    - 空路径：返回默认值（基于 target_workspace + default_basename）
    - 相对路径：以 target_workspace 为基准转为绝对路径
    - 绝对路径且位于 source_workspace 内：保持相对结构，重放到 target_workspace
    - 绝对路径但位于 source_workspace 之外：仅取 basename，放到 target_workspace 下
    """
    if not source_path:
        return os.path.abspath(os.path.join(target_workspace, default_basename))

    if not os.path.isabs(source_path):
        return os.path.abspath(os.path.join(target_workspace, source_path))

    try:
        rel = os.path.relpath(source_path, source_workspace)
    except ValueError:
        rel = None

    if rel and not rel.startswith("..") and not os.path.isabs(rel):
        return os.path.abspath(os.path.join(target_workspace, rel))

    return os.path.abspath(os.path.join(target_workspace, os.path.basename(source_path)))


def _remap_config_paths(snap_config: SurveyConfig, source_workspace: str,
                        target_workspace: str) -> SurveyConfig:
    """返回一个新的 SurveyConfig，其中 4 个路径字段已重映射到 target_workspace。"""
    new_cfg = SurveyConfig.from_dict(asdict(snap_config))
    new_cfg.manifest_path = _remap_single_path(
        snap_config.manifest_path, source_workspace, target_workspace, "manifest.csv")
    new_cfg.photo_dir = _remap_single_path(
        snap_config.photo_dir, source_workspace, target_workspace, "photos")
    new_cfg.track_dir = _remap_single_path(
        snap_config.track_dir, source_workspace, target_workspace, "tracks")
    new_cfg.table_dir = _remap_single_path(
        snap_config.table_dir, source_workspace, target_workspace, "tables")
    return new_cfg


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


def _build_preflight_log(report: ImportReport, snapshot_path: str,
                        strategy: str, include_config: bool) -> dict:
    return {
        "op": "import_preflight" if report.dry_run else "import",
        "timestamp": datetime.now().isoformat(),
        "import_id": report.import_id,
        "snapshot_path": snapshot_path,
        "dry_run": report.dry_run,
        "phase": report.phase,
        "preflight_conclusion": report.preflight_conclusion,
        "conflict_summary": report.conflict_summary,
        "conflicts": [
            {
                "conflict_type": c.conflict_type,
                "severity": c.severity,
                "category": c.category,
                "message": c.message,
            }
            for c in report.conflicts
        ],
        "strategy": strategy,
        "include_config": include_config,
        "snapshot_info": (asdict(report.snapshot_info)
                          if report.snapshot_info else None),
        "result": "preflight_complete" if report.dry_run else "pending_confirm",
    }


def _do_import(workspace: str, snapshot_path: str,
              dry_run: bool = False,
              strategy: str = "skip",
              include_config: bool = False) -> ImportReport:
    report = ImportReport(dry_run=dry_run)
    report.import_id = _generate_import_id()

    def _finalize_preflight():
        report.conflict_summary = _compute_conflict_summary(report.conflicts)
        report.preflight_conclusion = _determine_preflight_conclusion(report.conflicts)

    if not os.path.exists(snapshot_path):
        report.conflicts.append(ImportConflict(
            conflict_type="snapshot_missing",
            severity="error",
            category=CATEGORY_SNAPSHOT_INVALID,
            message=f"快照文件不存在: {snapshot_path}",
            hint="请检查快照文件路径是否正确",
        ))
        _finalize_preflight()
        _append_ops_log(workspace, {
            **_build_preflight_log(report, snapshot_path, strategy, include_config),
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
            category=CATEGORY_SNAPSHOT_INVALID,
            message=f"快照文件不是有效的 JSON: {e}",
            hint="文件可能在传输中损坏，请重新导出快照",
        ))
        _finalize_preflight()
        _append_ops_log(workspace, {
            **_build_preflight_log(report, snapshot_path, strategy, include_config),
            "result": "failure",
            "failure_phase": "snapshot_validation",
            "failure_reason": "invalid_json",
        })
        return report
    except Exception as e:
        report.conflicts.append(ImportConflict(
            conflict_type="snapshot_read_error",
            severity="error",
            category=CATEGORY_SNAPSHOT_INVALID,
            message=f"快照文件读取失败: {e}",
            hint="请检查文件是否存在且可读",
        ))
        _finalize_preflight()
        _append_ops_log(workspace, {
            **_build_preflight_log(report, snapshot_path, strategy, include_config),
            "result": "failure",
            "failure_phase": "snapshot_validation",
            "failure_reason": "read_error",
        })
        return report

    structure_conflicts = _validate_snapshot_structure(raw_data)
    report.conflicts.extend(structure_conflicts)
    if report.has_errors:
        _finalize_preflight()
        _append_ops_log(workspace, {
            **_build_preflight_log(report, snapshot_path, strategy, include_config),
            "result": "failure",
            "failure_phase": "snapshot_validation",
            "failure_reason": "structure_invalid",
        })
        return report

    try:
        snap_info, snap_config, snap_state = load_snapshot(snapshot_path)
        report.snapshot_info = snap_info
        source_ws = snap_info.source_workspace or workspace
        remapped_config = _remap_config_paths(snap_config, source_ws, workspace)
    except Exception as e:
        report.conflicts.append(ImportConflict(
            conflict_type="snapshot_parse_error",
            severity="error",
            category=CATEGORY_SNAPSHOT_INVALID,
            message=f"快照数据解析失败: {e}",
            hint="快照内部数据格式异常，请确认导出工具版本并重新导出",
        ))
        _finalize_preflight()
        _append_ops_log(workspace, {
            **_build_preflight_log(report, snapshot_path, strategy, include_config),
            "result": "failure",
            "failure_phase": "snapshot_validation",
            "failure_reason": "parse_error",
        })
        return report

    version_conflicts = _validate_version_compatibility(snap_info)
    report.conflicts.extend(version_conflicts)
    if report.has_errors:
        _finalize_preflight()
        _append_ops_log(workspace, {
            **_build_preflight_log(report, snapshot_path, strategy, include_config),
            "result": "failure",
            "failure_phase": "version_check",
            "failure_reason": "version_unsupported",
        })
        return report

    integrity_conflicts = _validate_integrity(raw_data, snap_info)
    report.conflicts.extend(integrity_conflicts)
    if report.has_errors:
        _finalize_preflight()
        _append_ops_log(workspace, {
            **_build_preflight_log(report, snapshot_path, strategy, include_config),
            "result": "failure",
            "failure_phase": "integrity_check",
            "failure_reason": "checksum_mismatch",
        })
        return report

    report.phase = "target_validation"
    target_conflicts = _validate_target_workspace(workspace)
    report.conflicts.extend(target_conflicts)
    if report.has_errors:
        _finalize_preflight()
        _append_ops_log(workspace, {
            **_build_preflight_log(report, snapshot_path, strategy, include_config),
            "result": "failure",
            "failure_phase": "target_validation",
            "failure_reason": "target_dir_invalid",
        })
        return report

    residual_conflicts = _detect_residual_conflicts(workspace)
    report.conflicts.extend(residual_conflicts)
    if report.has_errors:
        _finalize_preflight()
        _append_ops_log(workspace, {
            **_build_preflight_log(report, snapshot_path, strategy, include_config),
            "result": "failure",
            "failure_phase": "target_validation",
            "failure_reason": "residual_conflict",
        })
        return report

    report.phase = "content_check"
    target_config = load_config(workspace) if os.path.exists(get_config_path(workspace)) else None
    target_state = load_state(workspace)

    has_existing_data = (
        target_config is not None
        or len(target_state.issues) > 0
        or len(target_state.review_history) > 0
        or len(target_state.survey_points) > 0
    )
    if has_existing_data:
        target_cat = CATEGORY_TARGET_HAS_DATA
    else:
        target_cat = CATEGORY_CONFIG_MISSING

    config_diffs = _compare_configs(remapped_config, target_config) if target_config else []

    path_remap_info = {
        "manifest_path": (snap_config.manifest_path, remapped_config.manifest_path),
        "photo_dir": (snap_config.photo_dir, remapped_config.photo_dir),
        "track_dir": (snap_config.track_dir, remapped_config.track_dir),
        "table_dir": (snap_config.table_dir, remapped_config.table_dir),
    }

    if target_config is None:
        report.conflicts.append(ImportConflict(
            conflict_type="no_target_config",
            severity="info",
            category=target_cat,
            message="目标工作区无配置，将从快照恢复配置（路径已重映射至目标工作区）",
            details={"path_remap": path_remap_info},
            hint="导入将自动恢复并重映射路径，无需额外操作",
        ))
    elif config_diffs:
        diff_desc = "; ".join(f"{k}" for k, _, _ in config_diffs)
        report.conflicts.append(ImportConflict(
            conflict_type="config_mismatch",
            severity="warning",
            category=CATEGORY_CONTENT_CONFLICT,
            message=f"配置不一致（{len(config_diffs)} 处差异: {diff_desc}），导入时路径将重映射至目标工作区",
            details={"diffs": config_diffs, "path_remap": path_remap_info},
            hint="使用 --include-config 可覆盖目标配置（路径已自动重映射），否则保留当前配置",
        ))

    if target_state.last_scan_time and snap_state.last_scan_time:
        snap_time = snap_state.last_scan_time
        target_time = target_state.last_scan_time
        if target_time > snap_time:
            report.conflicts.append(ImportConflict(
                conflict_type="target_newer_scan",
                severity="warning",
                category=CATEGORY_CONTENT_CONFLICT,
                message=f"目标工作区扫描时间 ({target_time}) 晚于快照 ({snap_time})，目标可能有更新的结果",
                hint="建议先备份目标工作区（使用 export 命令），再决定是否导入",
            ))

    conflicting_ids, target_by_id = _find_conflicting_issues(snap_state, target_state)

    if conflicting_ids:
        report.conflicts.append(ImportConflict(
            conflict_type="issue_id_conflict",
            severity="warning" if strategy != "skip" else "info",
            category=CATEGORY_CONTENT_CONFLICT,
            message=f"发现 {len(conflicting_ids)} 个问题编号冲突，将使用 '{strategy}' 策略处理",
            details={"conflicting_ids": conflicting_ids, "strategy": strategy},
            hint=f"可使用 --strategy 指定冲突处理方式: skip(保留目标)/overwrite(覆盖)/renumber(重编号)/merge(智能合并)",
        ))

    if len(target_state.issues) > 0 and len(snap_state.issues) > 0:
        report.conflicts.append(ImportConflict(
            conflict_type="target_has_existing_issues",
            severity="warning",
            category=CATEGORY_TARGET_HAS_DATA,
            message=f"目标工作区已有 {len(target_state.issues)} 个问题记录，快照含 {len(snap_state.issues)} 个",
            hint="请确认导入策略：skip 保留目标，overwrite 覆盖，renumber 并存，merge 智能合并",
        ))

    duplicate_entry = _check_duplicate_import(workspace, snap_info.content_hash)
    if duplicate_entry:
        report.conflicts.append(ImportConflict(
            conflict_type="duplicate_import_warning",
            severity="warning",
            category=CATEGORY_CONTENT_CONFLICT,
            message=f"检测到相同内容的快照已成功导入过（时间: {duplicate_entry.get('timestamp', '?')}）",
            details={"previous_import": duplicate_entry},
            hint="若需重复导入请确认是否必要，skip 策略下通常无变化",
        ))

    _finalize_preflight()

    if dry_run:
        _simulate_import(snap_state, target_state, strategy, report)
        report.phase = "dry_run_complete"
        _append_ops_log(workspace, {
            **_build_preflight_log(report, snapshot_path, strategy, include_config),
            "op": "import_dry_run",
            "result": "success",
            "issues_imported": report.issues_imported,
            "issues_skipped": report.issues_skipped,
            "issues_overwritten": report.issues_overwritten,
            "issues_renumbered": report.issues_renumbered,
            "history_imported": report.history_imported,
        })
        return report

    _append_ops_log(workspace, {
        **_build_preflight_log(report, snapshot_path, strategy, include_config),
        "result": "pending_confirm",
    })

    if report.preflight_conclusion == PREFLIGHT_ABORT:
        report.phase = "aborted_preflight"
        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "import_id": report.import_id,
            "snapshot_path": snapshot_path,
            "phase": report.phase,
            "preflight_conclusion": report.preflight_conclusion,
            "result": "failure",
            "failure_phase": "preflight",
            "failure_reason": "aborted_by_preflight",
        })
        return report

    report.phase = "executing"
    backup_path = _generate_backup_path(workspace, "pre_import")
    _backup_workspace(workspace, backup_path)
    report.backup_path = backup_path

    original_target_state_dict = asdict(target_state)
    original_target_config_dict = asdict(target_config) if target_config else None

    try:
        need_write_config = False
        if target_config is None:
            need_write_config = True
        elif include_config and config_diffs:
            need_write_config = True

        if need_write_config:
            save_config_atomic(workspace, remapped_config)
            report.config_updated = True

        if os.environ.get("SURVEY_CHECK_TEST_INJECT_ABORT_AFTER_CONFIG"):
            raise RuntimeError("测试注入: 配置已写、状态未写时模拟失败")

        _apply_import(snap_state, target_state, strategy, report)

        if strategy == "overwrite":
            pass
        else:
            pass

        save_state_atomic(workspace, target_state)

        report.success = True
        report.phase = "completed"

        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "import_id": report.import_id,
            "snapshot_path": snapshot_path,
            "phase": report.phase,
            "preflight_conclusion": report.preflight_conclusion,
            "conflict_summary": report.conflict_summary,
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
            "path_remap": path_remap_info if report.config_updated else None,
            "source_workspace": snap_info.source_workspace,
        })
    except Exception as e:
        import traceback as _tb
        err_traceback = _tb.format_exc()

        report.conflicts.append(ImportConflict(
            conflict_type="import_failed",
            severity="error",
            category=CATEGORY_TARGET_INVALID,
            message=f"导入失败: {e}",
            details={"traceback": err_traceback},
            hint="已从备份恢复，请检查错误信息后重试",
        ))

        try:
            if original_target_config_dict is not None:
                cfg_obj = SurveyConfig.from_dict(original_target_config_dict)
                save_config_atomic(workspace, cfg_obj)
            state_obj = WorkspaceState.from_dict(original_target_state_dict)
            save_state_atomic(workspace, state_obj)
        except Exception:
            restore_from_backup(workspace, backup_path)

        report.backup_path = backup_path
        report.success = False
        report.phase = "failed_rolled_back"
        report.preflight_conclusion = PREFLIGHT_ABORT
        report.conflict_summary = _compute_conflict_summary(report.conflicts)

        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "import_id": report.import_id,
            "snapshot_path": snapshot_path,
            "phase": report.phase,
            "preflight_conclusion": report.preflight_conclusion,
            "conflict_summary": report.conflict_summary,
            "result": "failure",
            "failure_phase": "executing",
            "failure_reason": str(e),
            "failure_traceback": err_traceback,
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

    hist_count = 0
    renumber_map = None
    if strategy == "renumber":
        renum_target = target_by_id.keys() & {i.id for i in snap_state.issues}
        if renum_target:
            renumber_map = {}
            next_n = target_state.next_issue_number
            for issue in snap_state.issues:
                if issue.id in renum_target:
                    renumber_map[issue.id] = f"ISS-{next_n:04d}"
                    next_n += 1
    for action in snap_state.review_history:
        if strategy != "renumber" and _review_action_in_list(action, target_state.review_history):
            continue
        if strategy == "renumber" and renumber_map and _review_action_in_list(action, target_state.review_history, renumber_map):
            continue
        hist_count += 1
    report.history_imported = hist_count


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

    id_map = {old: new for old, new in renumbered_ids} if renumbered_ids else None
    hist_imported = 0
    for action in snap_state.review_history:
        if strategy != "renumber" and _review_action_in_list(action, target_state.review_history):
            continue
        if strategy == "renumber" and id_map and _review_action_in_list(action, target_state.review_history, id_map):
            continue
        action_id = f"ACT-{len(target_state.review_history) + 1:04d}"
        new_action = _clone_review_action(action)
        new_action.action_id = action_id

        if strategy == "renumber" and renumbered_ids:
            if new_action.issue_id in id_map:
                new_action.issue_id = id_map[new_action.issue_id]

        target_state.review_history.append(new_action)
        hist_imported += 1

    undo_imported = 0
    for batch in snap_state.undo_stack:
        new_batch = []
        for action in batch:
            if strategy != "renumber" and _review_action_in_list(action, target_state.review_history):
                continue
            if strategy == "renumber" and id_map and _review_action_in_list(action, target_state.review_history, id_map):
                continue
            new_action = _clone_review_action(action)
            new_action.action_id = f"ACT-{len(target_state.review_history) + 1 + len(new_batch):04d}"
            if strategy == "renumber" and renumbered_ids:
                if new_action.issue_id in id_map:
                    new_action.issue_id = id_map[new_action.issue_id]
            new_batch.append(new_action)
        if new_batch:
            target_state.undo_stack.append(new_batch)
            undo_imported += len(new_batch)

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
    report.history_imported = hist_imported


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


def _review_action_semantic_equal(a: ReviewAction, b: ReviewAction,
                                 issue_id_map: dict = None) -> bool:
    """比较两个 ReviewAction 的语义等价性（忽略 action_id，可传 issue_id_map 做 renumber 映射）"""
    a_issue_id = a.issue_id if issue_id_map is None else issue_id_map.get(a.issue_id, a.issue_id)
    return (a_issue_id == b.issue_id
            and a.old_status == b.old_status
            and a.new_status == b.new_status
            and a.old_remark == b.old_remark
            and a.new_remark == b.new_remark
            and a.timestamp == b.timestamp)


def _review_action_in_list(action: ReviewAction,
                           target_list: List[ReviewAction],
                           issue_id_map: dict = None) -> bool:
    return any(_review_action_semantic_equal(action, t, issue_id_map)
               for t in target_list)


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
