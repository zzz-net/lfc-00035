"""配置管理"""
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any


CONFIG_FILENAME = "survey_config.json"
STATE_FILENAME = "survey_state.json"
STATE_DIRNAME = ".survey_check"


@dataclass
class SurveyConfig:
    """调查资料包配置"""
    config_version: str = "1.0"
    manifest_path: str = ""
    photo_dir: str = ""
    track_dir: str = ""
    table_dir: str = ""
    photo_exts: list = field(default_factory=lambda: [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff"])
    track_exts: list = field(default_factory=lambda: [".gpx", ".kml", ".kmz", ".shp", ".geojson"])
    table_exts: list = field(default_factory=lambda: [".csv", ".xlsx", ".xls"])
    point_id_column: str = "point_id"
    name_column: str = "name"
    photo_pattern: str = "{point_id}"
    track_pattern: str = "{point_id}"
    table_pattern: str = "{point_id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SurveyConfig":
        config = cls()
        for key, value in data.items():
            if hasattr(config, key):
                setattr(config, key, value)
        return config


def find_workspace(start_path: str = None) -> Optional[str]:
    """查找工作区根目录（包含 .survey_check 目录的目录）"""
    if start_path is None:
        start_path = os.getcwd()

    current = os.path.abspath(start_path)
    while True:
        state_dir = os.path.join(current, STATE_DIRNAME)
        if os.path.isdir(state_dir):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def get_config_path(workspace: str) -> str:
    return os.path.join(workspace, STATE_DIRNAME, CONFIG_FILENAME)


def get_state_path(workspace: str) -> str:
    return os.path.join(workspace, STATE_DIRNAME, STATE_FILENAME)


def load_config(workspace: str) -> SurveyConfig:
    config_path = get_config_path(workspace)
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return SurveyConfig.from_dict(data)


def save_config(workspace: str, config: SurveyConfig) -> None:
    config_path = get_config_path(workspace)
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)


def init_workspace(workspace: str, config: SurveyConfig) -> None:
    """初始化工作区"""
    state_dir = os.path.join(workspace, STATE_DIRNAME)
    os.makedirs(state_dir, exist_ok=True)
    save_config(workspace, config)
