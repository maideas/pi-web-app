<script>
  import { onMount, tick } from 'svelte'
  import { marked } from 'marked'
  import DOMPurify from 'dompurify'
  import hljs from 'highlight.js'
  import hljsDark from 'highlight.js/styles/github-dark.css?inline'
  import hljsLight from 'highlight.js/styles/github.css?inline'

  // Inject hljs themes scoped per app theme so they don't clash
  {
    const style = document.createElement('style')
    style.textContent =
      hljsDark.replaceAll('.hljs', '[data-theme="dark"] .hljs') +
      '\n' +
      hljsLight.replaceAll('.hljs', '[data-theme="light"] .hljs')
    document.head.appendChild(style)
  }

  // Theme (persisted). 'cream' is a light-theme variant applied via
  // data-contrast while data-theme stays "light" — so the markdown
  // preview palette (which only knows light/dark) keeps rendering the
  // light variant. Unknown persisted values (e.g. a removed theme)
  // fall back to light.
  const THEME_VARIANTS = { cream: 'cream' }
  const KNOWN_THEMES = ['light', 'dark', ...Object.keys(THEME_VARIANTS)]
  const savedTheme = localStorage.getItem('theme')
  let theme = $state(KNOWN_THEMES.includes(savedTheme) ? savedTheme : 'light')
  $effect(() => {
    const variant = THEME_VARIANTS[theme]
    document.documentElement.dataset.theme = variant ? 'light' : theme
    if (variant) document.documentElement.dataset.contrast = variant
    else delete document.documentElement.dataset.contrast
    localStorage.setItem('theme', theme)
  })

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (m) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[m]))
  }

  // GitHub-style alert blockquotes (> [!NOTE] / [!TIP] / [!IMPORTANT] /
  // [!WARNING] / [!CAUTION]), rendered like md-to-html-renderer: an octicon
  // plus label as the title and a coloured left border. The marker must be
  // alone on the first line of the blockquote, as on github.com.
  const octicon = (name, path) =>
    `<svg class="octicon octicon-${name}" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="${path}"></path></svg>`
  const ALERT_ICONS = {
    note: octicon('info', 'M0 8a8 8 0 1 1 16 0A8 8 0 0 1 0 8Zm8-6.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13ZM6.5 7.75A.75.75 0 0 1 7.25 7h1a.75.75 0 0 1 .75.75v2.75h.25a.75.75 0 0 1 0 1.5h-2a.75.75 0 0 1 0-1.5h.25v-2h-.25a.75.75 0 0 1-.75-.75ZM8 6a1 1 0 1 1 0-2 1 1 0 0 1 0 2Z'),
    tip: octicon('light-bulb', 'M8 1.5c-2.363 0-4 1.69-4 3.75 0 .984.424 1.625.984 2.304l.214.253c.223.264.47.556.673.848.284.411.537.896.621 1.49a.75.75 0 0 1-1.484.211c-.04-.282-.163-.547-.37-.847a8.456 8.456 0 0 0-.542-.68c-.084-.1-.173-.205-.268-.32C3.201 7.75 2.5 6.766 2.5 5.25 2.5 2.31 4.863 0 8 0s5.5 2.31 5.5 5.25c0 1.516-.701 2.5-1.328 3.259-.095.115-.184.22-.268.319-.207.245-.383.453-.541.681-.208.3-.33.565-.37.847a.751.751 0 0 1-1.485-.212c.084-.593.337-1.078.621-1.489.203-.292.45-.584.673-.848.075-.088.147-.173.213-.253.561-.679.985-1.32.985-2.304 0-2.06-1.637-3.75-4-3.75ZM5.75 12h4.5a.75.75 0 0 1 0 1.5h-4.5a.75.75 0 0 1 0-1.5ZM6 15.25a.75.75 0 0 1 .75-.75h2.5a.75.75 0 0 1 0 1.5h-2.5a.75.75 0 0 1-.75-.75Z'),
    important: octicon('report', 'M0 1.75C0 .784.784 0 1.75 0h12.5C15.216 0 16 .784 16 1.75v9.5A1.75 1.75 0 0 1 14.25 13H8.06l-2.573 2.573A1.458 1.458 0 0 1 3 14.543V13H1.75A1.75 1.75 0 0 1 0 11.25Zm1.75-.25a.25.25 0 0 0-.25.25v9.5c0 .138.112.25.25.25h2a.75.75 0 0 1 .75.75v2.19l2.72-2.72a.749.749 0 0 1 .53-.22h6.5a.25.25 0 0 0 .25-.25v-9.5a.25.25 0 0 0-.25-.25Zm7 2.25v2.5a.75.75 0 0 1-1.5 0v-2.5a.75.75 0 0 1 1.5 0ZM9 9a1 1 0 1 1-2 0 1 1 0 0 1 2 0Z'),
    warning: octicon('alert', 'M6.457 1.047c.659-1.234 2.427-1.234 3.086 0l6.082 11.378A1.75 1.75 0 0 1 14.082 15H1.918a1.75 1.75 0 0 1-1.543-2.575Zm1.763.707a.25.25 0 0 0-.44 0L1.698 13.132a.25.25 0 0 0 .22.368h12.164a.25.25 0 0 0 .22-.368Zm.53 3.996v2.5a.75.75 0 0 1-1.5 0v-2.5a.75.75 0 0 1 1.5 0ZM9 11a1 1 0 1 1-2 0 1 1 0 0 1 2 0Z'),
    caution: octicon('stop', 'M4.47.22A.749.749 0 0 1 5 0h6c.199 0 .389.079.53.22l4.25 4.25c.141.141.22.331.22.53v6a.749.749 0 0 1-.22.53l-4.25 4.25A.749.749 0 0 1 11 16H5a.749.749 0 0 1-.53-.22L.22 11.53A.749.749 0 0 1 0 11V5c0-.199.079-.389.22-.53Zm.84 1.28L1.5 5.31v5.38l3.81 3.81h5.38l3.81-3.81V5.31L10.69 1.5ZM8 4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 8 4Zm0 8a1 1 0 1 1 0-2 1 1 0 0 1 0 2Z'),
  }
  const ALERT_LABELS = { note: 'Note', tip: 'Tip', important: 'Important', warning: 'Warning', caution: 'Caution' }
  const ALERT_MARKER = /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][^\S\n]*\n?/

  // Syntax-highlight fenced code blocks in chat markdown
  marked.use({
    renderer: {
      blockquote(token) {
        const match = token.text.match(ALERT_MARKER)
        if (!match) return false // plain blockquote: default rendering
        const kind = match[1].toLowerCase()
        const body = marked.parse(token.text.slice(match[0].length), { async: false })
        return `<div class="markdown-alert markdown-alert-${kind}">\n<p class="markdown-alert-title">${ALERT_ICONS[kind]}${ALERT_LABELS[kind]}</p>\n${body}</div>\n`
      },
      code({ text, lang }) {
        const language = lang && hljs.getLanguage(lang) ? lang : null
        const html = language
          ? hljs.highlight(text, { language }).value
          : escapeHtml(text)
        // Language hint shown as a muted label above the block (via CSS)
        const hint = lang ? ` data-lang="${escapeHtml(lang)}"` : ''
        return `<pre${hint}><code class="hljs">${html}</code></pre>`
      },
      html(token) {
        // Raw HTML embedded in markdown (e.g. <p align="center"> for a
        // centered logo): sanitize and pass through instead of escaping.
        // Relative img srcs resolve via /raw against mdImageBase, like
        // markdown images (see the image renderer below).
        // Forbid style/form elements and inline styles: this pipeline
        // also renders agent chat output, and unscoped <style> or fixed-
        // position overlays could restyle/spoof the whole app.
        const clean = DOMPurify.sanitize(token.text || '', {
          FORBID_TAGS: ['style', 'form', 'input', 'button', 'select', 'textarea', 'dialog'],
          FORBID_ATTR: ['style'],
        })
        return clean.replace(/<img([^>]*?)\ssrc="([^"]*)"/gi, (m, pre, src) => {
          if (/^[a-z][a-z0-9+.-]*:/i.test(src) || src.startsWith('/') || src.startsWith('//')) return m
          const path = normalizePath(mdImageBase ? `${mdImageBase}/${src}` : src)
          return `<img${pre} src="/raw/${encodeURI(path)}"`
        })
      },
      image(token) {
        // Images in markdown: absolute http(s)/data URLs pass through;
        // relative paths are served inline via /raw, resolved against
        // the markdown file's own directory (mdImageBase, set by
        // renderMarkdown) — otherwise they'd 404 against the SPA URL.
        const href = token.href ?? ''
        if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('//')) {
          if (!/^(https?|data):/i.test(href)) return escapeHtml(token.text || '')
          return false // safe absolute URL: default rendering
        }
        const path = normalizePath(mdImageBase ? `${mdImageBase}/${href}` : href)
        const title = token.title ? ` title="${escapeHtml(token.title)}"` : ''
        return `<img src="/raw/${encodeURI(path)}" alt="${escapeHtml(token.text || '')}"${title}>`
      },
      link(token) {
        // Drop links with unsafe schemes (javascript:, data:, ...) at
        // render time; keep the link text. Covers chat messages, which
        // have no click handler, unlike the file viewer (onMdClick).
        const href = token.href ?? ''
        if (/^[a-z][a-z0-9+.-]*:/i.test(href) && !/^(https?|mailto):/i.test(href)) {
          return this.parser.parseInline(token.tokens)
        }
        return false // safe link: default rendering
      },
    },
  })

  function highlight(text, path) {
    const ext = path?.split('.').pop()?.toLowerCase()
    try {
      if (ext && hljs.getLanguage(ext)) {
        return hljs.highlight(text, { language: ext }).value
      }
      if (text.length > 100_000) {
        return escapeHtml(text.slice(0, 100_000)) + '\n\n…(truncated)'
      }
      return hljs.highlightAuto(text).value
    } catch {
      // fall back to escaped plain text
      return escapeHtml(text)
    }
  }

  // Chat entries: { role: 'user'|'assistant'|'system', text, thinking?, images? } | { role: 'tool', ... }
  let entries = $state([])
  let input = $state('')
  let streaming = $state(false)
  let chatEl
  let inputEl

  // Model / thinking state
  let models = $state([])
  let currentModel = $state(null)
  let thinkingLevels = $state(['off'])
  let thinkingLevel = $state('off')

  // Session list
  let sessions = $state([])
  let currentSessionPath = $state('')

  // Project list / switcher. The active project is server-global: switching
  // respawns the pi subprocess with the project dir as cwd (killing the
  // running chat session, like an app restart).
  let projects = $state([])
  let currentProjectId = $state('')
  let showNewProject = $state(false)
  let showManage = $state(false) // manage-projects popup (detach)
  let showManageSessions = $state(false) // manage-sessions popup (delete from disk)
  let msChecked = $state({}) // session path -> checked for deletion

  // New-project directory picker (popup). Shows only directories and is
  // confined to the parent directory of the current project (users of a
  // web app usually don't know absolute paths, so they browse instead).
  let npBase = $state('')
  let npPath = $state('')
  let npParent = $state(null)
  let npEntries = $state([])
  let npFolder = $state('')
  let npGit = $state(false)

  // Auto-close the project dialogs when unused: closes after
  // DIALOG_IDLE_MS without interaction (any input/focus inside the
  // dialog resets the timer) or when the chat input gains focus.
  const DIALOG_IDLE_MS = 20_000
  let dialogTimer = null
  function pokeDialogTimer() {
    clearTimeout(dialogTimer)
    dialogTimer = setTimeout(() => {
      showNewProject = false
      showManage = false
      showManageSessions = false
    }, DIALOG_IDLE_MS)
  }
  $effect(() => {
    if (showNewProject || showManage || showManageSessions) pokeDialogTimer()
    else clearTimeout(dialogTimer)
  })

  // Slash commands: client-side built-ins + pi commands (extension/skill/template)
  // fetched from /commands. Built-in TUI commands (/model, /resume, ...) are NOT
  // recognized via RPC and get intercepted/blocked in handleSlashCommand.
  const BUILTIN_COMMANDS = [
    { name: 'new', description: 'Start a new session' },
    { name: 'abort', description: 'Stop the current run' },
    { name: 'compact', description: 'Compact context (optional instructions)' },
    { name: 'name', description: 'Set session display name' },
    { name: 'rename', description: 'Generate a session name from the chat content' },
    { name: 'export', description: 'Export session to HTML' },
  ]
  let slashCommands = $state([]) // [{ name, description, source }]
  let slashSuggestions = $derived(
    input.startsWith('/') && !input.includes(' ')
      ? [...BUILTIN_COMMANDS, ...slashCommands]
          .filter((c) => c.name.startsWith(input.slice(1)))
      : []
  )
  // Keyboard navigation in the slash menu: ArrowUp/ArrowDown move the
  // selection, Enter (and Tab) insert it. Resets whenever the
  // suggestion list changes.
  let slashIndex = $state(0)
  $effect(() => {
    slashSuggestions // track
    slashIndex = 0
  })
  $effect(() => {
    slashIndex // track
    document.querySelector('.slash-item.selected')?.scrollIntoView({ block: 'nearest' })
  })

  // Footer stats
  let stats = $state(null)

  // Split ratio between the directory browser (top) and the file
  // viewer (bottom) in the right-hand pane, adjustable by dragging the
  // divider between them; persisted across reloads.
  let browserRatio = $state(Number(localStorage.getItem('browserRatio')) || 0.25)
  let asideEl = $state(null)
  // Same for the vertical divider between the chat column and the
  // directory/file pane (sits on the right side of the gap).
  let chatRatio = $state(Number(localStorage.getItem('chatRatio')) || 0.5)
  let bodyEl = $state(null)
  // Shared pointer-drag plumbing for both splitters: `apply` maps a
  // pointer event to the new ratio, `key` persists it on release.
  function dragSplit(ev, apply, key) {
    ev.preventDefault()
    const move = (e) => apply(e)
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      localStorage.setItem(key, String(key === 'chatRatio' ? chatRatio : browserRatio))
      updateFades()
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }
  function startSplitDrag(ev) {
    const rect = asideEl.getBoundingClientRect()
    dragSplit(ev, (e) => {
      const r = (e.clientY - rect.top) / rect.height
      browserRatio = Math.min(0.9, Math.max(0.1, r))
    }, 'browserRatio')
  }
  function startChatSplitDrag(ev) {
    const rect = bodyEl.getBoundingClientRect()
    dragSplit(ev, (e) => {
      const r = (e.clientX - rect.left) / rect.width
      chatRatio = Math.min(0.8, Math.max(0.2, r))
    }, 'chatRatio')
  }

  // Pending attachments: [{ kind: 'image', name, data (base64), mimeType, url (data URL) }
  //                       | { kind: 'text', name, text }]
  let attachments = $state([])

  // File browser / viewer
  // Fades soften the hard cut-off at the top/bottom edges of the file
  // viewer and the directory list: shown only where content is actually
  // scrolled out of view.
  let fadeTop = $state(false)
  let fadeBottom = $state(false)
  let dirFadeTop = $state(false)
  let dirFadeBottom = $state(false)
  let chatFadeTop = $state(false)
  let chatFadeBottom = $state(false)
  function fadesFor(selector) {
    const el = document.querySelector(selector)
    if (!el) return { top: false, bottom: false }
    return {
      top: el.scrollTop > 8,
      bottom: el.scrollHeight - el.scrollTop - el.clientHeight > 8,
    }
  }
  function updateFades() {
    ;({ top: fadeTop, bottom: fadeBottom } = fadesFor('.viewer-body .filecontent'))
    ;({ top: dirFadeTop, bottom: dirFadeBottom } = fadesFor('.browser-body .dirlist'))
    ;({ top: chatFadeTop, bottom: chatFadeBottom } = fadesFor('.chat-body .chat'))
  }

  // Recompute once new content has rendered.
  $effect(() => {
    selectedFile // track
    dirEntries // track
    entries.length // track (chat grows while streaming)
    tick().then(updateFades)
  })

  let browserPath = $state('')
  let browserParent = $state(null)
  let dirEntries = $state([])
  let selectedFile = $state(null) // { path, text, reason }

  // Viewer navigation history (paths). Opening a file via the browser or
  // a markdown link pushes the previously shown file onto `viewerHistory`
  // and clears `viewerFuture`; the </> buttons move between the stacks.
  // refreshFiles (same file reloaded) and reinit (project switch, stacks
  // cleared) don't push.
  let viewerHistory = $state([])
  let viewerFuture = $state([])

  // Diff view: when set, the viewer shows the git diff of the selected
  // file instead of its content. { path, diff } (diff === '' means no
  // changes, diff === null means error — message in `error`). Cleared
  // whenever another file is shown.
  let diffView = $state(null)

  async function toggleDiff() {
    if (diffView) {
      diffView = null
      return
    }
    const path = selectedFile?.path
    if (!path) return
    diffView = await apiGet(`/api/diff?path=${encodeURIComponent(path)}`)
  }

  // Render a unified git diff as per-line highlighted HTML (GitHub-style
  // full-line backgrounds; hunk headers and file metadata dimmed).
  function renderDiff(text) {
    return text
      .split('\n')
      .map((line) => {
        let cls = 'ctx'
        if (line.startsWith('+++') || line.startsWith('---') || /^(diff |index |new file|deleted file|similarity|rename |old mode|new mode|Binary files)/.test(line)) cls = 'meta'
        else if (line.startsWith('@@')) cls = 'hunk'
        else if (line.startsWith('+')) cls = 'add'
        else if (line.startsWith('-')) cls = 'del'
        return `<span class="dline ${cls}">${escapeHtml(line)}\n</span>`
      })
      .join('')
  }

  // ----- overview ruler (VS Code / meld style) -----
  // Colored marks on the right edge of the viewer, shown only while a
  // diff is rendered: +/- runs positioned by their line index within
  // the diff text, scaled to the full diff height.
  function marksForDiff(diffText) {
    const lines = diffText.split('\n')
    const total = lines.length
    const marks = []
    let start = 0
    let kind = null
    const flush = (i) => {
      if (kind) marks.push({ top: start / total, height: (i - start) / total, kind })
      kind = null
    }
    lines.forEach((line, i) => {
      let k = null
      if (/^\+(?!\+\+)/.test(line)) k = 'add'
      else if (/^-(?!--)/.test(line)) k = 'del'
      if (k !== kind) {
        flush(i)
        if (k) {
          start = i
          kind = k
        }
      }
    })
    flush(total)
    return marks
  }

  const rulerMarks = $derived(diffView?.diff ? marksForDiff(diffView.diff) : [])

  // Label for the centered viewer-head status: what the pane shows.
  const viewerType = $derived.by(() => {
    if (!selectedFile) return ''
    if (diffView) return diffView.diff ? 'diff' : diffView.error ? 'diff · error' : 'diff · no changes'
    if (selectedFile.image) return 'image'
    if (selectedFile.text === null) return 'binary'
    if (isMarkdown(selectedFile.path)) return 'markdown · rendered'
    return 'source'
  })
  function pushViewerHistory(prevPath, newPath) {
    if (prevPath && prevPath !== newPath) {
      viewerHistory.push(prevPath)
      viewerFuture = []
    }
  }

  // Show a file at `path` and sync the directory browser to its location
  // (the selected entry is highlighted via selectedFile.path).
  async function showFileAt(path) {
    const f = await loadViewerFile(path)
    if (!f.path || f.error) return false
    diffView = null
    selectedFile = f
    rememberFile(path)
    await browse(path.split('/').slice(0, -1).join('/'))
    return true
  }

  // Back/forward: pop from one stack until a still-loadable file is found
  // (files may vanish between visits), moving the current file to the
  // other stack on success.
  async function viewerGo(dir) {
    const [from, to] = dir < 0 ? [viewerHistory, viewerFuture] : [viewerFuture, viewerHistory]
    while (from.length) {
      const path = from.pop()
      const cur = selectedFile?.path
      if (await showFileAt(path)) {
        if (cur && cur !== path) to.push(cur)
        return
      }
    }
  }

  // Markdown files are rendered in the viewer with the same marked +
  // highlight.js pipeline as the chat, so both look identical.
  const isMarkdown = (path) => /\.md|\.markdown$/i.test(path ?? '')
  // Image files are rendered as <img> via /raw (scaled to fit the
  // viewer, see .filecontent.image) instead of the binary placeholder.
  const isImage = (path) => /\.(png|jpe?g|gif|webp|bmp|ico|avif|svg)$/i.test(path ?? '')

  // Load a file for the viewer: images only need an existence check
  // (the <img> streams the bytes itself, so MAX_PREVIEW_BYTES doesn't
  // apply); `v` cache-busts the <img> after agent runs (refreshFiles).
  async function loadViewerFile(path) {
    const f = await apiGet(`/api/file?path=${encodeURIComponent(path)}`)
    if (!f.path || f.error) return f
    return isImage(f.path) ? { path: f.path, image: true, v: Date.now() } : f
  }
  let toolsUsedInRun = $state(false)
  let autoNaming = $state(false)

  async function browse(path) {
    const resp = await apiGet(`/api/list?path=${encodeURIComponent(path)}`)
    browserPath = resp.path ?? ''
    browserParent = resp.parent ?? null
    dirEntries = resp.entries ?? []
  }

  // Link clicks inside rendered markdown previews. Relative file links
  // would otherwise resolve against the app page URL (e.g. /app.py) and
  // navigate the SPA away to an invalid endpoint; instead load them in
  // the viewer, resolved against the markdown file's own directory.
  // External links open in a new tab; in-page anchors scroll natively.
  function normalizePath(p) {
    const parts = []
    for (const seg of p.split('/')) {
      if (!seg || seg === '.') continue
      if (seg === '..') parts.pop()
      else parts.push(seg)
    }
    return parts.join('/')
  }

  async function openLinkedFile(path) {
    const prev = selectedFile?.path
    if (await showFileAt(path)) {
      pushViewerHistory(prev, path)
      return
    }
    // Not a file — maybe a directory link: navigate the browser there.
    const list = await apiGet(`/api/list?path=${encodeURIComponent(path)}`)
    if (list.entries) {
      selectedFile = null
      browserPath = list.path ?? ''
      browserParent = list.parent ?? null
      dirEntries = list.entries
    } else {
      entries.push({ role: 'system', text: `⚠️ link target not found: ${path}` })
    }
  }

  function onMdClick(e) {
    const a = e.target.closest('a')
    if (!a) return
    const href = a.getAttribute('href') ?? ''
    if (!href || href.startsWith('#')) return // in-page anchor: native scroll
    if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('//')) {
      if (href.includes(':') && !/^(https?|mailto):/i.test(href)) {
        e.preventDefault()
        return // block unsafe schemes like javascript:, data:, etc.
      }
      a.target = '_blank'
      a.rel = 'noopener noreferrer'
      return
    }
    e.preventDefault()
    const [pathPart] = href.split('#')
    if (!pathPart) return
    const base = selectedFile.path.split('/').slice(0, -1).join('/')
    openLinkedFile(normalizePath(base ? `${base}/${pathPart}` : pathPart))
  }

  // Remember the open viewer file per project (registry: lastFile);
  // reinit restores it when the project is opened.
  function rememberFile(path) {
    if (currentProjectId && path) {
      apiPost(`/api/projects/${currentProjectId}/last-file`, { path })
    }
  }

  // Delete the file currently shown in the viewer (after confirmation),
  // then refresh the directory listing and clear the viewer. The deleted
  // path is dropped from the back/forward stacks.
  async function deleteViewerFile() {
    const path = selectedFile?.path
    if (!path || !confirm(`Delete ${path} from disk?`)) return
    const resp = await apiPost('/api/file/delete', { path })
    if (!resp.success) {
      entries.push({ role: 'system', text: `⚠️ delete failed: ${resp.error ?? 'unknown error'}` })
      return
    }
    viewerHistory = viewerHistory.filter((p) => p !== path)
    viewerFuture = viewerFuture.filter((p) => p !== path)
    selectedFile = null
    diffView = null
    await browse(browserPath)
  }

  async function selectEntry(e) {
    if (e.dir) {
      selectedFile = null
      diffView = null
      await browse(e.path)
    } else {
      const prev = selectedFile?.path
      const f = await loadViewerFile(e.path)
      if (f.path && !f.error) pushViewerHistory(prev, e.path)
      diffView = null
      selectedFile = f
      rememberFile(e.path)
    }
  }

  // After a run that used tools, files may have changed: reload the current
  // directory and the open file. If either vanished in the meantime, fall
  // back to the project root.
  async function refreshFiles() {
    const list = await apiGet(`/api/list?path=${encodeURIComponent(browserPath)}`)
    if (!list.entries) {
      selectedFile = null
      await browse('')
      return
    }
    browserPath = list.path ?? ''
    browserParent = list.parent ?? null
    dirEntries = list.entries
    if (selectedFile) {
      const f = await loadViewerFile(selectedFile.path)
      if (f.error) {
        selectedFile = null
        diffView = null
        await browse('')
      } else {
        selectedFile = f
        // Keep an open diff view current after agent runs.
        if (diffView) diffView = await apiGet(`/api/diff?path=${encodeURIComponent(f.path)}`)
      }
    }
  }

  function fmtSize(s) {
    if (s == null) return ''
    if (s < 1024) return `${s} B`
    if (s < 1024 * 1024) return `${(s / 1024).toFixed(1)} KB`
    return `${(s / 1024 / 1024).toFixed(1)} MB`
  }

  // Base directory for resolving relative image paths during a parse;
  // '' (project root) for chat messages, the file's directory for the
  // viewer. Module-scoped because marked renderers take no extra args.
  let mdImageBase = ''
  function renderMarkdown(text, base = '') {
    mdImageBase = base
    try {
      return marked.parse(text ?? '', { async: false })
    } finally {
      mdImageBase = ''
    }
  }

  // Auto-scroll keeps up with streaming only while the user is at (near)
  // the bottom; scrolling up stops it, scrolling back down resumes it.
  // The position is measured synchronously at call time — before the
  // pending DOM update grows scrollHeight — so growth without scroll
  // events can never corrupt the decision (a scroll-event flag could).
  let scrollQueued = false
  async function scrollToBottom(force = false) {
    if (scrollQueued || !chatEl) return
    const stick = force || chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 40
    if (!stick) return
    scrollQueued = true
    requestAnimationFrame(async () => {
      scrollQueued = false
      await tick()
      if (chatEl) chatEl.scrollTop = chatEl.scrollHeight
    })
  }

  function currentAssistant() {
    const last = entries[entries.length - 1]
    return last?.role === 'assistant' ? last : null
  }

  // Message navigator: the dots in the gap between chat and file panes.
  // Hovering opens a centered popup with (truncated) user messages;
  // clicking one scrolls the chat to it. The popup closes when the
  // mouse leaves it, or shortly after leaving the dots unless the popup
  // is reached in time.
  let showMsgNav = $state(false)
  let msgNavTimer = null
  const userMessages = $derived(entries.map((e, i) => ({ e, i })).filter((x) => x.e.role === 'user'))

  function openMsgNav() {
    clearTimeout(msgNavTimer)
    showMsgNav = true
  }
  function scheduleCloseMsgNav() {
    clearTimeout(msgNavTimer)
    msgNavTimer = setTimeout(() => (showMsgNav = false), 200)
  }
  function closeMsgNav() {
    clearTimeout(msgNavTimer)
    showMsgNav = false
  }
  function jumpToMessage(i) {
    closeMsgNav()
    document
      .querySelector(`.msg.user[data-idx="${i}"]`)
      ?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }
  function truncate(text, n = 80) {
    const t = (text ?? '').replace(/\s+/g, ' ').trim()
    return t.length > n ? t.slice(0, n) + '…' : t
  }

  async function apiGet(url) {
    try {
      const r = await fetch(url)
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return await r.json()
    } catch (err) {
      console.error('GET', url, err)
      return { success: false, error: err.message }
    }
  }

  async function apiPost(url, body) {
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        },
        body: JSON.stringify(body ?? {}),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return await r.json()
    } catch (err) {
      console.error('POST', url, err)
      return { success: false, error: err.message }
    }
  }

  async function refreshState() {
    const resp = await apiGet('/state')
    if (!resp.success) return
    currentModel = resp.data.model
    thinkingLevel = resp.data.thinkingLevel
    const levels = await apiGet('/thinking_levels')
    if (levels.success) thinkingLevels = levels.data.levels
  }

  async function refreshModels() {
    const resp = await apiGet('/models')
    if (resp.success) models = resp.data.models
  }

  async function refreshStats() {
    const resp = await apiGet('/stats')
    if (resp.success) stats = resp.data
  }

  // Debounced variant: collapses bursts of triggers (agent_end + agent_settled,
  // rapid message_end events) into a single /stats fetch.
  let statsTimer = null
  function refreshStatsDebounced() {
    if (statsTimer) return
    statsTimer = setTimeout(() => {
      statsTimer = null
      refreshStats()
    }, 300)
  }

  // Guard against out-of-order responses: only the latest request may
  // update the sessions list / current marker.
  let sessionsReq = 0
  async function refreshSessions() {
    const id = ++sessionsReq
    const resp = await apiGet('/sessions')
    if (id !== sessionsReq) return
    sessions = resp.sessions ?? []
    currentSessionPath = sessions.find((s) => s.current)?.path ?? ''
  }

  async function onModelChange(e) {
    const [provider, modelId] = e.target.value.split('::')
    const resp = await apiPost('/set_model', { provider, modelId })
    if (resp.success) {
      currentModel = resp.data
      const levels = await apiGet('/thinking_levels')
      if (levels.success) thinkingLevels = levels.data.levels
      if (!thinkingLevels.includes(thinkingLevel)) thinkingLevel = 'off'
    }
  }

  async function onThinkingChange(e) {
    const resp = await apiPost('/set_thinking', { level: e.target.value })
    if (resp.success) thinkingLevel = e.target.value
  }

  async function loadProjects() {
    const resp = await apiGet('/api/projects')
    projects = resp.projects ?? []
    currentProjectId = projects.find((p) => p.current)?.id ?? ''
  }

  // Full (re)initialization: after mount, a project switch, or a
  // project_switched event from another tab.
  async function reinit() {
    entries = []
    attachments = []
    selectedFile = null
    diffView = null
    viewerHistory = []
    viewerFuture = []
    toolsUsedInRun = false
    await browse('')
    await loadHistory()
    await Promise.all([
      refreshState(),
      refreshModels(),
      refreshStats(),
      refreshSessions(),
      loadCommands(),
      loadProjects(),
    ])
    // Restore the project's remembered viewer file, README as fallback.
    const remembered = projects.find((p) => p.current)?.lastFile ?? 'README.md'
    let f = await loadViewerFile(remembered)
    if ((!f.path || f.error) && remembered !== 'README.md') {
      f = await loadViewerFile('README.md')
    }
    if (f.path && !f.error) {
      selectedFile = f
      // Show the file's directory in the browser (also highlights it).
      const dir = f.path.split('/').slice(0, -1).join('/')
      if (dir) await browse(dir)
    }
  }

  async function switchProject(id) {
    const resp = await apiPost(`/api/projects/${id}/open`)
    if (!resp.success) {
      entries.push({ role: 'system', text: `⚠️ ${resp.error ?? 'failed to open project'}` })
      await loadProjects() // reset the select to the real current project
      return
    }
    // Our SSE stream is still subscribed to the now-terminated old pi
    // process — reconnect, or we won't see any events from the new one.
    // (Other tabs learn via the project_switched event; the initiating
    // tab must reconnect itself.)
    reconnectEvents()
    await reinit()
    inputEl?.focus()
  }

  async function onProjectChange(e) {
    const id = e.target.value
    if (!id || id === currentProjectId) return
    if (streaming && !window.confirm('Switching projects restarts the agent and kills the running session. Continue?')) {
      e.target.value = currentProjectId
      return
    }
    await switchProject(id)
  }

  async function browseDirs(path) {
    const resp = await apiGet(`/api/dirs?path=${encodeURIComponent(path)}`)
    if (resp.error) return
    npBase = resp.base ?? ''
    npPath = resp.path ?? ''
    npParent = resp.parent ?? null
    npEntries = resp.entries ?? []
  }

  function toggleNewProject() {
    showNewProject = !showNewProject
    if (showNewProject) browseDirs('')
  }

  function toggleManage() {
    showManage = !showManage
  }

  function toggleManageSessions() {
    showManageSessions = !showManageSessions
    if (showManageSessions) {
      msChecked = {}
      refreshSessions()
    }
  }

  const msSelectedPaths = () => sessions.filter((s) => msChecked[s.path] && !s.current).map((s) => s.path)

  // All selectable (non-current) sessions checked? Drives the all/none toggle.
  const msAllChecked = () => {
    const selectable = sessions.filter((s) => !s.current)
    return selectable.length > 0 && selectable.every((s) => msChecked[s.path])
  }

  function msToggleAll() {
    msChecked = msAllChecked()
      ? {}
      : Object.fromEntries(sessions.filter((s) => !s.current).map((s) => [s.path, true]))
  }

  async function deleteCheckedSessions() {
    const paths = msSelectedPaths()
    if (!paths.length) return
    if (!window.confirm(`Delete ${paths.length} session${paths.length > 1 ? 's' : ''} from disk?\n\nThis cannot be undone.`)) return
    const resp = await apiPost('/delete_sessions', { paths })
    if (resp.errors?.length) {
      entries.push({ role: 'system', text: `⚠️ ${resp.errors.map((e) => `${e.path}: ${e.error}`).join('\n')}` })
    }
    msChecked = {}
    await refreshSessions()
  }

  // Detach = remove from the registry only; the directory stays on disk.
  async function detachProject(p) {
    if (p.current) return
    if (!window.confirm(`Detach project “${p.name}” from the list?\n${p.path}\n\nThe directory itself is not deleted.`)) return
    const resp = await apiPost(`/api/projects/${p.id}/detach`)
    if (!resp.success) {
      entries.push({ role: 'system', text: `⚠️ ${resp.error ?? 'failed to detach project'}` })
    }
    await loadProjects()
  }

  function npFullPath() {
    return npPath ? `${npBase}/${npPath}` : npBase
  }

  // Register `path` as a project (creating it first when it doesn't
  // exist) and switch to it.
  async function chooseProjectDir(path, gitInit) {
    const resp = await apiPost('/api/projects', { path, gitInit })
    if (!resp.success) {
      entries.push({ role: 'system', text: `⚠️ ${resp.error ?? 'failed to create project'}` })
      return
    }
    showNewProject = false
    npFolder = ''
    npGit = false
    await loadProjects()
    if (streaming && !window.confirm('Switch to the new project now? This restarts the agent and kills the running session.')) return
    await switchProject(resp.project.id)
  }

  async function onSessionChange(e) {
    const path = e.target.value
    if (!path || path === currentSessionPath) return
    const resp = await apiPost('/switch_session', { path })
    if (resp.success && !resp.data?.cancelled) {
      currentSessionPath = path
      entries = []
      await loadHistory()
      await refreshState()
      await refreshStats()
      inputEl?.focus()
    }
    await refreshSessions()
  }

  function handleEvent(ev) {
    switch (ev.type) {
      case 'agent_start':
        streaming = true
        break

      case 'message_start':
        if (ev.message?.role === 'assistant') {
          entries.push({ role: 'assistant', text: '', thinking: '' })
          scrollToBottom()
        }
        break

      case 'message_update': {
        const d = ev.assistantMessageEvent
        let a = currentAssistant()
        if (!a) {
          a = { role: 'assistant', text: '', thinking: '' }
          entries.push(a)
        }
        if (d?.type === 'text_delta') {
          a.text += d.delta
        } else if (d?.type === 'thinking_delta') {
          a.thinking += d.delta
        }
        scrollToBottom()
        break
      }

      case 'message_end':
        if (ev.message?.role === 'assistant') {
          const a = currentAssistant()
          const text = (ev.message.content ?? [])
            .filter((c) => c.type === 'text')
            .map((c) => c.text)
            .join('')
          const thinking = (ev.message.content ?? [])
            .filter((c) => c.type === 'thinking')
            .map((c) => c.thinking)
            .join('')
          if (a) {
            a.text = text
            if (thinking) a.thinking = thinking
          } else if (text || thinking) {
            entries.push({ role: 'assistant', text, thinking })
          }
          // Live token/cost update once per completed assistant message.
          refreshStatsDebounced()
          // Surface failed/aborted assistant turns
          if (ev.message.stopReason === 'error') {
            entries.push({
              role: 'system',
              text: `❌ ${ev.message.errorMessage ?? 'The assistant turn failed.'}`,
            })
          } else if (ev.message.stopReason === 'aborted') {
            entries.push({ role: 'system', text: '⏹ aborted' })
          }
        }
        scrollToBottom()
        break

      case 'auto_retry_start':
        entries.push({
          role: 'system',
          text: `⚠️ ${ev.errorMessage ?? 'transient error'} — retrying (attempt ${ev.attempt}/${ev.maxAttempts} in ${Math.round((ev.delayMs ?? 0) / 1000)}s)…`,
        })
        scrollToBottom()
        break

      case 'auto_retry_end':
        if (!ev.success) {
          entries.push({
            role: 'system',
            text: `❌ Retry failed after ${ev.attempt} attempts: ${ev.finalError ?? 'unknown error'}`,
          })
        }
        scrollToBottom()
        break

      case 'compaction_end':
        if (!ev.result && !ev.aborted) {
          entries.push({
            role: 'system',
            text: `❌ Compaction failed: ${ev.errorMessage ?? 'unknown error'}`,
          })
        }
        scrollToBottom()
        break

      case 'extension_error':
        entries.push({
          role: 'system',
          text: `❌ Extension error (${ev.extensionPath}): ${ev.error}`,
        })
        scrollToBottom()
        break

      case 'tool_execution_start':
        toolsUsedInRun = true
        entries.push({
          role: 'tool',
          name: ev.toolName,
          args: ev.args,
          output: '',
          done: false,
          isError: false,
        })
        scrollToBottom()
        break

      case 'tool_execution_update': {
        const t = entries.findLast((e) => e.role === 'tool' && !e.done)
        if (t) {
          t.output = (ev.partialResult?.content ?? [])
            .map((c) => c.text ?? '')
            .join('')
        }
        scrollToBottom()
        break
      }

      case 'tool_execution_end': {
        const t = entries.findLast((e) => e.role === 'tool' && !e.done)
        if (t) {
          t.output = (ev.result?.content ?? []).map((c) => c.text ?? '').join('')
          t.done = true
          t.isError = !!ev.isError
        }
        scrollToBottom()
        break
      }

      case 'agent_end':
      case 'agent_settled':
        streaming = false
        refreshStatsDebounced()
        refreshSessions() // pick up new session files / reordered mtimes
        maybeAutoName() // name the session once enough content exists
        if (toolsUsedInRun) {
          // Consumed on the first of agent_end/agent_settled; the second is a no-op.
          toolsUsedInRun = false
          refreshFiles()
        }
        scrollToBottom()
        // Ready for the next prompt without clicking.
        tick().then(() => inputEl?.focus())
        break

      case 'extension_ui_request':
        handleUiRequest(ev).catch((err) => console.error('UI request error', err))
        break

      case 'project_switched':
        // Another tab (or client) switched the active project: our SSE
        // stream is attached to the now-dead pi process, so reconnect
        // and load the new project's state.
        entries.push({
          role: 'system',
          text: `⇄ switched to project “${ev.project?.name ?? '?'}”`,
        })
        reconnectEvents()
        reinit()
        break
    }
  }

  async function handleUiRequest(req) {
    let resp = { type: 'extension_ui_response', id: req.id }
    if (req.method === 'notify') {
      // empty response for one-way notify
    } else if (req.method === 'setStatus' || req.method === 'setWidget' || req.method === 'setTitle') {
      if (req.method === 'setTitle' && req.title) document.title = req.title
    } else if (req.method === 'confirm') {
      resp.confirmed = window.confirm(`${req.title ?? ''}\n${req.message ?? ''}`)
    } else if (req.method === 'select') {
      const choice = window.prompt(
        `${req.title ?? 'Choose:'}\n` + req.options.map((o, i) => `${i + 1}. ${o}`).join('\n')
      )
      const idx = parseInt(choice, 10) - 1
      if (idx >= 0 && idx < req.options.length) resp.value = req.options[idx]
      else resp.cancelled = true
    } else if (req.method === 'input' || req.method === 'editor') {
      const v = window.prompt(req.title ?? 'Input:', req.prefill ?? '')
      if (v === null) resp.cancelled = true
      else resp.value = v
    } else {
      resp.cancelled = true
    }
    await apiPost('/ui-response', resp)
  }

  function onPickImages(e) {
    for (const file of e.target.files) {
      if (file.type.startsWith('image/')) {
        const reader = new FileReader()
        reader.onload = () => {
          const dataUrl = reader.result
          attachments.push({
            kind: 'image',
            name: file.name,
            data: dataUrl.split(',')[1],
            mimeType: file.type,
            url: dataUrl,
          })
        }
        reader.readAsDataURL(file)
      } else {
        // Text-ish files: inline content into the prompt message
        const reader = new FileReader()
        reader.onload = () => {
          attachments.push({
            kind: 'text',
            name: file.name,
            text: reader.result,
          })
        }
        reader.readAsText(file)
      }
    }
    e.target.value = ''
  }

  // Intercept slash commands: built-in TUI commands don't work via RPC, so map
  // the useful ones to their RPC equivalents, pass through commands pi itself
  // recognizes (extension/skill/template), and block the rest with a hint.
  async function handleSlashCommand(message) {
    const [cmd, ...rest] = message.slice(1).split(/\s+/)
    const arg = rest.join(' ')
    switch (cmd) {
      case 'new':
        await newSession()
        return
      case 'abort':
        await abort()
        return
      case 'compact': {
        const r = await apiPost('/compact', arg ? { customInstructions: arg } : {})
        entries.push({
          role: 'system',
          text: r.success
            ? ` compacted: ${r.data?.tokensBefore ?? '?'} → ~${r.data?.estimatedTokensAfter ?? '?'} tokens`
            : `⚠️ compact failed: ${r.error ?? 'unknown error'}`,
        })
        return
      }
      case 'name': {
        if (!arg) {
          entries.push({ role: 'system', text: '⚠️ usage: /name <session name>' })
          return
        }
        const r = await apiPost('/set_session_name', { name: arg })
        if (r.success) await refreshSessions()
        else entries.push({ role: 'system', text: `⚠️ ${r.error ?? 'failed to set name'}` })
        return
      }
      case 'rename':
        await maybeAutoName(true)
        return
      case 'export': {
        const r = await apiPost('/export_html')
        entries.push({
          role: 'system',
          text: r.success ? ` exported to ${r.data?.path ?? 'file'}` : `⚠️ export failed: ${r.error ?? 'unknown error'}`,
        })
        return
      }
      default:
        if (slashCommands.some((c) => c.name === cmd)) {
          // Recognized by pi (extension/skill/template) — forward as prompt.
          entries.push({ role: 'user', text: message })
          const body = { message }
          if (streaming) body.streamingBehavior = 'steer'
          await apiPost('/prompt', body)
        } else {
          const available = [
            ...BUILTIN_COMMANDS.map((c) => '/' + c.name),
            ...slashCommands.map((c) => '/' + c.name),
          ].join(', ')
          entries.push({
            role: 'system',
            text: `⚠️ Unknown command /${cmd}. Built-in TUI commands don't work here. Available: ${available}`,
          })
        }
    }
  }

  async function sendPrompt() {
    const trimmed = input.trim()
    if (trimmed.startsWith('/')) {
      input = ''
      await handleSlashCommand(trimmed)
      scrollToBottom(true) // explicit command: jump to the end
      return
    }
    const textAttachments = attachments.filter((a) => a.kind === 'text')
    const imageAttachments = attachments.filter((a) => a.kind === 'image')
    let message = input.trim()
    for (const t of textAttachments) {
      message += `${message ? '\n\n' : ''}Content of \`${t.name}\`:\n\`\`\`\n${t.text}\n\`\`\``
    }
    if (!message && imageAttachments.length === 0) return
    const images = imageAttachments.map(({ data, mimeType }) => ({ data, mimeType }))
    entries.push({
      role: 'user',
      text: message,
      images: imageAttachments.map((a) => a.url),
    })
    input = ''
    attachments = []
    scrollToBottom(true) // sending is an explicit jump to the end
    const body = { message }
    if (images.length) body.images = images
    if (streaming) body.streamingBehavior = 'steer'
    const data = await apiPost('/prompt', body)
    if (!data.success) {
      entries.push({ role: 'assistant', text: `⚠️ ${data.error ?? 'prompt rejected'}` })
    }
  }

  // Auto-name the session from its content after a run ends; the
  // backend only names sessions that don't have a name yet (an existing
  // name is never overwritten automatically). force=true regenerates on
  // explicit user request (/rename). The autoNaming flag dedupes the
  // agent_end/agent_settled pair that both reach this call.
  async function maybeAutoName(force = false) {
    if (!force && autoNaming) return
    autoNaming = true
    try {
      const r = await apiPost('/api/auto_name', force ? { force: true } : {})
    if (r.success && r.name) {
      entries.push({
        role: 'system',
        text: r.previous ? `🏷 session renamed to “${r.name}”` : `🏷 session named “${r.name}”`,
      })
      scrollToBottom()
      await refreshSessions()
    } else if (force) {
      entries.push({ role: 'system', text: `⚠️ rename failed: ${r.error ?? 'unknown error'}` })
    }
    } finally {
      autoNaming = false
    }
  }

  async function abort() {
    await apiPost('/abort')
  }

  async function newSession() {
    await apiPost('/new_session')
    entries = []
    await refreshSessions()
    await refreshStats()
    inputEl?.focus()
  }

  async function loadHistory() {
    const resp = await apiGet('/messages')
    if (!resp.success) return
    for (const m of resp.data.messages) {
      if (m.role === 'user') {
        const parts = typeof m.content === 'string' ? [{ type: 'text', text: m.content }] : (m.content ?? [])
        const text = parts.filter((c) => c.type === 'text').map((c) => c.text).join('')
        const images = parts
          .filter((c) => c.type === 'image')
          .map((c) => `data:${c.mimeType ?? c.source?.mediaType ?? 'image/png'};base64,${c.data ?? c.source?.data}`)
        if (text || images.length) entries.push({ role: 'user', text, images })
      } else if (m.role === 'assistant') {
        const text = (m.content ?? []).filter((c) => c.type === 'text').map((c) => c.text).join('')
        const thinking = (m.content ?? []).filter((c) => c.type === 'thinking').map((c) => c.thinking).join('')
        if (text || thinking) entries.push({ role: 'assistant', text, thinking })
      } else if (m.role === 'toolResult') {
        entries.push({
          role: 'tool',
          name: m.toolName,
          args: {},
          output: (m.content ?? []).map((c) => c.text ?? '').join(''),
          done: true,
          isError: !!m.isError,
        })
      }
    }
    scrollToBottom(true) // history (re)load: start at the end
  }

  async function loadCommands() {
    const r = await apiGet('/commands')
    if (r.success) slashCommands = r.data?.commands ?? []
  }

  let eventSource = null
  function reconnectEvents() {
    eventSource?.close()
    const es = new EventSource('/events')
    eventSource = es
    // Retry on (re)connect: covers page loads while the server was down
    // and picks up new extensions/prompts after a server restart.
    es.onopen = () => loadCommands()
    es.onmessage = (e) => {
      try {
        handleEvent(JSON.parse(e.data))
      } catch (err) {
        console.error('event handler error', err)
      }
    }
    es.onerror = (e) => console.error('SSE error', e)
  }

  onMount(() => {
    reinit() // also shows the project README in the file viewer, if present
    reconnectEvents()
    inputEl?.focus()
    return () => eventSource?.close()
  })

  function applySlashSuggestion(c) {
    input = '/' + c.name + ' '
  }

  // Auto-grow the prompt textarea with the typed lines, up to
  // INPUT_MAX_LINES; beyond that it scrolls instead of growing.
  const INPUT_MAX_LINES = 8
  function autogrowInput() {
    if (!inputEl) return
    inputEl.style.height = 'auto'
    const style = getComputedStyle(inputEl)
    const line = parseFloat(style.lineHeight) || 20
    const pad = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom)
    // The textarea is content-box: style.height sets the content height,
    // but scrollHeight includes the padding — subtract it, otherwise the
    // padding is counted twice and the field is always one line too tall.
    const content = inputEl.scrollHeight - pad
    inputEl.style.height = Math.min(content, line * INPUT_MAX_LINES) + 'px'
    inputEl.style.overflowY = content > line * INPUT_MAX_LINES ? 'auto' : 'hidden'
  }
  $effect(() => {
    input // track: re-run on every keystroke and on reset after send
    autogrowInput()
  })

  function onKeydown(e) {
    if (slashSuggestions.length) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        slashIndex = (slashIndex + 1) % slashSuggestions.length
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        slashIndex = (slashIndex - 1 + slashSuggestions.length) % slashSuggestions.length
        return
      }
      // Tab completes without sending; Enter picks the selection.
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
        e.preventDefault()
        applySlashSuggestion(slashSuggestions[slashIndex])
        return
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendPrompt()
    }
  }

  function fmtCost(c) {
    return c == null ? '—' : `$${c.toFixed(2)}`
  }
