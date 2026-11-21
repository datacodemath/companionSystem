"""Run on the VPS; change only MCP and retain exact previous compose files."""
import json
from pathlib import Path
import subprocess

root = Path('/opt/profile-os')
release = root / 'releases/agy-get-20261121'
container = json.loads(subprocess.check_output(['docker', 'inspect', 'profile-os-mcp-1']))[0]
assert container['Config']['Image'] == 'profile-os-mcp:closeout-search-20261114'
original = subprocess.check_output(['docker', 'exec', 'profile-os-mcp-1', 'cat', '/app/profile_os/mcp_server.py'])
assert original == (release / 'mcp_server.before.py').read_bytes(), 'Concurrent source change'
files = container['Config']['Labels']['com.docker.compose.project.config_files'].split(',')
assert all(Path(f).is_file() for f in files)
commands = {}
for name, image in [('compose.json', 'profile-os-mcp:agy-get-20261121'),
                    ('compose.rollback.json', container['Config']['Image'])]:
    path = release / name
    path.write_text(json.dumps({'services': {'mcp': {'image': image}}}, indent=2) + '\n')
    command = ['docker', 'compose']
    for f in [*files, str(path)]:
        command += ['-f', f]
    commands[name] = command + ['up', '-d', '--no-build', '--no-deps', '--wait', '--wait-timeout', '60', 'mcp']
(release / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
try:
    subprocess.run(commands['compose.json'], cwd=root, check=True)
    with (release / 'verify.py').open('rb') as script:
        result = subprocess.run(['docker', 'exec', '-i', '-e', 'PYTHONPATH=/app',
            'profile-os-mcp-1', 'python', '-', '--live'], stdin=script,
            stdout=subprocess.PIPE, check=True)
    (release / 'live-verification.json').write_bytes(result.stdout)
    print(result.stdout.decode())
except Exception:
    subprocess.run(commands['compose.rollback.json'], cwd=root, check=True)
    raise
