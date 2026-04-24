import os
import re
import subprocess
import time
from pathlib import Path

cwd = '/Users/chenjunming/Desktop/KevinGit/AutoCodex/CanopyAutoCoding'
list_script = str(Path.home() / '.claude' / 'list-models.js')
raw = subprocess.check_output(['node', list_script], text=True)
models = []
for line in raw.splitlines():
    m = re.match(r'^-\s+(.+?)\s*$', line)
    if m:
        model = m.group(1)
        if model not in models:
            models.append(model)

env = os.environ.copy()
for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
    env[key] = 'http://127.0.0.1:10900'

print(f'FOUND {len(models)} models')
print('PROXY http://127.0.0.1:10900')
print('')
results = []
for i, model in enumerate(models, 1):
    cmd = ['claude', '--model', model, '--permission-mode', 'bypassPermissions', '--effort', 'low', '-p', '只输出 OK']
    print(f'[{i:02d}/{len(models)}] {model} ... ', end='', flush=True)
    start = time.time()
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        elapsed = time.time() - start
        output = (p.stdout or '').strip()
        first = ' '.join(output.split())[:240]
        if p.returncode == 0:
            status = 'OK'
            reason = first or 'exit 0'
        else:
            status = 'FAIL'
            reason = first or f'exit {p.returncode}'
        print(f'{status} ({elapsed:.1f}s) {reason}')
        results.append((model, status, p.returncode, elapsed, reason))
    except subprocess.TimeoutExpired as e:
        elapsed = time.time() - start
        out = e.stdout or ''
        if isinstance(out, bytes):
            out = out.decode(errors='replace')
        first = ' '.join(out.strip().split())[:240]
        print(f'TIMEOUT ({elapsed:.1f}s) {first}')
        results.append((model, 'TIMEOUT', 124, elapsed, first))

print('\nSUMMARY')
for model, status, code, elapsed, reason in results:
    print(f'{status}\t{model}\texit={code}\ttime={elapsed:.1f}s\t{reason}')