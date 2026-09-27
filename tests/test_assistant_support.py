import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from assistant_diagnostics import collect, redact, tail, current_bridge_error
from assistant_overlay import publish

class AssistantSupportTests(unittest.TestCase):
    def test_bridge_diagnostics_only_includes_current_launch_and_known_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / 'communication_mod_errors.log'
            log.write_bytes(b'OLD PRIVATE HISTORY\nUnicodeDecodeError: current failure\n')
            launch = {'bridge_stderr_path': str(log), 'bridge_stderr_start_offset': 20}
            metadata = root / 'launch-latest.json'
            metadata.write_text(json.dumps(launch), encoding='utf-8')
            with patch('assistant_diagnostics.installation', return_value=SimpleNamespace(game=root)):
                result = current_bridge_error(root)
                self.assertIn('UnicodeDecodeError', result)
                self.assertNotIn('PRIVATE HISTORY', result)
                launch['bridge_stderr_path'] = str(root / '.env')
                metadata.write_text(json.dumps(launch), encoding='utf-8')
                self.assertEqual('', current_bridge_error(root))

    def test_diagnostics_only_reads_allowlisted_logs_and_bounds_output(self):
        with tempfile.TemporaryDirectory(prefix='中文 空格 ') as tmp:
            root=Path(tmp); data=root/'data';data.mkdir();logs=root/'logs/campaign-starts';logs.mkdir(parents=True)
            (data/'auto-start.log').write_text('outer code 2',encoding='utf-8')
            (logs/'campaign-1.log').write_text('x'*20000+'actual failure',encoding='utf-8')
            (root/'.env').write_text('SHOULD_NOT_BE_READ',encoding='utf-8')
            result=collect(root,data,'auto')
            self.assertIn('actual failure',result);self.assertIn('outer code 2',result)
            self.assertNotIn('SHOULD_NOT_BE_READ',result);self.assertLess(len(result),13000)
            self.assertIn('尚未生成',tail(root/'missing'))

    def test_redacts_credentials_and_user_directory(self):
        value=redact('api_key=private-value token: abc password="hello" Bearer abcdef C:\\Users\\Friend\\game')
        for private in ('private-value','abc','hello','abcdef','Friend'):
            self.assertNotIn(private,value)

    def test_overlay_invalidated_and_bound_to_current_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'advisor-overlay.json'
            frame={'session':'game-a','state_seq':4}
            advice=SimpleNamespace(token='t',action='打出 防御',target='无',reason='获得格挡\n细节',scene='战斗')
            session=SimpleNamespace(frame=frame,advice=advice,token='t')
            publish(path,session);data=json.loads(path.read_text(encoding='utf-8'))
            self.assertTrue(data['visible']);self.assertEqual(4,data['state_seq']);self.assertEqual('获得格挡',data['reason'])
            session.token='new';publish(path,session)
            self.assertFalse(json.loads(path.read_text(encoding='utf-8'))['visible'])
            session.frame=None;session.advice=None;publish(path,session)
            self.assertNotIn('action',json.loads(path.read_text(encoding='utf-8')))
