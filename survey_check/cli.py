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
    validate_snapshot_file, read_ops_log, _append_ops_log,
    ImportReport, ImportConflict,
    PREFLIGHT_PROCEED, PREFLIGHT_CONFIRM, PREFLIGHT_ABORT,
    CATEGORY_CONFIG_MISSING, CATEGORY_RESIDUAL_STATE,
    CATEGORY_VERSION_MISMATCH, CATEGORY_TARGET_HAS_DATA,
    CATEGORY_SNAPSHOT_INVALID, CATEGORY_TARGET_INVALID,
    CATEGORY_CONTENT_CONFLICT,
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
    click.echo(f"  配置版本: {info.config_version}")
    click.echo(f"  状态版本: {info.state_version}")
    click.echo(f"  问题数量: {info.issue_count}")
    click.echo(f"  复核历史: {info.history_count} 条")
    click.echo(f"  撤销栈: {info.undo_stack_count} 步")
    click.echo(f"  调查点: {info.survey_points_count} 个")
    click.echo(f"  内容校验和: {info.content_hash}")


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
        _print_import_report(report)
        if report.has_errors:
            sys.exit(1)
        return

    preflight_report = preflight_import(workspace, snapshot_path)
    _print_preflight_summary(preflight_report)

    conclusion = preflight_report.preflight_conclusion

    if conclusion == PREFLIGHT_ABORT:
        _append_ops_log(workspace, {
            "op": "import",
            "timestamp": datetime.now().isoformat(),
            "import_id": preflight_report.import_id,
            "snapshot_path": snapshot_path,
            "phase": "aborted_preflight",
            "preflight_conclusion": preflight_report.preflight_conclusion,
            "result": "failure",
            "failure_phase": "preflight",
            "failure_reason": "aborted_by_preflight",
            "strategy": strategy,
            "include_config": include_config,
            "conflict_count": len(preflight_report.conflicts),
        })
        click.echo("")
        click.echo("=" * 60)
        click.echo("[中止] 预检结论为【必须中止】，导入未执行")
        click.echo("请根据上述冲突提示处理后重试，或使用 --dry-run 重新检查")
        click.echo("=" * 60)
        _print_import_report(preflight_report)
        sys.exit(1)

    if conclusion == PREFLIGHT_CONFIRM and not yes:
        click.echo("")
        click.echo("=" * 60)
        click.echo("[需确认] 预检结论为【需确认】，存在以下风险:")
        warning_conflicts = [c for c in preflight_report.conflicts if c.severity == "warning"]
        for c in warning_conflicts:
            click.echo(f"  ! [{c.conflict_type}] {c.message}")
        click.echo("")
        click.echo("策略说明:")
        click.echo(f"  冲突处理: {strategy}")
        click.echo(f"  导入配置: {'是' if include_config else '否（保留目标配置）'}")
        click.echo("")
        if not click.confirm("确认继续执行导入? 此操作将修改工作区状态，已自动生成备份"):
            _append_ops_log(workspace, {
                "op": "import",
                "timestamp": datetime.now().isoformat(),
                "import_id": preflight_report.import_id,
                "snapshot_path": snapshot_path,
                "phase": "cancelled_by_user",
                "preflight_conclusion": preflight_report.preflight_conclusion,
                "result": "cancelled",
                "strategy": strategy,
                "include_config": include_config,
                "conflict_count": len(preflight_report.conflicts),
            })
            click.echo("已取消导入，工作区未做任何修改")
            sys.exit(0)

    click.echo("")
    click.echo("开始执行导入...")
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
    click.echo(f"配置版本: {info.config_version}")
    click.echo(f"状态版本: {info.state_version}")
    click.echo(f"问题总数: {info.issue_count or len(state.issues)}")
    click.echo(f"复核历史: {info.history_count or len(state.review_history)} 条")
    click.echo(f"撤销栈: {info.undo_stack_count or len(state.undo_stack)} 步")
    click.echo(f"调查点: {info.survey_points_count or len(state.survey_points)} 个")
    if info.content_hash:
        click.echo(f"内容校验和: {info.content_hash}")
    click.echo("")
    click.echo(f"创建时间: {state.created_at}")
    click.echo(f"最后扫描: {state.last_scan_time or '未扫描'}")


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


