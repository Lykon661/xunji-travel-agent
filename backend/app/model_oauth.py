"""Local OAuth session coordinator. Public state never includes access or refresh tokens."""
from copy import deepcopy
import json
import shutil
import subprocess
from threading import RLock, Thread
from time import monotonic
from uuid import uuid4

from .config import PROJECT_ROOT as ROOT
from .llm import ModelRuntimeError
from .model_registry import OAUTH_PROVIDERS


class ModelOAuthManager:
    def __init__(self, store):
        self.store = store
        self.lock = RLock()
        self.sessions = {}

    def start(self, values):
        provider = self.store.oauth_candidate(values)
        with self.lock:
            for identity, session in list(self.sessions.items()):
                if session['state']['status'] not in ('pending', 'waiting_input') and monotonic() - session['created'] > 900:
                    self.sessions.pop(identity)
            if any(item['state']['status'] in ('pending', 'waiting_input') for item in self.sessions.values()):
                raise ValueError('已有订阅登录正在进行，请先完成或取消。')
            node = shutil.which('node')
            if not node:
                raise ModelRuntimeError('未找到 Node.js，请安装运行依赖后重启后端。')
            process = subprocess.Popen([node, str(ROOT / 'model-runtime' / 'oauth-bridge.mjs')],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', cwd=ROOT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            identity = str(uuid4())
            state = {'id': identity, 'provider_id': provider['id'], 'provider_name': provider['name'],
                'status': 'pending', 'events': [], 'prompt': None, 'message': '正在准备订阅登录…'}
            session = {'process': process, 'state': state, 'created': monotonic()}
            self.sessions[identity] = session
            try:
                self._send(session, {'action': 'login', 'provider': provider['provider'],
                    'credentials_file': str(self.store.oauth_path(provider['id']))})
            except Exception:
                process.kill(); process.wait(); process.stdin.close(); process.stdout.close()
                self.sessions.pop(identity)
                raise
            Thread(target=self._read, args=(identity,), daemon=True).start()
            return deepcopy(state)

    @staticmethod
    def _send(session, value):
        stream = session['process'].stdin
        stream.write(json.dumps(value, ensure_ascii=False) + '\n')
        stream.flush()

    def _read(self, identity):
        session = self.sessions[identity]
        process = session['process']
        try:
            for line in process.stdout:
                event = json.loads(line)
                with self.lock:
                    state = session['state']
                    if state['status'] == 'cancelled':
                        continue
                    if event['type'] == 'event':
                        state['events'] = (state['events'] + [event['event']])[-30:]
                        state['message'] = '请按照授权页面和下方提示完成登录。'
                    elif event['type'] == 'prompt':
                        state['prompt'] = {'id': event['prompt_id'], **event['prompt']}
                        state['status'] = 'waiting_input'
                    elif event['type'] == 'prompt_cancelled':
                        if state['prompt'] and state['prompt']['id'] == event['prompt_id']:
                            state['prompt'] = None
                            state['status'] = 'pending'
                    elif event['type'] == 'logged_in':
                        state.update(status='connected', prompt=None, events=[], message='订阅登录成功，凭据已保存在本机。')
                    elif event['type'] == 'error':
                        state.update(status='cancelled' if event.get('cancelled') else 'failed', prompt=None,
                            events=[], message=event['message'])
        except Exception:
            with self.lock:
                if session['state']['status'] in ('pending', 'waiting_input'):
                    session['state'].update(status='failed', prompt=None, events=[], message='订阅登录进程异常，请确认模型运行依赖已安装后重试。')
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            process.stdin.close()
            with self.lock:
                if session['state']['status'] in ('pending', 'waiting_input'):
                    session['state'].update(status='failed', prompt=None, events=[], message='订阅登录进程提前结束，请重试。')

    def public(self, identity):
        with self.lock:
            if identity not in self.sessions:
                raise KeyError(identity)
            return deepcopy(self.sessions[identity]['state'])

    def answer(self, identity, prompt_id, value):
        with self.lock:
            session = self.sessions.get(identity)
            if not session:
                raise KeyError(identity)
            prompt = session['state']['prompt']
            if not prompt or prompt['id'] != prompt_id:
                raise ValueError('此登录步骤已结束，请按当前提示继续。')
            self._send(session, {'action': 'answer', 'prompt_id': prompt_id, 'value': value})
            session['state'].update(prompt=None, status='pending')
            return deepcopy(session['state'])

    def cancel(self, identity):
        with self.lock:
            session = self.sessions.get(identity)
            if not session:
                raise KeyError(identity)
            if session['state']['status'] not in ('pending', 'waiting_input'):
                return deepcopy(session['state'])
            session['state'].update(status='cancelled', prompt=None, events=[], message='登录已取消。')
            try:
                self._send(session, {'action': 'cancel'})
            except (OSError, ValueError):
                pass
            if session['process'].poll() is None:
                session['process'].kill()
            return deepcopy(session['state'])

    def cancel_provider(self, provider_id):
        with self.lock:
            for identity, session in self.sessions.items():
                if session['state']['provider_id'] == provider_id:
                    self.cancel(identity)

    def logout(self, provider_id, provider_type):
        self.cancel_provider(provider_id)
        if provider_type not in OAUTH_PROVIDERS:
            return
        path = self.store.oauth_path(provider_id)
        if not path.exists():
            return
        node = shutil.which('node')
        if not node:
            raise ModelRuntimeError('未找到 Node.js，无法清除订阅凭据。')
        with subprocess.Popen([node, str(ROOT / 'model-runtime' / 'oauth-bridge.mjs')], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', cwd=ROOT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
            try:
                output, _ = process.communicate(json.dumps({'action': 'logout', 'provider': provider_type,
                    'credentials_file': str(path)}) + '\n', timeout=45)
            except subprocess.TimeoutExpired as exc:
                process.kill(); process.communicate()
                raise ModelRuntimeError('清除订阅凭据超时，请稍后重试。') from exc
        events = [json.loads(line) for line in output.splitlines() if line.strip()]
        if not any(event.get('type') == 'logged_out' for event in events):
            raise ModelRuntimeError('无法清除订阅凭据，请检查本机数据目录权限后重试。')
