"""CLI 主入口"""
import os
import sys
import click
from datetime import datetime

from .config import (
    SurveyConfig, find_workspace, load_config, save_config,
    init_workspace, get_config_path, get_state_path, STATE_DIRNAME,
)
from .state import (
    WorkspaceState, load_state, save_state,
    get_issue_by_id, update_issue_status,
    push_undo_batch, undo_last_batch, can_undo,
    compute_stats, add_issue,
)
from .manifest import parse_manifest
from .scanner import scan_all
from .detector import detect_issues
from .reporter import generate_text_report, generate_csv_report
from .models import IssueStatus, ScanResult, FileEntry, IssueType


def _get_workspace(ctx) -> str:
    """获取工作区路径"""
    workspace = ctx.obj.get("workspace") if ctx.obj else None
    if workspace:
        return workspace
    workspace = find_workspace()
    if not workspace:
        click.echo("错误: 未找到工作区，请先在当前目录或上级目录运行 'survey-check init'", err=True)
        sys.exit(1)
    return workspace


def _load_all(workspace: str):
    """加载配置和状态"""
    config = load_config(workspace)
    state = load_state(workspace)
    return config, state


@click.group()
@click.option("--workspace", "-w", help="工作区目录路径")
@click.pass_context
def cli(ctx, workspace):
    """外业调查资料包核对工具"""
    ctx.ensure_object(dict)
    if workspace:
        ctx.obj["workspace"] = os.path.abspath(workspace)


@cli.command()
@click.option("--manifest", "-m", required=True, help="调查清单文件路径 (CSV/Excel)")
@click.option("--photo-dir", "-p", default="photos", help="照片目录路径")
@click.option("--track-dir", "-t", default="tracks", help="轨迹目录路径")
@click.option("--table-dir", "-b", default="tables", help="表格目录路径")
@click.option("--force", "-f", is_flag=True, help="强制重新初始化（覆盖现有配置）")
@click.pass_context
def init(ctx, manifest, photo_dir, track_dir, table_dir, force):
    """初始化资料包工作区"""
    workspace = ctx.obj.get("workspace") or os.getcwd()
    workspace = os.path.abspath(workspace)

    state_dir = os.path.join(workspace, STATE_DIRNAME)
    if os.path.exists(state_dir) and not force:
        click.echo(f"错误: 工作区已存在于 {workspace}")
        click.echo("使用 --force 强制重新初始化")
        sys.exit(1)

    config = SurveyConfig(
        manifest_path=os.path.abspath(manifest),
        photo_dir=os.path.abspath(photo_dir),
        track_dir=os.path.abspath(track_dir),
        table_dir=os.path.abspath(table_dir),
    )

    init_workspace(workspace, config)

    state = load_state(workspace)
    state.config_version = config.config_version

    points, errors = parse_manifest(config, config.manifest_path)
    if errors:
        click.echo("解析清单时出现警告:")
        for err in errors:
            click.echo(f"  - {err}")

    state.survey_points = points
    save_state(workspace, state)

    click.echo(f"[OK] 工作区初始化完成: {workspace}")
    click.echo(f"  清单文件: {config.manifest_path}")
    click.echo(f"  照片目录: {config.photo_dir}")
    click.echo(f"  轨迹目录: {config.track_dir}")
    click.echo(f"  表格目录: {config.table_dir}")
    click.echo(f"  调查点数量: {len(points)}")


