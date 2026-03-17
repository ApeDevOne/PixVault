# Pixvault — Local Media Gallery

Dark-mode FastAPI gallery for your Downloads folder.  
Two pages: **Gallery** (masonry grid, filters, infinite scroll) and **Folders** (thumbnail grid).

## Documentation

For full documentation, visit [Project Documentation](https://apedevone.github.io/PixVault/).

## Features

### Gallery (Page 1)
- **Masonry grid** — no blank spaces, portrait/landscape auto-adjust column height
- **Smart filters** — auto-generated from folder names (nature, travel, food, people, art, tech, anime, memes, years…)
- **Infinite scroll** — lazy loads 40 images at a time as you scroll
- **Shuffle toggle** — randomize order
- **Lightbox** — click image to enlarge, arrow-key / swipe navigation
- **Responsive** — 1–6 columns depending on screen width

### Folders (Page 2)
- **Square thumbnail grid** — auto-fill columns, no folder names shown
- **Search bar** — filter folders by name or tag
- **Click folder** → opens scrollable image grid inside a modal
- **Infinite scroll inside modal**
- **Lightbox** inside folder view with swipe support

## Directory Structure

```
gallery/
├── main.py          # FastAPI app
├── requirements.txt
├── pages/
│   ├── gallery.html # Page 1
│   └── folders.html # Page 2
└── README.md
```

## Supported Media Formats

Images: `.jpg` `.jpeg` `.png` `.webp` `.gif` `.bmp` `.avif`  
(Videos are detected server-side but not shown in current UI — easy to add)
