"""Real KRaft + TLS/OAuth + FDP policy/SDK. Set FDP_KAFKA_TEST_CLASSPATH to opt in.

Build with mvn -Pintegration package dependency:build-classpath -Dmdep.outputFile=/tmp/kafka-classpath
in images/kafka/authorizer. No Docker, cluster, runtime installation or user data required.
"""
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import uuid

import httpx
import pytest

from .test_kafka_access import topic  # configure an isolated governed layer/topic
from .conftest import as_
from .rbac_helpers import grant_group


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0))
        return s.getsockname()[1]


@pytest.mark.skipif(not os.environ.get('FDP_KAFKA_TEST_CLASSPATH'), reason='Set FDP_KAFKA_TEST_CLASSPATH for real broker checks')
def test_real_broker_tls_oauth_sdk_and_revocation(client, topic, tmp_path, monkeypatch):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from kafka import KafkaAdminClient, KafkaConsumer, KafkaProducer
    from kafka.admin import NewTopic
    from kafka.sasl.oauth import AbstractTokenProvider
    from kafka.errors import TopicAuthorizationFailedError, GroupAuthorizationFailedError, SaslAuthenticationFailedError, NoBrokersAvailable, KafkaTimeoutError
    from sqlalchemy import delete
    from app.db import SessionLocal
    from app.models import RoleAssignment, Resource
    from app import kafka_access
    import fdp

    grant_group(client,topic['id'],'Writers','Contributor',users=('ana',))
    with SessionLocal() as db:
        layer=db.get(Resource,topic['config']['layer_id'])
        layer.config={**layer.config,'infrastructure':{**layer.config['infrastructure'],
            'kafka':{**layer.config['infrastructure']['kafka'],'token_ttl_seconds':30}}}
        db.commit()
    admin_port,controller_port,client_port = port(),port(),port()
    tls_bootstrap=f'localhost:{client_port}'
    lock=threading.Lock()
    class ControlPlane(BaseHTTPRequestHandler):
        def request(self):
            body=self.rfile.read(int(self.headers.get('content-length','0')))
            with lock:
                response=client.request(self.command,self.path,content=body,
                    headers={k:v for k,v in self.headers.items() if k.lower() not in ('host','content-length')})
            data=response.content
            if self.path.endswith('/session') and response.status_code==200:
                data=json.dumps({**response.json(),'bootstrap_servers':tls_bootstrap}).encode()
            self.send_response(response.status_code)
            self.send_header('content-type','application/json')
            self.send_header('content-length',str(len(data)))
            self.end_headers();self.wfile.write(data)
        do_GET=request
        do_POST=request
        def log_message(self,*_): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),ControlPlane)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    base=f'http://127.0.0.1:{server.server_port}'
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc)+timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]),critical=False)
        .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
    ca=tmp_path/'ca.pem';ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    store=tmp_path/'broker.p12';store.write_bytes(pkcs12.serialize_key_and_certificates(b'broker',key,cert,None,
        serialization.BestAvailableEncryption(b'test-password')))
    token=tmp_path/'policy';token.write_text(kafka_access.broker_credential(topic['config']['layer_id']))
    properties={
        'process.roles':'broker,controller','node.id':'1',
        'controller.quorum.bootstrap.servers':f'127.0.0.1:{controller_port}',
        'listeners':f'ADMIN://127.0.0.1:{admin_port},CONTROLLER://127.0.0.1:{controller_port},CLIENTS://127.0.0.1:{client_port}',
        'advertised.listeners':f'ADMIN://127.0.0.1:{admin_port},CLIENTS://{tls_bootstrap}',
        'listener.security.protocol.map':'ADMIN:PLAINTEXT,CONTROLLER:PLAINTEXT,CLIENTS:SASL_SSL',
        'controller.listener.names':'CONTROLLER','inter.broker.listener.name':'ADMIN',
        'log.dirs':str(tmp_path/'data'),'offsets.topic.replication.factor':'1',
        'transaction.state.log.replication.factor':'1','transaction.state.log.min.isr':'1',
        'auto.create.topics.enable':'false','num.network.threads':'2','num.io.threads':'2',
        'authorizer.class.name':'app.floren.kafka.FdpAuthorizer','super.users':'User:ANONYMOUS',
        'fdp.policy.url':base+'/internal/kafka/layers/'+topic['config']['layer_id']+'/allow',
        'fdp.policy.token.file':str(token),
        'principal.builder.class':'io.strimzi.kafka.oauth.server.OAuthKafkaPrincipalBuilder',
        'listener.name.clients.sasl.enabled.mechanisms':'OAUTHBEARER',
        'listener.name.clients.oauthbearer.sasl.server.callback.handler.class':'io.strimzi.kafka.oauth.server.JaasServerOauthValidatorCallbackHandler',
        'listener.name.clients.oauthbearer.sasl.login.callback.handler.class':'app.floren.kafka.FdpListenerLogin',
        'listener.name.clients.oauthbearer.sasl.jaas.config':
            'org.apache.kafka.common.security.oauthbearer.OAuthBearerLoginModule required '
            f'oauth.valid.issuer.uri="fdp-kafka" oauth.jwks.endpoint.uri="{base}/internal/kafka/jwks" '
            f'oauth.check.audience="true" oauth.client.id="fdp-kafka:{topic["config"]["layer_id"]}" oauth.username.claim="sub";',
        'listener.name.clients.connections.max.reauth.ms':'300000',
        'ssl.keystore.location':str(store),'ssl.keystore.password':'test-password',
        'ssl.key.password':'test-password','ssl.keystore.type':'PKCS12',
    }
    settings=tmp_path/'server.properties';settings.write_text('\n'.join(f'{k}={v}' for k,v in properties.items()))
    root=Path(__file__).resolve().parents[3]
    classpath=str(root/'images/kafka/authorizer/target/classes')+os.pathsep+Path(os.environ['FDP_KAFKA_TEST_CLASSPATH']).read_text().strip()
    java=str(Path(os.environ['JAVA_HOME'])/'bin/java') if os.environ.get('JAVA_HOME') else shutil.which('java')
    command=[java,'-Xms128m','-Xmx512m','-cp',classpath]
    cluster_id=base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip('=')
    log=(tmp_path/'broker.log').open('w+')
    broker=None
    try:
        formatted=subprocess.run(command+['kafka.tools.StorageTool','format','--standalone','-t',cluster_id,'-c',str(settings)],
            stdout=log,stderr=subprocess.STDOUT,timeout=45)
        assert formatted.returncode==0, (tmp_path/'broker.log').read_text()[-6000:]
        broker=subprocess.Popen(command+['kafka.Kafka',str(settings)],stdout=log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+60
        while True:
            assert broker.poll() is None,(tmp_path/'broker.log').read_text()[-7000:]
            try:
                admin=KafkaAdminClient(bootstrap_servers=f'127.0.0.1:{admin_port}',request_timeout_ms=3000,api_version_auto_timeout_ms=1000)
                admin.create_topics([NewTopic('orders',3,1),NewTopic('other',1,1)])
                admin.close();break
            except Exception:
                if time.monotonic()>deadline: raise
                time.sleep(.5)
        @contextmanager
        def sdk_client():
            with httpx.Client(base_url=base,headers=as_('ana'),timeout=30) as value:
                yield value
        monkeypatch.setattr(fdp,'_client',sdk_client)
        monkeypatch.setenv('SSL_CERT_FILE',str(ca))
        producer=fdp.kafka.producer('orders',request_timeout_ms=10000,max_block_ms=10000)
        consumer=None
        try:
            producer.send({'record_id':'one'}).get(timeout=20)
            consumer=fdp.kafka.consumer('orders',group='billing',auto_offset_reset='earliest',request_timeout_ms=15000)
            records=[]
            deadline=time.monotonic()+25
            while time.monotonic()<deadline and not records:
                records=[r.value for batch in consumer.poll(timeout_ms=1000).values() for r in batch]
            assert records==[{'record_id':'one'}]
            consumer.commit()
            security = {k:producer.config[k] for k in ('bootstrap_servers','security_protocol',
                'sasl_mechanism','sasl_oauth_token_provider','ssl_context')}
            hijack = KafkaConsumer('orders',group_id='another-identity.billing',enable_auto_commit=False,
                request_timeout_ms=15000, **{**security,'sasl_oauth_token_provider':consumer.config['sasl_oauth_token_provider']})
            try:
                with pytest.raises(GroupAuthorizationFailedError):
                    hijack.poll(timeout_ms=12000)
            finally:
                hijack.close()
            # A token with an invalid signature must fail authentication before any policy decision.
            pieces = producer._session.info['token'].split('.')
            pieces[2] = ('A' if pieces[2][0] != 'A' else 'B') + pieces[2][1:]
            class InvalidTokens(AbstractTokenProvider):
                def token(self):return '.'.join(pieces)
            invalid = None
            try:
                with pytest.raises((SaslAuthenticationFailedError,NoBrokersAvailable,KafkaTimeoutError)):
                    invalid = KafkaProducer(request_timeout_ms=3000,max_block_ms=5000,api_version_auto_timeout_ms=3000,
                        **{**security,'sasl_oauth_token_provider':InvalidTokens()})
                    invalid.send('orders',b'forged').get(timeout=10)
                assert 'Invalid token signature' in (tmp_path/'broker.log').read_text()
            finally:
                if invalid: invalid.close(timeout=1)
            # Keep the same client alive beyond its short-lived credentials. Reconnect
            # must obtain a fresh scoped session, without changing the identity's group.
            previous_expiry=producer._session.info['expires_at']
            while time.time() <= previous_expiry+1:
                time.sleep(.2)
            producer.send({'record_id':'refreshed'}).get(timeout=20)
            assert producer._session.info['expires_at']>previous_expiry
            deadline=time.monotonic()+20
            fresh=[]
            while time.monotonic()<deadline and not fresh:
                fresh=[r.value for batch in consumer.poll(timeout_ms=1000).values() for r in batch]
            assert fresh==[{'record_id':'refreshed'}]
            consumer.commit()
            # Even a raw client obtained from the SDK cannot write outside its scoped topic.
            with pytest.raises(TopicAuthorizationFailedError):
                producer._raw.send('other',{'record_id':'forbidden'}).get(timeout=15)
            with SessionLocal() as db:
                db.execute(delete(RoleAssignment).where(RoleAssignment.resource_id==topic['id'],RoleAssignment.role=='Contributor'))
                db.commit()
            with pytest.raises(TopicAuthorizationFailedError):
                producer.send({'record_id':'revoked'}).get(timeout=15)
        finally:
            if consumer: consumer.close()
            producer.close(timeout=5)
    finally:
        if broker:
            broker.terminate()
            try:broker.wait(timeout=20)
            except subprocess.TimeoutExpired:broker.kill();broker.wait()
        server.shutdown();server.server_close();log.close()
