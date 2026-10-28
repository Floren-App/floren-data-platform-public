import httpx
import pytest
from sqlalchemy import select

from app import directory_sync as sync, rbac
from app.auth import User
from app.config import get_settings
from app.db import SessionLocal
from app.models import DirectoryPrincipal


class IdP:
    def __init__(self):
        self.users=[{'id':'admin-id','username':'admin','enabled':True}, {'id':'ana-id','username':'ana','enabled':True}]
        self.groups=[{'id':'finance-id','name':'Finance','path':'/Finance'}]
        self.children={'finance-id':[{'id':'readers-id','name':'Readers','path':'/Finance/Readers'}]}
        self.members={'readers-id':[self.users[1]]};self.fail=False
        self.service_accounts=[]  # like real Keycloak: never in the listing, only by exact username
    def handle(self,request):
        path=request.url.path.split('/admin/realms/fdp')[-1]
        if self.fail:return httpx.Response(503)
        if path=='/users' and request.url.params.get('exact')=='true':
            data=[u for u in self.service_accounts if u['username']==request.url.params.get('username')]
        elif path=='/users':data=self.users
        elif path.endswith('/role-mappings/realm/composite'):
            data=[{'name':'fdp-admin'}] if 'admin-id' in path else []
        elif path=='/groups':data=self.groups
        elif path.endswith('/children'):data=self.children.get(path.split('/')[2],[])
        elif path.endswith('/members'):data=self.members.get(path.split('/')[2],[])
        else:raise AssertionError(path)
        return httpx.Response(200,json=data)


def test_keycloak_scim_provisioning_is_idempotent_and_preserves_nested_identity_bindings(client,monkeypatch):
    monkeypatch.setattr(get_settings(),'scim_token','provisioning-only')
    client.headers['Authorization']='Bearer provisioning-only'
    idp=IdP();kc=httpx.Client(base_url='http://keycloak',transport=httpx.MockTransport(idp.handle))
    writes=[];original=sync.request
    def track(target,method,path,**kwargs):
        if target is client and method!='GET':writes.append((method,path))
        return original(target,method,path,**kwargs)
    monkeypatch.setattr(sync,'request',track)
    assert sync.synchronize(kc,client,'fdp')=={'users':2,'groups':2}
    users=sync.scim_inventory(client,'Users');groups=sync.scim_inventory(client,'Groups')
    ana=next(u for u in users if u['userName']=='ana')
    parent=next(g for g in groups if g['displayName']=='/Finance')
    child=next(g for g in groups if g['displayName']=='/Finance/Readers')
    assert parent['members']==[{'value':child['id'],'type':'Group'}]
    assert child['members']==[{'value':ana['id'],'type':'User'}]
    with SessionLocal() as db:
        assert parent['id'] in rbac.closure(db,ana['id'])
        assert rbac.resolve_user(db,User('admin')).is_admin
    writes.clear();sync.synchronize(kc,client,'fdp')
    assert writes==[]
    idp.users[1]['username']='ana-renamed';idp.users[1]['enabled']=False
    sync.synchronize(kc,client,'fdp')
    updated=next(u for u in sync.scim_inventory(client,'Users') if u['id']==ana['id'])
    assert updated['userName']=='ana-renamed' and updated['active'] is False
    assert next(g for g in sync.scim_inventory(client,'Groups') if g['id']==child['id'])['members']==[]
    idp.users=idp.users[:1];idp.children={};idp.members={}
    # Another provider's provisioned identities are never retired by this adapter.
    sync.request(client,'POST','/scim/v2/Users',json={'userName':'other-provider','externalId':'other:123','active':True})
    sync.synchronize(kc,client,'fdp')
    assert {u['userName'] for u in sync.scim_inventory(client,'Users')}=={'admin','other-provider'}
    assert len(sync.scim_inventory(client,'Groups'))==1
    with SessionLocal() as db:
        retired=db.get(DirectoryPrincipal,ana['id'])
        assert retired.attributes['deleted'] and not retired.active


