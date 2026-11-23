"""Explicit project settings for Codex CLI/quota child processes only.

Never apply this environment to SMTP, HKEX downloads or WeChat. This module does
not change os.environ, system proxy settings, profiles, accounts or proxy apps.
"""
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
from urllib.parse import urlsplit

_PROXY_KEYS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
               'http_proxy', 'https_proxy', 'all_proxy', 'NO_PROXY', 'no_proxy')


class NetworkSettingsError(ValueError):
    """Invalid local settings; error text never repeats untrusted values."""


def load_settings(settings_path=None):
    """Read only the explicit file; None performs no settings discovery."""
    if settings_path is None:
        return {}
    path = Path(settings_path)
    try:
        settings = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise NetworkSettingsError('Cannot read the explicitly selected local settings JSON') from None
    if not isinstance(settings, dict):
        raise NetworkSettingsError('Local settings must be a JSON object')
    if set(settings) - {'proxy_url', 'codex_executable'}:
        raise NetworkSettingsError('Unsupported local settings field')
    for key in ('proxy_url', 'codex_executable'):
        if key in settings and not isinstance(settings[key], str):
            raise NetworkSettingsError('Local settings values must be strings')
    return settings


def _proxy_endpoint(value):
    if not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise NetworkSettingsError('Proxy URL must contain no whitespace or control characters')
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme != 'http' or not host or port is None or
                not 1 <= port <= 65535 or parsed.username is not None or
                parsed.password is not None or parsed.path not in ('', '/') or
                parsed.query or parsed.fragment):
            raise ValueError()
        # Resolve localhost ourselves, never via DNS or a configurable host alias.
        host = '127.0.0.1' if host.lower() == 'localhost' else host
        address = ipaddress.ip_address(host)
        if not address.is_loopback:
            raise ValueError()
    except (ValueError, TypeError):
        raise NetworkSettingsError('Only credential-free loopback HTTP proxy URLs with an explicit port are supported') from None
    canonical_host = '[' + str(address) + ']' if address.version == 6 else str(address)
    return 'http://' + canonical_host + ':' + str(port), str(address), port


def child_environment(settings_path=None, base_env=None):
    """Return isolated CLI/quota env, failing clearly if configured port is down.

    No explicit proxy means direct mode, including when the parent had inherited
    proxy variables. A TCP listener check confirms availability, not proxy protocol
    correctness or remote reachability; it never starts or changes that listener.
    """
    settings = load_settings(settings_path)
    env = dict(os.environ if base_env is None else base_env)
    for key in _PROXY_KEYS:
        env.pop(key, None)
    proxy = settings.get('proxy_url')
    if proxy is None:
        return env
    canonical, host, port = _proxy_endpoint(proxy)
    try:
        with socket.create_connection((host, port), timeout=1.0):
            pass
    except OSError:
        raise ConnectionError('Configured loopback HTTP proxy port is unavailable; start the existing local proxy or explicitly select direct mode') from None
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        env[key] = canonical
    env['NO_PROXY'] = 'localhost,127.0.0.1,::1'
    return env


def resolve_cli(settings_path=None):
    """Return a validated executable path; never modify PATH or invoke a shell."""
    settings = load_settings(settings_path)
    configured = settings.get('codex_executable')
    if configured is None:
        configured = shutil.which('codex')
        if configured is None:
            raise FileNotFoundError('Codex CLI is not on PATH; select an explicit executable in local settings')
    if not configured or any(ord(c) < 32 or ord(c) == 127 for c in configured):
        raise NetworkSettingsError('Codex executable must be an absolute executable file path')
    path = Path(configured)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise NetworkSettingsError('Codex executable must be an absolute executable file path')
    return str(path)