@cli.command()
@click.pass_context
def scan(ctx):
    """扫描目录并检测问题"""
    workspace = _get_workspace(ctx)
    config, state = _load_all(workspace)

    existing_issues = {i.id: i for i in state.issues} if state.issues else {}

    points, errors = parse_manifest(config, config.manifest_path)
    if errors:
        click.echo("解析清单时出现警告:")
        for err in errors:
            click.echo(f"  - {err}")
    state.survey_points = points

    click.echo("扫描中...")

    scan_result, scan_errors = scan_all(config)

    photos = scan_result.get("photos", [])
    tracks = scan_result.get("tracks", [])
    tables = scan_result.get("tables", [])

    click.echo(f"  照片: {len(photos)} 个文件")
    click.echo(f"  轨迹: {len(tracks)} 个文件")
    click.echo(f"  表格: {len(tables)} 个文件")

    if scan_errors:
        click.echo(f"  扫描错误: {len(scan_errors)} 个")
        for err in scan_errors:
            click.echo(f"    - {err}")

    state.scan_result = ScanResult(
        photos=photos,
        tracks=tracks,
        tables=tables,
        scan_time=datetime.now().isoformat(),
    )
    state.last_scan_time = datetime.now().isoformat()

    new_issues = detect_issues(state, config, scan_result, scan_errors)

    preserved_count = 0
    for new_issue in new_issues:
        key = (new_issue.issue_type, new_issue.point_id or "", new_issue.description)
        matched = None
        for old_id, old_issue in existing_issues.items():
            old_key = (old_issue.issue_type, old_issue.point_id or "", old_issue.description)
            if old_key == key:
                matched = old_issue
                break

        if matched:
            new_issue.id = matched.id
            new_issue.status = matched.status
            new_issue.remark = matched.remark
            new_issue.created_at = matched.created_at
            preserved_count += 1

    state.issues = new_issues
    save_state(workspace, state)

    stats = compute_stats(state)

    click.echo("")
    click.echo(f"扫描完成！共发现 {stats.total_issues} 个问题")
    click.echo(f"  待处理: {stats.open_issues}")
    click.echo(f"  待补充: {stats.pending_issues}")
    click.echo(f"  已接受: {stats.accepted_issues}")
    click.echo(f"  已忽略: {stats.ignored_issues}")

    if preserved_count:
        click.echo(f"  保留历史状态: {preserved_count} 条")


@cli.command("list")
@click.option("--status", "-s", help="按状态筛选: open/pending/accepted/ignored")
@click.option("--type", "-t", "issue_type", help="按类型筛选: missing/duplicate/name_conflict/bad_path")
@click.pass_context
def list_issues(ctx, status, issue_type):
    """列出所有问题"""
    workspace = _get_workspace(ctx)
    _, state = _load_all(workspace)

    if not state.issues:
        click.echo("暂无问题记录，请先运行 'survey-check scan'")
        return

    issues = state.issues

    if status:
        status_map = {
            "open": IssueStatus.OPEN,
            "pending": IssueStatus.PENDING,
            "accepted": IssueStatus.ACCEPTED,
            "ignored": IssueStatus.IGNORED,
        }
        if status not in status_map:
            click.echo(f"错误: 无效的状态 '{status}'", err=True)
            sys.exit(1)
        issues = [i for i in issues if i.status == status_map[status]]

    if issue_type:
        type_map = {
            "missing": IssueType.MISSING,
            "duplicate": IssueType.DUPLICATE,
            "name_conflict": IssueType.NAME_CONFLICT,
            "bad_path": IssueType.BAD_PATH,
        }
        if issue_type not in type_map:
            click.echo(f"错误: 无效的类型 '{issue_type}'", err=True)
            sys.exit(1)
        issues = [i for i in issues if i.issue_type == type_map[issue_type]]

    if not issues:
        click.echo("没有符合条件的问题")
        return

    click.echo(f"共 {len(issues)} 个问题:")
    click.echo("")

    for issue in issues:
        status_label = _status_label(issue.status)
        type_label = _type_label(issue.issue_type)
        click.echo(f"[{issue.id}] {status_label} | {type_label}")
        click.echo(f"  {issue.description}")
        if issue.point_id:
            click.echo(f"  调查点: {issue.point_id}")
        if issue.file_paths:
            click.echo(f"  文件: {', '.join(os.path.basename(f) for f in issue.file_paths[:3])}")
            if len(issue.file_paths) > 3:
                click.echo(f"        等 {len(issue.file_paths)} 个文件")
        if issue.remark:
            click.echo(f"  备注: {issue.remark}")
        click.echo("")


@cli.command()
@click.argument("issue_ids", nargs=-1)
@click.option("--status", "-s", required=True,
              type=click.Choice(["pending", "accepted", "ignored", "open"]),
              help="设置状态")
