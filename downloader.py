#!/usr/bin/env python3

import imaplib
import email
import logging
import hashlib
import os
import subprocess
import tempfile
import sys
import re
import shutil
#import requests
from email.policy import default
from pathlib import Path
from dotenv import load_dotenv
from pypdf import PdfWriter, PdfReader

load_dotenv()

# =========================
# CONFIG
# =========================
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
GMAIL_USERNAME = os.environ["GMAIL_USERNAME"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

ATTACHMENT_DIR = Path(os.getenv("ATTACHMENT_DIR", "/mnt/user/paperless/consume"))
TARGET_LABEL = os.getenv("TARGET_LABEL", "(Paperless Receipts)")
LABEL_INVOICE = os.getenv("LABEL_INVOICE", "Invoice")
LABEL_RECEIPT = os.getenv("LABEL_RECEIPT", "Receipt")

SUBJECT_KEYWORDS = [k.strip() for k in os.getenv("SUBJECT_KEYWORDS", "receipt,invoice,payment,billing statement").split(",")]
RECEIPT_EXTENSIONS = {e.strip() for e in os.getenv("RECEIPT_EXTENSIONS", ".pdf,.jpg,.jpeg,.png,.webp").split(",")}

HASH_DB = ATTACHMENT_DIR / ".receipt_hashes"
DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "ghcr.io/puppeteer/puppeteer:latest")
LOG_FILE = os.getenv("LOG_FILE", "/var/log/gmail_receipt_downloader.log")
PUPPETEER_WORKDIR = Path(os.getenv("PUPPETEER_WORKDIR", "/mnt/user/paperless/puppeteer"))

# not tested or in use
#PAPERLESS_API = os.getenv("PAPERLESS_API", "http://paperless:8000/api/documents/post_document/")
#PAPERLESS_TOKEN = os.getenv("PAPERLESS_TOKEN", "")

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)

# =========================
# UTILITIES
# =========================
def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def load_hashes():
    if HASH_DB.exists():
        return set(HASH_DB.read_text().splitlines())
    return set()

def save_hash(h):
    HASH_DB.write_text(HASH_DB.read_text() + h + "\n" if HASH_DB.exists() else h + "\n")

def subject_matches(msg):
    subject = msg.get("Subject", "").lower()
    return any(k in subject for k in SUBJECT_KEYWORDS)

def is_receipt_attachment(name):
    return Path(name.lower()).suffix in RECEIPT_EXTENSIONS

def upload_to_paperless(pdf_bytes, filename):
    r = requests.post(
        PAPERLESS_API,
        headers={"Authorization": f"Token {PAPERLESS_TOKEN}"},
        files={"document": (filename, pdf_bytes, "application/pdf")},
        timeout=30,
    )
    r.raise_for_status()

# =========================
# HTML SANITIZATION
# =========================
def sanitize_html(html: str) -> str:
    html = re.sub(r"<(script|iframe|object|embed).*?>.*?</\1>", "", html, flags=re.I | re.S)
    if "<html" not in html.lower():
        html = f"<html><body>{html}</body></html>"
    return html

