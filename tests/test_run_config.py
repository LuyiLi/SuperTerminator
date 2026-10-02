from __future__ import annotations

from app.run_config import (
    extract_training_config,
    extract_training_run_name,
    set_training_run_name,
    training_display_name,
)


COMMAND = """\
source ~/miniconda3/etc/profile.d/conda.sh
conda activate isaacsim5
export CUDA_VISIBLE_DEVICES=0,1,2,3
torchrun --nproc_per_node=4 scripts/rsl_rl/train.py \\
  --task=Tracking-Flat-G1-v0 \\
  --registry_name Datasets/g1_all \\
  --num_envs=10000 \\
  --max_iterations 30000 \\
  --distributed \\
  --run_name=glimpse_v4 \\
  --wandb_project whole_body_tracking
"""


def test_extract_training_config_from_multiline_torchrun_command():
    config = extract_training_config(COMMAND)

    assert config == {
        "training_run_name": "glimpse_v4",
        "training_run_name_source": "command:--run_name",
        "script": "scripts/rsl_rl/train.py",
        "task": "Tracking-Flat-G1-v0",
        "registry_name": "Datasets/g1_all",
        "num_envs": 10000,
        "max_iterations": 30000,
        "nproc_per_node": 4,
        "nnodes": None,
        "cuda_visible_devices": "0,1,2,3",
        "wandb_project": "whole_body_tracking",
        "experiment_name": None,
        "distributed": True,
    }


def test_extract_training_run_name_supports_spaced_and_quoted_values_and_nul_damage():
    assert extract_training_run_name("python train.py --run_name 'meaningful name'") == (
        "meaningful name"
    )
    assert extract_training_run_name("\x00\x00python train.py --run-name=clean") == "clean"


def test_set_training_run_name_preserves_command_layout_when_replacing():
    changed = set_training_run_name(COMMAND, "adaptive termination v5")

    assert "--run_name='adaptive termination v5'" in changed
    assert "source ~/miniconda3/etc/profile.d/conda.sh" in changed
    assert "--max_iterations 30000 \\" in changed


def test_set_training_run_name_inserts_for_training_command_but_not_arbitrary_shell():
    assert set_training_run_name("python scripts/train.py --task demo", "new") == (
        "python scripts/train.py --task demo --run_name=new"
    )
    assert set_training_run_name("echo hi", "new") == "echo hi"


def test_training_display_name_prefers_config_and_hides_copy_of_fallback():
    assert training_display_name(
        run_id=9,
        panel_name="copy of copy of old",
        command="python train.py --run_name=actual",
    ) == "actual"
    assert training_display_name(
        run_id=9,
        panel_name="copy of old",
        command="echo hi",
    ) == "Run #9"
