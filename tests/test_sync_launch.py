import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.db import init_db, session_scope
from app.models import Project, ProjectServer, Run, Server
from app.sync_launch import (build_sync_command, get_sync_options, refresh_sync_run,
                             stop_sync_run, sync_and_launch)
from app.sync_remote import SyncRemoteError


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / 'worktree'
    (source / 'scripts/rsl_rl').mkdir(parents=True)
    (source / 'scripts/rsl_rl/train.py').write_text('print("saved")')
    (source / 'pyproject.toml').write_text('[project]\nname="example"\nversion="1.0"\n')
    (source / 'uv.lock').write_text('version = 1\n')
    database = create_engine(f'sqlite:///{tmp_path}/app.db')
    init_db(database)
    with session_scope(database) as session:
        project = Project(name='whole body tracking')
        server = Server(alias='lhy_aliyun_5090')
        session.add_all([project, server])
        session.flush()
        session.add(ProjectServer(project_id=project.id, server_id=server.id))
        project_id, server_id = project.id, server.id
    config = {'version': 1, 'environment': 'newton', 'project_name': 'whole body tracking',
              'sources': [str(source)], 'discover_worktrees': False,
              'include': ['scripts', 'pyproject.toml', 'uv.lock'], 'exclude': [],
              'wandb': {'base_url': 'https://wandb.example', 'entity': 'team', 'project': 'train'},
              'machines': {'lhy_aliyun_5090': {'managed_root': '/srv/managed/whole_body_tracking',
                  'external_paths': {'dataset': '/data/motions'}}},
              'experiments': [{'id': 'athlete', 'name': 'Athlete',
                  'entrypoint': 'scripts/rsl_rl/train.py', 'task': 'Tracking-v0', 'dataset': 'dataset',
                  'defaults': {'num_envs': 100, 'seed': 42, 'max_iterations': 10}}]}
    return {'project_id': project_id, 'server_id': server_id, 'experiment_id': 'athlete',
            'source_path': str(source), 'gpu_ids': [0], 'parameters': {},
            'request_key': 'request_1234567890', 'target_engine': database,
            'config': config, 'cache_root': tmp_path / 'cache', 'gpu_loader': free_gpus}


async def free_gpus(server):
    return {'online': True, 'gpu_metrics_available': True,
            'gpus': [{'index': 0, 'occupancy': 'free'}, {'index': 1, 'occupancy': 'free'}]}


class Remote:
    def __init__(self):
        self.prepares = 0
        self.launches = 0
        self.inspections = 0
        self.result = {'state': 'starting', 'claimed': True, 'process_alive': True,
                       'status_detail': 'waiting for training output'}
        self.error = None
        self.prepare_error = None
        self.copied = None

    async def prepare(self, alias, snapshot, profile, progress=None):
        self.prepares += 1
        self.copied = (snapshot.path / 'scripts/rsl_rl/train.py').read_text()
        if self.prepare_error:
            raise self.prepare_error
        await progress({'stage': 'environment'})
        await asyncio.sleep(0.01)
        return {'workdir': f'{profile["managed_root"]}/versions/{snapshot.fingerprint}',
                'environment_reused': self.prepares > 1}

    async def launch(self, *args):
        self.launches += 1
        if self.error:
            raise self.error
        return dict(self.result)

    async def inspect(self, *args):
        self.inspections += 1
        return dict(self.result)

    async def stop(self, *args):
        self.result = {'state': 'stopped', 'claimed': True, 'process_alive': False}
        return dict(self.result)


async def test_persisted_request_double_click_and_retry_are_single_launch(setup):
    remote = Remote()
    first, second = await asyncio.gather(sync_and_launch(**setup, remote=remote),
                                         sync_and_launch(**setup, remote=remote))
    retry = await sync_and_launch(**setup, remote=remote)
    assert first['run_id'] == second['run_id'] == retry['run_id']
    assert remote.launches == 1 and remote.prepares == 1
    assert retry['state'] == 'starting'  # SSH success alone does not mean training.
    assert retry['sync_metadata']['fingerprint']
    assert retry['sync_metadata']['parameters'] == {'num_envs': 100, 'seed': 42, 'max_iterations': 10}
    with session_scope(setup['target_engine']) as session:
        assert session.query(Run).count() == 1


async def test_saved_files_frozen_and_identical_code_shares_remote_directory(setup):
    remote = Remote()
    source = Path(setup['source_path']) / 'scripts/rsl_rl/train.py'
    async def progress(event):
        if event['stage'] == 'sync':
            source.write_text('print("next training")')
    first = await sync_and_launch(**setup, remote=remote, progress=progress)
    assert remote.copied == 'print("saved")'
    second = await sync_and_launch(**{**setup, 'request_key': 'second_1234567890'}, remote=remote)
    assert first['workdir'] != second['workdir']
    third = await sync_and_launch(**{**setup, 'request_key': 'third_1234567890'}, remote=remote)
    assert second['workdir'] == third['workdir']
    assert second['sync_metadata']['log_path'] != third['sync_metadata']['log_path']


