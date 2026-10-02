from app.command_parser import collect_param_history, delete_param, parse_command, render_command, update_param


CMD = """source ~/miniconda3/etc/profile.d/conda.sh
conda activate isaacsim5
export CUDA_VISIBLE_DEVICES=0,1,2,3
torchrun --nnodes=1 --nproc_per_node=4 --master_addr=127.0.0.1 --master_port=29703 \\
  scripts/rsl_rl/train.py \\
  --task=Tracking-AthleteRecoveryUpperLowerTracking-G1-v0 \\
  --registry_name=Datasets/g1_all \\
  --num_envs=10000 \\
  --distributed \\
  --headless \\
  --run_name=test_multicritic \\
  --wandb_project=whole_body_tracking \\
  --disable_auto_eval
"""


def test_parse_past_torchrun_command_shapes():
    parsed = parse_command(CMD)
    assert [line.role for line in parsed.lines] == ["cmd:source", "cmd:conda", "env", "cmd:torchrun"]
    env = parsed.lines[2].params[0]
    assert (env.kind, env.key, env.value) == ("env", "CUDA_VISIBLE_DEVICES", "0,1,2,3")
    torch = parsed.lines[3]
    assert any(p.key == "--task" and p.value.startswith("Tracking-Athlete") for p in torch.params)
    assert any(p.key == "--distributed" and p.kind == "flag" for p in torch.params)


def test_update_and_delete_keep_renderable_command():
    parsed = parse_command(CMD)
    update_param(parsed, 3, "--num_envs", "6000")
    delete_param(parsed, 3, "--disable_auto_eval")
    rendered = render_command(parsed)
    assert "--num_envs=6000" in rendered
    assert "--disable_auto_eval" not in rendered


def test_history_is_grouped_by_role_kind_key():
    h = collect_param_history([CMD, CMD.replace("0,1,2,3", "4,5,6,7").replace("29703", "29701")])
    assert h["env|env|CUDA_VISIBLE_DEVICES"] == ["0,1,2,3", "4,5,6,7"]
    assert h["cmd:torchrun|option|--master_port"] == ["29703", "29701"]


def test_replace_param_can_edit_key_and_value_in_place():
    from app.command_parser import replace_param

    parsed = parse_command("torchrun --master_port=29703 scripts/train.py --headless")
    replace_param(parsed, 0, "--master_port", "--master_port", "29701", "option")
    replace_param(parsed, 0, "--headless", "--disable_auto_eval", "", "flag")
    rendered = render_command(parsed)
    assert "--master_port=29701" in rendered
    assert "--disable_auto_eval" in rendered


def test_replace_line_tokens_edits_non_parameter_line():
    from app.command_parser import replace_line_tokens

    parsed = parse_command("conda activate old")
    replace_line_tokens(parsed, 0, "conda", "activate isaacsim5")
    assert render_command(parsed) == "conda activate isaacsim5"


def test_parse_does_not_repeat_command_word_as_parameter():
    parsed = parse_command("torchrun --master_port=29703 scripts/train.py --headless")
    params = parsed.lines[0].params
    assert not any(p.kind == "positional" and p.value == "torchrun" for p in params)
    assert any(p.kind == "positional" and p.value == "scripts/train.py" for p in params)
    assert any(p.key == "--master_port" and p.value == "29703" for p in params)
