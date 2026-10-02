"""One persisted request, one saved-file snapshot, at most one remote launch."""
from __future__ import annotations

import asyncio
from datetime import datetime
import inspect
import os
from pathlib import Path
import re
import shlex

from sqlalchemy import Engine, text

from app.code_snapshot import SnapshotError, freeze_snapshot, source_metadata
from app.config import ROOT_DIR
from app.db import engine, session_scope
from app.machine_status import collect_machine_gpu_status
from app.models import Project, ProjectServer, Run, Server, Template
from app.runs import make_tmux_session_name
from app.sync_config import (load_sync_config, machine_profile, normalize_parameters,
                             registered_sources)

STAGES = {'snapshot': '准备代码', 'sync': '同步', 'environment': '准备环境',
          'launch': '检查并启动', 'training': '训练中'}
_TASKS: set[asyncio.Task] = set()


def _preparer_identity(pid: int) -> str | None:
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return f'{pid}:{fields[19]}' if fields[0] != 'Z' else None
    except (OSError, IndexError):
        return None


def preparation_interrupted(record: dict) -> bool:
    metadata = record.get('sync_metadata', {})
    owner = metadata.get('preparer')
    if record.get('status') != 'preparing' or not owner or metadata.get('launch_attempted'):
        return False
    try:
        return _preparer_identity(int(owner.split(':', 1)[0])) != owner
    except (ValueError, AttributeError):
        return False


def get_sync_options(project_id: int, *, target_engine: Engine = engine,
                     source_path: str | None = None, config: dict | None = None) -> dict:
    try:
        config = config or load_sync_config()
        with session_scope(target_engine) as session:
            project = session.get(Project, project_id)
            if project is None or project.name != config['project_name']:
                return {'enabled': False, 'error': '本项目未接入 Newton 同步启动'}
            servers = [{'id': s.id, 'alias': s.alias} for s in session.query(Server).join(
                ProjectServer, Server.id == ProjectServer.server_id).filter(
                    ProjectServer.project_id == project_id, ProjectServer.enabled.is_(True),
                    Server.enabled.is_(True)).all() if s.alias in config['machines']]
        sources = []
        for path in registered_sources(config):
            try:
                metadata = source_metadata(path)
                sources.append({'path': path, **metadata})
            except (ValueError, OSError, SnapshotError) as exc:
                sources.append({'path': path, 'branch': '', 'commit': '', 'error': str(exc)})
        selected = None
        if source_path:
            exact = str(Path(source_path).expanduser().resolve())
            if exact not in {s['path'] for s in sources}:
                raise ValueError('传入的 worktree 尚未登记；请先由开发者接入，不能猜测来源')
            selected = exact
        elif len(sources) == 1:
            selected = sources[0]['path']
        return {'enabled': True, 'sources': sources, 'selected_source': selected,
                'servers': servers, 'experiments': config['experiments'], 'problems': []}
    except (ValueError, OSError, KeyError) as exc:
        return {'enabled': False, 'error': str(exc)}


def _record(run_id: int, target_engine: Engine) -> dict:
    from app.run_status import get_run_record
    record = get_run_record(run_id, target_engine=target_engine)
    if record is None:
        raise ValueError(f'任务不存在: {run_id}')
    return record


def _update(run_id: int, target_engine: Engine, *, metadata: dict | None = None, **fields) -> None:
    with session_scope(target_engine) as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f'任务不存在: {run_id}')
        for key, value in fields.items():
            setattr(run, key, value)
        if metadata:
            run.sync_metadata = {**(run.sync_metadata or {}), **metadata}


