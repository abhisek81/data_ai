# Portfolio

Personal portfolio website for Abhisek Mohanty, built from the resume. It's a single static `index.html` with no build step or dependencies.

## Preview locally

```powershell
python -m http.server 8765 --directory projects\portfolio
```

Then open http://127.0.0.1:8765.

## Editing

All content lives in `index.html`, one section each for Featured work, Experience, Skills, Certification & education and Contact. Colors are CSS variables at the top of the file, with a light and a dark set.