CATEGORY_LABELS = {
    CATEGORY_CONFIG_MISSING: "配置缺失类",
    CATEGORY_RESIDUAL_STATE: "残留状态类",
    CATEGORY_VERSION_MISMATCH: "版本不匹配类",
    CATEGORY_TARGET_HAS_DATA: "目标已有数据类",
    CATEGORY_SNAPSHOT_INVALID: "快照无效类",
    CATEGORY_TARGET_INVALID: "目标目录无效类",
    CATEGORY_CONTENT_CONFLICT: "内容冲突类",
    "uncategorized": "未分类",
}

PREFLIGHT_CONCLUSION_LABELS = {
    PREFLIGHT_PROCEED: ("可继续", "[OK]", "绿色"),
    PREFLIGHT_CONFIRM: ("需确认", "[!]", "黄色"),
    PREFLIGHT_ABORT: ("必须中止", "[X]", "红色"),
}


def _print_preflight_summary(report: ImportReport) -> None:
    click.echo("=" * 60)
    click.echo("【预检阶段】结构化检查结果")
    click.echo("=" * 60)

    conclusion = report.preflight_conclusion
    label, icon, _ = PREFLIGHT_CONCLUSION_LABELS.get(
        conclusion, ("未知", "?", "无色")
    )
    click.echo(f"预检结论: {icon} 【{label}】 ({conclusion})")
    click.echo(f"导入ID: {report.import_id}")
    click.echo(f"检查阶段: {report.phase}")

    if report.conflict_summary:
        click.echo("")
        click.echo("冲突分类汇总:")
        for cat, count in sorted(report.conflict_summary.items()):
            label = CATEGORY_LABELS.get(cat, cat)
            click.echo(f"  - {label}: {count} 项")

    errors = [c for c in report.conflicts if c.severity == "error"]
    warnings = [c for c in report.conflicts if c.severity == "warning"]
    infos = [c for c in report.conflicts if c.severity == "info"]

    click.echo("")
    click.echo(f"严重程度: 错误={len(errors)}, 警告={len(warnings)}, 提示={len(infos)}")

    if report.snapshot_info:
        info = report.snapshot_info
        click.echo("")
        click.echo("快照信息:")
        click.echo(f"  版本: {info.snapshot_version}")
        click.echo(f"  导出时间: {info.exported_at}")
        click.echo(f"  来源: {info.source_workspace}")
        if info.note:
            click.echo(f"  备注: {info.note}")
        click.echo(f"  问题数: {info.issue_count}, 历史: {info.history_count} 条")

    if errors:
        click.echo("")
        click.echo("【必须中止的原因】:")
        for c in errors:
            cat_label = CATEGORY_LABELS.get(c.category, c.category or "未分类")
            click.echo(f"  [X] [{cat_label}] {c.conflict_type}: {c.message}")
            if c.hint:
                click.echo(f"     建议: {c.hint}")

    click.echo("")
    click.echo("-" * 60)


