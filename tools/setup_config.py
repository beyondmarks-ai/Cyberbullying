"""Local first-run credential form. Never sends credentials or provisions cloud resources."""
import argparse
from pathlib import Path
import sys
from urllib.parse import urlsplit

from launch_dashboard import update_env

ROOT = Path(__file__).resolve().parents[1]
FIELDS = [
    ('IG_APP_ID', 'Instagram App ID', False),
    ('IG_APP_SECRET', 'Instagram App Secret', True),
    ('AZURE_OPENAI_ENDPOINT', 'Azure OpenAI endpoint', False),
    ('AZURE_OPENAI_DEPLOYMENT', 'Azure model deployment name', False),
    ('AZURE_OPENAI_KEY', 'Azure OpenAI API key', True),
    ('GEMINI_API_PROVIDER', 'Google API provider', False),
    ('GEMINI_MODEL', 'Gemini model name', False),
    ('GEMINI_API_KEY', 'Google Gemini / Vertex API key', True),
    ('SARVAM_API_KEY', 'Sarvam API key (optional transcript)', True),
    ('AZURE_STORAGE_ACCOUNT', 'Azure storage account (optional previews)', False),
    ('AZURE_STORAGE_CONTAINER', 'Private storage container', False),
    ('AZURE_STORAGE_KEY', 'Azure storage key (optional previews)', True),
]
REQUIRED = {key for key, _, _ in FIELDS[:8]}


def load_values(path):
    values = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            key, separator, value = line.partition('=')
            if separator and not key.lstrip().startswith('#'):
                values[key.strip()] = value.strip().strip('\"\'')
    return values


def problems(values):
    issues = []
    for key, label, _ in FIELDS:
        value = values.get(key, '').strip()
        if key in REQUIRED and (not value or 'YOUR-' in value.upper()):
            issues.append(f'Enter {label}.')
        if '\n' in value or '\r' in value:
            issues.append(f'{label} must be one line.')
    endpoint = values.get('AZURE_OPENAI_ENDPOINT', '').strip()
    try:
        parsed = urlsplit(endpoint)
        if endpoint and (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
            issues.append('Azure OpenAI endpoint must be an HTTPS resource URL without credentials or query parameters.')
    except ValueError:
        issues.append('Azure OpenAI endpoint is not a valid URL.')
    if values.get('GEMINI_API_PROVIDER') not in ('gemini', 'vertex-express'):
        issues.append('Google provider must be gemini or vertex-express.')
    storage = [values.get(key, '').strip() for key in ('AZURE_STORAGE_ACCOUNT', 'AZURE_STORAGE_KEY')]
    if any(storage) and not all(storage):
        issues.append('For previews, enter both the Azure storage account and storage key, or leave both empty.')
    if all(storage) and not values.get('AZURE_STORAGE_CONTAINER', '').strip():
        issues.append('Enter the private Azure storage container name.')
    return issues


def configure(path):
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except ImportError:
        raise RuntimeError('Python needs Tcl/Tk support for the setup form. Install the standard Python 3.13 Windows distribution, or fill .env using README.md.') from None
    import webbrowser
    defaults = load_values(ROOT / '.env.example')
    defaults.update(load_values(path))
    window = tk.Tk()
    window.title('Instagram Safety Monitor - First-time setup')
    window.geometry('850x690')
    window.minsize(650, 480)
    canvas = tk.Canvas(window, highlightthickness=0)
    scroll = ttk.Scrollbar(window, orient='vertical', command=canvas.yview)
    canvas.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right', fill='y')
    canvas.pack(side='left', fill='both', expand=True)
    form = ttk.Frame(canvas, padding=20)
    content = canvas.create_window((0, 0), window=form, anchor='nw')
    form.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>', lambda event: canvas.itemconfigure(content, width=event.width))
    form.columnconfigure(1, weight=1)
    ttk.Label(form, text='Connect your services once', font=('Segoe UI', 17, 'bold')).grid(row=0, column=0, columnspan=2, sticky='w')
    ttk.Label(form, text='Ask the project administrator for these details, or follow the setup guide.\n'
              'Keys stay in .env on this PC. API use and Azure storage may incur charges.\n'
              'This form saves settings; it does not test keys or create cloud accounts.').grid(row=1, column=0, columnspan=2, sticky='w', pady=10)
    ttk.Button(form, text='Open setup guide', command=lambda: webbrowser.open('https://github.com/beyondmarks-ai/Cyberbullying#configuration-reference')).grid(row=2, column=0, columnspan=2, sticky='w', pady=5)
    entries = {}
    for row, (key, label, secret) in enumerate(FIELDS, start=3):
        ttk.Label(form, text=label).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=6)
        value = tk.StringVar(value=defaults.get(key, ''))
        entries[key] = value
        if key == 'GEMINI_API_PROVIDER':
            field = ttk.Combobox(form, textvariable=value, values=('gemini', 'vertex-express'), state='readonly')
        else:
            field = ttk.Entry(form, textvariable=value, show='*' if secret else '')
        field.grid(row=row, column=1, sticky='ew', pady=6)
    saved = False
    def save():
        nonlocal saved
        values = {key: variable.get().strip() for key, variable in entries.items()}
        issues = problems(values)
        if issues:
            messagebox.showerror('Check these settings', '\n'.join(issues), parent=window)
            return
        if not path.exists():
            path.write_bytes((ROOT / '.env.example').read_bytes())
        try:
            update_env(path, values)
        except OSError:
            messagebox.showerror('Could not save', 'This folder is not writable. Extract the project into your Documents folder.', parent=window)
            return
        saved = True
        window.destroy()
    ttk.Label(form, text='Next: the dashboard shows two URLs to save in Meta. Enable messages and comments.\n'
              'For previews, your administrator must also configure private Azure storage and retention.').grid(row=15, column=0, columnspan=2, sticky='w', pady=12)
    ttk.Button(form, text='Save and continue', command=save).grid(row=16, column=0, columnspan=2, sticky='e', pady=10)
    window.mainloop()
    return saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    path = ROOT / '.env'
    if args.check:
        issues = problems(load_values(path))
        for issue in issues:
            print(issue)  # Field labels only; never print credentials.
        return int(bool(issues))
    return 0 if configure(path) else 1


if __name__ == '__main__':
    sys.exit(main())
