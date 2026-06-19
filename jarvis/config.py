"""
Configuration loader for Yennefer.
Supports environment variable expansion in YAML config.
"""

import os
import re
from pathlib import Path
import yaml


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith('#'):
                continue
            # Parse KEY=value
            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                # Remove quotes if present
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                os.environ.setdefault(key, value)


def load_dotenv():
    """Load project and local user env files if they exist."""
    project_env = Path(__file__).parent.parent / ".env"
    user_env_dir = Path.home() / ".yennefer"

    for env_path in (
        project_env,
        user_env_dir / "shortcuts.env",
        user_env_dir / "secrets.env",
    ):
        _load_env_file(env_path)


def expand_env_vars(obj):
    """Recursively expand ${VAR} patterns in config values."""
    if isinstance(obj, dict):
        return {k: expand_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [expand_env_vars(item) for item in obj]
    elif isinstance(obj, str):
        # Match ${VAR} or $VAR patterns
        pattern = r'\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)'
        
        def replace_var(match):
            var_name = match.group(1) or match.group(2)
            return os.environ.get(var_name, match.group(0))
        
        return re.sub(pattern, replace_var, obj)
    else:
        return obj


def load_config(config_path: str = None) -> dict:
    """Load configuration from YAML file with environment variable expansion.
    
    Supports ${VAR} syntax in YAML values, loaded from:
    1. System environment variables
    2. .env file in project root
    """
    # Load .env file first
    load_dotenv()
    
    if config_path is None:
        config_path = Path(__file__).parent.parent / "config" / "jarvis.yaml"
    
    config_file = Path(config_path)
    
    if not config_file.exists():
        return {}
    
    with open(config_file, 'r') as f:
        config = yaml.safe_load(f) or {}
    
    # Expand environment variables
    return expand_env_vars(config)
