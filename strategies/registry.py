"""
Dynamic Strategy Registry
Scans the strategies/ directory, dynamically loads python strategy modules, and registers them.
"""
import os
import importlib
import logging
from pathlib import Path
from typing import Dict, List
from strategies.base import BaseStrategy

_log = logging.getLogger(__name__)

STRATEGIES: Dict[str, BaseStrategy] = {}

def load_strategies():
    """Dynamically discover and load strategies from the strategies/ directory."""
    global STRATEGIES
    STRATEGIES.clear()

    strategies_dir = Path(__file__).resolve().parent
    for file_path in strategies_dir.glob("*.py"):
        module_name = file_path.stem
        # Exclude core framework files
        if module_name in ("__init__", "base", "registry", "calibration", "engine", "settlement"):
            continue

        try:
            # Dynamically import the strategy module
            module = importlib.import_module(f"strategies.{module_name}")
            
            # Find and instantiate any class subclassing BaseStrategy
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and attr != BaseStrategy and issubclass(attr, BaseStrategy):
                    strategy_instance = attr()
                    STRATEGIES[strategy_instance.key] = strategy_instance
                    _log.debug("Successfully loaded strategy: %s (%s)", strategy_instance.name, strategy_instance.key)
        except Exception as e:
            _log.error("Failed to load strategy module %s: %s", module_name, e)

    print(f"[StrategyRegistry] Loaded {len(STRATEGIES)} strategies: {list(STRATEGIES.keys())}")

def get_strategy(key: str) -> BaseStrategy | None:
    """Retrieve an instanced strategy by key."""
    if not STRATEGIES:
        load_strategies()
    return STRATEGIES.get(key)

def get_all_strategies() -> List[BaseStrategy]:
    """Retrieve all instanced strategies."""
    if not STRATEGIES:
        load_strategies()
    return list(STRATEGIES.values())
