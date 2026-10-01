# Instagram Safety Monitor

A local, AI-assisted dashboard that reviews Instagram comments and media for potential cyberbullying.

The monitor combines Instagram ingestion with specialized models for text, vision, speech, and native
video understanding. Flagged activity is shown with an explicit red **Bullying detected** alert,
severity, confidence, and a short explanation.

> [!WARNING]
> Instagram access uses Meta's official Instagram API with Instagram Login. Use a Professional
> (Business or Creator) account and comply with Instagram's terms and all applicable privacy laws.

## Features

- Continuous polling for new comments and recent media
- Text and image moderation with Azure OpenAI
- Voice-note and video-audio transcription with Sarvam AI Saaras v4
- Native video analysis with Vertex AI Gemini 3.8 Flash for videos up to 60 seconds
- Automatic Azure frame-analysis fallback when Gemini is unavailable
- Per-account session, seen-item, and event isolation
- Browser-based Instagram OAuth account connection
- Responsive, keyboard-accessible local dashboard with alert filters
- No cloud credentials committed to the repository

## How it works

```mermaid
flowchart LR
    A[Instagram account] --> B[Official Instagram API]
    B --> C{New content type}
    C -->|Comment or DM text| D[Azure OpenAI]
    C -->|Image| D
    C -->|Voice note| E[Sarvam transcription]
    E --> D
    C -->|Video up to 60 seconds| F[Sarvam transcript]
    C -->|Complete video| G[Gemini 3.8 Flash]
    F --> G
    G -->|If unavailable| H[Azure sampled-frame fallback]
    D --> I[Structured safety result]
    G --> I
    H --> I
    I --> J[Local event history]
    J --> K[Red dashboard alert]
```

| Content               | Primary analysis                        | Fallback                         |
| --------------------- | --------------------------------------- | -------------------------------- |
| Comments and text DMs | Azure OpenAI                            | Recorded as analysis unavailable |
| Images                | Azure OpenAI vision                     | Recorded as analysis unavailable |
| Voice notes           | Sarvam transcript, then Azure OpenAI    | Recorded as analysis unavailable |
| Videos                | Gemini 3.8 Flash with Sarvam transcript | Azure OpenAI over sampled frames |

Every AI result is normalized to the same schema: bullying status, confidence, severity, reason, and
categories. The application reports potential harm; it does not delete, hide, reply to, or otherwise
moderate Instagram content automatically.

## Requirements

- Python 3.11 or newer
- FFmpeg available on `PATH`
- Azure CLI authenticated to an Azure OpenAI resource
- Google Cloud CLI authenticated to a billing-enabled project with Vertex AI enabled
- Sarvam AI API key
- Instagram account credentials

The dashboard is dependency-light: the web server uses Python's standard library and the monitor uses
the packages pinned in `requirements.txt`.

## Setup

### 1. Create the Python environment

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. Authenticate the cloud CLIs

```powershell
az login
gcloud auth login
gcloud config set project YOUR_GCP_PROJECT_ID
gcloud services enable aiplatform.googleapis.com
```

The Azure identity must be allowed to list keys for the configured Azure OpenAI resource. The Google
identity needs permission to call Vertex AI models in the selected project.

### 3. Configure the application

```powershell
Copy-Item .env.example .env
```

Edit `.env` and provide the following values:

| Variable                  | Required | Purpose                                                |
| ------------------------- | -------: | ------------------------------------------------------ |
| `IG_POLL_SECONDS`         |       No | Poll interval; defaults to `60`                        |
| `IG_APP_ID`               |      Yes | Meta Instagram app ID                                  |
| `IG_APP_SECRET`           |      Yes | Meta Instagram app secret; stored locally only         |
| `IG_REDIRECT_URI`         |      Yes | OAuth callback URL registered in Meta                  |
| `AZURE_OPENAI_GROUP`      |      Yes | Azure resource group                                   |
| `AZURE_OPENAI_RESOURCE`   |      Yes | Azure OpenAI account name                              |
| `AZURE_OPENAI_ENDPOINT`   |      Yes | Azure OpenAI endpoint URL                              |
| `AZURE_OPENAI_DEPLOYMENT` |       No | Vision-capable deployment; defaults to `gpt-4o`        |
| `SARVAM_API_KEY`          |      Yes | Sarvam speech-to-text credential                       |
| `GOOGLE_CLOUD_PROJECT`    |       No | GCP project; falls back to the active `gcloud` project |
| `GOOGLE_CLOUD_LOCATION`   |       No | Vertex location; defaults to `global`                  |
| `GOOGLE_VIDEO_MODEL`      |       No | Video model; defaults to `gemini-3.8-flash`            |

