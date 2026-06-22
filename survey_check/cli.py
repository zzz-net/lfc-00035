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
    compute_stats, add_issue, generate_issue_id,
)
from .manifest import parse_manifest
from .scanner import scan_all
from .detector import detect_issues
from .reporter import generate_text_report, generate_csv_report
from .models import IssueStatus, ScanResult, FileEntry, IssueType
from .snapshot import (
    export_snapshot, import_snapshot, load_snapshot, preflight_import,
    list_backups, restore_from_backup, get_backup_path,
    ImportReport, ImportConflict,
)


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

    existing_issues_by_key = {}
    for old_issue in state.issues:
        key = (old_issue.issue_type, old_issue.point_id or "", old_issue.description)
        existing_issues_by_key[key] = old_issue

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

    raw_issues = detect_issues(state, config, scan_result, scan_errors)

    now = datetime.now().isoformat()
    final_issues = []
    reused_count = 0
    new_count = 0

    for raw in raw_issues:
        key = (raw.issue_type, raw.point_id or "", raw.description)
        old = existing_issues_by_key.get(key)
        if old is not None:
            final_issues.append(old)
            reused_count += 1
        else:
            raw.id = generate_issue_id(state)
            raw.created_at = now
            raw.updated_at = now
            final_issues.append(raw)
            new_count += 1

    state.issues = final_issues
    save_state(workspace, state)

    stats = compute_stats(state)

    click.echo("")
    click.echo(f"扫描完成！共发现 {stats.total_issues} 个问题")
    click.echo(f"  待处理: {stats.open_issues}")
    click.echo(f"  待补充: {stats.pending_issues}")
    click.echo(f"  已接受: {stats.accepted_issues}")
    click.echo(f"  已忽略: {stats.ignored_issues}")

    if reused_count:
        click.echo(f"  复用历史问题: {reused_count} 条")
    if new_count:
        click.echo(f"  新增问题: {new_count} 条")


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


@cli.command("export")
@click.argument("output_path", required=False, default="survey_snapshot.json")
@click.option("--note", "-n", default="", help="快照备注信息")
@click.pass_context
def export_snapshot_cmd(ctx, output_path, note):
    """导出工作区复核快照"""
    workspace = _get_workspace(ctx)

    info = export_snapshot(workspace, output_path, note=note)

    click.echo(f"[OK] 快照已导出: {os.path.abspath(output_path)}")
    click.echo(f"  快照版本: {info.snapshot_version}")
    click.echo(f"  导出时间: {info.exported_at}")
    click.echo(f"  来源工作区: {info.source_workspace}")
    if info.note:
        click.echo(f"  备注: {info.note}")


@cli.command("import")
@click.argument("snapshot_path")
@click.option("--strategy", "-s", default="skip",
              type=click.Choice(["skip", "overwrite", "renumber", "merge"]),
              help="冲突处理策略: skip(跳过)/overwrite(覆盖)/renumber(重编号)/merge(智能合并)")
@click.option("--include-config", "-c", is_flag=True, help="同时导入配置")
@click.option("--dry-run", "-d", is_flag=True, help="预检模式，只检测不实际导入")
@click.option("--yes", "-y", is_flag=True, help="跳过确认直接执行")
@click.pass_context
def import_snapshot_cmd(ctx, snapshot_path, strategy, include_config, dry_run, yes):
    """导入复核快照到工作区"""
    workspace = _get_workspace(ctx)

    if dry_run:
        report = preflight_import(workspace, snapshot_path)
    else:
        report = import_snapshot(workspace, snapshot_path,
                                strategy=strategy,
                                include_config=include_config,
                                dry_run=False)

    _print_import_report(report)

    if report.has_errors:
        sys.exit(1)


@cli.command("snapshot-info")
@click.argument("snapshot_path")
@click.pass_context
def snapshot_info_cmd(ctx, snapshot_path):
    """查看快照文件信息"""
    if not os.path.exists(snapshot_path):
        click.echo(f"错误: 快照文件不存在: {snapshot_path}", err=True)
        sys.exit(1)

    try:
        info, config, state = load_snapshot(snapshot_path)
    except Exception as e:
        click.echo(f"错误: 无法解析快照: {e}", err=True)
        sys.exit(1)

    click.echo(f"快照文件: {snapshot_path}")
    click.echo(f"快照版本: {info.snapshot_version}")
    click.echo(f"导出时间: {info.exported_at}")
    click.echo(f"来源工作区: {info.source_workspace}")
    if info.note:
        click.echo(f"备注: {info.note}")
    click.echo("")
    click.echo(f"配置版本: {config.config_version}")
    click.echo(f"状态版本: {state.state_version}")
    click.echo(f"创建时间: {state.created_at}")
    click.echo(f"最后扫描: {state.last_scan_time or '未扫描'}")
    click.echo(f"调查点数量: {len(state.survey_points)}")
    click.echo(f"问题总数: {len(state.issues)}")
    click.echo(f"复核历史: {len(state.review_history)} 条")
    click.echo(f"可撤销步数: {len(state.undo_stack)}")


