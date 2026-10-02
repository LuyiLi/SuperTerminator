"""Developer-owned V1 Newton configuration. No remote paths in the daily form."""
from __future__ import annotations

from copy import deepcopy
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tomllib
from urllib.parse import urlparse

from app.config import ROOT_DIR

DEFAULT_CONFIG = ROOT_DIR / 'docs' / 'sync-launch.toml'


def load_sync_config(path: str | Path | None = None) -> dict:
    config_path = Path(path or os.environ.get('SUPERTERMINATOR_SYNC_CONFIG', DEFAULT_CONFIG))
    with config_path.open('rb') as stream:
        config = tomllib.load(stream)
    if config.get('version') != 1 or config.get('environment') != 'newton':
        raise ValueError('同步配置仅支持 version=1、environment=newton')
    if not config.get('sources') or not config.get('include'):
        raise ValueError('开发者须登记本地来源和同步范围')
    for source in config['sources']:
        if not Path(source).is_absolute():
            raise ValueError('代码来源必须为完整绝对路径')
    experiments = config.get('experiments', [])
    if not experiments or len({e['id'] for e in experiments}) != len(experiments):
        raise ValueError('实验 ID 必须非空且唯一')
    for experiment in experiments:
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', experiment['id']):
            raise ValueError('实验 ID 只允许字母、数字、下划线和连字符')
        if experiment.get('entrypoint') != 'scripts/rsl_rl/train.py':
            raise ValueError('首版仅支持固定 scripts/rsl_rl/train.py 入口')
        if not experiment.get('task') or not experiment.get('dataset'):
            raise ValueError('实验必须指定 task 和登记的数据路径名称')
        normalize_parameters(experiment, {})
    wandb = config.get('wandb', {})
    url = urlparse(wandb.get('base_url', ''))
    if url.scheme not in {'http', 'https'} or not url.netloc or url.username or url.password:
        raise ValueError('登记有效的 W&B 服务 URL；凭据必须留在远端')
    if not wandb.get('entity') or not wandb.get('project'):
        raise ValueError('必须登记 W&B entity/project')
    for alias, machine in config.get('machines', {}).items():
        root = PurePosixPath(machine.get('managed_root', ''))
        if not root.is_absolute() or '..' in root.parts or len(root.parts) < 4:
            raise ValueError(f'{alias}: 必须配置专用的绝对托管目录')
        if 'isaacsim5' in str(root).lower():
            raise ValueError('Newton 环境不能安装到 isaacsim5')
        if machine.get('uv_path') and not PurePosixPath(machine['uv_path']).is_absolute():
            raise ValueError(f'{alias}: uv_path 必须为已有 uv 的绝对路径')
        for name, value in machine.get('external_paths', {}).items():
            if not PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts:
                raise ValueError(f'{alias}: 外部路径 {name} 必须为绝对路径')
        for experiment in experiments:
            if experiment['dataset'] not in machine.get('external_paths', {}):
                raise ValueError(f'{alias}: 缺少数据路径 {experiment["dataset"]}')
    return config


def registered_sources(config: dict) -> list[str]:
    """Only explicitly registered repositories and their actual Git worktrees."""
    paths = set()
    for raw in config['sources']:
        path = Path(raw).expanduser().resolve()
        paths.add(str(path))
        if config.get('discover_worktrees', True) and path.is_dir():
            result = subprocess.run(['git', '-C', str(path), 'worktree', 'list', '--porcelain', '-z'],
                                    capture_output=True, timeout=10, check=False,
                                    env={**{k: v for k, v in os.environ.items() if not k.startswith('GIT_')},
                                         'GIT_OPTIONAL_LOCKS': '0'})
            if result.returncode == 0:
                for field in result.stdout.decode('utf-8').split('\0'):
                    if field.startswith('worktree '):
                        paths.add(str(Path(field[9:]).resolve()))
    return sorted(paths)


def normalize_parameters(experiment: dict, parameters: dict) -> dict[str, int]:
    limits = {'num_envs': (1, 1_000_000), 'max_iterations': (1, 10_000_000),
              'seed': (0, 2**31 - 1)}
    if set(parameters) - set(limits):
        raise ValueError('首版高级参数仅支持每卡环境数、迭代次数和 seed')
    values = {**experiment.get('defaults', {}), **parameters}
    if set(values) - set(limits):
        raise ValueError('实验默认参数包含不支持的配置项')
    for name, (low, high) in limits.items():
        value = values.get(name)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or int(value) != value):
            raise ValueError(f'{name} 必须为整数')
        if not low <= value <= high:
            raise ValueError(f'{name} 必须在 {low}–{high} 之间')
        values[name] = int(value)
    return values


def machine_profile(config: dict, alias: str, experiment: dict) -> dict:
    if alias not in config.get('machines', {}):
        raise ValueError(f'{alias} 未接入同步启动')
    profile = deepcopy(config['machines'][alias])
    profile.update(wandb=deepcopy(config['wandb']), environment='newton', python_version='3.12',
                   import_module='whole_body_tracking', entrypoint=experiment['entrypoint'],
                   task=experiment['task'], min_free_bytes=int(config.get('min_free_bytes', 30 * 1024**3)))
    return profile
