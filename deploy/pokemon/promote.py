#!/usr/bin/env python3
"""Promote the exact successful Test commit through a protected Gitea pull request.

Usage: python deploy/pokemon/promote.py --approve --expected-sha <full-test-sha>
Requires an interactive Floren Data Platform platform admin and successful Test ingestion/build at that SHA.
The local integration account performs the approved Git merge; Ana cannot push or merge main.
"""
import argparse
import os
import setup as s


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--approve',action='store_true')
    parser.add_argument('--expected-sha',required=True)
    args=parser.parse_args()
    s.ADMIN=s.login(os.environ.get('FDP_USERNAME','admin'),os.environ.get('FDP_PASSWORD','admin'))
    assert s.request('GET','/v1/me')['is_admin'],'platform admin required for promotion'
    head=s.gitea('GET','/repos/fdp/pokemon/branches/test')['commit']['id']
    assert head==args.expected_sha,'Test moved: review the new commit before approving'
    runs=s.request('GET','/v1/runs?limit=500')
    for program in ('ingest-test','models-test'):
        assert any(r.get('commit_sha')==head and r['program']==program and r['status']=='succeeded'
            and (program!='models-test' or r.get('args',{}).get('command')=='build') for r in runs), 'successful Test '+program+' required at exact commit'
    existing=s.gitea('GET','/repos/fdp/pokemon/pulls?state=open')
    pr=next((p for p in existing if p['head']['ref']=='test' and p['base']['ref']=='main'),None)
    if not pr:
        pr=s.gitea('POST','/repos/fdp/pokemon/pulls',{'head':'test','base':'main','title':'Promote Pokemon '+head[:12]+' to Prod',
            'body':'Promote the exact Test commit after successful ingestion and dbt build. Prod uses its own protected identities, API and data source; no Test data or credentials are copied.'})
    print('Review:',pr['html_url'].replace('http://gitea:3000','http://localhost:3000'))
    if not args.approve:return
    assert s.gitea('GET','/repos/fdp/pokemon/branches/test')['commit']['id']==head,'Test moved during approval'
    s.gitea('POST',f"/repos/fdp/pokemon/pulls/{pr['number']}/merge",{'Do':'merge','head_commit_id':head,'merge_commit_title':'Promote verified Pokemon '+head[:12]})
    print('Approved. GitOps will deploy main to Prod; Test and Prod permissions remain independent.')

if __name__=='__main__':main()
