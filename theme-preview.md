# Theme Preview

A demo document for comparing the light, claude, and dark themes.
It exercises the elements used across this project's docs — see
[README.md](README.md) for the real thing.

## Text and inline markup

Normal paragraph text with **bold**, *italic*, ~~strikethrough~~, and
`inline code`. A sentence with an [external link](https://github.com/earendil-works/pi)
and a [file link to app.py](app.py) that opens in the viewer.

> A blockquote: muted text with an accent bar on the left.
> Spanning two lines to show the wrapping.

> [!NOTE]
> Useful information that users should know, even when skimming content.

> [!TIP]
> Helpful advice for doing things better or more easily.

> [!IMPORTANT]
> Key information users need to know to achieve their goal.

> [!WARNING]
> Urgent info that needs immediate user attention to avoid problems.

> [!CAUTION]
> Advises about risks or negative outcomes of certain actions.

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

```bash
#!/usr/bin/env bash
# build and run
make build && make run
```

```css
/* a comment */
.msg.assistant {
  background: var(--panel);
  border-radius: 8px;
}
```

```html
<!-- a comment -->
<div class="msg assistant">
  <p>hello <strong>world</strong></p>
</div>
```

```json
{
  "name": "pi-web-app",
  "themes": ["light", "claude", "dark"],
  "port": 5000
}
```

```rust
// a comment
fn main() {
    let answer: u32 = 42;
    println!("answer: {answer}");
}
```

```sql
-- a comment
SELECT name, created
FROM projects
ORDER BY created DESC
LIMIT 5;
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
