"""
Configuration loader for Jarvis.
"""

import os
from pathlib import Path
from typing import Any, Dict

import yaml


def load_config(config_path: str) -> Dict[str, Any]:
    """
    Load configuration from YAML file.
    Supports environment variable substitution with ${VAR_NAME} syntax.
    """
    config_file = Path(config_path)
    
    if not config_file.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    
    with open(config_file, 'r') as f:
        content = f.read()
    
    # Substitute environment variables
    content = _substitute_env_vars(content)
    
    config = yaml.safe_load(content)
    
    # Set defaults
    config = _apply_defaults(config)
    
    return config


def _substitute_env_vars(content: str) -> str:
    """Replace ${VAR_NAME} with environment variable values."""
    import re
    
    pattern = r'\$\{([^}]+)\}'
    
    def replace(match):
        var_name = match.group(1)
        return os.environ.get(var_name, '')
    
    return re.sub(pattern, replace, content)


def _apply_defaults(config: Dict[str, Any]) -> Dict[str, Any]:
    """Apply default values for missing configuration."""
    defaults = {
        'voice_input': {
            'engine': 'whisper',
            'model': 'base.en',
            'language': 'en'
        },
        'voice_output': {
            'engine': 'macos',
            'macos_voice': 'Daniel',
            'rate': 180
        },
        'wake_word': {
            'enabled': False,
            'phrase': 'hey jarvis',
            'sensitivity': 0.5
        },
        'push_to_talk': {
            'enabled': True,
            'hotkey': '<alt>+<space>'
        },
        'claude': {
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 1024,
            'temperature': 0.7
        },
        'ui': {
            'enabled': True,
            'position': 'top-right',
            'opacity': 0.9,
            'always_on_top': True
        },
        'logging': {
            'level': 'INFO',
            'file': 'logs/jarvis.log'
        }
    }
    
    # Deep merge defaults with config
    return _deep_merge(defaults, config)


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """Deep merge two dictionaries."""
    result = base.copy()
    
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    
    return result