async def test_retry_survives_source_deleted_and_config_changed(setup):
    remote = Remote()
    original = await sync_and_launch(**setup, remote=remote)
    import shutil
    shutil.rmtree(setup['source_path'])
    again = await sync_and_launch(**{**setup, 'config': {'invalid': True}}, remote=remote)
    assert original['run_id'] == again['run_id']
    assert remote.launches == 1
    with pytest.raises(ValueError, match='绑定'):
        await sync_and_launch(**{**setup, 'parameters': {'seed': 43}}, remote=remote)


async def test_prepare_failure_never_launches_and_reports_exact_stage(setup):
    remote = Remote()
    remote.prepare_error = SyncRemoteError('environment', 'uv locked failed')
    result = await sync_and_launch(**setup, remote=remote)
    assert result['state'] == 'failed'
    assert '准备环境' in result['status_detail'] and 'uv locked failed' in result['status_detail']
    assert remote.launches == 0


async def test_gpu_conflict_fails_before_upload(setup):
    async def occupied(server):
        return {'online': True, 'gpu_metrics_available': True, 'gpus': [{'index': 0, 'occupancy': 'busy'}]}
    remote = Remote()
    result = await sync_and_launch(**{**setup, 'gpu_loader': occupied}, remote=remote)
    assert result['state'] == 'failed' and 'GPU 0' in result['status_detail']
    assert remote.prepares == remote.launches == 0


async def test_remote_final_gpu_rejection_is_actionable_not_unknown(setup):
    remote = Remote()
    remote.error = SyncRemoteError('launch', 'GPU 被任务 21 预留', definitive=True)
    remote.result = {'state': 'unknown', 'claimed': False}
    result = await sync_and_launch(**setup, remote=remote)
    assert result['state'] == 'failed'
    assert '21' in result['status_detail']
    assert remote.inspections == 1
    retry = await sync_and_launch(**setup, remote=remote)
    assert retry['state'] == 'failed' and '21' in retry['status_detail']
    assert remote.inspections == 1


async def test_lost_ack_queries_original_task_never_relaunches(setup):
    remote = Remote()
    remote.error = ConnectionError('lost SSH acknowledgement')
    remote.result = {'state': 'running', 'claimed': True, 'process_alive': True,
                     'wandb_url': 'https://wandb.example/team/train/runs/abc'}
    result = await sync_and_launch(**setup, remote=remote)
    assert result['state'] == 'running'
    assert result['sync_metadata']['wandb_url'].endswith('/runs/abc')
    again = await sync_and_launch(**setup, remote=remote)
    assert again['run_id'] == result['run_id'] and remote.launches == 1


async def test_unknown_launch_is_not_retried(setup):
    remote = Remote()
    remote.error = TimeoutError('SSH timeout')
    remote.result = {'state': 'unknown', 'claimed': False, 'status_detail': '状态待确认'}
    result = await sync_and_launch(**setup, remote=remote)
    assert result['state'] == 'unknown'
    again = await sync_and_launch(**setup, remote=remote)
    assert again['state'] == 'unknown' and remote.launches == 1


async def test_broken_browser_progress_cannot_abort_training(setup):
    async def progress(event):
        raise RuntimeError('browser disconnected')
    remote = Remote()
    result = await sync_and_launch(**setup, remote=remote, progress=progress)
    assert result['state'] == 'starting' and remote.launches == 1


