"""数据模型定义"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from enum import Enum
from datetime import datetime


class IssueType(str, Enum):
    MISSING = "missing"
    DUPLICATE = "duplicate"
    NAME_CONFLICT = "name_conflict"
    BAD_PATH = "bad_path"


class IssueStatus(str, Enum):
    OPEN = "open"
    PENDING = "pending"
    ACCEPTED = "accepted"
    IGNORED = "ignored"


class FileType(str, Enum):
    PHOTO = "photo"
    TRACK = "track"
    TABLE = "table"


@dataclass
class SurveyPoint:
    """调查点条目"""
    point_id: str
    name: str
    original_row: int
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FileEntry:
    """扫描到的文件条目"""
    file_type: FileType
    path: str
    filename: str
    size: int
    modified: str
    point_id: Optional[str] = None


@dataclass
class Issue:
    """问题条目"""
    id: str
    issue_type: IssueType
    status: IssueStatus
    description: str
    file_type: Optional[FileType] = None
    point_id: Optional[str] = None
    file_paths: List[str] = field(default_factory=list)
    remark: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ReviewAction:
    """复核操作记录，用于撤销"""
    action_id: str
    issue_id: str
    old_status: IssueStatus
    new_status: IssueStatus
    old_remark: str
    new_remark: str
    timestamp: str


@dataclass
class ScanResult:
    """扫描结果"""
    photos: List[FileEntry] = field(default_factory=list)
    tracks: List[FileEntry] = field(default_factory=list)
    tables: List[FileEntry] = field(default_factory=list)
    scan_time: str = ""


@dataclass
class ReportStats:
    """报告统计"""
    total_points: int = 0
    total_issues: int = 0
    open_issues: int = 0
    pending_issues: int = 0
    accepted_issues: int = 0
    ignored_issues: int = 0
    missing_count: int = 0
    duplicate_count: int = 0
    name_conflict_count: int = 0
    bad_path_count: int = 0
