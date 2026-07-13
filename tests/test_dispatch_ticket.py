import json
from pathlib import Path

from iicp_client.dispatch_ticket import verify_dispatch_route_ticket


def test_canonical_dispatch_ticket_fixture_verifies():
    fixture=json.loads((Path(__file__).parents[1]/'parity'/'dispatch-route-ticket-v1.json').read_text())
    claims=fixture['valid']['claims']
    assert verify_dispatch_route_ticket(fixture['valid']['token'], fixture['public_key_hex'], claims['iss'], claims['node_id'], claims['intent'], now_s=1_800_000_000)
    assert not verify_dispatch_route_ticket(fixture['valid']['token']+'0', fixture['public_key_hex'], claims['iss'], claims['node_id'], claims['intent'], now_s=1_800_000_000)

def test_canonical_dispatch_ticket_vectors_fail_closed():
    fixture=json.loads((Path(__file__).parents[1]/'parity'/'dispatch-route-ticket-v1.json').read_text())
    fixture['valid']['claims']
    for vector in fixture['validation_vectors']:
        token = fixture['valid']['token'] + ('0' if vector['token'] == 'valid+0' else '') if vector['token'].startswith('valid') else fixture['wrong_audience']['token'] if vector['token'] == 'wrong_audience' else vector['token']
        result = verify_dispatch_route_ticket(token, fixture['public_key_hex'], vector['issuer'], vector['node_id'], vector['intent'], now_s=vector['now_s'])
        assert (result is not None) == (vector['expected'] == 'valid'), vector['name']
