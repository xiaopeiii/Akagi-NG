import sys

from akagi_ng.core.paths import get_lib_dir

# Add runtime `lib/` directory to sys.path so local binary extensions (.pyd/.so) can be imported.
lib_dir = get_lib_dir()
if str(lib_dir) not in sys.path:
    # Prepend to ensure it takes precedence over site-packages.
    sys.path.insert(0, str(lib_dir))

try:
    import libriichi  # type: ignore[import-not-found]
except ImportError:
    try:
        # Copilot-compatible fallback for macOS arm64 packages where riichi wheel is used.
        import riichi as libriichi  # type: ignore[import-not-found]
    except ImportError as wheel_err:
        # 4P required: if import fails, no supported binary backend is available.
        raise ImportError(
            "Failed to load local libriichi binary from "
            f"{lib_dir} and fallback package 'riichi' is also unavailable. "
            "Install libriichi(.pyd/.so) under lib/ or install riichi wheel."
        ) from wheel_err

# 3P optional: allow missing libriichi3p when only 4P is needed.
try:
    import libriichi3p  # type: ignore[import-not-found]
except ImportError:
    libriichi3p = None  # type: ignore[assignment]

__all__ = ["libriichi", "libriichi3p"]