def test_failed_or_empty_inventory_never_retires_existing_users(client,monkeypatch):
    monkeypatch.setattr(get_settings(),'scim_token','provisioning-only')
    client.headers['Authorization']='Bearer provisioning-only'
    idp=IdP();kc=httpx.Client(base_url='http://keycloak',transport=httpx.MockTransport(idp.handle))
    sync.synchronize(kc,client,'fdp')
    before=sync.scim_inventory(client,'Users')
    idp.fail=True
    with pytest.raises(sync.SyncError):sync.synchronize(kc,client,'fdp')
    assert sync.scim_inventory(client,'Users')==before
    idp.fail=False;idp.users=[]
    with pytest.raises(sync.SyncError,match='Empty'):sync.synchronize(kc,client,'fdp')
    assert sync.scim_inventory(client,'Users')==before


def test_repeated_idp_pages_and_truncated_scim_inventory_are_rejected():
    rows=[{'id':str(i)} for i in range(100)]
    kc=httpx.Client(base_url='http://keycloak',transport=httpx.MockTransport(lambda request:httpx.Response(200,json=rows)))
    with pytest.raises(sync.SyncError,match='Repeated'):sync.pages(kc,'/users')
    scim=httpx.Client(base_url='http://control',transport=httpx.MockTransport(lambda request:httpx.Response(200,json={
        'totalResults':2,'Resources':[]})))
    with pytest.raises(sync.SyncError,match='Truncated'):sync.scim_inventory(scim,'Users')


def test_bundled_idp_gateway_route_uses_its_own_host_and_preserves_platform_auth(client,monkeypatch):
    settings=get_settings()
    monkeypatch.setattr(settings,'gateway_config_token','gateway-only')
    monkeypatch.setattr(settings,'identity_proxy_url','http://fdp-keycloak:8080')
    monkeypatch.setattr(settings,'oidc_issuer','https://idp.example.com/realms/fdp')
    assert client.get('/internal/traefik/config').status_code==401
    response=client.get('/internal/traefik/config',headers={'X-FDP-Gateway-Config':'gateway-only'})
    assert response.status_code==200,response.text
    config=response.json()['http']
    assert config['routers']['identity']=={'rule':'Host(`idp.example.com`)','service':'identity','priority':10000}
    assert config['services']['identity']['loadBalancer']['servers']==[{'url':'http://fdp-keycloak:8080'}]
    assert config['routers']['api']['middlewares']==['api-strip']
    assert config['middlewares']['fdp-auth']['forwardAuth']['address'].endswith('/internal/auth/forward')


def test_only_the_allow_listed_automation_service_account_is_provisioned(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'scim_token', 'provisioning-only')
    client.headers['Authorization'] = 'Bearer provisioning-only'
    idp = IdP()
    # Real Keycloak omits service accounts from the user listing and returns them without
    # serviceAccountClientId from an exact username query.
    idp.service_accounts = [{'id': 'admin-id-auto', 'username': 'service-account-fdp-automation', 'enabled': True},
                            {'id': 'prov-id', 'username': 'service-account-fdp-provisioner', 'enabled': True}]
    kc = httpx.Client(base_url='http://keycloak', transport=httpx.MockTransport(idp.handle))
    sync.synchronize(kc, client, 'fdp')
    names = {u['userName'] for u in sync.scim_inventory(client, 'Users')}
    assert 'service-account-fdp-automation' not in names  # not allow-listed: skipped as before
    sync.synchronize(kc, client, 'fdp', service_clients=('fdp-automation',))
    names = {u['userName'] for u in sync.scim_inventory(client, 'Users')}
    assert 'service-account-fdp-automation' in names
    assert 'service-account-fdp-provisioner' not in names  # the IdP's own reader is never a Floren Data Platform user
    with SessionLocal() as db:
        assert rbac.resolve_user(db, User('service-account-fdp-automation')).is_admin
