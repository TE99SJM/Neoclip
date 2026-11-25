"""Deploy the shared queue to Samuel's existing main and test containers."""
import argparse
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

APP = 'NeoClip-code-before-cleanup-20261028-164840/NeoClip-Web/neoclip-web/app'


def run(args, cwd=None, capture=False):
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def api(port, endpoint):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/{endpoint}', timeout=10) as response:
        return json.load(response)


def mount(service, target):
    entries = [v for v in service.get('volumes', []) if v.get('target') == target]
    if len(entries) != 1 or entries[0].get('type') != 'bind':
        raise RuntimeError(f'Expected one bind mount for {target}')
    return Path(entries[0]['source']).resolve()


def check_renders():
    for name, port in (('neoclip-web',8000),('neoclip-test-server',8001)):
        running = run(['docker','inspect','-f','{{.State.Running}}',name],capture=True).strip()
        if running == 'true':
            status = api(port, 'render/status').get('status')
            if status not in ('idle','done','cancelled','error'):
                raise RuntimeError(f'{name} is rendering. Let it finish, then run this command again.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--main-ref', required=True)
    parser.add_argument('--test-ref', required=True)
    args = parser.parse_args()
    production = Path.home()/'NeoClip-Web'
    test = Path.home()/'neoclip-test-server'
    repo = test/'repo'
    main_compose = ['docker', 'compose', '-f', str(production/'compose.yaml')]
    test_compose = ['docker', 'compose', '-f', str(test/'compose.json')]
    config = json.loads(run(main_compose+['config','--format','json'], cwd=production, capture=True))
    test_config = json.loads(run(test_compose+['config','--format','json'], cwd=test, capture=True))
    main_service = config['services']['neoclip']
    test_service = test_config['services']['neoclip-test']
    main_data, test_data = mount(main_service, '/data'), mount(test_service, '/data')
    if main_data == test_data:
        raise RuntimeError('Main and test need separate /data mounts for their proxy caches')
    if main_service.get('environment', {}).get('NEOCLIP_QUEUE_FILE', '/data/neoclip_queue.json') != '/data/neoclip_queue.json':
        raise RuntimeError('Main uses a custom queue path; review it before updating')
    if mount(test_service, '/app/app') != (repo/APP).resolve():
        raise RuntimeError('Test app mount differs from the expected repository path')
    if run(['git','status','--porcelain'], cwd=repo, capture=True).strip():
        raise RuntimeError('Test checkout has local edits; keep them and review before switching')
    for ref in (args.main_ref, args.test_ref):
        run(['git','cat-file','-e',ref+':'+APP+'/queue_store.py'], cwd=repo)
    check_renders()

    # Preserve every existing test setting, especially media, exports and GPU.
    service = test_config['services']['neoclip-test']
    service.setdefault('environment', {})['NEOCLIP_QUEUE_FILE'] = '/main-data/neoclip_queue.json'
    service['volumes'] = [v for v in service['volumes'] if v.get('target') != '/main-data']
    service['volumes'].append({'type':'bind','source':str(main_data),'target':'/main-data'})

    with tempfile.TemporaryDirectory(prefix='neoclip-shared-') as scratch:
        scratch = Path(scratch)
        prepared = scratch/'compose.json'
        prepared.write_text(json.dumps(test_config, indent=2)+'\n')
        run(['docker','compose','-f',str(prepared),'config','--quiet'])
        archive = subprocess.check_output(['git','archive',args.main_ref+':'+APP], cwd=repo)
        source = scratch/'app'
        source.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(source, filter='data')
        shutil.copytree(source, production/'app', dirs_exist_ok=True)
        print('Building the main app before stopping either container...', flush=True)
        run(main_compose+['build','neoclip'], cwd=production)
        check_renders()
        print('Connecting both apps to the main clip queue...', flush=True)
        run(test_compose+['stop','neoclip-test'], cwd=test)
        run(main_compose+['stop','neoclip'], cwd=production)
        # Run with the image's UID so permissions match existing container files.
        run(main_compose+['run','--rm','--no-deps','--entrypoint','python',
                         '--volume',str(test_data)+':/existing-test:ro','neoclip',
                         '-m','app.queue_store','/data/neoclip_queue.json',
                         '--import-from','/existing-test/neoclip_queue.json'], cwd=production)
        run(['git','switch','--detach',args.test_ref], cwd=repo)
        (test/'compose.json').write_text(prepared.read_text())
        run(main_compose+['up','-d','--force-recreate','--wait','--wait-timeout','90','neoclip'], cwd=production)
        run(test_compose+['up','-d','--force-recreate','--wait','--wait-timeout','90','neoclip-test'], cwd=test)

    for label, port, expected in (('MAIN',8000,'/data/neoclip_queue.json'),('TEST',8001,'/main-data/neoclip_queue.json')):
        status = api(port,'status')
        if status.get('queue_file') != expected:
            raise RuntimeError(f'{label} did not start with the shared queue configuration')
        state = api(port,'state')
        print(f'{label}: {state["build"]} — {state["total_queued_clips"]} queued clips')
    print('Both apps now use:', main_data/'neoclip_queue.json')
    print('Reload both web apps once to load the updated controls.')


if __name__ == '__main__':
    main()
