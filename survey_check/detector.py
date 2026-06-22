"""问题检测引擎"""
import os
from typing import List, Dict, Tuple
from collections import defaultdict

from .models import (
    Issue, IssueType, IssueStatus, FileType,
    SurveyPoint, FileEntry,
)
from .config import SurveyConfig
from .state import generate_issue_id, WorkspaceState
from datetime import datetime


def detect_issues(state: WorkspaceState, config: SurveyConfig,
                scan_result: dict, scan_errors: List[str]) -> List[Issue]:
    """
    检测所有问题

    Args:
        state: 工作区状态（用于生成 issue_id）
        config: 配置
        scan_result: 扫描结果 {'photos': [...], 'tracks': [...], 'tables': [...]}
        scan_errors: 扫描时的错误列表

    Returns:
        问题列表
    """
    issues: List[Issue] = []

    photos = scan_result.get("photos", [])
    tracks = scan_result.get("tracks", [])
    tables = scan_result.get("tables", [])
    points = state.survey_points

    issues.extend(_detect_bad_paths(scan_errors, state))
    issues.extend(_detect_missing_files(points, photos, FileType.PHOTO, config, state))
    issues.extend(_detect_missing_files(points, tracks, FileType.TRACK, config, state))
    issues.extend(_detect_missing_files(points, tables, FileType.TABLE, config, state))
    issues.extend(_detect_duplicates(photos, FileType.PHOTO, config, state))
    issues.extend(_detect_duplicates(tracks, FileType.TRACK, config, state))
    issues.extend(_detect_duplicates(tables, FileType.TABLE, config, state))
    issues.extend(_detect_name_conflicts(photos, FileType.PHOTO, config, state))
    issues.extend(_detect_name_conflicts(tracks, FileType.TRACK, config, state))
    issues.extend(_detect_name_conflicts(tables, FileType.TABLE, config, state))

    now = datetime.now().isoformat()
    for issue in issues:
        if not issue.created_at:
            issue.created_at = now
        if not issue.updated_at:
            issue.updated_at = now

    return issues


def _detect_bad_paths(scan_errors: List[str], state: WorkspaceState) -> List[Issue]:
    """检测坏路径错误"""
    issues = []
    for error in scan_errors:
        issue = Issue(
            id=generate_issue_id(state),
            issue_type=IssueType.BAD_PATH,
            status=IssueStatus.OPEN,
            description=error,
            file_paths=[],
        )
        issues.append(issue)
    return issues


def _detect_missing_files(points: List[SurveyPoint], files: List[FileEntry],
                         file_type: FileType, config: SurveyConfig,
                         state: WorkspaceState) -> List[Issue]:
    """检测缺失的文件"""
    issues = []
    file_names_lower = {f.filename.lower(): f for f in files}

    for point in points:
        pattern = _get_pattern_for_type(file_type, config)
        expected_names = _generate_expected_names(point.point_id, pattern, file_type, config)

        found = False
        for expected in expected_names:
            if expected.lower() in file_names_lower:
                found = True
                break

        if not found:
            issue = Issue(
                id=generate_issue_id(state),
                issue_type=IssueType.MISSING,
                status=IssueStatus.OPEN,
                description=f"调查点 '{point.point_id}' ({point.name}) 缺少{_type_label(file_type)}文件",
                file_type=file_type,
                point_id=point.point_id,
                file_paths=[],
            )
            issues.append(issue)

    return issues


def _detect_duplicates(files: List[FileEntry], file_type: FileType,
                      config: SurveyConfig, state: WorkspaceState) -> List[Issue]:
    """检测重复文件（多个文件对应同一调查点）"""
    issues = []
    point_files: Dict[str, List[FileEntry]] = defaultdict(list)

    for f in files:
        point_id = _extract_point_id(f.filename, config, file_type, state)
        if point_id:
            point_files[point_id].append(f)

    for point_id, file_list in point_files.items():
        if len(file_list) > 1:
            paths = [f.path for f in file_list]
            issue = Issue(
                id=generate_issue_id(state),
                issue_type=IssueType.DUPLICATE,
                status=IssueStatus.OPEN,
                description=f"调查点 '{point_id}' 有 {len(file_list)} 个{_type_label(file_type)}文件重复",
                file_type=file_type,
                point_id=point_id,
                file_paths=paths,
            )
            issues.append(issue)

    return issues


