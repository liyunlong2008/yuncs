"""配置加载单测：secrets.toml 的 [okx] 段必须正确映射（曾因嵌套 bug 全空）。"""
from app.config import load_config

CONFIG = """
[challenge]
initial_balance = 20
[exchange]
mode = "paper"
proxy = "http://127.0.0.1:10808"
"""

SECRETS = f"""
[okx]
api_key = "{'k' * 36}"
api_secret = "{'s' * 32}"
passphrase = "{'p' * 10}"
"""


def test_load_config_with_secrets(tmp_path):
    cfg_file = tmp_path / "config.toml"
    sec_file = tmp_path / "secrets.toml"
    cfg_file.write_text(CONFIG, encoding="utf-8")
    sec_file.write_text(SECRETS, encoding="utf-8")
    cfg = load_config(str(cfg_file), str(sec_file))
    assert cfg.challenge.initial_balance == 20
    assert cfg.secrets.api_key == "k" * 36
    assert cfg.secrets.api_secret == "s" * 32
    assert cfg.secrets.passphrase == "p" * 10


def test_load_config_without_secrets(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(CONFIG, encoding="utf-8")
    cfg = load_config(str(cfg_file), str(tmp_path / "secrets.toml"))
    assert cfg.secrets.api_key == ""