def build_sync_command(*, experiment: dict, profile: dict, parameters: dict,
                       gpu_ids: list[int], run_id: int, workdir: str) -> tuple[str, str]:
    """Build fixed argv from validated values; never reuse historical shell snippets."""
    name = f'{experiment["id"]}-task-{run_id}'
    log_dir = f'{workdir}/logs/task-{run_id}'
    argv = [profile.get('uv_path', 'uv'), 'run', '--locked', '--no-sync', 'python']
    if len(gpu_ids) > 1:
        argv += ['-m', 'torch.distributed.run', '--standalone', '--nnodes=1',
                 f'--nproc_per_node={len(gpu_ids)}']
    argv += [experiment['entrypoint'], f'--task={experiment["task"]}',
             f'--registry_name={profile["external_paths"][experiment["dataset"]]}',
             f'--num_envs={parameters["num_envs"]}', f'--seed={parameters["seed"]}',
             f'--max_iterations={parameters["max_iterations"]}', '--headless',
             f'--experiment_name={experiment["id"]}', f'--run_name={name}',
             f'--wandb_project={profile["wandb"]["project"]}', '--disable_auto_eval']
    if len(gpu_ids) > 1:
        argv.append('--distributed')
    env = {'CUDA_VISIBLE_DEVICES': ','.join(map(str, gpu_ids)), 'PYTHONUNBUFFERED': '1',
           'PYTHONDONTWRITEBYTECODE': '1', 'OMP_NUM_THREADS': '8', 'OPENBLAS_NUM_THREADS': '1',
           'MKL_NUM_THREADS': '1', 'UV_PROJECT_ENVIRONMENT': f'{workdir}/.venv',
           'WANDB_MODE': 'online', 'WANDB_BASE_URL': profile['wandb']['base_url'],
           'WANDB_ENTITY': profile['wandb']['entity'], 'WANDB_PROJECT': profile['wandb']['project'],
           'WANDB_DIR': log_dir, 'MPLCONFIGDIR': f'{log_dir}/mplconfig',
           'OMNI_KIT_ACCEPT_EULA': 'YES'}
    command = ('set -eu\nunset PYTHONPATH PYTHONHOME VIRTUAL_ENV WANDB_RUN_ID WANDB_DISABLED '
               'WANDB_CONFIG_PATHS WANDB_SWEEP_ID\n')
    command += '\n'.join(f'export {key}={shlex.quote(value)}' for key, value in env.items())
    command += f'\nmkdir -p {shlex.quote(log_dir)}\nexec {shlex.join(argv)}'
    return command, name


async def sync_and_launch(*, project_id: int, server_id: int, experiment_id: str,
                          source_path: str, gpu_ids: list[int], parameters: dict,
                          request_key: str, target_engine: Engine = engine,
                          progress=None, config: dict | None = None, remote=None,
                          gpu_loader=None, cache_root: Path | None = None) -> dict:
    """Keep work alive across browser disconnects; never restart a persisted request."""
    task = asyncio.create_task(_sync_and_launch(
        project_id=project_id, server_id=server_id, experiment_id=experiment_id,
        source_path=source_path, gpu_ids=gpu_ids, parameters=parameters,
        request_key=request_key, target_engine=target_engine, progress=progress,
        config=config, remote=remote, gpu_loader=gpu_loader, cache_root=cache_root))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return await asyncio.shield(task)


