import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from hkipo.__main__ import main


class ServiceCLITests(unittest.TestCase):
    def invoke(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        return code, json.loads(output.getvalue())

    @patch('hkipo.service.LocalService')
    def test_default_scan_writer_disabled(self, service):
        service.return_value.cycle.return_value = {'health': {'ok': True}}
        self.assertEqual(self.invoke(['service', '--data-dir', '/tmp/fake'])[0], 0)
        self.assertFalse(service.call_args.args[0].enable_writer)
        service.return_value.cycle.assert_called_once_with(scan=True, classify=False, work=False)

    @patch('hkipo.service.LocalService')
    def test_settings_cannot_enable_writer_and_directory_override(self, service):
        service.return_value.cycle.return_value = {'work': {'status': 'disabled'}, 'health': {'ok': True}}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'service.json'
            config.write_text(json.dumps({'data_dir': '/old', 'enable_writer': True,
                                          'writer_options': {'settings_path': '/local/network.json'}}))
            code, _ = self.invoke(['service', '--settings', str(config), '--data-dir', '/override', '--work'])
        settings = service.call_args.args[0]
        self.assertEqual(code, 2)
        self.assertFalse(settings.enable_writer)
        self.assertEqual(settings.data_dir, '/override')
        self.assertEqual(settings.writer_options, {'settings_path': '/local/network.json'})
        service.return_value.cycle.assert_called_once_with(scan=False, classify=False, work=True)

    @patch('hkipo.service.LocalService')
    def test_explicit_all_stages_and_writer(self, service):
        service.return_value.cycle.return_value = {'health': {'ok': True}, 'work': {'status': 'completed', 'results': [{'status': 'ready_for_delivery'}]}}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'service.json'
            config.write_text('{"data_dir": "/fake"}')
            code, _ = self.invoke(['service', '--config', str(config), '--scan', '--classify', '--work', '--enable-writer'])
        self.assertEqual(code, 0)
        self.assertTrue(service.call_args.args[0].enable_writer)
        service.return_value.cycle.assert_called_once_with(scan=True, classify=True, work=True)

    @patch('hkipo.service.LocalService')
    def test_exit_codes(self, service):
        for result, code in [({'health': {'ok': False}}, 3),
                             ({'health': {'ok': True}, 'scan': {'ok': False}}, 3),
                             ({'health': {'ok': True}, 'pipeline': {'status': 'busy'}}, 4),
                             ({'health': {'ok': True}, 'work': {'results': [{'status': 'blocked'}]}}, 2)]:
            with self.subTest(result=result):
                service.return_value.cycle.return_value = result
                self.assertEqual(self.invoke(['service', '--data-dir', '/fake', '--classify'])[0], code)

    @patch('hkipo.service.LocalService')
    def test_invalid_configuration_never_constructs_service(self, service):
        for args in [[], ['--data-dir', '/fake', '--enable-writer']]:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(['service'] + args)
                self.assertEqual(error.exception.code, 2)
        service.assert_not_called()

    @patch('hkipo.monitor.Monitor')
    @patch('hkipo.store.StateStore')
    def test_existing_scan_db_unchanged(self, store, monitor):
        monitor.return_value.scan.return_value = {'ok': True}
        self.assertEqual(self.invoke(['scan', '--db', '/fake.sqlite3'])[0], 0)
        store.assert_called_once_with('/fake.sqlite3')
        monitor.return_value.scan.assert_called_once_with()

    @patch('hkipo.writer.write_article')
    def test_existing_write_arguments_unchanged(self, writer):
        writer.return_value = {'status': 'ready_for_delivery'}
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / 'event.json'
            event.write_text('{"event_id": "fake"}')
            self.assertEqual(self.invoke(['write', '--event', str(event), '--pdf', '/fake.pdf', '--out', '/out', '--settings', '/network.json'])[0], 0)
        self.assertEqual(writer.call_args.args[0], {'event_id': 'fake'})
        self.assertEqual(writer.call_args.args[-1], '/network.json')


class WatchCLITests(unittest.TestCase):
    invoke = ServiceCLITests.invoke
    @patch('signal.signal')
    @patch('hkipo.service.LocalService')
    def test_watch_default_scan_only_restores_signals(self, service, signal_call):
        import signal
        signal_call.side_effect = ['old-int', 'old-term', None, None]
        service.return_value.run_loop.return_value = {'status': 'stopped', 'cycles': 1}
        code, result = self.invoke(['watch', '--data-dir', '/fake', '--max-cycles', '1'])
        self.assertEqual(code, 0)
        self.assertEqual(result['cycles'], 1)
        call = service.return_value.run_loop.call_args
        self.assertFalse(call.args[0].is_set())
        self.assertEqual(call.kwargs, {'max_cycles': 1, 'dispatch': False})
        self.assertFalse(service.call_args.args[0].enable_writer)
        self.assertEqual(signal_call.call_args_list[-2].args, (signal.SIGINT, 'old-int'))
        self.assertEqual(signal_call.call_args_list[-1].args, (signal.SIGTERM, 'old-term'))

    @patch('signal.signal')
    @patch('hkipo.service.LocalService')
    def test_watch_signal_stops_event_and_explicit_pipeline(self, service, signal_call):
        def run(stop, **kwargs):
            signal_call.call_args_list[0].args[1](None, None)
            self.assertTrue(stop.is_set())
            signal_call.call_args_list[1].args[1](None, None)
            self.assertTrue(stop.is_set())
            return {'status': 'stopped', 'cycles': 0}
        service.return_value.run_loop.side_effect = run
        code, _ = self.invoke(['watch', '--data-dir', '/fake', '--classify', '--work', '--enable-writer'])
        self.assertEqual(code, 0)
        self.assertTrue(service.call_args.args[0].enable_writer)
        self.assertEqual(service.return_value.run_loop.call_args.kwargs, {'max_cycles': None, 'dispatch': True})

    @patch('signal.signal')
    @patch('hkipo.service.LocalService')
    def test_watch_classification_only_and_busy(self, service, signal_call):
        service.return_value.run_loop.return_value = {'status': 'already_running', 'cycles': 0}
        self.assertEqual(self.invoke(['watch', '--data-dir', '/fake', '--classify'])[0], 4)
        self.assertFalse(service.call_args.args[0].enable_writer)
        self.assertTrue(service.return_value.run_loop.call_args.kwargs['dispatch'])

    @patch('hkipo.service.LocalService')
    def test_watch_invalid_flags_never_construct_service(self, service):
        for extra in [['--work'], ['--work', '--enable-writer'], ['--enable-writer'],
                      ['--max-cycles', '0'], ['--max-cycles', '-1']]:
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(['watch', '--data-dir', '/fake'] + extra)
        service.assert_not_called()

    @patch('signal.signal')
    @patch('hkipo.service.LocalService')
    def test_watch_exception_restores_handlers(self, service, signal_call):
        signal_call.side_effect = ['old-int', 'old-term', None, None]
        service.return_value.run_loop.side_effect = RuntimeError('fake failure')
        with self.assertRaises(RuntimeError):
            self.invoke(['watch', '--data-dir', '/fake'])
        self.assertEqual(signal_call.call_count, 4)


if __name__ == '__main__':
    unittest.main()
