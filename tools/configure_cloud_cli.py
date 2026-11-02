"""Populate local Azure OpenAI and Gemini API credentials from authenticated CLIs.

Secret values are written directly to .env and are never printed.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from launch_dashboard import update_env
from setup_config import load_values

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / '.env'
GOOGLE_KEY_NAME = 'cyberbullying-gemini-api'


def command(*args):
    executable = shutil.which(args[0] + '.cmd') if os.name == 'nt' else shutil.which(args[0])
    if not executable:
        raise RuntimeError(f'{args[0]} CLI is not installed or is not on PATH.')
    process = subprocess.run((executable, *args[1:]), cwd=ROOT, capture_output=True, text=True,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if process.returncode:
        raise RuntimeError(f"{args[0]} failed. Check that its CLI login and project/subscription are active.")
    return process.stdout.strip()


def main():
    values = load_values(ENV_FILE)
    group = values.get('AZURE_OPENAI_GROUP', '').strip()
    resource = values.get('AZURE_OPENAI_RESOURCE', '').strip()
    deployment = values.get('AZURE_OPENAI_DEPLOYMENT', '').strip()
    if not group or not resource or not deployment:
        raise RuntimeError('The existing .env needs AZURE_OPENAI_GROUP, AZURE_OPENAI_RESOURCE and AZURE_OPENAI_DEPLOYMENT.')

    azure_key = command('az', 'cognitiveservices', 'account', 'keys', 'list',
                        '--resource-group', group, '--name', resource,
                        '--query', 'key1', '--output', 'tsv', '--only-show-errors')
    azure_endpoint = command('az', 'cognitiveservices', 'account', 'show',
                             '--resource-group', group, '--name', resource,
                             '--query', 'properties.endpoint', '--output', 'tsv', '--only-show-errors').rstrip('/')
    if not azure_key or not azure_endpoint.startswith('https://'):
        raise RuntimeError('Azure CLI returned incomplete OpenAI credentials.')

    update_env(ENV_FILE, {
        'AZURE_OPENAI_KEY': azure_key,
        'AZURE_OPENAI_ENDPOINT': azure_endpoint,
        'AZURE_OPENAI_DEPLOYMENT': deployment,
    })

    projects = json.loads(command('gcloud', 'projects', 'list', '--format=json'))
    project_ids = [item.get('projectId') for item in projects if item.get('projectId')]
    if len(project_ids) != 1:
        raise RuntimeError('Select the intended Google Cloud project with gcloud config set project PROJECT_ID.')
    project = project_ids[0]
    command('gcloud', 'config', 'set', 'project', project, '--quiet')
    command('gcloud', 'services', 'enable', 'generativelanguage.googleapis.com', 'apikeys.googleapis.com',
            '--project', project, '--quiet')

    keys = json.loads(command('gcloud', 'services', 'api-keys', 'list', '--project', project, '--format=json'))
    matching = [item for item in keys if item.get('displayName') == GOOGLE_KEY_NAME]
    if not matching:
        command('gcloud', 'services', 'api-keys', 'create',
                f'--display-name={GOOGLE_KEY_NAME}', '--api-target=service=generativelanguage.googleapis.com',
                '--project', project, '--quiet', '--format=json')
        keys = json.loads(command('gcloud', 'services', 'api-keys', 'list',
                                  '--project', project, '--format=json'))
        matching = [item for item in keys if item.get('displayName') == GOOGLE_KEY_NAME]
    if len(matching) != 1 or not matching[0].get('name'):
        raise RuntimeError(f'Expected exactly one Google API key named {GOOGLE_KEY_NAME}.')
    google_key = command('gcloud', 'services', 'api-keys', 'get-key-string', matching[0]['name'],
                         '--project', project, '--format=value(keyString)', '--quiet')
    if not google_key:
        raise RuntimeError('Google Cloud CLI returned no Gemini API key string.')

    update_env(ENV_FILE, {
        'GOOGLE_CLOUD_PROJECT': project,
        'GEMINI_API_PROVIDER': 'gemini',
        'GEMINI_API_KEY': google_key,
        'GEMINI_MODEL': 'gemini-3.8-flash',
    })
    print('Configured Azure OpenAI and Gemini API credentials in .env (secret values hidden).')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Cloud configuration failed: {error}', file=sys.stderr)
        sys.exit(1)
