"""清单文件解析"""
import os
from typing import List, Tuple

from .models import SurveyPoint
from .config import SurveyConfig


def parse_manifest(config: SurveyConfig, manifest_path: str) -> Tuple[List[SurveyPoint], List[str]]:
    """
    解析调查清单文件

    Returns:
        (调查点列表, 错误信息列表)
    """
    errors = []
    points: List[SurveyPoint] = []

    if not os.path.exists(manifest_path):
        errors.append(f"清单文件不存在: {manifest_path}")
        return points, errors

    ext = os.path.splitext(manifest_path)[1].lower()

    try:
        if ext == ".csv":
            points, parse_errors = _parse_csv(config, manifest_path)
            errors.extend(parse_errors)
        elif ext in (".xlsx", ".xls"):
            points, parse_errors = _parse_excel(config, manifest_path)
            errors.extend(parse_errors)
        else:
            errors.append(f"不支持的清单格式: {ext}")
    except Exception as e:
        errors.append(f"解析清单时出错: {str(e)}")

    return points, errors


def _parse_csv(config: SurveyConfig, manifest_path: str) -> Tuple[List[SurveyPoint], List[str]]:
    import pandas as pd

    errors = []
    points = []

    try:
        df = pd.read_csv(manifest_path, dtype=str, keep_default_na=False)
    except Exception as e:
        errors.append(f"读取 CSV 失败: {str(e)}")
        return points, errors

    return _parse_dataframe(config, df, errors)


def _parse_excel(config: SurveyConfig, manifest_path: str) -> Tuple[List[SurveyPoint], List[str]]:
    import pandas as pd

    errors = []
    points = []

    try:
        df = pd.read_excel(manifest_path, dtype=str, keep_default_na=False)
    except Exception as e:
        errors.append(f"读取 Excel 失败: {str(e)}")
        return points, errors

    return _parse_dataframe(config, df, errors)


def _parse_dataframe(config: SurveyConfig, df, errors) -> Tuple[List[SurveyPoint], List[str]]:
    points = []

    df.columns = [str(c).strip() for c in df.columns]

    id_col = config.point_id_column
    name_col = config.name_column

    if id_col not in df.columns:
        errors.append(f"清单中缺少调查点编号列 '{id_col}' 不存在")
        return points, errors

    if name_col not in df.columns:
        errors.append(f"清单中缺少调查点名称列 '{name_col}' 不存在")
        return points, errors

    for idx, row in df.iterrows():
        original_row = idx + 2
        point_id = str(row[id_col]).strip() if row[id_col] else ""

        if not point_id:
            errors.append(f"第 {original_row} 行: 调查点编号为空")
            continue

        name = str(row[name_col]).strip() if row[name_col] else point_id

        attrs = {}
        for col in df.columns:
            if col not in (id_col, name_col):
                val = row[col]
                if val is not None and str(val).strip():
                    attrs[col] = str(val).strip()

        point = SurveyPoint(
            point_id=point_id,
            name=name,
            original_row=original_row,
            attributes=attrs,
        )
        points.append(point)

    seen_ids = set()
    duplicate_ids = set()
    for p in points:
        if p.point_id in seen_ids:
            duplicate_ids.add(p.point_id)
        seen_ids.add(p.point_id)

    if duplicate_ids:
        errors.append(f"清单中存在重复的调查点编号: {', '.join(sorted(duplicate_ids))}")

    return points, errors