def extract_inline_images(msg, html, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    created_files = []  # List to track the images we create

    for part in msg.walk():
        if part.get_content_maintype() == "image":
            cid = part.get("Content-ID")
            if not cid:
                continue
            cid = cid.strip("<>")

            # Use a simple filename relative to the target directory
            filename = part.get_filename(f"{cid}.img")
            img_path = target_dir / filename

            # Write the file and add it to our tracking list
            img_path.write_bytes(part.get_payload(decode=True))
            created_files.append(img_path)

            # Replace cid: with the filename so Docker can find it
            html = re.sub(rf"cid:{re.escape(cid)}", filename, html, flags=re.I)

    return html, created_files

def merge_pdfs(rendered_pdf_path: Path, attachment_pdf_path: Path, final_path: Path):
    writer = PdfWriter()

    # Add the rendered email body first
    reader_body = PdfReader(rendered_pdf_path)
    for page in reader_body.pages:
        writer.add_page(page)

    # Add the actual receipt attachment second
    reader_attach = PdfReader(attachment_pdf_path)
    for page in reader_attach.pages:
        writer.add_page(page)

    with open(final_path, "wb") as f:
        writer.write(f)

def render_html_to_pdf(html: str, pdf_path: Path):
    # Ensure we use the shared config directory
    workdir = PUPPETEER_WORKDIR
    workdir.mkdir(parents=True, exist_ok=True)

    input_html = workdir / "input.html"
    output_pdf = workdir / "output.pdf"
    render_js = workdir / "render.js"

    # Save the HTML file to the directory
    input_html.write_text(html, encoding="utf-8")

    # Update render.js to load the file directly (resolving local paths)
    render_js.write_text(
        """const puppeteer = require("puppeteer");

(async () => {
  const browser = await puppeteer.launch({
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  });

  const page = await browser.newPage();

  // 1. Set Desktop Viewport (prevents mobile layout rendering)
  await page.setViewport({ width: 1280, height: 1024 });

  // 2. Load the file via URL instead of setContent
  // This tells the browser: "The current directory is /work/"
  // so relative paths like <img src="image.jpg"> will be found.
  await page.goto("file:///work/input.html", {
    waitUntil: "networkidle0"
  });

  await page.pdf({
    path: "/work/output.pdf",
    format: "A4",
    printBackground: true,
    scale: 0.7,   // Scales content to fit width
    margin: {
        top: '20px', bottom: '20px', left: '20px', right: '20px'
    }
  });

  await browser.close();
})();"""
    )

    cmd = [
        "/usr/bin/docker",
        "run",
        "--rm",
        # "--network", "none",  <--- REMOVE THIS LINE (Fixes remote http/https images)
        "--user", "pptruser",
        "-v", f"{workdir}:/work",
        DOCKER_IMAGE,
        "node",
        "/work/render.js",
    ]

    subprocess.run(cmd, check=True)
    shutil.move(output_pdf, pdf_path)

# =========================
# MAIN
# =========================
def main():
    hashes = load_hashes()

    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(GMAIL_USERNAME, GMAIL_APP_PASSWORD)
    mail.select("INBOX")

    status, msgs = mail.search(None, "UNSEEN")
    if status != "OK":
        return

    for num in msgs[0].split():
        #status, data = mail.fetch(num, "(RFC822)")
        status, data = mail.fetch(num, "(BODY.PEEK[])")

        if status != "OK":
            continue

        msg = email.message_from_bytes(data[0][1], policy=default)
        subject = str(msg.get("Subject", "")).lower()
        # Determine the specific sub-label based on keywords
        specific_label = LABEL_INVOICE if "invoice" in subject else LABEL_RECEIPT
        message_id = msg.get("Message-ID", num.decode()).strip("<>")

        if not subject_matches(msg):
            logging.info("Skipped (subject): %s", subject)
            continue

        found_attachment_path = None
        temp_rendered_path = PUPPETEER_WORKDIR / f"body_{message_id}.pdf"
        final_output_path = ATTACHMENT_DIR / f"{message_id}_merged.pdf"

        # 1. Try to find/save the PDF attachment first
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            filename = part.get_filename()
            if filename and filename.lower().endswith(".pdf"):
                temp_attach_path = PUPPETEER_WORKDIR / f"attach_{message_id}.pdf"
                temp_attach_path.write_bytes(part.get_payload(decode=True))
                found_attachment_path = temp_attach_path
                break # Found the receipt

        # 2. Always Render the HTML Body
        html = next((p.get_content() for p in msg.walk() if p.get_content_type() == "text/html"), None)
        temp_images = []

        if html:
            html, temp_images = extract_inline_images(msg, html, PUPPETEER_WORKDIR)
            html = sanitize_html(html)
            try:
                render_html_to_pdf(html, temp_rendered_path)
            except Exception as e:
                logging.error(f"Failed to render HTML body: {e}")
                temp_rendered_path = None

        # 3. Decision Logic: Merge or Save Single
        try:
            receipt_saved = False
            if found_attachment_path and temp_rendered_path:
                logging.info("Merging email body and attachment...")
                merge_pdfs(temp_rendered_path, found_attachment_path, final_output_path)
                receipt_saved = True
            elif found_attachment_path:
                shutil.move(found_attachment_path, ATTACHMENT_DIR / f"{message_id}.pdf")
                receipt_saved = True
            elif temp_rendered_path:
                shutil.move(temp_rendered_path, ATTACHMENT_DIR / f"{message_id}.pdf")
                receipt_saved = True

            # 4. Final deduplication check on the resulting file
            if receipt_saved:
                final_file = ATTACHMENT_DIR / (f"{message_id}_merged.pdf" if (found_attachment_path and temp_rendered_path) else f"{message_id}.pdf")
                h = sha256(final_file.read_bytes())
                if h in hashes:
                    final_file.unlink()
                    logging.info("Duplicate detected, deleted.")
                    receipt_saved = False
                else:
                    save_hash(h)

        finally:
            # Cleanup tracked images and temp files
            for img in temp_images:
                img.unlink(missing_ok=True)
            if found_attachment_path: found_attachment_path.unlink(missing_ok=True)
            if temp_rendered_path: temp_rendered_path.unlink(missing_ok=True)
            (PUPPETEER_WORKDIR / "input.html").unlink(missing_ok=True)

        # ---------- Gmail state changes ----------
        if receipt_saved:
            # Add the specific Invoice or Receipt label
            mail.store(num, "+X-GM-LABELS", specific_label)

            # Explicitly mark as read
            mail.store(num, "+FLAGS", "\\Seen")

            mail.store(num, "-X-GM-LABELS", "\\Inbox")

            logging.info(f"Processed: {specific_label} saved, marked read, and ARCHIVED: {subject}")
        else:
            # Absolutely no Gmail mutations here
            logging.info("No receipt → left unread and unlabeled: %s", subject)

    #mail.expunge()
    mail.logout()

if __name__ == "__main__":
    main()
