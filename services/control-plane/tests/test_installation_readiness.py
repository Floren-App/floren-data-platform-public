"""The installation readiness report: evidence of what is configured, operated elsewhere or unknown; read-only."""
from app.config import get_settings
from .conftest import as_


def items(client):
    response = client.get('/v1/admin/installation-readiness', headers=as_('admin'))
    assert response.status_code == 200, response.text
    return {(i['area'], i['item']): i for i in response.json()['items']}


def test_report_claims_only_what_it_can_see_and_changes_nothing(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'bundled_services', 'keycloak,metadata-postgresql')
    before = client.get('/v1/backups/plans', headers=as_('admin')).json()
    found = items(client)
    assert found[('Release', 'Metadata schema')]['state'] == 'configured'
    assert found[('Dependencies', 'Metadata database')]['state'] == 'single_instance'
    assert found[('Dependencies', 'Identity provider')]['state'] == 'single_instance'
    assert found[('Dependencies', 'Secret store')]['state'] == 'customer_operated'
    assert found[('Recovery', 'Backup plans')]['state'] == 'not_configured'       # optional and off: said plainly
    assert found[('Recovery', 'Restore drill')]['state'] == 'unknown'             # never assumed tested
    assert found[('Recovery', 'Kafka messages')]['state'] == 'customer_operated'
    assert client.get('/v1/backups/plans', headers=as_('admin')).json() == before  # nothing enabled or created


def test_reusable_persona_policies_are_reported(client):
    from .rbac_helpers import grant_group, workspace_permissions
    assert items(client)[('Persona policies', 'Starting runs')]['state'] == 'not_configured'
    workspace_permissions(client, grant_group(client, 'workloads', 'Engineers', 'Reader'))
    assert items(client)[('Persona policies', 'Starting runs')]['state'] == 'configured'


def test_only_global_admin_reads_it(client):
    assert client.get('/v1/admin/installation-readiness', headers=as_('ana')).status_code == 403
