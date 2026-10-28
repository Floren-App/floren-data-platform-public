import json
import sys

import pytest

from fdp import dbt, lake

ICEBERG = {"name": "lake", "kind": "iceberg",
           "config": {"catalog_uri": "http://iceberg-rest:8181", "warehouse": "lake", "storage": "s3://lake",
                      "s3_endpoint": "http://rustfs:9000", "region": "us-east-1"},
           "credentials": {"access_key_id": "AK", "secret_access_key": "S'K"}}
FILES = {"name": "lake-files", "kind": "s3",
         "config": {"endpoint_url": "https://s3.example.com", "bucket": "files", "prefix": "finance/"},
         "credentials": {"access_key_id": "AK", "secret_access_key": "SK"}}


def files_without_client(info):
    f = lake.Files.__new__(lake.Files)
    f.name, f.bucket, f.prefix = info["name"], info["config"]["bucket"], lake._clean(info["config"]["prefix"])
    return f


def test_paths_stay_inside_the_prefix():
    f = files_without_client(FILES)
    assert f.key("reports/today.json") == "finance/reports/today.json"
    assert f.key("/a//b/./c.txt") == "finance/a/b/c.txt"
    assert f.url("x.json") == "s3://files/finance/x.json"
    with pytest.raises(ValueError):
        f.key("../other-team/secret.json")


def test_dbt_profile_for_governed_iceberg_uses_verified_trino(tmp_path, monkeypatch):
    monkeypatch.delenv('FDP_SQL_URL', raising=False)
    monkeypatch.delenv('FDP_API', raising=False)
    (tmp_path / "dbt_project.yml").write_text("name: lake\nprofile: lakehouse\n")
    info = {'name': 'lake', 'kind': 'trino', 'config': {'endpoint': 'https://fdp.example.com', 'catalog': 'lake'},
            'credentials': {'username': 'service:test', 'token': 'scoped-session'}}
    prof = dbt.profile("lake", "dbt_ana", "dev", 2, project_dir=str(tmp_path), info=info)
    out = prof["lakehouse"]["outputs"]["dev"]
    assert out['type'] == 'trino' and out['database'] == 'lake' and out['schema'] == 'dbt_ana'
    assert out['http_scheme'] == 'https' and out['cert'] is True
    assert out['http_headers'] == {'Authorization': 'Bearer scoped-session'}
    assert 'secrets' not in out and 'attach' not in out


def test_dbt_profile_cli_generates_governed_profile_without_legacy_env_setup(tmp_path, monkeypatch, capsys):
    from fdp import cli
    (tmp_path/'dbt_project.yml').write_text('name: lake\nprofile: lakehouse\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('FDP_SQL_URL', raising=False)
    monkeypatch.delenv('FDP_API', raising=False)
    monkeypatch.setattr(dbt, 'credentials', lambda *args, **kwargs: {
        'name': 'lake', 'kind': 'trino', 'config': {'endpoint': 'https://fdp.example.com', 'catalog': 'lake'},
        'credentials': {'username': 'service:test', 'token': 'scoped-session'}})
    monkeypatch.setattr(sys, 'argv', ['fdp', 'dbt-profile', '--source', 'lake', '--schema', 'analytics', '--target', 'dev'])
    cli.main()
    output = json.loads(capsys.readouterr().out)['lakehouse']['outputs']['dev']
    assert output['type'] == 'trino' and output['schema'] == 'analytics'
    assert output['cert'] is True and output['http_scheme'] == 'https'


@pytest.mark.parametrize('info', [ICEBERG, FILES])
def test_dbt_rejects_direct_storage_profiles(tmp_path, info):
    (tmp_path / 'dbt_project.yml').write_text('name: lake\nprofile: lakehouse\n')
    with pytest.raises(SystemExit, match='governed Trino endpoint'):
        dbt.profile(info['name'], 'dbt_ana', project_dir=str(tmp_path), info=info)
