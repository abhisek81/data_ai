# data_ai

A collection of data and AI projects.

## Layout

```
data_ai/
├── projects/      # one folder per project, each with its own README and requirements.txt
├── shared/        # helper code reused across projects
├── .env.example   # template for API keys and settings (copy to .env)
└── .gitignore
```

## Projects

| Project | Description |
|---------|-------------|
| [portfolio](projects/portfolio/) | Personal portfolio website (static HTML/CSS, no build step) |

## Getting started

1. Copy `.env.example` to `.env` and fill in your keys. `.env` is ignored by git, so your keys stay off GitHub.
2. Create a virtual environment for the project you're working on:

   ```powershell
   cd projects\<project-name>
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

## Adding a new project

1. Create `projects/<project-name>/` with a `README.md`, `requirements.txt` and a `src/` folder.
2. Add a row for it in the **Projects** table above.
