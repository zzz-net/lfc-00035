"""工作区状态管理"""
import json
import os
import copy
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field, asdict

from .models import Issue, IssueStatus, SurveyPoint, FileEntry, ReviewAction, ScanResult, ReportStats
from .config import get_state_path, STATE_DIRNAME


@dataclass
class WorkspaceState:
    """工作区完整状态"""
    state_version: str = "1.0"
    config_version: str = "1.0"
    created_at: str = ""
    last_scan_time: str = ""
    next_issue_number: int = 1
    survey_points: List[SurveyPoint] = field(default_factory=list)
    scan_result: Optional[ScanResult] = None
    issues: List[Issue] = field(default_factory=list)
    review_history: List[ReviewAction] = field(default_factory=list)
    undo_stack: List[List[ReviewAction]] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = {
            "state_version": self.state_version,
            "config_version": self.config_version,
            "created_at": self.created_at,
            "last_scan_time": self.last_scan_time,
            "next_issue_number": self.next_issue_number,
            "survey_points": [asdict(p) for p in self.survey_points],
            "scan_result": asdict(self.scan_result) if self.scan_result else None,
            "issues": [asdict(i) for i in self.issues],
            "review_history": [asdict(a) for a in self.review_history],
            "undo_stack": [[asdict(a) for a in batch] for batch in self.undo_stack],
        }
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "WorkspaceState":
        state = cls()
        state.state_version = data.get("state_version", "1.0")
        state.config_version = data.get("config_version", "1.0")
        state.created_at = data.get("created_at", "")
        state.last_scan_time = data.get("last_scan_time", "")
        state.next_issue_number = data.get("next_issue_number", 1)

        points_data = data.get("survey_points", [])
        state.survey_points = [SurveyPoint(**p) for p in points_data]

        scan_data = data.get("scan_result")
        if scan_data:
            photos = [FileEntry(**f) for f in scan_data.get("photos", [])]
            tracks = [FileEntry(**f) for f in scan_data.get("tracks", [])]
            tables = [FileEntry(**f) for f in scan_data.get("tables", [])]
            state.scan_result = ScanResult(
                photos=photos,
                tracks=tracks,
                tables=tables,
                scan_time=scan_data.get("scan_time", ""),
            )

        issues_data = data.get("issues", [])
        state.issues = [Issue(**i) for i in issues_data]

        history_data = data.get("review_history", [])
        state.review_history = [ReviewAction(**a) for a in history_data]

        undo_data = data.get("undo_stack", [])
        state.undo_stack = [[ReviewAction(**a) for a in batch] for batch in undo_data]

        return state


def load_state(workspace: str) -> WorkspaceState:
    state_path = get_state_path(workspace)
    if not os.path.exists(state_path):
        state = WorkspaceState()
        state.created_at = datetime.now().isoformat()
        return state
    with open(state_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return WorkspaceState.from_dict(data)


def save_state(workspace: str, state: WorkspaceState) -> None:
    state_path = get_state_path(workspace)
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)


def get_issue_by_id(state: WorkspaceState, issue_id: str) -> Optional[Issue]:
    for issue in state.issues:
        if issue.id == issue_id:
            return issue
    return None


def generate_issue_id(state: WorkspaceState) -> str:
    issue_id = f"ISS-{state.next_issue_number:04d}"
    state.next_issue_number += 1
    return issue_id


def add_issue(state: WorkspaceState, issue: Issue) -> None:
    now = datetime.now().isoformat()
    if not issue.created_at:
        issue.created_at = now
    if not issue.updated_at:
        issue.updated_at = now
    state.issues.append(issue)


def update_issue_status(state: WorkspaceState, issue_id: str, new_status: IssueStatus,
                      remark: str = "") -> Optional[ReviewAction]:
    issue = get_issue_by_id(state, issue_id)
    if not issue:
        return None

    old_status = issue.status
    old_remark = issue.remark

    action = ReviewAction(
        action_id=f"ACT-{len(state.review_history) + 1:04d}",
        issue_id=issue_id,
        old_status=old_status,
        new_status=new_status,
        old_remark=old_remark,
        new_remark=remark,
        timestamp=datetime.now().isoformat(),
    )

    issue.status = new_status
    issue.remark = remark
    issue.updated_at = datetime.now().isoformat()

    state.review_history.append(action)
    return action


def push_undo_batch(state: WorkspaceState, actions: List[ReviewAction]) -> None:
    if actions:
        state.undo_stack.append(actions)


def undo_last_batch(state: WorkspaceState) -> List[ReviewAction]:
    if not state.undo_stack:
        return []

    last_batch = state.undo_stack.pop()
    for action in reversed(last_batch):
        issue = get_issue_by_id(state, action.issue_id)
        if issue:
            issue.status = action.old_status
            issue.remark = action.old_remark
            issue.updated_at = datetime.now().isoformat()
            state.review_history.append(ReviewAction(
                action_id=f"ACT-{len(state.review_history) + 1:04d}",
                issue_id=action.issue_id,
                old_status=action.new_status,
                new_status=action.old_status,
                old_remark=action.new_remark,
                new_remark=action.old_remark,
                timestamp=datetime.now().isoformat(),
            ))

    return last_batch


def can_undo(state: WorkspaceState) -> bool:
    return len(state.undo_stack) > 0


def compute_stats(state: WorkspaceState) -> ReportStats:
    stats = ReportStats()
    stats.total_points = len(state.survey_points)
    stats.total_issues = len(state.issues)

    for issue in state.issues:
        if issue.status == IssueStatus.OPEN:
            stats.open_issues += 1
        elif issue.status == IssueStatus.PENDING:
            stats.pending_issues += 1
        elif issue.status == IssueStatus.ACCEPTED:
            stats.accepted_issues += 1
        elif issue.status == IssueStatus.IGNORED:
            stats.ignored_issues += 1

        if issue.issue_type == "missing":
            stats.missing_count += 1
        elif issue.issue_type == "duplicate":
            stats.duplicate_count += 1
        elif issue.issue_type == "name_conflict":
            stats.name_conflict_count += 1
        elif issue.issue_type == "bad_path":
            stats.bad_path_count += 1

    return stats
