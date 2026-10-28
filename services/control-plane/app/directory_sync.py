"""Optional bundled-Keycloak adapter: read IdP directory, provision standard SCIM.

No database access, no platform administrator token, and no Kubernetes API token.
Other IdPs provision the same SCIM endpoint using their own integration.
"""
import argparse
import os
from pathlib import Path
import time
from urllib.parse import quote

import httpx

CORE = 'urn:ietf:params:scim:schemas:core:2.0:'


class SyncError(Exception):
    pass


def request(client, method, path, **kwargs):
    try:
        response = client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise SyncError(f'Directory request failed (HTTP {response.status_code}); check endpoint permissions and availability')
        return response.json() if response.content else None
    except (httpx.HTTPError, ValueError):
        raise SyncError('Directory request unavailable or invalid response; retrying without retiring identities') from None


def pages(client, path, **params):
    result = []
    seen = set()
    while True:
        page = request(client, 'GET', path, params={**params, 'first':len(result), 'max':100})
        if not isinstance(page, list) or any(not isinstance(row, dict) or not row.get('id') for row in page):
            raise SyncError('Incomplete IdP inventory; refusing directory changes')
        if any(row['id'] in seen for row in page) or len({row['id'] for row in page}) != len(page):
            raise SyncError('Repeated IdP inventory page; refusing directory changes')
        seen.update(row['id'] for row in page)
        result.extend(page)
        if len(page) < 100:
            return result
        if len(result) > 100000:
            raise SyncError('IdP inventory exceeds the configured adapter limit')


def scim_inventory(client, collection):
    result = []
    total, seen = None, set()
    while True:
        page = request(client, 'GET', '/scim/v2/'+collection, params={'startIndex':len(result)+1, 'count':1000})
        if not isinstance(page, dict) or not isinstance(page.get('Resources'), list):
            raise SyncError('Incomplete SCIM inventory; refusing directory changes')
        reported = page.get('totalResults')
        if not isinstance(reported,int) or reported < 0 or (total is not None and total != reported):
            raise SyncError('SCIM inventory changed during pagination; retry synchronization')
        total = reported
        rows = page['Resources']
        if any(not isinstance(row, dict) or not row.get('id') for row in rows):
            raise SyncError('Invalid SCIM inventory')
        if any(row['id'] in seen for row in rows) or len({row['id'] for row in rows}) != len(rows):
            raise SyncError('Repeated SCIM inventory page')
        seen.update(row['id'] for row in rows)
        result.extend(rows)
        if len(result) == total:
            return result
        if not rows or len(result) > min(total,100000):
            raise SyncError('Truncated SCIM inventory; refusing directory changes')


def same(current, desired):
    def normalize(value):
        if isinstance(value, list):
            return sorted((normalize(row) for row in value), key=repr)
        if isinstance(value, dict):
            return {k:normalize(v) for k,v in value.items()}
        return value
    return all(normalize(current.get(key)) == normalize(value) for key,value in desired.items() if key != 'schemas')