def _detect_name_conflicts(files: List[FileEntry], file_type: FileType,
                          config: SurveyConfig, state: WorkspaceState) -> List[Issue]:
    """检测命名冲突（无法识别调查点编号的文件）"""
    issues = []
    point_ids = {p.point_id for p in state.survey_points}

    for f in files:
        point_id = _extract_point_id(f.filename, config, file_type, state)
        if not point_id:
            issue = Issue(
                id=generate_issue_id(state),
                issue_type=IssueType.NAME_CONFLICT,
                status=IssueStatus.OPEN,
                description=f"{_type_label(file_type).capitalize()}文件 '{f.filename}' 无法识别调查点编号",
                file_type=file_type,
                file_paths=[f.path],
            )
            issues.append(issue)
        elif point_id not in point_ids:
            issue = Issue(
                id=generate_issue_id(state),
                issue_type=IssueType.NAME_CONFLICT,
                status=IssueStatus.OPEN,
                description=f"{_type_label(file_type).capitalize()}文件 '{f.filename}' 对应的调查点 '{point_id}' 不在清单中",
                file_type=file_type,
                point_id=point_id,
                file_paths=[f.path],
            )
            issues.append(issue)

    return issues


def _get_pattern_for_type(file_type: FileType, config: SurveyConfig) -> str:
    if file_type == FileType.PHOTO:
        return config.photo_pattern
    elif file_type == FileType.TRACK:
        return config.track_pattern
    elif file_type == FileType.TABLE:
        return config.table_pattern
    return "{point_id}"


def _type_label(file_type: FileType) -> str:
    labels = {
        FileType.PHOTO: "照片",
        FileType.TRACK: "轨迹",
        FileType.TABLE: "表格",
    }
    return labels.get(file_type, "文件")


def _generate_expected_names(point_id: str, pattern: str, file_type: FileType,
                           config: SurveyConfig) -> List[str]:
    """
    根据模式生成期望的文件名（带扩展名）
    """
    base = pattern.format(point_id=point_id)
    exts = []
    if file_type == FileType.PHOTO:
        exts = config.photo_exts
    elif file_type == FileType.TRACK:
        exts = config.track_exts
    elif file_type == FileType.TABLE:
        exts = config.table_exts

    return [f"{base}{ext}" for ext in exts]


def _extract_point_id(filename: str, config: SurveyConfig,
                      file_type: FileType, state: WorkspaceState) -> str:
    """
    从文件名中提取调查点编号
    优先用模式匹配，然后验证是否在清单中；
    不在清单中时再尝试模糊匹配（前后缀匹配）
    """
    name_no_ext = os.path.splitext(filename)[0]
    pattern = _get_pattern_for_type(file_type, config)
    point_ids = {p.point_id for p in state.survey_points}

    extracted = None
    if "{point_id}" in pattern:
        prefix, suffix = pattern.split("{point_id}", 1)
        if name_no_ext.startswith(prefix) and name_no_ext.endswith(suffix):
            if suffix:
                extracted = name_no_ext[len(prefix):-len(suffix)]
            else:
                extracted = name_no_ext[len(prefix):]
            if extracted and extracted in point_ids:
                return extracted

    for point in state.survey_points:
        pid = point.point_id
        if name_no_ext == pid:
            return pid
        if name_no_ext.startswith(pid + "_") or name_no_ext.startswith(pid + "-"):
            return pid
        if name_no_ext.endswith("_" + pid) or name_no_ext.endswith("-" + pid):
            return pid

    if extracted:
        return extracted

    return ""
