import json
from pathlib import Path
from iicp_client.dispatch_ticket import verify_dispatch_route_ticket

def test_canonical_dispatch_ticket_fixture_verifies():
    fixture=json.loads((Path(__file__).parents[1]/'parity'/'dispatch-route-ticket-v1.json').read_text())
    claims=fixture['valid']['claims']
    assert verify_dispatch_route_ticket(fixture['valid']['token'], fixture['public_key_hex'], claims['iss'], claims['node_id'], claims['intent'], now_s=1_800_000_000)
    assert not verify_dispatch_route_ticket(fixture['valid']['token']+'0', fixture['public_key_hex'], claims['iss'], claims['node_id'], claims['intent'], now_s=1_800_000_000)