def _print_import_report(report: ImportReport) -> None:
    mode = "预检" if report.dry_run else "导入"
    status = "成功" if report.success or report.dry_run else "失败"

    click.echo("")
    click.echo("=" * 60)
    click.echo(f"快照{mode}报告: {status}")
    if report.import_id:
        click.echo(f"导入ID: {report.import_id}")
    if report.phase:
        click.echo(f"阶段: {report.phase}")
    if report.preflight_conclusion:
        label, icon, _ = PREFLIGHT_CONCLUSION_LABELS.get(
            report.preflight_conclusion, ("未知", "?", "无色")
        )
        click.echo(f"预检结论: {icon} 【{label}】")
    click.echo("=" * 60)

    if report.snapshot_info:
        info = report.snapshot_info
        click.echo(f"快照版本: {info.snapshot_version}")
        click.echo(f"导出时间: {info.exported_at}")
        click.echo(f"来源: {info.source_workspace}")
        if info.note:
            click.echo(f"备注: {info.note}")
        if info.content_hash:
            click.echo(f"校验和: {info.content_hash}")
        click.echo("")

    if report.conflict_summary:
        click.echo("冲突分类汇总:")
        for cat, count in sorted(report.conflict_summary.items()):
            label = CATEGORY_LABELS.get(cat, cat)
            click.echo(f"  - {label}: {count} 项")
        click.echo("")

    if report.conflicts:
        click.echo(f"冲突/警告: {len(report.conflicts)} 项")
        for c in report.conflicts:
            icon = "X" if c.severity == "error" else ("!" if c.severity == "warning" else "i")
            cat_label = ""
            if c.category:
                cat_label = f"[{CATEGORY_LABELS.get(c.category, c.category)}] "
            click.echo(f"  [{icon}] {cat_label}[{c.conflict_type}] {c.message}")
            if c.hint:
                click.echo(f"       建议: {c.hint}")
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

    if report.has_errors and not report.dry_run:
        click.echo("")
        click.echo("=" * 60)
        click.echo("[诊断分析] 按原因类别区分如下：")
        click.echo("=" * 60)
        error_conflicts = [c for c in report.conflicts if c.severity == "error"]
        warning_conflicts = [c for c in report.conflicts if c.severity == "warning"]

        snapshot_errors = [c for c in error_conflicts if c.category == CATEGORY_SNAPSHOT_INVALID]
        version_errors = [c for c in error_conflicts if c.category == CATEGORY_VERSION_MISMATCH]
        residual_errors = [c for c in error_conflicts if c.category == CATEGORY_RESIDUAL_STATE]
        target_errors = [c for c in error_conflicts if c.category == CATEGORY_TARGET_INVALID]
        config_warnings = [c for c in warning_conflicts
                           if c.category == CATEGORY_CONTENT_CONFLICT and c.conflict_type == "config_mismatch"]
        import_failed = [c for c in error_conflicts if c.conflict_type == "import_failed"]

        if snapshot_errors or version_errors:
            click.echo("")
            click.echo("【类别一】导出包问题（快照本身损坏或不兼容）")
            click.echo("-" * 40)
            for c in snapshot_errors:
                click.echo(f"  - [{c.conflict_type}] {c.message}")
            for c in version_errors:
                click.echo(f"  - [{c.conflict_type}] {c.message}")
            click.echo("  建议: 请回到源工作区重新执行 'survey-check export'，")
            click.echo("        或确认传输过程中文件未被篡改/截断。")

        if residual_errors:
            click.echo("")
            click.echo("【类别二】目录残留问题（目标目录有不完整的旧状态）")
            click.echo("-" * 40)
            for c in residual_errors:
                click.echo(f"  - [{c.conflict_type}] {c.message}")
            click.echo("  建议: 删除目标目录下的 .survey_check/ 文件夹后重试，")
            click.echo("        或先在目标目录执行 'survey-check init' 完成初始化。")

        if target_errors:
            click.echo("")
            click.echo("【类别二b】目标目录访问问题（无法写入或路径无效）")
            click.echo("-" * 40)
            for c in target_errors:
                click.echo(f"  - [{c.conflict_type}] {c.message}")
            click.echo("  建议: 检查目标路径是否正确、当前用户是否有写入权限。")

        if config_warnings:
            click.echo("")
            click.echo("【类别三】本地配置不一致（目标配置与快照配置有差异）")
            click.echo("-" * 40)
            for c in config_warnings:
                click.echo(f"  - [{c.conflict_type}] {c.message}")
                if "diffs" in c.details:
                    for field, snap_val, target_val in c.details["diffs"][:5]:
                        click.echo(f"      * {field}: 快照={snap_val!r} vs 目标={target_val!r}")
            click.echo("  建议: 如需使用快照中的配置，请追加 --include-config 参数；")
            click.echo("        否则保留当前目标配置（路径字段会自动重映射至目标工作区）。")

        if import_failed:
            click.echo("")
            click.echo("【执行异常】导入执行阶段出错，已自动从备份回滚")
            click.echo("-" * 40)
            for c in import_failed:
                click.echo(f"  - 错误: {c.message}")
            click.echo("  建议: 检查工作区文件完整性后重试；如需回滚到导入前状态，")
            click.echo("        可使用 'survey-check backup-restore <备份名>'。")

        if not any([snapshot_errors, version_errors, residual_errors,
                    target_errors, config_warnings, import_failed]):
            click.echo("  未识别到具体类别，请查看上方冲突列表逐项处理。")

    if not report.dry_run and report.success and not report.has_errors:
        click.echo("")
        click.echo("[完成] 快照导入成功，可继续使用以下命令复核:")
        click.echo("  survey-check status    查看工作区状态")
        click.echo("  survey-check list      列出全部问题")
        click.echo("  survey-check report    生成复核报告")
        click.echo("  survey-check review    复核问题")


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


