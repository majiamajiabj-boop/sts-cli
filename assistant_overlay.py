"""Display-only advice file; never sends game commands."""
import json
import time

def publish(path, session, *, enabled=True):
    value = {'schema_version': 1, 'visible': False, 'published_at': time.time()}
    frame, advice = session.frame, session.advice
    if enabled and frame and advice and session.token == advice.token:
        value.update(visible=True, session=frame['session'], state_seq=frame['state_seq'],
                     action=advice.action, target=advice.target,
                     reason=advice.reason.split('\n', 1)[0], scene=advice.scene)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)
