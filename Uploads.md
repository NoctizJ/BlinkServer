# File Uploads

Upload photos, videos, or any files to the server with a `multipart/form-data`
POST. The `file_upload` job saves each part to the gitignored `files/` folder
under a timestamped, collision-safe name.

## Endpoint

`POST /webhook/upload` — **requires the shared secret** (`X-Webhook-Secret`
header). Send one or more file parts in a form body. Field names can be
anything; multiple files are all saved.

Response:

```json
{
  "status": "ok",
  "result": {
    "status": "ok",
    "count": 1,
    "files": [
      { "stored_as": "20260711-104530-123456_photo.jpg", "original": "photo.jpg", "bytes": 20481 }
    ]
  }
}
```

## From an iPhone Shortcut

1. Add the **Get Contents of URL** action.
2. Set the URL to `http://<your-server>:5050/webhook/upload`.
3. Tap **Show More**.
4. **Method**: `POST`.
5. **Headers**: add `X-Webhook-Secret` = your secret (from `webhook_secret.json`).
6. **Request Body**: `Form`.
7. Tap **Add new field → File**, name it `file`, and pick the value — e.g. a
   Photos variable, a "Shortcut Input" (for a Share Sheet shortcut), or "Take
   Photo". Add more File fields to upload several at once.
8. Run the shortcut — the file uploads and lands in `files/`.

> Tip: to send whatever you share into the shortcut, enable **Receive
> Images/Media/Files** in the shortcut settings and use **Shortcut Input** as
> the File value.

## From curl

```bash
# Single file
curl -X POST http://localhost:5050/webhook/upload \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -F "file=@/path/to/photo.jpg"

# Multiple files
curl -X POST http://localhost:5050/webhook/upload \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -F "file=@photo.jpg" -F "file=@video.mov"
```

## Where files go

- Saved to `files/` at the repo root, which is **gitignored** — uploads are
  never committed.
- Each file is renamed to `<timestamp>_<sanitized-original-name>` so uploads
  never overwrite each other. Filenames are sanitized with
  `werkzeug.utils.secure_filename`.
- Every upload is recorded in the log under the `upload` type — it goes to
  `logs/upload.log` (see [Logging.md](Logging.md)).

## Notes

- There is no upload size cap by default, so large videos work. To add one, set
  `app.config["MAX_CONTENT_LENGTH"]` in `app.py` (returns `413` when exceeded).
- The job is enabled/disabled like any other via `/jobs/file_upload/...`.
- File parts without a filename (as Shortcuts sometimes send) are still saved,
  with the extension inferred from the MIME type (e.g. `..._upload.jpg`).

## Troubleshooting

If the response is `{"error": "no files", ...}`, it includes a `debug` object
showing what actually arrived:

- `content_type` is `application/json` → the Shortcut's **Request Body** is
  still JSON; switch it to **Form**.
- `form_fields` lists your field but `file_fields` is empty → the field is a
  **Text** field; change its type to **File**.
- both empty → no field was attached, or the request never carried a body.

## Testing

```bash
python3 test_file_upload.py    # requires Flask (run in your venv)
```
