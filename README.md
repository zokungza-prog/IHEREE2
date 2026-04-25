# IHERE LET'S GO!

Personal finance web app built with Flask and SQLite.

## Features

- Income and expense tracking
- Edit and delete entries
- Monthly charts and summaries
- Budget limits
- Recurring items
- Search and filters
- CSV export
- Simple built-in AI assistant
- Multi-page UI for Home, Add, AI, History, and Planner
- Monthly cycle reset that keeps only the saved running balance

## Run locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## Deploy to Render

This project is ready for Render with:

- `gunicorn` in `requirements.txt`
- environment-based `SECRET_KEY`
- environment-based `DATABASE_PATH`
- `render.yaml` included
- persistent disk path set to `/var/data/finance.db`

## Render steps

1. Create a new GitHub repo
2. Push this project to GitHub
3. Go to Render
4. Create a new `Blueprint` deployment
5. Connect the GitHub repo
6. Render will detect `render.yaml`
7. Deploy

## Important notes

- This app uses `SQLite`, so the Render disk is required if you want data to persist.
- Do not commit `finance.db` to Git.
- `SECRET_KEY` is generated automatically by Render from `render.yaml`.

## Main files

- `app.py`
- `templates/index.html`
- `static/styles.css`
- `requirements.txt`
- `render.yaml`
- `.gitignore`