@cli.command("backup-list")
@click.pass_context
def backup_list_cmd(ctx):
    """列出导入备份"""
    workspace = _get_workspace(ctx)
    backups = list_backups(workspace)

    if not backups:
        click.echo("暂无导入备份")
        return

    click.echo(f"共 {len(backups)} 个导入备份（最新在前）:")
    for i, name in enumerate(backups, 1):
        path = get_backup_path(workspace, name)
        click.echo(f"  {i}. {name}")
        click.echo(f"     路径: {path}")


@cli.command("backup-restore")
@click.argument("backup_name")
@click.option("--yes", "-y", is_flag=True, help="跳过确认直接恢复")
@click.pass_context
def backup_restore_cmd(ctx, backup_name, yes):
    """从导入备份恢复"""
    workspace = _get_workspace(ctx)
    backup_path = get_backup_path(workspace, backup_name)

    if not os.path.isdir(backup_path):
        click.echo(f"错误: 备份不存在: {backup_name}", err=True)
        sys.exit(1)

    if not yes:
        click.echo(f"将从备份 '{backup_name}' 恢复工作区状态")
        click.echo("警告: 当前状态将被覆盖！")
        click.confirm("确认继续?", abort=True)

    success = restore_from_backup(workspace, backup_path)
    if success:
        click.echo(f"[OK] 已从备份 {backup_name} 恢复")
    else:
        click.echo(f"错误: 恢复失败", err=True)
        sys.exit(1)


def _print_import_report(report: ImportReport) -> None:
    """打印导入报告"""
    mode = "预检" if report.dry_run else "导入"
    status = "成功" if report.success or report.dry_run else "失败"

    click.echo("=" * 60)
    click.echo(f"快照{mode}报告: {status}")
    click.echo("=" * 60)

    if report.snapshot_info:
        info = report.snapshot_info
        click.echo(f"快照版本: {info.snapshot_version}")
        click.echo(f"导出时间: {info.exported_at}")
        click.echo(f"来源: {info.source_workspace}")
        if info.note:
            click.echo(f"备注: {info.note}")
        click.echo("")

    if report.conflicts:
        click.echo(f"冲突/警告: {len(report.conflicts)} 项")
        for c in report.conflicts:
            icon = "X" if c.severity == "error" else ("!" if c.severity == "warning" else "i")
            click.echo(f"  [{icon}] [{c.conflict_type}] {c.message}")
            if c.conflict_type == "config_mismatch" and "diffs" in c.details:
                for field, snap_val, target_val in c.details["diffs"][:5]:
                    click.echo(f"      - {field}:")
                    click.echo(f"        快照: {snap_val}")
                    click.echo(f"        目标: {target_val}")
                if len(c.details["diffs"]) > 5:
                    click.echo(f"      ... 等 {len(c.details['diffs'])} 处差异")
            if c.conflict_type == "issue_id_conflict" and "conflicting_ids" in c.details:
                ids = c.details["conflicting_ids"]
                click.echo(f"      冲突编号: {', '.join(ids[:10])}")
                if len(ids) > 10:
                    click.echo(f"      ... 共 {len(ids)} 个")
        click.echo("")

    click.echo("统计:")
    click.echo(f"  新增问题: {report.issues_imported}")
    click.echo(f"  跳过问题: {report.issues_skipped}")
    click.echo(f"  覆盖问题: {report.issues_overwritten}")
    click.echo(f"  重编号问题: {report.issues_renumbered}")
    click.echo(f"  导入历史记录: {report.history_imported}")

    if report.config_updated:
        click.echo(f"  配置已更新")

    if report.backup_path:
        click.echo("")
        click.echo(f"备份已保存至: {report.backup_path}")
        click.echo(f"如需回退，可运行: survey-check backup-restore <备份名>")


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