def synchronize(keycloak, scim, realm, service_clients=()):
    """Fetch a complete snapshot before writing; retirement happens last.

    Service-account users are skipped, except clients explicitly listed in service_clients
    (the automation identity used by infrastructure-as-code)."""
    base = '/admin/realms/'+quote(realm, safe='')
    prefix = 'keycloak:'+realm+':'
    users, ignored = [], set()
    rows = list(pages(keycloak, base+'/users', briefRepresentation='false'))
    # Keycloak's user listing omits service accounts; fetch allow-listed ones by exact username.
    for client in service_clients:
        found = request(keycloak, 'GET', base+'/users', params={'username': 'service-account-'+client, 'exact': 'true',
                                                                 'briefRepresentation': 'false'})
        if not isinstance(found, list):
            raise SyncError('Incomplete IdP service-account inventory; refusing directory changes')
        rows += [dict(row, serviceAccountClientId=client) for row in found if row.get('username') == 'service-account-'+client]
    rows = list({row['id']: row for row in rows}.values())  # some Keycloak versions also list them
    for row in rows:
        if row.get('serviceAccountClientId') and row['serviceAccountClientId'] not in service_clients:
            ignored.add(row['id']); continue
        roles = request(keycloak, 'GET', base+'/users/'+quote(row['id'],safe='')+'/role-mappings/realm/composite')
        if not isinstance(roles, list) or any(not isinstance(r, dict) or not isinstance(r.get('name'), str) for r in roles):
            raise SyncError('Incomplete IdP role inventory; refusing directory changes')
        row['roles'] = [{'value':name} for name in sorted({r['name'] for r in roles})]
        users.append(row)
    if not users:
        raise SyncError('Empty IdP user inventory; refusing to retire the directory')
    groups, seen = [], set()
    pending = [(row,None) for row in pages(keycloak, base+'/groups', briefRepresentation='false')]
    while pending:
        row, parent = pending.pop()
        if row['id'] in seen:
            raise SyncError('Duplicate or cyclic IdP group inventory')
        seen.add(row['id'])
        row['parent'] = parent
        row['members'] = pages(keycloak,base+'/groups/'+quote(row['id'],safe='')+'/members')
        groups.append(row)
        pending.extend((child,row['id']) for child in pages(keycloak,base+'/groups/'+quote(row['id'],safe='')+'/children',briefRepresentation='false'))
    known_users = {u['id'] for u in users}
    if len(known_users)!=len(users) or any(m['id'] not in known_users | ignored for g in groups for m in g['members']):
        raise SyncError('Directory changed during inventory; refusing an incomplete synchronization')
    existing = {kind:scim_inventory(scim,kind) for kind in ('Users','Groups')}
    local = {kind:{row.get('externalId'):row for row in rows} for kind,rows in existing.items()}
    desired_external = set()

    def upsert(kind, desired):
        ext = desired['externalId']; desired_external.add(ext)
        current = local[kind].get(ext)
        if current and same(current, desired):
            return current
        path = '/scim/v2/'+kind
        result = request(scim, 'PUT' if current else 'POST', path+'/'+current['id'] if current else path, json=desired)
        local[kind][ext] = result
        return result

    mapping = {}
    active = {u['id'] for u in users if u.get('enabled', False)}
    for user in users:
        body = {'schemas':[CORE+'User'], 'externalId':prefix+'user:'+user['id'],
                'userName':user['username'], 'active':user.get('enabled',False), 'roles':user['roles'],
                'name':{'givenName':user.get('firstName',''), 'familyName':user.get('lastName','')},
                'displayName':(' '.join([user.get('firstName',''),user.get('lastName','')]).strip() or user['username']),
                'emails':[{'value':user['email'],'primary':True}] if user.get('email') else []}
        mapping[user['id']] = upsert('Users',body)['id']
    # Create missing groups before adding nested members. Existing memberships are
    # not cleared on every synchronization, avoiding needless revocation windows.
    for group in groups:
        ext = prefix+'group:'+group['id']
        body = {'schemas':[CORE+'Group'],'externalId':ext,'displayName':group.get('path') or group['name']}
        if ext not in local['Groups']:
            upsert('Groups',{**body,'members':[]})
        mapping[group['id']] = local['Groups'][ext]['id']
    for group in groups:
        members = [{'value':mapping[u['id']],'type':'User'} for u in group['members'] if u['id'] in active]
        members += [{'value':mapping[g['id']],'type':'Group'} for g in groups if g['parent']==group['id']]
        upsert('Groups',{'schemas':[CORE+'Group'],'externalId':prefix+'group:'+group['id'],
                        'displayName':group.get('path') or group['name'],'members':members})
    for kind in ('Groups','Users'):
        for row in existing[kind]:
            ext = row.get('externalId','')
            if ext.startswith(prefix) and ext not in desired_external:
                request(scim,'DELETE','/scim/v2/'+kind+'/'+row['id'])
    return {'users':len(users),'groups':len(groups)}


def run_once(client_factory=httpx.Client):
    endpoint = os.environ['KEYCLOAK_URL'].rstrip('/')
    realm = os.environ.get('KEYCLOAK_REALM','fdp')
    with client_factory(base_url=endpoint,timeout=30,follow_redirects=False) as keycloak:
        token = request(keycloak,'POST','/realms/'+quote(realm,safe='')+'/protocol/openid-connect/token',
            data={'grant_type':'client_credentials','client_id':'fdp-provisioner',
                  'client_secret':os.environ['KEYCLOAK_PROVISIONER_SECRET']})
        if not isinstance(token,dict) or not token.get('access_token'):
            raise SyncError('IdP provisioning authentication failed')
        keycloak.headers['Authorization'] = 'Bearer '+token['access_token']
        with client_factory(base_url=os.environ['FDP_URL'].rstrip('/'),timeout=30,follow_redirects=False,
                            headers={'Authorization':'Bearer '+os.environ['FDP_SCIM_TOKEN']}) as scim:
            allowed = tuple(c for c in os.environ.get('KEYCLOAK_SERVICE_CLIENTS','').split(',') if c)
            return synchronize(keycloak,scim,realm,allowed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--loop',action='store_true')
    parser.add_argument('--interval',type=int,default=60)
    args = parser.parse_args()
    ready = Path('/tmp/fdp-directory-sync-ready')
    while True:
        try:
            result = run_once()
            ready.touch(mode=0o600)
            print('Directory synchronized: '+str(result['users'])+' users, '+str(result['groups'])+' groups',flush=True)
        except (SyncError,KeyError):
            ready.unlink(missing_ok=True)
            print('Directory synchronization failed; check IdP/SCIM configuration and endpoint availability',flush=True)
            if not args.loop:
                raise SystemExit(1)
        if not args.loop:
            return
        time.sleep(max(5,args.interval))


if __name__ == '__main__':
    main()
