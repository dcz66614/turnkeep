import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
CFG = ROOT / "adapters" / "claude-code" / ".mcp.json"

def test_claude_config_parses_and_binds_scope():
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    srv = cfg["mcpServers"]["turnkeep"]
    assert srv["type"] == "stdio"
    assert srv["command"] and srv["args"]
    assert "--scope" in srv["args"] and "claude-code" in srv["args"]
    assert "--db" in srv["args"]
    assert srv["env"]["TURNKEEP_PRINCIPAL"] == "claude-code"

def test_claude_config_has_placeholder_note():
    readme = (ROOT / "adapters" / "claude-code" / "README.md").read_text()
    assert "<turnkeep-work>" in readme