async def _sync_and_launch(*, project_id, server_id, experiment_id, source_path,
                           gpu_ids, parameters, request_key, target_engine,
                           progress, config, remote, gpu_loader, cache_root) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', str(request_key)):
        raise ValueError('请求 ID 无效；请从同步启动表单创建任务')
    if not source_path:
        raise ValueError('请选择本次使用的本地代码目录 / worktree')
    # This identity uses the submitted intent. Existing requests do not depend on a
    # still-existing worktree or a config which may have changed after freezing.
    intent = {'project_id': project_id, 'server_id': server_id, 'experiment_id': experiment_id,
              'source_path': str(Path(source_path).expanduser().absolute()),
              'gpu_ids': gpu_ids, 'parameters': parameters}
    with session_scope(target_engine) as session:
        existing = session.query(Run).filter_by(request_key=request_key).one_or_none()
        if existing:
            if existing.sync_metadata.get('intent') != intent:
                raise ValueError('此请求已绑定另一个任务；点击“新训练”后才能更换参数')
            existing_id = existing.id
        else:
            existing_id = None
    if existing_id is not None:
        return await refresh_sync_run(existing_id, target_engine=target_engine, remote=remote)
    config = config or load_sync_config()
    source = str(Path(source_path).expanduser().resolve())
    if source not in registered_sources(config):
        raise ValueError('代码来源未登记；不能猜测 worktree')
    experiment = next((e for e in config['experiments'] if e['id'] == experiment_id), None)
    if experiment is None:
        raise ValueError('实验模板不存在')
    params = normalize_parameters(experiment, parameters)
    if not gpu_ids or any(type(i) is not int or i < 0 for i in gpu_ids) or len(set(gpu_ids)) != len(gpu_ids):
        raise ValueError('请选择不重复的有效 GPU 编号')
    # Serialize only the short local registration transaction, never SSH or uv.
    with session_scope(target_engine) as session:
        session.execute(text('BEGIN IMMEDIATE'))
        existing = session.query(Run).filter_by(request_key=request_key).one_or_none()
        if existing:
            if existing.sync_metadata.get('intent') != intent:
                raise ValueError('请求 ID 已绑定另一组参数')
            existing_id = existing.id
        else:
            project = session.get(Project, project_id)
            server = session.get(Server, server_id)
            link = session.query(ProjectServer).filter_by(project_id=project_id,
                server_id=server_id, enabled=True).one_or_none()
            if project is None or project.name != config['project_name'] or not server or not server.enabled or not link:
                raise ValueError('该项目/机器未启用同步启动')
            profile = machine_profile(config, server.alias, experiment)
            template = session.query(Template).filter_by(project_id=project_id,
                name='Newton sync launch V1').one_or_none()
            if template is None:
                template = Template(project_id=project_id, name='Newton sync launch V1',
                                    command_template='<fixed Newton entrypoint>', variables_schema=[])
                session.add(template)
                session.flush()
            run = Run(project_id=project_id, server_id=server_id, template_id=template.id,
                      workdir='', name=experiment['name'], rendered_command='', status='preparing',
                      launch_source='sync', request_key=request_key, sync_stage='snapshot',
                      sync_metadata={'intent': intent, 'source_path': source, 'gpu_ids': gpu_ids,
                          'parameters': params, 'total_envs': params['num_envs'] * len(gpu_ids),
                          'experiment_id': experiment_id, 'profile': profile, 'launch_attempted': False,
                          'preparer': _preparer_identity(os.getpid())},
                      status_source='sync_launch')
            session.add(run)
            session.flush()
            run_id, alias = run.id, server.alias
            run.tmux_session = make_tmux_session_name(run.id)
            session_name = run.tmux_session
    if existing_id is not None:
        return await refresh_sync_run(existing_id, target_engine=target_engine, remote=remote)
    if remote is None:
        from app.sync_remote import SyncRemote
        remote = SyncRemote()
    stage = 'snapshot'

    async def report(event: dict) -> None:
        nonlocal stage
        stage = event.get('stage', stage)
        _update(run_id, target_engine, sync_stage=stage)
        if progress:
            try:
                response = progress({**event, 'run_id': run_id})
                if inspect.isawaitable(response):
                    await response
            except Exception:
                pass  # A lost browser is not a failed training request.

    attempted = False
    try:
        await report({'stage': 'snapshot'})
        snapshot = await asyncio.to_thread(freeze_snapshot, source,
            cache_root or ROOT_DIR / 'data' / 'code-snapshots', include=tuple(config['include']),
            exclude=tuple(config.get('exclude', [])), max_file_bytes=config.get('max_file_bytes', 32 * 1024**2))
        if not (snapshot.path / experiment['entrypoint']).is_file():
            raise ValueError('快照缺少实验入口；检查同步范围和所选 worktree')
        _update(run_id, target_engine, metadata={'fingerprint': snapshot.fingerprint,
            'branch': snapshot.branch, 'commit': snapshot.commit, 'source_path': snapshot.source_path,
            'snapshot_bytes': snapshot.total_bytes})
        machine = await (gpu_loader or collect_machine_gpu_status)(
            {'server_id': server_id, 'server_alias': alias})
        _check_gpus(machine, gpu_ids)
        await report({'stage': 'sync'})
        prepared = await remote.prepare(alias, snapshot, profile, progress=report)
        workdir = prepared['workdir']
        command, name = build_sync_command(experiment=experiment, profile=profile, parameters=params,
                                          gpu_ids=gpu_ids, run_id=run_id, workdir=workdir)
        _update(run_id, target_engine, workdir=workdir, rendered_command=command,
                training_run_name=name, metadata={'remote_workdir': workdir, **prepared,
                    'log_path': f'{workdir}/logs/task-{run_id}/output.log'})
        await report({'stage': 'launch'})
        # Commit intent BEFORE crossing the uncertain SSH boundary.
        _update(run_id, target_engine, status='starting', metadata={'launch_attempted': True})
        attempted = True
        result = await remote.launch(alias, run_id, workdir, command, gpu_ids, profile, session_name)
        persist_sync_observation(run_id, result, target_engine)
    except asyncio.CancelledError:
        _update(run_id, target_engine, status='unknown' if attempted else 'failed',
                status_detail=f'{STAGES.get(stage, stage)}中断；先核实原任务，不自动重启')
        raise
    except Exception as exc:
        stage = getattr(exc, 'stage', stage)
        if attempted:
            # SSH may have lost the acknowledgement after the remote claim/start.
            try:
                result = await remote.inspect(alias, run_id, profile)
                from app.sync_remote import SyncRemoteError
                if isinstance(exc, SyncRemoteError) and exc.definitive and result.get('claimed') is False:
                    _update(run_id, target_engine, status='failed', ended_at=datetime.now(),
                        metadata={'launch_rejected': True},
                        status_detail=f'检查并启动失败：{exc}。修复后点击“新训练”。')
                else:
                    persist_sync_observation(run_id, result, target_engine)
            except Exception:
                _update(run_id, target_engine, status='unknown',
                    status_detail=f'状态待确认：{stage}连接中断。请刷新原任务核实，不要重复启动。{exc}')
        else:
            _update(run_id, target_engine, status='failed', ended_at=datetime.now(),
                status_detail=f'{STAGES.get(stage, stage)}失败：{exc}。修复后点击“新训练”。')
    return _record(run_id, target_engine)


