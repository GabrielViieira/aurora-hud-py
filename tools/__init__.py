
"""Ferramentas tipadas da Aurora."""

from aurora.tools import app_manager, desktop_control, filesystem, process_manager, python_runner, shell, windows
from aurora.tools.base import ToolContext, ToolResult, ToolSpec, validate_arguments
from aurora.tools.registry import ToolNotFound, ToolRegistry


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_many(filesystem.build_tools())
    registry.register_many(python_runner.build_tools())
    registry.register_many(process_manager.build_tools())
    registry.register_many(windows.build_tools())
    registry.register_many(desktop_control.build_tools())
    registry.register_many(app_manager.build_tools())

    # Shell do Windows: comandos de consulta autorizados.
    registry.register(shell.build_shell_tool())

    return registry


__all__ = [
    "ToolContext",
    "ToolNotFound",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "build_registry",
    "validate_arguments",
]
