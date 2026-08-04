# Theme Preview

A demo document for comparing the light, claude, and dark themes.
It exercises the elements used across this project's docs — see
[README.md](README.md) for the real thing.

## Text and inline markup

Normal paragraph text with **bold**, *italic*, ~~strikethrough~~, and
`inline code`. A sentence with an [external link](https://github.com/earendil-works/pi)
and a [file link to app.py](app.py) that opens in the viewer.

> A blockquote: muted background, accent border on the left.
> Spanning two lines to show the wrapping.

## Lists

- First bullet item
- Second item with `code`
  - A nested bullet
- Third item

1. Ordered item one
2. Ordered item two

- [x] A completed task
- [ ] An open task

## Headers at every level

### This is h3

#### This is h4

##### This is h5

###### This is h6

## Code

Inline `code` above; fenced blocks below with syntax highlighting:

```python
def greet(name: str) -> str:
    """Return a friendly greeting."""
    return f"hello, {name}"  # a comment
```

```javascript
// a comment
const sum = (a, b) => a + b
console.log(sum(2, 3))
```

## A table

| Element | Token | Light | Dark |
|---|---|---|---|
| Background | `--bg` | `#FAF9F7` | `#1a1b1e` |
| Foreground | `--fg` | `#111111` | `#e4e4e7` |
| Link | `--link` | `#0969da` | `#4493f8` |
| Accent | `--accent` | `#64809E` | `#2b4a6f` |

## File links

- [`web/README.md`](web/README.md) — into a subdirectory
- [`web/src/app.css`](web/src/app.css) — the theme variables themselves
- [`AGENTS.md`](AGENTS.md) — project conventions
- [`brainstorming-about-project-handling.md`](brainstorming-about-project-handling.md) — design notes

---

*The end — switch themes and watch backgrounds, links, code, and tables.*
