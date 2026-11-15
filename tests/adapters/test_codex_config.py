import tomllib
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
CFG = ROOT / "adapters" / "codex" / "config.toml"

def test_codex_config_parses_and_binds_scope():
    cfg = tomllib.loads(CFG.read_text(encoding="utf-8"))
    srv = cfg["mcp_servers"]["turnkeep"]
    assert srv["command"] and srv["args"]
    assert "--scope" in srv["args"] and "codex" in srv["args"]
    assert "--db" in srv["args"]
    assert srv["env"]["TURNKEEP_PRINCIPAL"] == "codex"

def test_codex_config_has_placeholder_note():
    assert "<turnkeep-work>" in CFG.read_text(encoding="utf-8")