@click.option("--remark", "-r", default="", help="备注信息")
@click.pass_context
def review(ctx, issue_ids, status, remark):
    """复核问题，设置状态和备注"""
    workspace = _get_workspace(ctx)
    _, state = _load_all(workspace)

    if not issue_ids:
        click.echo("错误: 请指定至少一个问题编号", err=True)
        sys.exit(1)

    status_map = {
        "open": IssueStatus.OPEN,
        "pending": IssueStatus.PENDING,
        "accepted": IssueStatus.ACCEPTED,
        "ignored": IssueStatus.IGNORED,
    }
    new_status = status_map[status]

    actions = []
    not_found = []

    for issue_id in issue_ids:
        issue = get_issue_by_id(state, issue_id)
        if not issue:
            not_found.append(issue_id)
            continue

        action = update_issue_status(state, issue_id, new_status, remark)
        if action:
            actions.append(action)

    if not_found:
        click.echo(f"警告: 未找到以下问题: {', '.join(not_found)}")

    if actions:
        push_undo_batch(state, actions)
        save_state(workspace, state)

        click.echo(f"已更新 {len(actions)} 个问题的状态为 {_status_label(new_status)}")
        for action in actions:
            click.echo(f"  - {action.issue_id}")
    else:
        click.echo("没有问题被更新")


@cli.command()
@click.pass_context
def undo(ctx):
    """撤销上一步复核操作"""
    workspace = _get_workspace(ctx)
    _, state = _load_all(workspace)

    if not can_undo(state):
        click.echo("没有可撤销的操作")
        return

    undone = undo_last_batch(state)
    save_state(workspace, state)

    click.echo(f"已撤销 {len(undone)} 个复核操作:")
    for action in undone:
        click.echo(f"  - {action.issue_id}: {_status_label(action.new_status)} → {_status_label(action.old_status)}")


@cli.command()
@click.option("--output", "-o", default="survey_report.txt", help="输出文件路径")
@click.option("--format", "-f", "fmt", default="text",
              type=click.Choice(["text", "csv"]),
              help="报告格式")
@click.pass_context
def report(ctx, output, fmt):
    """生成核对报告"""
    workspace = _get_workspace(ctx)
    _, state = _load_all(workspace)

    if not state.issues:
        click.echo("暂无问题记录，请先运行 'survey-check scan'")
        return

    output = os.path.abspath(output)

    if fmt == "csv":
        generate_csv_report(state, output)
    else:
        generate_text_report(state, output)

    stats = compute_stats(state)
    click.echo(f"[OK] 报告已生成: {output}")
    click.echo(f"  调查点总数: {stats.total_points}")
    click.echo(f"  问题总数: {stats.total_issues}")
    click.echo(f"    待处理: {stats.open_issues}")
    click.echo(f"    待补充: {stats.pending_issues}")
    click.echo(f"    已接受: {stats.accepted_issues}")
    click.echo(f"    已忽略: {stats.ignored_issues}")


@cli.command()
@click.pass_context
def status(ctx):
    """显示工作区状态"""
    workspace = _get_workspace(ctx)
    config, state = _load_all(workspace)

    click.echo(f"工作区: {workspace}")
    click.echo(f"配置版本: {config.config_version}")
    click.echo(f"状态版本: {state.state_version}")
    click.echo(f"创建时间: {state.created_at}")
    click.echo(f"最后扫描: {state.last_scan_time or '未扫描'}")
    click.echo(f"调查点数量: {len(state.survey_points)}")
    click.echo("")

    if state.scan_result:
        click.echo(f"照片文件: {len(state.scan_result.photos)}")
        click.echo(f"轨迹文件: {len(state.scan_result.tracks)}")
        click.echo(f"表格文件: {len(state.scan_result.tables)}")
        click.echo("")

    stats = compute_stats(state)
    click.echo(f"问题总数: {stats.total_issues}")
    click.echo(f"  待处理: {stats.open_issues}")
    click.echo(f"  待补充: {stats.pending_issues}")
    click.echo(f"  已接受: {stats.accepted_issues}")
    click.echo(f"  已忽略: {stats.ignored_issues}")
    click.echo("")
    click.echo(f"复核操作数: {len(state.review_history)}")
    click.echo(f"可撤销步数: {len(state.undo_stack)}")


def _status_label(status):
    labels = {
        IssueStatus.OPEN: "待处理",
        IssueStatus.PENDING: "待补充",
        IssueStatus.ACCEPTED: "已接受",
        IssueStatus.IGNORED: "已忽略",
    }
    return labels.get(status, str(status))


def _type_label(issue_type):
    labels = {
        IssueType.MISSING: "缺失",
        IssueType.DUPLICATE: "重复",
        IssueType.NAME_CONFLICT: "命名冲突",
        IssueType.BAD_PATH: "路径错误",
    }
    return labels.get(issue_type, str(issue_type))


def main():
    cli()


if __name__ == "__main__":
    main()