def _check_gpus(machine: dict, gpu_ids: list[int]) -> None:
    if not machine.get('online') or not machine.get('gpu_metrics_available'):
        raise ValueError(f'无法检查 GPU：{machine.get("error") or "机器不可用"}')
    by_id = {g['index']: g for g in machine['gpus']}
    for gpu_id in gpu_ids:
        if gpu_id not in by_id or by_id[gpu_id].get('occupancy') != 'free':
            raise ValueError(f'GPU {gpu_id} 不存在或已占用；请选择空闲 GPU')


def persist_sync_observation(run_id: int, result: dict, target_engine: Engine) -> dict:
    state = result.get('state', 'unknown')
    if state not in {'starting', 'running', 'succeeded', 'failed', 'stopped', 'unknown', 'lost'}:
        state = 'unknown'
    terminal = {'succeeded', 'failed', 'stopped'}
    with session_scope(target_engine) as session:
        session.execute(text('BEGIN IMMEDIATE'))
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f'任务不存在: {run_id}')
        # A task ID is never restarted. An older in-flight read must not undo stop/exit.
        if run.status not in terminal or state == run.status:
            run.status = state
            run.status_source = 'sync_remote'
            run.status_detail = result.get('status_detail', '')
            run.last_observed_at = datetime.now()
            if result.get('exit_code') is not None:
                run.exit_code = result['exit_code']
            if state == 'running':
                run.sync_stage = 'training'
            for field in ('started_at', 'ended_at'):
                if result.get(field) and getattr(run, field) is None:
                    try:
                        setattr(run, field, datetime.fromisoformat(str(result[field]).replace('Z', '+00:00')))
                    except ValueError:
                        pass
            if state in terminal and run.ended_at is None:
                run.ended_at = datetime.now()
            run.sync_metadata = {**run.sync_metadata, **{k: result[k] for k in
                ('log_path', 'wandb_url', 'pid', 'process_alive', 'started_at') if result.get(k) is not None}}
    return _record(run_id, target_engine)


async def refresh_sync_run(run_id: int, *, target_engine: Engine = engine, remote=None) -> dict:
    record = _record(run_id, target_engine)
    metadata = record.get('sync_metadata', {})
    if not metadata.get('launch_attempted') or metadata.get('launch_rejected'):
        if preparation_interrupted(record):
            _update(run_id, target_engine, status='failed', ended_at=datetime.now(),
                status_detail='准备过程中服务退出；没有自动启动训练。请检查失败阶段后点击“新训练”。')
            return _record(run_id, target_engine)
        return record
    if remote is None:
        from app.sync_remote import SyncRemote
        remote = SyncRemote()
    try:
        result = await remote.inspect(record['server_alias'], run_id, metadata['profile'])
        persist_sync_observation(run_id, result, target_engine)
    except Exception as exc:
        persist_sync_observation(run_id, {'state': 'unknown',
            'status_detail': f'状态待确认：{exc}；请稍后刷新原任务'}, target_engine)
    return _record(run_id, target_engine)


async def stop_sync_run(run_id: int, *, target_engine: Engine = engine, remote=None) -> dict:
    record = _record(run_id, target_engine)
    if record.get('launch_source') != 'sync':
        raise ValueError('不是同步启动任务')
    metadata = record['sync_metadata']
    if not metadata.get('launch_attempted'):
        raise ValueError('任务尚在准备阶段；请等待准备完成后再停止')
    if remote is None:
        from app.sync_remote import SyncRemote
        remote = SyncRemote()
    result = await remote.stop(record['server_alias'], run_id, metadata['profile'])
    persist_sync_observation(run_id, result, target_engine)
    return _record(run_id, target_engine)
