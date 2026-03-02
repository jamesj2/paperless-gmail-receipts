# Paperless Gmail Receipts

Automatically fetches receipt and invoice emails from Gmail via IMAP, renders email bodies to PDF using Puppeteer (in Docker), and deposits them into a Paperless-ngx consume directory for ingestion.

## How It Works

1. Connects to Gmail via IMAP and scans for unread emails
2. Matches emails by subject keywords (receipt, invoice, payment, billing statement)
3. Renders the HTML email body to PDF using a Dockerized Puppeteer instance
4. If a PDF attachment exists, merges the rendered body with the attachment
5. Saves the final PDF to the Paperless-ngx consume directory
6. Labels the email (Invoice or Receipt), marks it as read, and archives it
7. Deduplicates files using SHA-256 hashes

## Prerequisites

- Python 3.10+
- Docker (for Puppeteer HTML-to-PDF rendering)
- A Gmail account with an [App Password](https://support.google.com/accounts/answer/185833)
- Paperless-ngx (or any directory-based document consumer)

## Setup

1. Clone the repository:

   ```bash
   git clone https://github.com/jamesj2/paperless-gmail-receipts.git
   cd paperless-gmail-receipts

   # Create a virtual environment
   python3 -m venv venv
   source venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   #pip install -r requirements.txt
   docker exec -it paperless-ngx pip install \
     --target /usr/src/paperless/scripts/paperless-gmail-receipts/vendor \
     -r /usr/src/paperless/scripts/paperless-gmail-receipts/requirements.txt 2>/dev/null
   ```

3. Copy the example environment file and fill in your values:

   ```bash
   cp .env.example .env
   ```

4. Pull the Puppeteer Docker image:

   ```bash
   docker pull ghcr.io/puppeteer/puppeteer:latest
   ```

## Configuration

All configuration is managed through environment variables in `.env`. See `.env.example` for all available options.

| Variable | Description |
|---|---|
| `GMAIL_USERNAME` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | Gmail App Password (not your regular password) |
| `ATTACHMENT_DIR` | Path to the Paperless-ngx consume directory |
| `TARGET_LABEL` | Gmail label applied to matched emails |
| `SUBJECT_KEYWORDS` | Comma-separated keywords to match in subjects |
| `DOCKER_IMAGE` | Puppeteer Docker image for PDF rendering |
| `LOG_FILE` | Path to the log file |
| `PUPPETEER_WORKDIR` | Temp directory for Puppeteer working files |

## Usage

```bash
python downloader.py
```

For automated processing, set up a cron job:

```bash
# Run every 5 minutes
*/5 * * * * /usr/bin/python3 /path/to/downloader.py
```

## License

MIT