@cli.command("ops-log")
@click.option("--op", "-o", "op_filter", default=None,
              type=click.Choice(["export", "import", "import_dry_run", "backup_restore"]),
              help="按操作类型筛选")
@click.option("--limit", "-n", default=20, help="显示最近 N 条记录")
@click.pass_context
def ops_log_cmd(ctx, op_filter, limit):
    """查看导出/导入操作日志"""
    workspace = _get_workspace(ctx)
    entries = read_ops_log(workspace, op_filter=op_filter, limit=limit)

    if not entries:
        click.echo("暂无操作日志")
        return

    click.echo(f"操作日志（最近 {len(entries)} 条）:")
    click.echo("-" * 60)
    for entry in entries:
        op = entry.get("op", "?")
        ts = entry.get("timestamp", "?")
        result = entry.get("result", "?")
        result_icon = "OK" if result == "success" else "FAIL"
        click.echo(f"[{result_icon}] {op} @ {ts}")

        if op == "export":
            click.echo(f"     输出: {entry.get('output_path', '?')}")
            click.echo(f"     问题数: {entry.get('issue_count', 0)}, 历史: {entry.get('history_count', 0)}")
            click.echo(f"     校验和: {entry.get('content_hash', '?')}")
        elif op == "import":
            click.echo(f"     快照: {entry.get('snapshot_path', '?')}")
            click.echo(f"     策略: {entry.get('strategy', '?')}")
            if result == "success":
                click.echo(f"     新增: {entry.get('issues_imported', 0)}, "
                           f"跳过: {entry.get('issues_skipped', 0)}, "
                           f"覆盖: {entry.get('issues_overwritten', 0)}")
                click.echo(f"     历史导入: {entry.get('history_imported', 0)} 条")
                if entry.get('backup_path'):
                    click.echo(f"     备份: {entry['backup_path']}")
            else:
                click.echo(f"     失败阶段: {entry.get('failure_phase', '?')}")
                click.echo(f"     失败原因: {entry.get('failure_reason', '?')}")
                if entry.get('rolled_back'):
                    click.echo(f"     已回退: 是")
        elif op == "import_dry_run":
            click.echo(f"     快照: {entry.get('snapshot_path', '?')}")
            click.echo(f"     冲突: {entry.get('conflicts_count', 0)}, "
                       f"警告: {entry.get('warnings_count', 0)}")
        elif op == "backup_restore":
            click.echo(f"     备份: {entry.get('backup_path', '?')}")
        click.echo("")


def main():
    cli()


if __name__ == "__main__":
    main()
