import importlib.util
import json
from pathlib import Path


def test_managed_cloudbeaver_auth_updates_keep_saved_settings_and_scripts(tmp_path):
    spec=importlib.util.spec_from_file_location('sql_config',Path(__file__).resolve().parents[3]/'images/workspace-sql/configure.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    state=tmp_path/'.data';state.mkdir()
    path=state/'.cloudbeaver.runtime.conf'
    path.write_text(json.dumps({'server':{'serverName':'Queries'},'app':{'anonymousAccessEnabled':True,
        'enabledAuthProviders':['local'],'authConfigurations':[{'id':'custom','provider':'local'}],
        'productSettings':{'saved-setting':True}}}))
    saved=tmp_path/'user-projects/owner/script.sql';saved.parent.mkdir(parents=True);saved.write_text('SELECT 42')
    module.configure(tmp_path);module.configure(tmp_path)
    config=json.loads(path.read_text())
    assert config['server']=={'serverName':'Queries'}
    assert config['app']['anonymousAccessEnabled'] is False
    assert config['app']['productSettings']=={'saved-setting':True}
    assert config['app']['enabledAuthProviders']==['local','reverseProxy']
    assert len(config['app']['authConfigurations'])==2
    assert config['app']['authConfigurations'][1]['parameters']['team-header']=='X-FDP-Team'
    assert saved.read_text()=='SELECT 42'
