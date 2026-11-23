import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hkipo.network import child_environment, resolve_cli, NetworkSettingsError


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name, 'local-settings.json')

    def settings(self, **values):
        self.path.write_text(json.dumps(values))
        return self.path

    @patch('hkipo.network.socket.create_connection')
    def test_proxy_only_in_returned_child_and_port_checked(self, connect):
        original = os.environ.copy()
        base = {'PATH': '/safe', 'https_proxy': 'old', 'NO_PROXY': '*'}
        result = child_environment(self.settings(proxy_url='http://127.0.0.1:7890'), base)
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
            self.assertEqual(result[key], 'http://127.0.0.1:7890')
        self.assertNotIn('https_proxy', result)
        self.assertEqual(result['PATH'], '/safe')
        self.assertNotEqual(result['NO_PROXY'], '*')
        self.assertEqual(base, {'PATH': '/safe', 'https_proxy': 'old', 'NO_PROXY': '*'})
        self.assertEqual(os.environ.copy(), original)
        connect.assert_called_once_with(('127.0.0.1', 7890), timeout=1.0)

    @patch('hkipo.network.socket.create_connection')
    def test_unset_is_direct_without_settings_discovery(self, connect):
        base = {'PATH': '/safe', 'HTTP_PROXY': 'inherited', 'all_proxy': 'inherited'}
        result = child_environment(base_env=base)
        self.assertEqual(result, {'PATH': '/safe'})
        self.assertIn('HTTP_PROXY', base)
        connect.assert_not_called()

    @patch('hkipo.network.socket.create_connection')
    def test_rejects_credentials_remote_injection_and_implicit_port(self, connect):
        for url in ('http://user:secret@127.0.0.1:7890', 'http://example.com:7890',
                    'http://0.0.0.0:7890', 'https://127.0.0.1:7890',
                    'http://127.0.0.1', 'http://127.0.0.1:7890/path',
                    'http://127.0.0.1:7890?secret=1', 'http://127.0.0.1:7890\nX=1',
                    'http://127.0.0.1:7890;evil', 'http://127.0.0.1:99999'):
            with self.subTest(url=url), self.assertRaises(NetworkSettingsError) as cm:
                child_environment(self.settings(proxy_url=url), {})
            self.assertNotIn('secret', str(cm.exception))
        connect.assert_not_called()

    @patch('hkipo.network.socket.create_connection', side_effect=ConnectionRefusedError())
    def test_closed_port_fails_without_direct_fallback(self, connect):
        with self.assertRaisesRegex(ConnectionError, 'port is unavailable'):
            child_environment(self.settings(proxy_url='http://127.0.0.1:7890'), {})

    @patch('hkipo.network.socket.create_connection')
    def test_localhost_is_literal_loopback(self, connect):
        env = child_environment(self.settings(proxy_url='http://localhost:7890/'), {})
        self.assertEqual(env['HTTPS_PROXY'], 'http://127.0.0.1:7890')
        connect.assert_called_once_with(('127.0.0.1', 7890), timeout=1.0)

    def test_explicit_cli_is_file_not_shell_command(self):
        cli = Path(self.tmp.name, 'codex executable')
        cli.write_text('#!/bin/sh\nexit 0\n')
        cli.chmod(0o700)
        self.assertEqual(resolve_cli(self.settings(codex_executable=str(cli))), str(cli))
        for bad in ('codex', str(cli) + '; echo bad', str(cli) + '\n'):
            with self.assertRaises(NetworkSettingsError):
                resolve_cli(self.settings(codex_executable=bad))

    def test_no_settings_cli_uses_path_without_launch(self):
        cli = Path(self.tmp.name, 'codex')
        cli.touch(); cli.chmod(0o700)
        with patch('hkipo.network.shutil.which', return_value=str(cli)):
            self.assertEqual(resolve_cli(), str(cli))

    def test_no_secret_fields_are_accepted_or_echoed(self):
        self.path.write_text('{"subscription_secret":"do-not-echo"}')
        with self.assertRaises(NetworkSettingsError) as cm:
            child_environment(self.path, {})
        self.assertNotIn('do-not-echo', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
