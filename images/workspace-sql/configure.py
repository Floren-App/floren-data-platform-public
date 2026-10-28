"""Apply managed authentication settings without discarding saved CloudBeaver state."""
import json
import sys
from pathlib import Path

AUTH = {"id":"fdp","provider":"reverseProxy","displayName":"Floren Data Platform","disabled":False,
        "parameters":{"user-header":"X-FDP-User","team-header":"X-FDP-Team",
                      "full-name-header":"X-FDP-Display-Name","auto-user-provisioning":True}}


def configure(state):
    path=Path(state)/'.data/.cloudbeaver.runtime.conf'
    if not path.exists():
        return
    config=json.loads(path.read_text())
    app=config.setdefault('app',{})
    app['anonymousAccessEnabled']=False
    app['authConfigurations']=[c for c in app.get('authConfigurations',[]) if c.get('id')!='fdp']+[AUTH]
    app['enabledAuthProviders']=list(dict.fromkeys([*app.get('enabledAuthProviders',[]),'reverseProxy']))
    path.write_text(json.dumps(config,indent=2)+'\n')


if __name__=='__main__':
    configure(sys.argv[1])