async def test_cancelling_browser_wait_does_not_cancel_launch(setup):
    entered, release = asyncio.Event(), asyncio.Event()
    remote = Remote()
    original = remote.prepare
    async def prepare(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    remote.prepare = prepare
    task = asyncio.create_task(sync_and_launch(**setup, remote=remote))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    for _ in range(100):
        if remote.launches:
            break
        await asyncio.sleep(0.01)
    assert remote.launches == 1


async def test_task_stop_and_refresh_keep_provenance(setup):
    remote = Remote()
    result = await sync_and_launch(**setup, remote=remote)
    stopped = await stop_sync_run(result['run_id'], target_engine=setup['target_engine'], remote=remote)
    assert stopped['state'] == 'stopped'
    assert stopped['sync_metadata']['fingerprint'] == result['sync_metadata']['fingerprint']
    refreshed = await refresh_sync_run(result['run_id'], target_engine=setup['target_engine'], remote=remote)
    assert refreshed['state'] == 'stopped'


def test_command_is_fixed_newton_wandb_and_per_gpu(setup):
    from app.sync_config import machine_profile
    config = setup['config']
    experiment = config['experiments'][0]
    command, name = build_sync_command(experiment=experiment,
        profile=machine_profile(config, 'lhy_aliyun_5090', experiment), parameters=experiment['defaults'],
        gpu_ids=[2, 3], run_id=24, workdir='/srv/managed/versions/hash')
    assert 'uv run --locked --no-sync python -m torch.distributed.run --standalone --nnodes=1 --nproc_per_node=2' in command
    assert '--num_envs=100' in command and '--seed=42' in command
    assert 'export CUDA_VISIBLE_DEVICES=2,3' in command
    assert 'export WANDB_MODE=online' in command and '--no_wandb' not in command
    assert name == 'athlete-task-24'


def test_options_never_guess_between_sources(setup):
    config = setup['config']
    config['sources'].append(str(Path(setup['source_path']).parent / 'missing-tree'))
    options = get_sync_options(setup['project_id'], target_engine=setup['target_engine'], config=config)
    assert options['enabled'] and options['selected_source'] is None
    assert len(options['sources']) == 2
    bound = get_sync_options(setup['project_id'], target_engine=setup['target_engine'], config=config,
                             source_path=setup['source_path'])
    assert bound['selected_source'] == setup['source_path']


def test_migration_is_additive_and_idempotency_unique(tmp_path):
    database = create_engine(f'sqlite:///{tmp_path}/legacy.db')
    with database.begin() as connection:
        connection.execute(text('CREATE TABLE runs (id INTEGER PRIMARY KEY, name TEXT)'))
        connection.execute(text("INSERT INTO runs VALUES (1, 'historical')"))
    init_db(database)
    init_db(database)
    with database.begin() as connection:
        row = connection.execute(text('SELECT name,request_key,sync_stage,sync_metadata FROM runs WHERE id=1')).one()
        assert row == ('historical', None, '', '{}')
        connection.execute(text("INSERT INTO runs (id,name,request_key) VALUES (2,'a','unique')"))
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO runs (id,name,request_key) VALUES (3,'b','unique')"))


async def test_interrupted_preparation_becomes_actionable_without_starting(setup):
    remote = Remote()
    result = await sync_and_launch(**setup, remote=remote)
    with session_scope(setup['target_engine']) as session:
        run = session.get(Run, result['run_id'])
        run.status = 'preparing'
        run.sync_metadata = {**run.sync_metadata, 'launch_attempted': False, 'preparer': '999999999:0'}
    retry = await sync_and_launch(**setup, remote=remote)
    assert retry['state'] == 'failed' and '服务退出' in retry['status_detail']
    assert remote.launches == 1 and remote.inspections == 0


@pytest.mark.parametrize('value', [float('inf'), float('nan'), True, -1, '100'])
def test_invalid_advanced_parameters_fail_before_registration(setup, value):
    from app.sync_config import normalize_parameters
    with pytest.raises(ValueError):
        normalize_parameters(setup['config']['experiments'][0], {'num_envs': value})


@pytest.mark.parametrize('via_list', [False, True])
async def test_delayed_running_observation_cannot_undo_stop(setup, monkeypatch, via_list):
    from app.run_status import observe_run_records
    remote = Remote()
    result = await sync_and_launch(**setup, remote=remote)
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_inspect(*args):
        entered.set()
        await release.wait()
        return {'state': 'running', 'process_alive': True, 'live_verified': True}
    remote.inspect = delayed_inspect
    if via_list:
        monkeypatch.setattr('app.sync_remote.SyncRemote', lambda *args: remote)
        pending = asyncio.create_task(observe_run_records([result], target_engine=setup['target_engine']))
    else:
        pending = asyncio.create_task(refresh_sync_run(result['run_id'], target_engine=setup['target_engine'], remote=remote))
    await entered.wait()
    stopped = await stop_sync_run(result['run_id'], target_engine=setup['target_engine'], remote=remote)
    assert stopped['state'] == 'stopped'
    release.set()
    observed = await pending
    if via_list:
        observed = observed[0]
        assert observed['session_alive'] is False
    assert observed['state'] == 'stopped'
    assert observed['ended_at'] == stopped['ended_at']


def test_fixed_uv_executable_used_by_training(setup):
    from app.sync_config import machine_profile
    config = setup['config']
    experiment = config['experiments'][0]
    profile = machine_profile(config, 'lhy_aliyun_5090', experiment)
    profile['uv_path'] = '/opt/already installed/uv'
    command, _ = build_sync_command(experiment=experiment, profile=profile,
        parameters=experiment['defaults'], gpu_ids=[0], run_id=25, workdir='/srv/snapshot')
    assert "exec '/opt/already installed/uv' run --locked --no-sync python" in command
