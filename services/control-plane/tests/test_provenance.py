"""Management provenance: Terraform-managed objects, app changes since the last apply, and nothing invented."""
from .conftest import as_

TERRAFORM = {**as_('admin'), 'X-FDP-Managed-By': 'terraform', 'X-FDP-Configuration': 'platform-config/terraform',
             'X-FDP-Configuration-Revision': 'abc1234'}


def provenance(client, target, user='admin'):
    return client.get('/v1/rbac/provenance', headers=as_(user), params={'target': target})


def test_terraform_changes_mark_objects_and_app_changes_are_drift(client):
    managed = client.post('/v1/rbac/folders', headers=TERRAFORM, json={'name': 'Managed'}).json()
    by_app = client.post('/v1/rbac/folders', headers=as_('admin'), json={'name': 'Made in the app'}).json()
    found = provenance(client, managed['id']).json()
    assert found['source'] == 'terraform' and found['reference'] == 'platform-config/terraform' and found['revision'] == 'abc1234'
    assert found['app_change'] is None
    assert provenance(client, by_app['id']).json() == {'source': 'unknown'}  # never invented
    # An app change to a Terraform-managed object is recorded as likely drift, and listed for review.
    renamed = client.patch(f"/v1/rbac/resources/{managed['id']}", headers=as_('admin'), json={'name': 'Renamed in the app', 'parent_id': 'workloads'})
    assert renamed.status_code == 200, renamed.text
    change = provenance(client, managed['id']).json()['app_change']
    assert change and change['by'] == 'admin'
    assert [c['target'] for c in client.get('/v1/rbac/provenance/changes', headers=as_('admin')).json()] == [managed['id']]
    # The next apply takes it back: no drift left.
    client.patch(f"/v1/rbac/resources/{managed['id']}", headers=TERRAFORM, json={'name': 'Managed', 'parent_id': 'workloads'}).raise_for_status()
    assert provenance(client, managed['id']).json()['app_change'] is None
    assert client.get('/v1/rbac/provenance/changes', headers=as_('admin')).json() == []


def test_provenance_is_only_for_people_who_may_see_the_object(client):
    managed = client.post('/v1/rbac/folders', headers=TERRAFORM, json={'name': 'Hidden'}).json()
    assert provenance(client, managed['id'], 'bob').status_code == 404
    assert provenance(client, 'nothing-like-this', 'bob').status_code == 404
    assert client.get('/v1/rbac/provenance/changes', headers=as_('bob')).status_code == 403


def test_a_declaration_is_needed_not_a_user_agent(client):
    folder = client.post('/v1/rbac/folders', headers={**as_('admin'), 'User-Agent': 'Terraform/1.9 terraform-provider-fdp'},
                         json={'name': 'Looks like Terraform'}).json()
    assert provenance(client, folder['id']).json() == {'source': 'unknown'}
