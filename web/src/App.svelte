<script>
  import { onMount, tick } from 'svelte'
  import { slide } from 'svelte/transition'
  import { marked } from 'marked'
  import GithubSlugger from 'github-slugger'
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

  // Encode a project-relative path for use in a URL path (/raw/...,
  // /download/...): per segment, so '/' separators survive but '?', '#'
  // and '%' in file names don't break the URL (encodeURI leaves ?/#
  // intact and would truncate the path server-side).
  function encPath(p) {
    return String(p).split('/').map(encodeURIComponent).join('/')
  }

  // Markdown hrefs may be percent-encoded by the author (my%20file.png);
  // decode before re-encoding with encPath or the % would double-encode.
  // Raw filesystem paths from the API must NOT go through this.
  function decodeHref(href) {
    try {
      return decodeURIComponent(href)
    } catch {
      return href // malformed escape: use as-is
    }
  }

  // Heading ids for in-page anchors are GitHub-compatible slugs; the
  // instance is shared and reset per parse (see renderMarkdown).
  const slugger = new GithubSlugger()

  // Heading text reaches the renderer as HTML, so entities (&amp;, &#39;)
  // must be decoded before slugging or '&' headings get the wrong id.
  function decodeEntities(html) {
    const el = document.createElement('textarea')
    el.innerHTML = html
    return el.value
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

  // Custom marked renderers: alert blockquotes, highlighted code blocks,
  // sanitized raw HTML, and safe image/link handling
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
        return `<pre${hint}><button class="code-copy" type="button" title="Copy code" aria-label="Copy code"></button><code class="hljs">${html}</code></pre>`
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
          const path = normalizePath(mdImageBase ? `${mdImageBase}/${decodeHref(src)}` : decodeHref(src))
          return `<img${pre} src="/raw/${encPath(path)}"`
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
        const path = normalizePath(mdImageBase ? `${mdImageBase}/${decodeHref(href)}` : decodeHref(href))
        const title = token.title ? ` title="${escapeHtml(token.title)}"` : ''
        return `<img src="/raw/${encPath(path)}" alt="${escapeHtml(token.text || '')}"${title}>`
      },
      heading(token) {
        // marked emits plain <hN> without ids, so in-page anchors
        // (#table-of-contents links) would have no target. Add
        // GitHub-compatible slug ids; the slugger is reset per parse in
        // renderMarkdown so numbering of duplicates stays stable.
        const html = this.parser.parseInline(token.tokens)
        const id = slugger.slug(decodeEntities(html.replace(/<[^>]*>/g, '')))
        return `<h${token.depth} id="${escapeHtml(id)}">${html}</h${token.depth}>\n`
      },
      link(token) {
        // Drop links with unsafe schemes (javascript:, data:, ...) at
        // render time; keep the link text. Defense in depth on top of
        // the onMdClick handlers on chat and file viewer.
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
      if (text.length > 200_000) {
        return escapeHtml(text.slice(0, 200_000)) + '\n\n…(truncated)'
      }
      return hljs.highlightAuto(text).value
    } catch {
      // fall back to escaped plain text
      return escapeHtml(text)
    }
  }

  // Chat entries: { role: 'user'|'assistant'|'system', text, thinking?, images? } | { role: 'tool', ... }
  let entries = $state([])
  // Lazy chat history: only the last CHAT_PAGE entries are rendered
  // after a history load (markdown + highlight.js per message is the
  // dominant cost on big sessions); hiddenCount counts the leading
  // entries left out. Grows while streaming; trimmed again on the next
  // loadHistory().
  const CHAT_PAGE = 50
  let hiddenCount = $state(0)
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
  let showSettings = $state(false)
  // Sidebar (projects on top, sessions below): collapse state
  // persisted per project; small per-row popup menus (⋯) host the
  // destructive actions (delete session / detach project).
  let sidebarCollapsed = $state(localStorage.getItem('sidebarCollapsed:default') === '1')
  let sessionMenuFor = $state(null) // session path whose ⋯ menu is open
  let projectMenuFor = $state(null) // project id whose ⋯ menu is open
  let menuFlip = $state(false) // open the ⋯ menu upward when the row sits at the bottom of its scroll area

  // Mobile layout (<= 1024px): one full-screen pane at a time, switched
  // via a slide-in menu opened from a floating hamburger button. All
  // panes stay mounted (hidden via CSS on .body) to preserve scroll
  // positions, the editor draft and stream state.
  let isMobile = $state(false)
  let mobileView = $state('chat')
  let mobileMenuOpen = $state(false)
  const MOBILE_VIEWS = [
    ['projects', 'Projects'],
    ['sessions', 'Sessions'],
    ['messages', 'Messages'],
    ['chat', 'Chat'],
    ['files', 'Files'],
    // No 'viewer' entry: the viewer is reached by tapping a file in Files.
  ]
  function setMobileView(v) {
    mobileView = v
    mobileMenuOpen = false
    if (v === 'sessions') ensureSessionsLoaded()
    tick().then(updateFades)
  }

  const sidebarKey = () => `sidebarCollapsed:${currentProjectId || 'default'}`

  function loadSidebarState() {
    sidebarCollapsed = localStorage.getItem(sidebarKey()) === '1'
  }

  function toggleSidebar() {
    sidebarCollapsed = !sidebarCollapsed
    localStorage.setItem(sidebarKey(), sidebarCollapsed ? '1' : '0')
    if (!sidebarCollapsed) ensureSessionsLoaded()
  }

  // The ⋯ popup is anchored below its row inside the sidebar's scroll
  // container (.sb-list, overflow-y: auto). For the last visible row the
  // popup would extend past the visible scroll area and be clipped away —
  // so measure at open time and flip it upward when there is no room.
  function menuFitsBelow(btn) {
    const scroller = btn.closest('.sb-list')
    if (!scroller) return true
    const btnRect = btn.getBoundingClientRect()
    const listRect = scroller.getBoundingClientRect()
    // Approximate popup height: padding + one ~2rem item (matches .sb-menu).
    const menuH = 4 /* gap */ + 2.1 * 16
    return btnRect.bottom + menuH <= listRect.bottom + 1
  }

  function toggleSessionMenu(e, path) {
    e.stopPropagation()
    projectMenuFor = null
    if (sessionMenuFor === path) { sessionMenuFor = null; return }
    sessionMenuFor = path
    menuFlip = !menuFitsBelow(e.currentTarget)
  }

  function toggleProjectMenu(e, id) {
    e.stopPropagation()
    sessionMenuFor = null
    if (projectMenuFor === id) { projectMenuFor = null; return }
    projectMenuFor = id
    menuFlip = !menuFitsBelow(e.currentTarget)
  }

  async function deleteSession(s) {
    sessionMenuFor = null
    const warning = s.current
      ? '\n\nThis is the current session — you will be switched to the latest remaining session (or a new one).'
      : ''
    if (!window.confirm(`Delete session “${s.name}” from disk?${warning}\n\nThis cannot be undone.`)) return
    const resp = await apiPost('/delete_sessions', { paths: [s.path] })
    if (resp.errors?.length) {
      entries.push({ role: 'system', text: `⚠️ ${resp.errors.map((e) => `${e.path}: ${e.error}`).join('\n')}` })
    }
    await refreshSessions()
    // If the current session was deleted, move to the latest remaining
    // session (list is newest-first); if none is left, start a new one.
    if (s.current && resp.deleted?.includes(s.path)) {
      const next = sessions[0]
      if (next) {
        await switchToSession(next.path)
      } else {
        await newSession()
      }
    }
  }

  // New-project directory picker (popup). Shows only directories and is
  // confined to the workspace root (users of a web app usually don't
  // know absolute paths, so they browse instead).
  let npBase = $state('')
  let npPath = $state('')
  let npParent = $state(null)
  let npEntries = $state([])
  let npFolder = $state('')
  let npGit = $state(false)
  let npGitUrl = $state('')
  let npCloning = $state(false)

  // Auto-close the project/session dialogs when unused: closes after
  // DIALOG_IDLE_MS without interaction (any input/focus inside the
  // dialog resets the timer) or when the chat input gains focus.
  const DIALOG_IDLE_MS = 20_000
  let dialogTimer = null
  function pokeDialogTimer() {
    clearTimeout(dialogTimer)
    dialogTimer = setTimeout(() => {
      showNewProject = false
    }, DIALOG_IDLE_MS)
  }
  $effect(() => {
    if (showNewProject) pokeDialogTimer()
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
  // Escape closes the menu until the input changes again.
  let slashDismissed = $state(false)
  let slashSuggestions = $derived(
    !slashDismissed && input.startsWith('/') && !input.includes(' ')
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
    input // track
    slashDismissed = false
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
  // Width of the sessions sidebar in px, draggable via its right-edge
  // splitter; persisted like the other split ratios. Defaults to 1/8
  // of the window width (clamped to the drag limits).
  let sidebarWidth = $state(
    Number(localStorage.getItem('sidebarWidth')) ||
      Math.min(400, Math.max(150, Math.round(window.innerWidth / 8)))
  )
  // Shared pointer-drag plumbing for the splitters: `apply` maps a
  // pointer event to the new ratio/width, `key` persists it on release.
  // While a drag is in progress the hover-expand of the projects pane
  // and the directory browser is suppressed: the pointer rests inside
  // those panes during the drag, and an inline expand height would
  // override the ratio the user is dragging, making it feel stuck.
  let splitterDragging = $state(false)
  function dragSplit(ev, apply, key) {
    ev.preventDefault()
    splitterDragging = true
    // drop any pending/persisting expansion so the drag moves the
    // pane borders immediately
    clearTimeout(projHoverTimer)
    projExpandedMax = ''
    clearTimeout(browserHoverTimer)
    browserHovered = false
    browserExpandedHeight = null
    const move = (e) => apply(e)
    const up = () => {
      splitterDragging = false
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      const value = { chatRatio, browserRatio, sidebarWidth }[key]
      localStorage.setItem(key, String(value))
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
  function startSidebarDrag(ev) {
    // The sidebar starts at the window's left padding edge; measure the
    // current left edge once and track the pointer from there.
    const left = ev.currentTarget.parentElement.getBoundingClientRect().left
    dragSplit(ev, (e) => {
      sidebarWidth = Math.min(400, Math.max(150, e.clientX - left))
    }, 'sidebarWidth')
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
  let chatFadeTop = $state(false)
  let chatFadeBottom = $state(false)
  let projFadeTop = $state(false)
  let projFadeBottom = $state(false)
  let sessFadeTop = $state(false)
  let sessFadeBottom = $state(false)
  function fadesFor(selector) {
    const el = document.querySelector(selector)
    if (!el) return { top: false, bottom: false }
    return {
      top: el.scrollTop > 8,
      bottom: el.scrollHeight - el.scrollTop - el.clientHeight > 8,
    }
  }
  // Hover-expand of the projects section: when not all projects are
  // visible, resting the mouse on the section for ~300ms grows it
  // (eased) until everything fits — capped at 80% of the window
  // height. Leaving collapses it back to the default 1/3 cap.
  let projExpandedMax = $state('') // inline max-height while hovered
  // Dwell delay: the mouse must rest on the pane this long before it
  // expands, so a quick pass-through doesn't trigger the effect.
  let projHoverTimer = 0
  function scheduleExpandProjects() {
    if (splitterDragging) return
    clearTimeout(projHoverTimer)
    projHoverTimer = setTimeout(() => { if (!isMobile) expandProjects() }, 300)
  }
  // Natural (unstretched) height of a scrolling list's content. The
  // lists are flex children that stretch to fill their pane, so when
  // the content is shorter than the pane, scrollHeight == clientHeight
  // (the stretched height) — useless for computing a shrink target.
  // Instead sum the children's boxes (offsetTop is relative to the
  // same positioned ancestor for list and children alike).
  function listContentHeight(list) {
    let h = 0
    for (const c of list.children) {
      const cs = getComputedStyle(c)
      h = Math.max(h, c.offsetTop + c.offsetHeight + parseFloat(cs.marginBottom || '0'))
    }
    const cs = getComputedStyle(list)
    return h + parseFloat(cs.paddingTop || '0') + parseFloat(cs.paddingBottom || '0')
  }

  function expandProjects() {
    const list = document.querySelector('.sb-projects .sb-list')
    const section = document.querySelector('.sb-projects')
    if (!list || !section) return
    if (isMobile) return
    keepVisibleSel = null // hovering again ends the post-collapse watch
    // header + full list content; independent of any previous
    // expansion (the pane's current height must not feed into the
    // measurement, otherwise repeated expands would over-add)
    const needed = section.offsetHeight - list.clientHeight + listContentHeight(list)
    const defaultMax = window.innerHeight / 3 // the CSS 33.33vh cap
    if (needed <= defaultMax + 1) {
      // default cap already shows everything — drop the inline value
      projExpandedMax = ''
      return
    }
    projExpandedMax = `${Math.min(needed, 0.8 * window.innerHeight)}px`
  }
  // After a pane collapses (250ms eased transition), make sure the
  // selected item is still within the shrunken viewport: if it got cut
  // off by the shrink, scroll the least amount that brings it back.
  function keepSelectedVisible(listSel, itemSel) {
    const list = document.querySelector(listSel)
    const item = list?.querySelector(itemSel)
    if (!list || !item) return
    const lr = list.getBoundingClientRect()
    const ir = item.getBoundingClientRect()
    if (ir.top < lr.top) list.scrollTop -= lr.top - ir.top
    else if (ir.bottom > lr.bottom) list.scrollTop += ir.bottom - lr.bottom
  }
  // Track the collapse so the item stays pinned while the pane shrinks:
  // a one-shot check after the nominal 250ms can fire mid-transition on
  // a busy machine (heavy layout, long lists), after which the pane
  // keeps shrinking and cuts the item off again. Instead re-check every
  // frame and stop early once the collapse has surely finished AND the
  // selected item is inside the viewport — but keep watching for a while
  // (3s) even after a "good" frame, because selecting a project switches
  // the backend state and the .current class can move to the newly
  // clicked item only after that reload lands, well after the collapse.
  let keepVisibleRaf = 0
  let keepVisibleUntil = 0
  let keepVisibleSel = null // [listSel, itemSel] from the last collapse
  function trackSelectedDuringCollapse(listSel, itemSel) {
    keepVisibleSel = [listSel, itemSel]
    keepVisibleUntil = performance.now() + 3000
    if (keepVisibleRaf) return // already tracking
    const step = () => {
      if (keepVisibleSel) keepSelectedVisible(keepVisibleSel[0], keepVisibleSel[1])
      if (performance.now() >= keepVisibleUntil) {
        keepVisibleRaf = 0
        return
      }
      keepVisibleRaf = requestAnimationFrame(step)
    }
    keepVisibleRaf = requestAnimationFrame(step)
  }
  function collapseProjects() {
    clearTimeout(projHoverTimer)
    projExpandedMax = ''
    trackSelectedDuringCollapse('.sb-projects .sb-list', '.sb-item.current')
  }

  // Same hover-expand for the directory browser: when entries are cut
  // off, resting the mouse on the pane for ~300ms grows it (eased,
  // max 80% of the window height); leaving restores the splitter-set
  // ratio height.
  let browserExpandedHeight = $state(null) // px while hovered, null = ratio
  let browserHovered = $state(false) // mouse currently over the pane
  let browserHoverTimer = 0 // dwell timer, like the projects pane
  function expandBrowser() {
    browserHovered = true
    if (isMobile) return
    keepVisibleSel = null // hovering again ends the post-collapse watch
    const list = document.querySelector('.browser-body .dirlist')
    const pane = document.querySelector('.browser')
    if (!list || !pane || !pane.parentElement) return
    // header + path bar + full list content; independent of any
    // previous expansion (measuring pane.offsetHeight + overflow would
    // over-add when the pane is already expanded)
    const needed = pane.offsetHeight - list.clientHeight + listContentHeight(list)
    // the splitter-set ratio height: .browser height is a % of the
    // aside it sits in — that default, NOT the currently expanded
    // height, decides whether expanding is needed at all
    const defaultH = pane.parentElement.clientHeight * browserRatio
    if (needed <= defaultH + 1) {
      // ratio height already shows everything — back to it
      browserExpandedHeight = null
      return
    }
    browserExpandedHeight = `${Math.min(needed, 0.8 * window.innerHeight)}px`
  }
  function scheduleExpandBrowser() {
    if (splitterDragging) return
    clearTimeout(browserHoverTimer)
    browserHoverTimer = setTimeout(() => { if (!isMobile) expandBrowser() }, 300)
  }
  function collapseBrowser() {
    clearTimeout(browserHoverTimer)
    browserHovered = false
    browserExpandedHeight = null
    trackSelectedDuringCollapse('.browser-body .dirlist', '.direntry.selected')
  }

  // Re-measure whenever the browser content changes while hovered:
  // navigating into another directory can add/remove entries, so the
  // expand height must follow the new content. Effects run after the
  // DOM update, so the measured content height is already correct.
  $effect(() => {
    dirEntries // track
    browserPath // track
    if (browserHovered && !splitterDragging) expandBrowser()
  })
  function updateFades() {
    ;({ top: fadeTop, bottom: fadeBottom } = fadesFor(editMode ? '.viewer-body .editarea' : '.viewer-body .filecontent'))
    ;({ top: chatFadeTop, bottom: chatFadeBottom } = fadesFor('.chat-body .chat'))
    ;({ top: projFadeTop, bottom: projFadeBottom } = fadesFor('.sb-projects .sb-list'))
    ;({ top: sessFadeTop, bottom: sessFadeBottom } = fadesFor('.sb-sessions .sb-list'))
  }

  // Recompute once new content has rendered.
  $effect(() => {
    selectedFile // track
    dirEntries // track
    entries.length // track (chat grows while streaming)
    projects // track
    sessions // track
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

  // Diff view: when set, the viewer shows an in-file diff of the selected
  // file (full content with changed lines highlighted; the backend sends
  // a full-context git diff). { path, diff } (diff === '' means no
  // changes, diff === null means error — message in `error`). Cleared
  // whenever another file is shown.
  let diffView = $state(null)
  let wrapView = $state(false)
  // True while a diff is being fetched/rendered or a file is being
  // loaded into the viewer — shows a centered spinner overlay on the
  // viewer (noticeable on large files).
  let diffLoading = $state(false)
  let viewerLoading = $state(false)

  // Viewer edit mode: the draft buffer (editText) is separate from
  // selectedFile.text, so refreshFiles reloading the file after agent
  // runs can never clobber unsaved edits. editBase is the disk text
  // the buffer started from — the basis for the dirty check and for
  // the save endpoint's optimistic concurrency check (pi may edit the
  // same file via tools; a changed file on disk means a save conflict).
  let editMode = $state(false)
  let editPath = $state('')
  let editText = $state('')
  let editBase = $state('')
  // Disk-divergence state: 'changed' (file changed on disk while
  // editing), 'save' (save rejected: disk != base), 'gone' (file no
  // longer exists on disk). null = no known divergence.
  let editConflict = $state(null)
  let editSaving = $state(false)
  let editEl = $state(null) // the editor textarea
  const editDirty = $derived(editMode && editText !== editBase)

  // Highlighted underlay for the editor: a transparent textarea on top
  // shows caret, selection and (invisible) text; the hljs-colored code
  // sits underneath with identical font metrics and synced scroll.
  // Debounced so large files don't re-highlight on every keystroke.
  let editHtml = $state('')
  let editHlTimer = null
  $effect(() => {
    if (!editMode) return
    const text = editText
    const path = editPath
    clearTimeout(editHlTimer)
    editHlTimer = setTimeout(() => {
      editHtml = highlightForEdit(text, path)
    }, 150)
  })

  // Like highlight(), but never truncates: the underlay must render
  // the exact buffer text or it loses alignment with the textarea.
  function highlightForEdit(text, path) {
    const ext = path?.split('.').pop()?.toLowerCase()
    try {
      if (ext && hljs.getLanguage(ext)) return hljs.highlight(text, { language: ext }).value
      if (text.length > 200_000) return escapeHtml(text) // alignment over coloring
      return hljs.highlightAuto(text).value
    } catch {
      return escapeHtml(text)
    }
  }

  // Underlay content: a trailing newline collapses inside <pre> but
  // not inside <textarea>, so add one more to keep the layers aligned.
  const editUnderHtml = $derived(editHtml + (editText.endsWith('\n') ? '\n' : ''))

  // Blur+spinner overlay (the .diff-loading layer) during the
  // enter/quit transition: entering runs a synchronous hljs pass
  // (noticeable on large files), and both directions swap the viewer
  // content, which looks janky unmasked. A minimum display time keeps
  // tiny files from flickering the overlay.
  let editSwitching = $state(false)
  const EDIT_SWITCH_MIN_MS = 250
  const nextFrame = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))

  function enterEdit() {
    if (editMode || editSwitching || !selectedFile || selectedFile.text === null || selectedFile.image) return
    clearTimeout(editHlTimer)
    const started = Date.now()
    editSwitching = true
    // Async IIFE: quitEdit-style guards need this function to stay
    // fire-and-forget; the overlay must paint before the sync hljs
    // pass, so the work happens after a rendered frame.
    void (async () => {
      await nextFrame()
      editMode = true
      editPath = selectedFile.path
      editBase = selectedFile.text
      editText = selectedFile.text
      editConflict = null
      editHtml = highlightForEdit(editText, editPath)
      diffView = null // a diff against HEAD would go stale with every edit
      await tick() // editor DOM rendered (still under the blur)
      const wait = EDIT_SWITCH_MIN_MS - (Date.now() - started)
      if (wait > 0) await new Promise((r) => setTimeout(r, wait))
      editSwitching = false
      if (editEl) {
        // Focus alone leaves caret/scroll browser-dependent (some put
        // the caret at the end): start at the top of the file.
        editEl.focus()
        editEl.setSelectionRange(0, 0)
        editEl.scrollTop = 0
        editEl.scrollLeft = 0
        syncEditScroll()
      }
    })()
  }

  // Leave edit mode; returns false when the user cancels at the
  // discard prompt — callers use this to guard file/project switches.
  function quitEdit({ force = false } = {}) {
    if (!editMode) return true
    if (!force && editDirty && !confirm(`Discard unsaved changes to ${editPath}?`)) return false
    clearTimeout(editHlTimer)
    editMode = false
    editConflict = null
    // Mask the content swap: overlay for one painted frame plus the
    // minimum display time. Fire-and-forget — the mode flip is
    // synchronous so guard callers (if (!quitEdit()) return) work.
    const started = Date.now()
    editSwitching = true
    tick()
      .then(nextFrame)
      .then(() => {
        const wait = EDIT_SWITCH_MIN_MS - (Date.now() - started)
        setTimeout(() => (editSwitching = false), Math.max(0, wait))
      })
    return true
  }

  async function saveEdit(force = false) {
    if (!editMode || editSaving) return
    if (!force && !editDirty) return
    editSaving = true
    try {
      const resp = await apiPost('/api/file/save', { path: editPath, text: editText, base: editBase, force })
      if (resp.success) {
        editConflict = null
        editBase = editText
        if (selectedFile?.path === editPath) selectedFile = { ...selectedFile, text: editText }
        await browse(browserPath) // refresh git status marks (likely just became 'M')
        return
      }
      if (resp.conflict) {
        // Disk diverged from the base (agent-side edit): the banner
        // offers overwrite / reload / keep editing.
        editConflict = 'save'
        return
      }
      entries.push({ role: 'system', text: `⚠️ save failed: ${resp.error ?? 'unknown error'}` })
    } finally {
      editSaving = false
    }
  }

  // Discard the draft and adopt the current disk content.
  async function reloadEditFromDisk() {
    viewerLoading = true
    let f
    try {
      f = await loadViewerFile(editPath)
    } finally {
      viewerLoading = false
    }
    if (f.error || f.text === null) {
      entries.push({ role: 'system', text: `⚠️ reload failed for ${editPath}` })
      return
    }
    clearTimeout(editHlTimer)
    editBase = f.text
    editText = f.text
    editHtml = highlightForEdit(f.text, editPath)
    editConflict = null
    selectedFile = f
  }

  // A fresh copy of the currently edited file arrived from disk
  // (agent-run refresh or explicit reload). Unchanged → nothing to do;
  // clean buffer → silently adopt the new content; dirty buffer →
  // flag the divergence, the user decides via the banner.
  function syncEditBase(f) {
    if (!editMode || f.path !== editPath) return
    if (f.error || f.text === null) {
      editConflict = 'gone'
      return
    }
    if (f.text === editBase) {
      if (editConflict === 'changed' || editConflict === 'gone') editConflict = null
      return
    }
    if (!editDirty) {
      clearTimeout(editHlTimer)
      editBase = f.text
      editText = f.text
      editHtml = highlightForEdit(f.text, editPath)
      editConflict = null
    } else {
      editConflict = 'changed'
    }
  }

  // Mirror the textarea's scroll offsets onto the highlight underlay
  // (the textarea is the only scrolling layer).
  function syncEditScroll() {
    const under = editEl?.parentElement?.querySelector('.editunder')
    if (!under) return
    under.scrollTop = editEl.scrollTop
    under.scrollLeft = editEl.scrollLeft
  }

  // Ctrl+S saves (cut/copy/paste/undo/redo are native textarea
  // behavior); Esc quits edit mode like the quit button.
  function onEditKeydown(e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault()
      saveEdit()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      quitEdit()
    }
  }

  async function toggleDiff() {
    if (diffView) {
      diffView = null
      return
    }
    const path = selectedFile?.path
    if (!path) return
    diffLoading = true
    try {
      diffView = await apiGet(`/api/diff?path=${encodeURIComponent(path)}`)
    } finally {
      diffLoading = false
    }
    // Jump to the first diff block once the diff is rendered.
    await tick()
    document.getElementById('diffblock-0')?.scrollIntoView({ block: 'center' })
  }

  // Parse a full-context unified diff into the file body: header and
  // hunk lines are dropped, the leading +/-/space marker is stripped,
  // leaving { kind, text } per file line ('add' | 'del' | 'ctx').
  function diffBody(text) {
    const out = []
    for (const line of text.split('\n')) {
      if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@') || /^(diff |index |new file|deleted file|similarity|rename |old mode|new mode|Binary files|\\ No newline)/.test(line)) continue
      let kind = 'ctx'
      if (line.startsWith('+')) kind = 'add'
      else if (line.startsWith('-')) kind = 'del'
      out.push({ kind, text: line.slice(1) })
    }
    // Drop the trailing empty context line from the diff's final newline.
    if (out.length && out[out.length - 1].kind === 'ctx' && out[out.length - 1].text === '') out.pop()
    return out
  }

  // Highlight an array of lines as one document (so multi-line
  // constructs like block comments highlight correctly), then split
  // the result back into per-line HTML. hljs only emits <span> tags;
  // spans left open at a line break are closed at the end of the line
  // and reopened on the next, so every returned line is balanced.
  function highlightLines(lines, path) {
    const ext = path?.split('.').pop()?.toLowerCase()
    const known = ext && hljs.getLanguage(ext)
    const text = lines.join('\n')
    if (!known && text.length > 200_000) return lines.map(escapeHtml)
    try {
      const html = known
        ? hljs.highlight(text, { language: ext }).value
        : hljs.highlightAuto(text).value
      const out = []
      const open = []
      for (const raw of html.split('\n')) {
        let line = open.join('') + raw
        for (const m of raw.matchAll(/<span[^>]*>|<\/span>/g)) {
          if (m[0] === '</span>') open.pop()
          else open.push(m[0])
        }
        line += '</span>'.repeat(open.length)
        out.push(line)
      }
      return out
    } catch {
      return lines.map(escapeHtml)
    }
  }

  // Render the in-file diff: complete file content with GitHub-style
  // full-line backgrounds on changed lines and a +/- gutter. The first
  // line of each changed run gets an id so the prev/next buttons can
  // scroll to it. Syntax highlighting is applied to the new and the
  // old file version separately (ctx+add / ctx+del lines), so deleted
  // lines are highlighted too.
  function renderDiff(text, path) {
    const body = diffBody(text)
    const newLines = highlightLines(body.filter((l) => l.kind !== 'del').map((l) => l.text), path)
    const oldLines = highlightLines(body.filter((l) => l.kind !== 'add').map((l) => l.text), path)
    let ni = 0
    let oi = 0
    let block = 0
    let prevKind = 'ctx'
    return body
      .map(({ kind }) => {
        const marker = kind === 'add' ? '+' : kind === 'del' ? '-' : ' '
        let html
        if (kind === 'del') html = oldLines[oi++]
        else if (kind === 'add') html = newLines[ni++]
        else {
          html = newLines[ni++]
          oi++
        }
        let id = ''
        if (kind !== 'ctx' && prevKind === 'ctx') id = ` id="diffblock-${block++}"`
        prevKind = kind
        return `<span class="dline ${kind}"${id}><span class="dgut">${marker}</span>${html}\n</span>`
      })
      .join('')
  }

  // ----- overview ruler (VS Code / meld style) -----
  // Colored marks on the right edge of the viewer, shown only while a
  // diff is rendered: +/- runs positioned by their line index within
  // the diff text, scaled to the full diff height.
  function marksForDiff(diffText) {
    const lines = diffBody(diffText)
    const total = lines.length
    const marks = []
    let start = 0
    let kind = null
    const flush = (i) => {
      if (kind) marks.push({ top: start / total, height: (i - start) / total, kind })
      kind = null
    }
    lines.forEach((line, i) => {
      const k = line.kind === 'ctx' ? null : line.kind
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

  // A non-empty diff whose body parses to nothing has only metadata
  // changes (e.g. a file-mode flip) — show a message instead of an
  // empty pane.
  const diffMetaOnly = $derived(!!diffView?.diff && diffBody(diffView.diff).length === 0)

  // Number of diff blocks (contiguous runs of changed lines, +/- mixed)
  // in the active diff — drives the prev/next navigation buttons. Must
  // match the diffblock-N ids assigned by renderDiff.
  function countDiffBlocks(text) {
    let n = 0
    let prevKind = 'ctx'
    for (const { kind } of diffBody(text)) {
      if (kind !== 'ctx' && prevKind === 'ctx') n++
      prevKind = kind
    }
    return n
  }
  const diffBlockCount = $derived(diffView?.diff && !diffMetaOnly ? countDiffBlocks(diffView.diff) : 0)

  // Scroll-position state: true when the viewer is scrolled on/above the
  // first diff block (diffAtFirst) or on/below the last one (diffAtLast)
  // — disables the corresponding navigation button.
  let diffAtFirst = $state(true)
  let diffAtLast = $state(false)
  $effect(() => {
    diffView // reset navigation whenever the diff view changes
    diffAtFirst = true
    diffAtLast = false
    tick().then(updateDiffNav)
  })

  // Diff navigation is purely position-based: the reference line is the
  // viewport center, and each block's position is its vertical center.
  // Returns null when no diff navigation is possible right now.
  function diffNavState() {
    if (!diffBlockCount) return null
    // The scrollable element is the .filecontent pane, not .viewer-body.
    const scroller = document.querySelector('.viewer-body .filecontent')
    if (!scroller) return null
    const base = scroller.getBoundingClientRect().top
    const pos = scroller.scrollTop + scroller.clientHeight / 2
    const centers = []
    for (let i = 0; i < diffBlockCount; i++) {
      const el = document.getElementById(`diffblock-${i}`)
      if (!el) return null
      centers.push(el.getBoundingClientRect().top + el.offsetHeight / 2 - base + scroller.scrollTop)
    }
    return { pos, centers }
  }

  // Sync the button states with the viewer's scroll position. Called on
  // scroll and after the diff renders.
  function updateDiffNav() {
    const s = diffNavState()
    if (!s) return
    diffAtFirst = s.centers[0] >= s.pos - 4
    diffAtLast = s.centers[s.centers.length - 1] <= s.pos + 4
  }

  // Jump to the first diff block below (dir > 0) or the last block above
  // (dir < 0) the viewport center.
  function diffGo(dir) {
    const s = diffNavState()
    if (!s) return
    let target = -1
    if (dir > 0) {
      for (let i = 0; i < s.centers.length; i++) {
        if (s.centers[i] > s.pos + 4) {
          target = i
          break
        }
      }
    } else {
      for (let i = s.centers.length - 1; i >= 0; i--) {
        if (s.centers[i] < s.pos - 4) {
          target = i
          break
        }
      }
    }
    if (target < 0) return
    document.getElementById(`diffblock-${target}`)?.scrollIntoView({ block: 'center' })
    updateDiffNav()
  }


  // Toggle between rendered and plain (source) view for markdown/HTML.
  let plainView = $state(false)
  function pushViewerHistory(prevPath, newPath) {
    if (prevPath && prevPath !== newPath) {
      viewerHistory.push(prevPath)
      viewerFuture = []
    }
  }

  // Show a file at `path` and sync the directory browser to its location
  // (the selected entry is highlighted via selectedFile.path).
  async function showFileAt(path) {
    viewerLoading = true
    let f
    try {
      f = await loadViewerFile(path)
    } finally {
      viewerLoading = false
    }
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
    if (!quitEdit()) return
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
  // HTML files are rendered in a sandboxed iframe via /raw so relative
  // assets resolve. `allow-scripts` (without allow-same-origin) lets
  // the page's own JS run in an opaque origin — no access to the app's
  // cookies, storage, or API.
  const isHtml = (path) => /\.x?html?$/i.test(path ?? '')
  // Image files are rendered as <img> via /raw (scaled to fit the
  // viewer, see .filecontent.image) instead of the binary placeholder.
  const isImage = (path) => /\.(png|jpe?g|gif|webp|bmp|ico|avif|svg)$/i.test(path ?? '')

  // Load a file for the viewer: images only need an existence check
  // (the <img> streams the bytes itself, so MAX_PREVIEW_BYTES doesn't
  // apply); `v` cache-busts the <img> after agent runs (refreshFiles).
  async function loadViewerFile(path) {
    const f = await apiGet(`/api/file?path=${encodeURIComponent(path)}`)
    if (!f.path || f.error) return f
    // `v` also cache-busts the HTML preview iframe after agent runs.
    return isImage(f.path) ? { path: f.path, image: true, v: Date.now() } : { ...f, v: Date.now() }
  }
  let toolsUsedInRun = $state(false)
  let autoNaming = $state(false)

  async function browse(path) {
    const resp = await apiGet(`/api/list?path=${encodeURIComponent(path)}`)
    if (!resp.entries) {
      // 404: the directory is gone — fall back to the project root.
      // Transient failures (network error, 500): keep the current
      // listing instead of silently wiping the browser pane.
      if (resp.status === 404 && path) return browse('')
      if (path) return
      browserPath = ''
      browserParent = null
      dirEntries = []
      return
    }
    browserPath = resp.path ?? ''
    browserParent = resp.parent ?? null
    dirEntries = resp.entries
  }

  // Link clicks inside rendered markdown (chat and file viewer).
  // Relative file links
  // would otherwise resolve against the app page URL (e.g. /app.py) and
  // navigate the SPA away to an invalid endpoint; instead load them in
  // the viewer, resolved against the markdown file's own directory.
  // External links open in a new tab; in-page anchors (#heading-slug) are
  // scrolled explicitly inside their own scroll container — the app URL
  // has no matching document fragment, so the browser default would just
  // dirty the location bar without moving the viewer/chat pane.
  function normalizePath(p) {
    const parts = []
    for (const seg of p.split('/')) {
      if (!seg || seg === '.') continue
      if (seg === '..') parts.pop()
      else parts.push(seg)
    }
    return parts.join('/')
  }

  // `frag`: optional #fragment of the link — after the file is rendered,
  // scroll the viewer to that heading (links like [x](other.md#section)).
  async function openLinkedFile(path, frag = '') {
    if (path !== editPath && !quitEdit()) return
    const prev = selectedFile?.path
    if (await showFileAt(path)) {
      pushViewerHistory(prev, path)
      if (frag) {
        await tick()
        const pane = document.querySelector('.viewer-body .filecontent')
        if (pane) scrollToAnchor(pane, frag)
      }
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

  // `base`: directory that relative links resolve against — '' (project
  // root) for chat messages, the file's own directory for the viewer.
  function onMdClick(e, base = '') {
    // Copy button injected into rendered code blocks (chat + md viewer);
    // handled here via delegation because the blocks come from {@html}.
    const copyBtn = e.target.closest('.code-copy')
    if (copyBtn) {
      e.preventDefault()
      copyText(copyBtn.parentElement?.querySelector('code')?.innerText ?? '', copyBtn)
      return
    }
    const a = e.target.closest('a')
    if (!a) return
    const href = a.getAttribute('href') ?? ''
    if (!href) return
    if (href.startsWith('#')) {
      e.preventDefault()
      scrollToAnchor(a, href.slice(1))
      return
    }
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
    const [pathPart, frag] = href.split('#')
    if (!pathPart) return
    // Author-written hrefs may be percent-encoded (my%20file.md).
    const decoded = decodeHref(pathPart)
    openLinkedFile(normalizePath(base ? `${base}/${decoded}` : decoded), frag)
  }

  // Scroll to the element carrying `id` within the pane the link lives in
  // (.filecontent for the viewer, .chat for chat messages), so the rest
  // of the layout stays put. Ids come from the heading renderer's slugs;
  // hrefs may be percent-encoded (e.g. emoji headings), hence decodeURI.
  function scrollToAnchor(fromEl, rawId) {
    const pane = fromEl.closest('.filecontent, .chat')
    if (!pane || !rawId) return false
    let id = rawId
    try {
      id = decodeURIComponent(rawId)
    } catch {
      // malformed escape sequence: use the raw fragment as-is
    }
    const esc = (v) => (window.CSS?.escape ? CSS.escape(v) : v.replace(/[^\w-]/g, '\\$&'))
    let target =
      pane.querySelector(`#${esc(id)}`) ??
      pane.querySelector(`#${esc(rawId)}`) ??
      pane.querySelector(`[name="${esc(id)}"]`)
    if (!target) {
      // Tolerant fallback: hand-written or tool-generated TOCs often
      // disagree with the slug on non-alphanumerics (emoji, variation
      // selectors, punctuation), e.g. '#\u{fe0f}-low-x' vs id 'ℹ\u{fe0f}-low-x'.
      // Compare on [a-z0-9-] only before giving up.
      const norm = (v) => v.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
      const want = norm(id)
      target = [...pane.querySelectorAll('[id]')].find((el) => norm(el.id) === want)
    }
    if (!target) return false
    pane.scrollTop += target.getBoundingClientRect().top - pane.getBoundingClientRect().top - 8
    return true
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
    quitEdit({ force: true }) // the file is gone, saving is impossible
    viewerHistory = viewerHistory.filter((p) => p !== path)
    viewerFuture = viewerFuture.filter((p) => p !== path)
    selectedFile = null
    diffView = null
    await browse(browserPath)
  }

  // Delete the directory currently shown in the browser (after
  // confirmation) together with everything below it, then navigate to
  // its parent. Any file from the deleted subtree is dropped from the
  // viewer and its back/forward stacks.
  async function deleteBrowserDir() {
    const path = browserPath
    if (!path) return // project root — nothing above to delete safely
    if (!confirm(`Delete /${path} and all files and directories below it?`)) return
    const resp = await apiPost('/api/file/delete', { path })
    if (!resp.success) {
      entries.push({ role: 'system', text: `⚠️ delete failed: ${resp.error ?? 'unknown error'}` })
      return
    }
    if (selectedFile?.path.startsWith(path + '/')) {
      quitEdit({ force: true })
      selectedFile = null
      diffView = null
    }
    viewerHistory = viewerHistory.filter((p) => !(p === path || p.startsWith(path + '/')))
    viewerFuture = viewerFuture.filter((p) => !(p === path || p.startsWith(path + '/')))
    await browse(browserParent ?? '')
  }

  // "New file" / "new dir" input mask (modal). Creates the entry in
  // the directory the browser currently shows (browserPath).
  let newEntryKind = $state(null) // null | 'file' | 'dir'
  let newEntryName = $state('')
  let newEntryError = $state('')
  let newEntryBusy = $state(false)
  let newEntryInput = $state(null)

  function openNewEntry(kind) {
    newEntryKind = kind
    newEntryName = ''
    newEntryError = ''
    tick().then(() => newEntryInput?.focus())
  }

  async function createNewEntry() {
    const name = newEntryName.trim()
    if (!name || newEntryBusy) return
    if (name.includes('/') || name === '.' || name === '..') {
      newEntryError = 'Name must not contain / or be . / ..'
      return
    }
    newEntryBusy = true
    newEntryError = ''
    const resp = await apiPost('/api/file/create', { path: browserPath, name, dir: newEntryKind === 'dir' })
    newEntryBusy = false
    if (!resp.success) {
      newEntryError = resp.error ?? 'create failed'
      return
    }
    const kind = newEntryKind
    newEntryKind = null
    if (kind === 'file') {
      await showFileAt(resp.path)
    } else {
      await browse(browserPath)
    }
  }

  async function selectEntry(e) {
    if (e.dir) {
      if (!quitEdit()) return
      selectedFile = null
      diffView = null
      await browse(e.path)
      return
    }
    // Switching to another file requires leaving edit mode first.
    if (editMode && e.path !== editPath && !quitEdit()) return
    const prev = selectedFile?.path
    viewerLoading = true
    let f
    try {
      f = await loadViewerFile(e.path)
    } finally {
      viewerLoading = false
    }
    // Re-clicking the edited file: stay in edit mode, reconcile the
    // buffer with the disk content (may flag a conflict banner).
    if (editMode && e.path === editPath) {
      if (f.path && !f.error) {
        selectedFile = f
        syncEditBase(f)
      }
      return
    }
    if (f.path && !f.error) pushViewerHistory(prev, e.path)
    diffView = null
    selectedFile = f
    rememberFile(e.path)
    if (isMobile) mobileView = 'viewer'
  }

  // After a run that used tools, files may have changed: reload the current
  // directory and the open file. If either vanished in the meantime, fall
  // back to the project root.
  async function refreshFiles() {
    const list = await apiGet(`/api/list?path=${encodeURIComponent(browserPath)}`)
    if (!list.entries) {
      // Directory gone: fall back to the project root. Transient
      // failures (network error, 500) keep the current listing and
      // viewer state instead of discarding them.
      if (list.status === 404) {
        selectedFile = null
        await browse('')
      }
      return
    }
    browserPath = list.path ?? ''
    browserParent = list.parent ?? null
    dirEntries = list.entries
    if (selectedFile) {
      viewerLoading = true
      let f
      try {
        f = await loadViewerFile(selectedFile.path)
      } finally {
        viewerLoading = false
      }
      if (f.error) {
        // 404: the file really is gone — clear the viewer (and keep the
        // edit draft with a conflict banner). Transient failures keep
        // the current state.
        if (f.status === 404) {
          if (editMode) editConflict = 'gone' // draft kept; banner offers discard
          selectedFile = null
          diffView = null
          await browse('')
        }
      } else {
        selectedFile = f
        syncEditBase(f) // reconcile an open edit buffer with the disk content
        // Keep an open diff view current after agent runs.
        if (diffView) {
          diffLoading = true
          try {
            diffView = await apiGet(`/api/diff?path=${encodeURIComponent(f.path)}`)
          } finally {
            diffLoading = false
          }
        }
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
    slugger.reset()
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
  // Forced calls (submit, history load) must never be swallowed by an
  // already-queued non-forced one, and they re-pin once more after the
  // first frame: layout keeps shifting right after a submit (the prompt
  // textarea shrinks back, attached images decode, highlighting settles),
  // which would otherwise leave the view short of the bottom.
  let scrollQueued = false
  let scrollForce = false
  function scrollToBottom(force = false) {
    if (!chatEl) return
    const stick = force || chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 40
    if (!stick) return
    scrollForce ||= force
    if (scrollQueued) return
    scrollQueued = true
    requestAnimationFrame(async () => {
      scrollQueued = false
      const forced = scrollForce
      scrollForce = false
      await tick()
      const pin = () => {
        if (chatEl) chatEl.scrollTop = chatEl.scrollHeight
      }
      pin()
      if (forced) {
        requestAnimationFrame(pin)
        setTimeout(pin, 80) // late async growth (image decode etc.)
      }
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
  async function jumpToMessage(i) {
    closeMsgNav()
    // Target may be outside the rendered window — reveal enough of
    // the history first, then scroll once it exists in the DOM.
    if (i < hiddenCount) {
      hiddenCount = Math.max(0, i - CHAT_PAGE)
      await tick()
    }
    document
      .querySelector(`.msg.user[data-idx="${i}"]`)
      ?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }
  // Messages view on mobile: pick a message, switch to the chat pane,
  // then scroll to it (the chat must be visible before scrolling).
  async function mobileJumpToMessage(i) {
    setMobileView('chat')
    await tick()
    requestAnimationFrame(() => jumpToMessage(i))
  }
  function truncate(text, n = 80) {
    const t = (text ?? '').replace(/\s+/g, ' ').trim()
    return t.length > n ? t.slice(0, n) + '…' : t
  }

  // Copy text to the clipboard with a brief ✓ feedback on the button.
  // navigator.clipboard needs a secure context (localhost qualifies);
  // fall back to the legacy execCommand path elsewhere.
  async function copyText(text, btn) {
    try {
      if (navigator.clipboard) {
        await navigator.clipboard.writeText(text)
      } else {
        const ta = document.createElement('textarea')
        ta.value = text
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        ta.remove()
      }
    } catch (err) {
      console.error('copy failed', err)
      return
    }
    if (btn) {
      btn.classList.add('copied')
      setTimeout(() => btn.classList.remove('copied'), 1200)
    }
  }

  // ----- prompt history recall (shell-style) -----
  // ArrowUp with the caret on the first input line recalls previously
  // sent prompts (newest first); ArrowDown moves forward and finally
  // restores the unsent draft. Persisted per session in localStorage
  // (keyed by session path) so prompts from other sessions of the same
  // project never leak into the recall list.
  const HISTORY_MAX = 100
  let promptHistory = []
  let sessionPrompts = [] // user prompts from the loaded session (loadHistory)
  let histIndex = -1 // -1 = not browsing
  let histDraft = ''

  const historyKey = () => `promptHistory:${currentSessionPath || 'no-session'}`

  function loadPromptHistory() {
    try {
      const parsed = JSON.parse(localStorage.getItem(historyKey()) ?? '[]')
      promptHistory = Array.isArray(parsed) ? parsed.filter((t) => typeof t === 'string') : []
    } catch {
      promptHistory = []
    }
    histIndex = -1
  }

  // Seed the recall history with the prompts of the loaded session, so
  // ArrowUp works right after a project/session switch even when this
  // browser has no localStorage history for the session yet (prompts
  // sent from another browser, or before history persistence existed).
  // Session prompts are older, localStorage ones newer; not persisted —
  // each load re-merges from the session.
  function mergeSessionPrompts() {
    const merged = []
    for (const t of sessionPrompts) if (t && !merged.includes(t)) merged.push(t)
    for (const t of promptHistory) if (t && !merged.includes(t)) merged.push(t)
    promptHistory = merged.slice(-HISTORY_MAX)
    histIndex = -1
  }

  function pushPromptHistory(text) {
    if (!text) return
    if (promptHistory[promptHistory.length - 1] !== text) {
      promptHistory.push(text)
      if (promptHistory.length > HISTORY_MAX) promptHistory = promptHistory.slice(-HISTORY_MAX)
      try {
        localStorage.setItem(historyKey(), JSON.stringify(promptHistory))
      } catch (err) {
        console.error('failed to persist prompt history', err)
      }
    }
    histIndex = -1
  }

  // dir: -1 older, +1 newer. Returns true when the key was consumed.
  function recallHistory(dir) {
    if (!promptHistory.length) return false
    if (histIndex === -1) {
      if (dir > 0) return false
      histDraft = input
      histIndex = promptHistory.length
    }
    const next = histIndex + dir
    if (next < 0) return true // already at the oldest entry
    if (next >= promptHistory.length) {
      histIndex = -1
      input = histDraft
    } else {
      histIndex = next
      input = promptHistory[histIndex]
    }
    // Caret to the end once the textarea has re-rendered.
    tick().then(() => inputEl?.setSelectionRange(input.length, input.length))
    return true
  }

  async function apiGet(url) {
    let status = 0
    try {
      const r = await fetch(url)
      status = r.status
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return await r.json()
    } catch (err) {
      console.error('GET', url, err)
      // status lets callers distinguish "gone" (404) from transient
      // failures (0 = network error) instead of resetting state blindly
      return { success: false, status, error: err.message }
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
      // Parse the body even on error status: endpoints like
      // /api/file/save signal specific failures (e.g. save conflicts)
      // via their JSON payload, not just the status code.
      const data = await r.json().catch(() => null)
      if (!r.ok) return { success: false, status: r.status, ...(data ?? { error: `HTTP ${r.status}` }) }
      return data
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
    // The active session path comes cheaply with /state; the sessions
    // list itself is loaded lazily (see ensureSessionsLoaded).
    if (resp.data.sessionFile) currentSessionPath = resp.data.sessionFile
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
  // Lazy sessions pane: the list is sidebar-only and /sessions scans
  // session files (name resolution) — don't load it on the critical
  // path of a project switch. Loaded when the pane becomes visible
  // (sidebar expand / mobile sessions view) or, if it already is
  // visible, when the browser goes idle after the switch.
  let sessionsLoaded = $state(false)
  function ensureSessionsLoaded() {
    if (sessionsLoaded) return
    return refreshSessions()
  }
  async function refreshSessions() {
    const id = ++sessionsReq
    const resp = await apiGet('/sessions')
    if (id !== sessionsReq) return
    sessions = resp.sessions ?? []
    sessionsLoaded = true
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
    // A project click while the pane was hovered collapses the pane on
    // mouse-leave; the .current class lands on the newly selected item
    // only here, when the reload returns — potentially seconds later,
    // after the collapse tracker already gave up. If a collapse
    // happened recently, make the (new) selection visible now.
    if (keepVisibleSel) keepSelectedVisible(keepVisibleSel[0], keepVisibleSel[1])
  }

  // Full (re)initialization: after mount, a project switch, or a
  // project_switched event from another tab.
  async function reinit() {
    entries = []
    hiddenCount = 0
    attachments = []
    sessions = []
    sessionsLoaded = false
    // The stream this UI tracked is gone (project switch, pi exit,
    // overflow reconnect); a fresh agent_start re-enables it when a run
    // is actually active on the new stream.
    streaming = false
    if (editMode) {
      // The project's pi process is already gone; saving the draft is
      // impossible (paths are confined to the new project root).
      if (editDirty) {
        entries.push({ role: 'system', text: `⚠️ unsaved changes to ${editPath} discarded: project switched` })
      }
      quitEdit({ force: true })
    }
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
      loadCommands(),
      loadProjects(),
    ])
    loadPromptHistory() // per session; needs currentSessionPath (set by refreshState)
    mergeSessionPrompts() // seed recall from the resumed session's prompts
    loadSidebarState() // sidebar collapse state is per project too
    // The current project can sit below the fold of the (collapsed)
    // projects pane — scroll it into view, on app start and on every
    // project switch alike. Wait a tick for the list to render.
    await tick()
    keepSelectedVisible('.sb-projects .sb-list', '.sb-item.current')
    // Restore the project's remembered viewer file, README as fallback.
    const remembered = projects.find((p) => p.current)?.lastFile ?? 'README.md'
    viewerLoading = true
    let f
    try {
      f = await loadViewerFile(remembered)
      if ((!f.path || f.error) && remembered !== 'README.md') {
        f = await loadViewerFile('README.md')
      }
    } finally {
      viewerLoading = false
    }
    if (f.path && !f.error) {
      selectedFile = f
      // Show the file's directory in the browser (also highlights it)
      // and scroll the entry into view, so a selected file deep in a
      // long listing is actually visible after the project switch.
      // '' is the project root — browse('') for root files too, so the
      // root listing scrolls to them as well.
      const dir = f.path.split('/').slice(0, -1).join('/')
      await browse(dir)
      await tick()
      document
        .querySelector('.browser-body .dirlist .direntry.selected')
        ?.scrollIntoView({ block: 'nearest' })
    }
    // Sessions list is sidebar-only: if the pane is visible, load it
    // once the browser is idle (off the switch's critical path); if
    // not, the sidebar-expand / mobile-view triggers load it on demand.
    if (!isMobile && !sidebarCollapsed) {
      const ric = window.requestIdleCallback ?? ((f) => setTimeout(f, 200))
      ric(() => ensureSessionsLoaded())
    }
  }

  async function switchProject(id) {
    if (!quitEdit()) return
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

  // Project click in the sidebar: confirm while a run is active, then
  // switch (which respawns the pi subprocess with the new cwd).
  async function selectProject(p) {
    if (p.outsideWorkspace) return
    // Mobile: tapping the already-current project navigates to the
    // chat, like any other project (no switch happens).
    if (p.current) {
      if (isMobile) mobileView = 'chat'
      return
    }
    if (streaming && !window.confirm('Switching projects restarts the agent and kills the running session. Continue?')) return
    await switchProject(p.id)
    // Mobile: jump straight to the chat, which shows the session that
    // was last open for this project (restored by the project switch).
    if (isMobile) mobileView = 'chat'
  }

  // No project selected (e.g. the current one was detached): chat and
  // sessions are blocked until the user actively opens or creates one.
  const noProject = $derived(projects.length > 0 && !currentProjectId)

  // Local UI reset after the current project was detached (by this tab
  // or another one): clear the chat and wait for an explicit choice.
  function enterDetachedMode(name) {
    entries = [
      { role: 'system', text: `✖ project “${name}” was detached — select a project on the left or create a new one` },
    ]
    hiddenCount = 0
    sessions = []
    sessionsLoaded = true // nothing to load while detached: show the empty state
    currentSessionPath = ''
    quitEdit({ force: true }) // no active project, nowhere to save to
    selectedFile = null
    diffView = null
    dirEntries = []
    browserPath = ''
    browserParent = null
    stats = null
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

  // Detach = remove from the registry only; the directory stays on disk.
  // Detaching the current project clears the chat; the user must then
  // actively select another project (or create one) to continue. Not
  // allowed while the agent is working — finish or abort the run first.
  async function detachProject(p) {
    projectMenuFor = null
    if (p.current && streaming) {
      entries.push({ role: 'system', text: '⚠️ cannot detach the current project while the agent is working — wait or abort first' })
      return
    }
    const extra = p.current
      ? '\n\nThis is the current project: the chat will be cleared and you must select another project to continue.'
      : ''
    if (!window.confirm(`Detach project “${p.name}” from the list?\n${p.path}\n\nThe directory itself is not deleted.${extra}`)) return
    const resp = await apiPost(`/api/projects/${p.id}/detach`)
    if (!resp.success) {
      entries.push({ role: 'system', text: `⚠️ ${resp.error ?? 'failed to detach project'}` })
      return
    }
    if (resp.wasCurrent) enterDetachedMode(p.name)
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

  // Clone a git repo into the workspace and switch to it. The backend
  // derives the folder name from the URL; cloning can take a while, so
  // the dialog buttons are disabled via npCloning meanwhile.
  async function cloneProject() {
    const gitUrl = npGitUrl.trim()
    if (!gitUrl || npCloning) return
    npCloning = true
    try {
      const resp = await apiPost('/api/projects', { gitUrl })
      if (!resp.success) {
        entries.push({ role: 'system', text: `⚠️ ${resp.error ?? 'failed to clone repository'}` })
        return
      }
      showNewProject = false
      npGitUrl = ''
      await loadProjects()
      if (streaming && !window.confirm('Switch to the new project now? This restarts the agent and kills the running session.')) return
      await switchProject(resp.project.id)
    } finally {
      npCloning = false
    }
  }

  async function switchToSession(path) {
    const resp = await apiPost('/switch_session', { path })
    if (resp.success && !resp.data?.cancelled) {
      currentSessionPath = path
      entries = []
      hiddenCount = 0
      await loadHistory()
      loadPromptHistory()
      mergeSessionPrompts()
      await refreshState()
      await refreshStats()
      inputEl?.focus()
      if (isMobile) mobileView = 'chat'
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
            if (text || thinking) {
              a.text = text
              if (thinking) a.thinking = thinking
            } else {
              // Tool-call-only turn: drop the blank placeholder pushed at
              // message_start, otherwise it renders as an empty bubble
              // (the large vertical gap; reload skips these in loadHistory).
              entries.pop()
            }
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

      // Direct RPC bash commands (the `!`/`!!` inputs). Chunk events
      // carry the command id; unknown ids mean the command was started
      // in another tab — the authoritative full result arrives with
      // bash_execution_end, so unknown chunks are simply skipped.
      case 'bash_execution_update': {
        const b = entries.findLast((e) => e.role === 'bash' && !e.done && e.id === ev.id)
        if (b) {
          b.output += ev.delta ?? ''
          scrollToBottom()
        }
        break
      }

      case 'bash_execution_end': {
        let b = entries.findLast((e) => e.role === 'bash' && e.id === ev.id)
        if (!b) {
          // Started in another tab: render the finished result here.
          b = {
            role: 'bash',
            id: ev.id,
            command: ev.command ?? '',
            output: '',
            done: false,
            exclude: !!ev.excludeFromContext,
            exitCode: null,
            truncated: false,
            cancelled: false,
            error: null,
            fullOutputPath: null,
          }
          entries.push(b)
        }
        const resp = ev.response ?? {}
        if (resp.success) {
          const d = resp.data ?? {}
          b.output = d.output ?? ''
          b.exitCode = d.exitCode ?? null
          b.cancelled = !!d.cancelled
          b.truncated = !!d.truncated
          b.fullOutputPath = d.fullOutputPath ?? null
        } else {
          b.error = resp.error ?? 'command failed'
        }
        b.done = true
        scrollToBottom()
        break
      }

      case 'extension_ui_request':
        // On any handler error still answer pi (cancelled): it waits
        // for the correlated response and would hang otherwise.
        handleUiRequest(ev).catch((err) => {
          console.error('UI request error', err)
          if (ev.id) apiPost('/ui-response', { type: 'extension_ui_response', id: ev.id, cancelled: true })
        })
        break

      case 'stream_overflow':
        // The server kicked this subscriber because it stopped keeping
        // up — events were about to be lost. Reconnect and reload all
        // state instead of continuing from a silently gappy stream.
        console.warn('SSE stream overflowed; reconnecting')
        reconnectEvents()
        reinit()
        break

      case 'project_detached':
        // The current project was detached (possibly by another tab):
        // clear the chat and wait for an explicit project choice.
        enterDetachedMode(ev.project?.name ?? '?')
        loadProjects()
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

      case 'pi_exited':
        // The pi subprocess died unexpectedly (crash, kill): the server
        // respawns it on the next request, but our SSE stream is still
        // attached to the dead process — reconnect (the new /events
        // subscription triggers the respawn and session resume) and
        // reload all state, or the UI would freeze silently.
        console.warn('pi process exited; reconnecting')
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
      // Malformed request (options not an array) must not throw: pi
      // waits for the correlated response and would hang without one.
      const options = Array.isArray(req.options) ? req.options : []
      const choice = options.length
        ? window.prompt(`${req.title ?? 'Choose:'}\n` + options.map((o, i) => `${i + 1}. ${o}`).join('\n'))
        : null
      const idx = parseInt(choice, 10) - 1
      if (idx >= 0 && idx < options.length) resp.value = options[idx]
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

  // crypto.randomUUID needs a secure context; the app is also served
  // over plain HTTP on LAN IPs, so fall back to a Math.random id.
  // NOT cryptographically secure — only for correlating bash command
  // events; never use for tokens, secrets, or anything security-relevant.
  function uuidish() {
    const u = crypto.randomUUID?.() ?? 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0
      return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16)
    })
    return u.replaceAll('-', '')
  }

  // Shell commands: `!cmd` runs via pi's RPC bash with the output added
  // to the model context on the next prompt; `!!cmd` sets
  // excludeFromContext (output stays local). The entry is created here
  // and streams/finalizes via the bash_execution_update / synthetic
  // bash_execution_end SSE events, correlated by the client-chosen id
  // (update chunks can arrive before the POST response returns the id).
  async function runShellCommand(raw) {
    const exclude = raw.startsWith('!!')
    const command = raw.slice(exclude ? 2 : 1).trim()
    if (!command) {
      entries.push({ role: 'system', text: '⚠️ usage: !<command> (result goes to the model) or !!<command> (local only)' })
      return
    }
    const entry = {
      role: 'bash',
      id: uuidish(),
      command,
      output: '',
      done: false,
      exclude,
      exitCode: null,
      truncated: false,
      cancelled: false,
      error: null,
      fullOutputPath: null,
    }
    entries.push(entry)
    const r = await apiPost('/bash', { id: entry.id, command, excludeFromContext: exclude })
    if (!r.success) {
      entry.done = true
      entry.error = r.error ?? 'failed to run command'
      scrollToBottom()
    } else if (r.id && r.id !== entry.id) {
      entry.id = r.id
    }
  }

  // pi cancels all running RPC bash commands at once.
  async function abortBash() {
    await apiPost('/abort_bash')
  }

  async function sendPrompt() {
    if (noProject) {
      entries.push({ role: 'system', text: '⚠️ no project selected — pick one on the left or create a new one first' })
      return
    }
    const trimmed = input.trim()
    if (trimmed) pushPromptHistory(trimmed)
    if (trimmed.startsWith('!')) {
      input = ''
      await runShellCommand(trimmed)
      scrollToBottom(true) // explicit command: jump to the end
      return
    }
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
    if (noProject) return
    await apiPost('/new_session')
    entries = []
    hiddenCount = 0
    sessionPrompts = [] // no loadHistory here; drop the old session's prompts
    await refreshSessions()
    loadPromptHistory() // re-key to the fresh session
    await refreshStats()
    if (isMobile) mobileView = 'chat' // jump to the fresh empty chat
    inputEl?.focus()
  }

  async function loadHistory() {
    sessionPrompts = []
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
        if (text) sessionPrompts.push(text)
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
      } else if (m.role === 'bashExecution') {
        entries.push({
          role: 'bash',
          id: null,
          command: m.command ?? '',
          output: m.output ?? '',
          done: true,
          exclude: !!m.excludeFromContext,
          exitCode: m.exitCode ?? null,
          truncated: !!m.truncated,
          cancelled: !!m.cancelled,
          error: null,
          fullOutputPath: m.fullOutputPath ?? null,
        })
      }
    }
    hiddenCount = Math.max(0, entries.length - CHAT_PAGE)
    scrollToBottom(true) // history (re)load: start at the end
  }

  // Reveal one more page of history, keeping the viewport anchored on
  // the message it currently shows at the top. We pin the first
  // rendered message element (not a height delta): older entries are
  // inserted above it and the button itself may disappear when the
  // history is fully revealed — and heights keep shifting after the
  // DOM update while images decode, so re-pin on the next frames too
  // (same late-pin scheme as scrollToBottom).
  async function showOlder() {
    if (!hiddenCount) return
    const el = chatEl
    const anchor = el?.querySelector('.msg:not(.older), .msgrow, details') ?? null
    const prevTop = anchor?.getBoundingClientRect().top ?? null
    const prevHeight = el?.scrollHeight ?? 0
    hiddenCount = Math.max(0, hiddenCount - CHAT_PAGE)
    await tick()
    if (!el) return
    if (anchor?.isConnected && prevTop != null) {
      const pin = () => {
        if (anchor.isConnected) el.scrollTop += anchor.getBoundingClientRect().top - prevTop
      }
      pin()
      requestAnimationFrame(pin)
      setTimeout(pin, 80) // late async growth (image decode etc.)
    } else {
      el.scrollTop += el.scrollHeight - prevHeight
    }
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
    const mq = window.matchMedia('(max-width: 1024px)')
    isMobile = mq.matches
    const onMq = (e) => { isMobile = e.matches }
    mq.addEventListener('change', onMq)
    return () => {
      eventSource?.close()
      mq.removeEventListener('change', onMq)
    }
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
    input, isMobile // track: re-run on every keystroke, breakpoint change, reset after send
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
      if (e.key === 'Escape') {
        e.preventDefault()
        slashDismissed = true
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
      return
    }
    // Prompt history: only when the caret can't move within the text
    // in that direction (first line for up, last line for down), so
    // multi-line editing keeps its native arrow-key behavior.
    if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      const onFirstLine = !input.slice(0, inputEl.selectionStart).includes('\n')
      const onLastLine = !input.slice(inputEl.selectionEnd).includes('\n')
      if (e.key === 'ArrowUp' && onFirstLine && recallHistory(-1)) e.preventDefault()
      else if (e.key === 'ArrowDown' && histIndex !== -1 && onLastLine && recallHistory(1)) e.preventDefault()
    }
  }

  function fmtCost(c) {
    return c == null ? '—' : `$${c.toFixed(2)}`
  }
</script>

<svelte:window onclick={() => { sessionMenuFor = null; projectMenuFor = null }} />

<main>
  <div class="body mv-{mobileView}">
   {#if sidebarCollapsed && !isMobile}
    <!-- Collapsed rail: expand toggle at the top, settings right below it -->
    <div class="sidebar-rail" transition:slide={{ duration: 200, axis: 'x' }}>
      <button class="sb-toggle" onclick={toggleSidebar} title="Show projects and sessions" aria-label="Show projects and sessions" aria-expanded="false">
        <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M0 2.75C0 1.784.784 1 1.75 1h12.5c.966 0 1.75.784 1.75 1.75v10.5A1.75 1.75 0 0 1 14.25 15H1.75A1.75 1.75 0 0 1 0 13.25Zm1.75-.25a.25.25 0 0 0-.25.25v10.5c0 .138.112.25.25.25H5.5v-11Zm5.25 0v11h7.25a.25.25 0 0 0 .25-.25V2.75a.25.25 0 0 0-.25-.25Z"/></svg>
      </button>
      <button class="sb-toggle" onclick={() => { toggleSidebar(); showSettings = true }} title="Settings" aria-label="Settings">
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M8 0a8.2 8.2 0 0 1 .701.031C9.444.095 9.99.645 10.16 1.29l.288 1.107c.018.066.079.158.212.224.231.114.454.243.668.386.123.082.233.09.299.071l1.103-.303c.644-.176 1.392.021 1.82.63.27.385.506.792.704 1.218.315.675.111 1.422-.364 1.891l-.814.806c-.049.048-.098.147-.088.294.016.257.016.515 0 .772-.01.147.038.246.088.294l.814.806c.475.469.679 1.216.364 1.891a7.977 7.977 0 0 1-.704 1.217c-.428.61-1.176.807-1.82.63l-1.102-.302c-.067-.019-.177-.011-.3.071a5.909 5.909 0 0 1-.668.386c-.133.066-.194.158-.211.224l-.29 1.106c-.168.646-.715 1.196-1.458 1.26a8.006 8.006 0 0 1-1.402 0c-.743-.064-1.289-.614-1.458-1.26l-.289-1.106c-.018-.066-.079-.158-.212-.224a5.738 5.738 0 0 1-.668-.386c-.123-.082-.233-.09-.299-.071l-1.103.303c-.644.176-1.392-.021-1.82-.63a8.12 8.12 0 0 1-.704-1.218c-.315-.675-.111-1.422.363-1.891l.815-.806c.05-.048.098-.147.088-.294a6.214 6.214 0 0 1 0-.772c.01-.147-.038-.246-.088-.294l-.815-.806C.635 6.045.431 5.298.746 4.623a7.92 7.92 0 0 1 .704-1.217c.428-.61 1.176-.807 1.82-.63l1.102.302c.067.019.177.011.3-.071.214-.143.437-.272.668-.386.133-.066.194-.158.212-.224l.289-1.106C6.009.645 6.556.095 7.299.03 7.53.01 7.764 0 8 0Zm-.571 1.525c-.036.003-.108.036-.137.146l-.289 1.105c-.147.561-.549.967-.998 1.189-.173.086-.34.183-.5.29-.417.278-.97.423-1.529.27l-1.103-.303c-.109-.03-.175.016-.195.045-.22.312-.412.644-.573.99-.014.031-.021.11.059.19l.815.806c.411.406.562.957.53 1.456a4.709 4.709 0 0 0 0 .582c.032.499-.119 1.05-.53 1.456l-.815.806c-.081.08-.073.159-.059.19.162.346.353.677.573.989.02.03.085.076.195.046l1.102-.303c.56-.153 1.113-.008 1.53.27.161.107.328.204.501.29.447.222.85.629.997 1.189l.289 1.105c.029.109.101.143.137.146a6.6 6.6 0 0 0 1.142 0c.036-.003.108-.036.137-.146l.289-1.105c.147-.561.549-.967.998-1.189.173-.086.34-.183.5-.29.417-.278.97-.423 1.529-.27l1.103.303c.109.029.175-.016.195-.045.22-.313.411-.644.573-.99.014-.031.021-.11-.059-.19l-.815-.806c-.411-.406-.562-.957-.53-1.456a4.709 4.709 0 0 0 0-.582c-.032-.499.119-1.05.53-1.456l.815-.806c.081-.08.073-.159.059-.19a6.464 6.464 0 0 0-.573-.989c-.02-.03-.085-.076-.195-.046l-1.102.303c-.56.153-1.113.008-1.53-.27a4.44 4.44 0 0 0-.501-.29c-.447-.222-.85-.629-.997-1.189l-.289-1.105c-.029-.11-.101-.143-.137-.146a6.6 6.6 0 0 0-1.142 0ZM11 8a3 3 0 1 1-6 0 3 3 0 0 1 6 0ZM9.5 8a1.5 1.5 0 1 0-3.001.001A1.5 1.5 0 0 0 9.5 8Z"/></svg>
      </button>
    </div>
   {:else}
    <aside class="sidebar" style="width: {sidebarWidth}px" transition:slide={{ duration: 200, axis: 'x' }}>
      <!-- Top row above the Projects header: settings button left of the
           collapse toggle, both right-aligned -->
      <div class="sidebar-top">
        <button class="sb-toggle" onclick={() => (showSettings = !showSettings)} title="Settings" aria-label="Settings" aria-expanded={showSettings}>
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M8 0a8.2 8.2 0 0 1 .701.031C9.444.095 9.99.645 10.16 1.29l.288 1.107c.018.066.079.158.212.224.231.114.454.243.668.386.123.082.233.09.299.071l1.103-.303c.644-.176 1.392.021 1.82.63.27.385.506.792.704 1.218.315.675.111 1.422-.364 1.891l-.814.806c-.049.048-.098.147-.088.294.016.257.016.515 0 .772-.01.147.038.246.088.294l.814.806c.475.469.679 1.216.364 1.891a7.977 7.977 0 0 1-.704 1.217c-.428.61-1.176.807-1.82.63l-1.102-.302c-.067-.019-.177-.011-.3.071a5.909 5.909 0 0 1-.668.386c-.133.066-.194.158-.211.224l-.29 1.106c-.168.646-.715 1.196-1.458 1.26a8.006 8.006 0 0 1-1.402 0c-.743-.064-1.289-.614-1.458-1.26l-.289-1.106c-.018-.066-.079-.158-.212-.224a5.738 5.738 0 0 1-.668-.386c-.123-.082-.233-.09-.299-.071l-1.103.303c-.644.176-1.392-.021-1.82-.63a8.12 8.12 0 0 1-.704-1.218c-.315-.675-.111-1.422.363-1.891l.815-.806c.05-.048.098-.147.088-.294a6.214 6.214 0 0 1 0-.772c.01-.147-.038-.246-.088-.294l-.815-.806C.635 6.045.431 5.298.746 4.623a7.92 7.92 0 0 1 .704-1.217c.428-.61 1.176-.807 1.82-.63l1.102.302c.067.019.177.011.3-.071.214-.143.437-.272.668-.386.133-.066.194-.158.212-.224l.289-1.106C6.009.645 6.556.095 7.299.03 7.53.01 7.764 0 8 0Zm-.571 1.525c-.036.003-.108.036-.137.146l-.289 1.105c-.147.561-.549.967-.998 1.189-.173.086-.34.183-.5.29-.417.278-.97.423-1.529.27l-1.103-.303c-.109-.03-.175.016-.195.045-.22.312-.412.644-.573.99-.014.031-.021.11.059.19l.815.806c.411.406.562.957.53 1.456a4.709 4.709 0 0 0 0 .582c.032.499-.119 1.05-.53 1.456l-.815.806c-.081.08-.073.159-.059.19.162.346.353.677.573.989.02.03.085.076.195.046l1.102-.303c.56-.153 1.113-.008 1.53.27.161.107.328.204.501.29.447.222.85.629.997 1.189l.289 1.105c.029.109.101.143.137.146a6.6 6.6 0 0 0 1.142 0c.036-.003.108-.036.137-.146l.289-1.105c.147-.561.549-.967.998-1.189.173-.086.34-.183.5-.29.417-.278.97-.423 1.529-.27l1.103.303c.109.029.175-.016.195-.045.22-.313.411-.644.573-.99.014-.031.021-.11-.059-.19l-.815-.806c-.411-.406-.562-.957-.53-1.456a4.709 4.709 0 0 0 0-.582c-.032-.499.119-1.05.53-1.456l.815-.806c.081-.08.073-.159.059-.19a6.464 6.464 0 0 0-.573-.989c-.02-.03-.085-.076-.195-.046l-1.102.303c-.56.153-1.113.008-1.53-.27a4.44 4.44 0 0 0-.501-.29c-.447-.222-.85-.629-.997-1.189l-.289-1.105c-.029-.11-.101-.143-.137-.146a6.6 6.6 0 0 0-1.142 0ZM11 8a3 3 0 1 1-6 0 3 3 0 0 1 6 0ZM9.5 8a1.5 1.5 0 1 0-3.001.001A1.5 1.5 0 0 0 9.5 8Z"/></svg>
        </button>
        <button class="sb-toggle" onclick={toggleSidebar} title="Hide projects and sessions" aria-label="Hide projects and sessions" aria-expanded="true">
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M0 2.75C0 1.784.784 1 1.75 1h12.5c.966 0 1.75.784 1.75 1.75v10.5A1.75 1.75 0 0 1 14.25 15H1.75A1.75 1.75 0 0 1 0 13.25Zm1.75-.25a.25.25 0 0 0-.25.25v10.5c0 .138.112.25.25.25H5.5v-11Zm5.25 0v11h7.25a.25.25 0 0 0 .25-.25V2.75a.25.25 0 0 0-.25-.25Z"/></svg>
        </button>
      </div>
      {#if showSettings}
        <!-- Settings: expands below the top row, styled like the
             projects/sessions lists; picking a theme closes it -->
        <div class="sb-section sb-settings" in:slide={{ duration: 200 }} out:slide={{ duration: 350 }}>
          <div class="sidebar-head">
            <span class="sb-title">UI theme</span>
          </div>
          <div class="sb-list" role="radiogroup" aria-label="UI theme">
            {#each ['light', 'cream', 'dark'] as t}
              <div class="sb-item" class:current={theme === t}>
                <button
                  class="sb-session"
                  role="radio"
                  aria-checked={theme === t}
                  onclick={() => { theme = t; showSettings = false }}
                >
                  <span class="sb-name">{t}</span>
                </button>
              </div>
            {/each}
          </div>
        </div>
      {/if}
      <!-- Projects section: capped at 1/3 of the window height; grows
           on hover when projects are cut off (max 80% window height).
           Hover is decorative (visual expand only), no ARIA role needed. -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div class="sb-section sb-projects" style="max-height: {projExpandedMax}" onmouseenter={scheduleExpandProjects} onmouseleave={collapseProjects}>
      <div class="sidebar-head">
        <span class="sb-title">Projects</span>
        <button class="sb-new" onclick={toggleNewProject} title="New project">add</button>
      </div>
      <div class="sb-scroll" onscrollcapture={updateFades}>
      <div class="fade fade-top" class:visible={projFadeTop}></div>
      <div class="fade fade-bottom" class:visible={projFadeBottom && projExpandedMax === ''}></div>
      <div class="sb-list">
        {#each projects as p (p.id)}
          <div class="sb-item" class:current={p.current} class:outside={p.outsideWorkspace}>
            <button
              class="sb-session"
              onclick={() => selectProject(p)}
              title={p.path}
            >
              <span class="sb-name">{p.name}{p.outsideWorkspace ? ' (outside workspace)' : ''}</span>
            </button>
            <button class="sb-menu-btn" title="Project actions" aria-label="Project actions" onclick={(e) => toggleProjectMenu(e, p.id)}>⋯</button>
            {#if projectMenuFor === p.id}
              <div class="sb-menu" class:flip={menuFlip} role="menu">
                <button
                  class="sb-menu-item danger"
                  role="menuitem"
                  disabled={p.current && streaming}
                  title={p.current && streaming ? 'Wait for the agent to finish (or abort) first' : 'Detach (the directory stays on disk)'}
                  onclick={() => detachProject(p)}
                >detach</button>
              </div>
            {/if}
          </div>
        {:else}
          <div class="sb-empty">No projects registered.</div>
        {/each}
      </div>
      </div>
      </div>
      <!-- Sessions section: takes the remaining height -->
      <div class="sb-section sb-sessions">
      <div class="sidebar-head">
        <span class="sb-title">Sessions</span>
        <button class="sb-new" onclick={newSession} title="New session">new</button>
      </div>
      <div class="sb-scroll" onscrollcapture={updateFades}>
      <div class="fade fade-top" class:visible={sessFadeTop}></div>
      <div class="fade fade-bottom" class:visible={sessFadeBottom}></div>
      <div class="sb-list">
        {#each sessions as s (s.path)}
          <div class="sb-item" class:current={s.current}>
            <button
              class="sb-session"
              onclick={() => (s.current ? (isMobile && (mobileView = 'chat')) : switchToSession(s.path))}
              title={s.name}
            >
              <span class="sb-name">{s.name}</span>
              <span class="sb-date">{new Date(s.mtime * 1000).toLocaleString()}</span>
            </button>
            <button class="sb-menu-btn" title="Session actions" aria-label="Session actions" onclick={(e) => toggleSessionMenu(e, s.path)}>⋯</button>
            {#if sessionMenuFor === s.path}
              <div class="sb-menu" class:flip={menuFlip} role="menu">
                <button class="sb-menu-item danger" role="menuitem" onclick={() => deleteSession(s)}>delete</button>
              </div>
            {/if}
          </div>
        {:else}
          <div class="sb-empty">{sessionsLoaded ? 'No sessions yet.' : 'Loading sessions…'}</div>
        {/each}
      </div>
      </div>
      </div>
    </aside>
    <div class="vsplitter sb-splitter" role="separator" aria-orientation="vertical" aria-label="Resize sidebar" onpointerdown={startSidebarDrag} transition:slide={{ duration: 200, axis: 'x' }}></div>
   {/if}
   <div class="workspace" bind:this={bodyEl}>
   <!-- chatRatio marks the splitter position; the chat column ends
        2vw earlier, preserving the gap that hosts the dot menu -->
   <div class="chatcol" style="flex: 0 0 calc({(chatRatio * 100).toFixed(2)}% - 2vw - 1px)">
    <div class="toolbar">
      <div class="tgroup">
      <select class="model-select" value={currentModel ? `${currentModel.provider}::${currentModel.id}` : ''} onchange={onModelChange}>
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
    </div>

  <div class="chat-body" onscrollcapture={updateFades}>
    <div class="fade fade-top" class:visible={chatFadeTop}></div>
    <div class="fade fade-bottom" class:visible={chatFadeBottom}></div>
  <!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
  <div class="chat" bind:this={chatEl} onclick={onMdClick}>
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
    {#if hiddenCount > 0}
      <button class="msg older" onclick={showOlder}>
        load {Math.min(CHAT_PAGE, hiddenCount)} earlier messages ({hiddenCount} hidden)
      </button>
    {/if}
    {#each entries.slice(hiddenCount) as entry, k (hiddenCount + k)}
      {#if entry.role === 'system'}
        <div class="msg system">{entry.text}</div>
      {:else if entry.role === 'user'}
        <div class="msgrow user">
          <div class="msg user" data-idx={hiddenCount + k}>{entry.text}
            {#if entry.images?.length}
              <div class="imgs">
                {#each entry.images as src}
                  <img {src} alt="attachment" />
                {/each}
              </div>
            {/if}
          </div>
          <div class="copycol">
            <button class="msg-copy" title="Copy message" aria-label="Copy message" onclick={(e) => copyText(entry.text, e.currentTarget)}></button>
          </div>
        </div>
      {:else if entry.role === 'assistant'}
        {#if entry.thinking}
          <div class="msgrow assistant thinkrow">
            <details class="thinking-block" open>
              <summary>thinking</summary>
              <pre>{entry.thinking}</pre>
            </details>
            <div class="copycol">
              {#if entry.thinking?.trim()}
                <button class="msg-copy think" title="Copy thinking" aria-label="Copy thinking" onclick={(e) => copyText(entry.thinking, e.currentTarget)}></button>
              {/if}
            </div>
          </div>
        {/if}
        {#if entry.text || !entry.thinking}
          <div class="msgrow assistant">
            <div class="msg assistant md">
              {@html renderMarkdown(entry.text)}
            </div>
            <div class="copycol">
              {#if entry.text?.trim()}
                <button class="msg-copy" title="Copy message as markdown" aria-label="Copy message as markdown" onclick={(e) => copyText(entry.text, e.currentTarget)}></button>
              {/if}
            </div>
          </div>
        {/if}
      {:else if entry.role === 'bash'}
        <details class="msg tool bash" class:error={entry.done && !entry.cancelled && (entry.error || entry.exitCode !== 0)} class:ok={entry.done && !entry.error && entry.exitCode === 0} open={!isMobile}>
          <summary>
            <code class="bash-cmd">{entry.exclude ? '!!' : '!'}{entry.command}</code>
            <span class="bash-badge" title={entry.exclude ? 'output stays local; the model never sees it' : 'output is added to the model context with the next prompt'}>{entry.exclude ? 'local' : 'in context'}</span>
            {#if !entry.done}
              <span class="spinner">…</span>
              <button class="bash-abort" onclick={(e) => { e.preventDefault(); e.stopPropagation(); abortBash() }}>abort</button>
            {:else if entry.error}
              <span class="bash-badge err">⚠ {entry.error}</span>
            {:else}
              <span class="bash-badge">{entry.cancelled ? 'aborted' : `exit ${entry.exitCode}`}</span>
              {#if entry.truncated}
                <span class="bash-badge" title={entry.fullOutputPath ? `full output: ${entry.fullOutputPath}` : ''}>truncated</span>
              {/if}
            {/if}
          </summary>
          {#if entry.output}<pre>{entry.output}</pre>{/if}
        </details>
      {:else}
        <details class="msg tool" class:error={entry.isError} class:ok={entry.done && !entry.isError} open={!isMobile}>
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
      onfocus={() => (showNewProject = false)}
      onkeydown={onKeydown}
      placeholder={streaming
        ? isMobile
          ? 'Agent is working — send to steer…'
          : 'Agent is working — type to steer, Enter to send…'
        : isMobile
          ? 'Prompt pi…'
          : 'Prompt pi… (Enter to send, Shift+Enter for newline)'}
      rows={isMobile ? 1 : 2}
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

   <button class="gap-dots" style="left: calc({(chatRatio * 100).toFixed(2)}% - 1vw - 0.5px)" aria-label="Jump to one of your messages" onmouseenter={openMsgNav} onmouseleave={scheduleCloseMsgNav} onfocus={openMsgNav} onblur={scheduleCloseMsgNav} onclick={openMsgNav}>
    <span></span><span></span><span></span>
   </button>

   {#if showMsgNav}
    <div class="msgnav-popup" role="dialog" aria-label="Messages" tabindex="-1" onmouseenter={openMsgNav} onmouseleave={closeMsgNav}>
      {#each userMessages as { e, i } (i)}
        <button class="msgnav-item" onclick={() => jumpToMessage(i)}>{truncate(e.text)}</button>
      {:else}
        <div class="msgnav-empty">No messages yet.</div>
      {/each}
    </div>
   {/if}

   <div class="vsplitter" role="separator" aria-orientation="vertical" aria-label="Resize chat/files split" onpointerdown={startChatSplitDrag}></div>
   <aside bind:this={asideEl}>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="browser" style="height: {browserExpandedHeight ?? `${(browserRatio * 100).toFixed(2)}%`}" onmouseenter={scheduleExpandBrowser} onmouseleave={collapseBrowser}>
      <div class="browser-path">
        <div class="head-left">
          <button title="refresh the directory listing" onclick={() => browse(browserPath)}>refresh</button>
          <span>/{browserPath}</span>
        </div>
        <div class="browser-actions">
          <button title="create a new file here" onclick={() => openNewEntry('file')}>new file</button>
          <button title="create a new directory here" onclick={() => openNewEntry('dir')}>new dir</button>
          <button class="danger" title="delete this directory and everything below it" disabled={!browserPath} onclick={deleteBrowserDir}>delete</button>
        </div>
      </div>
      <div class="browser-body" onscrollcapture={updateFades}>
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
            <button class="nav" title="back" disabled={viewerHistory.length === 0} onclick={() => viewerGo(-1)}>&#9664;</button>
            <button class="nav" title="forward" disabled={viewerFuture.length === 0} onclick={() => viewerGo(1)}>&#9654;</button>
            <button class="dl" title={plainView ? 'show rendered view' : 'show plain source'} disabled={!(isMarkdown(selectedFile.path) || isHtml(selectedFile.path)) || selectedFile.image || selectedFile.text === null} onclick={() => (plainView = !plainView)}>{plainView ? 'rendered' : 'plain'}</button>
            <span class="fname">{selectedFile.path}</span>
          </div>
          <div class="head-right">
            {#if editMode}
              <button class="dl" title="save file (Ctrl+S)" disabled={!editDirty || editSaving} onclick={() => saveEdit()}>save</button>
              <button class="dl" title="quit edit mode (Esc)" onclick={() => quitEdit()}>quit</button>
            {:else}
              <span class="diff-actions">
                <button class="dl" title="jump to previous diff block" disabled={!diffBlockCount || diffAtFirst} onclick={() => diffGo(-1)}>▲</button>
                <button class="dl" title="jump to next diff block" disabled={!diffBlockCount || diffAtLast} onclick={() => diffGo(1)}>▼</button>
                <button class="dl" class:active={diffView} title="toggle in-file diff against git HEAD" disabled={selectedFile.image || selectedFile.text === null} onclick={toggleDiff}>diff view</button>
                <button class="dl" class:active={wrapView} title="toggle soft-wrapping of long lines" disabled={selectedFile.image || selectedFile.text === null} onclick={() => (wrapView = !wrapView)}>wrap</button>
              </span>
              <span class="file-actions">
                <button class="dl" title="edit file" disabled={selectedFile.image || selectedFile.text === null || !!diffView || editSwitching} onclick={enterEdit}>edit</button>
                <a class="dl" href={`/download/${encPath(selectedFile.path)}`} download>download</a>
                <button class="dl danger" title="delete file from disk" onclick={deleteViewerFile}>delete</button>
              </span>
            {/if}
          </div>
        </div>
      {/if}
      <div class="viewer-body" onscrollcapture={() => { updateFades(); updateDiffNav() }}>
        <div class="fade fade-top" class:visible={fadeTop}></div>
        <div class="fade fade-bottom" class:visible={fadeBottom}></div>
        {#if diffLoading || viewerLoading || editSwitching}
          <div class="diff-loading"><span class="diff-spinner"></span></div>
        {/if}
        {#if rulerMarks.length}
          <div class="diffruler">
            {#each rulerMarks as m}
              <div class="mark {m.kind}" style="top:{(m.top * 100).toFixed(3)}%; height:{(m.height * 100).toFixed(3)}%"></div>
            {/each}
          </div>
        {/if}
        {#if editMode}
          <div class="filecontent editwrap">
            {#if editConflict}
              <div class="editbanner">
                <span>
                  {#if editConflict === 'changed'}
                    The file changed on disk while you were editing (e.g. an agent-side edit).
                  {:else if editConflict === 'save'}
                    Save conflict: the file on disk differs from the version you loaded.
                  {:else}
                    The file no longer exists on disk.
                  {/if}
                </span>
                <span class="eb-actions">
                  {#if editConflict === 'save'}
                    <button onclick={() => saveEdit(true)}>overwrite disk</button>
                  {/if}
                  {#if editConflict === 'gone'}
                    <button onclick={() => quitEdit({ force: true })}>discard draft</button>
                  {:else}
                    <button onclick={reloadEditFromDisk}>reload disk version</button>
                  {/if}
                  <button onclick={() => (editConflict = null)}>keep editing</button>
                </span>
              </div>
            {/if}
            <div class="editstack">
              <pre class="editunder hljs" aria-hidden="true"><code>{@html editUnderHtml}</code></pre>
              <textarea
                class="editarea"
                bind:this={editEl}
                bind:value={editText}
                onscroll={syncEditScroll}
                onkeydown={onEditKeydown}
                spellcheck="false"
                wrap="off"
              ></textarea>
            </div>
          </div>
        {:else if selectedFile && diffView}
          {#if diffView.error}
            <div class="filecontent binary">Diff failed: {diffView.error}</div>
          {:else if !diffView.diff}
            <div class="filecontent binary">No changes against git HEAD.</div>
          {:else if diffMetaOnly}
            <div class="filecontent binary">No content changes against git HEAD — only file metadata changed (e.g. permissions):<pre>{diffView.diff.trim()}</pre></div>
          {:else}
            <pre class="filecontent diff" class:wrap={wrapView}><code class="hljs">{@html renderDiff(diffView.diff, diffView.path)}</code></pre>
          {/if}
        {:else if selectedFile}
          {#if selectedFile.image}
            <div class="filecontent image">
              <img src={`/raw/${encPath(selectedFile.path)}?v=${selectedFile.v}`} alt={selectedFile.path} />
            </div>
          {:else if selectedFile.text !== null && isMarkdown(selectedFile.path) && !plainView}
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div class="filecontent md" onclick={(e) => onMdClick(e, selectedFile.path.split('/').slice(0, -1).join('/'))}>{@html renderMarkdown(selectedFile.text, selectedFile.path.split('/').slice(0, -1).join('/'))}</div>
          {:else if selectedFile.text !== null && isHtml(selectedFile.path) && !plainView}
            <div class="filecontent html">
              <iframe src={`/raw/${encPath(selectedFile.path)}?v=${selectedFile.v}`} sandbox="allow-scripts allow-forms" title={selectedFile.path}></iframe>
            </div>
          {:else if selectedFile.text !== null}
            <pre class="filecontent hljs" class:wrap={wrapView}><code>{@html highlight(selectedFile.text, selectedFile.path)}</code></pre>
          {:else}
            <div class="filecontent binary">No preview ({selectedFile.reason}) — use download.</div>
          {/if}
        {:else}
          <div class="filecontent placeholder">Select a file to preview it here.</div>
        {/if}
      </div>
    </div>
   </aside>
   {#if newEntryKind}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="np-overlay" role="presentation" onclick={(e) => e.target === e.currentTarget && (newEntryKind = null)}>
      <div class="np-modal ne-modal" role="dialog" aria-modal="true" aria-label="Create new {newEntryKind}">
        <div class="np-head">
          <span class="np-title">New {newEntryKind} in /{browserPath}</span>
          <button class="np-close" onclick={() => (newEntryKind = null)}>×</button>
        </div>
        <div class="np-actions">
          <input
            type="text"
            bind:this={newEntryInput}
            bind:value={newEntryName}
            placeholder="{newEntryKind} name"
            disabled={newEntryBusy}
            onkeydown={(e) => e.key === 'Enter' && createNewEntry()}
          />
          <button onclick={createNewEntry} disabled={!newEntryName.trim() || newEntryBusy}>
            {newEntryBusy ? 'Creating…' : 'Create'}
          </button>
        </div>
        {#if newEntryError}
          <div class="ne-error">{newEntryError}</div>
        {/if}
      </div>
    </div>
   {/if}
   <!-- Mobile messages view: full-screen list of the user's own
        messages; picking one switches to the chat and scrolls there.
        Hidden on desktop (the gap-dots popup serves this there). -->
   <div class="msgview">
     <div class="msgview-head">Messages</div>
     <div class="msgview-list">
       {#each userMessages as { e, i } (i)}
         <button class="msgnav-item" onclick={() => mobileJumpToMessage(i)}>{truncate(e.text)}</button>
       {:else}
         <div class="msgnav-empty">No messages yet.</div>
       {/each}
     </div>
   </div>
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
        <div class="np-actions">
          <input
            type="text"
            bind:value={npGitUrl}
            placeholder="git repo URL (https://… or git@…)"
            disabled={npCloning}
            onkeydown={(e) => e.key === 'Enter' && cloneProject()}
          />
          <button onclick={cloneProject} disabled={!npGitUrl.trim() || npCloning}>
            {npCloning ? 'Cloning…' : 'Clone'}
          </button>
        </div>
      </div>
    </div>
  {/if}

  <!-- Mobile navigation: floating hamburger opens a slide-in menu with
       the view links and the model/thinking selectors (which live in
       the chat toolbar on desktop). -->
  {#if isMobile}
    <button class="fab" aria-label="Open menu" aria-expanded={mobileMenuOpen} onclick={() => { mobileMenuOpen = !mobileMenuOpen; showNewProject = false; newEntryKind = null; slashDismissed = true }}>
      <span></span><span></span><span></span>
    </button>
    {#if mobileMenuOpen}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div class="mnav-backdrop" onclick={() => (mobileMenuOpen = false)}></div>
      <nav class="mnav" transition:slide={{ duration: 200, axis: 'x' }} aria-label="Views">
        <div class="mnav-links">
          {#each MOBILE_VIEWS as [v, label]}
            <button class="mnav-link" class:current={mobileView === v} onclick={() => setMobileView(v)}>{label}</button>
          {/each}
        </div>
        <div class="mnav-controls">
          <select class="model-select" value={currentModel ? `${currentModel.provider}::${currentModel.id}` : ''} onchange={onModelChange}>
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
        <!-- Session stats: on mobile they live here in the menu
             instead of the chat status bar, to save vertical space -->
        <div class="mnav-stats">
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
      </nav>
    {/if}
  {/if}
</main>