</script>

<main>
  <div class="body" bind:this={bodyEl}>
   <!-- chatRatio marks the splitter position; the chat column ends
        50px earlier, preserving the gap that hosts the dot menu -->
   <div class="chatcol" style="flex: 0 0 calc({(chatRatio * 100).toFixed(2)}% - 51px)">
    <div class="toolbar">
      <div class="tgroup">
      <select bind:value={theme} title="Theme">
        <option value="light">light</option>
        <option value="cream">cream</option>
        <option value="dark">dark</option>
      </select>
      </div>
      <div class="tgroup">
      <select value={currentProjectId} onchange={onProjectChange} title="Project">
        {#each projects as p (p.id)}
          <option value={p.id}>{p.name}</option>
        {/each}
      </select>
      <button onclick={toggleNewProject} title="New project">add</button>
      <button onclick={toggleManage} title="Manage projects">manage</button>
      </div>
      <div class="tgroup">
      <select value={currentModel ? `${currentModel.provider}::${currentModel.id}` : ''} onchange={onModelChange}>
        {#each models as m}
          <option value={`${m.provider}::${m.id}`} selected={currentModel && m.provider === currentModel.provider && m.id === currentModel.id}>
            {m.name ?? m.id}
          </option>
        {/each}
      </select>
      <select value={thinkingLevel} onchange={onThinkingChange}>
        {#each thinkingLevels as l}
          <option value={l} selected={l === thinkingLevel}>{l}</option>
        {/each}
      </select>
      </div>
      <div class="tgroup">
      <select class="sessionselect" value={currentSessionPath} onchange={onSessionChange}>
        {#if !currentSessionPath}
          <option value="" disabled>sessions…</option>
        {/if}
        {#each sessions as s (s.path)}
          <option value={s.path}>
            {s.name}{s.current ? ' (current)' : ''} — {new Date(s.mtime * 1000).toLocaleString()}
          </option>
        {/each}
      </select>
      <button onclick={newSession}>new</button>
      <button onclick={toggleManageSessions} title="Manage sessions">manage</button>
      </div>
    </div>
  {#if showNewProject}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="np-overlay" role="presentation" onclick={(e) => e.target === e.currentTarget && (showNewProject = false)}>
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <div class="np-modal" role="dialog" aria-modal="true" aria-label="Choose project directory" tabindex="-1" oninput={pokeDialogTimer} onfocusin={pokeDialogTimer} onclick={pokeDialogTimer}>
        <div class="np-head">
          <span class="np-title">Choose project directory</span>
          <button class="np-close" onclick={() => (showNewProject = false)}>×</button>
        </div>
        <div class="np-path">
          <span title={npFullPath()}>{npBase}/{npPath}</span>
        </div>
        <div class="np-list">
          {#if npParent !== null}
            <button class="direntry" onclick={() => browseDirs(npParent)}>
              <span class="icon">📁</span>
              <span class="name">..</span>
            </button>
          {/if}
          {#each npEntries as e}
            <button class="direntry" onclick={() => browseDirs(e.path)}>
              <span class="icon">📁</span>
              <span class="name">{e.name}</span>
            </button>
          {:else}
            <div class="np-empty">No subdirectories here.</div>
          {/each}
        </div>
        <div class="np-actions">
          <input
            type="text"
            bind:value={npFolder}
            placeholder="new folder name"
            onkeydown={(e) => e.key === 'Enter' && npFolder.trim() && chooseProjectDir(`${npFullPath()}/${npFolder.trim()}`, npGit)}
          />
          <label><input type="checkbox" bind:checked={npGit} /> git init</label>
          <button onclick={() => chooseProjectDir(`${npFullPath()}/${npFolder.trim()}`, npGit)} disabled={!npFolder.trim()}>Create</button>
          <button onclick={() => chooseProjectDir(npFullPath(), false)} disabled={!npPath}>Select</button>
        </div>
      </div>
    </div>
  {/if}
  {#if showManage}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="np-overlay" role="presentation" onclick={(e) => e.target === e.currentTarget && (showManage = false)}>
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <div class="np-modal" role="dialog" aria-modal="true" aria-label="Manage projects" tabindex="-1" oninput={pokeDialogTimer} onfocusin={pokeDialogTimer} onclick={pokeDialogTimer}>
        <div class="np-head">
          <span class="np-title">Manage projects</span>
          <button class="np-close" onclick={() => (showManage = false)}>×</button>
        </div>
        <div class="np-list">
          {#each projects as p (p.id)}
            <div class="mp-row">
              <span class="mp-name">{p.name}{p.current ? ' (current)' : ''}</span>
              <span class="mp-path" title={p.path}>{p.path}</span>
              <button
                onclick={() => detachProject(p)}
                disabled={p.current}
                title={p.current ? 'Switch to another project first' : 'Detach (the directory stays on disk)'}
              >detach</button>
            </div>
          {:else}
            <div class="np-empty">No projects registered.</div>
          {/each}
        </div>
      </div>
    </div>
  {/if}
  {#if showManageSessions}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="np-overlay" role="presentation" onclick={(e) => e.target === e.currentTarget && (showManageSessions = false)}>
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <div class="np-modal" role="dialog" aria-modal="true" aria-label="Manage sessions" tabindex="-1" oninput={pokeDialogTimer} onfocusin={pokeDialogTimer} onclick={pokeDialogTimer}>
        <div class="np-head">
          <span class="np-title">Manage sessions</span>
          <button class="np-close" onclick={() => (showManageSessions = false)}>×</button>
        </div>
        <div class="ms-toolbar">
          <button class="ms-all" onclick={msToggleAll}>{msAllChecked() ? 'select none' : 'select all'}</button>
        </div>
        <div class="np-list">
          {#each sessions as s (s.path)}
            <label class="mp-row ms-row" class:ms-current={s.current}>
              <input type="checkbox" bind:checked={msChecked[s.path]} disabled={s.current} title={s.current ? 'The current session cannot be deleted' : ''} />
              <span class="mp-name">{s.name}{s.current ? ' (current)' : ''}</span>
              <span class="ms-date">{new Date(s.mtime * 1000).toLocaleString()}</span>
            </label>
          {:else}
            <div class="np-empty">No sessions found.</div>
          {/each}
        </div>
        <div class="np-actions">
          <span class="ms-count">{msSelectedPaths().length} selected</span>
          <button onclick={deleteCheckedSessions} disabled={!msSelectedPaths().length} title="Delete the checked sessions from disk">delete</button>
        </div>
      </div>
    </div>
  {/if}
  <div class="chat-body" onscrollcapture={updateFades}>
    <div class="fade fade-top" class:visible={chatFadeTop}></div>
    <div class="fade fade-bottom" class:visible={chatFadeBottom}></div>
  <div class="chat" bind:this={chatEl}>
    {#if entries.length === 0}
      <div class="chat-empty" aria-hidden="true">
        <svg class="logo" viewBox="0 0 120 120" xmlns="http://www.w3.org/2000/svg">
          <!-- outline variant of the app icon (web/public/logo.svg) -->
          <g stroke="currentColor" fill="none" stroke-linecap="round">
            <path
              stroke-width="6"
              stroke-linejoin="round"
              d="M36 13h48a23 23 0 0 1 23 23v30a23 23 0 0 1-23 23H61l-19.1 16.7c-2 1.7-5 .3-5-2.3V89.7A23 23 0 0 1 13 66V36a23 23 0 0 1 23-23z"
            />
            <g stroke-width="9">
              <path d="M40 34h40" />
              <path d="M51 34v32" />
              <path d="M69 34v24a8 8 0 0 0 8 8" />
            </g>
          </g>
        </svg>
        <div class="chat-empty-text">pi agent web UI</div>
      </div>
    {/if}
    {#each entries as entry, i}
      {#if entry.role === 'system'}
        <div class="msg system">{entry.text}</div>
      {:else if entry.role === 'user'}
        <div class="msg user" data-idx={i}>
          {entry.text}
          {#if entry.images?.length}
            <div class="imgs">
              {#each entry.images as src}
                <img {src} alt="attachment" />
              {/each}
            </div>
          {/if}
        </div>
      {:else if entry.role === 'assistant'}
        <div class="msg assistant md">
          {#if entry.thinking}
            <details class="thinking-block" open>
              <summary>thinking</summary>
              <pre>{entry.thinking}</pre>
            </details>
          {/if}
          {@html renderMarkdown(entry.text)}
        </div>
      {:else}
        <details class="msg tool" class:error={entry.isError} class:ok={entry.done && !entry.isError} open>
          <summary>
            🔧 {entry.name}
            {#if entry.args?.command}<code>{entry.args.command}</code>{/if}
            {#if !entry.done}<span class="spinner">…</span>{/if}
            {#if entry.isError}❌{/if}
          </summary>
          {#if entry.output}<pre>{entry.output}</pre>{/if}
        </details>
      {/if}
    {/each}
    {#if streaming && !currentAssistant()}
      <div class="msg assistant thinking">thinking…</div>
    {/if}
  </div>
  </div>

  {#if attachments.length}
    <div class="attachments">
      {#each attachments as a, i}
        <div class="thumb">
          {#if a.kind === 'image'}
            <img src={a.url} alt="attachment" />
          {:else}
            <div class="textfile">📄 {a.name}</div>
          {/if}
          <button onclick={() => attachments.splice(i, 1)}>×</button>
        </div>
      {/each}
    </div>
  {/if}

  {#if streaming}
    <div class="progress-bar" aria-hidden="true"></div>
  {/if}
  {#if slashSuggestions.length}
    <div class="slash-menu">
      {#each slashSuggestions as c, i (c.name)}
        <button class="slash-item" class:selected={i === slashIndex} onmousedown={(e) => { e.preventDefault(); applySlashSuggestion(c) }}>
          <span class="slash-name">/{c.name}</span>
          {#if c.description}<span class="slash-desc">{c.description}</span>{/if}
        </button>
      {/each}
    </div>
  {/if}
  <footer>
    <label class="attach">
      📎
      <input type="file" multiple onchange={onPickImages} hidden />
    </label>
    <textarea
      bind:this={inputEl}
      bind:value={input}
      onfocus={() => { showNewProject = false; showManage = false; showManageSessions = false }}
      onkeydown={onKeydown}
      placeholder={streaming ? 'Agent is working — type to steer, Enter to send…' : 'Prompt pi… (Enter to send, Shift+Enter for newline)'}
      rows="2"
    ></textarea>
    <!-- While the agent works, an empty input means the button aborts;
         with content it becomes a steer/send button again -->
    {#if streaming && !input.trim() && attachments.length === 0}
      <button onclick={abort}>abort</button>
    {:else}
      <button onclick={sendPrompt} disabled={!input.trim() && attachments.length === 0}>send</button>
    {/if}
  </footer>

  <div class="statusbar">
    {#if stats}
      <span>tokens: {stats.tokens?.total?.toLocaleString() ?? '—'}</span>
      <span>cost: {fmtCost(stats.cost)}</span>
      {#if stats.contextUsage}
        <span>context: {stats.contextUsage.percent != null ? Math.round(stats.contextUsage.percent * 100) / 100 : '—'}% ({stats.contextUsage.tokens?.toLocaleString() ?? '—'}/{stats.contextUsage.contextWindow?.toLocaleString() ?? '—'})</span>
      {/if}
      <span>tool calls: {stats.toolCalls ?? 0}</span>
    {:else}
      <span>loading stats…</span>
    {/if}
  </div>
   </div>

   <button class="gap-dots" style="left: calc({(chatRatio * 100).toFixed(2)}% - 26px)" aria-label="Jump to one of your messages" onmouseenter={openMsgNav} onmouseleave={scheduleCloseMsgNav} onfocus={openMsgNav} onblur={scheduleCloseMsgNav} onclick={openMsgNav}>
    <span></span><span></span><span></span>
   </button>

   {#if showMsgNav}
    <div class="msgnav-popup" role="dialog" aria-label="Your messages" tabindex="-1" onmouseenter={openMsgNav} onmouseleave={closeMsgNav}>
      {#each userMessages as { e, i } (i)}
        <button class="msgnav-item" onclick={() => jumpToMessage(i)}>{truncate(e.text)}</button>
      {:else}
        <div class="msgnav-empty">No messages yet.</div>
      {/each}
    </div>
   {/if}

   <div class="vsplitter" role="separator" aria-orientation="vertical" aria-label="Resize chat/files split" onpointerdown={startChatSplitDrag}></div>
   <aside bind:this={asideEl}>
    <div class="browser" style="height: {(browserRatio * 100).toFixed(2)}%">
      <div class="browser-path">
        <span>/{browserPath}</span>
      </div>
      <div class="browser-body" onscrollcapture={updateFades}>
        <div class="fade fade-top" class:visible={dirFadeTop}></div>
        <div class="fade fade-bottom" class:visible={dirFadeBottom}></div>
        <div class="dirlist">
          {#if browserParent !== null}
            <button class="direntry" onclick={() => browse(browserParent)}>
              <span class="icon">📁</span>
              <span class="name">..</span>
            </button>
          {/if}
          {#each dirEntries as e}
            <button class="direntry" class:selected={selectedFile?.path === e.path} onclick={() => selectEntry(e)}>
              <span class="icon">{e.dir ? '📁' : '📄'}</span>
              <span class="name" class:git-modified={e.git === 'modified'} class:git-added={e.git === 'added' || e.git === 'untracked'} class:git-ignored={e.git === 'ignored'}>{e.name}</span>
              <span class="size">{fmtSize(e.size)}</span>
              <span class="gitmark" class:git-modified={e.git === 'modified'} class:git-added={e.git === 'added' || e.git === 'untracked'} class:git-ignored={e.git === 'ignored'} class:git-clean={e.git === 'clean'} title={e.git ?? ''}>{e.git === 'modified' ? 'M' : e.git === 'added' ? 'A' : e.git === 'untracked' ? 'U' : e.git === 'ignored' ? 'I' : e.git === 'clean' ? '✓' : ''}</span>
            </button>
          {/each}
        </div>
      </div>
    </div>
    <div class="splitter" role="separator" aria-orientation="horizontal" aria-label="Resize browser/viewer split" onpointerdown={startSplitDrag}></div>
    <div class="viewer">
      {#if selectedFile}
        <div class="viewer-head">
          <div class="head-left">
            <button class="nav" title="back" disabled={viewerHistory.length === 0} onclick={() => viewerGo(-1)}>&lt;</button>
            <button class="nav" title="forward" disabled={viewerFuture.length === 0} onclick={() => viewerGo(1)}>&gt;</button>
            <span class="fname">{selectedFile.path}</span>
          </div>
          <div class="head-mid">
            {#if viewerType}<span class="ftype">{viewerType}</span>{/if}
          </div>
          <div class="head-right">
            <button class="dl danger" title="delete file from disk" onclick={deleteViewerFile}>delete</button>
            <button class="dl" class:active={diffView} title="toggle git diff of this file" disabled={selectedFile.image || selectedFile.text === null} onclick={toggleDiff}>git diff</button>
            <a class="dl" href={`/download/${selectedFile.path}`} download>download</a>
          </div>
        </div>
      {/if}
      <div class="viewer-body" onscrollcapture={updateFades}>
        <div class="fade fade-top" class:visible={fadeTop}></div>
        <div class="fade fade-bottom" class:visible={fadeBottom}></div>
        {#if rulerMarks.length}
          <div class="diffruler">
            {#each rulerMarks as m}
              <div class="mark {m.kind}" style="top:{(m.top * 100).toFixed(3)}%; height:{(m.height * 100).toFixed(3)}%"></div>
            {/each}
          </div>
        {/if}
        {#if selectedFile && diffView}
          {#if diffView.error}
            <div class="filecontent binary">Diff failed: {diffView.error}</div>
          {:else if !diffView.diff}
            <div class="filecontent binary">No changes against git HEAD.</div>
          {:else}
            <pre class="filecontent diff"><code>{@html renderDiff(diffView.diff)}</code></pre>
          {/if}
        {:else if selectedFile}
          {#if selectedFile.image}
            <div class="filecontent image">
              <img src={`/raw/${encodeURI(selectedFile.path)}?v=${selectedFile.v}`} alt={selectedFile.path} />
            </div>
          {:else if selectedFile.text !== null && isMarkdown(selectedFile.path)}
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div class="filecontent md" onclick={onMdClick}>{@html renderMarkdown(selectedFile.text, selectedFile.path.split('/').slice(0, -1).join('/'))}</div>
          {:else if selectedFile.text !== null}
            <pre class="filecontent hljs"><code>{@html highlight(selectedFile.text, selectedFile.path)}</code></pre>
          {:else}
            <div class="filecontent binary">No preview ({selectedFile.reason}) — use download.</div>
          {/if}
        {:else}
          <div class="filecontent placeholder">Select a file to preview it here.</div>
        {/if}
      </div>
    </div>
   </aside>
  </div>
</main>
