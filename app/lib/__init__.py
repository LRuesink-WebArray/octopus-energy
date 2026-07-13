import logging

import homey

logger = logging.getLogger(__name__)

# Used only when no server URL is configured anywhere. Hitting this means
# pairing/polling will fail, so resolve_server_url() logs loudly when it is used.
_PLACEHOLDER_SERVER_URL = "https://octopus-energy-server.example.com"


def _env_get(env: object, key: str) -> str | None:
    """Read a key from env.json regardless of how the SDK exposes it (dict or object)."""
    if env is None:
        return None
    if hasattr(env, "get"):
        try:
            return env.get(key)
        except Exception:
            pass
    return getattr(env, key, None)


def resolve_server_url(homey_manager: object) -> tuple[str, str]:
    """Resolve the backend server URL and report where it came from.

    Resolution order:
      1. App setting ``server_url`` (user-configurable override)
      2. ``SERVER_URL`` from env.json (the normal, baked-in configuration)
      3. Placeholder (means: not configured — calls will fail)

    Returns ``(url, source)`` so callers can log which source won.
    """
    # 1. Settings override
    try:
        setting = homey_manager.settings.get("server_url")
    except Exception as exc:  # settings manager may be unavailable in some contexts
        logger.debug("resolve_server_url: settings.get failed: %s", exc)
        setting = None
    if setting:
        return setting, "settings.server_url"

    # 2. env.json — exposed module-level (homey.env) per SDK, instance as fallback.
    url = _env_get(getattr(homey, "env", None), "SERVER_URL")
    if url:
        return url, "env.SERVER_URL"
    url = _env_get(getattr(homey_manager, "env", None), "SERVER_URL")
    if url:
        return url, "homey.env.SERVER_URL"

    return _PLACEHOLDER_SERVER_URL, "placeholder (NOT CONFIGURED)"