Do not commit `.env`. It is intentionally ignored by Git.

### 4. Start the dashboard

```powershell
.\start-dashboard.cmd
```

Or run it directly:

```powershell
.\.venv\Scripts\python.exe dashboard.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Click **Connect Instagram** and approve access through Meta.

## Monitoring behavior

Each polling cycle checks:

- Up to 100 recent posts (paginated)
- Up to 100 comments per post, requested in pages of at most 50, plus replies

Previously observed item IDs are stored per account under `tools/accounts/`, preventing duplicate
alerts. Events are also isolated per account and the dashboard reads the latest 200 records.

Signed Instagram webhooks at `/webhooks/instagram` accept comments and incoming messages, including
image/audio/video attachments. Events are saved before acknowledgment, then processed by the monitor.
Configure the public HTTPS URL and verify token shown in the local dashboard in Meta, and subscribe
the account to comments/messages. Meta must permit event delivery for your app/account state.
A working local handler does not imply Meta has enabled delivery.

The dashboard is intended for local use. Cloudflare-forwarded requests may reach OAuth and webhook
routes only; event data and setup values are blocked on the public tunnel. The quick-tunnel URL changes
when restarted; update Meta and the local configuration together.

Videos are normalized locally to MP4, limited to 60 seconds, and sent inline to Vertex AI. Sarvam
receives mono 16 kHz audio in chunks of at most 25 seconds. Downloaded media and conversion files are
temporary and deleted after processing.

## Verification

Run the local checks before committing:

```powershell
.\.venv\Scripts\python.exe -m py_compile dashboard.py examples\comment_monitor.py examples\azure_moderation.py
.\.venv\Scripts\python.exe dashboard.py --self-test
.\.venv\Scripts\python.exe examples\comment_monitor.py --self-test
.\.venv\Scripts\python.exe examples\azure_moderation.py
npm run build
```

The self-tests do not call Instagram or paid AI APIs. End-to-end cloud checks require the configured
accounts and may incur usage charges.

## Security and privacy

- The HTTP server binds only to `127.0.0.1`; do not expose it directly to a public network.
- Instagram app secrets and Sarvam credentials stay in the ignored local `.env` file or environment.
- OAuth tokens are stored locally, with expiry tracking and refresh when nearing expiration.
- Azure credentials are obtained at runtime through Azure CLI; no Azure key is written by the app.
- Vertex credentials are obtained at runtime through Google Cloud CLI; no service-account key is added.
- Instagram content is transmitted to Azure OpenAI, Sarvam AI, and Google Vertex AI for analysis.
- Session files, event history, temporary media, virtual environments, and build output are ignored.

Review the providers' retention, regional processing, and data-governance terms before monitoring real
people or deploying beyond a controlled test environment.

## Troubleshooting

### Monitor stopped

If the dashboard works but reports **Monitor stopped**, the official token may be missing or expired.
Click **Connect Instagram** again and approve access.

### AI analysis unavailable

Confirm all three provider credentials are active:

```powershell
az account show
gcloud auth list --filter=status:ACTIVE
gcloud config get-value project
```

Also confirm `SARVAM_API_KEY` is set and `ffmpeg -version` succeeds.

## Project layout

```text
dashboard.py                    Local HTTP server and account switching
dashboard/index.html            Monitoring interface
examples/comment_monitor.py     Official Instagram polling and moderation pipeline
examples/instagram_graph.py     Official Instagram API client
examples/instagram_store.py     Atomic account storage and webhook normalization
tests/test_monitor.py           Offline ingestion regression checks
examples/azure_moderation.py    Azure, Sarvam, Gemini, and media processing
requirements.txt                Pinned Python dependencies
start-dashboard.cmd             Windows launcher
```

The active safety-monitor application is Python-based. The legacy TypeScript private-API source remains
in `src/` for compatibility but is not imported by the dashboard.

## License

MIT. See `LICENSE` for the complete terms and retained notices.

### Verification status and limits

Run `.\\.venv\\Scripts\\python.exe tests/test_monitor.py` for offline regression checks.
The dashboard reports last scan time and errors, rather than treating a live process as a successful scan.
Comments remain visible when analysis fails, labeled unknown; repeated attempts replace their display entry.
An empty Meta comments response is reported explicitly if the media reports a positive comment count.
Webhook delivery still requires configuration in Meta. No guarantee is made that App Review alone resolves an empty response.
